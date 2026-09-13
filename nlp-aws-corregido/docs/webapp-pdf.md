# Interfaz web local — variante PDF

Igual que [`webapp/`](webapp.md) (mismo Lambda, mismo modelo de confianza: local, credenciales de
AWS que nunca salen de este servidor), con una sola diferencia: el reporte descargable es un
**PDF renderizado** (`reportlab`, Python puro, sin dependencias de sistema) en vez de Markdown.
Lee primero [docs/webapp.md](webapp.md) — aquí solo se documenta lo que cambia.

## Instalación y ejecución

```bash
pip install -r webapp-pdf/requirements.txt
python webapp-pdf/server.py --profile nlp-dev --region us-east-1
```

Mismo `http://127.0.0.1:8000`, mismos endpoints, salvo `GET /api/audits/{job_id}/descarga/pdf`
en vez de `.../descarga/md`.

## Qué cambia frente a `webapp/`

- `build_pdf_report(informe)` en `webapp-pdf/server.py` arma el PDF directamente desde el JSON
  del resultado (no parte del Markdown ni de `reporte_auditoria.md` en S3 — ese archivo, que el
  worker sigue escribiendo, simplemente no se usa en esta variante).
- Colores del veredicto (verde/ámbar/rojo) igual que los chips de la interfaz web, usando los
  mismos tonos de `static/styles.css`.
- **Sin emoji.** Se probó renderizando a imagen: los emoji (`⚠️`, `✔️`, `⛔`) salen como
  recuadros negros sólidos con las fuentes base de PDF (Helvetica/WinAnsi no los incluyen). Se
  reemplazaron por texto plano: `AVISO:`, `[Revisado]`, `RECHAZADO — ...`. Los acentos y eñes del
  español sí se renderizan bien con esas fuentes, así que el resto del texto no cambia.
- Cada regla se dibuja como una tarjeta con borde (`reportlab.platypus.Table` + `KeepTogether`)
  que nunca se corta entre dos páginas.
- El documento rechazado (`resultado: RECHAZADO`) también genera un PDF corto, no solo el JSON.

## Pruebas

```bash
python -m pytest tests/test_webapp_pdf.py -q
```

Mismo patrón de mocks que `tests/test_webapp.py`. Las aserciones sobre contenido leen el PDF de
vuelta con `pypdf` (`PdfReader(...).extract_text()`) en vez de comparar texto de Markdown. También
incluye la misma prueba cruzada contra `genera_resumen`/`veredicto_efectivo` de
`app/audito_rag_pdf.py`.

## Cuándo usar esta variante en vez de `webapp/`

Un PDF es lo que normalmente se archiva, se firma o se adjunta a un correo de cumplimiento;
Markdown es más cómodo si el destino es un repositorio, un wiki o seguir editando el texto. Son
dos servidores independientes (puertos distintos si corren a la vez) — no hay pérdida en tener
ambos disponibles.
