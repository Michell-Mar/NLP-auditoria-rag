# NLP - Auditoría de Avisos de Privacidad (LFPDPPP)

Este repositorio contiene el desarrollo de un auditor basado en RAG (LangChain + OpenAI) que revisa avisos de privacidad en PDF contra los requisitos de la Ley Federal de Protección de Datos Personales en Posesión de los Particulares (LFPDPPP).

## 📌 Dónde está el proyecto

**Toda la información necesaria para generar, desplegar y entender el proyecto vive en [`nlp-aws-corregido/`](nlp-aws-corregido/).**

Esa carpeta es autocontenida e incluye su propio [`README.md`](nlp-aws-corregido/README.md) con:

- El objetivo del proyecto y su arquitectura (CLI, interfaz web local y versión serverless en AWS).
- Los pasos para recrear el proyecto con tus propios recursos de AWS (bootstrap, despliegue del stack, validación end-to-end, despliegue opcional de la interfaz web).
- La lista de componentes que habría que cambiar para llevar el proyecto a un escenario de producción a gran escala.

Si vienes a este repositorio a construir, desplegar o probar el proyecto, empieza directamente en esa carpeta.

## Contenido del resto del repositorio

El resto de los archivos en la raíz corresponden a exploración y prototipado previos a la versión productivizada:

- `audito_rag_pdf.py`, `auditor_rag.py` — versiones tempranas del script de auditoría, previas a su empaquetado en `nlp-aws-corregido/`.
- `ejemplo_avisos_privacidad/`, `BanCoppel aviso privacidad.pdf`, `aviso_privacidad.txt` — PDFs y textos de ejemplo usados durante las pruebas.
- `chunks_generados.txt`, `resultado_auditoria.json`, `reporte_auditoria.md` — salidas generadas localmente al correr el script de exploración.
- `deploy_aws_runbook.html` — copia guardada de notas de despliegue de una etapa anterior del proyecto.
- `rag_nlp_aviso/` — entorno virtual local (no forma parte del código fuente).

Estos archivos se conservan como referencia histórica del proceso, pero no son necesarios para reproducir el proyecto: para eso, usa `nlp-aws-corregido/`.
