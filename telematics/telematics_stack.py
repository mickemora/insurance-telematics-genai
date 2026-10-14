"""Day 1 stack: synthetic data generation and rule-based scoring.

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
    aws_dynamodb as dynamodb,
    aws_events as events,
    aws_events_targets as targets,
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
            code=lambda_.Code.from_asset(str(LAMBDA_DIR / "generator")),
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

        scoring_fn = lambda_.Function(
            self,
            "TripScoringFn",
            code=lambda_.Code.from_asset(str(LAMBDA_DIR / "scoring")),
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

        # ------------------------------------------------------------------ outputs
        CfnOutput(self, "RawBucketName", value=raw_bucket.bucket_name)
        CfnOutput(self, "TableName", value=table.table_name)
        CfnOutput(self, "GeneratorFunctionName", value=generator_fn.function_name)
        CfnOutput(self, "ScoringFunctionName", value=scoring_fn.function_name)
        CfnOutput(self, "TripQueueUrl", value=trip_queue.queue_url)
        CfnOutput(self, "DeadLetterQueueUrl", value=dlq.queue_url)
        CfnOutput(self, "CrashEventsLogGroup", value=crash_log.log_group_name)
