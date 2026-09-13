"""Local web interface for the deployed audit worker.

Runs only on the operator's machine, reusing the same AWS profile/credentials
as scripts/audit.py. The browser never receives AWS credentials: it only
talks to this local server over HTTP, and this server is the only thing
that calls S3/Lambda. Nothing here changes app/ or infra/ -- the deployed
Lambda is untouched, this is a friendlier front end for the same pipeline.

Run with:
    python webapp/server.py --profile nlp-dev --region us-east-1
"""
import argparse
import json
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import boto3
from botocore.config import Config
from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_PDF_BYTES = 10 * 1024 * 1024  # mirrors scripts/audit.py and worker.py

# Hard cap on simultaneous audits. Each one is 9 sequential gpt-4o calls, so
# this is the only real brake on cost/abuse when the page has no login of
# its own (a pilot behind "the link is the secret" has nothing else stopping
# someone from firing off many uploads at once).
MAX_AUDITORIAS_CONCURRENTES = 3
_semaforo_auditorias = threading.Semaphore(MAX_AUDITORIAS_CONCURRENTES)

# Verdict labels, mirrored from app/audito_rag_pdf.py so the recomputed
# summary and rendered Markdown stay byte-for-byte consistent with what the
# CLI itself produces. Kept as a small standalone copy (not an import) so
# this web layer never has to install langchain/openai just to render text.
VEREDICTOS = ("Cumple total", "Cumple parcial", "No cumple")
_VEREDICTOS_VACIOS = {"", "ninguna", "ninguno", "n/a", "na", "none", "no aplica"}


def _es_vacio(valor) -> bool:
    return (valor or "").strip().lower() in _VEREDICTOS_VACIOS


def _firma_nota(nota: str) -> str:
    """Groups notes about the same underlying issue (all INAI/IFAI mentions are one),
    mirroring _firma_nota in app/audito_rag_pdf.py -- every rule sees the whole
    document, so each of the nine rules reports its own slightly different
    wording of the same INAI/IFAI sentence. Without this grouping, saving a
    review re-triggers _recalcula_resumen and the near-duplicates pile up."""
    low = nota.lower()
    if "inai" in low or "ifai" in low or "instituto nacional de transparencia" in low:
        return "inai_ifai"
    return re.sub(r"[^a-z0-9]", "", low)[:80]


def _recopila_notas_desactualizacion(detalle: list[dict]) -> list[str]:
    por_firma: dict[str, str] = {}
    for r in detalle:
        nota = (r["dictamen"].get("nota_desactualizacion") or "").strip()
        if _es_vacio(nota):
            continue
        firma = _firma_nota(nota)
        if firma not in por_firma or len(nota) > len(por_firma[firma]):
            por_firma[firma] = nota  # keeps the most complete wording of each issue
    return list(por_firma.values())

app = FastAPI(title="Auditor de Avisos de Privacidad (interfaz local)")


# --------------------------------------------------------------------------
# AWS wiring
# --------------------------------------------------------------------------
class AWSContext:
    """Holds the boto3 clients and resource names for one deployed stack."""

    def __init__(self, session: boto3.Session, input_bucket: str,
                 result_bucket: str, function_name: str):
        self.s3 = session.client("s3")
        # Same guardrail as scripts/audit.py: never silently retry a paid audit.
        self.lambda_client = session.client("lambda", config=Config(
            read_timeout=930, connect_timeout=10, retries={"total_max_attempts": 1}))
        self.input_bucket = input_bucket
        self.result_bucket = result_bucket
        self.function_name = function_name


AWS_CTX: Optional[AWSContext] = None


# --------------------------------------------------------------------------
# In-memory job registry (single-process, single-operator local tool)
# --------------------------------------------------------------------------
@dataclass
class Job:
    job_id: str
    filename: str
    state: str = "PENDIENTE"
    error: Optional[str] = None
    informe: Optional[dict] = None          # original result, as returned by S3
    informe_editado: Optional[dict] = None  # current version (edits applied)
    markdown: Optional[str] = None          # current Markdown, regenerated on edit
    lock: threading.Lock = field(default_factory=threading.Lock)


