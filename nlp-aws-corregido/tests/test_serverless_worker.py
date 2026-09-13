"""Tests for serverless/worker.py. No real AWS calls."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("serverless_worker", ROOT / "serverless/worker.py")
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class FakeTable:
    def __init__(self, items=None):
        self.items = items or {}

    def get_item(self, Key):
        item = self.items.get(Key["job_id"])
        return {"Item": dict(item)} if item is not None else {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues,
                    ExpressionAttributeNames=None):
        item = self.items.setdefault(Key["job_id"], {"job_id": Key["job_id"]})
        for asignacion in UpdateExpression.removeprefix("SET ").split(","):
            nombre, _, valor_ref = asignacion.strip().partition("=")
            nombre, valor_ref = nombre.strip(), valor_ref.strip()
            if ExpressionAttributeNames and nombre in ExpressionAttributeNames:
                nombre = ExpressionAttributeNames[nombre]
            item[nombre] = ExpressionAttributeValues[valor_ref]


class _BytesBody:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


class FakeLambdaClient:
    def __init__(self, resultado):
        self._resultado = resultado
        self.invocaciones = []

    def invoke(self, **kwargs):
        self.invocaciones.append(kwargs)
        return {"Payload": _BytesBody(json.dumps(self._resultado).encode())}


class FakeS3:
    def __init__(self, informe):
        self._informe = informe

    def get_object(self, Bucket, Key):
        return {"Body": _BytesBody(json.dumps(self._informe).encode())}


def _evento(job_id):
    return {"Records": [{"body": json.dumps({"job_id": job_id})}]}


@pytest.fixture(autouse=True)
def _entorno(monkeypatch):
    monkeypatch.setenv("JOBS_TABLE", "fake-jobs")
    monkeypatch.setenv("AUDIT_FUNCTION_NAME", "fake-audit-fn")
    monkeypatch.setenv("AUDIT_RESULT_BUCKET", "fake-results")


def test_succeeded_writes_informe_and_restores_filename(monkeypatch):
    tabla = FakeTable(items={"job1": {"job_id": "job1", "filename": "aviso_real.pdf", "state": "PROCESANDO"}})
    informe = {"documento_auditado": "document.pdf", "resumen": {}, "detalle": []}
    monkeypatch.setattr(worker, "_tabla", lambda: tabla)
    monkeypatch.setattr(worker, "_lambda_client",
                        lambda: FakeLambdaClient({"state": "SUCCEEDED", "result_prefix": "results/job1/"}))
    monkeypatch.setattr(worker, "_s3", lambda: FakeS3(informe))

    resultado = worker.handler(_evento("job1"), None)

    assert resultado == {"procesados": [{"job_id": "job1", "state": "SUCCEEDED"}]}
    assert tabla.items["job1"]["state"] == "SUCCEEDED"
    informe_guardado = json.loads(tabla.items["job1"]["informe"])
    # worker.py (el Lambda de auditoría) siempre guarda "document.pdf" -- el
    # orquestador debe restaurar el nombre real que se guardó al crear el job.
    assert informe_guardado["documento_auditado"] == "aviso_real.pdf"


def test_rejected_flow(monkeypatch):
    tabla = FakeTable(items={"job2": {"job_id": "job2", "filename": "x.pdf", "state": "PROCESANDO"}})
    rechazo = {"resultado": "RECHAZADO", "validacion": {"tipo_documento_detectado": "contrato"}}
    monkeypatch.setattr(worker, "_tabla", lambda: tabla)
    monkeypatch.setattr(worker, "_lambda_client",
                        lambda: FakeLambdaClient({"state": "REJECTED", "result_prefix": "results/job2/"}))
    monkeypatch.setattr(worker, "_s3", lambda: FakeS3(rechazo))

    worker.handler(_evento("job2"), None)

    assert tabla.items["job2"]["state"] == "REJECTED"
    assert json.loads(tabla.items["job2"]["informe"])["resultado"] == "RECHAZADO"


def test_failed_invocation_marks_job_failed_without_raising(monkeypatch):
    tabla = FakeTable(items={"job3": {"job_id": "job3", "filename": "x.pdf", "state": "PROCESANDO"}})
    monkeypatch.setattr(worker, "_tabla", lambda: tabla)
    monkeypatch.setattr(worker, "_lambda_client",
                        lambda: FakeLambdaClient({"state": "FAILED", "error_type": "RuntimeError"}))
    monkeypatch.setattr(worker, "_s3", lambda: FakeS3({}))

    resultado = worker.handler(_evento("job3"), None)

    assert resultado == {"procesados": [{"job_id": "job3", "state": "FAILED"}]}
    assert tabla.items["job3"]["state"] == "FAILED"
    assert tabla.items["job3"]["error"] == "RuntimeError"


def test_unexpected_exception_marks_failed_then_reraises_for_sqs_retry(monkeypatch):
    tabla = FakeTable(items={"job4": {"job_id": "job4", "filename": "x.pdf", "state": "PROCESANDO"}})

    class BoomLambda:
        def invoke(self, **kwargs):
            raise ConnectionError("boom")

    monkeypatch.setattr(worker, "_tabla", lambda: tabla)
    monkeypatch.setattr(worker, "_lambda_client", lambda: BoomLambda())

    with pytest.raises(ConnectionError):
        worker.handler(_evento("job4"), None)

    assert tabla.items["job4"]["state"] == "FAILED"
    assert tabla.items["job4"]["error"] == "ConnectionError"
