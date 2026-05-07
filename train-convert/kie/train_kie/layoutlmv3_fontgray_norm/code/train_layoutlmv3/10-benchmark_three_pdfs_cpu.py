from __future__ import annotations

import argparse
import copy
import json
import os
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def set_thread_env(threads: int) -> None:
    for key in (
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
    ):
        os.environ[key] = str(threads)
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    os.environ["DIRECT_OCR_NUM_PROCESSES"] = "1"
    os.environ["DIRECT_OCR_NUM_DLL_PER_PROCESS"] = "1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark LightGBM vs LayoutLMv3 ONNX on a few local PDFs.")
    parser.add_argument("--pdf", action="append", required=True, help="PDF path. Companion JSON is resolved as <pdf>.json.")
    parser.add_argument("--layout-project-root", required=True)
    parser.add_argument("--lightgbm-project-root", required=True)
    parser.add_argument("--layout-model-path", required=True)
    parser.add_argument("--onnx", action="append", required=True, help="ONNX variant name=path. Repeatable.")
    parser.add_argument("--output", required=True)
    parser.add_argument("--threads", type=int, default=6)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--page-scope", choices=["selected", "all"], default="all")
    parser.add_argument(
        "--ocr-source",
        choices=["existing-json", "direct"],
        default="existing-json",
        help="existing-json upgrades <pdf>.json to canonical; direct runs direct_ocr_engine sequentially first.",
    )
    parser.add_argument("--method", choices=["both", "lightgbm", "onnx"], default="both")
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--max-length", type=int, default=512)
    parser.add_argument("--stride", type=int, default=128)
    parser.add_argument("--warmup-runs", type=int, default=2)
    parser.add_argument(
        "--onnx-graph-optimization-level",
        choices=["disable", "basic", "extended", "all"],
        default="all",
    )
    return parser.parse_args()


args = parse_args()
set_thread_env(args.threads)

import numpy as np  # noqa: E402
import onnxruntime as ort  # noqa: E402
from torch.utils.data import DataLoader  # noqa: E402
from transformers import AutoTokenizer, DataCollatorForTokenClassification  # noqa: E402

from kie_json_utils import upgrade_ocr_data_in_place  # noqa: E402
from train_kie.labeling_workspace import analyze_page_selection  # noqa: E402
from train_layoutlmv3.common import apply_cardinality, decode_bio_spans, write_json  # noqa: E402
from train_layoutlmv3.hf_utils import (  # noqa: E402
    TokenizedPageDataset,
    aggregate_logits_to_words,
    label_maps_from_model,
    rows_from_canonical,
)
from train_lightgbm.common import read_json  # noqa: E402
from train_lightgbm.dataset import _build_features, generate_candidates, load_ocr_document  # noqa: E402
from train_lightgbm.schema_decoder import CandidatePrediction, decode_document_predictions, link_signers  # noqa: E402
from train_lightgbm.training import load_models, score_rows  # noqa: E402


GRAPH_OPT_LEVELS = {
    "disable": ort.GraphOptimizationLevel.ORT_DISABLE_ALL,
    "basic": ort.GraphOptimizationLevel.ORT_ENABLE_BASIC,
    "extended": ort.GraphOptimizationLevel.ORT_ENABLE_EXTENDED,
    "all": ort.GraphOptimizationLevel.ORT_ENABLE_ALL,
}


def parse_variant(raw: str) -> tuple[str, Path]:
    if "=" not in raw:
        raise argparse.ArgumentTypeError("--onnx must use name=path")
    name, path = raw.split("=", 1)
    name = name.strip()
    if not name:
        raise argparse.ArgumentTypeError("ONNX variant name is empty")
    return name, Path(path)


def load_source_json(pdf_path: Path) -> dict[str, Any]:
    path = Path(str(pdf_path) + ".json")
    if not path.exists():
        raise FileNotFoundError(f"Missing companion OCR JSON: {path}")
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def page_indices(doc: dict[str, Any]) -> list[int]:
    return [int(page.get("page_index", idx)) for idx, page in enumerate(doc.get("pages", []))]