JOBS: dict[str, Job] = {}


def _obtiene_job(job_id: str) -> Job:
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "Auditoría no encontrada (¿se reinició el servidor?).")
    return job


def ejecuta_auditoria(job_id: str, contenido: bytes) -> None:
    """Background task: upload, invoke, fetch. Runs in a worker thread.

    The caller (crear_auditoria) has already acquired _semaforo_auditorias
    before scheduling this task; it is released here no matter how the
    audit ends, so a slot is never leaked on error.
    """
    job = JOBS[job_id]
    aws = AWS_CTX
    try:
        job.state = "SUBIENDO"
        aws.s3.put_object(Bucket=aws.input_bucket, Key=f"inputs/{job_id}/document.pdf",
                          Body=contenido, ContentType="application/pdf")
        job.state = "PROCESANDO"
        respuesta = aws.lambda_client.invoke(
            FunctionName=aws.function_name,
            InvocationType="RequestResponse",
            Payload=json.dumps({"job_id": job_id}).encode(),
        )
        resultado = json.loads(respuesta["Payload"].read())
        if respuesta.get("FunctionError") or resultado.get("state") == "FAILED":
            job.state = "FAILED"
            job.error = resultado.get("error_type", "Error desconocido durante la auditoría")
            return
        prefix = resultado.get("result_prefix")
        if prefix:
            obj = aws.s3.get_object(Bucket=aws.result_bucket, Key=prefix + "resultado_auditoria.json")
            job.informe = json.loads(obj["Body"].read())
            # worker.py always stores the upload as inputs/{job_id}/document.pdf and
            # never learns the original filename, so documento_auditado in the JSON
            # is always "document.pdf". Restore the real name the browser sent us.
            if isinstance(job.informe, dict) and "documento_auditado" in job.informe:
                job.informe["documento_auditado"] = job.filename
            job.informe_editado = json.loads(json.dumps(job.informe))  # independent copy
            obj_md = aws.s3.get_object(Bucket=aws.result_bucket, Key=prefix + "reporte_auditoria.md")
            job.markdown = obj_md["Body"].read().decode("utf-8")
        job.state = resultado.get("state", "FAILED")
    except Exception as exc:  # noqa: BLE001 -- mirrors worker.py: never leak exception text
        job.state = "FAILED"
        job.error = type(exc).__name__
    finally:
        _semaforo_auditorias.release()


# --------------------------------------------------------------------------
# Pure-Python recompute of summary + Markdown after a human edit.
# Mirrors genera_resumen / veredicto_efectivo / guarda_reportes in
# app/audito_rag_pdf.py exactly -- kept in sync by hand, not imported, so
# this web layer stays free of the heavy LLM/embeddings dependencies.
# --------------------------------------------------------------------------
def _veredicto_efectivo(regla: dict) -> str:
    rev = regla.get("revision") or {}
    return rev.get("veredicto_final") or regla["dictamen"]["cumple"]


def _recalcula_resumen(detalle: list[dict]) -> dict:
    conteo = {v: 0 for v in VEREDICTOS}
    for r in detalle:
        conteo[_veredicto_efectivo(r)] += 1
    total = len(detalle)
    puntos = conteo["Cumple total"] + 0.5 * conteo["Cumple parcial"]
    porcentaje = round(100 * puntos / total, 1) if total else 0.0
    if conteo["No cumple"] == 0 and conteo["Cumple parcial"] == 0:
        veredicto = "CUMPLE"
    elif conteo["Cumple total"] == 0:
        veredicto = "NO CUMPLE"
    else:
        veredicto = "CUMPLE PARCIALMENTE"
    revisadas = sum(1 for r in detalle if (r.get("revision") or {}).get("revisado"))
    ajustadas = sum(1 for r in detalle if (r.get("revision") or {}).get("ajustado"))
    notas = _recopila_notas_desactualizacion(detalle)
    return {
        "veredicto_global": veredicto,
        "porcentaje_cumplimiento": porcentaje,
        "conteo": conteo,
        "reglas_evaluadas": total,
        "reglas_revisadas_por_humano": revisadas,
        "reglas_ajustadas_por_humano": ajustadas,
        "aviso_desactualizado": bool(notas),
        "notas_desactualizacion": notas,
    }


