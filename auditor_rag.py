import os
import json
from langchain_community.document_loaders import TextLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_openai import OpenAIEmbeddings, ChatOpenAI
# from langchain_community.vectorstores import FAISS # <-- Asegúrate de que sea FAISS

from langchain_core.vectorstores import InMemoryVectorStore

from langchain_core.prompts import ChatPromptTemplate
from dotenv import load_dotenv



# Configuración de credenciales (Asegúrate de tener esto en tus variables de entorno)
# os.environ["OPENAI_API_KEY"] = "tu-api-key-aqui"
load_dotenv()



def fase_1_ingesta_y_chunking(file_path: str):
    """
    Fase 1: Carga el aviso de privacidad y lo divide en fragmentos semánticos.
    """
    print(f"Cargando documento: {file_path}")
    loader = TextLoader(file_path, encoding='utf-8')
    document = loader.load()
    
    # Hiperparámetros de chunking
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=500,
        chunk_overlap=50,
        separators=["\n\n", "\n", ".", " "]
    )
    
    chunks = text_splitter.split_documents(document)
    print(f"Documento dividido en {len(chunks)} fragmentos.")
    return chunks

def fase_2_vectorizacion(chunks):
    """
    Fase 2 (Modificada): Convierte fragmentos en embeddings usando la memoria nativa de Python.
    """
    print("Vectorizando e indexando en InMemoryVectorStore (Puro Python)...")
    embeddings_model = OpenAIEmbeddings(model="text-embedding-3-small")
    
    # Crea la base de datos directamente en la RAM sin dependencias externas
    vector_store = InMemoryVectorStore.from_documents(chunks, embeddings_model)
    return vector_store

def fase_4_y_5_auditoria(vector_store, queries):
    """
    Fases 4 y 5: Recupera contexto por cada pregunta y genera el veredicto con el LLM.
    """
    # Usamos gpt-4o-mini por su excelente balance entre costo y capacidad de seguir JSON
    llm = ChatOpenAI(
        model="gpt-4o-mini", 
        temperature=0, 
        model_kwargs={"response_format": {"type": "json_object"}}
    )
    
    # Diseño del Prompt del Juez (Fase 5)
    prompt_template = ChatPromptTemplate.from_messages([
        ("system", """Eres un auditor legal experto en la Ley Federal de Protección de Datos Personales en Posesión de los Particulares (LFPDPPP) de México.
        Tu tarea es evaluar si un aviso de privacidad cumple con un principio normativo específico.
        
        REGLA A EVALUAR: {query}
        
        Evidencia extraída del aviso de privacidad (CONTEXTO):
        {context}
        
        Basado ÚNICAMENTE en el contexto proporcionado, emite tu dictamen.
        Debes responder estrictamente en formato JSON con la siguiente estructura:
        {{
            "cumple": "Sí / No / Parcial",
            "evidencia_encontrada": "Cita textual breve o 'Ninguna'",
            "justificacion": "Por qué cumple o qué le falta exactamente"
        }}"""),
    ])
    
    chain = prompt_template | llm
    resultados_auditoria = []
    
    print("Iniciando auditoría iterativa de principios legales...")
    for query in queries:
        # Fase 4: Recuperación Semántica (Top-K = 3)
        docs_recuperados = vector_store.similarity_search(query, k=3)
        
        # Unir los textos de los fragmentos recuperados para pasarlos al LLM
        contexto_texto = "\n---\n".join([doc.page_content for doc in docs_recuperados])
        
        # Fase 5: Generación del LLM
        respuesta = chain.invoke({
            "query": query,
            "context": contexto_texto
        })
        
        # Parsear el string JSON a un diccionario de Python
        dictamen_json = json.loads(respuesta.content)
        
        # Guardar el resultado agregando qué regla se evaluó
        resultados_auditoria.append({
            "regla_evaluada": query,
            "dictamen": dictamen_json
        })
        
    return resultados_auditoria

# ==========================================
# EJECUCIÓN PRINCIPAL (MAIN)
# ==========================================
if __name__ == "__main__":
    
    # Archivo de prueba (Asegúrate de crear un archivo txt con un aviso de prueba)
    ruta_aviso = "aviso_privacidad.txt"
    
    # Fase 3: Traducción de la LFPDPPP a consultas (Queries)
    diccionario_lfpdppp = [
        "¿Se mencionan explícitamente las finalidades principales y secundarias del tratamiento de los datos?",
        "¿Cuáles son los medios y procedimientos específicos para que el titular ejerza sus derechos ARCO?",
        "¿Se indica expresamente el procedimiento y medio por el cual se comunicarán los cambios al aviso de privacidad?",
        "¿Se mencionan transferencias de datos a terceros y se especifica la finalidad de dichas transferencias?",
        "¿Se detallan mecanismos para que el titular pueda revocar su consentimiento para el tratamiento de sus datos?"
    ]
    
    # Ejecutar el pipeline
    try:
        mis_chunks = fase_1_ingesta_y_chunking(ruta_aviso)
        db_vectorial = fase_2_vectorizacion(mis_chunks)
        resultados = fase_4_y_5_auditoria(db_vectorial, diccionario_lfpdppp)
        
        # Imprimir resultados finales en consola
        print("\n=== RESULTADOS DE LA AUDITORÍA ===")
        print(json.dumps(resultados, indent=4, ensure_ascii=False))
        
    except Exception as e:
        print(f"Error durante la ejecución del pipeline: {e}")