// Vanilla JS, no build step. Served by the API Lambda (app.py) behind API
// Gateway. Unlike webapp/webapp-pdf, the upload is now two steps: reserve a
// job + presigned URL, then PUT the file straight to S3 -- a multipart
// upload through API Gateway would hit a Lambda synchronous invocation's
// 6 MB payload limit well before the 10 MiB PDF cap this app allows.

const dropzone = document.getElementById("dropzone");
const inputArchivo = document.getElementById("input-archivo");
const nombreArchivo = document.getElementById("nombre-archivo");
const botonAuditar = document.getElementById("boton-auditar");

const panelEstado = document.getElementById("panel-estado");
const estadoTexto = document.getElementById("estado-texto");
const panelError = document.getElementById("panel-error");
const errorTexto = document.getElementById("error-texto");
const panelRechazo = document.getElementById("panel-rechazo");
const panelResultados = document.getElementById("panel-resultados");

let archivoSeleccionado = null;
let jobIdActual = null;
let temporizadorSondeo = null;

const ESTADOS_TERMINALES = new Set(["SUCCEEDED", "REJECTED", "FAILED"]);

const ETIQUETAS_ESTADO = {
  PENDIENTE: "Preparando…",
  PROCESANDO: "Procesando (puede tardar varios minutos, incluyendo la espera en cola)…",
  SUCCEEDED: "Auditoría completada.",
  REJECTED: "Documento rechazado por el sanity check.",
  FAILED: "La auditoría falló.",
};

function ocultarTodosLosPaneles() {
  panelEstado.hidden = true;
  panelError.hidden = true;
  panelRechazo.hidden = true;
  panelResultados.hidden = true;
}

function mostrarError(mensaje) {
  ocultarTodosLosPaneles();
  errorTexto.textContent = mensaje;
  panelError.hidden = false;
}

// --- File selection (click-to-browse and drag & drop) -------------------
function seleccionaArchivo(file) {
  if (!file) return;
  if (!file.name.toLowerCase().endsWith(".pdf")) {
    mostrarError("Solo se aceptan archivos .pdf.");
    return;
  }
  archivoSeleccionado = file;
  nombreArchivo.textContent = file.name;
  nombreArchivo.hidden = false;
  botonAuditar.disabled = false;
}

inputArchivo.addEventListener("change", (e) => seleccionaArchivo(e.target.files[0]));

["dragenter", "dragover"].forEach((evento) =>
  dropzone.addEventListener(evento, (e) => {
    e.preventDefault();
    dropzone.classList.add("arrastrando");
  })
);
["dragleave", "drop"].forEach((evento) =>
  dropzone.addEventListener(evento, (e) => {
    e.preventDefault();
    dropzone.classList.remove("arrastrando");
  })
);
dropzone.addEventListener("drop", (e) => seleccionaArchivo(e.dataTransfer.files[0]));

// --- Submit (reserve -> upload to S3 -> enqueue) + polling ---------------
botonAuditar.addEventListener("click", async () => {
  if (!archivoSeleccionado) return;
  botonAuditar.disabled = true;
  ocultarTodosLosPaneles();
  panelEstado.hidden = false;

  let jobId, uploadUrl;
  try {
    estadoTexto.textContent = "Reservando la auditoría…";
    const r1 = await fetch("/api/audits", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filename: archivoSeleccionado.name }),
    });
    if (!r1.ok) {
      const detalle = await r1.json().catch(() => ({}));
      throw new Error(detalle.detail || `Error al reservar la auditoría (HTTP ${r1.status}).`);
    }
    ({ job_id: jobId, upload_url: uploadUrl } = await r1.json());

    estadoTexto.textContent = "Subiendo el PDF…";
    const r2 = await fetch(uploadUrl, {
      method: "PUT",
      headers: { "Content-Type": "application/pdf" },
      body: archivoSeleccionado,
    });
    if (!r2.ok) throw new Error("No se pudo subir el PDF. Intenta de nuevo.");

    estadoTexto.textContent = "Encolando el procesamiento…";
    const r3 = await fetch(`/api/audits/${jobId}/procesar`, { method: "POST" });
    if (!r3.ok) {
      const detalle = await r3.json().catch(() => ({}));
      throw new Error(detalle.detail || `Error al encolar la auditoría (HTTP ${r3.status}).`);
    }
  } catch (err) {
    mostrarError(err.message || "No se pudo iniciar la auditoría.");
    botonAuditar.disabled = false;
    return;
  }

  jobIdActual = jobId;
  iniciaSondeo(jobId);
});

function iniciaSondeo(jobId) {
  clearInterval(temporizadorSondeo);
  temporizadorSondeo = setInterval(() => actualizaEstado(jobId), 3000);
  actualizaEstado(jobId);
}

async function actualizaEstado(jobId) {
  let respuesta;
  try {
    respuesta = await fetch(`/api/audits/${jobId}`);
  } catch {
    return; // transient network hiccup; the next tick will retry
  }
  if (!respuesta.ok) return;
  const data = await respuesta.json();

  estadoTexto.textContent = ETIQUETAS_ESTADO[data.state] || data.state;

  if (!ESTADOS_TERMINALES.has(data.state)) return;

  clearInterval(temporizadorSondeo);
  botonAuditar.disabled = false;

  if (data.state === "FAILED") {
    mostrarError(`La auditoría falló (${data.error || "error desconocido"}). Intenta de nuevo.`);
    return;
  }

  const informe = await fetch(`/api/audits/${jobId}/informe`).then((r) => r.json());
  if (informe.resultado === "RECHAZADO") {
    renderizaRechazo(informe.validacion);
  } else {
    renderizaInforme(informe);
  }
}

