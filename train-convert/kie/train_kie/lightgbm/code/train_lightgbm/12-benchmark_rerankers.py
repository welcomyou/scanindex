from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from train_lightgbm.common import build_paths, read_json, write_json
from train_lightgbm.config import LABELS
from train_lightgbm.reranker_common import candidate_payload, query_for
from train_lightgbm.schema_decoder import CandidatePrediction
from train_lightgbm.training import _load_scored_candidates


MODEL_CONFIGS = {
    "mminilm": {
        "model_id": "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1",
        "max_length": 384,
        "batch_size": 16,
        "trust_remote_code": False,
    },
    "phoranker": {
        "model_id": "itdainb/PhoRanker",
        "max_length": 256,
        "batch_size": 16,
        "trust_remote_code": False,
    },
    "gte": {
        "model_id": "Alibaba-NLP/gte-multilingual-reranker-base",
        "max_length": 512,
        "batch_size": 12,
        "trust_remote_code": True,
    },
    "viranker": {
        "model_id": "namdp-ptit/ViRanker",
        "max_length": 512,
        "batch_size": 4,
        "trust_remote_code": False,
    },
    "qwen3-0.6b": {
        "model_id": "Qwen/Qwen3-Reranker-0.6B",
        "max_length": 512,
        "batch_size": 4,
        "trust_remote_code": True,
    },
}

EXACT_CLASSES = {"LM_CAN_CHOOSE_EXACT", "LM_CAN_CHOOSE_EXACT_BUT_RULE_BETTER"}
TRIM_CLASSES = {"LM_CAN_TRIM_SUPERSET", "LM_CAN_TRIM_SUPERSET_BUT_RULE_BETTER"}
REJECT_CLASSES = {"LM_CAN_REJECT_EXTRA", "LM_LOW_VALUE_EXTRA"}


def _configure_cpu_threads(threads: int | None, interop_threads: int | None) -> dict:
    if threads is None or threads <= 0:
        threads = os.cpu_count() or 1
    if interop_threads is None or interop_threads <= 0:
        # Intra-op does the heavy GEMM work. Too many inter-op threads can oversubscribe.
        interop_threads = min(4, threads)
    for key in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[key] = str(threads)
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    info = {"requested_threads": threads, "requested_interop_threads": interop_threads}
    try:
        import torch

        torch.set_num_threads(threads)
        torch.set_num_interop_threads(interop_threads)
        info["torch_num_threads"] = torch.get_num_threads()
        info["torch_num_interop_threads"] = torch.get_num_interop_threads()
    except Exception as exc:
        info["torch_thread_error"] = f"{type(exc).__name__}: {exc}"
    return info


def _select_device(device: str) -> str:
    requested = (device or "auto").lower()
    if requested == "auto":
        try:
            import torch

            return "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:
            return "cpu"
    return requested


def _ascii(text: object) -> str:
    raw = "" if text is None else str(text)
    raw = raw.replace("Ä", "D").replace("Ä‘", "d")
    decomposed = unicodedata.normalize("NFKD", raw)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _candidate_doc(candidate: CandidatePrediction, include_layout: bool = False) -> str:
    if include_layout:
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
    text = _ascii(candidate.text).strip()
    return text or "[EMPTY]"


def _query_for(field: str) -> str:
    return query_for(field)


def _load_model(model_key: str, model_path: str | None, max_length: int | None, trust_remote_code: bool | None, device: str):
    from sentence_transformers import CrossEncoder

    cfg = MODEL_CONFIGS[model_key] if model_key else {}
    model_id = model_path or cfg["model_id"]
    kwargs = {
        "max_length": max_length or cfg.get("max_length", 384),
        "device": device,
    }
    if trust_remote_code if trust_remote_code is not None else cfg.get("trust_remote_code"):
        kwargs["trust_remote_code"] = True
    return CrossEncoder(model_id, **kwargs)


