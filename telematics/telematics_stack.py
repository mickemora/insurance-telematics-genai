"""Telematics stack.

Day 1: synthetic data generation and rule-based scoring.
Day 2: score explainer API (API Gateway + Lambda + Bedrock + Guardrail).

Every stateful resource uses RemovalPolicy.DESTROY (and the bucket empties
itself) so `cdk destroy` leaves nothing behind in the account.
"""
from __future__ import annotations

from pathlib import Path

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_apigateway as apigw,
    aws_bedrock as bedrock,
    aws_dynamodb as dynamodb,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_lambda_event_sources as lambda_events,
    aws_logs as logs,
    aws_s3 as s3,
    aws_s3_notifications as s3n,
    aws_sqs as sqs,
)
from constructs import Construct

LAMBDA_DIR = Path(__file__).resolve().parent.parent / "lambdas"
EVENT_SOURCE = "telematics.scoring"
ASSET_EXCLUDE = ["**/__pycache__", "*.pyc"]
DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-5-5"
PROFILE_PREFIXES = ("us", "eu", "apac", "global", "jp", "au", "ca", "us-gov")


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
        # Day 3 will route these into a claims workflow. For now they land in a
        # log group so you can see them arrive.
        crash_log = logs.LogGroup(
            self,
            "PossibleCrashEventsLogs",
            log_group_name=f"/aws/events/{self.stack_name}-possible-crash",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )
        events.Rule(
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
        api_fn.add_to_role_policy(iam.PolicyStatement(
            sid="InvokeClaude",
            actions=["bedrock:InvokeModel"],
            resources=[
                # cross-region inference profile in this account/region ...
                self.format_arn(service="bedrock", resource="inference-profile", resource_name=model_id),
                # ... and the foundation model in whichever region the profile routes to
                f"arn:{self.partition}:bedrock:*::foundation-model/{foundation_model_id(model_id)}*",
            ],
        ))
        api_fn.add_to_role_policy(iam.PolicyStatement(
            sid="ApplyCoachGuardrail",
            actions=["bedrock:ApplyGuardrail"],
            resources=[guardrail.attr_guardrail_arn, f"{guardrail.attr_guardrail_arn}:*"],
        ))

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

        api_key = api.add_api_key("DemoApiKey", api_key_name=f"{self.stack_name}-demo-key")
        plan = api.add_usage_plan(
            "DemoUsagePlan",
            name=f"{self.stack_name}-demo-plan",
            throttle=apigw.ThrottleSettings(rate_limit=5, burst_limit=10),
            quota=apigw.QuotaSettings(limit=2000, period=apigw.Period.DAY),
        )
        plan.add_api_key(api_key)
        plan.add_api_stage(stage=api.deployment_stage)

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
