import os
import re
import sys
import json
from datetime import datetime

from langchain_community.document_loaders import PyPDFLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_openai import OpenAIEmbeddings, ChatOpenAI
from langchain_core.vectorstores import InMemoryVectorStore
from langchain_core.prompts import ChatPromptTemplate
from dotenv import load_dotenv

# Configuración de credenciales
load_dotenv()


# ==========================================
# FASE 1: INGESTA Y CHUNKING
# ==========================================
def fase_1_ingesta_y_chunking(file_path: str):
    """
    Carga, limpieza de ruido y chunking del aviso de privacidad en PDF.
    """
    print(f"Cargando documento PDF: {file_path}")
    loader = PyPDFLoader(file_path)
    documentos = loader.load()

    # Preprocesamiento de limpieza
    for doc in documentos:
        # Elimina líneas de "Fecha de la actualización ..."
        doc.page_content = re.sub(
            r"Fecha de la actualización[^\n]*",
            "",
            doc.page_content,
            flags=re.IGNORECASE,
        )
        # Colapsa saltos de línea múltiples
        doc.page_content = re.sub(r"\n{3,}", "\n\n", doc.page_content)

    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=800,
        chunk_overlap=200,
        separators=[r"\n\n", r"(?<!:)\n", r"\n", r"\.", " "],
        is_separator_regex=True,
    )

    chunks = text_splitter.split_documents(documentos)
    print(f"Documento dividido en {len(chunks)} fragmentos.")

    # Texto completo (para preguntas donde la recuperación semántica se queda corta)
    texto_completo = "\n".join(doc.page_content for doc in documentos)

    # Guardar los chunks para inspección
    try:
        with open("chunks_generados.txt", "w", encoding="utf-8") as f:
            for i, chunk in enumerate(chunks):
                f.write(f"--- CHUNK {i + 1} ---\n{chunk.page_content}\n")
                f.write("---------------------\n\n")
    except IOError as e:
        print(f"Advertencia: No se pudo escribir chunks_generados.txt. Error: {e}")

    return chunks, texto_completo


# ==========================================
# FASE 2: VECTORIZACIÓN
# ==========================================
def fase_2_vectorizacion(chunks):
    """
    Convierte fragmentos en embeddings y los indexa en memoria.
    """
    print("Vectorizando e indexando en InMemoryVectorStore...")
    embeddings_model = OpenAIEmbeddings(model="text-embedding-3-small")
    return InMemoryVectorStore.from_documents(chunks, embeddings_model)


# ==========================================
# FASE 3: DICCIONARIO NORMATIVO (REGLAS LFPDPPP)
# ==========================================
# Cada regla incluye la referencia legal para enriquecer la justificación del LLM.
DICCIONARIO_LFPDPPP = [
    {
        "id": "identidad_domicilio_responsable",
        "pregunta": "¿Se señala la identidad (nombre o razón social) y el domicilio del responsable que recaba los datos personales?",
        "referencia": "Art. 16 fr. I LFPDPPP; Art. 27 fr. I Reglamento",
    },
    {
        "id": "datos_recabados",
        "pregunta": "¿Se indican las categorías o el listado de los datos personales que se someterán a tratamiento?",
        "referencia": "Art. 27 fr. II Reglamento (en relación con Art. 16 LFPDPPP)",
    },
    {
        "id": "datos_sensibles",
        "pregunta": "Si se tratan datos personales sensibles, ¿se señala expresamente esa circunstancia y se recaba el consentimiento expreso y por escrito del titular?",
        "referencia": "Art. 8 y Art. 9 LFPDPPP; Art. 9 y Art. 56 Reglamento",
    },
    {
        "id": "finalidades",
        "pregunta": "¿Se mencionan explícitamente las finalidades principales (necesarias) y secundarias del tratamiento de los datos, y se distingue entre ambas?",
        "referencia": "Art. 16 fr. II LFPDPPP; Art. 27 fr. III y Art. 40 Reglamento",
    },
    {
        "id": "limitar_uso_divulgacion",
        "pregunta": "¿Se ofrecen opciones y medios para que el titular limite el uso o divulgación de sus datos (p. ej. mecanismo para negar el tratamiento de finalidades secundarias, listado de exclusión)?",
        "referencia": "Art. 16 fr. III LFPDPPP; Art. 27 fr. IV Reglamento",
    },
    {
        "id": "derechos_arco",
        "pregunta": "¿Se indican los medios y procedimientos específicos para que el titular ejerza sus derechos ARCO (Acceso, Rectificación, Cancelación, Oposición)?",
        "referencia": "Art. 16 fr. IV y Art. 22-24 LFPDPPP",
    },
    {
        "id": "cambios_aviso",
        "pregunta": "¿Se indica expresamente el procedimiento y el medio por el cual se comunicarán al titular los cambios o actualizaciones al aviso de privacidad?",
        "referencia": "Art. 16 fr. VI LFPDPPP; Art. 18 Reglamento",
    },
    {
        "id": "transferencias",
        "pregunta": "¿Se mencionan las transferencias de datos a terceros, se identifica al destinatario y se especifica la finalidad de dichas transferencias, incluyendo la cláusula para negarlas?",
        "referencia": "Art. 16 fr. V y Art. 36-37 LFPDPPP; Art. 68 Reglamento",
    },
    {
        "id": "revocacion_consentimiento",
        "pregunta": "¿Se detallan los mecanismos y el procedimiento para que el titular pueda revocar su consentimiento para el tratamiento de sus datos?",
        "referencia": "Art. 8 y Art. 16 fr. IV LFPDPPP; Art. 21 Reglamento",
    },
]


