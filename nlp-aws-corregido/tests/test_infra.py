import json
import sys
from pathlib import Path

from aws_cdk import App, Environment
from aws_cdk.assertions import Match, Template

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "infra"))
from app import AuditStack
from webapp_stack import WebappStack

_OPENAI_SECRET_ARN = "arn:aws:secretsmanager:us-east-1:111111111111:secret:nlp-audit/openai-ABCDEF"


def test_private_storage_and_bounded_worker():
    app = App(context={"openaiSecretArn": _OPENAI_SECRET_ARN})
    stack = AuditStack(app, "TestAudit", env=Environment(
        account="111111111111", region="us-east-1"))
    template = Template.from_stack(stack)
    template.resource_count_is("AWS::S3::Bucket", 2)
    template.all_resources_properties("AWS::S3::Bucket", {
        "PublicAccessBlockConfiguration": {
            "BlockPublicAcls": True, "BlockPublicPolicy": True,
            "IgnorePublicAcls": True, "RestrictPublicBuckets": True}})
    template.has_resource_properties("AWS::Lambda::Function", {
        "Timeout": 900, "MemorySize": 2048, "PackageType": "Image",
        "Environment": {"Variables": Match.object_like({"AUDIT_MODEL": "gpt-4o"})}})
    template.resource_count_is("AWS::EC2::NatGateway", 0)
    template.resource_count_is("AWS::Lambda::Url", 0)


def _webapp_stack():
    app = App(context={
        "openaiSecretArn": _OPENAI_SECRET_ARN,  # AuditStack.__init__ corre igual al sintetizar la app
        "auditFunctionName": "NlpAuditStack-Worker-TEST",
        "auditInputBucket": "test-input-bucket",
        "auditResultBucket": "test-result-bucket",
    })
    AuditStack(app, "TestAudit", env=Environment(account="111111111111", region="us-east-1"))
    stack = WebappStack(app, "TestWebapp", env=Environment(account="111111111111", region="us-east-1"))
    return Template.from_stack(stack)


def test_webapp_stack_has_bounded_concurrency_and_no_new_public_surface():
    template = _webapp_stack()
    # El equivalente al threading.Semaphore(3) de webapp/webapp-pdf, pero
    # aplicado a nivel de infraestructura (varias instancias del worker
    # pueden correr a la vez; esto sí las limita a todas juntas).
    template.has_resource_properties("AWS::Lambda::EventSourceMapping",
        {"ScalingConfig": {"MaximumConcurrency": 3}})
    template.resource_count_is("AWS::DynamoDB::GlobalTable", 1)
    template.has_resource_properties("AWS::DynamoDB::GlobalTable",
        Match.object_like({"TimeToLiveSpecification": {"AttributeName": "ttl", "Enabled": True}}))
    template.resource_count_is("AWS::SQS::Queue", 2)  # cola + DLQ
    template.has_resource_properties("AWS::SQS::Queue",
        Match.object_like({"RedrivePolicy": Match.object_like({"maxReceiveCount": 2})}))
    template.has_resource_properties("AWS::Lambda::Function",
        Match.object_like({"Timeout": 29, "PackageType": "Image",
            "Environment": {"Variables": Match.object_like({"MAX_AUDIT_USES": "10"})}}))  # ApiFn: límite real de HTTP API + tope de usos por default
    template.has_resource_properties("AWS::Lambda::Function",
        Match.object_like({"Timeout": 900, "PackageType": "Image"}))  # QueueWorkerFn
    template.resource_count_is("AWS::EC2::NatGateway", 0)
    template.resource_count_is("AWS::S3::Bucket", 0)  # no crea buckets propios, reusa los de AuditStack
    template.resource_count_is("AWS::CloudFront::Distribution", 0)


def test_queue_worker_invoke_permission_uses_colon_arn_format():
    """
    Regression test: Stack.format_arn() defaults to "resource/name" (slash),
    but Lambda function ARNs are "resource:name" (colon). Using the default
    silently signs QueueWorkerFn's IAM policy against an ARN that never
    matches the real function, and it fails at runtime with
    AccessDeniedException on lambda:InvokeFunction -- CDK synth stays green
    the whole time because the ARN is syntactically valid, just wrong.
    Caught this exact bug in a real deployment; this pins the fix.
    """
    policy_json = json.dumps(_webapp_stack().find_resources("AWS::IAM::Policy"))
    assert ":function:NlpAuditStack-Worker-TEST" in policy_json
    assert ":function/NlpAuditStack-Worker-TEST" not in policy_json


def test_webapp_stack_requires_audit_stack_context():
    app = App(context={"openaiSecretArn": _OPENAI_SECRET_ARN})
    try:
        WebappStack(app, "TestWebapp", env=Environment(account="111111111111", region="us-east-1"))
        raised = False
    except ValueError:
        raised = True
    assert raised, "WebappStack debe exigir auditFunctionName/auditInputBucket/auditResultBucket"
