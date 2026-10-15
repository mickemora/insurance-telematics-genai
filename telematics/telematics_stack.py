"""Telematics stack.

Day 1: synthetic data generation and rule-based scoring.
Day 2: score explainer API (API Gateway + Lambda + Bedrock + Guardrail).
Day 3: crash -> claims workflow (EventBridge + Step Functions + Bedrock).
Day 4: web app (CloudFront + S3, passcode) and an Amazon Bedrock Agent copilot.

Every stateful resource uses RemovalPolicy.DESTROY (and the bucket empties
itself) so `cdk destroy` leaves nothing behind in the account.
"""
from __future__ import annotations

import base64
import os
from pathlib import Path

from aws_cdk import (
    Annotations,
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_apigateway as apigw,
    aws_bedrock as bedrock,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_dynamodb as dynamodb,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_lambda_event_sources as lambda_events,
    aws_logs as logs,
    aws_s3 as s3,
    aws_s3_deployment as s3deploy,
    aws_s3_notifications as s3n,
    aws_secretsmanager as secretsmanager,
    aws_sqs as sqs,
    aws_stepfunctions as sfn,
    aws_stepfunctions_tasks as tasks,
)
from constructs import Construct

LAMBDA_DIR = Path(__file__).resolve().parent.parent / "lambdas"
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
EVENT_SOURCE = "telematics.scoring"
ASSET_EXCLUDE = ["**/__pycache__", "*.pyc"]
DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-5-5"
DEFAULT_STATEMENT_TIMEOUT_MINUTES = 30
PROFILE_PREFIXES = ("us", "eu", "apac", "global", "jp", "au", "ca", "us-gov")
DEFAULT_SITE_USER = "demo"
DEFAULT_SITE_PASSWORD = "telematics-demo"

AGENT_INSTRUCTION = """You are the Telematics Copilot for an auto insurer's member-service agents and claims adjusters. You answer questions about members' driving scores, trips, coaching explanations, and telematics-detected claims by calling the TelematicsData tools. All data is synthetic demo data.

How to work:
- Always look data up with the tools; never guess. If a driver or claim is not found, say so.
- Driver ids look like DRV-0001 and claim ids look like CLM-1A2B3C4D. If the user gives a name, \
call list_drivers to find the id.
- Be concise: lead with the answer, then 2-5 short bullets with the specific numbers you used. \
Always mention the driver or claim ids you looked at.
- For claims, report the facts, triage, statement check, and draft status. The AI draft is for \
an adjuster to review.

Never discuss insurance premiums, prices, discounts, or what a member pays. Never determine or \
suggest fault or liability, and never say whether a claim is covered, approved, or will be paid. \
You are read-only: you cannot change scores, claims, or member data."""

SITE_AUTH_JS = """function handler(event) {
  var request = event.request;
  var auth = request.headers.authorization;
  if (!auth || auth.value !== '%s') {
    return {
      statusCode: 401,
      statusDescription: 'Unauthorized',
      headers: { 'www-authenticate': { value: 'Basic realm="Telematics demo"' } }
    };
  }
  if (request.uri.indexOf('/api/') === 0) {
    request.uri = request.uri.substring(4);
  }
  return request;
}"""


def foundation_model_id(model_id: str) -> str:
    """'us.anthropic.claude-haiku-5-5' -> 'anthropic.claude-haiku-5-5'."""
    prefix, _, rest = model_id.partition(".")
    return rest if prefix in PROFILE_PREFIXES and rest else model_id


class TelematicsStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ------------------------------------------------------------------ storage
        raw_bucket = s3.Bucket(
            self,
            "RawTripsBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(90))],
        )

        # Single table, one partition per driver:
        #   sk = PROFILE                       driver profile
        #   sk = TRIP#<start_utc>#<trip_id>    scored trip + penalty breakdown
        #   sk = WEEK#<iso week>               weekly counters (score derived on read)
        table = dynamodb.Table(
            self,
            "TelematicsTable",
            partition_key=dynamodb.Attribute(name="driver_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="expires_at",  # copilot jobs expire after a day
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ------------------------------------------------------------------ queueing
        dlq = sqs.Queue(
            self,
            "TripScoringDLQ",
            retention_period=Duration.days(4),
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
        )
        scoring_timeout = Duration.seconds(60)
        trip_queue = sqs.Queue(
            self,
            "TripScoringQueue",
            visibility_timeout=Duration.seconds(scoring_timeout.to_seconds() * 6),
            enforce_ssl=True,
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=3, queue=dlq),
            removal_policy=RemovalPolicy.DESTROY,
        )
        raw_bucket.add_event_notification(
            s3.EventType.OBJECT_CREATED,
            s3n.SqsDestination(trip_queue),
            s3.NotificationKeyFilter(prefix="raw/trips/", suffix=".json"),
        )

        # ------------------------------------------------------------------ functions
        def log_group(name: str) -> logs.LogGroup:
            return logs.LogGroup(
                self,
                f"{name}Logs",
                retention=logs.RetentionDays.ONE_WEEK,
                removal_policy=RemovalPolicy.DESTROY,
            )

        common = dict(
            runtime=lambda_.Runtime.PYTHON_3_13,
            architecture=lambda_.Architecture.ARM_64,
            handler="handler.handler",
        )

        generator_fn = lambda_.Function(
            self,
            "TripGeneratorFn",
            code=lambda_.Code.from_asset(str(LAMBDA_DIR / "generator"), exclude=ASSET_EXCLUDE),
            memory_size=1024,
            timeout=Duration.minutes(10),
            description="Generates synthetic drivers and trips (raw/trips/ in S3, PROFILE items in DynamoDB)",
            environment={"RAW_BUCKET": raw_bucket.bucket_name, "TABLE_NAME": table.table_name},
            log_group=log_group("TripGenerator"),
            **common,
        )
        raw_bucket.grant_read_write(generator_fn)
        raw_bucket.grant_delete(generator_fn)
        table.grant_read_write_data(generator_fn)

        # score.py and facts.py, shared by the scoring and API functions
        shared_layer = lambda_.LayerVersion(
            self,
            "SharedLayer",
            code=lambda_.Code.from_asset(str(LAMBDA_DIR / "shared"), exclude=ASSET_EXCLUDE),
            compatible_runtimes=[lambda_.Runtime.PYTHON_3_13],
            compatible_architectures=[lambda_.Architecture.ARM_64],
            description="Scoring and facts modules shared by the telematics functions",
            removal_policy=RemovalPolicy.DESTROY,
        )

        scoring_fn = lambda_.Function(
            self,
            "TripScoringFn",
            code=lambda_.Code.from_asset(str(LAMBDA_DIR / "scoring"), exclude=ASSET_EXCLUDE),
            layers=[shared_layer],
            memory_size=512,
            timeout=scoring_timeout,
            description="Scores raw trips and maintains weekly driver aggregates",
            environment={
                "TABLE_NAME": table.table_name,
                "EVENT_BUS_NAME": "default",
                "EVENT_SOURCE": EVENT_SOURCE,
            },
            log_group=log_group("TripScoring"),
            **common,
        )
        raw_bucket.grant_read(scoring_fn)
        table.grant_read_write_data(scoring_fn)
        events.EventBus.from_event_bus_name(self, "DefaultBus", "default").grant_put_events_to(scoring_fn)
        scoring_fn.add_event_source(
            lambda_events.SqsEventSource(
                trip_queue,
                batch_size=10,
                max_batching_window=Duration.seconds(5),
                report_batch_item_failures=True,
            )
        )

        # ------------------------------------------------------------------ crash events
        # Logged for visibility; Day 3 also routes them into the claims workflow.
        crash_log = logs.LogGroup(
            self,
            "PossibleCrashEventsLogs",
            log_group_name=f"/aws/events/{self.stack_name}-possible-crash",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )
        crash_rule = events.Rule(
            self,
            "PossibleCrashRule",
            description="Captures PossibleCrashDetected events from the scoring pipeline",
            event_pattern=events.EventPattern(
                source=[EVENT_SOURCE], detail_type=["PossibleCrashDetected"]
            ),
            targets=[targets.CloudWatchLogGroup(crash_log)],
        )

        # ------------------------------------------------------------------ Day 2: guardrail
        guardrail = bedrock.CfnGuardrail(
            self,
            "CoachGuardrail",
            name=f"{self.stack_name}-coach",
            description="Keeps driving-coach output on driving habits: no pricing promises, fault, or legal advice",
            blocked_input_messaging="This request can't be processed by the driving coach.",
            blocked_outputs_messaging="The driving coach can't share that.",
            topic_policy_config=bedrock.CfnGuardrail.TopicPolicyConfigProperty(
                topics_config=[
                    bedrock.CfnGuardrail.TopicConfigProperty(
                        name="Insurance pricing",
                        type="DENY",
                        definition=(
                            "Statements or promises about insurance premiums, prices, discounts, savings, "
                            "refunds, or how driving will change what the member pays."
                        ),
                        examples=[
                            "Your premium will go up next renewal.",
                            "Keep this up and you'll save 20% on your policy.",
                            "This score lowers your insurance rate.",
                        ],
                    ),
                    bedrock.CfnGuardrail.TopicConfigProperty(
                        name="Fault and legal advice",
                        type="DENY",
                        definition=(
                            "Determining who is at fault for an accident, liability, or giving legal advice."
                        ),
                        examples=[
                            "You were at fault for the accident.",
                            "You are liable for the damage.",
                            "You should talk to a lawyer about this.",
                        ],
                    ),
                ]
            ),
            content_policy_config=bedrock.CfnGuardrail.ContentPolicyConfigProperty(
                filters_config=[
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        type=t, input_strength="HIGH", output_strength="HIGH")
                    for t in ("HATE", "INSULTS", "SEXUAL", "VIOLENCE", "MISCONDUCT")
                ]
                + [bedrock.CfnGuardrail.ContentFilterConfigProperty(
                    type="PROMPT_ATTACK", input_strength="HIGH", output_strength="NONE")]
            ),
        )
        guardrail_version = bedrock.CfnGuardrailVersion(
            self,
            "CoachGuardrailVersion",
            guardrail_identifier=guardrail.attr_guardrail_id,
            description="Published version used by the explainer API",
        )

        # ------------------------------------------------------------------ Day 2: API
        model_id = self.node.try_get_context("bedrock_model_id") or DEFAULT_MODEL_ID

        api_fn = lambda_.Function(
            self,
            "DriverApiFn",
            code=lambda_.Code.from_asset(str(LAMBDA_DIR / "api"), exclude=ASSET_EXCLUDE),
            layers=[shared_layer],
            memory_size=512,
            timeout=Duration.seconds(29),
            description="Driver API: scores and gen AI score explanations",
            environment={
                "TABLE_NAME": table.table_name,
                "MODEL_ID": model_id,
                "GUARDRAIL_ID": guardrail.attr_guardrail_id,
                "GUARDRAIL_VERSION": guardrail_version.attr_version,
            },
            log_group=log_group("DriverApi"),
            **common,
        )
        table.grant_read_write_data(api_fn)

        def allow_claude(fn: lambda_.Function) -> None:
            """Invoke exactly one model (via its inference profile) and apply the guardrail."""
            fn.add_to_role_policy(iam.PolicyStatement(
                sid="InvokeClaude",
                actions=["bedrock:InvokeModel"],
                resources=[
                    # cross-region inference profile in this account/region ...
                    self.format_arn(service="bedrock", resource="inference-profile", resource_name=model_id),
                    # ... and the foundation model in whichever region the profile routes to
                    f"arn:{self.partition}:bedrock:*::foundation-model/{foundation_model_id(model_id)}*",
                ],
            ))
            fn.add_to_role_policy(iam.PolicyStatement(
                sid="ApplyCoachGuardrail",
                actions=["bedrock:ApplyGuardrail"],
                resources=[guardrail.attr_guardrail_arn, f"{guardrail.attr_guardrail_arn}:*"],
            ))

        allow_claude(api_fn)

        api = apigw.RestApi(
            self,
            "DriverApi",
            rest_api_name=f"{self.stack_name}-driver-api",
            description="Telematics demo: driver scores and AI explanations",
            cloud_watch_role=False,  # avoid an account-level role that outlives the stack
            deploy_options=apigw.StageOptions(
                stage_name="v1",
                throttling_rate_limit=5,
                throttling_burst_limit=10,
            ),
            default_method_options=apigw.MethodOptions(api_key_required=True),
            endpoint_types=[apigw.EndpointType.REGIONAL],
        )
        integration = apigw.LambdaIntegration(api_fn)
        drivers = api.root.add_resource("drivers")
        drivers.add_method("GET", integration)
        driver = drivers.add_resource("{driver_id}")
        driver.add_resource("score").add_method("GET", integration)
        driver.add_resource("explanation").add_method("GET", integration)

        # The key value lives in Secrets Manager so CloudFront can add it for the web app (Day 4)
        # without it ever reaching the browser.
        api_key_secret = secretsmanager.Secret(
            self,
            "ApiKeySecret",
            description="API key used by CloudFront and scripts/demo.py for the telematics demo API",
            generate_secret_string=secretsmanager.SecretStringGenerator(exclude_punctuation=True, password_length=40),
            removal_policy=RemovalPolicy.DESTROY,
        )
        api_key = api.add_api_key("SiteApiKey", api_key_name=f"{self.stack_name}-key",
                                  value=api_key_secret.secret_value.unsafe_unwrap())
        plan = api.add_usage_plan(
            "DemoUsagePlan",
            name=f"{self.stack_name}-demo-plan",
            throttle=apigw.ThrottleSettings(rate_limit=5, burst_limit=10),
            quota=apigw.QuotaSettings(limit=2000, period=apigw.Period.DAY),
        )
        plan.add_api_key(api_key)
        plan.add_api_stage(stage=api.deployment_stage)

        # ------------------------------------------------------------------ Day 3: claims workflow
        statement_timeout = int(self.node.try_get_context("statement_timeout_minutes")
                                or DEFAULT_STATEMENT_TIMEOUT_MINUTES)
        claims_code = lambda_.Code.from_asset(str(LAMBDA_DIR / "claims"), exclude=ASSET_EXCLUDE)
        claims_env = {"TABLE_NAME": table.table_name}

        def claims_fn(name: str, handler: str, description: str, timeout_s: int = 30,
                      env: dict | None = None) -> lambda_.Function:
            return lambda_.Function(
                self,
                name,
                code=claims_code,
                handler=handler,
                runtime=lambda_.Runtime.PYTHON_3_13,
                architecture=lambda_.Architecture.ARM_64,
                layers=[shared_layer],
                memory_size=512,
                timeout=Duration.seconds(timeout_s),
                description=description,
                environment={**claims_env, **(env or {})},
                log_group=log_group(name.removesuffix("Fn")),
            )

        enrich_fn = claims_fn("ClaimEnrichFn", "handlers.enrich",
                              "Claims: crash facts from the raw trip, street address, triage",
                              env={"RAW_BUCKET": raw_bucket.bucket_name})
        raw_bucket.grant_read(enrich_fn)
        table.grant_read_write_data(enrich_fn)
        enrich_fn.add_to_role_policy(iam.PolicyStatement(
            sid="ReverseGeocode",
            actions=["geo-places:ReverseGeocode"],
            resources=[f"arn:{self.partition}:geo-places:{self.region}::provider/default"],
        ))

        false_alarm_fn = claims_fn("ClaimFalseAlarmFn", "handlers.close_false_alarm",
                                   "Claims: close a likely false alarm with a member message")
        table.grant_read_write_data(false_alarm_fn)

        checkin_fn = claims_fn("ClaimCheckinFn", "handlers.checkin",
                               "Claims: ask the member for a statement and park the task token",
                               env={"STATEMENT_TIMEOUT_MINUTES": str(statement_timeout)})
        table.grant_read_write_data(checkin_fn)

        draft_fn = claims_fn("ClaimDraftFn", "handlers.draft", "Claims: FNOL draft with Claude on Bedrock",
                             timeout_s=120,
                             env={"MODEL_ID": model_id, "GUARDRAIL_ID": guardrail.attr_guardrail_id,
                                  "GUARDRAIL_VERSION": guardrail_version.attr_version})
        table.grant_read_write_data(draft_fn)
        allow_claude(draft_fn)

        # execution_id lets a task ignore a claim that a newer run (same trip, after a reset) now owns
        claim_id = {"claim_id": sfn.JsonPath.string_at("$.claim_id"),
                    "execution_id": sfn.JsonPath.string_at("$$.Execution.Id")}
        enrich_task = tasks.LambdaInvoke(
            self, "Enrich crash data",
            lambda_function=enrich_fn,
            payload=sfn.TaskInput.from_object({
                "detail": sfn.JsonPath.object_at("$.detail"),
                "execution_id": sfn.JsonPath.string_at("$$.Execution.Id"),
            }),
            payload_response_only=True,
        )
        false_alarm_task = tasks.LambdaInvoke(
            self, "Close as false alarm",
            lambda_function=false_alarm_fn,
            payload=sfn.TaskInput.from_object(claim_id),
            payload_response_only=True,
        )
        checkin_task = tasks.LambdaInvoke(
            self, "Ask member for statement",
            lambda_function=checkin_fn,
            integration_pattern=sfn.IntegrationPattern.WAIT_FOR_TASK_TOKEN,
            payload=sfn.TaskInput.from_object({"task_token": sfn.JsonPath.task_token, **claim_id}),
            result_path="$.member",
            task_timeout=sfn.Timeout.duration(Duration.minutes(statement_timeout)),
        )
        draft_task = tasks.LambdaInvoke(
            self, "Draft FNOL with Claude",
            lambda_function=draft_fn,
            payload=sfn.TaskInput.from_object(claim_id),
            payload_response_only=True,
        )
        # No statement in time? Draft anyway; the adjuster sees "no statement".
        checkin_task.add_catch(draft_task, errors=["States.Timeout"], result_path="$.member_timeout")

        definition = enrich_task.next(
            sfn.Choice(self, "Triage")
            .when(sfn.Condition.boolean_equals("$.duplicate", True), sfn.Succeed(self, "Already handled"))
            .when(sfn.Condition.string_equals("$.triage", "likely_false_alarm"),
                  false_alarm_task.next(sfn.Succeed(self, "Closed: false alarm")))
            .otherwise(checkin_task.next(draft_task.next(sfn.Succeed(self, "Draft ready for adjuster"))))
        )
        claims_machine = sfn.StateMachine(
            self,
            "ClaimsWorkflow",
            state_machine_name=f"{self.stack_name}-claims",
            definition_body=sfn.DefinitionBody.from_chainable(definition),
            state_machine_type=sfn.StateMachineType.STANDARD,
            timeout=Duration.minutes(statement_timeout + 30),
            logs=sfn.LogOptions(destination=log_group("ClaimsWorkflow"), level=sfn.LogLevel.ALL),
        )
        crash_rule.add_target(targets.SfnStateMachine(claims_machine))
        claims_machine.grant_task_response(api_fn)

        claims = api.root.add_resource("claims")
        claims.add_method("GET", integration)
        claim = claims.add_resource("{claim_id}")
        claim.add_method("GET", integration)
        claim.add_resource("statement").add_method("POST", integration)

        # ------------------------------------------------------------------ Day 4: Bedrock Agent copilot
        agent_tools_fn = lambda_.Function(
            self,
            "AgentToolsFn",
            code=lambda_.Code.from_asset(str(LAMBDA_DIR / "api"), exclude=ASSET_EXCLUDE),
            handler="agent_tools.handler",
            runtime=lambda_.Runtime.PYTHON_3_13,
            architecture=lambda_.Architecture.ARM_64,
            layers=[shared_layer],
            memory_size=512,
            timeout=Duration.seconds(60),
            description="Copilot agent action group: read-only telematics and claims tools",
            environment={
                "TABLE_NAME": table.table_name,
                "MODEL_ID": model_id,
                "GUARDRAIL_ID": guardrail.attr_guardrail_id,
                "GUARDRAIL_VERSION": guardrail_version.attr_version,
            },
            log_group=log_group("AgentTools"),
        )
        table.grant_read_write_data(agent_tools_fn)  # write = explanation cache only
        allow_claude(agent_tools_fn)

        agent_role = iam.Role(
            self,
            "CopilotAgentRole",
            role_name=f"AmazonBedrockExecutionRoleForAgents_{self.stack_name}"[:64],
            assumed_by=iam.ServicePrincipal(
                "bedrock.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": self.account},
                    "ArnLike": {"aws:SourceArn": f"arn:{self.partition}:bedrock:{self.region}:{self.account}:agent/*"},
                },
            ),
            description="Execution role for the telematics copilot Bedrock Agent",
        )
        profile_arn = self.format_arn(service="bedrock", resource="inference-profile", resource_name=model_id)
        agent_role.add_to_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
            resources=[profile_arn,
                       f"arn:{self.partition}:bedrock:*::foundation-model/{foundation_model_id(model_id)}*"],
        ))
        agent_role.add_to_policy(iam.PolicyStatement(
            actions=["bedrock:GetInferenceProfile"], resources=[profile_arn]))
        agent_role.add_to_policy(iam.PolicyStatement(
            actions=["bedrock:ApplyGuardrail", "bedrock:GetGuardrail"],
            resources=[guardrail.attr_guardrail_arn, f"{guardrail.attr_guardrail_arn}:*"],
        ))

        def tool(name: str, description: str, **params: tuple[str, str, bool]):
            return bedrock.CfnAgent.FunctionProperty(
                name=name,
                description=description,
                parameters={p: bedrock.CfnAgent.ParameterDetailProperty(type=t, description=d, required=r)
                            for p, (t, d, r) in params.items()} or None,
            )

        driver_param = ("string", "Driver id, for example DRV-0020", True)
        agent = bedrock.CfnAgent(
            self,
            "CopilotAgent",
            agent_name=f"{self.stack_name}-copilot",
            description="Read-only copilot over telematics scores, trips, explanations, and claims",
            agent_resource_role_arn=agent_role.role_arn,
            foundation_model=model_id,
            instruction=AGENT_INSTRUCTION,
            idle_session_ttl_in_seconds=900,
            auto_prepare=True,
            skip_resource_in_use_check_on_delete=True,
            guardrail_configuration=bedrock.CfnAgent.GuardrailConfigurationProperty(
                guardrail_identifier=guardrail.attr_guardrail_id,
                guardrail_version=guardrail_version.attr_version,
            ),
            action_groups=[bedrock.CfnAgent.AgentActionGroupProperty(
                action_group_name="TelematicsData",
                description="Read-only lookups over drivers, trips, score explanations, and claims",
                action_group_state="ENABLED",
                action_group_executor=bedrock.CfnAgent.ActionGroupExecutorProperty(lambda_=agent_tools_fn.function_arn),
                function_schema=bedrock.CfnAgent.FunctionSchemaProperty(functions=[
                    tool("list_drivers", "List every driver with id, name, metro, latest weekly score, band, "
                                         "and change versus the previous week."),
                    tool("get_driver_score", "Weekly scores for one driver, with the points lost per factor.",
                         driver_id=driver_param),
                    tool("get_driver_trips", "A driver's most recent trips (newest first) with score, miles, "
                                             "points lost per factor, and event counts.",
                         driver_id=driver_param,
                         limit=("integer", "How many trips, 1 to 25 (default 10)", False)),
                    tool("get_score_explanation", "The plain-English explanation and coaching tips for a "
                                                  "driver's latest weekly score.", driver_id=driver_param),
                    tool("list_claims", "Claims opened from telematics crash detection, newest first.",
                         status=("string", "Optional filter: awaiting_statement, drafting, "
                                           "draft_ready_for_adjuster, or closed_false_alarm", False)),
                    tool("get_claim", "One claim: crash facts, triage, member statement, statement checks, "
                                      "and the FNOL draft.",
                         claim_id=("string", "Claim id, for example CLM-1A2B3C4D", True)),
                ]),
            )],
        )
        agent.node.add_dependency(agent_role)  # the role's policy must exist before the agent is prepared
        agent_tools_fn.add_permission(
            "AllowCopilotAgent",
            principal=iam.ServicePrincipal("bedrock.amazonaws.com"),
            action="lambda:InvokeFunction",
            source_arn=agent.attr_agent_arn,
            source_account=self.account,
        )

        copilot_worker_fn = lambda_.Function(
            self,
            "CopilotWorkerFn",
            code=lambda_.Code.from_asset(str(LAMBDA_DIR / "api"), exclude=ASSET_EXCLUDE),
            handler="copilot_worker.handler",
            runtime=lambda_.Runtime.PYTHON_3_13,
            architecture=lambda_.Architecture.ARM_64,
            memory_size=256,
            timeout=Duration.minutes(3),
            description="Copilot: runs a question through the Bedrock Agent and stores the answer",
            # TSTALIASID is the built-in alias for the agent's working draft, which auto_prepare keeps
            # current on every deploy (a versioned alias would need a new version per change).
            environment={"TABLE_NAME": table.table_name, "AGENT_ID": agent.attr_agent_id,
                         "AGENT_ALIAS_ID": "TSTALIASID"},
            log_group=log_group("CopilotWorker"),
        )
        table.grant_read_write_data(copilot_worker_fn)
        copilot_worker_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeAgent"],
            resources=[f"arn:{self.partition}:bedrock:{self.region}:{self.account}:agent-alias/"
                       f"{agent.attr_agent_id}/*"],
        ))
        copilot_worker_fn.grant_invoke(api_fn)
        api_fn.add_environment("COPILOT_WORKER_FUNCTION", copilot_worker_fn.function_name)

        copilot = api.root.add_resource("copilot")
        copilot.add_method("POST", integration)
        copilot.add_resource("{job_id}").add_method("GET", integration)

        # ------------------------------------------------------------------ Day 4: web app
        site_user = self.node.try_get_context("site_user") or DEFAULT_SITE_USER
        site_password = self.node.try_get_context("site_password") or os.environ.get("SITE_PASSWORD")
        if not site_password:
            site_password = DEFAULT_SITE_PASSWORD
            Annotations.of(self).add_warning(
                "Using the default web passcode. Set your own: cdk deploy -c site_password=...")
        basic = base64.b64encode(f"{site_user}:{site_password}".encode()).decode()

        site_bucket = s3.Bucket(
            self,
            "SiteBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        site_auth = cloudfront.Function(
            self,
            "SiteAuthFn",
            code=cloudfront.FunctionCode.from_inline(SITE_AUTH_JS % f"Basic {basic}"),
            runtime=cloudfront.FunctionRuntime.JS_2_0,
            comment="Passcode for the telematics demo and /api prefix rewrite",
        )
        protect = [cloudfront.FunctionAssociation(function=site_auth,
                                                  event_type=cloudfront.FunctionEventType.VIEWER_REQUEST)]
        api_origin_policy = cloudfront.OriginRequestPolicy(
            self,
            "ApiOriginRequestPolicy",
            comment="Forward query strings and Content-Type only (no Host, no Authorization)",
            header_behavior=cloudfront.OriginRequestHeaderBehavior.allow_list("Content-Type"),
            query_string_behavior=cloudfront.OriginRequestQueryStringBehavior.all(),
            cookie_behavior=cloudfront.OriginRequestCookieBehavior.none(),
        )
        distribution = cloudfront.Distribution(
            self,
            "SiteDistribution",
            comment=f"{self.stack_name} demo web app",
            default_root_object="index.html",
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(site_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,  # always serve the latest demo build
                function_associations=protect,
            ),
            additional_behaviors={
                "/api/*": cloudfront.BehaviorOptions(
                    origin=origins.RestApiOrigin(
                        api, custom_headers={"x-api-key": api_key_secret.secret_value.unsafe_unwrap()}),
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                    cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                    origin_request_policy=api_origin_policy,
                    function_associations=protect,
                ),
            },
        )
        s3deploy.BucketDeployment(
            self,
            "DeploySite",
            sources=[s3deploy.Source.asset(str(WEB_DIR))],
            destination_bucket=site_bucket,
            distribution=distribution,
            distribution_paths=["/*"],
        )

        # ------------------------------------------------------------------ outputs
        CfnOutput(self, "RawBucketName", value=raw_bucket.bucket_name)
        CfnOutput(self, "TableName", value=table.table_name)
        CfnOutput(self, "GeneratorFunctionName", value=generator_fn.function_name)
        CfnOutput(self, "ScoringFunctionName", value=scoring_fn.function_name)
        CfnOutput(self, "TripQueueUrl", value=trip_queue.queue_url)
        CfnOutput(self, "DeadLetterQueueUrl", value=dlq.queue_url)
        CfnOutput(self, "CrashEventsLogGroup", value=crash_log.log_group_name)
        CfnOutput(self, "ApiUrl", value=api.url)
        CfnOutput(self, "ApiKeyId", value=api_key.key_id,
                  description="aws apigateway get-api-key --include-value --api-key <id>")
        CfnOutput(self, "ApiFunctionName", value=api_fn.function_name)
        CfnOutput(self, "BedrockModelId", value=model_id)
        CfnOutput(self, "GuardrailId", value=guardrail.attr_guardrail_id)
        CfnOutput(self, "ClaimsStateMachineArn", value=claims_machine.state_machine_arn)
        CfnOutput(self, "SiteUrl", value=f"https://{distribution.distribution_domain_name}")
        CfnOutput(self, "SiteUser", value=site_user)
        CfnOutput(self, "CopilotAgentId", value=agent.attr_agent_id)