class OnnxCrossEncoder:
    def __init__(self, onnx_path: str, tokenizer_path: str, max_length: int, providers: list[str] | None = None):
        import onnxruntime as ort
        from transformers import AutoTokenizer

        self.tokenizer = AutoTokenizer.from_pretrained(tokenizer_path)
        self.max_length = max_length
        options = ort.SessionOptions()
        intra_threads = int(os.environ.get("OMP_NUM_THREADS") or 0)
        inter_threads = int(os.environ.get("ORT_INTER_OP_THREADS") or os.environ.get("TORCH_INTEROP_THREADS") or 0)
        if intra_threads > 0:
            options.intra_op_num_threads = intra_threads
        if inter_threads > 0:
            options.inter_op_num_threads = inter_threads
        self.session = ort.InferenceSession(
            onnx_path,
            sess_options=options,
            providers=providers or ["CPUExecutionProvider"],
        )
        self.input_names = {item.name for item in self.session.get_inputs()}
        self.output_name = self.session.get_outputs()[0].name

    def predict(self, pairs: list[tuple[str, str]], batch_size: int, show_progress_bar: bool = False):
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


def _candidate_maps(scored_by_doc: dict[str, dict[str, list[CandidatePrediction]]]) -> dict[tuple[str, str], list[CandidatePrediction]]:
    out: dict[tuple[str, str], list[CandidatePrediction]] = {}
    for doc_id, fields in scored_by_doc.items():
        for field, candidates in fields.items():
            out[(doc_id, field)] = sorted(candidates, key=lambda item: item.score, reverse=True)
    return out


def _collect_cases(
    project_root: Path,
    top_k: int,
    splits: list[str],
    max_cases_per_group: int | None,
    *,
    include_layout: bool,
) -> tuple[list[dict], list[dict]]:
    paths = build_paths(project_root)
    lm_report = read_json(paths.reports_root / "lm_fixability_report_current.json")
    errors = [
        row
        for row in lm_report["errors"]
        if row["split"] in splits
        and row["field"] in LABELS
    ]
    thresholds = read_json(paths.models_root / "fieldwise" / "thresholds.json", default={})
    scored_by_split = {}
    for split in splits:
        _, scored_by_doc = _load_scored_candidates(project_root, paths.models_root / "fieldwise", split)
        scored_by_split[split] = _candidate_maps(scored_by_doc)

    rank_cases: list[dict] = []
    binary_cases: list[dict] = []
    group_counts = Counter()
    for row in errors:
        field = row["field"]
        split = row["split"]
        doc_id = row["doc_id"]
        candidates = scored_by_split[split].get((doc_id, field), [])
        stats = row.get("candidate_stats") or {}
        lm_class = row.get("lm_class")
        if lm_class in EXACT_CLASSES:
            group = "exact"
            target = stats.get("exact") or {}
            target_id = target.get("candidate_id")
        elif lm_class in TRIM_CLASSES:
            group = "superset"
            target = stats.get("contains_gt") or {}
            target_id = target.get("candidate_id")
        elif lm_class in REJECT_CLASSES and row["kind"] == "EXTRA":
            group = "reject_negative"
            target_id = None
        else:
            continue
        if max_cases_per_group is not None and group_counts[group] >= max_cases_per_group:
            continue
        group_counts[group] += 1

        if group in {"exact", "superset"}:
            top_candidates = candidates[:top_k]
            target_in_topk = any(cand.candidate_id == target_id for cand in top_candidates)
            rank_cases.append(
                {
                    "group": group,
                    "split": split,
                    "file": row["file"],
                    "field": field,
                    "target_candidate_id": target_id,
                    "target_in_topk": target_in_topk,
                    "top_k": len(top_candidates),
                    "candidates": [
                        {
                            "candidate_id": cand.candidate_id,
                            "text": _candidate_doc(cand, include_layout=include_layout),
                            "lgbm_score": float(cand.score),
                        }
                        for cand in top_candidates
                    ],
                }
            )
            if target_id:
                # The positive score for reject calibration uses the oracle target text.
                target_candidate = next((cand for cand in candidates if cand.candidate_id == target_id), None)
                if target_candidate is not None:
                    binary_cases.append(
                        {
                            "label": 1,
                            "source_group": group,
                            "split": split,
                            "file": row["file"],
                            "field": field,
                            "text": _candidate_doc(target_candidate, include_layout=include_layout),
                        }
                    )
        elif group == "reject_negative":
            pred_text = _ascii(row.get("pred_text") or "").strip()
            if pred_text:
                if include_layout:
                    pred_text = (
                        f"FIELD={field}\nTEXT:\n{pred_text}\n"
                        f"LAYOUT: page={row.get('pred_page_index')} source={row.get('pred_source_kind')} lgbm_score={row.get('pred_score')}"
                    )
                binary_cases.append(
                    {
                        "label": 0,
                        "source_group": group,
                        "split": split,
                        "file": row["file"],
                        "field": field,
                        "text": pred_text,
                    }
                )
    return rank_cases, binary_cases


