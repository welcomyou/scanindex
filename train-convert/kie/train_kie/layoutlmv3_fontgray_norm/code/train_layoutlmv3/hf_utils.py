from __future__ import annotations

import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np
import torch
from torch.utils.data import DataLoader, Dataset
from transformers import AutoModelForTokenClassification, AutoTokenizer, DataCollatorForTokenClassification

from train_layoutlmv3.common import (
    LABEL_LIST,
    OCRPage,
    bbox_union,
    decode_bio_spans,
    ensure_project_dirs,
    is_valid_bbox,
    load_ocr_document,
    normalize_bbox,
    read_json,
)


class TokenizedPageDataset(Dataset):
    def __init__(
        self,
        rows: list[dict[str, Any]],
        tokenizer,
        label2id: dict[str, int],
        max_length: int,
        stride: int,
        subword_label_strategy: str = "same",
    ) -> None:
        self.rows = rows
        self.features: list[dict[str, Any]] = []
        self.metadata: list[dict[str, Any]] = []
        for row_index, row in enumerate(rows):
            labels = row.get("labels") or ["O"] * len(row["tokens"])
            enc = tokenizer(
                row["tokens"],
                boxes=row["bboxes"],
                truncation=True,
                max_length=max_length,
                stride=stride,
                return_overflowing_tokens=True,
                padding=False,
            )
            for chunk_index in range(len(enc["input_ids"])):
                word_ids = enc.word_ids(batch_index=chunk_index)
                seen_words: set[int] = set()
                aligned_labels: list[int] = []
                for word_id in word_ids:
                    if word_id is None:
                        aligned_labels.append(-100)
                    elif subword_label_strategy == "first" and word_id in seen_words:
                        aligned_labels.append(-100)
                    else:
                        aligned_labels.append(label2id[labels[word_id]])
                    if word_id is not None:
                        seen_words.add(int(word_id))
                feature = {
                    "input_ids": enc["input_ids"][chunk_index],
                    "attention_mask": enc["attention_mask"][chunk_index],
                    "bbox": enc["bbox"][chunk_index],
                    "labels": aligned_labels,
                }
                if "token_type_ids" in enc:
                    feature["token_type_ids"] = enc["token_type_ids"][chunk_index]
                self.features.append(feature)
                self.metadata.append(
                    {
                        "row_index": row_index,
                        "chunk_index": chunk_index,
                        "word_ids": word_ids,
                        "doc_id": row["doc_id"],
                        "page_index": row["page_index"],
                    }
                )

    def __len__(self) -> int:
        return len(self.features)

    def __getitem__(self, index: int) -> dict[str, Any]:
        return self.features[index]


def label_maps_from_model(model_path: str | Path) -> tuple[list[str], dict[str, int], dict[int, str]]:
    model_path = Path(model_path)
    label_path = model_path / "label_list.json"
    if label_path.exists():
        label_list = read_json(label_path)
    else:
        label_list = LABEL_LIST
    label2id = {label: idx for idx, label in enumerate(label_list)}
    id2label = {idx: label for label, idx in label2id.items()}
    return label_list, label2id, id2label


def softmax_np(logits: np.ndarray) -> np.ndarray:
    logits = logits - np.max(logits, axis=-1, keepdims=True)
    exp = np.exp(logits)
    return exp / np.sum(exp, axis=-1, keepdims=True)


def aggregate_logits_to_words(
    rows: list[dict[str, Any]],
    dataset: TokenizedPageDataset,
    logits: np.ndarray,
    id2label: dict[int, str],
) -> tuple[list[list[str]], list[list[float]]]:
    probs = softmax_np(np.asarray(logits))
    row_word_probs: dict[tuple[int, int], list[np.ndarray]] = defaultdict(list)
    for chunk_index, meta in enumerate(dataset.metadata):
        for token_index, word_id in enumerate(meta["word_ids"]):
            if word_id is None:
                continue
            row_word_probs[(meta["row_index"], int(word_id))].append(probs[chunk_index, token_index])

    pred_labels: list[list[str]] = []
    pred_scores: list[list[float]] = []
    for row_index, row in enumerate(rows):
        labels: list[str] = []
        scores: list[float] = []
        for word_index in range(len(row["tokens"])):
            parts = row_word_probs.get((row_index, word_index))
            if not parts:
                labels.append("O")
                scores.append(0.0)
                continue
            mean_prob = np.mean(np.stack(parts, axis=0), axis=0)
            pred_id = int(np.argmax(mean_prob))
            labels.append(id2label[pred_id])
            scores.append(float(mean_prob[pred_id]))
        pred_labels.append(labels)
        pred_scores.append(scores)
    return pred_labels, pred_scores


