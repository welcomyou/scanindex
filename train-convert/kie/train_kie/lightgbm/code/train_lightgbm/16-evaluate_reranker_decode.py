from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from train_lightgbm.common import build_paths, read_json, write_json
from train_lightgbm.config import LABELS
from train_lightgbm.reranker_common import RERANK_FIELDS, candidate_payload, query_for
from train_lightgbm.schema_decoder import CandidatePrediction
from train_lightgbm.training import (
    _evaluate_scored_candidates,
    _load_scored_candidates,
    _score_threshold_grid,
)


def _configure_threads(threads: int, interop_threads: int) -> dict:
    if threads <= 0:
        threads = os.cpu_count() or 1
    if interop_threads <= 0:
        interop_threads = min(4, threads)
    for key in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[key] = str(threads)
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    import torch

    torch.set_num_threads(threads)
    torch.set_num_interop_threads(interop_threads)
    return {"threads": torch.get_num_threads(), "interop_threads": torch.get_num_interop_threads()}


class OnnxCrossEncoder:
    def __init__(self, onnx_path: str, tokenizer_path: str, max_length: int, threads: int, interop_threads: int):
        import onnxruntime as ort
        from transformers import AutoTokenizer

        self.tokenizer = AutoTokenizer.from_pretrained(tokenizer_path)
        self.max_length = max_length
        options = ort.SessionOptions()
        if threads > 0:
            options.intra_op_num_threads = threads
        if interop_threads > 0:
            options.inter_op_num_threads = interop_threads
        self.session = ort.InferenceSession(str(onnx_path), sess_options=options, providers=["CPUExecutionProvider"])
        self.input_names = {item.name for item in self.session.get_inputs()}
        self.output_name = self.session.get_outputs()[0].name

    def predict(self, pairs: list[tuple[str, str]], batch_size: int, show_progress_bar: bool = False) -> list[float]:
        import numpy as np

        scores: list[float] = []
        for start in range(0, len(pairs), batch_size):
            batch = pairs[start : start + batch_size]
            encoded = self.tokenizer(
                [item[0] for item in batch],
                [item[1] for item in batch],
                padding=True,
                truncation=True,
                max_length=self.max_length,
                return_tensors="np",
            )
            inputs = {key: value.astype(np.int64) for key, value in encoded.items() if key in self.input_names}
            logits = self.session.run([self.output_name], inputs)[0].reshape(-1)
            scores.extend(float(value) for value in logits.tolist())
        return scores


def _load_reranker(args: argparse.Namespace):
    if args.onnx_path:
        return OnnxCrossEncoder(args.onnx_path, args.tokenizer_path or args.model_path, args.max_length, args.threads, args.interop_threads)
    from sentence_transformers import CrossEncoder

    return CrossEncoder(args.model_path, max_length=args.max_length, device=args.device)


def _predict_scores(model, pairs: list[tuple[str, str]], batch_size: int) -> list[float]:
    scores = model.predict(pairs, batch_size=batch_size, show_progress_bar=False)
    if hasattr(scores, "tolist"):
        scores = scores.tolist()
    out = []
    for score in scores:
        if isinstance(score, (list, tuple)):
            score = score[0]
        out.append(float(score))
    return out


def _candidate_doc(candidate: CandidatePrediction) -> str:
    return candidate_payload(
        field=candidate.field,
        text=candidate.text,
        candidate_id=candidate.candidate_id,
        page_index=candidate.page_index,
        bbox=candidate.bbox,
        line_ids=candidate.line_ids,
        word_ids=candidate.word_ids,
        lgbm_score=candidate.score,
    )


def _score_reranker_split(
    model,
    scored_by_doc: dict[str, dict[str, list[CandidatePrediction]]],
    fields: list[str],
    top_k: int,
    batch_size: int,
) -> tuple[dict[tuple[str, str, str], float], dict]:
    pairs: list[tuple[str, str]] = []
    keys: list[tuple[str, str, str]] = []
    counts = {field: 0 for field in fields}
    for doc_id, field_map in scored_by_doc.items():
        for field in fields:
            candidates = sorted(field_map.get(field, []), key=lambda item: item.score, reverse=True)[:top_k]
            query = query_for(field)
            for cand in candidates:
                pairs.append((query, _candidate_doc(cand)))
                keys.append((doc_id, field, cand.candidate_id))
                counts[field] += 1
    t0 = time.perf_counter()
    scores = _predict_scores(model, pairs, batch_size=batch_size) if pairs else []
    elapsed = time.perf_counter() - t0
    return dict(zip(keys, scores)), {
        "pairs": len(pairs),
        "elapsed_sec": elapsed,
        "pairs_per_sec": len(pairs) / max(elapsed, 1e-9),
        "counts_by_field": counts,
    }