def _evaluate_rank_cases(model, rank_cases: list[dict], batch_size: int) -> dict:
    pairs: list[tuple[str, str]] = []
    spans: list[tuple[int, int]] = []
    for case in rank_cases:
        start = len(pairs)
        query = _query_for(case["field"])
        for cand in case["candidates"]:
            pairs.append((query, cand["text"]))
        spans.append((start, len(pairs)))
    t0 = time.perf_counter()
    scores = _predict_scores(model, pairs, batch_size=batch_size) if pairs else []
    elapsed = time.perf_counter() - t0
    rows = []
    for case, (start, end) in zip(rank_cases, spans):
        case_scores = scores[start:end]
        best_idx = max(range(len(case_scores)), key=lambda idx: case_scores[idx]) if case_scores else None
        best_candidate = case["candidates"][best_idx] if best_idx is not None else None
        target_id = case["target_candidate_id"]
        target_rank = None
        sorted_indices = sorted(range(len(case_scores)), key=lambda idx: case_scores[idx], reverse=True)
        for rank, idx in enumerate(sorted_indices, start=1):
            if case["candidates"][idx]["candidate_id"] == target_id:
                target_rank = rank
                break
        rows.append(
            {
                "group": case["group"],
                "split": case["split"],
                "field": case["field"],
                "file": case["file"],
                "target_in_topk": case["target_in_topk"],
                "correct_top1": bool(best_candidate and best_candidate["candidate_id"] == target_id),
                "target_rank": target_rank,
                "best_candidate_id": best_candidate["candidate_id"] if best_candidate else None,
                "best_score": float(case_scores[best_idx]) if best_idx is not None else None,
            }
        )
    return {"rows": rows, "elapsed_sec": elapsed, "pairs": len(pairs)}


def _best_threshold(train_items: list[dict]) -> float:
    if not train_items:
        return 0.0
    scores = sorted(set(float(item["score"]) for item in train_items))
    if not scores:
        return 0.0
    candidates = [scores[0] - 1e-6, scores[-1] + 1e-6]
    for a, b in zip(scores, scores[1:]):
        candidates.append((a + b) / 2.0)
    best_t = candidates[0]
    best_f1 = -1.0
    for threshold in candidates:
        tp = fp = fn = 0
        for item in train_items:
            pred = 1 if item["score"] >= threshold else 0
            label = int(item["label"])
            if pred == 1 and label == 1:
                tp += 1
            elif pred == 1 and label == 0:
                fp += 1
            elif pred == 0 and label == 1:
                fn += 1
        precision = tp / max(tp + fp, 1)
        recall = tp / max(tp + fn, 1)
        f1 = 2 * precision * recall / max(precision + recall, 1e-9)
        if f1 > best_f1:
            best_f1 = f1
            best_t = threshold
    return best_t


