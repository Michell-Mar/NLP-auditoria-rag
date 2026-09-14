"""API Lambda for the serverless variant: FastAPI wrapped with Mangum.

Unlike webapp/ and webapp-pdf/ (a long-lived local process holding job state
in a Python dict and a threading.Semaphore), this runs as a Lambda behind
API Gateway HTTP API -- there is no guarantee two requests for the same job
hit the same execution environment, so state lives in DynamoDB and the
concurrency cap lives on the SQS-triggered worker (see worker.py + the CDK
stack's max_concurrency), not in this process.

This Lambda does NOT run the audit itself. It only:
  - reserves a job_id and hands back a presigned S3 PUT URL (the browser
    uploads the PDF directly to S3 -- a 10 MiB file base64-encoded would
    exceed a synchronous Lambda invocation's 6 MB payload limit if it went
    through API Gateway instead),
  - enqueues an SQS message once the upload is done,
  - reads/edits the job record that worker.py writes after invoking the
    existing, unchanged audit Lambda (NlpAuditStack's Worker),
  - renders the PDF report on demand from the stored JSON.
"""
import io
import json
import os
import re
import time
import uuid
from datetime import datetime
from xml.sax.saxutils import escape as xml_escape

import boto3
from botocore.exceptions import ClientError
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response
from mangum import Mangum
from pathlib import Path
from pydantic import BaseModel
from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (KeepTogether, ListFlowable, ListItem, Paragraph,
                                SimpleDocTemplate, Spacer, Table, TableStyle)

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_PDF_BYTES = 10 * 1024 * 1024
UPLOAD_URL_TTL_SECONDS = 900
JOB_TTL_DAYS = 7
# Item reservado en la misma tabla de Jobs para el contador global de usos --
# nunca puede colisionar con un job_id real (uuid4().hex, siempre 32 hex).
USO_COUNTER_KEY = "__uso_contador__"

app = FastAPI(title="Auditor de Avisos de Privacidad (serverless)")


# --------------------------------------------------------------------------
# AWS clients -- created lazily inside each function (not at import time),
# so tests can monkeypatch `server.boto3` the same way tests/test_worker.py
# already does for app/worker.py.
# --------------------------------------------------------------------------
def _tabla():
    return boto3.resource("dynamodb").Table(os.environ["JOBS_TABLE"])


def _s3():
    return boto3.client("s3")


def _sqs():
    return boto3.client("sqs")


def _obtiene_job(job_id: str) -> dict:
    item = _tabla().get_item(Key={"job_id": job_id}).get("Item")
    if item is None:
        raise HTTPException(404, "Auditoría no encontrada.")
    return item


def _informe_de(item: dict) -> dict:
    informe = item.get("informe")
    if informe is None:
        raise HTTPException(409, "La auditoría todavía no tiene un resultado disponible.")
    return json.loads(informe)


def _limite_auditorias() -> int:
    return int(os.environ.get("MAX_AUDIT_USES", "10"))


