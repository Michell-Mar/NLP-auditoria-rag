import os
import re
import sys
import json
import argparse
import difflib
from collections import Counter
from datetime import datetime

from pypdf import PdfReader
from langchain_community.document_loaders import PyPDFLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_openai import OpenAIEmbeddings, ChatOpenAI
from langchain_core.vectorstores import InMemoryVectorStore
from langchain_core.prompts import ChatPromptTemplate
from dotenv import load_dotenv

# Configuración de credenciales
load_dotenv()

VEREDICTOS = ("Cumple total", "Cumple parcial", "No cumple")


# ==========================================
# FASE 1: INGESTA Y CHUNKING
# ==========================================
def _fragmentos_con_posicion(page):
    """Posición (x, y) de cada fragmento de texto de una página, en espacio de página."""
    frags = []

    def _visitor(texto, cm, tm, font_dict, font_size):
        if texto and texto.strip():
            # El origen del texto está en espacio de texto (tm); hay que
            # llevarlo a espacio de página aplicando la matriz de transformación
            # vigente (cm) — si no, en PDFs con "cm" no identidad las x salen
            # fuera del ancho de la página y cualquier detección de columnas sale mal.
            x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
            y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
            frags.append((x, y, texto))

    try:
        page.extract_text(visitor_text=_visitor)
    except Exception:
        return []
    return frags


def _punto_de_corte_columnas(frags, ancho, cobertura_min=0.5, hueco_min_frac=0.30):
    """
    Busca dos márgenes izquierdos (posiciones x) dominantes y bien separados
    entre sí -- la firma de dos columnas de texto alineado. Un solo párrafo
    con indentado variable no produce esta firma (sus x dominantes quedan
    cerca entre sí). Devuelve el punto medio entre ambos márgenes, o None.
    """
    xs = [f[0] for f in frags]
    comunes = Counter(round(x / 4) * 4 for x in xs).most_common(2)
    if len(comunes) < 2:
        return None
    (x1, n1), (x2, n2) = comunes
    cobertura = (n1 + n2) / len(xs)
    hueco = abs(x1 - x2)
    if cobertura < cobertura_min or hueco < ancho * hueco_min_frac:
        return None
    return (x1 + x2) / 2


def _texto_por_columna(frags, tolerancia_y=3):
    """Agrupa fragmentos de una columna en líneas (por y) y las ordena de arriba a abajo."""
    frags = sorted(frags, key=lambda f: (-f[1], f[0]))
    lineas, linea_actual, y_linea = [], [], None
    for x, y, texto in frags:
        if y_linea is None or abs(y - y_linea) <= tolerancia_y:
            linea_actual.append((x, texto))
            if y_linea is None:
                y_linea = y
        else:
            lineas.append(linea_actual)
            linea_actual, y_linea = [(x, texto)], y
    if linea_actual:
        lineas.append(linea_actual)
    return "\n".join(
        "".join(t for _, t in sorted(linea, key=lambda p: p[0])) for linea in lineas
    )


def _guardia_columnas(page, texto_plano):
    """
    Guardia contra maquetación a 2 columnas (frecuente en avisos de bancos y
    aseguradoras). pypdf extrae el texto en el orden del content stream del
    PDF; casi siempre coincide con el orden de lectura visual incluso a
    2 columnas, pero no hay garantía. Esta guardia:
      1) detecta geométricamente si la página tiene 2 columnas,
      2) si las tiene, reconstruye el texto por posición (columna izquierda
         completa y luego la derecha),
      3) solo LO USA si diverge de forma significativa del texto plano de
         pypdf -- si ya coinciden, se deja el original sin tocar.
    Devuelve (texto_final, se_corrigió).
    """
    frags = _fragmentos_con_posicion(page)
    if len(frags) < 20:
        return texto_plano, False  # muy poco texto para decidir con confianza

    ancho = float(page.mediabox.width)
    corte = _punto_de_corte_columnas(frags, ancho)
    if corte is None:
        return texto_plano, False  # geometría de una sola columna

    reconstruido = (
        _texto_por_columna([f for f in frags if f[0] < corte])
        + "\n\n"
        + _texto_por_columna([f for f in frags if f[0] >= corte])
    )
    similitud = difflib.SequenceMatcher(
        None, texto_plano.split(), reconstruido.split()
    ).ratio()
    if similitud < 0.85:
        return reconstruido, True
    return texto_plano, False