def _binary_metrics(items: list[dict], threshold: float) -> dict:
    tp = tn = fp = fn = 0
    for item in items:
        pred = 1 if item["score"] >= threshold else 0
        label = int(item["label"])
        if pred == 1 and label == 1:
            tp += 1
        elif pred == 0 and label == 0:
            tn += 1
        elif pred == 1 and label == 0:
            fp += 1
        else:
            fn += 1
    precision = tp / max(tp + fp, 1)
    recall = tp / max(tp + fn, 1)
    f1 = 2 * precision * recall / max(precision + recall, 1e-9)
    accuracy = (tp + tn) / max(len(items), 1)
    return {"n": len(items), "tp": tp, "tn": tn, "fp": fp, "fn": fn, "precision": precision, "recall": recall, "f1": f1, "accuracy": accuracy}


def _evaluate_binary_cases(model, binary_cases: list[dict], batch_size: int) -> dict:
    pairs = [(_query_for(case["field"]), case["text"]) for case in binary_cases]
    t0 = time.perf_counter()
    scores = _predict_scores(model, pairs, batch_size=batch_size) if pairs else []
    elapsed = time.perf_counter() - t0
    rows = []
    for case, score in zip(binary_cases, scores):
        item = dict(case)
        item["score"] = float(score)
        rows.append(item)
    train = [item for item in rows if item["split"] == "train"]
    threshold = _best_threshold(train)
    metrics_by_split = {}
    for split in ["train", "val", "test", "valtest", "all"]:
        if split == "valtest":
            subset = [item for item in rows if item["split"] in {"val", "test"}]
        elif split == "all":
            subset = rows
        else:
            subset = [item for item in rows if item["split"] == split]
        metrics_by_split[split] = _binary_metrics(subset, threshold)
    return {"rows": rows, "elapsed_sec": elapsed, "pairs": len(pairs), "threshold": threshold, "metrics": metrics_by_split}