def _registra_uso() -> None:
    """Incrementa el contador global de usos de forma atómica y rechaza la
    solicitud si ya se alcanzó el tope. Se llama al ENCOLAR la auditoría
    (no al crearla), porque ese es el momento en que de verdad se compromete
    el costo real (worker Lambda + llamadas al LLM) -- crear un job sin
    llegar a procesarlo (p. ej. el usuario nunca sube el PDF) no debería
    consumir el cupo.

    El ConditionExpression hace que el incremento y la verificación del tope
    sean una sola operación atómica en DynamoDB: bajo solicitudes
    concurrentes, nunca se puede rebasar el límite por una condición de
    carrera (dos requests leyendo el mismo valor antes de que ninguna escriba).
    """
    limite = _limite_auditorias()
    try:
        _tabla().update_item(
            Key={"job_id": USO_COUNTER_KEY},
            UpdateExpression="ADD contador :uno",
            ConditionExpression="attribute_not_exists(contador) OR contador < :max",
            ExpressionAttributeValues={":uno": 1, ":max": limite},
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise HTTPException(
                429,
                f"Se alcanzó el límite de {limite} auditorías disponibles en esta demo. "
                "Contacta al administrador si necesitas más.",
            )
        raise


# --------------------------------------------------------------------------
# Verdict labels + pure-Python recompute of summary after a human edit.
# Mirrors genera_resumen / veredicto_efectivo / recopila_notas_desactualizacion
# in app/audito_rag_pdf.py exactly -- kept in sync by hand, not imported, so
# this Lambda never has to install langchain/openai just to render a report.
# --------------------------------------------------------------------------
VEREDICTOS = ("Cumple total", "Cumple parcial", "No cumple")
_VEREDICTOS_VACIOS = {"", "ninguna", "ninguno", "n/a", "na", "none", "no aplica"}


def _es_vacio(valor) -> bool:
    return (valor or "").strip().lower() in _VEREDICTOS_VACIOS


def _firma_nota(nota: str) -> str:
    low = nota.lower()
    if "inai" in low or "ifai" in low or "instituto nacional de transparencia" in low:
        return "inai_ifai"
    return re.sub(r"[^a-z0-9]", "", low)[:80]


def _recopila_notas_desactualizacion(detalle: list) -> list:
    por_firma = {}
    for r in detalle:
        nota = (r["dictamen"].get("nota_desactualizacion") or "").strip()
        if _es_vacio(nota):
            continue
        firma = _firma_nota(nota)
        if firma not in por_firma or len(nota) > len(por_firma[firma]):
            por_firma[firma] = nota
    return list(por_firma.values())


def _veredicto_efectivo(regla: dict) -> str:
    rev = regla.get("revision") or {}
    return rev.get("veredicto_final") or regla["dictamen"]["cumple"]


def _recalcula_resumen(detalle: list) -> dict:
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


# --------------------------------------------------------------------------
# PDF rendering -- identical to webapp-pdf/server.py's build_pdf_report.
# See that file's comments for why emoji are avoided (base PDF fonts render
# them as solid boxes) and why the delimiters are plain "cards".
# --------------------------------------------------------------------------
_HEX_VEREDICTO = {
    "Cumple total": "#2e7a38", "CUMPLE": "#2e7a38",
    "Cumple parcial": "#9e5b0b", "CUMPLE PARCIALMENTE": "#9e5b0b",
    "No cumple": "#ae3b2c", "NO CUMPLE": "#ae3b2c",
}
_INK = "#2a251c"


def _color_veredicto(veredicto: str) -> str:
    return _HEX_VEREDICTO.get(veredicto, _INK)


def _pdf_escape(valor) -> str:
    return xml_escape(str(valor if valor is not None else ""))


def _pdf_styles() -> dict:
    base = getSampleStyleSheet()
    return {
        "titulo": ParagraphStyle("titulo", parent=base["Title"], fontSize=16,
                                 spaceAfter=10, textColor=colors.HexColor(_INK)),
        "meta": ParagraphStyle("meta", parent=base["Normal"], fontSize=9.5,
                               leading=14, textColor=colors.HexColor(_INK)),
        "aviso_titulo": ParagraphStyle("aviso_titulo", parent=base["Normal"], fontSize=9.5,
                                       leading=13, fontName="Helvetica-Bold",
                                       textColor=colors.HexColor("#5c3a06")),
        "aviso_cuerpo": ParagraphStyle("aviso_cuerpo", parent=base["Normal"], fontSize=8.8,
                                       leading=12.5, textColor=colors.HexColor("#5c3a06")),
        "regla_titulo": ParagraphStyle("regla_titulo", parent=base["Normal"], fontSize=10.5,
                                       leading=14, fontName="Helvetica-Bold",
                                       textColor=colors.HexColor(_INK)),
        "referencia": ParagraphStyle("referencia", parent=base["Normal"], fontSize=8.3,
                                     leading=11, fontName="Helvetica-Oblique",
                                     textColor=colors.HexColor("#6b6152"), spaceAfter=4),
        "campo": ParagraphStyle("campo", parent=base["Normal"], fontSize=9,
                                leading=12.5, textColor=colors.HexColor(_INK), spaceAfter=2),
        "rechazo_titulo": ParagraphStyle("rechazo_titulo", parent=base["Title"], fontSize=15,
                                         textColor=colors.HexColor(_HEX_VEREDICTO["No cumple"])),
    }


def build_pdf_report(informe: dict) -> bytes:
    estilos = _pdf_styles()
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=LETTER, title="Auditoria de Aviso de Privacidad",
        topMargin=1.7 * cm, bottomMargin=1.7 * cm, leftMargin=1.9 * cm, rightMargin=1.9 * cm,
    )
    flujo = [
        Paragraph("Auditoría de Aviso de Privacidad — LFPDPPP", estilos["titulo"]),
        Paragraph(f"<b>Documento:</b> {_pdf_escape(informe.get('documento_auditado'))}", estilos["meta"]),
        Paragraph(f"<b>Fecha:</b> {_pdf_escape(informe.get('fecha_auditoria'))}", estilos["meta"]),
    ]

    if informe.get("resultado") == "RECHAZADO":
        v = informe["validacion"]
        flujo += [
            Spacer(1, 10),
            Paragraph("RECHAZADO — no parece un aviso de privacidad", estilos["rechazo_titulo"]),
            Spacer(1, 6),
            Paragraph(f"<b>Tipo de documento detectado:</b> {_pdf_escape(v.get('tipo_documento_detectado'))}", estilos["campo"]),
            Paragraph(f"<b>Confianza:</b> {_pdf_escape(v.get('confianza'))}", estilos["campo"]),
            Paragraph(f"<b>Motivo:</b> {_pdf_escape(v.get('motivo'))}", estilos["campo"]),
        ]
        doc.build(flujo)
        return buffer.getvalue()

    resumen = informe["resumen"]
    c = resumen["conteo"]
    flujo += [
        Paragraph(
            f"<b>Veredicto global:</b> "
            f"<font color='{_color_veredicto(resumen['veredicto_global'])}'>"
            f"<b>{_pdf_escape(resumen['veredicto_global'])}</b></font>",
            estilos["meta"]),
        Paragraph(
            f"<b>Cumplimiento:</b> {resumen['porcentaje_cumplimiento']}% "
            f"(total: {c['Cumple total']}, parcial: {c['Cumple parcial']}, no cumple: {c['No cumple']})",
            estilos["meta"]),
        Paragraph(
            f"<b>Revisión humana:</b> {resumen['reglas_revisadas_por_humano']}/{resumen['reglas_evaluadas']} "
            f"reglas revisadas, {resumen['reglas_ajustadas_por_humano']} ajustadas",
            estilos["meta"]),
        Spacer(1, 8),
    ]

    if resumen.get("aviso_desactualizado"):
        cuerpo_aviso = [
            Paragraph("AVISO: nota de desactualización normativa", estilos["aviso_titulo"]),
            Paragraph(
                "El aviso hace referencia al INAI/IFAI como autoridad de control. Tras la "
                "reforma de 2025, la vigilancia, protección y sanción en materia de datos "
                "personales corresponde a la Secretaría Anticorrupción y Buen Gobierno.",
                estilos["aviso_cuerpo"]),
        ]
        items = [ListItem(Paragraph(_pdf_escape(n), estilos["aviso_cuerpo"]))
                for n in resumen["notas_desactualizacion"]]
        if items:
            cuerpo_aviso.append(ListFlowable(items, bulletType="bullet", leftIndent=12))
        tabla_aviso = Table([[cuerpo_aviso]], colWidths=[doc.width])
        tabla_aviso.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f6ead4")),
            ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
            ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ]))
        flujo += [tabla_aviso, Spacer(1, 12)]

    for r in informe["detalle"]:
        d = r["dictamen"]
        rev = r.get("revision") or {}
        final = _veredicto_efectivo(r)
        marca = ""
        if rev.get("ajustado"):
            marca = f"  ·  <b>AVISO: ajustado por revisor (la IA decía: {_pdf_escape(rev.get('veredicto_ia'))})</b>"
        elif rev.get("revisado"):
            marca = "  ·  <b>[Revisado]</b>"
        if r.get("origen"):
            marca += f"  ·  ({_pdf_escape(r['origen'])})"

        contenido = [
            Paragraph(_pdf_escape(r["regla_evaluada"]), estilos["regla_titulo"]),
            Paragraph(_pdf_escape(r["referencia_legal"]), estilos["referencia"]),
            Paragraph(
                f"<b>Resultado:</b> <font color='{_color_veredicto(final)}'>"
                f"<b>{_pdf_escape(final)}</b></font>{marca}",
                estilos["campo"]),
            Paragraph(f"<b>Evidencia:</b> {_pdf_escape(d.get('evidencia_encontrada', 'Ninguna'))}", estilos["campo"]),
            Paragraph(f"<b>Elementos faltantes:</b> {_pdf_escape(d.get('elementos_faltantes', 'Ninguno'))}", estilos["campo"]),
            Paragraph(f"<b>Justificación:</b> {_pdf_escape(d.get('justificacion', ''))}", estilos["campo"]),
        ]
        if rev.get("nota_revisor"):
            contenido.append(Paragraph(
                f"<b>Nota del revisor ({_pdf_escape(rev.get('revisor', 'N/D'))}):</b> "
                f"{_pdf_escape(rev['nota_revisor'])}",
                estilos["campo"]))

        tarjeta = Table([[contenido]], colWidths=[doc.width])
        tarjeta.setStyle(TableStyle([
            ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#e2dccf")),
            ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
            ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ]))
        flujo.append(KeepTogether([tarjeta, Spacer(1, 10)]))

    doc.build(flujo)
    return buffer.getvalue()


