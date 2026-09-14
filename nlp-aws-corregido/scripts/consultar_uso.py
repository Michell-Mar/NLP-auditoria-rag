"""Consulta cuántas auditorías se han encolado en el auditor serverless y
contra qué tope están corriendo (ver USO_COUNTER_KEY / _registra_uso en
serverless/app.py). Guarda el último resultado en uso_actual.json (raíz del
proyecto, gitignored) para poder revisarlo sin volver a consultar AWS.
"""
import argparse
import json
from pathlib import Path

import boto3

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CACHE_PATH = PROJECT_ROOT / "uso_actual.json"
DEFAULT_OUTPUTS_PATH = PROJECT_ROOT / "cdk-outputs-webapp.json"
USO_COUNTER_KEY = "__uso_contador__"  # debe coincidir con serverless/app.py


def _tabla_desde_outputs(path: Path) -> str | None:
    if not path.exists():
        return None
    for salida in json.loads(path.read_text()).values():
        if "JobsTable" in salida:
            return salida["JobsTable"]
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", help="Perfil de AWS CLI a usar.")
    parser.add_argument("--region", help="Región de AWS (p. ej. us-east-1).")
    parser.add_argument("--jobs-table",
                        help="Nombre de la tabla Jobs de WebappStack. Si se omite, se "
                             "intenta leer de cdk-outputs-webapp.json.")
    parser.add_argument("--limite", type=int, default=10,
                        help="Tope configurado en el deploy (-c maxAuditUses); solo "
                             "informativo para este script, no lo lee de AWS.")
    args = parser.parse_args()

    jobs_table = args.jobs_table or _tabla_desde_outputs(DEFAULT_OUTPUTS_PATH)
    if not jobs_table:
        parser.error(
            "No se pudo determinar la tabla Jobs: pasa --jobs-table o asegúrate de "
            "tener cdk-outputs-webapp.json en la raíz del proyecto."
        )

    sesion = boto3.Session(profile_name=args.profile, region_name=args.region)
    tabla = sesion.resource("dynamodb").Table(jobs_table)
    item = tabla.get_item(Key={"job_id": USO_COUNTER_KEY}).get("Item")
    contador = int(item["contador"]) if item else 0

    print(f"Tabla:              {jobs_table}")
    print(f"Usos registrados:   {contador}")
    print(f"Tope configurado:   {args.limite}  (ajusta --limite si desplegaste otro valor)")
    if contador >= args.limite:
        print("⚠️  Se alcanzó (o superó) el tope configurado en el deploy.")

    CACHE_PATH.write_text(json.dumps({
        "tabla": jobs_table, "usos_registrados": contador, "limite_configurado": args.limite,
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