def count_words(doc: dict[str, Any], pages: list[int] | None = None) -> int:
    keep = set(pages) if pages is not None else None
    total = 0
    for idx, page in enumerate(doc.get("pages", [])):
        page_index = int(page.get("page_index", idx))
        if keep is not None and page_index not in keep:
            continue
        total += len(page.get("words") or [])
    return total


def prepare_canonical(pdf_path: Path, canonical_dir: Path) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    doc = copy.deepcopy(load_source_json(pdf_path))
    doc["input_path"] = str(pdf_path)
    document = doc.setdefault("document", {})
    document["source_path"] = str(pdf_path)
    document["source_name"] = pdf_path.name
    upgrade_ocr_data_in_place(doc)
    selection = analyze_page_selection(doc)
    canonical_path = canonical_dir / f"{pdf_path.name}.canonical.json"
    write_json(canonical_path, doc)
    return canonical_path, doc, selection


def run_direct_ocr(pdf_path: Path, ocr_dir: Path) -> tuple[Path, dict[str, Any], dict[str, Any], dict[str, Any]]:
    from direct_ocr_engine import process_pdf, shutdown_pool

    ocr_dir.mkdir(parents=True, exist_ok=True)
    output_pdf = ocr_dir / f"{pdf_path.stem}_seq_ocr.pdf"

    logs: list[dict[str, str]] = []

    def log_callback(message: str, level: str = "info") -> None:
        logs.append({"level": level, "message": str(message)})
        print(f"    OCR {level}: {message}", flush=True)

    start = time.perf_counter()
    ok, error = process_pdf(
        str(pdf_path),
        str(output_pdf),
        update_callback=log_callback,
        source_document_path=str(pdf_path),
        allow_page_parallel=False,
    )
    seconds = time.perf_counter() - start
    try:
        shutdown_pool()
    except Exception:
        pass
    if not ok:
        raise RuntimeError(f"OCR failed for {pdf_path}: {error}")
    canonical_path = Path(str(output_pdf) + ".json")
    with canonical_path.open("r", encoding="utf-8") as f:
        doc = json.load(f)
    selection = analyze_page_selection(doc)
    return canonical_path, doc, selection, {
        "source": "direct_screen_ai_serial",
        "output_pdf": str(output_pdf),
        "output_json": str(canonical_path),
        "seconds": seconds,
        "ms_per_page": seconds * 1000.0 / max(1, len(page_indices(doc))),
        "logs_tail": logs[-20:],
    }


def scope_pages(doc: dict[str, Any], selection: dict[str, Any], page_scope: str) -> tuple[list[int], int, int | None]:
    all_pages = page_indices(doc)
    if page_scope == "all":
        pages = all_pages
    else:
        pages = [int(page) for page in (selection.get("selected_pages") or all_pages)]
    primary_page = selection.get("primary_page")
    primary = int(primary_page) if primary_page is not None else (all_pages[0] if all_pages else 0)
    signature_page = selection.get("signature_page")
    signature = int(signature_page) if signature_page is not None else (all_pages[-1] if len(all_pages) > 1 else None)
    return pages, primary, signature


def set_lightgbm_threads(model: Any, threads: int) -> None:
    return None


