"""Tests for serverless/app.py. No real AWS calls, no real OpenAI calls."""
import importlib.util
import io
import json
import re
import threading
from pathlib import Path

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("serverless_app", ROOT / "serverless/app.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


def _texto_de(pdf_bytes: bytes) -> str:
    return "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(pdf_bytes)).pages)


class FakeTable:
    """Minimal DynamoDB Table fake: only the operations app.py/worker.py use
    (put_item, get_item, and update_item with a plain "SET a = :a, ..."
    expression, optionally using #alias names). Not a general DynamoDB
    emulator -- just enough to exercise the real route/handler logic."""

    def __init__(self):
        self.items = {}
        self.lock = threading.Lock()

    def put_item(self, Item):
        with self.lock:
            self.items[Item["job_id"]] = dict(Item)

    def get_item(self, Key):
        item = self.items.get(Key["job_id"])
        return {"Item": dict(item)} if item is not None else {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues,
                    ExpressionAttributeNames=None, ConditionExpression=None):
        with self.lock:
            item = self.items.setdefault(Key["job_id"], {"job_id": Key["job_id"]})
            if UpdateExpression.startswith("ADD "):
                nombre, valor_ref = UpdateExpression.removeprefix("ADD ").split()
                if ConditionExpression:
                    # Solo soporta el único patrón que usa el código real:
                    # "attribute_not_exists(x) OR x < :max".
                    if nombre in item and item[nombre] >= ExpressionAttributeValues[":max"]:
                        raise ClientError(
                            {"Error": {"Code": "ConditionalCheckFailedException",
                                      "Message": "The conditional request failed"}},
                            "UpdateItem")
                item[nombre] = item.get(nombre, 0) + ExpressionAttributeValues[valor_ref]
                return
            asignaciones = UpdateExpression.removeprefix("SET ").split(",")
            for asignacion in asignaciones:
                nombre, _, valor_ref = asignacion.strip().partition("=")
                nombre, valor_ref = nombre.strip(), valor_ref.strip()
                if ExpressionAttributeNames and nombre in ExpressionAttributeNames:
                    nombre = ExpressionAttributeNames[nombre]
                item[nombre] = ExpressionAttributeValues[valor_ref]


def _informe_exitoso():
    return {
        "documento_auditado": "document.pdf",
        "fecha_auditoria": "2026-01-01T00:00:00",
        "resumen": {
            "veredicto_global": "CUMPLE PARCIALMENTE", "porcentaje_cumplimiento": 50.0,
            "conteo": {"Cumple total": 1, "Cumple parcial": 1, "No cumple": 0},
            "reglas_evaluadas": 2, "reglas_revisadas_por_humano": 0, "reglas_ajustadas_por_humano": 0,
            "aviso_desactualizado": False, "notas_desactualizacion": [],
        },
        "detalle": [
            {"id": "regla_1", "regla_evaluada": "¿Cumple regla 1?", "referencia_legal": "Art. 1",
             "dictamen": {"cumple": "Cumple total", "evidencia_encontrada": "x",
                         "elementos_faltantes": "Ninguno", "justificacion": "y",
                         "nota_desactualizacion": "Ninguna"}, "revision": None},
            {"id": "regla_2", "regla_evaluada": "¿Cumple regla 2?", "referencia_legal": "Art. 2",
             "dictamen": {"cumple": "Cumple parcial", "evidencia_encontrada": "x",
                         "elementos_faltantes": "algo", "justificacion": "y",
                         "nota_desactualizacion": "Ninguna"}, "revision": None},
        ],
    }


@pytest.fixture
def client(monkeypatch):
    tabla = FakeTable()
    sqs_enviados = []

    class FakeSqs:
        def send_message(self, QueueUrl, MessageBody):
            sqs_enviados.append(json.loads(MessageBody))

    class FakeS3:
        def generate_presigned_url(self, op, Params, ExpiresIn):
            return f"https://fake-s3/{Params['Bucket']}/{Params['Key']}?ttl={ExpiresIn}"

    monkeypatch.setenv("JOBS_TABLE", "fake-jobs")
    monkeypatch.setenv("INPUT_BUCKET", "fake-input")
    monkeypatch.setenv("JOBS_QUEUE_URL", "https://fake-sqs/fake-queue")
    monkeypatch.setattr(server, "_tabla", lambda: tabla)
    monkeypatch.setattr(server, "_s3", lambda: FakeS3())
    monkeypatch.setattr(server, "_sqs", lambda: FakeSqs())

    test_client = TestClient(server.app)
    test_client.tabla = tabla
    test_client.sqs_enviados = sqs_enviados
    return test_client


def test_crear_auditoria_reserva_job_y_url_de_subida(client):
    respuesta = client.post("/api/audits", json={"filename": "aviso.pdf"})
    assert respuesta.status_code == 201
    cuerpo = respuesta.json()
    assert "job_id" in cuerpo
    assert cuerpo["upload_url"].endswith(f"inputs/{cuerpo['job_id']}/document.pdf?ttl=900")

    item = client.tabla.items[cuerpo["job_id"]]
    assert item["state"] == "PENDIENTE"
    assert item["filename"] == "aviso.pdf"
    assert item["ttl"] > item["creado_en"]


def test_crear_auditoria_rechaza_no_pdf(client):
    respuesta = client.post("/api/audits", json={"filename": "notas.txt"})
    assert respuesta.status_code == 400


def test_procesar_encola_y_cambia_estado(client):
    job_id = client.post("/api/audits", json={"filename": "aviso.pdf"}).json()["job_id"]
    respuesta = client.post(f"/api/audits/{job_id}/procesar")
    assert respuesta.status_code == 202
    assert client.tabla.items[job_id]["state"] == "PROCESANDO"
    assert client.sqs_enviados == [{"job_id": job_id}]


def test_procesar_dos_veces_falla(client):
    job_id = client.post("/api/audits", json={"filename": "aviso.pdf"}).json()["job_id"]
    client.post(f"/api/audits/{job_id}/procesar")
    respuesta = client.post(f"/api/audits/{job_id}/procesar")
    assert respuesta.status_code == 409


def test_procesar_respeta_limite_de_usos(client, monkeypatch):
    monkeypatch.setenv("MAX_AUDIT_USES", "1")
    primero = client.post("/api/audits", json={"filename": "aviso.pdf"}).json()["job_id"]
    assert client.post(f"/api/audits/{primero}/procesar").status_code == 202

    segundo = client.post("/api/audits", json={"filename": "otro.pdf"}).json()["job_id"]
    respuesta = client.post(f"/api/audits/{segundo}/procesar")
    assert respuesta.status_code == 429
    # El job se quedó en PENDIENTE -- no se encoló ni se marcó PROCESANDO.
    assert client.tabla.items[segundo]["state"] == "PENDIENTE"
    assert client.sqs_enviados == [{"job_id": primero}]


def test_crear_auditoria_no_consume_el_limite_solo_procesarla(client, monkeypatch):
    monkeypatch.setenv("MAX_AUDIT_USES", "1")
    # Crear varios jobs sin procesarlos no debe agotar el cupo: el costo
    # real (worker + LLM) solo se compromete al encolar.
    for _ in range(5):
        client.post("/api/audits", json={"filename": "aviso.pdf"})
    job_id = client.post("/api/audits", json={"filename": "aviso.pdf"}).json()["job_id"]
    assert client.post(f"/api/audits/{job_id}/procesar").status_code == 202


def test_estado_de_job_inexistente_es_404(client):
    assert client.get("/api/audits/no-existe").status_code == 404


def test_informe_no_disponible_antes_de_completar(client):
    job_id = client.post("/api/audits", json={"filename": "aviso.pdf"}).json()["job_id"]
    assert client.get(f"/api/audits/{job_id}/informe").status_code == 409


def test_flujo_completo_hasta_descarga(client):
    job_id = client.post("/api/audits", json={"filename": "aviso.pdf"}).json()["job_id"]
    client.post(f"/api/audits/{job_id}/procesar")

    # Simula lo que worker.py escribe cuando el Lambda de auditoría termina.
    client.tabla.update_item(
        Key={"job_id": job_id}, UpdateExpression="SET #s = :s, informe = :i",
        ExpressionAttributeNames={"#s": "state"},
        ExpressionAttributeValues={":s": "SUCCEEDED", ":i": json.dumps(_informe_exitoso())},
    )

    estado = client.get(f"/api/audits/{job_id}").json()
    assert estado["state"] == "SUCCEEDED"

    informe = client.get(f"/api/audits/{job_id}/informe").json()
    assert informe["resumen"]["veredicto_global"] == "CUMPLE PARCIALMENTE"

    descarga_json = client.get(f"/api/audits/{job_id}/descarga/json")
    assert descarga_json.status_code == 200
    assert "attachment" in descarga_json.headers["content-disposition"]

    descarga_pdf = client.get(f"/api/audits/{job_id}/descarga/pdf")
    assert descarga_pdf.status_code == 200
    assert descarga_pdf.content[:5] == b"%PDF-"
    assert "CUMPLE PARCIALMENTE" in _texto_de(descarga_pdf.content)


def test_revision_recalcula_resumen_y_se_refleja_en_pdf(client):
    job_id = client.post("/api/audits", json={"filename": "aviso.pdf"}).json()["job_id"]
    client.post(f"/api/audits/{job_id}/procesar")
    client.tabla.update_item(
        Key={"job_id": job_id}, UpdateExpression="SET #s = :s, informe = :i",
        ExpressionAttributeNames={"#s": "state"},
        ExpressionAttributeValues={":s": "SUCCEEDED", ":i": json.dumps(_informe_exitoso())},
    )

    respuesta = client.post(f"/api/audits/{job_id}/revision", json={
        "revisor": "auditor.humano",
        "ediciones": [
            {"id": "regla_1", "veredicto_final": "Cumple total", "nota_revisor": ""},
            {"id": "regla_2", "veredicto_final": "No cumple", "nota_revisor": "Falta el procedimiento"},
        ],
    })
    assert respuesta.status_code == 200
    informe = respuesta.json()
    assert informe["resumen"]["conteo"] == {"Cumple total": 1, "Cumple parcial": 0, "No cumple": 1}
    regla_2 = next(r for r in informe["detalle"] if r["id"] == "regla_2")
    assert regla_2["revision"]["ajustado"] is True

    texto_pdf = _texto_de(client.get(f"/api/audits/{job_id}/descarga/pdf").content)
    assert "Falta el procedimiento" in texto_pdf


def test_revision_rechazada_si_no_esta_completada(client):
    job_id = client.post("/api/audits", json={"filename": "aviso.pdf"}).json()["job_id"]
    respuesta = client.post(f"/api/audits/{job_id}/revision", json={"ediciones": []})
    assert respuesta.status_code == 409


def test_pdf_para_documento_rechazado(client):
    job_id = client.post("/api/audits", json={"filename": "x.pdf"}).json()["job_id"]
    rechazo = {"documento_auditado": "document.pdf", "fecha_auditoria": "2026-01-01T00:00:00",
              "resultado": "RECHAZADO",
              "validacion": {"es_aviso_privacidad": False, "tipo_documento_detectado": "contrato",
                            "confianza": "Alta", "motivo": "No es un aviso de privacidad."}}
    client.tabla.update_item(
        Key={"job_id": job_id}, UpdateExpression="SET #s = :s, informe = :i",
        ExpressionAttributeNames={"#s": "state"},
        ExpressionAttributeValues={":s": "REJECTED", ":i": json.dumps(rechazo)},
    )
    texto_pdf = _texto_de(client.get(f"/api/audits/{job_id}/descarga/pdf").content)
    assert "RECHAZADO" in texto_pdf


def test_recompute_logic_matches_app_audito_rag_pdf():
    """Same guardrail as tests/test_webapp.py / test_webapp_pdf.py: this
    Lambda duplicates genera_resumen/veredicto_efectivo instead of importing
    them (no langchain/openai in this image), so this compares both against
    the same fixtures to catch drift."""
    audito_spec = importlib.util.spec_from_file_location("audito_rag_pdf", ROOT / "app/audito_rag_pdf.py")
    audito = importlib.util.module_from_spec(audito_spec)
    audito_spec.loader.exec_module(audito)

    casos = [
        [],
        [{"dictamen": {"cumple": "Cumple total", "nota_desactualizacion": "Ninguna"}, "revision": None}],
        [
            {"dictamen": {"cumple": "Cumple total", "nota_desactualizacion": "Ninguna"}, "revision": None},
            {"dictamen": {"cumple": "Cumple parcial", "nota_desactualizacion": "Ninguna"},
             "revision": {"revisado": True, "veredicto_final": "No cumple", "ajustado": True}},
            {"dictamen": {"cumple": "No cumple", "nota_desactualizacion": "menciona al INAI"}, "revision": None},
        ],
    ]
    for detalle in casos:
        assert server._recalcula_resumen(detalle) == audito.genera_resumen(detalle)
        for regla in detalle:
            assert server._veredicto_efectivo(regla) == audito.veredicto_efectivo(regla)