# --------------------------------------------------------------------------
# HTTP API
# --------------------------------------------------------------------------
class NuevaAuditoria(BaseModel):
    filename: str


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


@app.post("/api/audits", status_code=201)
def crear_auditoria(cuerpo: NuevaAuditoria) -> dict:
    if not cuerpo.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Solo se aceptan archivos .pdf.")
    job_id = uuid.uuid4().hex
    ahora = int(time.time())
    _tabla().put_item(Item={
        "job_id": job_id,
        "filename": cuerpo.filename,
        "state": "PENDIENTE",
        "creado_en": ahora,
        "ttl": ahora + JOB_TTL_DAYS * 86400,
    })
    upload_url = _s3().generate_presigned_url(
        "put_object",
        Params={"Bucket": os.environ["INPUT_BUCKET"], "Key": f"inputs/{job_id}/document.pdf",
                "ContentType": "application/pdf"},
        ExpiresIn=UPLOAD_URL_TTL_SECONDS,
    )
    return {"job_id": job_id, "upload_url": upload_url}


@app.post("/api/audits/{job_id}/procesar", status_code=202)
def procesar_auditoria(job_id: str) -> dict:
    item = _obtiene_job(job_id)
    if item["state"] != "PENDIENTE":
        raise HTTPException(409, f"La auditoría ya está en estado {item['state']}.")
    _registra_uso()
    _sqs().send_message(QueueUrl=os.environ["JOBS_QUEUE_URL"],
                        MessageBody=json.dumps({"job_id": job_id}))
    _tabla().update_item(Key={"job_id": job_id}, UpdateExpression="SET #s = :s",
                         ExpressionAttributeNames={"#s": "state"},
                         ExpressionAttributeValues={":s": "PROCESANDO"})
    return {"state": "PROCESANDO"}