class LightGBMRunner:
    def __init__(self, project_root: Path, threads: int) -> None:
        self.project_root = project_root
        self.models_dir = project_root / "models" / "fieldwise"
        start = time.perf_counter()
        self.thresholds = read_json(self.models_dir / "thresholds.json", default={})
        self.models = load_models(self.models_dir)
        for model in self.models.values():
            set_lightgbm_threads(model, threads)
        self.metas = {field: read_json(self.models_dir / f"{field}.meta.json") for field in self.models}
        self.load_seconds = time.perf_counter() - start

    def run(
        self,
        canonical_path: Path,
        pdf_path: Path,
        selected_pages: list[int],
        primary_page: int,
        signature_page: int | None,
    ) -> dict[str, Any]:
        doc_meta = {
            "doc_id": pdf_path.stem,
            "relative_pdf_path": pdf_path.name,
            "split": "inference",
            "source_canonical_json": str(canonical_path),
            "selected_pages": selected_pages,
            "primary_page": primary_page,
            "signature_page": signature_page,
            "doc_kind": "regular",
        }
        start = time.perf_counter()
        doc = load_ocr_document(doc_meta)
        load_seconds = time.perf_counter() - start

        candidate_seconds = 0.0
        score_seconds = 0.0
        decoded_input: dict[str, list[CandidatePrediction]] = {}
        candidate_count = 0
        for field, model in self.models.items():
            print(f"    lightgbm field start {pdf_path.name} {field}", flush=True)
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
            scores = score_rows(model, rows, self.metas[field]["feature_names"])
            score_seconds += time.perf_counter() - start
            print(
                f"    lightgbm field done {pdf_path.name} {field}: candidates={len(candidates)}",
                flush=True,
            )
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
        decoded = decode_document_predictions(decoded_input, self.thresholds)
        relations = link_signers(decoded)
        decode_seconds = time.perf_counter() - start
        return {
            "pages": len(selected_pages),
            "candidate_count": candidate_count,
            "field_count": sum(len(items) for items in decoded.values()),
            "relation_count": len(relations),
            "seconds": {
                "load_ocr_json": load_seconds,
                "candidate_and_features": candidate_seconds,
                "model_score": score_seconds,
                "decode": decode_seconds,
                "total_after_ocr_json": load_seconds + candidate_seconds + score_seconds + decode_seconds,
            },
        }


class LayoutOnnxRunner:
    def __init__(self, name: str, onnx_path: Path, model_path: Path, threads: int) -> None:
        self.name = name
        self.onnx_path = onnx_path
        self.model_path = model_path
        start = time.perf_counter()
        _label_list, self.label2id, self.id2label = label_maps_from_model(model_path)
        self.tokenizer = AutoTokenizer.from_pretrained(model_path, use_fast=True)
        self.collator = DataCollatorForTokenClassification(
            tokenizer=self.tokenizer,
            padding="max_length",
            max_length=args.max_length,
            label_pad_token_id=-100,
        )
        session_options = ort.SessionOptions()
        session_options.graph_optimization_level = GRAPH_OPT_LEVELS[args.onnx_graph_optimization_level]
        session_options.intra_op_num_threads = threads
        session_options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(onnx_path), sess_options=session_options, providers=["CPUExecutionProvider"])
        self.input_names = {item.name for item in self.session.get_inputs()}
        self.load_seconds = time.perf_counter() - start
        self.warmed = False

    def _batches(self, rows: list[dict[str, Any]]) -> tuple[TokenizedPageDataset, list[dict[str, np.ndarray]]]:
        dataset = TokenizedPageDataset(rows, self.tokenizer, self.label2id, args.max_length, args.stride, "same")
        loader = DataLoader(dataset, batch_size=args.batch_size, shuffle=False, collate_fn=self.collator)
        batches = []
        for batch in loader:
            batch.pop("labels", None)
            candidate = {
                "input_ids": batch["input_ids"].cpu().numpy().astype(np.int64),
                "attention_mask": batch["attention_mask"].cpu().numpy().astype(np.int64),
                "bbox": batch["bbox"].cpu().numpy().astype(np.int64),
            }
            batches.append({key: value for key, value in candidate.items() if key in self.input_names})
        return dataset, batches

    def run(self, canonical_path: Path, selected_pages: list[int]) -> dict[str, Any]:
        start = time.perf_counter()
        rows = rows_from_canonical(canonical_path, selected_pages=selected_pages)
        load_rows_seconds = time.perf_counter() - start

        start = time.perf_counter()
        dataset, batches = self._batches(rows)
        tokenize_seconds = time.perf_counter() - start

        warmup_seconds = 0.0
        if not self.warmed and batches:
            start = time.perf_counter()
            for batch in batches[:1]:
                for _ in range(max(0, args.warmup_runs)):
                    self.session.run(None, batch)
            warmup_seconds = time.perf_counter() - start
            self.warmed = True

        logits_parts = []
        start = time.perf_counter()
        for batch in batches:
            logits_parts.append(self.session.run(None, batch)[0])
        model_seconds = time.perf_counter() - start

        start = time.perf_counter()
        logits = (
            np.concatenate(logits_parts, axis=0)
            if logits_parts
            else np.zeros((0, args.max_length, len(self.id2label)), dtype=np.float32)
        )
        pred_labels, pred_scores = aggregate_logits_to_words(rows, dataset, logits, self.id2label)
        raw_spans = []
        for row, labels, scores in zip(rows, pred_labels, pred_scores):
            raw_spans.extend(decode_bio_spans(row, labels, scores))
        schema_spans, fragmentation = apply_cardinality(raw_spans)
        decode_seconds = time.perf_counter() - start

        return {
            "pages": len(rows),
            "chunks": len(dataset),
            "field_count": len(schema_spans),
            "fragmented_fields": fragmentation.get("fragmented_fields", 0),
            "seconds": {
                "load_ocr_json_to_rows": load_rows_seconds,
                "tokenize_and_batch": tokenize_seconds,
                "warmup_excluded": warmup_seconds,
                "model_session_run": model_seconds,
                "aggregate_and_decode": decode_seconds,
                "total_after_ocr_json": load_rows_seconds + tokenize_seconds + model_seconds + decode_seconds,
            },
        }


