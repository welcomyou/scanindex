from __future__ import annotations

import argparse
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark LayoutLMv3 ONNX on OCR JSON companion files.")
    parser.add_argument("--pdf", action="append", required=True)
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--onnx", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--threads", type=int, default=6)
    parser.add_argument("--page-scope", choices=["selected", "all"], default="selected")
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--max-length", type=int, default=512)
    parser.add_argument("--stride", type=int, default=128)
    parser.add_argument("--warmup-runs", type=int, default=1)
    parser.add_argument("--log", help="Optional progress log file.")
    return parser.parse_args()


args = parse_args()
for key in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ[key] = str(args.threads)
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import numpy as np  # noqa: E402
import onnxruntime as ort  # noqa: E402
from torch.utils.data import DataLoader  # noqa: E402
from transformers import AutoTokenizer, DataCollatorForTokenClassification  # noqa: E402

from train_kie.labeling_workspace import analyze_page_selection  # noqa: E402
from train_layoutlmv3.common import apply_cardinality, decode_bio_spans, write_json  # noqa: E402
from train_layoutlmv3.hf_utils import (  # noqa: E402
    TokenizedPageDataset,
    aggregate_logits_to_words,
    label_maps_from_model,
    rows_from_canonical,
)


def log(message: str) -> None:
    print(message, flush=True)
    if args.log:
        path = Path(args.log)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(message + "\n")


def companion_json(pdf: Path) -> Path:
    path = Path(str(pdf) + ".json")
    if not path.exists():
        raise FileNotFoundError(path)
    return path


def selected_pages(raw: dict[str, Any]) -> tuple[list[int], dict[str, Any]]:
    all_pages = [int(page.get("page_index", idx)) for idx, page in enumerate(raw.get("pages", []))]
    selection = analyze_page_selection(raw)
    if args.page_scope == "all":
        pages = all_pages
    else:
        pages = [int(page) for page in (selection.get("selected_pages") or all_pages)]
    return pages, selection


class OnnxRunner:
    def __init__(self) -> None:
        start = time.perf_counter()
        _labels, self.label2id, self.id2label = label_maps_from_model(args.model_path)
        self.tokenizer = AutoTokenizer.from_pretrained(args.model_path, use_fast=True)
        self.collator = DataCollatorForTokenClassification(
            tokenizer=self.tokenizer,
            padding="max_length",
            max_length=args.max_length,
            label_pad_token_id=-100,
        )
        options = ort.SessionOptions()
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        options.intra_op_num_threads = args.threads
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(args.onnx), sess_options=options, providers=["CPUExecutionProvider"])
        self.input_names = {item.name for item in self.session.get_inputs()}
        self.load_seconds = time.perf_counter() - start
        self.warmed = False

    def make_batches(self, rows: list[dict[str, Any]]) -> tuple[TokenizedPageDataset, list[dict[str, np.ndarray]]]:
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

    def run(self, pdf: Path, pages: list[int]) -> dict[str, Any]:
        json_path = companion_json(pdf)
        start = time.perf_counter()
        rows = rows_from_canonical(json_path, selected_pages=pages, doc_id=pdf.stem)
        load_rows_seconds = time.perf_counter() - start

        start = time.perf_counter()
        dataset, batches = self.make_batches(rows)
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
        spans, fragmentation = apply_cardinality(raw_spans)
        decode_seconds = time.perf_counter() - start
        total_seconds = load_rows_seconds + tokenize_seconds + model_seconds + decode_seconds
        return {
            "pdf": str(pdf),
            "json": str(json_path),
            "page_scope": args.page_scope,
            "selected_pages": pages,
            "pages": len(rows),
            "chunks": len(dataset),
            "words": sum(len(row.get("tokens", [])) for row in rows),
            "field_count": len(spans),
            "fragmented_fields": fragmentation.get("fragmented_fields", 0),
            "seconds": {
                "load_ocr_json_to_rows": load_rows_seconds,
                "tokenize_and_batch": tokenize_seconds,
                "warmup_excluded": warmup_seconds,
                "model_session_run": model_seconds,
                "aggregate_and_decode": decode_seconds,
                "total_after_ocr_json": total_seconds,
            },
        }


def main() -> None:
    runner = OnnxRunner()
    report: dict[str, Any] = {
        "method": "layoutlmv3_onnx",
        "onnx": str(Path(args.onnx)),
        "model_path": str(Path(args.model_path)),
        "cpu_threads": args.threads,
        "page_scope": args.page_scope,
        "batch_size": args.batch_size,
        "max_length": args.max_length,
        "stride": args.stride,
        "model_load_seconds": runner.load_seconds,
        "documents": [],
    }
    for raw_pdf in args.pdf:
        pdf = Path(raw_pdf)
        raw = json.loads(companion_json(pdf).read_text(encoding="utf-8"))
        pages, selection = selected_pages(raw)
        log(f"doc start {pdf} pages={pages}")
        item = runner.run(pdf, pages)
        item["page_selection"] = {key: value for key, value in selection.items() if key != "candidates"}
        report["documents"].append(item)
        write_json(args.output, report)
        log(
            f"doc done {pdf.name}: total={item['seconds']['total_after_ocr_json']:.3f}s "
            f"model={item['seconds']['model_session_run']:.3f}s chunks={item['chunks']}"
        )
    pages = sum(item["pages"] for item in report["documents"])
    total = sum(item["seconds"]["total_after_ocr_json"] for item in report["documents"])
    model = sum(item["seconds"]["model_session_run"] for item in report["documents"])
    report["aggregate"] = {
        "documents": len(report["documents"]),
        "pages": pages,
        "chunks": sum(item["chunks"] for item in report["documents"]),
        "total_seconds": total,
        "total_ms_per_page": total * 1000.0 / pages if pages else 0.0,
        "model_session_run_seconds": model,
        "model_session_run_ms_per_page": model * 1000.0 / pages if pages else 0.0,
    }
    write_json(args.output, report)
    log(json.dumps(report["aggregate"], ensure_ascii=False))


if __name__ == "__main__":
    main()
