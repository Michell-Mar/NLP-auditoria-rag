import os
from pathlib import Path

from aws_cdk import App, CfnOutput, Duration, Environment, RemovalPolicy, Size, Stack
from aws_cdk import aws_ecr_assets as assets
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_secretsmanager as secrets
from constructs import Construct

from webapp_stack import WebappStack


class AuditStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs):
        super().__init__(scope, construct_id, **kwargs)
        secret_arn = self.node.try_get_context("openaiSecretArn")
        if not secret_arn:
            raise ValueError("Pass -c openaiSecretArn=<existing Secrets Manager ARN>")
        secret = secrets.Secret.from_secret_complete_arn(self, "OpenAISecret", secret_arn)
        bucket_options = dict(
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(7),
                            abort_incomplete_multipart_upload_after=Duration.days(1))],
        )
        inputs = s3.Bucket(
            self, "Inputs", **bucket_options,
            # Necesario solo para el stack serverless (WebappStack): el
            # navegador sube el PDF directo a este bucket con una URL
            # prefirmada. La autorización real la da la firma de esa URL, no
            # esta política de CORS -- CORS únicamente decide si el
            # JavaScript de una página puede iniciar la petición PUT.
            cors=[s3.CorsRule(allowed_methods=[s3.HttpMethods.PUT],
                              allowed_origins=["*"], allowed_headers=["*"])],
        )
        results = s3.Bucket(self, "Results", **bucket_options)
        log_group = logs.LogGroup(self, "WorkerLogs",
                                  retention=logs.RetentionDays.ONE_WEEK,
                                  removal_policy=RemovalPolicy.DESTROY)
        worker = lambda_.DockerImageFunction(
            self, "Worker",
            code=lambda_.DockerImageCode.from_image_asset(
                str(Path(__file__).resolve().parents[1] / "app"),
                platform=assets.Platform.LINUX_AMD64),
            architecture=lambda_.Architecture.X86_64,
            timeout=Duration.minutes(15), memory_size=2048,
            ephemeral_storage_size=Size.mebibytes(512),
            log_group=log_group,
            environment={"INPUT_BUCKET": inputs.bucket_name,
                         "RESULT_BUCKET": results.bucket_name,
                         "OPENAI_SECRET_ARN": secret.secret_arn,
                         "MAX_PDF_BYTES": "10485760",
                         "AUDIT_MODEL": self.node.try_get_context("auditModel") or "gpt-4o"},
        )
        inputs.grant_read(worker, "inputs/*")
        results.grant_put(worker, "results/*")
        secret.grant_read(worker)
        for name, value in {"InputBucket": inputs.bucket_name,
                            "ResultBucket": results.bucket_name,
                            "FunctionName": worker.function_name,
                            "LogGroup": log_group.log_group_name}.items():
            CfnOutput(self, name, value=value)


if __name__ == "__main__":
    app = App()
    env = Environment(account=os.getenv("CDK_DEFAULT_ACCOUNT"), region=os.getenv("CDK_DEFAULT_REGION"))
    # AuditStack.__init__ exige -c openaiSecretArn sin importar qué stack le
    # pidas a `cdk deploy`/`cdk synth` -- CDK construye la app completa antes
    # de elegir cuál desplegar. WebappStack, en cambio, SOLO se construye si
    # ya le pasaste su contexto (auditFunctionName/auditInputBucket/
    # auditResultBucket): así "cdk deploy NlpAuditStack -c openaiSecretArn=..."
    # sigue funcionando solo, sin pedir datos de un stack que quizá ni exista
    # todavía. Pasa los 4 valores juntos cuando sí quieras tocar WebappStack.
    AuditStack(app, "NlpAuditStack", env=env)
    if app.node.try_get_context("auditFunctionName"):
        WebappStack(app, "WebappStack", env=env)
    app.synth()
