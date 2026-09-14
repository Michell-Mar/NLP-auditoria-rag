"""Serverless web interface: API Gateway + 2 Lambdas + SQS + DynamoDB.

Invokes the existing, unchanged AuditStack Worker exactly like webapp/ and
webapp-pdf/ do from a laptop -- this stack only replaces WHERE that
orchestration runs (a long-lived local process -> two Lambdas) so a pilot
doesn't depend on someone's machine staying on.

Deploy after AuditStack, passing its outputs as context (see docs):
    cdk deploy WebappStack \\
      -c auditFunctionName=<FunctionName from cdk-outputs.json> \\
      -c auditInputBucket=<InputBucket> \\
      -c auditResultBucket=<ResultBucket>
"""
from pathlib import Path

from aws_cdk import App, ArnFormat, CfnOutput, Duration, RemovalPolicy, Size, Stack
from aws_cdk import aws_dynamodb as ddb
from aws_cdk import aws_ecr_assets as assets
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as sources
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sqs as sqs
from aws_cdk.aws_apigatewayv2 import HttpApi, HttpMethod
from aws_cdk.aws_apigatewayv2_integrations import HttpLambdaIntegration
from constructs import Construct

SERVERLESS_DIR = str(Path(__file__).resolve().parents[1] / "serverless")


class WebappStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs):
        super().__init__(scope, construct_id, **kwargs)

        audit_function_name = self.node.try_get_context("auditFunctionName")
        audit_input_bucket_name = self.node.try_get_context("auditInputBucket")
        audit_result_bucket_name = self.node.try_get_context("auditResultBucket")
        for name, value in (("auditFunctionName", audit_function_name),
                            ("auditInputBucket", audit_input_bucket_name),
                            ("auditResultBucket", audit_result_bucket_name)):
            if not value:
                raise ValueError(f"Pass -c {name}=<value from cdk-outputs.json (AuditStack)>")

        audit_function = lambda_.Function.from_function_attributes(
            self, "AuditFunction", function_arn=self.format_arn(
                service="lambda", resource="function", resource_name=audit_function_name,
                # Los ARN de Lambda van con ":function:nombre" (dos puntos),
                # no "/function/nombre" (diagonal) como la mayoría de los
                # demás servicios -- sin esto, el permiso que se firma abajo
                # apunta a un ARN que nunca coincide con el real, y
                # QueueWorkerFn truena con AccessDeniedException al invocar.
                arn_format=ArnFormat.COLON_RESOURCE_NAME),
            same_environment=True)
        # Referencia de solo lectura al bucket de entradas de AuditStack -- se
        # necesita para poder firmar URLs de subida hacia ÉL, no para crear
        # uno nuevo. AuditStack sigue siendo dueño del bucket.
        audit_input_bucket = s3.Bucket.from_bucket_name(self, "AuditInputBucket", audit_input_bucket_name)
        audit_result_bucket = s3.Bucket.from_bucket_name(self, "AuditResultBucket", audit_result_bucket_name)

        jobs_table = ddb.TableV2(
            self, "Jobs",
            partition_key=ddb.Attribute(name="job_id", type=ddb.AttributeType.STRING),
            billing=ddb.Billing.on_demand(),
            time_to_live_attribute="ttl",
            removal_policy=RemovalPolicy.DESTROY,
        )

        dlq = sqs.Queue(self, "JobsDLQ")
        queue = sqs.Queue(
            self, "JobsQueue",
            visibility_timeout=Duration.seconds(920),  # > el timeout del worker
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=2, queue=dlq),
        )

        log_group_api = logs.LogGroup(self, "ApiLogs", retention=logs.RetentionDays.ONE_WEEK,
                                      removal_policy=RemovalPolicy.DESTROY)
        log_group_worker = logs.LogGroup(self, "QueueWorkerLogs", retention=logs.RetentionDays.ONE_WEEK,
                                         removal_policy=RemovalPolicy.DESTROY)

        entorno_comun = {"JOBS_TABLE": jobs_table.table_name}
        # Tope de auditorías que se pueden ENCOLAR desde ahora en adelante --
        # ver USO_COUNTER_KEY/_registra_uso en serverless/app.py. Cambiar
        # este valor y volver a desplegar sube o baja el tope sin resetear
        # el contador (vive en un item aparte de la misma tabla Jobs).
        max_audit_uses = self.node.try_get_context("maxAuditUses") or "10"

        api_fn = lambda_.DockerImageFunction(
            self, "ApiFn",
            code=lambda_.DockerImageCode.from_image_asset(
                SERVERLESS_DIR, platform=assets.Platform.LINUX_AMD64, cmd=["app.handler"]),
            architecture=lambda_.Architecture.X86_64,
            timeout=Duration.seconds(29),  # el límite real de un HTTP API con integración Lambda
            memory_size=512,
            log_group=log_group_api,
            environment={**entorno_comun,
                        "INPUT_BUCKET": audit_input_bucket_name,
                        "JOBS_QUEUE_URL": queue.queue_url,
                        "MAX_AUDIT_USES": str(max_audit_uses)},
        )

        worker_fn = lambda_.DockerImageFunction(
            self, "QueueWorkerFn",
            code=lambda_.DockerImageCode.from_image_asset(
                SERVERLESS_DIR, platform=assets.Platform.LINUX_AMD64, cmd=["worker.handler"]),
            architecture=lambda_.Architecture.X86_64,
            timeout=Duration.minutes(15),  # el Worker de AuditStack puede tardar hasta 15 min
            memory_size=512,
            ephemeral_storage_size=Size.mebibytes(512),
            log_group=log_group_worker,
            environment={**entorno_comun,
                        "AUDIT_FUNCTION_NAME": audit_function_name,
                        "AUDIT_RESULT_BUCKET": audit_result_bucket_name},
        )
        # max_concurrency, no una variable de Python: el equivalente al
        # threading.Semaphore(3) de webapp/webapp-pdf, pero válido cuando hay
        # muchas instancias del worker corriendo a la vez, no solo un hilo.
        worker_fn.add_event_source(sources.SqsEventSource(queue, batch_size=1, max_concurrency=3))

        jobs_table.grant_read_write_data(api_fn)
        jobs_table.grant_read_write_data(worker_fn)
        queue.grant_send_messages(api_fn)
        audit_input_bucket.grant_put(api_fn, "inputs/*")  # firma URLs de subida hacia ese prefijo
        audit_function.grant_invoke(worker_fn)
        audit_result_bucket.grant_read(worker_fn, "results/*")

        # Un solo proxy Lambda: FastAPI (vía Mangum) hace su propio ruteo
        # interno, así que API Gateway solo necesita reenviar todo -- no hay
        # que mantener aquí una lista de rutas en sync con app.py.
        http_api = HttpApi(self, "Http")
        integracion = HttpLambdaIntegration("ApiIntegration", api_fn)
        http_api.add_routes(path="/", methods=[HttpMethod.ANY], integration=integracion)
        http_api.add_routes(path="/{proxy+}", methods=[HttpMethod.ANY], integration=integracion)

        CfnOutput(self, "SiteUrl", value=http_api.api_endpoint)
        CfnOutput(self, "JobsTable", value=jobs_table.table_name)
        CfnOutput(self, "JobsQueueUrl", value=queue.queue_url)
        CfnOutput(self, "JobsDLQUrl", value=dlq.queue_url)
        # Sin esto, encontrar los log groups para depurar un error real
        # significa adivinar el nombre generado por CloudFormation.
        CfnOutput(self, "ApiLogGroup", value=log_group_api.log_group_name)
        CfnOutput(self, "QueueWorkerLogGroup", value=log_group_worker.log_group_name)