# Muchos avisos (bancos, aseguradoras) están redactados en secciones numeradas:
# "1. EL RESPONSABLE...", "7. MEDIOS Y PROCEDIMIENTOS PARA EJERCER LOS DERECHOS...".
_PATRON_SECCION = re.compile(r"(?m)^\s*(\d{1,2})\.\s+[A-ZÁÉÍÓÚÑÜ]")


def _segmenta_por_secciones(texto, minimo_secciones=4):
    """
    Si el aviso tiene estructura numerada, corta ahí en vez de a ciegas por
    tamaño de caracteres -- así una regla (p. ej. "7. DERECHOS ARCO") nunca
    queda partida a la mitad entre dos chunks ni mezclada con la sección
    vecina. Devuelve una lista de (titulo, bloque_de_texto).
    Si no detecta suficiente estructura (aviso redactado como narrativa
    continua, sin numerar), devuelve None y se usa el splitter genérico.
    """
    posiciones = [m.start() for m in _PATRON_SECCION.finditer(texto)]
    if len(posiciones) < minimo_secciones:
        return None
    posiciones.append(len(texto))
    secciones = []
    for inicio, fin in zip(posiciones, posiciones[1:]):
        bloque = texto[inicio:fin].strip()
        if bloque:
            titulo = bloque.split("\n", 1)[0].strip()[:100]
            secciones.append((titulo, bloque))
    return secciones


def fase_1_ingesta_y_chunking(file_path: str):
    """
    Carga, limpieza de ruido y chunking del aviso de privacidad en PDF.
    """
    print(f"Cargando documento PDF: {file_path}")
    loader = PyPDFLoader(file_path)
    documentos = loader.load()

    # Guardia anti-2-columnas (ver _guardia_columnas). Se abre el PDF una
    # segunda vez con pypdf porque PyPDFLoader no expone las páginas nativas.
    paginas = PdfReader(file_path).pages
    for i, doc in enumerate(documentos):
        if i >= len(paginas):
            continue
        texto_corregido, corregido = _guardia_columnas(paginas[i], doc.page_content)
        if corregido:
            print(f"  ⚠️ Página {i + 1}: orden de lectura a 2 columnas corregido "
                  f"(el texto de pypdf no coincidía con el orden por posición).")
            doc.page_content = texto_corregido

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

    # Texto completo (para preguntas donde la recuperación semántica se queda corta)
    texto_completo = "\n".join(doc.page_content for doc in documentos)

    secciones = _segmenta_por_secciones(texto_completo)
    if secciones:
        print(f"Estructura numerada detectada: {len(secciones)} secciones. "
              f"Segmentando por sección (cada una se sub-divide solo si excede el tamaño de chunk).")
        chunks = []
        for titulo, bloque in secciones:
            for sub in text_splitter.create_documents([bloque], metadatas=[{"seccion": titulo}]):
                # Antepone el título de la sección a cada sub-fragmento: ayuda al
                # embedding a capturar el tema aunque el recorte concreto no
                # mencione términos clave de forma explícita (p. ej. un pedazo de
                # la sección de ARCO que solo habla de plazos), y le da contexto
                # directo al LLM en el momento del dictamen.
                if not sub.page_content.startswith(titulo):
                    sub.page_content = f"[{titulo}]\n{sub.page_content}"
                chunks.append(sub)
    else:
        print("Sin estructura numerada detectable; segmentando por tamaño de texto.")
        chunks = text_splitter.split_documents(documentos)

    print(f"Documento dividido en {len(chunks)} fragmentos.")

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
# FASE 1B: SANITY CHECK -- ¿ES REALMENTE UN AVISO DE PRIVACIDAD?
# ==========================================
# El usuario puede subir cualquier PDF por error (un contrato, un estado de
# cuenta, un CV...). Auditarlo igual gastaría las 9 llamadas de la Fase 4-5
# y entregaría un dictamen sin sentido. Esta fase corta eso antes de tiempo.
_PROMPT_VALIDACION = ChatPromptTemplate.from_messages([
    ("system", """Eres un clasificador de documentos legales mexicanos. Tu única tarea es \
determinar si el texto que se te da corresponde a un AVISO DE PRIVACIDAD conforme a la \
Ley Federal de Protección de Datos Personales en Posesión de los Particulares (LFPDPPP) \
-- sin importar si está completo, mal redactado o desactualizado; solo si el documento \
ES de ese tipo.

MUESTRA DEL DOCUMENTO (puede ser un extracto, no necesariamente el documento completo):
{muestra}

Responde ESTRICTAMENTE en JSON:
{{
    "es_aviso_privacidad": true o false,
    "tipo_documento_detectado": "descripción breve de qué tipo de documento parece ser (p. ej. 'contrato de arrendamiento', 'estado de cuenta bancario', 'aviso de privacidad', 'currículum')",
    "confianza": "Alta | Media | Baja",
    "motivo": "una o dos frases explicando el veredicto"
}}"""),
])

