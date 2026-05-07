from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    _here = Path(__file__).resolve()
    sys.path.insert(0, str(_here.parents[1]))
    sys.path.insert(0, str(_here.parents[5]))
    sys.path.insert(0, str(_here.parents[4] / "kie" / "train_kie" / "lightgbm" / "code"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark LightGBM KIE on OCR JSON companion files.")
    parser.add_argument("--pdf", action="append", required=True)
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--threads", type=int, default=6)
    parser.add_argument("--page-scope", choices=["selected", "all"], default="selected")
    parser.add_argument("--log", help="Optional progress log file.")
    return parser.parse_args()


args = parse_args()
for key in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ[key] = str(args.threads)

from kie_core.labeling_workspace import analyze_page_selection  # noqa: E402
from train_lightgbm.common import read_json, write_json  # noqa: E402
from train_lightgbm.config import LABELS  # noqa: E402
from train_lightgbm.dataset import _build_features, generate_candidates, load_ocr_document  # noqa: E402
from train_lightgbm.schema_decoder import CandidatePrediction, decode_document_predictions, link_signers  # noqa: E402
from train_lightgbm.training import load_models, score_rows  # noqa: E402


def log(message: str) -> None:
    print(message, flush=True)
    if args.log:
        path = Path(args.log)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(message + "\n")


def set_model_threads(model: Any, threads: int) -> None:
    return None


def companion_json(pdf: Path) -> Path:
    path = Path(str(pdf) + ".json")
    if not path.exists():
        raise FileNotFoundError(path)
    return path


def selected_pages(raw: dict[str, Any]) -> tuple[list[int], int, int | None, dict[str, Any]]:
    all_pages = [int(page.get("page_index", idx)) for idx, page in enumerate(raw.get("pages", []))]
    selection = analyze_page_selection(raw)
    if args.page_scope == "all":
        pages = all_pages
    else:
        pages = [int(page) for page in (selection.get("selected_pages") or all_pages)]
    primary = selection.get("primary_page")
    primary_page = int(primary) if primary is not None else (all_pages[0] if all_pages else 0)
    signature = selection.get("signature_page")
    signature_page = int(signature) if signature is not None else (all_pages[-1] if len(all_pages) > 1 else None)
    return pages, primary_page, signature_page, selection


def benchmark_doc(
    pdf: Path,
    models: dict[str, Any],
    metas: dict[str, Any],
    thresholds: dict[str, float],
) -> dict[str, Any]:
    json_path = companion_json(pdf)
    raw = json.loads(json_path.read_text(encoding="utf-8"))
    pages, primary_page, signature_page, page_selection = selected_pages(raw)
    doc_meta = {
        "doc_id": pdf.stem,
        "relative_pdf_path": pdf.name,
        "split": "inference",
        "source_canonical_json": str(json_path),
        "selected_pages": pages,
        "primary_page": primary_page,
        "signature_page": signature_page,
        "doc_kind": "regular",
    }

    start = time.perf_counter()
    doc = load_ocr_document(doc_meta)
    load_seconds = time.perf_counter() - start

    decoded_input: dict[str, list[CandidatePrediction]] = {}
    candidate_seconds = 0.0
    feature_seconds = 0.0
    score_seconds = 0.0
    candidate_count = 0
    field_timings: dict[str, Any] = {}

    for field in LABELS:
        log(f"{pdf.name} field start {field}")
        start = time.perf_counter()
        candidates = generate_candidates(doc, field)
        field_candidate_seconds = time.perf_counter() - start

        rows = []
        start = time.perf_counter()
        for cand in candidates:
            page = doc.pages[cand.page_index]
            rows.append(
                {
                    "features": _build_features(
                        cand.field,
                        cand.source_kind,
                        page,
                        cand.page_role,
                        cand.line_ids,
                        cand.word_ids,
                        cand.bbox,
                        cand.text,
                        cand.normalized_text,
                        doc,
                    ),
                    "target": 0,
                }
            )
        field_feature_seconds = time.perf_counter() - start

        start = time.perf_counter()
        scores = score_rows(models[field], rows, metas[field]["feature_names"])
        field_score_seconds = time.perf_counter() - start

        decoded_input[field] = [
            CandidatePrediction(
                field=field,
                score=score,
                page_index=cand.page_index,
                line_ids=list(cand.line_ids),
                word_ids=list(cand.word_ids),
                bbox=list(cand.bbox),
                text=cand.text,
                candidate_id=cand.candidate_id,
            )
            for cand, score in zip(candidates, scores)
        ]
        candidate_count += len(candidates)
        candidate_seconds += field_candidate_seconds
        feature_seconds += field_feature_seconds
        score_seconds += field_score_seconds
        field_timings[field] = {
            "candidates": len(candidates),
            "candidate_seconds": field_candidate_seconds,
            "feature_seconds": field_feature_seconds,
            "score_seconds": field_score_seconds,
        }
        log(f"{pdf.name} field done {field}: candidates={len(candidates)}")

    start = time.perf_counter()
    decoded = decode_document_predictions(decoded_input, thresholds)
    relations = link_signers(decoded)
    decode_seconds = time.perf_counter() - start
    total_seconds = load_seconds + candidate_seconds + feature_seconds + score_seconds + decode_seconds
    word_count = sum(len(doc.pages[p].words) for p in pages if p in doc.pages)
    return {
        "pdf": str(pdf),
        "json": str(json_path),
        "page_scope": args.page_scope,
        "selected_pages": pages,
        "page_selection": {key: value for key, value in page_selection.items() if key != "candidates"},
        "pages": len(pages),
        "words": word_count,
        "candidate_count": candidate_count,
        "field_count": sum(len(items) for items in decoded.values()),
        "relation_count": len(relations),
        "seconds": {
            "load_ocr_json": load_seconds,
            "candidate_generation": candidate_seconds,
            "feature_build": feature_seconds,
            "model_score": score_seconds,
            "decode": decode_seconds,
            "total_after_ocr_json": total_seconds,
        },
        "field_timings": field_timings,
    }


def main() -> None:
    project_root = Path(args.project_root)
    models_dir = project_root / "models" / "fieldwise"
    start = time.perf_counter()
    thresholds = read_json(models_dir / "thresholds.json", default={})
    models = load_models(models_dir)
    for model in models.values():
        set_model_threads(model, args.threads)
    metas = {field: read_json(models_dir / f"{field}.meta.json") for field in models}
    model_load_seconds = time.perf_counter() - start

    report: dict[str, Any] = {
        "method": "lightgbm",
        "cpu_threads": args.threads,
        "page_scope": args.page_scope,
        "model_load_seconds": model_load_seconds,
        "documents": [],
    }
    for raw_pdf in args.pdf:
        pdf = Path(raw_pdf)
        log(f"doc start {pdf}")
        item = benchmark_doc(pdf, models, metas, thresholds)
        report["documents"].append(item)
        write_json(args.output, report)
        log(f"doc done {pdf.name}: total={item['seconds']['total_after_ocr_json']:.3f}s")

    pages = sum(item["pages"] for item in report["documents"])
    total = sum(item["seconds"]["total_after_ocr_json"] for item in report["documents"])
    score = sum(item["seconds"]["model_score"] for item in report["documents"])
    report["aggregate"] = {
        "documents": len(report["documents"]),
        "pages": pages,
        "total_seconds": total,
        "total_ms_per_page": total * 1000.0 / pages if pages else 0.0,
        "model_score_seconds": score,
        "model_score_ms_per_page": score * 1000.0 / pages if pages else 0.0,
        "candidate_count": sum(item["candidate_count"] for item in report["documents"]),
    }
    write_json(args.output, report)
    log(json.dumps(report["aggregate"], ensure_ascii=False))


if __name__ == "__main__":
    main()
