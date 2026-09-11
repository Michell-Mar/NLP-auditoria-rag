import sys
from pathlib import Path

from aws_cdk import App, Environment
from aws_cdk.assertions import Match, Template

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "infra"))
from app import AuditStack


def test_private_storage_and_bounded_worker():
    app = App(context={"openaiSecretArn":
        "arn:aws:secretsmanager:us-east-1:111111111111:secret:nlp-audit/openai-ABCDEF"})
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
