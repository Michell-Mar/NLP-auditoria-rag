"""Tests for webapp-pdf/server.py. No real AWS calls, no real OpenAI calls."""
import importlib.util
import io
import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader


def _texto_de(pdf_bytes: bytes) -> str:
    """Reads a generated PDF back to plain text, to assert on its content."""
    return "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(pdf_bytes)).pages)

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("webapp_pdf_server", ROOT / "webapp-pdf/server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


def _informe_exitoso():
    return {
        # The real worker always names it "document.pdf" (see app/worker.py):
        # it never learns the filename the user actually uploaded.
        "documento_auditado": "document.pdf",
        "fecha_auditoria": "2026-01-01T00:00:00",
        "resumen": {
            "veredicto_global": "CUMPLE PARCIALMENTE",
            "porcentaje_cumplimiento": 50.0,
            "conteo": {"Cumple total": 1, "Cumple parcial": 1, "No cumple": 0},
            "reglas_evaluadas": 2,
            "reglas_revisadas_por_humano": 0,
            "reglas_ajustadas_por_humano": 0,
            "aviso_desactualizado": False,
            "notas_desactualizacion": [],
        },
        "detalle": [
            {"id": "regla_1", "regla_evaluada": "¿Cumple regla 1?", "referencia_legal": "Art. 1",
             "dictamen": {"cumple": "Cumple total", "evidencia_encontrada": "x",
                         "elementos_faltantes": "Ninguno", "justificacion": "y",
                         "nota_desactualizacion": "Ninguna"},
             "revision": None},
            {"id": "regla_2", "regla_evaluada": "¿Cumple regla 2?", "referencia_legal": "Art. 2",
             "dictamen": {"cumple": "Cumple parcial", "evidencia_encontrada": "x",
                         "elementos_faltantes": "algo", "justificacion": "y",
                         "nota_desactualizacion": "Ninguna"},
             "revision": None},
        ],
    }


def _informe_desactualizado():
    """
    Shaped like a real worker report: every rule sees the whole document, so
    each of the (here, three) rules reports its own slightly different
    wording of the same INAI/IFAI sentence. The initial S3 JSON already has
    this deduplicated to one note by app/audito_rag_pdf.py's own
    recopila_notas_desactualizacion -- the bug only showed up after
    "Guardar cambios" (POST /revision) recomputed the summary from `detalle`
    without that same grouping.
    """
    notas_por_regla = [
        "Le informamos que el Instituto Nacional de Transparencia, Acceso a la Información y "
        "Protección de Datos Personales es la autoridad encargada de vigilar por la debida "
        "observancia de las disposiciones legales en materia de protección de datos personales. "
        "+ La autoridad vigente es la Secretaría Anticorrupción y Buen Gobierno.",
        "Le informamos que el Instituto Nacional de Transparencia, Acceso a la Información y "
        "Protección de Datos Personales es la autoridad encargada de vigilar por la debida "
        "observancia de las disposiciones legales en materia de protección de datos personales. "
        "+ Secretaría Anticorrupción y Buen Gobierno",
        "El Instituto Nacional de Transparencia, Acceso a la Información y Protección de Datos "
        "Personales es la autoridad encargada de vigilar por la debida observancia de las "
        "disposiciones legales en materia de protección de datos personales. + La autoridad "
        "vigente es la Secretaría Anticorrupción y Buen Gobierno.",
    ]
    detalle = [
        {"id": f"regla_{i}", "regla_evaluada": f"¿Regla {i}?", "referencia_legal": f"Art. {i}",
         "dictamen": {"cumple": "Cumple total", "evidencia_encontrada": "x",
                     "elementos_faltantes": "Ninguno", "justificacion": "y",
                     "nota_desactualizacion": nota},
         "revision": None}
        for i, nota in enumerate(notas_por_regla, 1)
    ]
    return {
        "documento_auditado": "document.pdf",
        "fecha_auditoria": "2026-01-01T00:00:00",
        "resumen": {
            "veredicto_global": "CUMPLE", "porcentaje_cumplimiento": 100.0,
            "conteo": {"Cumple total": 3, "Cumple parcial": 0, "No cumple": 0},
            "reglas_evaluadas": 3, "reglas_revisadas_por_humano": 0, "reglas_ajustadas_por_humano": 0,
            "aviso_desactualizado": True,
            "notas_desactualizacion": [notas_por_regla[0]],  # already deduplicated, like the CLI
        },
        "detalle": detalle,
    }


class FakeS3:
    def __init__(self, informe, markdown="# Reporte"):
        self.puts = {}
        self._informe = informe
        self._markdown = markdown

    def put_object(self, **kwargs):
        self.puts[kwargs["Key"]] = kwargs["Body"]

    def get_object(self, Bucket, Key):
        if Key.endswith(".json"):
            body = json.dumps(self._informe).encode()
        else:
            body = self._markdown.encode()
        return {"Body": _BytesBody(body)}


class _BytesBody:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


class FakeLambda:
    def __init__(self, result):
        self._result = result

    def invoke(self, **kwargs):
        return {"Payload": _BytesBody(json.dumps(self._result).encode())}


class FakeAWSContext:
    def __init__(self, s3, lambda_client, function_name="fn"):
        self.s3 = s3
        self.lambda_client = lambda_client
        self.input_bucket = "in"
        self.result_bucket = "out"
        self.function_name = function_name


@pytest.fixture
def client():
    return TestClient(server.app)


def _espera_estado_terminal(client, job_id, timeout=2.0):
    limite = time.monotonic() + timeout
    while time.monotonic() < limite:
        estado = client.get(f"/api/audits/{job_id}").json()
        if estado["state"] in ("SUCCEEDED", "REJECTED", "FAILED"):
            return estado
        time.sleep(0.02)
    raise AssertionError("El job no llegó a un estado terminal a tiempo")


def test_rejects_non_pdf_upload(client):
    response = client.post("/api/audits", files={"pdf": ("notas.txt", b"hola", "text/plain")})
    assert response.status_code == 400


def test_rejects_oversized_pdf(client, monkeypatch):
    monkeypatch.setattr(server, "MAX_PDF_BYTES", 10)
    response = client.post("/api/audits", files={"pdf": ("aviso.pdf", b"%PDF-" + b"x" * 20, "application/pdf")})
    assert response.status_code == 400


def test_full_success_flow_and_download(client, monkeypatch):
    resultado_lambda = {"state": "SUCCEEDED", "result_prefix": "results/job/"}
    server.AWS_CTX = FakeAWSContext(FakeS3(_informe_exitoso()), FakeLambda(resultado_lambda))

    respuesta = client.post("/api/audits", files={"pdf": ("aviso_privacidad.pdf", b"%PDF-1.4", "application/pdf")})
    assert respuesta.status_code == 202
    job_id = respuesta.json()["job_id"]

    estado = _espera_estado_terminal(client, job_id)
    assert estado["state"] == "SUCCEEDED"

    informe = client.get(f"/api/audits/{job_id}/informe").json()
    assert informe["resumen"]["veredicto_global"] == "CUMPLE PARCIALMENTE"
    assert len(informe["detalle"]) == 2
    # The worker's report always says "document.pdf" internally; the webapp
    # must restore the name the user actually uploaded.
    assert informe["documento_auditado"] == "aviso_privacidad.pdf"

    descarga = client.get(f"/api/audits/{job_id}/descarga/json")
    assert descarga.status_code == 200
    assert "attachment" in descarga.headers["content-disposition"]

    descarga_pdf = client.get(f"/api/audits/{job_id}/descarga/pdf")
    assert descarga_pdf.status_code == 200
    assert descarga_pdf.headers["content-type"].startswith("application/pdf")
    assert descarga_pdf.content[:5] == b"%PDF-"
    assert "aviso_privacidad.pdf" in _texto_de(descarga_pdf.content)
    assert "document.pdf" not in _texto_de(descarga_pdf.content)


def test_rejected_document_flow(client):
    informe_rechazo = {"resultado": "RECHAZADO", "validacion": {
        "es_aviso_privacidad": False, "tipo_documento_detectado": "contrato",
        "confianza": "Alta", "motivo": "No es un aviso de privacidad.",
    }}
    resultado_lambda = {"state": "REJECTED", "result_prefix": "results/job/"}
    server.AWS_CTX = FakeAWSContext(FakeS3(informe_rechazo), FakeLambda(resultado_lambda))

    job_id = client.post("/api/audits", files={"pdf": ("x.pdf", b"%PDF-1.4", "application/pdf")}).json()["job_id"]
    estado = _espera_estado_terminal(client, job_id)
    assert estado["state"] == "REJECTED"

    informe = client.get(f"/api/audits/{job_id}/informe").json()
    assert informe["resultado"] == "RECHAZADO"

    # A rejected document still gets a (short) downloadable PDF.
    descarga_pdf = client.get(f"/api/audits/{job_id}/descarga/pdf")
    assert descarga_pdf.status_code == 200
    assert "RECHAZADO" in _texto_de(descarga_pdf.content)

    # Editing a rejected document must be refused: there is nothing to revise.
    revision = client.post(f"/api/audits/{job_id}/revision", json={"ediciones": []})
    assert revision.status_code == 409


def test_failed_lambda_flow(client):
    server.AWS_CTX = FakeAWSContext(FakeS3({}), FakeLambda({"state": "FAILED", "error_type": "RuntimeError"}))
    job_id = client.post("/api/audits", files={"pdf": ("x.pdf", b"%PDF-1.4", "application/pdf")}).json()["job_id"]
    estado = _espera_estado_terminal(client, job_id)
    assert estado["state"] == "FAILED"
    assert estado["error"] == "RuntimeError"


def test_revision_recomputes_summary_and_pdf(client):
    resultado_lambda = {"state": "SUCCEEDED", "result_prefix": "results/job/"}
    server.AWS_CTX = FakeAWSContext(FakeS3(_informe_exitoso()), FakeLambda(resultado_lambda))
    job_id = client.post("/api/audits", files={"pdf": ("x.pdf", b"%PDF-1.4", "application/pdf")}).json()["job_id"]
    _espera_estado_terminal(client, job_id)

    # Downgrade "regla_2" from Cumple parcial to No cumple.
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
    # Matches genera_resumen in app/audito_rag_pdf.py: "NO CUMPLE" only fires
    # when Cumple total == 0; one full pass keeps the global verdict partial.
    assert informe["resumen"]["veredicto_global"] == "CUMPLE PARCIALMENTE"
    regla_2 = next(r for r in informe["detalle"] if r["id"] == "regla_2")
    assert regla_2["revision"]["ajustado"] is True
    assert regla_2["revision"]["revisor"] == "auditor.humano"

    texto_pdf = _texto_de(client.get(f"/api/audits/{job_id}/descarga/pdf").content)
    assert "CUMPLE PARCIALMENTE" in texto_pdf
    assert "Falta el procedimiento" in texto_pdf


def test_revision_rejects_invalid_verdict(client):
    resultado_lambda = {"state": "SUCCEEDED", "result_prefix": "results/job/"}
    server.AWS_CTX = FakeAWSContext(FakeS3(_informe_exitoso()), FakeLambda(resultado_lambda))
    job_id = client.post("/api/audits", files={"pdf": ("x.pdf", b"%PDF-1.4", "application/pdf")}).json()["job_id"]
    _espera_estado_terminal(client, job_id)

    respuesta = client.post(f"/api/audits/{job_id}/revision", json={
        "ediciones": [{"id": "regla_1", "veredicto_final": "Tal vez", "nota_revisor": ""}],
    })
    assert respuesta.status_code == 400


def test_saving_a_review_does_not_duplicate_the_outdated_authority_note(client):
    """
    Regression test for the reported bug: the note looked fine right after the
    audit finished, but duplicated (one bullet per rule, near-identical text)
    in the downloaded PDF after "Guardar cambios de revisión". Root cause:
    _recalcula_resumen deduplicated notes by exact string match, so the nine
    rules' slightly different phrasings of the same INAI/IFAI sentence all
    survived into the regenerated PDF.
    """
    resultado_lambda = {"state": "SUCCEEDED", "result_prefix": "results/job/"}
    server.AWS_CTX = FakeAWSContext(FakeS3(_informe_desactualizado()), FakeLambda(resultado_lambda))
    job_id = client.post("/api/audits", files={"pdf": ("x.pdf", b"%PDF-1.4", "application/pdf")}).json()["job_id"]
    _espera_estado_terminal(client, job_id)

    informe_inicial = client.get(f"/api/audits/{job_id}/informe").json()
    assert len(informe_inicial["resumen"]["notas_desactualizacion"]) == 1

    # Confirm all three rules without changing any verdict -- this is what
    # "Guardar cambios de revisión" sends even when nothing was edited.
    respuesta = client.post(f"/api/audits/{job_id}/revision", json={
        "revisor": "auditor.humano",
        "ediciones": [{"id": f"regla_{i}", "veredicto_final": "Cumple total", "nota_revisor": ""}
                     for i in (1, 2, 3)],
    })
    assert respuesta.status_code == 200
    informe_tras_revision = respuesta.json()
    assert len(informe_tras_revision["resumen"]["notas_desactualizacion"]) == 1

    texto_pdf = _texto_de(client.get(f"/api/audits/{job_id}/descarga/pdf").content)
    assert texto_pdf.count("Instituto Nacional de Transparencia") == 1


def test_unknown_job_returns_404(client):
    assert client.get("/api/audits/no-existe").status_code == 404


def test_concurrent_audit_limit_returns_429(client, monkeypatch):
    """
    With no login on the page, the concurrency cap is the only brake against
    someone (or an accident) firing off many paid audits at once.

    Starlette's TestClient runs a request's BackgroundTasks synchronously to
    completion before client.post() returns, so there is no way to catch a
    real background audit "mid-flight" from this thread. Instead, this
    simulates that state directly by holding the semaphore, which is exactly
    what the endpoint checks -- and then verifies the endpoint's own
    finally-release (not this test's mock) hands the slot back afterwards.
    """
    monkeypatch.setattr(server, "MAX_AUDITORIAS_CONCURRENTES", 1)
    semaforo = threading.Semaphore(1)
    monkeypatch.setattr(server, "_semaforo_auditorias", semaforo)

    semaforo.acquire()
    try:
        respuesta = client.post("/api/audits", files={"pdf": ("a.pdf", b"%PDF-1.4", "application/pdf")})
        assert respuesta.status_code == 429
    finally:
        semaforo.release()

    resultado_lambda = {"state": "SUCCEEDED", "result_prefix": "results/job/"}
    server.AWS_CTX = FakeAWSContext(FakeS3(_informe_exitoso()), FakeLambda(resultado_lambda))

    respuesta = client.post("/api/audits", files={"pdf": ("b.pdf", b"%PDF-1.4", "application/pdf")})
    assert respuesta.status_code == 202
    _espera_estado_terminal(client, respuesta.json()["job_id"])

    # If ejecuta_auditoria's own finally-block hadn't released the slot, this
    # third request (max concurrency is still 1) would also come back 429.
    respuesta = client.post("/api/audits", files={"pdf": ("c.pdf", b"%PDF-1.4", "application/pdf")})
    assert respuesta.status_code == 202
    _espera_estado_terminal(client, respuesta.json()["job_id"])


def test_recompute_logic_matches_app_audito_rag_pdf():
    """
    webapp-pdf/server.py deliberately duplicates genera_resumen/veredicto_efectivo
    from app/audito_rag_pdf.py instead of importing it (to avoid pulling
    langchain/openai into a plain HTTP server). This test is the guardrail
    against that copy drifting from the source of truth: it runs both
    implementations over the same fixtures and asserts identical summaries.
    """
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
        [
            # Every rule sees the whole document, so all nine can report their own
            # slightly different wording of the same INAI/IFAI sentence -- this is
            # exactly the shape that exposed the duplication bug on "guardar cambios".
            {"dictamen": {"cumple": "Cumple total",
                         "nota_desactualizacion": "Le informamos que el Instituto Nacional de "
                         "Transparencia es la autoridad. + La autoridad vigente es la Secretaría."},
             "revision": None},
            {"dictamen": {"cumple": "Cumple total",
                         "nota_desactualizacion": "El Instituto Nacional de Transparencia es la "
                         "autoridad. + Secretaría Anticorrupción y Buen Gobierno"},
             "revision": None},
            {"dictamen": {"cumple": "Cumple parcial",
                         "nota_desactualizacion": "le informamos que el INAI es la autoridad."},
             "revision": None},
        ],
    ]
    for detalle in casos:
        assert server._recalcula_resumen(detalle) == audito.genera_resumen(detalle)
        for regla in detalle:
            assert server._veredicto_efectivo(regla) == audito.veredicto_efectivo(regla)