def predict_pytorch(
    rows: list[dict[str, Any]],
    model_path: str | Path,
    max_length: int = 512,
    stride: int = 128,
    batch_size: int = 4,
    subword_label_strategy: str = "same",
    device: str | None = None,
) -> tuple[list[list[str]], list[list[float]], dict[str, Any]]:
    label_list, label2id, id2label = label_maps_from_model(model_path)
    tokenizer = AutoTokenizer.from_pretrained(model_path, use_fast=True)
    model = AutoModelForTokenClassification.from_pretrained(model_path)
    device = device or ("cuda" if torch.cuda.is_available() else "cpu")
    model.to(device)
    model.eval()

    dataset = TokenizedPageDataset(rows, tokenizer, label2id, max_length, stride, subword_label_strategy)
    collator = DataCollatorForTokenClassification(
        tokenizer=tokenizer,
        padding="max_length",
        max_length=max_length,
        label_pad_token_id=-100,
    )
    loader = DataLoader(dataset, batch_size=batch_size, shuffle=False, collate_fn=collator)
    logits_parts: list[np.ndarray] = []
    start = time.perf_counter()
    with torch.no_grad():
        for batch in loader:
            labels = batch.pop("labels", None)
            batch = {key: value.to(device) for key, value in batch.items()}
            outputs = model(**batch)
            logits_parts.append(outputs.logits.detach().cpu().numpy())
            if labels is not None:
                batch["labels"] = labels
    elapsed = time.perf_counter() - start
    logits = np.concatenate(logits_parts, axis=0) if logits_parts else np.zeros((0, 0, len(label_list)), dtype=np.float32)
    pred_labels, pred_scores = aggregate_logits_to_words(rows, dataset, logits, id2label)
    info = {
        "chunks": len(dataset),
        "pages": len(rows),
        "seconds": elapsed,
        "ms_per_page": elapsed * 1000.0 / len(rows) if rows else 0.0,
        "device": device,
    }
    return pred_labels, pred_scores, info


def predict_onnx(
    rows: list[dict[str, Any]],
    onnx_path: str | Path,
    model_path: str | Path,
    max_length: int = 512,
    stride: int = 128,
    batch_size: int = 1,
    warmup: int = 3,
    subword_label_strategy: str = "same",
    intra_op_num_threads: int = 0,
    inter_op_num_threads: int = 0,
    graph_optimization_level: str = "all",
    optimized_model_path: str | Path | None = None,
) -> tuple[list[list[str]], list[list[float]], dict[str, Any]]:
    import onnxruntime as ort

    _label_list, label2id, id2label = label_maps_from_model(model_path)
    tokenizer = AutoTokenizer.from_pretrained(model_path, use_fast=True)
    dataset = TokenizedPageDataset(rows, tokenizer, label2id, max_length, stride, subword_label_strategy)
    collator = DataCollatorForTokenClassification(
        tokenizer=tokenizer,
        padding="max_length",
        max_length=max_length,
        label_pad_token_id=-100,
    )
    loader = DataLoader(dataset, batch_size=batch_size, shuffle=False, collate_fn=collator)

    session_options = ort.SessionOptions()
    level_map = {
        "disable": ort.GraphOptimizationLevel.ORT_DISABLE_ALL,
        "basic": ort.GraphOptimizationLevel.ORT_ENABLE_BASIC,
        "extended": ort.GraphOptimizationLevel.ORT_ENABLE_EXTENDED,
        "all": ort.GraphOptimizationLevel.ORT_ENABLE_ALL,
    }
    session_options.graph_optimization_level = level_map[graph_optimization_level]
    if intra_op_num_threads > 0:
        session_options.intra_op_num_threads = intra_op_num_threads
    if inter_op_num_threads > 0:
        session_options.inter_op_num_threads = inter_op_num_threads
    if optimized_model_path:
        optimized_path = Path(optimized_model_path)
        optimized_path.parent.mkdir(parents=True, exist_ok=True)
        session_options.optimized_model_filepath = str(optimized_path)

    session = ort.InferenceSession(str(onnx_path), sess_options=session_options, providers=["CPUExecutionProvider"])
    input_names = {item.name for item in session.get_inputs()}

    cached_batches = []
    for batch in loader:
        batch.pop("labels", None)
        candidate = {
            "input_ids": batch["input_ids"].cpu().numpy().astype(np.int64),
            "attention_mask": batch["attention_mask"].cpu().numpy().astype(np.int64),
            "bbox": batch["bbox"].cpu().numpy().astype(np.int64),
        }
        cached_batches.append({key: value for key, value in candidate.items() if key in input_names})

    for batch in cached_batches[: max(0, warmup)]:
        session.run(None, batch)

    logits_parts: list[np.ndarray] = []
    start = time.perf_counter()
    for batch in cached_batches:
        logits_parts.append(session.run(None, batch)[0])
    elapsed = time.perf_counter() - start
    logits = np.concatenate(logits_parts, axis=0) if logits_parts else np.zeros((0, max_length, len(id2label)), dtype=np.float32)
    pred_labels, pred_scores = aggregate_logits_to_words(rows, dataset, logits, id2label)
    info = {
        "chunks": len(dataset),
        "pages": len(rows),
        "seconds": elapsed,
        "ms_per_page": elapsed * 1000.0 / len(rows) if rows else 0.0,
        "path": str(Path(onnx_path).resolve()),
        "graph_optimization_level": graph_optimization_level,
        "intra_op_num_threads": intra_op_num_threads,
        "inter_op_num_threads": inter_op_num_threads,
        "optimized_model_path": str(Path(optimized_model_path).resolve()) if optimized_model_path else None,
    }
    return pred_labels, pred_scores, info