def _construye_markdown(documento: str, fecha: str, resumen: dict, detalle: list[dict]) -> str:
    c = resumen["conteo"]
    lineas = [
        "# Auditoría de Aviso de Privacidad — LFPDPPP", "",
        f"- **Documento:** {documento}",
        f"- **Fecha:** {fecha}",
        f"- **Veredicto global:** {resumen['veredicto_global']}",
        f"- **Cumplimiento:** {resumen['porcentaje_cumplimiento']}%  "
        f"(total: {c['Cumple total']}, parcial: {c['Cumple parcial']}, no cumple: {c['No cumple']})",
        f"- **Revisión humana:** {resumen['reglas_revisadas_por_humano']}/{resumen['reglas_evaluadas']} "
        f"reglas revisadas, {resumen['reglas_ajustadas_por_humano']} ajustadas",
        "",
    ]
    if resumen.get("aviso_desactualizado"):
        lineas += [
            "> ⚠️ **Nota de desactualización normativa**", ">",
            "> El aviso hace referencia al **INAI/IFAI** como autoridad de control. "
            "Tras la reforma de 2025, la vigilancia, protección y sanción en materia de "
            "datos personales corresponde a la **Secretaría Anticorrupción y Buen Gobierno**.", ">",
        ]
        lineas += [f"> - {n}" for n in resumen["notas_desactualizacion"]]
        lineas.append("")
    lineas += ["---", ""]
    for r in detalle:
        d = r["dictamen"]
        rev = r.get("revision") or {}
        final = _veredicto_efectivo(r)
        marca = ""
        if rev.get("ajustado"):
            marca = f"  ·  ⚠️ ajustado por revisor (IA decía: {rev.get('veredicto_ia')})"
        elif rev.get("revisado"):
            marca = "  ·  ✔️ revisado"
        if r.get("origen"):
            marca += f"  ·  ({r['origen']})"
        lineas += [
            f"## {r['regla_evaluada']}",
            f"*{r['referencia_legal']}*", "",
            f"- **Resultado:** {final}{marca}",
            f"- **Evidencia:** {d.get('evidencia_encontrada', 'Ninguna')}",
            f"- **Elementos faltantes:** {d.get('elementos_faltantes', 'Ninguno')}",
            f"- **Justificación:** {d.get('justificacion', '')}",
        ]
        if rev.get("nota_revisor"):
            lineas.append(f"- **Nota del revisor ({rev.get('revisor', 'N/D')}):** {rev['nota_revisor']}")
        lineas.append("")
    return "\n".join(lineas)


# --------------------------------------------------------------------------
# HTTP API
# --------------------------------------------------------------------------
class Edicion(BaseModel):
    id: str
    veredicto_final: str
    nota_revisor: str = ""


class RevisionRequest(BaseModel):
    revisor: str = "revisor"
    ediciones: list[Edicion]


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/app.js")
def app_js() -> FileResponse:
    return FileResponse(STATIC_DIR / "app.js", media_type="application/javascript")


@app.get("/styles.css")
def styles_css() -> FileResponse:
    return FileResponse(STATIC_DIR / "styles.css", media_type="text/css")


