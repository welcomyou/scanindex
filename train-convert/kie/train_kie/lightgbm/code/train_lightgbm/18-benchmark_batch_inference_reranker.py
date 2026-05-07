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
from train_lightgbm.dataset import _build_features, generate_candidates, load_field_instances, load_ocr_document
from train_lightgbm.reranker_common import RERANK_FIELDS, candidate_payload, query_for
from train_lightgbm.schema_decoder import CandidatePrediction, decode_document_predictions
from train_lightgbm.training import _aggregate_word_f1, _match_instances, load_models, score_rows


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


def _batch_docs(manifest_path: Path, batch_name: str) -> list[dict]:
    manifest = read_json(manifest_path)
    needle = f"/{batch_name}/"
    docs = []
    for doc in manifest.get("documents", []):
        label_output = str(doc.get("label_output_json") or "").replace("\\", "/")
        label_input = str(doc.get("label_input_json") or "").replace("\\", "/")
        if needle in label_output or needle in label_input:
            docs.append(doc)
    return docs


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


def _field_instances_to_gt(fields) -> dict[str, list[dict]]:
    gt_by_field = {field: [] for field in LABELS}
    for item in fields:
        if item.label not in gt_by_field:
            continue
        gt_by_field[item.label].append(
            {
                "field_id": item.field_id,
                "label": item.label,
                "page_index": item.page_index,
                "line_ids": list(item.line_ids),
                "word_ids": list(item.word_ids),
                "text": item.text,
                "bbox": list(item.bbox),
            }
        )
    return gt_by_field


def _decoded_to_pred_dict(decoded: dict[str, list[CandidatePrediction]]) -> dict[str, list[dict]]:
    out = {field: [] for field in LABELS}
    for field, preds in decoded.items():
        out[field] = [
            {
                "word_ids": pred.word_ids,
                "line_ids": pred.line_ids,
                "page_index": pred.page_index,
                "score": pred.score,
                "bbox": pred.bbox,
                "text": pred.text,
                "candidate_id": pred.candidate_id,
            }
            for pred in preds
        ]
    return out


def _evaluate_docs(doc_results: list[dict], key: str) -> dict:
    per_field_stats = {field: [] for field in LABELS}
    for result in doc_results:
        gt_by_field = result["gt_by_field"]
        pred_by_field = result[key]
        for field in LABELS:
            per_field_stats[field].append(_match_instances(pred_by_field.get(field, []), gt_by_field.get(field, [])))
    field_metrics = {field: _aggregate_word_f1(stats) for field, stats in per_field_stats.items()}
    overall = _aggregate_word_f1(metric for stats in per_field_stats.values() for metric in stats)
    return {"overall": overall, "field_metrics": field_metrics, "doc_count": len(doc_results)}


def _score_doc_lightgbm(doc_meta: dict, models: dict, feature_names_by_field: dict, thresholds: dict) -> tuple[dict, dict]:
    timing = {}
    t0 = time.perf_counter()
    doc = load_ocr_document(doc_meta)
    fields, relations = load_field_instances(doc, doc_meta["label_output_json"])
    timing["load_canonical_and_gt_sec"] = time.perf_counter() - t0

    decoded_input: dict[str, list[CandidatePrediction]] = {}
    candidate_count = 0
    model_score_sec = 0.0
    candidate_features_sec = 0.0
    for field, model in models.items():
        t0 = time.perf_counter()
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
        candidate_features_sec += time.perf_counter() - t0
        candidate_count += len(candidates)

        t0 = time.perf_counter()
        scores = score_rows(model, rows, feature_names_by_field[field])
        model_score_sec += time.perf_counter() - t0
        decoded_input[field] = [
            CandidatePrediction(
                field=field,
                score=float(score),
                page_index=cand.page_index,
                line_ids=list(cand.line_ids),
                word_ids=list(cand.word_ids),
                bbox=list(cand.bbox),
                text=cand.text,
                candidate_id=cand.candidate_id,
            )
            for cand, score in zip(candidates, scores)
        ]

    t0 = time.perf_counter()
    decoded = decode_document_predictions(decoded_input, thresholds)
    timing["baseline_decode_sec"] = time.perf_counter() - t0
    timing["candidate_and_features_sec"] = candidate_features_sec
    timing["model_score_sec"] = model_score_sec
    timing["candidate_count"] = candidate_count
    timing["selected_page_count"] = len(doc.selected_pages)
    timing["selected_pages"] = list(doc.selected_pages)
    timing["total_lightgbm_sec"] = (
        timing["load_canonical_and_gt_sec"]
        + timing["candidate_and_features_sec"]
        + timing["model_score_sec"]
        + timing["baseline_decode_sec"]
    )
    return {
        "doc_id": doc.doc_id,
        "relative_pdf_path": doc.relative_pdf_path,
        "split": doc.split,
        "selected_pages": list(doc.selected_pages),
        "page_count": len(doc.selected_pages),
        "candidate_count": candidate_count,
        "gt_by_field": _field_instances_to_gt(fields),
        "baseline_pred_by_field": _decoded_to_pred_dict(decoded),
        "decoded_input": decoded_input,
    }, timing


