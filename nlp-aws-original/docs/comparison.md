# Comparar las tres modalidades

## Dos variantes del código base

- **Original:** `app/audito_rag_pdf.py` es idéntico, byte por byte, al archivo recibido. Su CLI calcula embeddings incluso si luego utiliza contexto completo.
- **Corregido:** el único cambio en ese archivo evita generar embeddings cuando el CLI utiliza contexto completo. Para documentos largos mantiene la recuperación semántica.

Esta optimización ahorra una etapa, pero no cambia intencionalmente el contexto enviado al modelo. Por sí sola no implementa un experimento contexto completo vs. RAG. Las respuestas de un modelo externo pueden variar entre ejecuciones.

## Comparación explícita

Ambos paquetes incluyen el mismo `scripts/comparar.py`. Reutiliza las funciones del auditor sin modificar el archivo base y ejecuta dos modalidades sobre el mismo PDF:

| Modalidad | Contexto del modelo | Intervención humana |
|---|---|---|
| `contexto_completo` | Todo el texto extraído | Ninguna |
| `embeddings` | Fragmentos recuperados mediante embeddings y MMR, por regla | Ninguna |
| `revision_usuario` | Parte de una de las dos evaluaciones anteriores | Aceptar, ajustar, anotar o solicitar reevaluación |

Los embeddings recuperan fragmentos; el LLM sigue emitiendo el dictamen. La revisión humana es una etapa posterior, no un tercer mecanismo de recuperación.

Desde la raíz del paquete, instala las dependencias según `docs/deployment.md`, configura `.env` localmente y ejecuta:

```bash
python scripts/comparar.py /ruta/aviso.pdf --revisar-base embeddings --revisor Jaydy
```

Esto realiza contexto completo, embeddings y revisión humana de la salida de embeddings. Para revisar la salida de contexto completo, utiliza `--revisar-base contexto_completo`. Si omites `--revisar-base`, sólo ejecuta las dos modalidades automáticas. `--modelo` controla el mismo modelo para ambas; `--k 8` fija el número solicitado de fragmentos recuperados.

El comparador fuerza ambas rutas incluso en PDFs cortos. Para acotar esta primera comparación admite como máximo 60,000 caracteres extraídos; no realiza una comprobación exacta del límite de tokens del proveedor. El CLI original mantiene su selección automática por longitud.

## Resultados y trazabilidad

Cada ejecución crea un directorio nuevo en `experimentos/` con:

- `contexto_completo/`: JSON y Markdown de la primera evaluación.
- `embeddings/`: JSON y Markdown de la segunda evaluación.
- `revision_usuario/`: JSON y Markdown de la revisión, si se solicitó.
- `experimento.json`: hashes de PDF/código, modelo, k, base de revisión, estado y tiempos.

El resultado humano parte de una copia del resultado automático ya obtenido: no se vuelve a generar toda la auditoría. Se conserva el reporte automático anterior. Las reevaluaciones que el revisor solicite sí realizan llamadas adicionales. Si termina la revisión antes de completarla, el reporte conserva el número de reglas revisadas; `COMPLETED` describe la ejecución, no certifica revisión total.

Los tiempos de contexto completo y embeddings excluyen la ingesta y validación compartidas. El tiempo de embeddings incluye vectorización y evaluación; el de revisión humana incluye la interacción y posibles reevaluaciones. No se miden tokens ni costos monetarios. El comparador tiene la misma conducta en los dos paquetes: úsalo una vez para comparar modalidades; usa los CLIs base por separado si quieres medir el efecto de la optimización.

Esta comparación puede realizar una llamada de validación y 18 evaluaciones (nueve por método), además de embeddings y las reevaluaciones humanas solicitadas. No se ha ejecutado contra OpenAI durante la preparación de estos archivos.

## Diseño de la evaluación

Mantén PDF, reglas, modelo y k constantes. Una revisión del usuario final puede estar influida por el resultado automático: no la trates automáticamente como verdad de referencia. Para medir calidad, usa etiquetas independientes y revisadas. Si el revisor añade reglas, compara las reglas comunes y reporta las nuevas por separado.

## AWS

El worker de Lambda sigue usando el CLI de su variante, con selección automática por longitud. El comparador y la revisión humana son locales. No se han añadido modos explícitos a Lambda ni una interfaz web de revisión. Los nombres del stack CDK son iguales: desplegar ambas variantes en la misma cuenta y región actualizaría el mismo stack. Para mantener ambos despliegues simultáneamente, cambia el identificador de stack antes de desplegar.