@app.post("/api/audits", status_code=202)
async def crear_auditoria(background_tasks: BackgroundTasks, pdf: UploadFile = File(...)):
    if not (pdf.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "Solo se aceptan archivos .pdf.")
    contenido = await pdf.read()
    if not contenido:
        raise HTTPException(400, "El archivo está vacío.")
    if len(contenido) > MAX_PDF_BYTES:
        raise HTTPException(400, f"El PDF excede el límite de {MAX_PDF_BYTES // (1024 * 1024)} MiB.")
    if not _semaforo_auditorias.acquire(blocking=False):
        raise HTTPException(
            429, f"Ya hay {MAX_AUDITORIAS_CONCURRENTES} auditorías en curso "
            "(el máximo permitido a la vez). Intenta de nuevo en unos minutos.")
    job_id = uuid.uuid4().hex
    JOBS[job_id] = Job(job_id=job_id, filename=pdf.filename)
    background_tasks.add_task(ejecuta_auditoria, job_id, contenido)
    return {"job_id": job_id}


@app.get("/api/audits/{job_id}")
def estado(job_id: str) -> dict:
    job = _obtiene_job(job_id)
    return {"job_id": job.job_id, "filename": job.filename, "state": job.state, "error": job.error}


@app.get("/api/audits/{job_id}/informe")
def informe(job_id: str) -> dict:
    job = _obtiene_job(job_id)
    if job.informe_editado is None:
        raise HTTPException(409, "La auditoría todavía no tiene un resultado disponible.")
    return job.informe_editado


@app.post("/api/audits/{job_id}/revision")
def revisar(job_id: str, cuerpo: RevisionRequest) -> dict:
    job = _obtiene_job(job_id)
    if job.state != "SUCCEEDED" or job.informe_editado is None:
        raise HTTPException(409, "Solo se puede editar una auditoría completada (no rechazada ni fallida).")
    informe_actual = job.informe_editado
    por_id = {r["id"]: r for r in informe_actual["detalle"]}
    ahora = datetime.now().isoformat(timespec="seconds")
    for edicion in cuerpo.ediciones:
        regla = por_id.get(edicion.id)
        if regla is None:
            continue
        if edicion.veredicto_final not in VEREDICTOS:
            raise HTTPException(400, f"Veredicto inválido: {edicion.veredicto_final!r}")
        veredicto_ia = regla["dictamen"]["cumple"]
        regla["revision"] = {
            "revisado": True,
            "veredicto_ia": veredicto_ia,
            "veredicto_final": edicion.veredicto_final,
            "ajustado": edicion.veredicto_final != veredicto_ia,
            "reevaluado": False,
            "nota_revisor": edicion.nota_revisor,
            "revisor": cuerpo.revisor or "revisor",
            "fecha": ahora,
        }
    informe_actual["resumen"] = _recalcula_resumen(informe_actual["detalle"])
    with job.lock:
        job.informe_editado = informe_actual
        job.markdown = _construye_markdown(
            informe_actual["documento_auditado"], informe_actual["fecha_auditoria"],
            informe_actual["resumen"], informe_actual["detalle"],
        )
    return informe_actual


@app.get("/api/audits/{job_id}/descarga/json")
def descarga_json(job_id: str) -> Response:
    job = _obtiene_job(job_id)
    if job.informe_editado is None:
        raise HTTPException(409, "Nada que descargar todavía.")
    cuerpo = json.dumps(job.informe_editado, indent=2, ensure_ascii=False)
    return Response(cuerpo, media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="auditoria_{job_id}.json"'})


@app.get("/api/audits/{job_id}/descarga/md")
def descarga_md(job_id: str) -> Response:
    job = _obtiene_job(job_id)
    if job.markdown is None:
        raise HTTPException(409, "Nada que descargar todavía.")
    return Response(job.markdown, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="auditoria_{job_id}.md"'})


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Interfaz web local para el auditor desplegado en AWS.")
    parser.add_argument("--outputs", type=Path, default=ROOT / "cdk-outputs.json")
    parser.add_argument("--profile")
    parser.add_argument("--region", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    outputs = json.loads(args.outputs.read_text())["NlpAuditStack"]
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    global AWS_CTX
    AWS_CTX = AWSContext(session, outputs["InputBucket"], outputs["ResultBucket"], outputs["FunctionName"])

    import uvicorn
    print(f"Auditor web: http://{args.host}:{args.port}  (Ctrl+C para detener)")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