# ==========================================
# FASES 4 Y 5: RECUPERACIÓN + DICTAMEN
# ==========================================
_PROMPT = ChatPromptTemplate.from_messages([
    ("system", """Eres un auditor legal experto en la Ley Federal de Protección de Datos Personales \
en Posesión de los Particulares (LFPDPPP) de México y su Reglamento.

Evalúa si el aviso de privacidad cumple con la siguiente regla normativa.

REGLA A EVALUAR: {query}
REFERENCIA LEGAL: {referencia}

CONTEXTO extraído del aviso de privacidad:
{context}

Instrucciones:
- Basa tu dictamen ÚNICAMENTE en el CONTEXTO proporcionado.
- "Cumple total" = el aviso satisface todos los elementos exigidos por la regla.
- "Cumple parcial" = aborda el tema pero omite algún elemento (p. ej. menciona ARCO pero no el medio para ejercerlos).
- "No cumple" = el contexto no contiene información que satisfaga la regla.
- Si no hay evidencia, responde "No cumple" y evidencia "Ninguna".

Responde ESTRICTAMENTE en JSON con esta estructura:
{{
    "cumple": "Cumple total | Cumple parcial | No cumple",
    "evidencia_encontrada": "Cita textual breve del aviso o 'Ninguna'",
    "elementos_faltantes": "Qué elementos concretos faltan, o 'Ninguno'",
    "justificacion": "Razonamiento legal breve"
}}"""),
])


def _normaliza_veredicto(valor: str) -> str:
    v = (valor or "").strip().lower()
    if "parcial" in v:
        return "Cumple parcial"
    if v.startswith("no") or "no cumple" in v:
        return "No cumple"
    if "cumple" in v or v in ("sí", "si", "total"):
        return "Cumple total"
    return "No cumple"


# Si el aviso completo cabe cómodamente en el contexto del modelo (~15k tokens),
# se evalúa contra el texto íntegro y la recuperación semántica solo actúa como
# respaldo para documentos grandes.
MAX_CHARS_DOC_COMPLETO = 60_000


def fase_4_y_5_auditoria(vector_store, texto_completo, reglas, k=8):
    llm = ChatOpenAI(
        model="gpt-4o",
        temperature=0,
        model_kwargs={"response_format": {"type": "json_object"}},
    )
    chain = _PROMPT | llm
    resultados = []
    doc_completo = len(texto_completo) <= MAX_CHARS_DOC_COMPLETO
    if doc_completo:
        print(f"Aviso pequeño ({len(texto_completo)} car.): se evalúa contra el texto completo.")
    else:
        print(f"Aviso grande ({len(texto_completo)} car.): se usa recuperación semántica (k={k}).")

    print("Iniciando auditoría iterativa de principios legales...")
    for regla in reglas:
        pregunta = regla["pregunta"]

        if doc_completo:
            contexto_final = texto_completo
        else:
            docs = vector_store.similarity_search(pregunta, k=k)
            # Dedup preservando orden
            vistos, fragmentos = set(), []
            for d in docs:
                if d.page_content not in vistos:
                    vistos.add(d.page_content)
                    fragmentos.append(d.page_content)
            contexto_final = "\n---\n".join(fragmentos)

        respuesta = chain.invoke({
            "query": pregunta,
            "referencia": regla["referencia"],
            "context": contexto_final,
        })

        try:
            dictamen = json.loads(respuesta.content)
        except json.JSONDecodeError:
            dictamen = {
                "cumple": "No cumple",
                "evidencia_encontrada": "Ninguna",
                "elementos_faltantes": "Respuesta del modelo no parseable",
                "justificacion": respuesta.content[:500],
            }

        dictamen["cumple"] = _normaliza_veredicto(dictamen.get("cumple"))
        resultados.append({
            "id": regla["id"],
            "regla_evaluada": pregunta,
            "referencia_legal": regla["referencia"],
            "dictamen": dictamen,
        })
        print(f"  [{dictamen['cumple']:>15}]  {regla['id']}")

    return resultados