@app.get("/api/audits/{job_id}")
def estado(job_id: str) -> dict:
    item = _obtiene_job(job_id)
    return {"job_id": item["job_id"], "filename": item.get("filename"),
            "state": item["state"], "error": item.get("error")}


@app.get("/api/audits/{job_id}/informe")
def informe(job_id: str) -> dict:
    return _informe_de(_obtiene_job(job_id))


@app.post("/api/audits/{job_id}/revision")
def revisar(job_id: str, cuerpo: RevisionRequest) -> dict:
    item = _obtiene_job(job_id)
    if item["state"] != "SUCCEEDED":
        raise HTTPException(409, "Solo se puede editar una auditoría completada (no rechazada ni fallida).")
    informe_actual = _informe_de(item)
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
    _tabla().update_item(Key={"job_id": job_id}, UpdateExpression="SET informe = :i",
                         ExpressionAttributeValues={":i": json.dumps(informe_actual)})
    return informe_actual


@app.get("/api/audits/{job_id}/descarga/json")
def descarga_json(job_id: str) -> Response:
    cuerpo = json.dumps(_informe_de(_obtiene_job(job_id)), indent=2, ensure_ascii=False)
    return Response(cuerpo, media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="auditoria_{job_id}.json"'})


@app.get("/api/audits/{job_id}/descarga/pdf")
def descarga_pdf(job_id: str) -> Response:
    pdf_bytes = build_pdf_report(_informe_de(_obtiene_job(job_id)))
    return Response(pdf_bytes, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="auditoria_{job_id}.pdf"'})


handler = Mangum(app)
