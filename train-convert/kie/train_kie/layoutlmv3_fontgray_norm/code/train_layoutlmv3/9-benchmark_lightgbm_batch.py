from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from statistics import mean, median
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from train_lightgbm.common import read_json, write_json
from train_lightgbm.dataset import _build_features, generate_candidates, load_ocr_document
from train_lightgbm.schema_decoder import CandidatePrediction, decode_document_predictions, link_signers
from train_lightgbm.training import load_models, score_rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark LightGBM KIE inference on one labeled batch.")
    parser.add_argument("--project-root", required=True, help="LightGBM project root.")
    parser.add_argument("--label-rel-prefix", default="batch_0027/")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--output", help="Defaults to reports/lightgbm_<batch>_speed_report.json")
    return parser.parse_args()


def batch_docs(manifest: dict[str, Any], prefix: str) -> list[dict[str, Any]]:
    prefix = prefix.replace("\\", "/")
    if not prefix.endswith("/"):
        prefix += "/"
    docs = []
    for doc in manifest.get("documents", []):
        label_output = str(doc.get("label_output_json") or "").replace("\\", "/")
        if f"/json_output_labeled/{prefix}" in label_output or label_output.endswith(prefix.rstrip("/")):
            docs.append(doc)
    return docs


def predict_doc(doc_meta: dict[str, Any], models: dict[str, Any], metas: dict[str, Any], thresholds: dict[str, float]) -> dict[str, Any]:
    doc_start = time.perf_counter()
    doc = load_ocr_document(doc_meta)
    load_seconds = time.perf_counter() - doc_start

    candidate_seconds = 0.0
    score_seconds = 0.0
    decoded_input = {}
    candidate_count = 0
    for field, model in models.items():
        start = time.perf_counter()
        candidates = generate_candidates(doc, field)
        rows = []
        for cand in candidates:
            page = doc.pages[cand.page_index]
            features = _build_features(
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
            )
            rows.append({"features": features, "target": 0})
        candidate_seconds += time.perf_counter() - start
        candidate_count += len(candidates)

        start = time.perf_counter()
        scores = score_rows(model, rows, metas[field]["feature_names"])
        score_seconds += time.perf_counter() - start
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

    start = time.perf_counter()
    decoded = decode_document_predictions(decoded_input, thresholds)
    relations = link_signers(decoded)
    decode_seconds = time.perf_counter() - start
    total_seconds = load_seconds + candidate_seconds + score_seconds + decode_seconds
    return {
        "doc_id": doc.doc_id,
        "split": doc.split,
        "pages": len(doc.selected_pages),
        "candidate_count": candidate_count,
        "field_count": sum(len(items) for items in decoded.values()),
        "relation_count": len(relations),
        "seconds": {
            "load_canonical": load_seconds,
            "candidate_and_features": candidate_seconds,
            "model_score": score_seconds,
            "decode": decode_seconds,
            "total": total_seconds,
        },
    }


def summarize(values: list[float]) -> dict[str, float]:
    if not values:
        return {"mean": 0.0, "median": 0.0, "min": 0.0, "max": 0.0}
    return {
        "mean": mean(values),
        "median": median(values),
        "min": min(values),
        "max": max(values),
    }


def main() -> None:
    args = parse_args()
    project_root = Path(args.project_root)
    manifest = read_json(project_root / "manifest.json")
    docs = batch_docs(manifest, args.label_rel_prefix)
    if not docs:
        raise SystemExit(f"No docs found for prefix {args.label_rel_prefix}")

    models_dir = project_root / "models" / "fieldwise"
    start = time.perf_counter()
    thresholds = read_json(models_dir / "thresholds.json", default={})
    models = load_models(models_dir)
    metas = {field: read_json(models_dir / f"{field}.meta.json") for field in models}
    model_load_seconds = time.perf_counter() - start

    run_reports = []
    for repeat in range(args.repeats):
        start = time.perf_counter()
        docs_report = [predict_doc(doc, models, metas, thresholds) for doc in docs]
        elapsed = time.perf_counter() - start
        run_reports.append({"repeat": repeat + 1, "elapsed_seconds": elapsed, "documents": docs_report})

    best_run = min(run_reports, key=lambda item: item["elapsed_seconds"])
    doc_reports = best_run["documents"]
    pages = sum(item["pages"] for item in doc_reports)
    candidates = sum(item["candidate_count"] for item in doc_reports)
    totals = [item["seconds"]["total"] for item in doc_reports]
    score_totals = [item["seconds"]["model_score"] for item in doc_reports]
    report = {
        "project_root": str(project_root.resolve()),
        "label_rel_prefix": args.label_rel_prefix,
        "docs": len(doc_reports),
        "pages": pages,
        "candidate_count": candidates,
        "model_load_seconds": model_load_seconds,
        "repeats": args.repeats,
        "best_elapsed_seconds": best_run["elapsed_seconds"],
        "end_to_end_after_ocr_json": {
            "seconds": sum(totals),
            "ms_per_doc": sum(totals) * 1000.0 / len(doc_reports),
            "ms_per_page": sum(totals) * 1000.0 / pages if pages else 0.0,
            "docs_per_second": len(doc_reports) / sum(totals) if totals else 0.0,
            "pages_per_second": pages / sum(totals) if totals else 0.0,
        },
        "model_score_only": {
            "seconds": sum(score_totals),
            "ms_per_doc": sum(score_totals) * 1000.0 / len(doc_reports),
            "ms_per_page": sum(score_totals) * 1000.0 / pages if pages else 0.0,
        },
        "per_doc_total_seconds": summarize(totals),
        "per_doc_model_score_seconds": summarize(score_totals),
        "documents": doc_reports,
    }
    output = (
        Path(args.output)
        if args.output
        else project_root / "reports" / f"lightgbm_{args.label_rel_prefix.strip('/').replace('/', '_')}_speed_report.json"
    )
    write_json(output, report)
    print(json.dumps({k: v for k, v in report.items() if k != "documents"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
