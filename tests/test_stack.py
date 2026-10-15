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


def test_api_requires_key_and_has_three_routes(template):
    methods = template.find_resources("AWS::ApiGateway::Method")
    assert len(methods) == 3
    for m in methods.values():
        assert m["Properties"]["HttpMethod"] == "GET"
        assert m["Properties"]["ApiKeyRequired"] is True
    template.resource_count_is("AWS::ApiGateway::ApiKey", 1)
    template.has_resource_properties("AWS::ApiGateway::UsagePlan", {
        "Throttle": {"RateLimit": 5, "BurstLimit": 10}})
    template.resource_count_is("AWS::ApiGateway::Account", 0)  # no account-level role left behind


def test_guardrail_denies_pricing_and_fault_topics(template):
    template.resource_count_is("AWS::Bedrock::Guardrail", 1)
    template.resource_count_is("AWS::Bedrock::GuardrailVersion", 1)
    topics = list(template.find_resources("AWS::Bedrock::Guardrail").values())[0][
        "Properties"]["TopicPolicyConfig"]["TopicsConfig"]
    assert {t["Name"] for t in topics} == {"Insurance pricing", "Fault and legal advice"}
    assert all(t["Type"] == "DENY" for t in topics)


def test_api_function_scoped_to_model_and_guardrail(template):
    template.has_resource_properties("AWS::Lambda::Function", {
        "Environment": {"Variables": Match.object_like({"MODEL_ID": "us.anthropic.claude-haiku-5-5"})}})
    policies = template.find_resources("AWS::IAM::Policy")
    statements = [s for p in policies.values() for s in p["Properties"]["PolicyDocument"]["Statement"]]
    invoke = [s for s in statements if s.get("Action") == "bedrock:InvokeModel"]
    assert invoke, "API function must be allowed to invoke the model"
    flat = str(invoke[0]["Resource"])
    assert "inference-profile/us.anthropic.claude-haiku-5-5" in flat
    assert "foundation-model/anthropic.claude-haiku-5-5*" in flat
    assert any(s.get("Action") == "bedrock:ApplyGuardrail" for s in statements)


def test_model_can_be_overridden_by_context():
    app = cdk.App(context={"bedrock_model_id": "us.anthropic.claude-sonnet-5-5"})
    t = Template.from_stack(TelematicsStack(app, "SonnetStack"))
    t.has_resource_properties("AWS::Lambda::Function", {
        "Environment": {"Variables": Match.object_like({"MODEL_ID": "us.anthropic.claude-sonnet-5-5"})}})


def test_shared_layer_attached_to_scoring_and_api(template):
    template.resource_count_is("AWS::Lambda::LayerVersion", 1)
    with_layers = [f for f in template.find_resources("AWS::Lambda::Function").values()
                   if f["Properties"].get("Layers")]
    assert len(with_layers) == 2


def test_foundation_model_id():
    from telematics.telematics_stack import foundation_model_id
    assert foundation_model_id("us.anthropic.claude-haiku-5-5") == "anthropic.claude-haiku-5-5"
    assert foundation_model_id("global.anthropic.claude-sonnet-5-5") == "anthropic.claude-sonnet-5-5"
    assert foundation_model_id("anthropic.claude-haiku-5-5") == "anthropic.claude-haiku-5-5"