def _collect_reranker_pairs(doc_results: list[dict], fields: list[str], top_k: int) -> tuple[list[tuple[str, str]], list[tuple[int, str, str]]]:
    pairs: list[tuple[str, str]] = []
    keys: list[tuple[int, str, str]] = []
    for doc_index, result in enumerate(doc_results):
        for field in fields:
            query = query_for(field)
            candidates = sorted(result["decoded_input"].get(field, []), key=lambda item: item.score, reverse=True)[:top_k]
            for cand in candidates:
                keys.append((doc_index, field, cand.candidate_id))
                pairs.append((query, _candidate_doc(cand)))
    return pairs, keys


def _apply_reranker(
    doc_results: list[dict],
    reranker_scores: dict[tuple[int, str, str], float],
    fields: set[str],
    alpha: float,
    thresholds: dict,
) -> float:
    t0 = time.perf_counter()
    for doc_index, result in enumerate(doc_results):
        adjusted: dict[str, list[CandidatePrediction]] = {}
        for field, candidates in result["decoded_input"].items():
            out = []
            for cand in candidates:
                key = (doc_index, field, cand.candidate_id)
                score = cand.score + alpha * reranker_scores[key] if field in fields and key in reranker_scores else cand.score
                out.append(replace(cand, score=float(score)))
            adjusted[field] = out
        decoded = decode_document_predictions(adjusted, thresholds)
        result["reranked_pred_by_field"] = _decoded_to_pred_dict(decoded)
    return time.perf_counter() - t0


def _delta_metrics(new: dict, base: dict) -> dict:
    keys = ["f1", "precision", "recall", "exact_instance_accuracy", "bbox_iou80_recall", "missing_word_rate", "extra_word_rate"]
    return {key: float(new["overall"][key] - base["overall"][key]) for key in keys}


