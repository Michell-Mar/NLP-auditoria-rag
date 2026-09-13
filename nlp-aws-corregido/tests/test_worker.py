import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("worker", ROOT / "app/worker.py")
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


@pytest.mark.parametrize("event", [{}, {"job_id": "../../etc"}, {"job_id": 42}, []])
def test_rejects_invalid_jobs(event):
    with pytest.raises(ValueError):
        worker.validate_event(event)


@pytest.mark.parametrize("mode,expected", [
    ("success", "SUCCEEDED"), ("rejected", "REJECTED"), ("error", "FAILED")])
def test_pipeline_lifecycle(monkeypatch, mode, expected):
    objects = {}

    class S3:
        def head_object(self, **kwargs):
            return {"ContentLength": 100}

        def download_file(self, bucket, key, destination):
            Path(destination).write_bytes(b"%PDF-1.4 test fixture")

        def put_object(self, **kwargs):
            objects[kwargs["Key"]] = kwargs["Body"]

        def upload_file(self, path, bucket, key, **kwargs):
            objects[key] = Path(path).read_bytes()

    class Secret:
        def get_secret_value(self, **kwargs):
            return {"SecretString": json.dumps({"OPENAI_API_KEY": "test-key"})}

    def run(command, **kwargs):
        assert kwargs["env"]["OPENAI_API_KEY"] == "test-key"
        assert "--revisar" not in command
        assert 0 < kwargs["timeout"] <= 840
        if mode == "error":
            return SimpleNamespace(returncode=1)
        directory = Path(kwargs["cwd"])
        report = {"resultado": "RECHAZADO"} if mode == "rejected" else {"resumen": {}}
        (directory / "resultado_auditoria.json").write_text(json.dumps(report))
        (directory / "reporte_auditoria.md").write_text("# Test report")
        return SimpleNamespace(returncode=int(mode == "rejected"))

    monkeypatch.setattr(worker.boto3, "client", lambda name: S3() if name == "s3" else Secret())
    monkeypatch.setattr(worker.subprocess, "run", run)
    for key in ("INPUT_BUCKET", "RESULT_BUCKET", "OPENAI_SECRET_ARN"):
        monkeypatch.setenv(key, "fixture")
    result = worker.handler({"job_id": "a" * 32}, SimpleNamespace(
        aws_request_id="test", get_remaining_time_in_millis=lambda: 900000))
    assert result["state"] == expected
    assert json.loads(objects[f"results/{'a' * 32}/status.json"])["state"] == expected
    assert not any("test-key" in value.decode() for value in objects.values())
    if mode != "error":
        assert f"results/{'a' * 32}/reporte_auditoria.md" in objects