def _apply_reranker_scores(
    scored_by_doc: dict[str, dict[str, list[CandidatePrediction]]],
    rerank_scores: dict[tuple[str, str, str], float],
    fields: set[str],
    *,
    alpha: float,
    mode: str,
    demote_unreranked: bool,
) -> dict[str, dict[str, list[CandidatePrediction]]]:
    adjusted: dict[str, dict[str, list[CandidatePrediction]]] = {}
    for doc_id, field_map in scored_by_doc.items():
        adjusted[doc_id] = {}
        for field, candidates in field_map.items():
            out: list[CandidatePrediction] = []
            for cand in candidates:
                key = (doc_id, field, cand.candidate_id)
                if field in fields:
                    if key in rerank_scores:
                        ce_score = rerank_scores[key]
                        if mode == "ce_only":
                            score = ce_score
                        elif mode == "fusion":
                            score = float(cand.score + alpha * ce_score)
                        else:
                            raise ValueError(f"Unknown mode: {mode}")
                    elif demote_unreranked:
                        score = -1e6
                    else:
                        score = cand.score
                else:
                    score = cand.score
                out.append(replace(cand, score=float(score)))
            adjusted[doc_id][field] = out
    return adjusted


def _objective(report: dict, objective: str) -> float:
    overall = report["overall"]
    if objective == "exact":
        return (
            overall.get("exact_instance_accuracy", 0.0)
            + 0.05 * overall.get("f1", 0.0)
            + 0.03 * overall.get("bbox_iou80_recall", 0.0)
            - 0.03 * overall.get("missing_word_rate", 0.0)
            - 0.03 * overall.get("extra_word_rate", 0.0)
        )
    return (
        overall.get("f1", 0.0)
        + 0.20 * overall.get("exact_instance_accuracy", 0.0)
        + 0.10 * overall.get("bbox_iou80_recall", 0.0)
        - 0.10 * overall.get("missing_word_rate", 0.0)
        - 0.10 * overall.get("extra_word_rate", 0.0)
    )


def _tune_thresholds(
    gt_rows: list[dict],
    scored_by_doc: dict[str, dict[str, list[CandidatePrediction]]],
    base_thresholds: dict[str, float],
    tune_fields: list[str],
    max_passes: int,
    objective: str,
) -> tuple[dict[str, float], dict, list[dict]]:
    thresholds = {field: float(base_thresholds.get(field, 0.5)) for field in LABELS}
    grids = {
        field: _score_threshold_grid(cand.score for doc_fields in scored_by_doc.values() for cand in doc_fields.get(field, []))
        for field in tune_fields
    }
    best_report = _evaluate_scored_candidates(gt_rows, scored_by_doc, thresholds, "val")
    best_objective = _objective(best_report, objective)
    trials: list[dict] = []
    for pass_index in range(max_passes):
        improved = False
        for field in tune_fields:
            print(f"[reranker_decode] tune pass={pass_index + 1}/{max_passes} field={field}", flush=True)
            field_best_threshold = thresholds[field]
            field_best_report = best_report
            field_best_objective = best_objective
            for threshold in grids[field]:
                trial = dict(thresholds)
                trial[field] = threshold
                report = _evaluate_scored_candidates(gt_rows, scored_by_doc, trial, "val")
                value = _objective(report, objective)
                if value > field_best_objective + 1e-9:
                    field_best_threshold = threshold
                    field_best_report = report
                    field_best_objective = value
            if field_best_threshold != thresholds[field]:
                thresholds[field] = field_best_threshold
                best_report = field_best_report
                best_objective = field_best_objective
                improved = True
            trials.append(
                {
                    "pass": pass_index + 1,
                    "field": field,
                    "threshold": thresholds[field],
                    "objective": field_best_objective,
                    "val_f1": field_best_report["overall"]["f1"],
                    "val_exact_instance_accuracy": field_best_report["overall"]["exact_instance_accuracy"],
                }
            )
        if not improved:
            break
    return thresholds, best_report, trials