# Términos que prácticamente cualquier aviso de privacidad real menciona.
# Si el texto no trae NINGUNO, ni vale la pena gastar la llamada al LLM.
_TERMINOS_AVISO_PRIVACIDAD = [
    r"aviso de privacidad", r"datos personales", r"protecci[oó]n de datos",
    r"LFPDPPP", r"responsable del tratamiento", r"\bARCO\b", r"consentimiento",
]


def construir_chain_validacion(modelo="gpt-4o-mini"):
    # Clasificar el tipo de documento no requiere el rigor (ni el costo) de
    # gpt-4o -- es una tarea mucho más simple que auditar cumplimiento legal.
    llm = ChatOpenAI(
        model=modelo,
        temperature=0,
        model_kwargs={"response_format": {"type": "json_object"}},
    )
    return _PROMPT_VALIDACION | llm


def _parece_aviso_privacidad_heuristica(texto):
    """Filtro gratuito (sin LLM) para los casos obvios. Devuelve (parece_valido, motivo)."""
    if len(texto.strip()) < 200:
        return False, "El PDF no tiene texto extraíble (¿es una imagen escaneada sin OCR?)."
    if not any(re.search(t, texto, flags=re.IGNORECASE) for t in _TERMINOS_AVISO_PRIVACIDAD):
        return False, "El texto no contiene ninguno de los términos esperados en un aviso de privacidad."
    return True, ""


def verifica_es_aviso_privacidad(chain_validacion, texto_completo, doc_completo):
    """
    Sanity check antes de auditar. Dos casos, como pide el nombre de la fase:
      - Si el documento CABE en la ventana de contexto (doc_completo=True):
        se manda completo -- ya se iba a mandar así de todos modos en la
        auditoría, así que no cuesta nada extra revisarlo entero.
      - Si EXCEDE la ventana: no tiene sentido (ni es necesario) mandarlo
        entero solo para esta verificación. La identidad de un documento es
        evidente por su encabezado y su cierre, así que se usa una muestra
        acotada del principio + el final en vez del texto completo.
    """
    parece_valido, motivo = _parece_aviso_privacidad_heuristica(texto_completo)
    if not parece_valido:
        return {
            "es_aviso_privacidad": False,
            "tipo_documento_detectado": "indeterminado (sin evidencia textual)",
            "confianza": "Alta",
            "motivo": motivo,
        }

    if doc_completo:
        muestra = texto_completo
    else:
        muestra = (
            texto_completo[:6000]
            + "\n\n[... documento continúa ...]\n\n"
            + texto_completo[-2000:]
        )

    respuesta = chain_validacion.invoke({"muestra": muestra})
    try:
        dictamen = json.loads(respuesta.content)
    except json.JSONDecodeError:
        # Ante la duda (el clasificador no devolvió JSON válido) se deja
        # pasar a la auditoría en vez de bloquear por un fallo del propio
        # sanity check -- si de verdad no es un aviso, las 9 reglas
        # deberían salir "No cumple" de todas formas.
        return {
            "es_aviso_privacidad": True,
            "tipo_documento_detectado": "no determinado",
            "confianza": "Baja",
            "motivo": "El clasificador no devolvió JSON válido; se deja pasar por precaución.",
        }
    dictamen.setdefault("es_aviso_privacidad", True)
    dictamen.setdefault("tipo_documento_detectado", "no determinado")
    dictamen.setdefault("confianza", "Media")
    dictamen.setdefault("motivo", "")
    return dictamen


