# Evidencia para el portafolio

Separa demostrar que la aplicación despliega de demostrar que sus dictámenes son correctos. Las pruebas del repositorio verifican el adaptador y configuración de infraestructura; no miden calidad NLP.

## Protocolo inicial

1. Seleccionar avisos utilizables y representativos de formatos, longitudes y sectores; separar un conjunto de desarrollo y uno de prueba.
2. Etiquetar cada criterio con revisión humana, evidencia textual y fuente/versión normativa. Registrar desacuerdos de revisores.
3. Comparar documento completo frente a recuperación MMR sobre documentos que ambos métodos puedan procesar; mantener modelo y reglas constantes.
4. Medir macro-F1 por dictamen, matriz de confusión y resultados por criterio. Medir además si la evidencia respalda el dictamen y si la recuperación trae los fragmentos etiquetados.
5. Reportar latencia p50/p95, tasa de errores, tokens y costo por documento con número de observaciones. Registrar modelo, prompts, versión del código y fecha.

| Métrica | Estado |
|---|---|
| Documentos etiquetados | Pendiente |
| Macro-F1 por dictamen | Pendiente |
| Recuperación de evidencia | Pendiente |
| Latencia p50/p95 | Pendiente |
| Costo por auditoría | Pendiente |
| Despliegue AWS verificado | Pendiente |

Para el README público, añade un video corto y un reporte sintético. Describe sólo capacidades implementadas y resultados medidos; marca las funcionalidades futuras como tales. No publiques este archivo como si las métricas pendientes ya estuvieran obtenidas.
