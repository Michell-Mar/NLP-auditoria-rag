# Decisiones y límites

| Decisión | Motivo y compromiso |
|---|---|
| CDK en Python | Infraestructura versionada en el mismo lenguaje del proyecto; crea CloudFormation |
| Lambda en contenedor | Empaqueta dependencias de LangChain sin administrar un servidor; debe caber en 15 minutos |
| S3 privado | Intercambio de PDFs/reportes y retención; no incluye una interfaz de usuario |
| OpenAI externo | Mantiene los modelos ya usados; los textos salen de AWS y hay facturación separada |
| Vector store en memoria | El código indexa cada documento para esa auditoría; no necesita un buscador persistente para este flujo |
| Sin VPC personalizada | El worker necesita salida HTTPS al proveedor; evita añadir un NAT al MVP |
| Invocación privada síncrona | Menos componentes y sin API pública; la terminal debe esperar |
| Subproceso aislado | Conserva el CLI existente y evita colisiones entre nombres de reporte; añade algo de arranque |
| Revisión humana local | `input()` requiere interacción; una revisión web necesita endpoints y almacenamiento de decisiones |

## Costos

No se ha ejecutado una estimación monetaria para una región y volumen concretos. El gasto incluye solicitudes y duración de Lambda, S3, ECR, logs, almacenamiento del secreto y uso de la API de OpenAI. El stack no configura concurrencia aprovisionada, NAT, balanceador o base vectorial persistente.

Para estimar: mide duración por PDF, tokens de entrada/salida por modelo, llamadas de embeddings en documentos largos, cantidad de documentos y tamaño/retención de archivos. Con 2 GiB y 120 segundos, cada auditoría consumiría aproximadamente 240 GB-segundo de Lambda; esto es una hipótesis aritmética, no una medición del proyecto. Multiplica por tarifas vigentes de la región y agrega solicitudes/almacenamiento/proveedor.

Configura alertas de presupuesto en AWS y controles de gasto en el proveedor antes de una demo con usuarios. Las alertas por sí solas no detienen el gasto. Un usuario con permiso de invocar Lambda puede iniciar múltiples ejecuciones: no hay límite agregado de gasto implementado. El límite de 10 MiB tampoco acota por sí solo páginas, tokens o tiempo de procesamiento.

## Observaciones sobre el código recibido

- Hasta 60,000 caracteres el contexto es el documento completo. Se eliminó la vectorización innecesaria sólo en esta ruta del CLI.
- La ausencia de evidencia recuperada actualmente se clasifica como `No cumple`; puede ser un problema de recuperación. Debe evaluarse por separado.
- Un JSON inválido del modelo también puede acabar como `No cumple`. Para uso más serio, incorpora validación de esquema y un estado de error distinto del dictamen.
- La segmentación por secciones puede perder texto previo a la primera sección y metadatos de página. Añade casos de prueba antes de afirmar cobertura completa de evidencias.
- El porcentaje pondera `Cumple parcial` como 0.5; no es una probabilidad calibrada ni una métrica de calidad del modelo.
- El contenido del PDF entra al prompt. Falta evaluar resistencia a instrucciones maliciosas incluidas en el documento.
- No se incluye OCR, autenticación web, revisión humana web ni versionado del corpus normativo.

## Evolución después del primer despliegue

1. Medir calidad, latencia y consumo con un conjunto pequeño de documentos revisados.
2. Refactorizar el auditor en funciones que devuelvan objetos, añadir validación de respuestas y trazabilidad por página/regla/modelo.
3. Si las ejecuciones se acercan al límite, separar etapas o mover el worker a ECS Fargate como tarea; decidir con tiempos medidos.
4. Si necesitas demo web: agregar autenticación, subida a S3 con URL prefirmada, API de trabajos, cola y estado persistente; el navegador consulta el resultado sin mantener una petición larga.
5. Incorporar revisiones humanas persistentes y despliegue con GitHub Actions/OIDC ligado al repo real.

Fuentes técnicas: [Lambda timeout](https://docs.aws.amazon.com/lambda/latest/dg/configuration-timeout.html), [reintentos](https://docs.aws.amazon.com/us_en/lambda/latest/dg/invocation-retries.html), [OIDC con AWS](https://docs.github.com/actions/deployment/security-hardening-your-deployments/configuring-openid-connect-in-amazon-web-services).
