from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Iterable

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from train_lightgbm.common import build_paths, read_json, read_jsonl, write_json
from train_lightgbm.config import LABELS
from train_lightgbm.schema_decoder import CandidatePrediction
from train_lightgbm.training import (
    _evaluate_scored_candidates,
    _load_scored_candidates,
    _score_threshold_grid,
    _threshold_objective,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate semantic reranking on top LightGBM candidates.")
    parser.add_argument("--project-root", required=True, help="LightGBM project root.")
    parser.add_argument(
        "--model-name",
        default="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        help="SentenceTransformer model name.",
    )
    parser.add_argument("--top-k", type=int, default=30, help="Top candidates per doc/field to semantic-score.")
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument(
        "--alphas",
        nargs="+",
        type=float,
        default=[0.0, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0],
        help="Semantic margin multipliers to test.",
    )
    parser.add_argument("--max-passes", type=int, default=1, help="Threshold tuning coordinate passes per alpha.")
    return parser.parse_args()


def _clean_text(text: str) -> str:
    return " ".join((text or "").replace("\n", " ").split())


def _load_field_texts(project_root: Path) -> dict[str, list[str]]:
    texts = {field: [] for field in LABELS}
    for row in read_jsonl(project_root / "exports" / "ground_truth" / "train.jsonl"):
        for inst in row.get("field_instances", []):
            label = inst.get("label")
            text = _clean_text(inst.get("text", ""))
            if label in texts and text:
                texts[label].append(text)
    return texts


def _encode_unique(model, texts: Iterable[str], batch_size: int) -> dict[str, np.ndarray]:
    unique = sorted({text for text in texts if text})
    if not unique:
        return {}
    vectors = model.encode(
        unique,
        batch_size=batch_size,
        normalize_embeddings=True,
        show_progress_bar=True,
    )
    return {text: np.asarray(vector, dtype=np.float32) for text, vector in zip(unique, vectors)}


def _build_prototypes(model, field_texts: dict[str, list[str]], batch_size: int) -> dict[str, np.ndarray]:
    encoded = _encode_unique(model, (text for texts in field_texts.values() for text in texts), batch_size)
    prototypes: dict[str, np.ndarray] = {}
    for field, texts in field_texts.items():
        vectors = [encoded[text] for text in texts if text in encoded]
        if not vectors:
            continue
        proto = np.mean(np.stack(vectors, axis=0), axis=0)
        norm = np.linalg.norm(proto)
        prototypes[field] = proto / norm if norm else proto
    return prototypes


def _top_candidate_texts(scored_by_doc: dict[str, dict[str, list[CandidatePrediction]]], top_k: int) -> list[str]:
    texts: list[str] = []
    for field_map in scored_by_doc.values():
        for field in LABELS:
            candidates = sorted(field_map.get(field, []), key=lambda item: item.score, reverse=True)[:top_k]
            for cand in candidates:
                text = _clean_text(cand.text)
                if text:
                    texts.append(text)
    return texts


def _semantic_margins(
    scored_by_doc: dict[str, dict[str, list[CandidatePrediction]]],
    encoded_texts: dict[str, np.ndarray],
    prototypes: dict[str, np.ndarray],
    top_k: int,
) -> dict[str, float]:
    margins: dict[str, float] = {}
    proto_items = list(prototypes.items())
    for field_map in scored_by_doc.values():
        for field in LABELS:
            own_proto = prototypes.get(field)
            if own_proto is None:
                continue
            candidates = sorted(field_map.get(field, []), key=lambda item: item.score, reverse=True)[:top_k]
            for cand in candidates:
                text = _clean_text(cand.text)
                vector = encoded_texts.get(text)
                if vector is None:
                    continue
                own = float(np.dot(vector, own_proto))
                other = max(
                    (float(np.dot(vector, proto)) for other_field, proto in proto_items if other_field != field),
                    default=0.0,
                )
                margins[cand.candidate_id] = own - other
    return margins


def _apply_semantic_margin(
    scored_by_doc: dict[str, dict[str, list[CandidatePrediction]]],
    margins: dict[str, float],
    alpha: float,
) -> dict[str, dict[str, list[CandidatePrediction]]]:
    adjusted: dict[str, dict[str, list[CandidatePrediction]]] = {}
    for doc_id, field_map in scored_by_doc.items():
        adjusted[doc_id] = {}
        for field, candidates in field_map.items():
            adjusted[doc_id][field] = [
                replace(cand, score=float(cand.score + alpha * margins.get(cand.candidate_id, 0.0)))
                for cand in candidates
            ]
    return adjusted


def _tune_thresholds(
    gt_rows: list[dict],
    scored_by_doc: dict[str, dict[str, list[CandidatePrediction]]],
    base_thresholds: dict[str, float],
    max_passes: int,
) -> tuple[dict[str, float], dict]:
    thresholds = {field: float(base_thresholds.get(field, 0.5)) for field in LABELS}
    grids = {
        field: _score_threshold_grid(cand.score for doc_fields in scored_by_doc.values() for cand in doc_fields.get(field, []))
        for field in LABELS
    }
    best_report = _evaluate_scored_candidates(gt_rows, scored_by_doc, thresholds, "val")
    best_objective = _threshold_objective(best_report["overall"])
    for _ in range(max_passes):
        improved = False
        for field in LABELS:
            field_threshold = thresholds[field]
            field_report = best_report
            field_objective = best_objective
            for threshold in grids[field]:
                trial = dict(thresholds)
                trial[field] = threshold
                report = _evaluate_scored_candidates(gt_rows, scored_by_doc, trial, "val")
                objective = _threshold_objective(report["overall"])
                if objective > field_objective + 1e-9:
                    field_threshold = threshold
                    field_report = report
                    field_objective = objective
            if field_threshold != thresholds[field]:
                thresholds[field] = field_threshold
                best_report = field_report
                best_objective = field_objective
                improved = True
        if not improved:
            break
    return thresholds, best_report


def main() -> None:
    args = parse_args()
    project_root = Path(args.project_root).resolve()
    paths = build_paths(project_root)
    models_dir = paths.models_root / "fieldwise"
    base_thresholds = read_json(models_dir / "thresholds.json", default={})
    baseline_report = read_json(paths.reports_root / "eval_report.json", default={})

    from sentence_transformers import SentenceTransformer

    print(f"[semantic] loading model: {args.model_name}", flush=True)
    model = SentenceTransformer(args.model_name)
    print("[semantic] building field prototypes from train GT", flush=True)
    prototypes = _build_prototypes(model, _load_field_texts(project_root), args.batch_size)
    if set(prototypes) != set(LABELS):
        missing = sorted(set(LABELS) - set(prototypes))
        raise RuntimeError(f"Missing semantic prototypes for fields: {missing}")

    print("[semantic] loading LightGBM scored candidates", flush=True)
    gt_val, scored_val = _load_scored_candidates(project_root, models_dir, "val")
    gt_test, scored_test = _load_scored_candidates(project_root, models_dir, "test")

    print(f"[semantic] encoding top-{args.top_k} val/test candidate texts", flush=True)
    candidate_texts = _top_candidate_texts(scored_val, args.top_k) + _top_candidate_texts(scored_test, args.top_k)
    encoded_candidates = _encode_unique(model, candidate_texts, args.batch_size)
    val_margins = _semantic_margins(scored_val, encoded_candidates, prototypes, args.top_k)
    test_margins = _semantic_margins(scored_test, encoded_candidates, prototypes, args.top_k)

    alpha_reports = []
    best = None
    for alpha in args.alphas:
        print(f"[semantic] evaluating alpha={alpha}", flush=True)
        adjusted_val = _apply_semantic_margin(scored_val, val_margins, alpha)
        thresholds, val_report = _tune_thresholds(gt_val, adjusted_val, base_thresholds, max_passes=args.max_passes)
        adjusted_test = _apply_semantic_margin(scored_test, test_margins, alpha)
        test_report = _evaluate_scored_candidates(gt_test, adjusted_test, thresholds, "test")
        item = {
            "alpha": alpha,
            "thresholds": thresholds,
            "val": val_report,
            "test": test_report,
            "val_objective": _threshold_objective(val_report["overall"]),
            "test_objective": _threshold_objective(test_report["overall"]),
        }
        alpha_reports.append(item)
        if best is None or item["val_objective"] > best["val_objective"]:
            best = item

    report = {
        "model_name": args.model_name,
        "top_k": args.top_k,
        "alphas": args.alphas,
        "baseline_eval_report": str(paths.reports_root / "eval_report.json"),
        "baseline_test_overall": baseline_report.get("test", {}).get("overall"),
        "best_by_val": best,
        "alpha_reports": alpha_reports,
    }
    write_json(paths.reports_root / "semantic_rerank_report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
