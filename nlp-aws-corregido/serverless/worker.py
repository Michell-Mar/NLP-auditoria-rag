"""SQS-triggered orchestrator for the serverless variant.

This Lambda does NOT audit anything itself -- it invokes the existing,
unchanged NlpAuditStack Worker (the same Lambda app/worker.py + scripts/
audit.py already use) exactly like webapp/server.py's ejecuta_auditoria does
locally, and writes the outcome into the Jobs DynamoDB table so the API
Lambda (app.py) can serve it to polling clients.

Concurrency is capped where it belongs for a distributed, multi-instance
compute model: on the SQS event source's max_concurrency in the CDK stack,
not with a Python-level threading.Semaphore (that only protects a single
long-lived process, which a Lambda is not).
"""
import json
import os

import boto3


def _tabla():
    return boto3.resource("dynamodb").Table(os.environ["JOBS_TABLE"])


def _lambda_client():
    return boto3.client("lambda")


def _s3():
    return boto3.client("s3")


def _actualiza(job_id, **campos):
    campos = {k: v for k, v in campos.items() if v is not None}
    if not campos:
        return
    tabla = _tabla()
    tabla.update_item(
        Key={"job_id": job_id},
        UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in campos),
        ExpressionAttributeNames={f"#{k}": k for k in campos},
        ExpressionAttributeValues={f":{k}": v for k, v in campos.items()},
    )


def handler(event, context):
    resultados = []
    for record in event["Records"]:
        job_id = json.loads(record["body"])["job_id"]
        try:
            respuesta = _lambda_client().invoke(
                FunctionName=os.environ["AUDIT_FUNCTION_NAME"],
                InvocationType="RequestResponse",
                Payload=json.dumps({"job_id": job_id}).encode(),
            )
            resultado = json.loads(respuesta["Payload"].read())
            if respuesta.get("FunctionError") or resultado.get("state") == "FAILED":
                _actualiza(job_id, state="FAILED",
                          error=resultado.get("error_type", "Error desconocido durante la auditoría"))
                resultados.append({"job_id": job_id, "state": "FAILED"})
                continue

            prefix = resultado.get("result_prefix")
            informe_json = None
            if prefix:
                obj = _s3().get_object(Bucket=os.environ["AUDIT_RESULT_BUCKET"],
                                       Key=prefix + "resultado_auditoria.json")
                informe = json.loads(obj["Body"].read())
                # worker.py (el Lambda de auditoría) siempre guarda el archivo
                # como inputs/{job_id}/document.pdf y nunca conoce el nombre
                # original -- se restaura aquí con el que sí guardamos al
                # crear el job en app.py.
                item = _tabla().get_item(Key={"job_id": job_id}).get("Item") or {}
                if isinstance(informe, dict) and "documento_auditado" in informe:
                    informe["documento_auditado"] = item.get("filename", "document.pdf")
                informe_json = json.dumps(informe)

            estado_final = resultado.get("state", "FAILED")
            _actualiza(job_id, state=estado_final, informe=informe_json)
            resultados.append({"job_id": job_id, "state": estado_final})
        except Exception as exc:  # noqa: BLE001 -- mirrors app/worker.py: never leak exception text
            _actualiza(job_id, state="FAILED", error=type(exc).__name__)
            raise  # deja que SQS reintente y, si se agota, caiga a la DLQ
    return {"procesados": resultados}
