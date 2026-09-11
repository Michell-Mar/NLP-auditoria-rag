"""Compare full context, MMR retrieval, and an optional paired human review locally."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import audito_rag_pdf as auditor


def evaluate(chain, rules, vector_store, text, full_context, k):
    results = []
    for rule in rules:
        context = auditor.preparar_contexto(rule, vector_store, text, full_context, k)
        results.append({
            "id": rule["id"], "regla_evaluada": rule["pregunta"],
            "referencia_legal": rule["referencia"], "contexto_usado": context,
            "dictamen": auditor.evalua_regla(chain, rule, context), "revision": None,
        })
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--modelo", default="gpt-4o")
    parser.add_argument("--k", type=int, default=8)
    parser.add_argument("--revisar-base", choices=["contexto_completo", "embeddings"])
    parser.add_argument("--revisor", default="revisor")
    parser.add_argument("--salida", type=Path, default=Path("experimentos"))
    args = parser.parse_args()
    if args.k < 1:
        parser.error("--k must be positive")
    pdf = args.pdf.resolve(strict=True)
    if args.revisar_base and not sys.stdin.isatty():
        parser.error("Human review requires an interactive terminal")
    output = args.salida.resolve() / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output.mkdir(parents=True, exist_ok=False)
    original_cwd = Path.cwd()
    metadata = {
        "pdf_sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
        "code_sha256": hashlib.sha256(Path(auditor.__file__).read_bytes()).hexdigest(),
        "model": args.modelo, "k": args.k, "human_base": args.revisar_base,
        "status": "RUNNING", "seconds": {},
    }

    def save(name, results):
        folder = output / name
        folder.mkdir()
        os.chdir(folder)
        auditor.guarda_reportes(results, auditor.genera_resumen(results), str(pdf))
        os.chdir(output)

    try:
        os.chdir(output)
        chunks, text = auditor.fase_1_ingesta_y_chunking(str(pdf))
        # Keep a conservative experiment bound for a fair same-document comparison.
        if len(text) > auditor.MAX_CHARS_DOC_COMPLETO:
            raise ValueError("Use a PDF of at most 60,000 extracted characters for this comparison")
        validation = auditor.verifica_es_aviso_privacidad(
            auditor.construir_chain_validacion(), text, True)
        if not validation["es_aviso_privacidad"]:
            auditor.guarda_reporte_rechazo(validation, str(pdf))
            metadata["status"] = "REJECTED"
            return
        rules = [dict(rule) for rule in auditor.DICCIONARIO_LFPDPPP]
        chain = auditor.construir_chain(args.modelo)
        started = time.monotonic()
        full_results = evaluate(chain, rules, None, text, True, args.k)
        metadata["seconds"]["contexto_completo"] = time.monotonic() - started
        save("contexto_completo", full_results)
        started = time.monotonic()
        vector_store = auditor.fase_2_vectorizacion(chunks)
        rag_results = evaluate(chain, rules, vector_store, text, False, args.k)
        metadata["seconds"]["embeddings"] = time.monotonic() - started
        save("embeddings", rag_results)
        if args.revisar_base:
            baseline = full_results if args.revisar_base == "contexto_completo" else rag_results
            started = time.monotonic()
            reviewed = auditor.fase_5b_revision_humana(
                copy.deepcopy(baseline), chain, vector_store, text,
                args.revisar_base == "contexto_completo", copy.deepcopy(rules), args.revisor)
            metadata["seconds"]["revision_usuario"] = time.monotonic() - started
            save("revision_usuario", reviewed)
        metadata["status"] = "COMPLETED"
    except Exception as error:
        metadata["status"] = "FAILED"
        metadata["error_type"] = type(error).__name__
        raise
    finally:
        (output / "experimento.json").write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")
        os.chdir(original_cwd)
        print(f"Experiment outputs: {output}")


if __name__ == "__main__":
    main()
