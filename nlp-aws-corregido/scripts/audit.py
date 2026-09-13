"""Upload one PDF, invoke the private worker, then download its reports."""
import argparse
import json
from pathlib import Path
import sys
import uuid

import boto3
from botocore.config import Config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--outputs", type=Path, default=Path("cdk-outputs.json"))
    parser.add_argument("--profile")
    parser.add_argument("--region", required=True)
    args = parser.parse_args()
    if not args.pdf.is_file() or args.pdf.stat().st_size > 10 * 1024 * 1024:
        parser.error("Provide an existing PDF of at most 10 MiB")
    outputs = json.loads(args.outputs.read_text())["NlpAuditStack"]
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    s3 = session.client("s3")
    # Never retry a paid audit implicitly after a connection error.
    client = session.client("lambda", config=Config(
        read_timeout=930, connect_timeout=10, retries={"total_max_attempts": 1}))
    job_id = uuid.uuid4().hex
    prefix = f"results/{job_id}/"
    print(f"Job: {job_id}", flush=True)
    print(f"Status/reports: s3://{outputs['ResultBucket']}/{prefix}", flush=True)
    s3.upload_file(str(args.pdf), outputs["InputBucket"], f"inputs/{job_id}/document.pdf",
                   ExtraArgs={"ContentType": "application/pdf"})
    response = client.invoke(FunctionName=outputs["FunctionName"],
                             InvocationType="RequestResponse",
                             Payload=json.dumps({"job_id": job_id}).encode())
    result = json.loads(response["Payload"].read())
    print(json.dumps(result, indent=2))
    if response.get("FunctionError") or result.get("state") == "FAILED":
        sys.exit(1)
    if "result_prefix" in result:
        destination = Path("downloads") / job_id
        destination.mkdir(parents=True, exist_ok=True)
        for name in ("resultado_auditoria.json", "reporte_auditoria.md", "status.json"):
            s3.download_file(outputs["ResultBucket"], prefix + name, str(destination / name))
        print(f"Reports: {destination.resolve()}")
    if result.get("state") != "SUCCEEDED":
        sys.exit(2)


if __name__ == "__main__":
    main()