def summarize(runs: list[dict[str, Any]], pages: int) -> dict[str, Any]:
    keys = sorted({key for run in runs for key in run["seconds"]})
    summary = {"runs": runs}
    for key in keys:
        values = [run["seconds"].get(key, 0.0) for run in runs]
        summary[key] = {
            "mean_seconds": statistics.mean(values),
            "median_seconds": statistics.median(values),
            "best_seconds": min(values),
            "mean_ms_per_page": statistics.mean(values) * 1000.0 / pages if pages else 0.0,
            "best_ms_per_page": min(values) * 1000.0 / pages if pages else 0.0,
        }
    return summary


def save_report(path: Path, report: dict[str, Any]) -> None:
    write_json(path, report)
    print(f"saved {path}", flush=True)


def main() -> None:
    output = Path(args.output)
    report_dir = output.parent
    canonical_dir = report_dir / "canonical"
    ocr_dir = report_dir / "ocr_serial"
    canonical_dir.mkdir(parents=True, exist_ok=True)

    prepared = []
    for raw_pdf in args.pdf:
        pdf_path = Path(raw_pdf)
        if args.ocr_source == "direct":
            print(f"OCR serial {pdf_path}", flush=True)
            canonical_path, doc, selection, ocr_report = run_direct_ocr(pdf_path, ocr_dir)
        else:
            canonical_path, doc, selection = prepare_canonical(pdf_path, canonical_dir)
            ocr_report = {
                "source": "existing_json_companion_upgraded_to_canonical",
                "output_json": str(canonical_path),
                "seconds": 0.0,
                "ms_per_page": 0.0,
            }
        selected_pages, primary_page, signature_page = scope_pages(doc, selection, args.page_scope)
        prepared.append(
            {
                "pdf": pdf_path,
                "canonical": canonical_path,
                "selected_pages": selected_pages,
                "primary_page": primary_page,
                "signature_page": signature_page,
                "page_selection": {key: value for key, value in selection.items() if key != "candidates"},
                "ocr": ocr_report,
                "stats": {
                    "all_pages": len(page_indices(doc)),
                    "all_words": count_words(doc),
                    "bench_pages": len(selected_pages),
                    "bench_words": count_words(doc, selected_pages),
                },
            }
        )

    report: dict[str, Any] = {
        "scope": "OCR is measured separately when --ocr-source direct; KIE timings start after OCR JSON exists.",
        "ocr_source": args.ocr_source,
        "cpu_threads": args.threads,
        "repeats": args.repeats,
        "page_scope": args.page_scope,
        "batch_size": args.batch_size,
        "max_length": args.max_length,
        "stride": args.stride,
        "onnx_graph_optimization_level": args.onnx_graph_optimization_level,
        "method": args.method,
        "model_load_seconds": {},
        "documents": [],
        "aggregate": {},
    }

    lightgbm = None
    if args.method in {"both", "lightgbm"}:
        print("loading LightGBM models", flush=True)
        lightgbm = LightGBMRunner(Path(args.lightgbm_project_root), args.threads)
        report["model_load_seconds"]["lightgbm"] = lightgbm.load_seconds
        save_report(output, report)

    layout_runners = {}
    if args.method in {"both", "onnx"}:
        for name, path in [parse_variant(raw) for raw in args.onnx]:
            print(f"loading ONNX {name}: {path}", flush=True)
            runner = LayoutOnnxRunner(name, path, Path(args.layout_model_path), args.threads)
            layout_runners[name] = runner
            report["model_load_seconds"][name] = runner.load_seconds
            save_report(output, report)

    aggregate_runs: dict[str, list[dict[str, Any]]] = defaultdict(list)
    aggregate_page_runs: dict[str, int] = defaultdict(int)
    for item in prepared:
        pdf_path = item["pdf"]
        pages = len(item["selected_pages"])
        print(f"benchmark {pdf_path.name}: pages={item['selected_pages']} words={item['stats']['bench_words']}", flush=True)
        doc_report: dict[str, Any] = {
            "pdf": str(pdf_path),
            "canonical": str(item["canonical"]),
            "selected_pages": item["selected_pages"],
            "primary_page": item["primary_page"],
            "signature_page": item["signature_page"],
            "page_selection": item["page_selection"],
            "ocr": item["ocr"],
            "stats": item["stats"],
            "benchmarks": {},
        }

        if lightgbm is not None:
            lightgbm_runs = []
            for repeat in range(args.repeats):
                run = lightgbm.run(item["canonical"], pdf_path, item["selected_pages"], item["primary_page"], item["signature_page"])
                lightgbm_runs.append(run)
                print(
                    f"  lightgbm repeat={repeat + 1} total={run['seconds']['total_after_ocr_json']:.3f}s "
                    f"score={run['seconds']['model_score']:.3f}s candidates={run['candidate_count']}",
                    flush=True,
                )
            doc_report["benchmarks"]["lightgbm"] = summarize(lightgbm_runs, pages)
            aggregate_runs["lightgbm"].extend(lightgbm_runs)
            aggregate_page_runs["lightgbm"] += pages * len(lightgbm_runs)

        for name, runner in layout_runners.items():
            runs = []
            for repeat in range(args.repeats):
                run = runner.run(item["canonical"], item["selected_pages"])
                runs.append(run)
                print(
                    f"  {name} repeat={repeat + 1} total={run['seconds']['total_after_ocr_json']:.3f}s "
                    f"model={run['seconds']['model_session_run']:.3f}s chunks={run['chunks']}",
                    flush=True,
                )
            doc_report["benchmarks"][name] = summarize(runs, pages)
            aggregate_runs[name].extend(runs)
            aggregate_page_runs[name] += pages * len(runs)

        report["documents"].append(doc_report)
        save_report(output, report)

    for name, runs in aggregate_runs.items():
        page_runs = aggregate_page_runs[name]
        total_values = [run["seconds"]["total_after_ocr_json"] for run in runs]
        if name == "lightgbm":
            model_values = [run["seconds"]["model_score"] for run in runs]
        else:
            model_values = [run["seconds"]["model_session_run"] for run in runs]
        report["aggregate"][name] = {
            "runs": len(runs),
            "page_runs": page_runs,
            "sum_total_seconds": sum(total_values),
            "total_ms_per_page": sum(total_values) * 1000.0 / page_runs if page_runs else 0.0,
            "sum_model_seconds": sum(model_values),
            "model_ms_per_page": sum(model_values) * 1000.0 / page_runs if page_runs else 0.0,
        }
    baseline = report["aggregate"].get("lightgbm", {}).get("total_ms_per_page")
    if baseline:
        for name, agg in report["aggregate"].items():
            agg["relative_total_vs_lightgbm_x"] = agg["total_ms_per_page"] / baseline
    total_ocr_seconds = sum(float(item.get("ocr", {}).get("seconds") or 0.0) for item in prepared)
    total_ocr_pages = sum(int(item.get("stats", {}).get("all_pages") or 0) for item in prepared)
    report["ocr_total"] = {
        "seconds": total_ocr_seconds,
        "pages": total_ocr_pages,
        "ms_per_page": total_ocr_seconds * 1000.0 / total_ocr_pages if total_ocr_pages else 0.0,
    }
    save_report(output, report)
    print(json.dumps(report["aggregate"], ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