def guarda_reporte_rechazo(validacion, ruta_pdf):
    """Deja constancia del rechazo con el mismo par de archivos que un reporte normal."""
    salida = {
        "documento_auditado": os.path.basename(ruta_pdf),
        "fecha_auditoria": datetime.now().isoformat(timespec="seconds"),
        "resultado": "RECHAZADO",
        "validacion": validacion,
    }
    with open("resultado_auditoria.json", "w", encoding="utf-8") as f:
        json.dump(salida, f, indent=2, ensure_ascii=False)

    with open("reporte_auditoria.md", "w", encoding="utf-8") as f:
        f.write(
            "# Auditoría de Aviso de Privacidad — LFPDPPP\n\n"
            f"- **Documento:** {salida['documento_auditado']}\n"
            f"- **Fecha:** {salida['fecha_auditoria']}\n"
            "- **Resultado:** ⛔ RECHAZADO — no parece un aviso de privacidad\n\n"
            f"- **Tipo de documento detectado:** {validacion['tipo_documento_detectado']}\n"
            f"- **Confianza del clasificador:** {validacion['confianza']}\n"
            f"- **Motivo:** {validacion['motivo']}\n"
        )
    print("\nReportes de rechazo guardados: resultado_auditoria.json, reporte_auditoria.md")


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

OBSERVACIÓN DEL REVISOR HUMANO (si la hay, tenla muy en cuenta; puede señalar
evidencia que pasaste por alto o un criterio a reconsiderar):
{observacion}

Instrucciones:
- Basa tu dictamen ÚNICAMENTE en el CONTEXTO proporcionado.
- "Cumple total" = el aviso satisface todos los elementos exigidos por la regla.
- "Cumple parcial" = aborda el tema pero omite algún elemento (p. ej. menciona ARCO pero no el medio para ejercerlos).
- "No cumple" = el contexto no contiene información que satisfaga la regla.
- Si no hay evidencia, responde "No cumple" y evidencia "Ninguna".

DETECCIÓN DE DESACTUALIZACIÓN NORMATIVA (autoridad de control):
- Si el CONTEXTO menciona al INAI (Instituto Nacional de Transparencia, Acceso a la
  Información y Protección de Datos Personales) o al IFAI como la autoridad encargada de
  vigilar, proteger, garantizar o sancionar el tratamiento indebido de los datos
  personales, el aviso está DESACTUALIZADO: tras la reforma de 2025 esa función
  corresponde a la Secretaría Anticorrupción y Buen Gobierno.
- En ese caso llena "nota_desactualizacion" con la cita textual de la mención y la
  aclaración de cuál es la autoridad vigente. Si no aplica, pon exactamente "Ninguna".

