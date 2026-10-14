"""CDK template assertions: the stack must be fully destroyable."""
import pytest

cdk = pytest.importorskip("aws_cdk")
from aws_cdk.assertions import Match, Template  # noqa: E402

from telematics.telematics_stack import TelematicsStack  # noqa: E402


@pytest.fixture(scope="module")
def template():
    app = cdk.App()
    return Template.from_stack(TelematicsStack(app, "TestStack"))


def test_bucket_is_private_and_auto_deletes(template):
    template.resource_count_is("AWS::S3::Bucket", 1)
    template.has_resource("AWS::S3::Bucket", {"DeletionPolicy": "Delete", "UpdateReplacePolicy": "Delete"})
    template.has_resource_properties("AWS::S3::Bucket", {
        "PublicAccessBlockConfiguration": {
            "BlockPublicAcls": True, "BlockPublicPolicy": True,
            "IgnorePublicAcls": True, "RestrictPublicBuckets": True,
        }
    })
    template.resource_count_is("Custom::S3AutoDeleteObjects", 1)


def test_no_retained_resources(template):
    for logical_id, res in template.to_json()["Resources"].items():
        assert res.get("DeletionPolicy", "Delete") == "Delete", logical_id


def test_table_keys(template):
    template.has_resource_properties("AWS::DynamoDB::Table", {
        "KeySchema": [{"AttributeName": "driver_id", "KeyType": "HASH"},
                      {"AttributeName": "sk", "KeyType": "RANGE"}],
        "BillingMode": "PAY_PER_REQUEST",
    })


def test_functions(template):
    template.has_resource_properties("AWS::Lambda::Function", {
        "Runtime": "python3.13", "Architectures": ["arm64"], "Timeout": 600})
    template.has_resource_properties("AWS::Lambda::EventSourceMapping", {
        "BatchSize": 10, "FunctionResponseTypes": ["ReportBatchItemFailures"]})


def test_queue_has_dlq(template):
    template.has_resource_properties("AWS::SQS::Queue", {
        "RedrivePolicy": {"maxReceiveCount": 3, "deadLetterTargetArn": Match.any_value()}})


def test_crash_rule(template):
    template.has_resource_properties("AWS::Events::Rule", {
        "EventPattern": {"source": ["telematics.scoring"], "detail-type": ["PossibleCrashDetected"]}})