def _sum_timings(doc_timings: list[dict], key: str) -> float:
    return sum(float(item.get(key, 0.0)) for item in doc_timings)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark real LightGBM selected-page inference with optional ONNX reranker.")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--batch-name", default="batch_0027")
    parser.add_argument("--onnx-path", required=True)
    parser.add_argument("--tokenizer-path", required=True)
    parser.add_argument("--output-name", default="batch27_selected_pages_reranker_benchmark")
    parser.add_argument("--fields", nargs="+", default=RERANK_FIELDS)
    parser.add_argument("--top-k", type=int, default=24)
    parser.add_argument("--alpha", type=float, default=1.0)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--max-length", type=int, default=384)
    parser.add_argument("--threads", type=int, default=6)
    parser.add_argument("--interop-threads", type=int, default=2)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    thread_info = _configure_threads(args.threads, args.interop_threads)
    paths = build_paths(args.project_root)
    models_dir = paths.models_root / "fieldwise"
    models = load_models(models_dir)
    thresholds = read_json(models_dir / "thresholds.json", default={})
    feature_names_by_field = {field: read_json(models_dir / f"{field}.meta.json")["feature_names"] for field in LABELS}
    docs = _batch_docs(paths.manifest, args.batch_name)
    if not docs:
        raise RuntimeError(f"No docs found for {args.batch_name}")

    doc_results: list[dict] = []
    doc_timings: list[dict] = []
    t_all = time.perf_counter()
    for index, doc_meta in enumerate(docs, start=1):
        result, timing = _score_doc_lightgbm(doc_meta, models, feature_names_by_field, thresholds)
        doc_results.append(result)
        doc_timings.append(timing)
        if index % 10 == 0 or index == len(docs):
            elapsed = time.perf_counter() - t_all
            print(f"[selected_page_bench] LightGBM {index}/{len(docs)} docs elapsed={elapsed:.2f}s", flush=True)

    lightgbm_elapsed = time.perf_counter() - t_all
    fields = [field for field in args.fields if field in LABELS]
    reranker = OnnxCrossEncoder(args.onnx_path, args.tokenizer_path, args.max_length, args.threads, args.interop_threads)
    pairs, keys = _collect_reranker_pairs(doc_results, fields, args.top_k)
    t0 = time.perf_counter()
    scores = reranker.predict(pairs, batch_size=args.batch_size) if pairs else []
    reranker_score_sec = time.perf_counter() - t0
    reranker_scores = dict(zip(keys, scores))
    reranked_decode_sec = _apply_reranker(doc_results, reranker_scores, set(fields), args.alpha, thresholds)

    baseline_report = _evaluate_docs(doc_results, "baseline_pred_by_field")
    reranked_report = _evaluate_docs(doc_results, "reranked_pred_by_field")
    page_count = sum(result["page_count"] for result in doc_results)
    candidate_count = sum(result["candidate_count"] for result in doc_results)
    total_reranked_sec = lightgbm_elapsed + reranker_score_sec + reranked_decode_sec
    report = {
        "project_root": str(paths.root),
        "batch_name": args.batch_name,
        "doc_count": len(doc_results),
        "page_count_selected": page_count,
        "candidate_count": candidate_count,
        "thread_info": thread_info,
        "onnx_path": str(args.onnx_path),
        "tokenizer_path": str(args.tokenizer_path),
        "fields": fields,
        "top_k": args.top_k,
        "alpha": args.alpha,
        "reranker_pairs": len(pairs),
        "baseline": baseline_report,
        "reranked": reranked_report,
        "delta": _delta_metrics(reranked_report, baseline_report),
        "timing": {
            "lightgbm_selected_page_inference_sec": lightgbm_elapsed,
            "reranker_score_sec": reranker_score_sec,
            "reranked_decode_sec": reranked_decode_sec,
            "reranked_total_sec": total_reranked_sec,
            "reranker_overhead_sec": reranker_score_sec + reranked_decode_sec,
            "lightgbm_ms_per_doc": 1000.0 * lightgbm_elapsed / max(len(doc_results), 1),
            "lightgbm_ms_per_selected_page": 1000.0 * lightgbm_elapsed / max(page_count, 1),
            "reranked_ms_per_doc": 1000.0 * total_reranked_sec / max(len(doc_results), 1),
            "reranked_ms_per_selected_page": 1000.0 * total_reranked_sec / max(page_count, 1),
            "reranker_ms_per_pair": 1000.0 * reranker_score_sec / max(len(pairs), 1),
            "load_canonical_and_gt_sec": _sum_timings(doc_timings, "load_canonical_and_gt_sec"),
            "candidate_and_features_sec": _sum_timings(doc_timings, "candidate_and_features_sec"),
            "model_score_sec": _sum_timings(doc_timings, "model_score_sec"),
            "baseline_decode_sec": _sum_timings(doc_timings, "baseline_decode_sec"),
        },
        "documents": [
            {
                "doc_id": result["doc_id"],
                "relative_pdf_path": result["relative_pdf_path"],
                "split": result["split"],
                "selected_pages": result["selected_pages"],
                "page_count": result["page_count"],
                "candidate_count": result["candidate_count"],
                "seconds": doc_timings[index],
            }
            for index, result in enumerate(doc_results)
        ],
    }
    output_path = paths.reports_root / f"{args.output_name}.json"
    write_json(output_path, report)
    summary = {
        "output": str(output_path),
        "docs": report["doc_count"],
        "selected_pages": page_count,
        "candidate_count": candidate_count,
        "lightgbm_sec": report["timing"]["lightgbm_selected_page_inference_sec"],
        "lightgbm_ms_per_page": report["timing"]["lightgbm_ms_per_selected_page"],
        "reranked_total_sec": report["timing"]["reranked_total_sec"],
        "reranked_ms_per_page": report["timing"]["reranked_ms_per_selected_page"],
        "reranker_pairs": len(pairs),
        "reranker_ms_per_pair": report["timing"]["reranker_ms_per_pair"],
        "baseline_f1": baseline_report["overall"]["f1"],
        "baseline_exact": baseline_report["overall"]["exact_instance_accuracy"],
        "reranked_f1": reranked_report["overall"]["f1"],
        "reranked_exact": reranked_report["overall"]["exact_instance_accuracy"],
        "delta": report["delta"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