Responde ESTRICTAMENTE en JSON con esta estructura:
{{
    "cumple": "Cumple total | Cumple parcial | No cumple",
    "evidencia_encontrada": "Cita textual breve del aviso o 'Ninguna'",
    "elementos_faltantes": "Qué elementos concretos faltan, o 'Ninguno'",
    "justificacion": "Razonamiento legal breve",
    "nota_desactualizacion": "Cita de la mención al INAI/IFAI + autoridad vigente, o 'Ninguna'"
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


def construir_chain(modelo="gpt-4o"):
    llm = ChatOpenAI(
        model=modelo,
        temperature=0,
        model_kwargs={"response_format": {"type": "json_object"}},
    )
    return _PROMPT | llm


def preparar_contexto(regla, vector_store, texto_completo, doc_completo, k=8):
    if doc_completo:
        return texto_completo
    # MMR (Maximal Marginal Relevance) en vez de similitud pura: evita traer
    # k fragmentos casi idénticos de la misma sección y deja hueco para que
    # entren fragmentos relevantes de otras partes del aviso -- importante en
    # documentos grandes, donde una regla puede tener evidencia repartida
    # (p. ej. "transferencias" mencionadas en la sección de finalidades Y en
    # la de transferencias propiamente dicha).
    docs = vector_store.max_marginal_relevance_search(
        regla["pregunta"], k=k, fetch_k=max(k * 4, 20), lambda_mult=0.5,
    )
    vistos, fragmentos = set(), []
    for d in docs:
        if d.page_content not in vistos:
            vistos.add(d.page_content)
            fragmentos.append(d.page_content)
    return "\n---\n".join(fragmentos)


def evalua_regla(chain, regla, contexto, observacion=""):
    """Ejecuta el LLM para una regla y devuelve el dictamen normalizado."""
    respuesta = chain.invoke({
        "query": regla["pregunta"],
        "referencia": regla["referencia"],
        "context": contexto,
        "observacion": observacion or "Ninguna",
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
    nota = (dictamen.get("nota_desactualizacion") or "").strip()
    dictamen["nota_desactualizacion"] = "Ninguna" if _es_vacio(nota) else nota
    return dictamen


def _es_vacio(valor: str) -> bool:
    return (valor or "").strip().lower() in ("", "ninguna", "ninguno", "n/a", "na", "none", "no aplica")


def fase_4_y_5_auditoria(chain, vector_store, texto_completo, reglas, k=None):
    resultados = []
    doc_completo = len(texto_completo) <= MAX_CHARS_DOC_COMPLETO
    if doc_completo:
        print(f"Aviso pequeño ({len(texto_completo)} car.): se evalúa contra el texto completo.")
    else:
        if k is None:
            # Más texto -> más fragmentos por regla, con techo para no disparar
            # el costo/latencia en avisos extremadamente largos.
            k = min(24, max(8, len(texto_completo) // 6000))
        print(f"Aviso grande ({len(texto_completo)} car.): se usa recuperación semántica MMR (k={k}).")

    print("Iniciando auditoría iterativa de principios legales...")
    for regla in reglas:
        contexto = preparar_contexto(regla, vector_store, texto_completo, doc_completo, k)
        dictamen = evalua_regla(chain, regla, contexto)
        resultados.append({
            "id": regla["id"],
            "regla_evaluada": regla["pregunta"],
            "referencia_legal": regla["referencia"],
            "contexto_usado": contexto,
            "dictamen": dictamen,
            "revision": None,
        })
        print(f"  [{dictamen['cumple']:>15}]  {regla['id']}")

    return resultados, doc_completo


# ==========================================
# FASE 5B: REVISIÓN HUMANA (HUMAN IN THE LOOP)
# ==========================================
def veredicto_efectivo(resultado):
    """Veredicto final: el ajuste del revisor si existe, si no el de la IA."""
    rev = resultado.get("revision")
    if rev and rev.get("veredicto_final"):
        return rev["veredicto_final"]
    return resultado["dictamen"]["cumple"]


def _muestra_dictamen(idx, total, r):
    d = r["dictamen"]
    print("\n" + "=" * 78)
    print(f"[{idx}/{total}]  {r['id']}   ·   {r['referencia_legal']}")
    print("-" * 78)
    print(r["regla_evaluada"])
    print(f"\n  Veredicto IA........ {d['cumple']}")
    print(f"  Evidencia.......... {d.get('evidencia_encontrada', 'Ninguna')}")
    print(f"  Elementos faltantes. {d.get('elementos_faltantes', 'Ninguno')}")
    print(f"  Justificación...... {d.get('justificacion', '')}")


def _pide_veredicto():
    opciones = {"1": "Cumple total", "2": "Cumple parcial", "3": "No cumple"}
    while True:
        sel = input("    Nuevo veredicto [1=total, 2=parcial, 3=no cumple]: ").strip()
        if sel in opciones:
            return opciones[sel]
        print("    Opción inválida.")


def _acta_revision(r, veredicto_final, nota, revisor):
    """Construye el registro de auditoría de la decisión del revisor."""
    veredicto_ia = (r.get("reevaluaciones") or [{}])[0].get(
        "veredicto_previo", r["dictamen"]["cumple"]
    ) if r.get("reevaluaciones") else r["dictamen"]["cumple"]
    return {
        "revisado": True,
        "veredicto_ia": veredicto_ia,
        "veredicto_dictamen_actual": r["dictamen"]["cumple"],
        "veredicto_final": veredicto_final,
        "ajustado": veredicto_final != r["dictamen"]["cumple"],
        "reevaluado": bool(r.get("reevaluaciones")),
        "nota_revisor": nota,
        "revisor": revisor,
        "fecha": datetime.now().isoformat(timespec="seconds"),
    }


def fase_5b_revision_humana(resultados, chain, vector_store, texto_completo,
                            doc_completo, reglas_ejecutadas, revisor):
    """
    Revisión interactiva: el humano confirma, corrige o pide re-evaluar cada
    dictamen, y puede añadir reglas que la IA no consideró.
    """
    if not sys.stdin.isatty():
        print("\n[revisión] Entrada no interactiva; se omite la revisión humana.")
        return resultados

    print("\n" + "#" * 78)
    print("#  REVISIÓN HUMANA (human in the loop)")
    print("#  Por cada regla:  [Enter]=aceptar  o=cambiar veredicto  "
          "n=nota  r=re-evaluar con observación  q=terminar revisión")
    print("#" * 78)

    for nota in recopila_notas_desactualizacion(resultados):
        print(f"\n  ⚠️ DESACTUALIZACIÓN NORMATIVA: {nota}")

    ids_existentes = {r["id"] for r in resultados}
    total = len(resultados)

    for i, r in enumerate(resultados, 1):
        _muestra_dictamen(i, total, r)
        regla = next((x for x in reglas_ejecutadas if x["id"] == r["id"]), None)

        while True:
            accion = input("\n  Acción > ").strip().lower()

            if accion in ("", "a", "ok"):
                r["revision"] = _acta_revision(r, r["dictamen"]["cumple"], "", revisor)
                break

            if accion == "o":
                nuevo = _pide_veredicto()
                nota = input("    Nota del revisor (por qué se ajusta): ").strip()
                r["revision"] = _acta_revision(r, nuevo, nota, revisor)
                print(f"    -> Veredicto final: {nuevo}")
                break

            if accion == "n":
                nota = input("    Nota del revisor: ").strip()
                r["revision"] = _acta_revision(r, r["dictamen"]["cumple"], nota, revisor)
                break

            if accion == "r":
                if regla is None:
                    print("    No se puede re-evaluar esta regla.")
                    continue
                obs = input("    Observación para la IA (evidencia omitida, criterio...): ").strip()
                nuevo_dictamen = evalua_regla(chain, regla, r["contexto_usado"], obs)
                print(f"\n    Nuevo veredicto IA: {nuevo_dictamen['cumple']}")
                print(f"    Evidencia.......... {nuevo_dictamen.get('evidencia_encontrada', 'Ninguna')}")
                print(f"    Elementos faltantes {nuevo_dictamen.get('elementos_faltantes', 'Ninguno')}")
                print(f"    Justificación...... {nuevo_dictamen.get('justificacion', '')}")
                r.setdefault("reevaluaciones", []).append({
                    "observacion": obs,
                    "veredicto_previo": r["dictamen"]["cumple"],
                    "veredicto": nuevo_dictamen["cumple"],
                })
                r["dictamen"] = nuevo_dictamen
                # Sigue pidiendo acción sobre el nuevo dictamen
                continue

            if accion == "q":
                print("    Revisión terminada anticipadamente.")
                return resultados

            print("    Acción no reconocida.")

    # --- Añadir reglas no consideradas ---
    while input("\n¿Añadir una regla que la IA no consideró? [s/N]: ").strip().lower() == "s":
        rid = input("  id (slug): ").strip() or f"regla_extra_{len(resultados) + 1}"
        if rid in ids_existentes:
            print("  Ese id ya existe.")
            continue
        pregunta = input("  Pregunta / regla: ").strip()
        referencia = input("  Referencia legal: ").strip()
        if not pregunta:
            print("  Pregunta vacía, se descarta.")
            continue
        nueva = {"id": rid, "pregunta": pregunta, "referencia": referencia or "N/D"}
        contexto = preparar_contexto(nueva, vector_store, texto_completo, doc_completo)
        dictamen = evalua_regla(chain, nueva, contexto)
        nuevo_r = {
            "id": rid, "regla_evaluada": pregunta, "referencia_legal": nueva["referencia"],
            "contexto_usado": contexto, "dictamen": dictamen,
            "origen": "añadida por revisor",
            "revision": None,
        }
        resultados.append(nuevo_r)
        ids_existentes.add(rid)
        reglas_ejecutadas.append(nueva)
        _muestra_dictamen(len(resultados), len(resultados), nuevo_r)
        accion = input("\n  [Enter]=aceptar  o=cambiar veredicto > ").strip().lower()
        if accion == "o":
            nuevo = _pide_veredicto()
            nota = input("    Nota: ").strip()
            nuevo_r["revision"] = _acta_revision(nuevo_r, nuevo, nota, revisor)
        else:
            nuevo_r["revision"] = _acta_revision(nuevo_r, dictamen["cumple"], "", revisor)

    return resultados


# ==========================================
# FASE 6: RESUMEN Y REPORTE
# ==========================================
def _firma_nota(nota: str) -> str:
    """Agrupa notas que se refieren al mismo problema (todas las del INAI/IFAI son una)."""
    low = nota.lower()
    if "inai" in low or "ifai" in low or "instituto nacional de transparencia" in low:
        return "inai_ifai"
    return re.sub(r"[^a-z0-9]", "", low)[:80]


def recopila_notas_desactualizacion(resultados):
    """Notas de desactualización normativa detectadas, deduplicadas por problema."""
    por_firma = {}
    for r in resultados:
        n = (r["dictamen"].get("nota_desactualizacion") or "").strip()
        if _es_vacio(n):
            continue
        firma = _firma_nota(n)
        # se queda con la redacción más completa de cada problema
        if firma not in por_firma or len(n) > len(por_firma[firma]):
            por_firma[firma] = n
    return list(por_firma.values())


def genera_resumen(resultados):
    conteo = {v: 0 for v in VEREDICTOS}
    for r in resultados:
        conteo[veredicto_efectivo(r)] += 1

    total = len(resultados)
    puntos = conteo["Cumple total"] + 0.5 * conteo["Cumple parcial"]
    porcentaje = round(100 * puntos / total, 1) if total else 0.0

    if conteo["No cumple"] == 0 and conteo["Cumple parcial"] == 0:
        veredicto = "CUMPLE"
    elif conteo["Cumple total"] == 0:
        veredicto = "NO CUMPLE"
    else:
        veredicto = "CUMPLE PARCIALMENTE"

    revisadas = sum(1 for r in resultados if (r.get("revision") or {}).get("revisado"))
    ajustadas = sum(1 for r in resultados if (r.get("revision") or {}).get("ajustado"))

    notas_desact = recopila_notas_desactualizacion(resultados)

    return {
        "veredicto_global": veredicto,
        "porcentaje_cumplimiento": porcentaje,
        "conteo": conteo,
        "reglas_evaluadas": total,
        "reglas_revisadas_por_humano": revisadas,
        "reglas_ajustadas_por_humano": ajustadas,
        "aviso_desactualizado": bool(notas_desact),
        "notas_desactualizacion": notas_desact,
    }


def guarda_reportes(resultados, resumen, ruta_pdf):
    detalle_serializable = [
        {k: v for k, v in r.items() if k != "contexto_usado"} for r in resultados
    ]
    salida = {
        "documento_auditado": os.path.basename(ruta_pdf),
        "fecha_auditoria": datetime.now().isoformat(timespec="seconds"),
        "resumen": resumen,
        "detalle": detalle_serializable,
    }
    with open("resultado_auditoria.json", "w", encoding="utf-8") as f:
        json.dump(salida, f, indent=2, ensure_ascii=False)

    c = resumen["conteo"]
    lineas = [
        "# Auditoría de Aviso de Privacidad — LFPDPPP",
        "",
        f"- **Documento:** {salida['documento_auditado']}",
        f"- **Fecha:** {salida['fecha_auditoria']}",
        f"- **Veredicto global:** {resumen['veredicto_global']}",
        f"- **Cumplimiento:** {resumen['porcentaje_cumplimiento']}%  "
        f"(total: {c['Cumple total']}, parcial: {c['Cumple parcial']}, no cumple: {c['No cumple']})",
        f"- **Revisión humana:** {resumen['reglas_revisadas_por_humano']}/{resumen['reglas_evaluadas']} "
        f"reglas revisadas, {resumen['reglas_ajustadas_por_humano']} ajustadas",
        "",
    ]
    if resumen.get("aviso_desactualizado"):
        lineas += [
            "> ⚠️ **Nota de desactualización normativa**",
            ">",
            "> El aviso hace referencia al **INAI/IFAI** como autoridad de control. "
            "Tras la reforma de 2025, la vigilancia, protección y sanción en materia de "
            "datos personales corresponde a la **Secretaría Anticorrupción y Buen Gobierno**.",
            ">",
        ]
        for n in resumen["notas_desactualizacion"]:
            lineas.append(f"> - {n}")
        lineas.append("")
    lineas += ["---", ""]
    for r in resultados:
        d = r["dictamen"]
        rev = r.get("revision") or {}
        final = veredicto_efectivo(r)
        marca = ""
        if rev.get("ajustado"):
            marca = f"  ·  ⚠️ ajustado por revisor (IA decía: {rev.get('veredicto_ia')})"
        elif rev.get("revisado"):
            marca = "  ·  ✔️ revisado"
        if r.get("origen"):
            marca += f"  ·  ({r['origen']})"

        lineas += [
            f"## {r['regla_evaluada']}",
            f"*{r['referencia_legal']}*",
            "",
            f"- **Resultado:** {final}{marca}",
            f"- **Evidencia:** {d.get('evidencia_encontrada', 'Ninguna')}",
            f"- **Elementos faltantes:** {d.get('elementos_faltantes', 'Ninguno')}",
            f"- **Justificación:** {d.get('justificacion', '')}",
        ]
        if rev.get("nota_revisor"):
            lineas.append(f"- **Nota del revisor ({rev.get('revisor', 'N/D')}):** {rev['nota_revisor']}")
        lineas.append("")

    with open("reporte_auditoria.md", "w", encoding="utf-8") as f:
        f.write("\n".join(lineas))

    print("\nReportes guardados: resultado_auditoria.json, reporte_auditoria.md")


# ==========================================
# MAIN
# ==========================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Auditor RAG de avisos de privacidad (LFPDPPP).")
    parser.add_argument("pdf", nargs="?", default="aviso_privacidad.pdf", help="Ruta al PDF del aviso.")
    parser.add_argument("--revisar", action="store_true",
                        help="Activa la revisión humana interactiva tras la auditoría automática.")
    parser.add_argument("--revisor", default=os.getenv("USER", "revisor"),
                        help="Nombre del revisor humano (para el acta).")
    parser.add_argument("--modelo", default="gpt-4o", help="Modelo de OpenAI para el dictamen.")
    parser.add_argument("--forzar", action="store_true",
                        help="Audita aunque el sanity check no reconozca el PDF como aviso de privacidad.")
    args = parser.parse_args()

    try:
        chunks, texto_completo = fase_1_ingesta_y_chunking(args.pdf)
        doc_completo = len(texto_completo) <= MAX_CHARS_DOC_COMPLETO

        chain_validacion = construir_chain_validacion()
        validacion = verifica_es_aviso_privacidad(chain_validacion, texto_completo, doc_completo)
        if not validacion["es_aviso_privacidad"] and not args.forzar:
            print("\n=== DOCUMENTO RECHAZADO ===")
            print(f"Este PDF no parece ser un aviso de privacidad (confianza: {validacion['confianza']}).")
            print(f"Tipo de documento detectado: {validacion['tipo_documento_detectado']}")
            print(f"Motivo: {validacion['motivo']}")
            print("Usa --forzar si quieres auditarlo de todas formas.")
            guarda_reporte_rechazo(validacion, args.pdf)
            sys.exit(1)
        elif not validacion["es_aviso_privacidad"]:
            print(f"\n⚠️ Sanity check no reconoció el documento como aviso de privacidad "
                  f"(tipo detectado: {validacion['tipo_documento_detectado']}), pero se continúa por --forzar.")
        else:
            print(f"Validación: el documento parece ser un aviso de privacidad "
                  f"(confianza: {validacion['confianza']}).")

        db_vectorial = fase_2_vectorizacion(chunks)
        chain = construir_chain(args.modelo)

        reglas_ejecutadas = [dict(r) for r in DICCIONARIO_LFPDPPP]
        resultados, doc_completo = fase_4_y_5_auditoria(
            chain, db_vectorial, texto_completo, reglas_ejecutadas
        )

        if args.revisar:
            resultados = fase_5b_revision_humana(
                resultados, chain, db_vectorial, texto_completo,
                doc_completo, reglas_ejecutadas, args.revisor,
            )

        resumen = genera_resumen(resultados)

        print("\n=== RESUMEN DE LA AUDITORÍA ===")
        print(json.dumps(resumen, indent=2, ensure_ascii=False))

        guarda_reportes(resultados, resumen, args.pdf)

    except Exception as e:
        print(f"Error durante la ejecución del pipeline: {e}")
        raise
