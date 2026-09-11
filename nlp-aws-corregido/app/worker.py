"""Private, synchronous Lambda worker. One temporary directory per audit."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

import boto3


def validate_event(event):
    if not isinstance(event, dict):
        raise ValueError("Expected an object")
    job_id = event.get("job_id", "")
    if not isinstance(job_id, str) or not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise ValueError("job_id must be a UUID hex string")
    return job_id


def handler(event, context):
    job_id = validate_event(event)
    s3 = boto3.client("s3")
    input_bucket = os.environ["INPUT_BUCKET"]
    result_bucket = os.environ["RESULT_BUCKET"]
    key = f"inputs/{job_id}/document.pdf"
    output_prefix = f"results/{job_id}/"
    started = time.monotonic()

    def status(state, **extra):
        data = {"job_id": job_id, "state": state,
                "request_id": context.aws_request_id, **extra}
        s3.put_object(Bucket=result_bucket, Key=output_prefix + "status.json",
                      Body=json.dumps(data).encode(), ContentType="application/json")
        return data

    try:
        metadata = s3.head_object(Bucket=input_bucket, Key=key)
        if metadata["ContentLength"] > int(os.environ.get("MAX_PDF_BYTES", "10485760")):
            return status("REJECTED", reason="PDF exceeds the configured size limit")
        status("RUNNING")
        secret = boto3.client("secretsmanager").get_secret_value(
            SecretId=os.environ["OPENAI_SECRET_ARN"])
        api_key = json.loads(secret["SecretString"])["OPENAI_API_KEY"]
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("Missing API key")
        with tempfile.TemporaryDirectory(prefix="audit-", dir="/tmp") as directory:
            pdf_path = Path(directory) / "document.pdf"
            s3.download_file(input_bucket, key, str(pdf_path))
            with pdf_path.open("rb") as stream:
                if b"%PDF-" not in stream.read(1024):
                    return status("REJECTED", reason="File does not have a PDF header")
            env = os.environ.copy()
            env["OPENAI_API_KEY"] = api_key
            # Only pass a server-configured model. Do not accept commands from the event.
            timeout = min(840, context.get_remaining_time_in_millis() / 1000 - 30)
            if timeout <= 0:
                raise TimeoutError("Insufficient remaining time")
            process = subprocess.run(
                [sys.executable, str(Path(__file__).with_name("audito_rag_pdf.py")),
                 str(pdf_path), "--modelo", os.environ.get("AUDIT_MODEL", "gpt-4o")],
                cwd=directory, env=env, timeout=timeout, check=False,
                # The CLI can print document excerpts. Keep them out of CloudWatch.
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            report_path = Path(directory) / "resultado_auditoria.json"
            report = json.loads(report_path.read_text()) if report_path.exists() else None
            rejected = isinstance(report, dict) and report.get("resultado") == "RECHAZADO"
            if process.returncode != 0 and not rejected:
                raise RuntimeError("Pipeline failed")
            for filename, content_type in (
                ("resultado_auditoria.json", "application/json"),
                ("reporte_auditoria.md", "text/markdown; charset=utf-8"),
            ):
                s3.upload_file(str(Path(directory) / filename), result_bucket,
                               output_prefix + filename,
                               ExtraArgs={"ContentType": content_type})
        state = "REJECTED" if rejected else "SUCCEEDED"
        result = status(state, elapsed_seconds=round(time.monotonic() - started, 2),
                        model=os.environ.get("AUDIT_MODEL", "gpt-4o"),
                        result_prefix=output_prefix)
        print(json.dumps(result))
        return result
    except Exception as error:
        # Do not log exception text: third-party errors can contain sensitive data.
        error_type = type(error).__name__
        print(json.dumps({"job_id": job_id, "state": "FAILED", "error_type": error_type}))
        return status("FAILED", error_type=error_type)
