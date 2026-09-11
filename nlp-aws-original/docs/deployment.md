# Despliegue reproducible

## 1. Preparación local

Utiliza Python 3.12 y Node.js 22. Instala AWS CLI v2 y Docker Desktop para tu sistema. Docker debe estar iniciado al desplegar. En Mac con Apple Silicon, la imagen está configurada para `linux/amd64`; la construcción puede ser más lenta por emulación.

Desde la raíz de este proyecto:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
npm install -g aws-cdk
aws configure sso --profile nlp-dev
aws sso login --profile nlp-dev
aws sts get-caller-identity --profile nlp-dev
python -m pytest -q
```

El administrador de tu cuenta debe habilitar IAM Identity Center para utilizar SSO. Si tu cuenta utiliza otro método de autenticación, configura su perfil de AWS CLI. No agregues access keys a los archivos del repo.

`requirements.txt` y `requirements-dev.txt` fijan las dependencias Python resueltas. Los `.in` documentan sus rangos de mantenimiento. La etiqueta de la imagen base, el CLI CDK y las etiquetas mayores de Actions aún pueden cambiar: para una release plenamente fijada, registra la versión del CLI usado, fija la imagen por digest y Actions por commit SHA después de validar CI.

## 2. Crear el secreto

```bash
python scripts/create_secret.py --profile nlp-dev --region us-east-1
```

Introduce la clave en la solicitud oculta. El script crea `nlp-audit/openai` y devuelve su ARN. Ejecuta este paso una sola vez; si el secreto ya existe, actualízalo en Secrets Manager y conserva su ARN. El secreto debe estar en la misma cuenta y región de este despliegue.

El ARN no es el valor de la clave. Puedes versionar nombres lógicos o ejemplos de ARN, pero no el valor del secreto. Para los comandos siguientes, sustituye los valores de ejemplo:

```bash
export AWS_PROFILE=nlp-dev
export AWS_REGION=us-east-1
export AWS_DEFAULT_REGION=us-east-1
export NLP_ACCOUNT_ID=123456789012
export NLP_SECRET_ARN='arn:aws:secretsmanager:us-east-1:123456789012:secret:nlp-audit/openai-ABCDEF'
```

## 3. Preparar CDK y revisar la infraestructura

```bash
cdk bootstrap aws://$NLP_ACCOUNT_ID/$AWS_REGION -c openaiSecretArn="$NLP_SECRET_ARN"
cdk synth -c openaiSecretArn="$NLP_SECRET_ARN"
cdk diff -c openaiSecretArn="$NLP_SECRET_ARN"
```

Bootstrap crea recursos auxiliares de CDK, incluidos almacenamiento de assets y roles. La identidad que despliega necesita permisos de bootstrap y CloudFormation/CDK suficientes para crear estos recursos. El rol de ejecución de la aplicación tiene permisos más limitados; no uses su política como política de despliegue.

## 4. Desplegar

Este comando crea recursos facturables en la cuenta seleccionada:

```bash
cdk deploy -c openaiSecretArn="$NLP_SECRET_ARN" --outputs-file cdk-outputs.json
```

Lee los cambios y la confirmación que muestre CDK. La imagen Docker se construye y publica como asset de CDK en ECR. No debes crear un repositorio ECR manualmente.

Configuración inicial: Lambda de 2,048 MiB, máximo 900 segundos, 512 MiB temporales, PDFs de máximo 10 MiB y modelo de auditoría `gpt-4o`. La validación sigue usando `gpt-4o-mini`; embeddings, `text-embedding-3-small`. Verifica acceso en tu cuenta del proveedor. Para cambiar el modelo principal:

```bash
cdk deploy -c openaiSecretArn="$NLP_SECRET_ARN" -c auditModel=gpt-4o-mini --outputs-file cdk-outputs.json
```

Cambiar el modelo exige volver a evaluar calidad; no asumas resultados equivalentes.

## 5. Ejecutar y comprobar

```bash
python scripts/audit.py /ruta/aviso.pdf --profile nlp-dev --region us-east-1
```

La terminal permanece esperando. Guarda el `job_id` que imprime. Comprueba que los reportes descargados contienen los nueve criterios y que corresponden al PDF. Un documento rechazado devuelve código de salida 2; un fallo de ejecución devuelve 1. Un rechazo de tipo documental puede incluir su propio reporte.

Si se corta la conexión, revisa primero `results/<job_id>/status.json` en el bucket de resultados. La función puede continuar ejecutándose: no vuelvas a enviar el mismo PDF inmediatamente. Ante un timeout abrupto de Lambda, el estado podría quedarse en `RUNNING`; contrasta la hora y el request ID con CloudWatch. No hay proceso automático para reconciliar estados abandonados.

Para revisar logs, toma `LogGroup` de `cdk-outputs.json`:

```bash
aws logs tail NOMBRE_DEL_LOG_GROUP --since 1h --profile nlp-dev --region us-east-1
```

Los logs muestran estado, duración y tipo de error; el stdout del auditor se suprime porque puede incluir fragmentos del documento. Para errores detallados, reproduce el problema localmente con un PDF sintético:

```bash
python app/audito_rag_pdf.py /ruta/aviso.pdf --revisar
```

Para la ejecución local, copia `.env.example` a `.env` y añade la clave sólo en tu máquina.

## 6. GitHub

Si estás incorporándolo a un repo existente, copia y revisa cada cambio antes de confirmar. Si creas un repo nuevo, crea primero el repositorio vacío en GitHub y usa su URL:

```bash
git init -b main
git add .
git diff --cached --stat
git diff --cached
git commit -m "Add reproducible AWS deployment for NLP auditor"
git remote add origin https://github.com/TU_USUARIO/TU_REPO.git
git push -u origin main
```

Antes del commit, comprueba que no se incluyen secretos ni documentos reales. La CI no requiere credenciales AWS; las pruebas usan servicios simulados y una plantilla sintetizada. La construcción real del contenedor ocurre en GitHub Actions.

## 7. Retirar el despliegue

```bash
cdk destroy -c openaiSecretArn="$NLP_SECRET_ARN"
```

Los buckets se conservan intencionalmente con `RETAIN`. Sus objetos tienen una política de expiración de siete días; la eliminación por lifecycle no es instantánea. El secreto fue creado fuera del stack y también se conserva. Los assets de bootstrap/ECR y el stack CDKToolkit no se eliminan con este comando. Si ya no los necesitas, elimina manualmente los recursos dedicados al proyecto después de revisar su contenido y dependencias. No elimines recursos de bootstrap compartidos con otros proyectos.

## Referencias

- [AWS CDK: inicio y autenticación](https://docs.aws.amazon.com/cdk/v2/guide/getting-started.html)
- [Bootstrap de CDK](https://docs.aws.amazon.com/cdk/v2/guide/bootstrapping.html)
- [Imágenes Python de Lambda](https://docs.aws.amazon.com/lambda/latest/dg/python-image.html)
- [Límite de duración](https://docs.aws.amazon.com/lambda/latest/dg/configuration-timeout.html)
- [Almacenamiento temporal](https://docs.aws.amazon.com/lambda/latest/dg/configuration-ephemeral-storage.html)
