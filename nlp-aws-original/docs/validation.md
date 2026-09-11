# Validación de la base entregada

Validación local realizada el 11 de septiembre de 2026 con Python 3.12:

- `python -m pytest -q`: **8 pruebas aprobadas**. Incluyen rechazo de identificadores inválidos, éxito/rechazo/fallo simulado del pipeline y aserciones sobre la plantilla CDK sintetizada.
- `python -m pip check`: sin incompatibilidades declaradas de dependencias.
- `python app/audito_rag_pdf.py --help`: importación y CLI correctos.

No se construyó la imagen Docker localmente porque Docker no está disponible en este entorno. La CI incluye ese paso, pero aún no se ha ejecutado en tu repositorio.

No se crearon recursos AWS, no se enviaron PDFs a OpenAI y no se publicaron cambios en GitHub. Pendientes: build de contenedor, despliegue real, prueba con un PDF y medición de tiempos/costos/calidad. Las pruebas simuladas no sustituyen esas comprobaciones.

Las reglas y prompts del archivo original se conservan. En esta variante el archivo del auditor se conserva idéntico al original; no se aplica la optimización de embeddings. El adaptador de Lambda y los demás archivos son nuevos.

## Variantes y comparador

Se comprobó la identidad exacta de la variante original y la diferencia acotada de la corregida. El comparador adicional se comprobó mediante compilación y `--help`; no se ejecutaron llamadas reales ni una sesión humana de comparación.