def rows_from_canonical(canonical_json: str | Path, selected_pages: list[int] | None = None, doc_id: str | None = None) -> list[dict[str, Any]]:
    canonical_json = Path(canonical_json)
    ocr_doc = load_ocr_document(canonical_json)
    raw_doc = ocr_doc.raw.get("document", {})
    doc_id = doc_id or canonical_json.stem
    page_indices = sorted(selected_pages) if selected_pages else sorted(ocr_doc.pages)
    rows: list[dict[str, Any]] = []
    for page_index in page_indices:
        page = ocr_doc.pages.get(page_index)
        if not page:
            continue
        row = row_from_page(page, doc_id=doc_id, source_file=canonical_json)
        if row:
            row["relative_pdf_path"] = raw_doc.get("source_name") or raw_doc.get("source_path") or ""
            rows.append(row)
    return rows


def row_from_page(page: OCRPage, doc_id: str, source_file: str | Path) -> dict[str, Any] | None:
    tokens: list[str] = []
    bboxes: list[list[int]] = []
    raw_bboxes: list[list[float]] = []
    word_ids: list[str] = []
    for word in page.words:
        if not word.text.strip() or not is_valid_bbox(word.bbox):
            continue
        tokens.append(word.text)
        bboxes.append(normalize_bbox(word.bbox, page.width, page.height))
        raw_bboxes.append([float(v) for v in word.bbox])
        word_ids.append(word.id)
    if not tokens:
        return None
    return {
        "doc_id": doc_id,
        "page_id": page.page_id,
        "source_file": str(source_file),
        "label_file": None,
        "label_rel": None,
        "relative_pdf_path": "",
        "split": "inference",
        "page_index": page.page_index,
        "tokens": tokens,
        "bboxes": bboxes,
        "raw_bboxes": raw_bboxes,
        "labels": ["O"] * len(tokens),
        "word_ids": word_ids,
        "page_width": page.width,
        "page_height": page.height,
    }


def spans_to_field_output(rows: list[dict[str, Any]], labels: list[list[str]], scores: list[list[float]]) -> dict[str, list[dict[str, Any]]]:
    fields: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row, row_labels, row_scores in zip(rows, labels, scores):
        for span in decode_bio_spans(row, row_labels, row_scores):
            raw_boxes = []
            word_index = {wid: idx for idx, wid in enumerate(row["word_ids"])}
            for wid in span.get("word_ids", []):
                idx = word_index.get(wid)
                if idx is not None:
                    raw_boxes.append(row["raw_bboxes"][idx])
            fields[span["field"]].append(
                {
                    "text": span["text"],
                    "word_ids": span["word_ids"],
                    "bbox": bbox_union([tuple(box) for box in raw_boxes]),
                    "page_index": row["page_index"],
                    "confidence": span.get("confidence"),
                }
            )
    return dict(fields)
