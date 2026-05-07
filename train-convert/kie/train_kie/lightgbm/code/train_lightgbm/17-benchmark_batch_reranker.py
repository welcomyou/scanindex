from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from dataclasses import replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from train_lightgbm.common import build_paths, read_json, read_jsonl, write_json
from train_lightgbm.config import LABELS
from train_lightgbm.reranker_common import RERANK_FIELDS, candidate_payload, query_for
from train_lightgbm.schema_decoder import CandidatePrediction
from train_lightgbm.training import (
    _aggregate_word_f1,
    _evaluate_scored_candidates,
    _row_to_prediction,
    load_models,
    score_rows,
)


class OnnxCrossEncoder:
    def __init__(self, onnx_path: str | Path, tokenizer_path: str | Path, max_length: int, threads: int, interop_threads: int):
        import onnxruntime as ort
        from transformers import AutoTokenizer

        options = ort.SessionOptions()
        options.intra_op_num_threads = max(1, threads)
        options.inter_op_num_threads = max(1, interop_threads)
        self.session = ort.InferenceSession(str(onnx_path), sess_options=options, providers=["CPUExecutionProvider"])
        self.tokenizer = AutoTokenizer.from_pretrained(str(tokenizer_path))
        self.max_length = max_length
        self.input_names = {item.name for item in self.session.get_inputs()}
        self.output_name = self.session.get_outputs()[0].name

    def predict(self, pairs: list[tuple[str, str]], batch_size: int) -> list[float]:
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


def _configure_threads(threads: int, interop_threads: int) -> dict:
    if threads <= 0:
        threads = os.cpu_count() or 1
    if interop_threads <= 0:
        interop_threads = min(4, threads)
    for key in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[key] = str(threads)
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    return {"threads": threads, "interop_threads": interop_threads}


def _batch_doc_ids(manifest_path: Path, batch_name: str) -> dict[str, dict]:
    manifest = read_json(manifest_path)
    out: dict[str, dict] = {}
    needle = f"/{batch_name}/"
    for doc in manifest.get("documents", []):
        label_output = str(doc.get("label_output_json") or "").replace("\\", "/")
        label_input = str(doc.get("label_input_json") or "").replace("\\", "/")
        if needle in label_output or needle in label_input:
            out[str(doc["doc_id"])] = doc
    return out


def _load_scored_candidates_for_docs(
    project_root: Path,
    models_dir: Path,
    split: str,
    wanted_doc_ids: set[str],
) -> tuple[list[dict], dict[str, dict[str, list[CandidatePrediction]]], dict]:
    export_root = project_root / "exports"
    gt_rows = [row for row in read_jsonl(export_root / "ground_truth" / f"{split}.jsonl") if row["doc_id"] in wanted_doc_ids]
    scored_by_doc: dict[str, dict[str, list[CandidatePrediction]]] = {row["doc_id"]: {} for row in gt_rows}
    if not gt_rows:
        return [], {}, {"candidate_rows": 0, "rows_by_field": {}}

    models = load_models(models_dir)
    feature_names_by_field = {field: read_json(models_dir / f"{field}.meta.json")["feature_names"] for field in LABELS}
    rows_by_field: dict[str, int] = {}
    total_rows = 0
    for field in LABELS:
        rows = [
            row
            for row in read_jsonl(export_root / "fieldwise" / field / f"{split}.jsonl")
            if row["doc_id"] in wanted_doc_ids
        ]
        rows_by_field[field] = len(rows)
        total_rows += len(rows)
        scores = score_rows(models[field], rows, feature_names_by_field[field])
        grouped: dict[str, list[CandidatePrediction]] = defaultdict(list)
        for row, score in zip(rows, scores):
            grouped[row["doc_id"]].append(_row_to_prediction(field, row, score))
        for doc_id in scored_by_doc:
            scored_by_doc[doc_id][field] = grouped.get(doc_id, [])
    return gt_rows, scored_by_doc, {"candidate_rows": total_rows, "rows_by_field": rows_by_field}


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


