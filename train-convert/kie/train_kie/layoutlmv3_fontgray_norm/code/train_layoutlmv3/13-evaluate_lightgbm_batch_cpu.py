from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from train_lightgbm.common import read_json, read_jsonl, write_json
from train_lightgbm.config import LABELS
from train_lightgbm.dataset import _build_features, generate_candidates, load_ocr_document
from train_lightgbm.schema_decoder import CandidatePrediction, decode_document_predictions, link_signers
from train_lightgbm.training import _evaluate_scored_candidates, load_models, score_rows

_MODELS: dict[str, Any] | None = None
_METAS: dict[str, Any] | None = None
_THRESHOLDS: dict[str, float] | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate and benchmark LightGBM KIE on one labeled batch.")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--label-rel-prefix", default="batch_0027/")
    parser.add_argument("--output", required=True)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--worker-omp-threads", type=int, default=1)
    return parser.parse_args()


def normalized_prefix(prefix: str) -> str:
    prefix = prefix.replace("\\", "/")
    return prefix if prefix.endswith("/") else prefix + "/"


def batch_docs(manifest: dict[str, Any], prefix: str) -> list[dict[str, Any]]:
    prefix = normalized_prefix(prefix)
    docs = []
    for doc in manifest.get("documents", []):
        label_output = str(doc.get("label_output_json") or "").replace("\\", "/")
        if f"/json_output_labeled/{prefix}" in label_output:
            docs.append(doc)
    return docs


def init_worker(models_dir: str, omp_threads: int) -> None:
    global _MODELS, _METAS, _THRESHOLDS
    for key in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[key] = str(max(1, omp_threads))
    models_path = Path(models_dir)
    _THRESHOLDS = read_json(models_path / "thresholds.json", default={})
    _MODELS = load_models(models_path)
    _METAS = {field: read_json(models_path / f"{field}.meta.json") for field in _MODELS}


def worker_ready(_: int) -> int:
    return os.getpid()


def predict_doc(doc_meta: dict[str, Any]) -> dict[str, Any]:
    if _MODELS is None or _METAS is None or _THRESHOLDS is None:
        raise RuntimeError("Worker models were not initialized.")
    start = time.perf_counter()
    doc = load_ocr_document(doc_meta)
    load_seconds = time.perf_counter() - start

    candidate_seconds = 0.0
    score_seconds = 0.0
    decoded_input: dict[str, list[CandidatePrediction]] = {}
    candidate_count = 0
    for field, model in _MODELS.items():
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
        scores = score_rows(model, rows, _METAS[field]["feature_names"])
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
    decoded = decode_document_predictions(decoded_input, _THRESHOLDS)
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
        "predictions": decoded_input,
    }