def _delta_metrics(new: dict, base: dict) -> dict:
    keys = ["f1", "precision", "recall", "exact_instance_accuracy", "bbox_iou80_recall", "missing_word_rate", "extra_word_rate"]
    return {key: float(new["overall"].get(key, 0.0) - base["overall"].get(key, 0.0)) for key in keys}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate final LightGBM decode after applying a CrossEncoder reranker.")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--onnx-path", default=None)
    parser.add_argument("--tokenizer-path", default=None)
    parser.add_argument("--output-name", default="reranker_decode_eval")
    parser.add_argument("--fields", nargs="+", default=RERANK_FIELDS)
    parser.add_argument("--splits", nargs="+", default=["train", "val", "test"], choices=["train", "val", "test"])
    parser.add_argument("--top-k", type=int, default=24)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--max-length", type=int, default=384)
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--interop-threads", type=int, default=4)
    parser.add_argument("--alphas", nargs="+", type=float, default=[0.0, 0.1, 0.25, 0.5, 1.0, 2.0, 4.0])
    parser.add_argument("--modes", nargs="+", default=["fusion", "ce_only"], choices=["fusion", "ce_only"])
    parser.add_argument("--objective", default="exact", choices=["exact", "balanced"])
    parser.add_argument("--max-passes", type=int, default=1)
    parser.add_argument("--keep-unreranked", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    thread_info = _configure_threads(args.threads, args.interop_threads)
    paths = build_paths(args.project_root)
    models_dir = paths.models_root / "fieldwise"
    base_thresholds = read_json(models_dir / "thresholds.json", default={})
    fields = [field for field in args.fields if field in LABELS]
    model = _load_reranker(args)
    report: dict = {
        "project_root": str(paths.root),
        "model_path": args.model_path,
        "onnx_path": args.onnx_path,
        "runtime": "onnxruntime" if args.onnx_path else "sentence-transformers",
        "thread_info": thread_info,
        "fields": fields,
        "top_k": args.top_k,
        "objective": args.objective,
        "variants": [],
    }

    split_data = {}
    for split in args.splits:
        print(f"[reranker_decode] loading split={split}", flush=True)
        gt_rows, scored_by_doc = _load_scored_candidates(paths.root, models_dir, split)
        baseline = _evaluate_scored_candidates(gt_rows, scored_by_doc, base_thresholds, split)
        print(f"[reranker_decode] scoring reranker split={split}", flush=True)
        rerank_scores, score_info = _score_reranker_split(model, scored_by_doc, fields, args.top_k, args.batch_size)
        split_data[split] = {
            "gt_rows": gt_rows,
            "scored_by_doc": scored_by_doc,
            "baseline": baseline,
            "rerank_scores": rerank_scores,
            "score_info": score_info,
        }
        report.setdefault("baseline", {})[split] = baseline
        report.setdefault("score_info", {})[split] = score_info

    if "val" not in split_data:
        raise RuntimeError("Validation split is required for selecting reranker variant.")

    for mode in args.modes:
        alpha_values = [0.0] if mode == "ce_only" else args.alphas
        for alpha in alpha_values:
            print(f"[reranker_decode] evaluating mode={mode} alpha={alpha}", flush=True)
            adjusted_val = _apply_reranker_scores(
                split_data["val"]["scored_by_doc"],
                split_data["val"]["rerank_scores"],
                set(fields),
                alpha=alpha,
                mode=mode,
                demote_unreranked=not args.keep_unreranked,
            )
            thresholds, val_report, trials = _tune_thresholds(
                split_data["val"]["gt_rows"],
                adjusted_val,
                base_thresholds,
                fields,
                args.max_passes,
                args.objective,
            )
            variant = {
                "mode": mode,
                "alpha": alpha,
                "thresholds": thresholds,
                "val_objective": _objective(val_report, args.objective),
                "threshold_trials": trials,
                "splits": {},
                "deltas_vs_baseline": {},
            }
            for split in args.splits:
                adjusted = _apply_reranker_scores(
                    split_data[split]["scored_by_doc"],
                    split_data[split]["rerank_scores"],
                    set(fields),
                    alpha=alpha,
                    mode=mode,
                    demote_unreranked=not args.keep_unreranked,
                )
                split_report = _evaluate_scored_candidates(split_data[split]["gt_rows"], adjusted, thresholds, split)
                variant["splits"][split] = split_report
                variant["deltas_vs_baseline"][split] = _delta_metrics(split_report, split_data[split]["baseline"])
            report["variants"].append(variant)

    report["best_by_val_exact"] = max(report["variants"], key=lambda item: item["splits"]["val"]["overall"]["exact_instance_accuracy"])
    report["best_by_val_objective"] = max(report["variants"], key=lambda item: item["val_objective"])
    output_path = paths.reports_root / f"{args.output_name}.json"
    write_json(output_path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"Wrote {output_path}")


if __name__ == "__main__":
    main()