// --- Rejection view ---------------------------------------------------
function renderizaRechazo(validacion) {
  ocultarTodosLosPaneles();
  document.getElementById("rechazo-tipo").textContent = validacion.tipo_documento_detectado;
  document.getElementById("rechazo-confianza").textContent = validacion.confianza;
  document.getElementById("rechazo-motivo").textContent = validacion.motivo;
  panelRechazo.hidden = false;
}

// --- Results view -------------------------------------------------------
function renderizaInforme(informe) {
  ocultarTodosLosPaneles();
  panelResultados.hidden = false;

  const r = informe.resumen;
  const veredictoEl = document.getElementById("veredicto-global");
  veredictoEl.textContent = r.veredicto_global;
  veredictoEl.dataset.veredicto = r.veredicto_global;
  document.getElementById("veredicto-porcentaje").textContent = `${r.porcentaje_cumplimiento}%`;
  document.getElementById("conteo-total").textContent = r.conteo["Cumple total"];
  document.getElementById("conteo-parcial").textContent = r.conteo["Cumple parcial"];
  document.getElementById("conteo-no").textContent = r.conteo["No cumple"];
  document.getElementById("resumen-revision").textContent =
    `Revisión humana: ${r.reglas_revisadas_por_humano}/${r.reglas_evaluadas} reglas revisadas, ` +
    `${r.reglas_ajustadas_por_humano} ajustadas.`;

  const bannerDesact = document.getElementById("banner-desactualizacion");
  const listaDesact = document.getElementById("lista-desactualizacion");
  if (r.aviso_desactualizado) {
    listaDesact.innerHTML = "";
    for (const nota of r.notas_desactualizacion) {
      const li = document.createElement("li");
      li.textContent = nota;
      listaDesact.appendChild(li);
    }
    bannerDesact.hidden = false;
  } else {
    bannerDesact.hidden = true;
  }

  document.getElementById("descarga-json").href = `/api/audits/${jobIdActual}/descarga/json`;
  document.getElementById("descarga-pdf").href = `/api/audits/${jobIdActual}/descarga/pdf`;

  renderizaReglas(informe.detalle);
}

const plantillaRegla = document.getElementById("plantilla-regla");
const listaReglas = document.getElementById("lista-reglas");

function renderizaReglas(detalle) {
  listaReglas.innerHTML = "";
  for (const regla of detalle) {
    const nodo = plantillaRegla.content.cloneNode(true);
    const li = nodo.querySelector(".regla");
    li.dataset.reglaId = regla.id;

    nodo.querySelector(".regla__titulo").textContent = regla.regla_evaluada;
    nodo.querySelector(".regla__referencia").textContent = regla.referencia_legal;

    const efectivo = (regla.revision && regla.revision.veredicto_final) || regla.dictamen.cumple;
    const chip = nodo.querySelector(".regla__chip");
    chip.textContent = efectivo;
    chip.dataset.veredicto = efectivo;

    nodo.querySelector(".regla__evidencia").textContent = regla.dictamen.evidencia_encontrada || "Ninguna";
    nodo.querySelector(".regla__faltantes").textContent = regla.dictamen.elementos_faltantes || "Ninguno";
    nodo.querySelector(".regla__justificacion").textContent = regla.dictamen.justificacion || "";

    const select = nodo.querySelector(".regla__select-veredicto");
    select.value = efectivo;
    const textarea = nodo.querySelector(".regla__nota");
    textarea.value = (regla.revision && regla.revision.nota_revisor) || "";

    const marca = nodo.querySelector(".regla__marca");
    if (regla.revision && regla.revision.ajustado) {
      marca.textContent = `⚠️ Ajustado por revisor (la IA decía: ${regla.revision.veredicto_ia})`;
    } else if (regla.revision && regla.revision.revisado) {
      marca.textContent = "✔️ Revisado";
    } else {
      marca.textContent = "Sin revisar todavía.";
    }

    listaReglas.appendChild(nodo);
  }
}

// --- Save review edits --------------------------------------------------
document.getElementById("boton-guardar-revision").addEventListener("click", async () => {
  if (!jobIdActual) return;
  const ediciones = [];
  document.querySelectorAll(".regla").forEach((li) => {
    ediciones.push({
      id: li.dataset.reglaId,
      veredicto_final: li.querySelector(".regla__select-veredicto").value,
      nota_revisor: li.querySelector(".regla__nota").value,
    });
  });

  const respuesta = await fetch(`/api/audits/${jobIdActual}/revision`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ revisor: "revisor web", ediciones }),
  });
  if (!respuesta.ok) {
    const detalle = await respuesta.json().catch(() => ({}));
    mostrarError(detalle.detail || "No se pudieron guardar los cambios.");
    return;
  }
  const informeActualizado = await respuesta.json();
  renderizaInforme(informeActualizado);

  const confirmacion = document.getElementById("guardado-confirmacion");
  confirmacion.hidden = false;
  setTimeout(() => (confirmacion.hidden = true), 2500);
});