def load_gt_rows(project_root: Path, docs: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    keep_by_split: dict[str, set[str]] = {"train": set(), "val": set(), "test": set()}
    for doc in docs:
        keep_by_split.setdefault(doc["split"], set()).add(doc["doc_id"])
    out: dict[str, list[dict[str, Any]]] = {}
    for split in ("train", "val", "test"):
        rows = read_jsonl(project_root / "exports" / "ground_truth" / f"{split}.jsonl")
        out[split] = [row for row in rows if row.get("doc_id") in keep_by_split.get(split, set())]
    out["all"] = out["train"] + out["val"] + out["test"]
    return out


def compact_metrics(report: dict[str, Any]) -> dict[str, Any]:
    overall = report["overall"]
    gt_instances = float(overall.get("gt_instances") or 0.0)
    exact = float(overall.get("exact_instance_accuracy") or 0.0)
    return {
        "docs": report["doc_count"],
        "precision": overall["precision"],
        "recall": overall["recall"],
        "f1": overall["f1"],
        "exact_instance_accuracy": exact,
        "errors": int(round(gt_instances * (1.0 - exact))),
        "missing_word_rate": overall["missing_word_rate"],
        "extra_word_rate": overall["extra_word_rate"],
        "bbox_iou_avg": overall["bbox_iou_avg"],
        "bbox_iou80_recall": overall["bbox_iou80_recall"],
        "gt_instances": overall["gt_instances"],
        "pred_instances": overall["pred_instances"],
        "field_metrics": report["field_metrics"],
    }


def timing_summary(doc_reports: list[dict[str, Any]], wall_seconds: float) -> dict[str, Any]:
    pages = sum(item["pages"] for item in doc_reports)
    candidates = sum(item["candidate_count"] for item in doc_reports)
    total_sum = sum(item["seconds"]["total"] for item in doc_reports)
    model_sum = sum(item["seconds"]["model_score"] for item in doc_reports)
    return {
        "docs": len(doc_reports),
        "pages": pages,
        "candidate_count": candidates,
        "wall_seconds": wall_seconds,
        "wall_ms_per_page": wall_seconds * 1000.0 / pages if pages else 0.0,
        "sum_doc_seconds": total_sum,
        "sum_doc_ms_per_page": total_sum * 1000.0 / pages if pages else 0.0,
        "model_score_seconds_sum": model_sum,
        "model_score_ms_per_page_sum": model_sum * 1000.0 / pages if pages else 0.0,
    }


def main() -> None:
    args = parse_args()
    project_root = Path(args.project_root)
    manifest = read_json(project_root / "manifest.json")
    docs = batch_docs(manifest, args.label_rel_prefix)
    if not docs:
        raise SystemExit(f"No docs found for {args.label_rel_prefix}")
    models_dir = project_root / "models" / "fieldwise"
    thresholds = read_json(models_dir / "thresholds.json", default={})

    workers = max(1, int(args.workers))
    warmup_start = time.perf_counter()
    if workers == 1:
        init_worker(str(models_dir), args.worker_omp_threads)
        ready_pids = [worker_ready(0)]
        warmup_seconds = time.perf_counter() - warmup_start
        start = time.perf_counter()
        results = [predict_doc(doc) for doc in docs]
        wall_seconds = time.perf_counter() - start
    else:
        with ProcessPoolExecutor(
            max_workers=workers,
            initializer=init_worker,
            initargs=(str(models_dir), args.worker_omp_threads),
        ) as executor:
            ready_pids = list(executor.map(worker_ready, range(workers), chunksize=1))
            warmup_seconds = time.perf_counter() - warmup_start
            start = time.perf_counter()
            results = list(executor.map(predict_doc, docs, chunksize=1))
            wall_seconds = time.perf_counter() - start

    doc_reports = [{key: value for key, value in item.items() if key != "predictions"} for item in results]
    scored_by_doc = {item["doc_id"]: item["predictions"] for item in results}
    gt_rows_by_split = load_gt_rows(project_root, docs)
    split_reports = {}
    for split in ("all", "train", "val", "test"):
        split_reports[split] = compact_metrics(_evaluate_scored_candidates(gt_rows_by_split[split], scored_by_doc, thresholds, split))
        split_doc_ids = {row["doc_id"] for row in gt_rows_by_split[split]}
        split_docs = [item for item in doc_reports if item["doc_id"] in split_doc_ids]
        split_reports[split]["timing"] = timing_summary(
            split_docs,
            wall_seconds * (sum(item["pages"] for item in split_docs) / max(sum(item["pages"] for item in doc_reports), 1)),
        )

    report = {
        "method": "lightgbm",
        "project_root": str(project_root.resolve()),
        "label_rel_prefix": normalized_prefix(args.label_rel_prefix),
        "workers": workers,
        "worker_omp_threads": args.worker_omp_threads,
        "ready_pids": ready_pids,
        "worker_warmup_seconds": warmup_seconds,
        "timing": timing_summary(doc_reports, wall_seconds),
        "splits": split_reports,
        "documents": doc_reports,
    }
    write_json(args.output, report)
    print(json.dumps({k: v for k, v in report.items() if k != "documents"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
