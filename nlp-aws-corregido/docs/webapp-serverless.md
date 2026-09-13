# Interfaz web serverless — API Gateway + 2 Lambdas + SQS + DynamoDB

Variante de `webapp-pdf/` para cuando el piloto ya no cabe en "corre en tu laptop" o en
una sola instancia EC2: no depende de que un proceso viva continuamente, así que corre
igual de bien con un tester subiendo un PDF que con diez subiendo a la vez (hasta el
límite de concurrencia, que aquí es de infraestructura real, no un `threading.Semaphore`
en memoria).

Sigue **sin tocar** `app/` ni el Lambda de auditoría ya desplegado (`NlpAuditStack`). Esta
variante solo reemplaza *dónde vive la orquestación* que antes hacía `webapp/server.py` a
mano: subir a S3, invocar el Lambda de auditoría, esperar, y guardar el resultado.

## Por qué dos Lambdas y no uno

`BackgroundTasks` de FastAPI (lo que usa `webapp-pdf/server.py` para no bloquear al
navegador mientras corren las 9 llamadas a gpt-4o) no funciona en Lambda: el contenedor se
congela en cuanto termina de mandar la respuesta HTTP, así que cualquier trabajo agendado
"para después" simplemente no corre. La solución es la misma que ya existía como opción en
este proyecto desde el primer runbook: una Lambda **API** que solo atiende HTTP y encola,
y una Lambda **worker** que consume la cola y hace el trabajo de verdad.

```
Navegador
  │  POST /api/audits {filename}
  ▼
Lambda API (serverless/app.py, vía API Gateway HTTP API)
  │  crea el job en DynamoDB, firma una URL de subida
  ▼
Navegador → PUT directo a S3 (mismo bucket InputBucket de AuditStack)
  │  POST /api/audits/{id}/procesar
  ▼
Lambda API → manda el job_id a SQS
  ▼
Lambda QueueWorker (serverless/worker.py, disparada por SQS)
  │  invoke() síncrono al Lambda "Worker" de NlpAuditStack -- SIN CAMBIOS
  ▼
Lambda "Worker" de NlpAuditStack (app/audito_rag_pdf.py, sin tocar)
  │  audita, escribe resultado en el bucket de resultados
  ▼
Lambda QueueWorker lee ese resultado y lo guarda en DynamoDB
  ▼
Navegador sondea GET /api/audits/{id} hasta ver un estado final
```

El PDF nunca pasa por una Lambda como payload: si subieras un PDF de 10 MiB codificado en
la petición, superarías el límite de 6 MB de una invocación síncrona de Lambda antes de
llegar al límite propio de la app. Por eso la subida es una URL prefirmada de S3 -- el
navegador habla directo con S3, igual que hace `scripts/audit.py` con `boto3` desde tu
terminal.

## Qué cambia frente a `webapp-pdf/`

| | `webapp-pdf/` (EC2/laptop) | `serverless/` |
|---|---|---|
| Estado del job | `dict` en memoria de un proceso que vive siempre | Tabla DynamoDB (`Jobs`) |
| "Correr en segundo plano" | `threading` + `BackgroundTasks` | Cola SQS + Lambda separada |
| Límite de concurrencia | `threading.Semaphore(3)` | `max_concurrency=3` en el *event source* de SQS -- aplica de verdad aunque AWS levante varias instancias del worker a la vez |
| Subida del PDF | multipart directo al proceso | URL de S3 prefirmada, el navegador sube directo |
| Cómputo | Tú administras una instancia | Nada que parchear ni reiniciar |
| Costo en reposo | La instancia cobra aunque nadie la use | Prácticamente cero: solo pagas por auditoría real |

## Desplegarlo

Necesitas los outputs de `NlpAuditStack` ya desplegado (`FunctionName`, `InputBucket`,
`ResultBucket` en tu `cdk-outputs.json`):

```bash
cd nlp-aws-corregido
cdk deploy WebappStack \
  -c openaiSecretArn="$NLP_SECRET_ARN" \
  -c auditFunctionName="<FunctionName de cdk-outputs.json>" \
  -c auditInputBucket="<InputBucket de cdk-outputs.json>" \
  -c auditResultBucket="<ResultBucket de cdk-outputs.json>" \
  --outputs-file cdk-outputs-webapp.json
```

`-c openaiSecretArn` es necesario aunque `WebappStack` no lo use: CDK sintetiza `app.py`
completo (las dos clases de stack) antes de elegir cuál desplegar, y `AuditStack.__init__`
lo exige siempre. Al terminar, `cdk-outputs-webapp.json` trae `SiteUrl` -- esa es la
dirección que le compartes a tus testers, sin necesidad de EC2 ni de dejar tu laptop
prendida.

## Pruebas

```bash
pip install -r serverless/requirements.txt   # fastapi, mangum, boto3, reportlab, pydantic
python -m pytest tests/test_serverless_app.py tests/test_serverless_worker.py tests/test_infra.py -q
```

- `test_serverless_app.py` / `test_serverless_worker.py`: mismo patrón de fakes que el
  resto del proyecto (sin AWS real, sin OpenAI real). El fake de DynamoDB solo entiende el
  subconjunto de `UpdateExpression` que este código realmente usa -- no es un emulador
  general de DynamoDB.
- `test_infra.py`: usa `aws_cdk.assertions.Template` para verificar el CloudFormation
  sintetizado de verdad (concurrencia acotada, TTL activo, DLQ configurada, sin buckets ni
  CloudFront de más). Requiere Docker corriendo localmente -- CDK construye las imágenes de
  los Lambdas para calcular su hash de contenido incluso solo para sintetizar, no hace
  falta desplegar de verdad.
- Cada handler (`_recalcula_resumen`, `_veredicto_efectivo`) se compara contra
  `genera_resumen`/`veredicto_efectivo` reales de `app/audito_rag_pdf.py`, igual que en
  `webapp/` y `webapp-pdf/`.

## Limitaciones que siguen sin resolverse

- Sigue sin haber autenticación propia -- el link de `SiteUrl` es el secreto, igual que en
  el runbook de EC2. `max_concurrency=3` limita el daño, no lo elimina.
- Sin HTTPS con dominio propio "de fábrica": el endpoint de API Gateway HTTP API ya trae
  TLS con su propio certificado (`*.execute-api...amazonaws.com`), así que a diferencia del
  runbook de EC2 **esto sí es HTTPS desde el primer despliegue**, sin Caddy ni certificado
  manual.
- El bucket de entradas de `AuditStack` ahora tiene CORS abierto (`allowed_origins=["*"]`)
  para el método PUT -- ver el comentario en `infra/app.py`: la autorización real la da la
  firma de la URL prefirmada, no la política de CORS, pero si te incomoda un `*` ahí,
  puedes acotarlo al dominio de `SiteUrl` una vez que lo conozcas (requiere un segundo
  `cdk deploy` de `NlpAuditStack`).