def _summarize_rank(rows: list[dict]) -> dict:
    out = {}
    for group in ["exact", "superset"]:
        group_rows = [row for row in rows if row["group"] == group]
        for split in ["train", "val", "test", "valtest", "all"]:
            if split == "valtest":
                subset = [row for row in group_rows if row["split"] in {"val", "test"}]
            elif split == "all":
                subset = group_rows
            else:
                subset = [row for row in group_rows if row["split"] == split]
            covered = [row for row in subset if row["target_in_topk"]]
            correct = [row for row in subset if row["correct_top1"]]
            out[f"{group}_{split}"] = {
                "n": len(subset),
                "covered": len(covered),
                "coverage": len(covered) / max(len(subset), 1),
                "top1_effective": len(correct) / max(len(subset), 1),
                "top1_when_covered": len(correct) / max(len(covered), 1),
                "mrr_when_covered": sum(1.0 / row["target_rank"] for row in covered if row["target_rank"]) / max(len(covered), 1),
            }
    return out


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark cross-encoder rerankers on LM-fixable LightGBM KIE errors.")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--model", default="mminilm", choices=sorted(MODEL_CONFIGS))
    parser.add_argument("--model-path", default=None, help="Local fine-tuned CrossEncoder/Transformers model path.")
    parser.add_argument("--onnx-path", default=None, help="Use an ONNX CrossEncoder model instead of sentence-transformers.")
    parser.add_argument("--tokenizer-path", default=None, help="Tokenizer path for --onnx-path. Defaults to --model-path.")
    parser.add_argument("--onnx-provider", action="append", default=None, help="ONNX Runtime provider. Repeat for priority order.")
    parser.add_argument("--max-length", type=int, default=None)
    parser.add_argument("--batch-size", type=int, default=None)
    parser.add_argument("--trust-remote-code", action="store_true")
    parser.add_argument("--include-layout", action="store_true")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--splits", nargs="+", default=["train", "val", "test"], choices=["train", "val", "test"])
    parser.add_argument("--max-cases-per-group", type=int, default=None)
    parser.add_argument("--output-name", default=None)
    parser.add_argument("--threads", type=int, default=0, help="Torch/BLAS intra-op CPU threads. 0 means os.cpu_count().")
    parser.add_argument("--interop-threads", type=int, default=0, help="Torch inter-op CPU threads. 0 means min(4, threads).")
    parser.add_argument("--device", default="cpu", choices=["auto", "cpu", "cuda"], help="Inference device. Use cpu for production latency, cuda for faster accuracy runs.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    paths = build_paths(args.project_root)
    cfg = MODEL_CONFIGS[args.model]
    batch_size = args.batch_size or cfg["batch_size"]
    thread_info = _configure_cpu_threads(args.threads, args.interop_threads)
    device = _select_device(args.device)
    print(f"[rerank_bench] cpu_threads={thread_info}", flush=True)
    print(f"[rerank_bench] device={device}", flush=True)
    print(f"[rerank_bench] collecting cases top_k={args.top_k}", flush=True)
    rank_cases, binary_cases = _collect_cases(
        paths.root,
        args.top_k,
        args.splits,
        args.max_cases_per_group,
        include_layout=args.include_layout,
    )
    print(f"[rerank_bench] rank_cases={len(rank_cases)} binary_cases={len(binary_cases)}", flush=True)
    print(f"[rerank_bench] loading {args.model}: {args.onnx_path or args.model_path or cfg['model_id']}", flush=True)
    load_t0 = time.perf_counter()
    if args.onnx_path:
        model = OnnxCrossEncoder(
            args.onnx_path,
            args.tokenizer_path or args.model_path or cfg["model_id"],
            args.max_length or cfg["max_length"],
            providers=args.onnx_provider,
        )
    else:
        model = _load_model(args.model, args.model_path, args.max_length, args.trust_remote_code, device)
    load_sec = time.perf_counter() - load_t0
    print(f"[rerank_bench] loaded in {load_sec:.2f}s", flush=True)
    rank_result = _evaluate_rank_cases(model, rank_cases, batch_size)
    print(f"[rerank_bench] rank pairs={rank_result['pairs']} elapsed={rank_result['elapsed_sec']:.2f}s", flush=True)
    binary_result = _evaluate_binary_cases(model, binary_cases, batch_size)
    print(f"[rerank_bench] binary pairs={binary_result['pairs']} elapsed={binary_result['elapsed_sec']:.2f}s", flush=True)
    total_pairs = rank_result["pairs"] + binary_result["pairs"]
    total_score_sec = rank_result["elapsed_sec"] + binary_result["elapsed_sec"]
    report = {
        "model": args.model,
        "model_id": args.onnx_path or args.model_path or cfg["model_id"],
        "runtime": "onnxruntime" if args.onnx_path else "sentence-transformers",
        "onnx_provider": args.onnx_provider,
        "top_k": args.top_k,
        "include_layout": args.include_layout,
        "splits": args.splits,
        "max_cases_per_group": args.max_cases_per_group,
        "load_sec": load_sec,
        "thread_info": thread_info,
        "device": device,
        "score_sec": total_score_sec,
        "pairs": total_pairs,
        "pairs_per_sec": total_pairs / max(total_score_sec, 1e-9),
        "ms_per_pair": 1000.0 * total_score_sec / max(total_pairs, 1),
        "rank_summary": _summarize_rank(rank_result["rows"]),
        "reject_summary": {
            "threshold": binary_result["threshold"],
            "metrics": binary_result["metrics"],
        },
        "case_counts": {
            "rank": Counter(case["group"] for case in rank_cases),
            "binary": Counter(case["source_group"] for case in binary_cases),
        },
    }
    # Convert counters for JSON.
    report["case_counts"] = {key: dict(value) for key, value in report["case_counts"].items()}
    output_name = args.output_name or f"reranker_benchmark_{args.model}_top{args.top_k}"
    out_path = paths.reports_root / f"{output_name}.json"
    write_json(out_path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