def _score_reranker(
    model: OnnxCrossEncoder,
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
            query = query_for(field)
            candidates = sorted(field_map.get(field, []), key=lambda item: item.score, reverse=True)[:top_k]
            for cand in candidates:
                keys.append((doc_id, field, cand.candidate_id))
                pairs.append((query, _candidate_doc(cand)))
                counts[field] += 1
    t0 = time.perf_counter()
    scores = model.predict(pairs, batch_size=batch_size) if pairs else []
    elapsed = time.perf_counter() - t0
    return dict(zip(keys, scores)), {
        "pairs": len(pairs),
        "elapsed_sec": elapsed,
        "pairs_per_sec": len(pairs) / max(elapsed, 1e-9),
        "counts_by_field": counts,
    }


def _apply_fusion(
    scored_by_doc: dict[str, dict[str, list[CandidatePrediction]]],
    rerank_scores: dict[tuple[str, str, str], float],
    fields: set[str],
    alpha: float,
) -> dict[str, dict[str, list[CandidatePrediction]]]:
    adjusted: dict[str, dict[str, list[CandidatePrediction]]] = {}
    for doc_id, field_map in scored_by_doc.items():
        adjusted[doc_id] = {}
        for field, candidates in field_map.items():
            out: list[CandidatePrediction] = []
            for cand in candidates:
                key = (doc_id, field, cand.candidate_id)
                score = cand.score + alpha * rerank_scores[key] if field in fields and key in rerank_scores else cand.score
                out.append(replace(cand, score=float(score)))
            adjusted[doc_id][field] = out
    return adjusted


def _combine_reports(split_reports: dict[str, dict]) -> dict:
    field_metrics: dict[str, dict] = {}
    for field in LABELS:
        stats = []
        for report in split_reports.values():
            metric = report["field_metrics"][field]
            stats.append(
                {
                    "tp_words": metric["tp_words"],
                    "pred_words": metric["pred_words"],
                    "gt_words": metric["gt_words"],
                    "exact_match": metric["exact_instance_accuracy"] * metric["gt_instances"],
                    "gt_instances": metric["gt_instances"],
                    "pred_instances": metric["pred_instances"],
                    "matched_instances": metric["matched_instances"],
                    "missing_words": metric["missing_word_rate"] * metric["gt_words"],
                    "extra_words": metric["extra_word_rate"] * metric["pred_words"],
                    "bbox_iou_sum": metric["bbox_iou_avg"] * metric["matched_instances"],
                    "bbox_iou_count": metric["matched_instances"],
                    "bbox_iou80": metric["bbox_iou80_recall"] * metric["gt_instances"],
                }
            )
        field_metrics[field] = _aggregate_word_f1(stats)
    overall = _aggregate_word_f1(metric for field in field_metrics.values() for metric in [{
        "tp_words": field["tp_words"],
        "pred_words": field["pred_words"],
        "gt_words": field["gt_words"],
        "exact_match": field["exact_instance_accuracy"] * field["gt_instances"],
        "gt_instances": field["gt_instances"],
        "pred_instances": field["pred_instances"],
        "matched_instances": field["matched_instances"],
        "missing_words": field["missing_word_rate"] * field["gt_words"],
        "extra_words": field["extra_word_rate"] * field["pred_words"],
        "bbox_iou_sum": field["bbox_iou_avg"] * field["matched_instances"],
        "bbox_iou_count": field["matched_instances"],
        "bbox_iou80": field["bbox_iou80_recall"] * field["gt_instances"],
    }])
    return {"overall": overall, "field_metrics": field_metrics, "doc_count": sum(r["doc_count"] for r in split_reports.values())}


def _metric_delta(new: dict, base: dict) -> dict:
    keys = ["f1", "precision", "recall", "exact_instance_accuracy", "bbox_iou80_recall", "missing_word_rate", "extra_word_rate"]
    return {key: float(new["overall"][key] - base["overall"][key]) for key in keys}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark LightGBM vs LightGBM + ONNX reranker for one labeled batch.")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--batch-name", default="batch_0027")
    parser.add_argument("--onnx-path", required=True)
    parser.add_argument("--tokenizer-path", required=True)
    parser.add_argument("--output-name", default="batch27_reranker_benchmark")
    parser.add_argument("--fields", nargs="+", default=RERANK_FIELDS)
    parser.add_argument("--top-k", type=int, default=24)
    parser.add_argument("--alpha", type=float, default=1.0)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--max-length", type=int, default=384)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--interop-threads", type=int, default=4)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    thread_info = _configure_threads(args.threads, args.interop_threads)
    paths = build_paths(args.project_root)
    models_dir = paths.models_root / "fieldwise"
    thresholds = read_json(models_dir / "thresholds.json")
    batch_docs = _batch_doc_ids(paths.root / "manifest.json", args.batch_name)
    if not batch_docs:
        raise RuntimeError(f"No docs found for {args.batch_name}")
    fields = [field for field in args.fields if field in LABELS]
    model = OnnxCrossEncoder(args.onnx_path, args.tokenizer_path, args.max_length, args.threads, args.interop_threads)

    report: dict = {
        "project_root": str(paths.root),
        "batch_name": args.batch_name,
        "doc_count": len(batch_docs),
        "split_counts": {},
        "thread_info": thread_info,
        "onnx_path": str(args.onnx_path),
        "tokenizer_path": str(args.tokenizer_path),
        "fields": fields,
        "top_k": args.top_k,
        "alpha": args.alpha,
        "splits": {},
    }
    baseline_reports: dict[str, dict] = {}
    reranked_reports: dict[str, dict] = {}
    total_timing = {
        "load_lgbm_score_sec": 0.0,
        "baseline_decode_sec": 0.0,
        "reranker_score_sec": 0.0,
        "reranked_decode_sec": 0.0,
        "candidate_rows": 0,
        "reranker_pairs": 0,
    }

    for split in ("train", "val", "test"):
        wanted = {doc_id for doc_id, doc in batch_docs.items() if doc.get("split") == split}
        report["split_counts"][split] = len(wanted)
        if not wanted:
            continue
        print(f"[batch_benchmark] split={split} docs={len(wanted)}", flush=True)
        t0 = time.perf_counter()
        gt_rows, scored_by_doc, candidate_info = _load_scored_candidates_for_docs(paths.root, models_dir, split, wanted)
        load_lgbm_score_sec = time.perf_counter() - t0
        t0 = time.perf_counter()
        baseline = _evaluate_scored_candidates(gt_rows, scored_by_doc, thresholds, split)
        baseline_decode_sec = time.perf_counter() - t0
        rerank_scores, rerank_info = _score_reranker(model, scored_by_doc, fields, args.top_k, args.batch_size)
        adjusted = _apply_fusion(scored_by_doc, rerank_scores, set(fields), args.alpha)
        t0 = time.perf_counter()
        reranked = _evaluate_scored_candidates(gt_rows, adjusted, thresholds, split)
        reranked_decode_sec = time.perf_counter() - t0

        baseline_reports[split] = baseline
        reranked_reports[split] = reranked
        timing = {
            "load_lgbm_score_sec": load_lgbm_score_sec,
            "baseline_decode_sec": baseline_decode_sec,
            "reranker_score_sec": rerank_info["elapsed_sec"],
            "reranked_decode_sec": reranked_decode_sec,
            "baseline_total_sec": load_lgbm_score_sec + baseline_decode_sec,
            "reranked_total_sec": load_lgbm_score_sec + rerank_info["elapsed_sec"] + reranked_decode_sec,
            "candidate_rows": candidate_info["candidate_rows"],
            "reranker_pairs": rerank_info["pairs"],
        }
        for key in total_timing:
            total_timing[key] += timing[key]
        report["splits"][split] = {
            "doc_count": len(gt_rows),
            "candidate_info": candidate_info,
            "reranker_info": rerank_info,
            "timing": timing,
            "baseline": baseline,
            "reranked": reranked,
            "delta": _metric_delta(reranked, baseline),
        }

    baseline_all = _combine_reports(baseline_reports)
    reranked_all = _combine_reports(reranked_reports)
    total_timing["baseline_total_sec"] = total_timing["load_lgbm_score_sec"] + total_timing["baseline_decode_sec"]
    total_timing["reranked_total_sec"] = (
        total_timing["load_lgbm_score_sec"] + total_timing["reranker_score_sec"] + total_timing["reranked_decode_sec"]
    )
    total_timing["reranker_overhead_sec"] = total_timing["reranked_total_sec"] - total_timing["baseline_total_sec"]
    total_timing["baseline_ms_per_doc"] = 1000.0 * total_timing["baseline_total_sec"] / max(len(batch_docs), 1)
    total_timing["reranked_ms_per_doc"] = 1000.0 * total_timing["reranked_total_sec"] / max(len(batch_docs), 1)
    total_timing["reranker_overhead_ms_per_doc"] = 1000.0 * total_timing["reranker_overhead_sec"] / max(len(batch_docs), 1)
    total_timing["reranker_ms_per_pair"] = 1000.0 * total_timing["reranker_score_sec"] / max(total_timing["reranker_pairs"], 1)
    report["overall"] = {
        "baseline": baseline_all,
        "reranked": reranked_all,
        "delta": _metric_delta(reranked_all, baseline_all),
    }
    report["timing_total"] = total_timing
    output_path = paths.reports_root / f"{args.output_name}.json"
    write_json(output_path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"Wrote {output_path}")


if __name__ == "__main__":
    main()
