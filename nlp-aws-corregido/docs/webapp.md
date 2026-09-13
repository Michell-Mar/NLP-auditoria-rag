# Interfaz web local

Una página para subir el PDF, ver el dictamen en pantalla, corregirlo si hace falta y
descargar el reporte, sin usar la terminal para cada paso. Es una capa adicional sobre
`scripts/audit.py`: sigue invocando el mismo Lambda desplegado, con las mismas
credenciales de AWS. **No es un servicio público.** Corre en tu máquina, escucha solo en
`127.0.0.1` por defecto, y las credenciales de AWS nunca salen de este servidor local: el
navegador solo le habla a él por HTTP, nunca directamente a AWS.

`app/` e `infra/` no se tocan. El Lambda desplegado sigue siendo exactamente el mismo.

## Instalación

```bash
pip install -r webapp/requirements.txt
```

(`webapp/requirements.txt` está escrito a mano, no generado con pip-compile como el resto
del repo — si quieres la misma reproducibilidad, corre pip-compile sobre él tú mismo.)

## Ejecutar

Con el stack ya desplegado (`cdk-outputs.json` presente en la raíz) y sesión de AWS
iniciada:

```bash
python webapp/server.py --profile nlp-dev --region us-east-1
```

Abre `http://127.0.0.1:8000`. Para exponerlo en otra interfaz de red usa `--host`/`--port`,
pero recuerda que quien acceda a esa dirección puede disparar auditorías facturables en tu
cuenta — no lo expongas fuera de tu propia máquina sin agregar autenticación.

## Qué hace y qué no hace

- Sube el PDF a S3, invoca el Lambda de forma síncrona (igual que `scripts/audit.py`) y
  sondea el estado desde el navegador cada 3 segundos mientras espera.
- Muestra el resumen, el desglose por las nueve reglas y la nota de desactualización
  normativa si el auditor la detectó.
- Si el sanity check rechaza el documento (no parece un aviso de privacidad), lo muestra
  con el tipo detectado y el motivo, en vez de una tabla vacía.
- Permite corregir el veredicto de cada regla y anotar por qué, exactamente como el modo
  `--revisar` del CLI, pero en un formulario en vez de por terminal.
- Descarga el JSON y el Markdown, reflejando las correcciones si las hiciste.
- **No** reevalúa una regla con el modelo (el `r` de re-evaluación de `--revisar` no tiene
  equivalente aquí); un cambio de veredicto es una corrección humana, no una nueva
  llamada al LLM.
- **No** persiste nada entre reinicios del servidor: el registro de auditorías vive en
  memoria del proceso. Si reinicias `webapp/server.py`, los `job_id` anteriores dejan de
  existir (los reportes ya generados siguen en el bucket de resultados en S3, con su
  política de expiración de 7 días).
- **No** agrega autenticación, HTTPS, ni límite de tasa. Es una herramienta de operador
  para uso local, con el mismo modelo de confianza que ejecutar `scripts/audit.py` en tu
  terminal.

## Pruebas

```bash
python -m pytest tests/test_webapp.py -q
```

Usan clientes de S3/Lambda simulados, igual que `tests/test_worker.py` — no llaman a AWS
ni a OpenAI. Una de las pruebas (`test_recompute_logic_matches_app_audito_rag_pdf`)
compara el recálculo de resumen de `webapp/server.py` contra `genera_resumen`/
`veredicto_efectivo` de `app/audito_rag_pdf.py`: la web **duplica** esa lógica en vez de
importarla (para no forzar a un servidor HTML sencillo a instalar langchain/openai), así
que esta prueba es la que evita que las dos copias se desincronicen con el tiempo. Si
modificas `genera_resumen` o `veredicto_efectivo` en `app/audito_rag_pdf.py`, revisa y
actualiza `_recalcula_resumen`/`_veredicto_efectivo` en `webapp/server.py` a mano.