# ==========================================
# FASE 6: RESUMEN Y REPORTE
# ==========================================
def genera_resumen(resultados):
    conteo = {"Cumple total": 0, "Cumple parcial": 0, "No cumple": 0}
    for r in resultados:
        conteo[r["dictamen"]["cumple"]] += 1

    total = len(resultados)
    # Puntaje: total=1, parcial=0.5, no=0
    puntos = conteo["Cumple total"] + 0.5 * conteo["Cumple parcial"]
    porcentaje = round(100 * puntos / total, 1) if total else 0.0

    if conteo["No cumple"] == 0 and conteo["Cumple parcial"] == 0:
        veredicto = "CUMPLE"
    elif conteo["Cumple total"] == 0:
        veredicto = "NO CUMPLE"
    else:
        veredicto = "CUMPLE PARCIALMENTE"

    return {
        "veredicto_global": veredicto,
        "porcentaje_cumplimiento": porcentaje,
        "conteo": conteo,
        "reglas_evaluadas": total,
    }


def guarda_reportes(resultados, resumen, ruta_pdf):
    salida = {
        "documento_auditado": os.path.basename(ruta_pdf),
        "fecha_auditoria": datetime.now().isoformat(timespec="seconds"),
        "resumen": resumen,
        "detalle": resultados,
    }
    with open("resultado_auditoria.json", "w", encoding="utf-8") as f:
        json.dump(salida, f, indent=2, ensure_ascii=False)

    lineas = [
        f"# Auditoría de Aviso de Privacidad — LFPDPPP",
        "",
        f"- **Documento:** {salida['documento_auditado']}",
        f"- **Fecha:** {salida['fecha_auditoria']}",
        f"- **Veredicto global:** {resumen['veredicto_global']}",
        f"- **Cumplimiento:** {resumen['porcentaje_cumplimiento']}%  "
        f"(total: {resumen['conteo']['Cumple total']}, "
        f"parcial: {resumen['conteo']['Cumple parcial']}, "
        f"no cumple: {resumen['conteo']['No cumple']})",
        "",
        "---",
        "",
    ]
    for r in resultados:
        d = r["dictamen"]
        lineas += [
            f"## {r['regla_evaluada']}",
            f"*{r['referencia_legal']}*",
            "",
            f"- **Resultado:** {d['cumple']}",
            f"- **Evidencia:** {d.get('evidencia_encontrada', 'Ninguna')}",
            f"- **Elementos faltantes:** {d.get('elementos_faltantes', 'Ninguno')}",
            f"- **Justificación:** {d.get('justificacion', '')}",
            "",
        ]
    with open("reporte_auditoria.md", "w", encoding="utf-8") as f:
        f.write("\n".join(lineas))

    print("\nReportes guardados: resultado_auditoria.json, reporte_auditoria.md")


# ==========================================
# MAIN
# ==========================================
if __name__ == "__main__":
    ruta_aviso = sys.argv[1] if len(sys.argv) > 1 else "aviso_privacidad.pdf"

    try:
        chunks, texto_completo = fase_1_ingesta_y_chunking(ruta_aviso)
        db_vectorial = fase_2_vectorizacion(chunks)
        resultados = fase_4_y_5_auditoria(db_vectorial, texto_completo, DICCIONARIO_LFPDPPP)
        resumen = genera_resumen(resultados)

        print("\n=== RESUMEN DE LA AUDITORÍA ===")
        print(json.dumps(resumen, indent=2, ensure_ascii=False))

        guarda_reportes(resultados, resumen, ruta_aviso)

    except Exception as e:
        print(f"Error durante la ejecución del pipeline: {e}")
        raise
