from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from train_layoutlmv3 import common as base


FIELDS = base.FIELDS
SINGLE_FIELDS = base.SINGLE_FIELDS
MULTI_FIELDS = base.MULTI_FIELDS
LABEL_LIST = base.LABEL_LIST
PROJECT_SCHEMA_VERSION = "layoutlmv3_fontgray_norm_kie_project_v2"
STYLE_TYPE_VOCAB_SIZE = 64


def now_iso() -> str:
    return base.now_iso()


def read_json(path: str | Path) -> Any:
    return base.read_json(path)


def write_json(path: str | Path, payload: Any) -> None:
    base.write_json(path, payload)


def write_jsonl(path: str | Path, rows: Iterable[dict[str, Any]]) -> int:
    return base.write_jsonl(path, rows)


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    return base.read_jsonl(path)


def project_dirs(project_root: str | Path) -> dict[str, Path]:
    root = Path(project_root)
    return {
        "root": root,
        "dataset": root / "exports" / "dataset",
        "models": root / "models",
        "reports": root / "reports",
        "logs": root / "logs",
        "onnx": root / "onnx",
    }


def ensure_project_dirs(project_root: str | Path) -> dict[str, Path]:
    dirs = project_dirs(project_root)
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def dataset_paths(project_root: str | Path) -> dict[str, Path]:
    dataset = project_dirs(project_root)["dataset"]
    return {
        "dataset": dataset,
        "train": dataset / "train.jsonl",
        "val": dataset / "val.jsonl",
        "test": dataset / "test.jsonl",
        "label_list": dataset / "label_list.json",
        "manifest": dataset / "manifest.json",
    }


def iter_label_files(source_root: str | Path, limit_docs: int | None = None) -> list[Path]:
    return base.iter_label_files(source_root, limit_docs)


def load_source_context(source_root: str | Path):
    return base.load_source_context(source_root)


def resolve_doc_meta(label_path: Path, context):
    return base.resolve_doc_meta(label_path, context)


def load_ocr_document(canonical_json: str | Path):
    return base.load_ocr_document(canonical_json)


def label_counts(rows: Iterable[dict[str, Any]]) -> Counter:
    return base.label_counts(rows)


def label_field(label: str) -> str:
    return base.label_field(label)


def percentile(values: list[int | float], pct: float) -> float:
    return base.percentile(values, pct)


def decode_bio_spans(row: dict[str, Any], labels: list[str], scores: list[float] | None = None, repair: bool = True):
    return base.decode_bio_spans(row, labels, scores, repair=repair)


def compute_kie_metrics(rows: list[dict[str, Any]], pred_labels: list[list[str]], pred_scores: list[list[float]] | None = None):
    return base.compute_kie_metrics(rows, pred_labels, pred_scores)


def apply_cardinality(spans: list[dict[str, Any]]):
    return base.apply_cardinality(spans)


def bbox_union(boxes):
    return base.bbox_union(boxes)


def normalize_bbox(box, width: float, height: float) -> list[int]:
    return base.normalize_bbox(box, width, height)


def is_valid_bbox(box) -> bool:
    return base.is_valid_bbox(box)


def load_dataset_split(project_root: str | Path, split: str, limit_docs: int | None = None) -> list[dict[str, Any]]:
    path = dataset_paths(project_root)[split]
    rows = read_jsonl(path)
    if limit_docs is None:
        return rows
    keep: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        doc_id = str(row.get("doc_id"))
        if doc_id not in keep and len(keep) >= limit_docs:
            continue
        keep.add(doc_id)
        out.append(row)
    return out


def _font_relative_group(font_size: float, median_font: float) -> int:
    if font_size <= 0 or median_font <= 0:
        return 0
    ratio = font_size / max(median_font, 1e-6)
    if ratio < 0.92:
        return 1
    if ratio > 1.08:
        return 3
    return 2


def _gray_relative_group(gray: float, median_gray: float) -> int:
    if gray < 0 or median_gray < 0:
        return 0
    delta = gray - median_gray
    if delta <= -14.0:
        return 1
    if delta >= 14.0:
        return 3
    return 2


def _height_group(height: float, median_height: float) -> int:
    if height <= 0 or median_height <= 0:
        return 0
    ratio = height / max(median_height, 1e-6)
    if ratio < 0.88:
        return 1
    if ratio > 1.12:
        return 3
    return 2


def style_type_ids(font_size: list[float], fg_gray: list[float], word_height: list[float]) -> list[int]:
    median_font = base._median_float(v for v in font_size if float(v) > 0)  # type: ignore[attr-defined]
    median_gray = base._median_float(v for v in fg_gray if 0 <= float(v) <= 255)  # type: ignore[attr-defined]
    median_height = base._median_float(v for v in word_height if float(v) > 0)  # type: ignore[attr-defined]
    ids: list[int] = []
    for fs, gray, height in zip(font_size, fg_gray, word_height):
        rel = _font_relative_group(float(fs), median_font)
        g = _gray_relative_group(float(gray), median_gray)
        h = _height_group(float(height), median_height)
        ids.append(min(STYLE_TYPE_VOCAB_SIZE - 1, rel * 16 + g * 4 + h))
    return ids


def relative_style_debug(font_size: list[float], fg_gray: list[float], word_height: list[float]) -> dict[str, Any]:
    median_font = base._median_float(v for v in font_size if float(v) > 0)  # type: ignore[attr-defined]
    median_gray = base._median_float(v for v in fg_gray if 0 <= float(v) <= 255)  # type: ignore[attr-defined]
    median_height = base._median_float(v for v in word_height if float(v) > 0)  # type: ignore[attr-defined]
    return {
        "page_median_font_size": round(float(median_font), 3),
        "page_median_fg_gray": round(float(median_gray), 3),
        "page_median_word_height": round(float(median_height), 3),
        "font_size_ratio": [
            round((float(v) / median_font), 4) if median_font > 0 and float(v) > 0 else 0.0
            for v in font_size
        ],
        "fg_gray_delta": [
            round(float(v) - median_gray, 3) if median_gray >= 0 and float(v) >= 0 else 0.0
            for v in fg_gray
        ],
        "word_height_ratio": [
            round((float(v) / median_height), 4) if median_height > 0 and float(v) > 0 else 0.0
            for v in word_height
        ],
    }


def add_layoutlmv3_style_features(row: dict[str, Any]) -> dict[str, Any]:
    row = base.add_style_features_to_row(row)
    font_size = [float(v) for v in row.get("font_size", [])]
    fg_gray = [float(v) for v in row.get("fg_gray", [])]
    word_height = [float(v) for v in row.get("word_height", [])]
    debug = relative_style_debug(font_size, fg_gray, word_height)
    out = dict(row)
    out["layoutlmv3_style_type_id"] = style_type_ids(font_size, fg_gray, word_height)
    out["page_median_font_size"] = debug["page_median_font_size"]
    out["page_median_fg_gray"] = debug["page_median_fg_gray"]
    out["page_median_word_height"] = debug["page_median_word_height"]
    out["font_size_ratio"] = debug["font_size_ratio"]
    out["fg_gray_delta"] = debug["fg_gray_delta"]
    out["word_height_ratio"] = debug["word_height_ratio"]
    return out


def build_rows_for_doc_with_style(
    meta: Any,
    label_payload: dict[str, Any],
    ocr_doc: Any,
    stats: Counter,
    conflict_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows = base.build_rows_for_doc(meta, label_payload, ocr_doc, stats, conflict_rows)
    out = [add_layoutlmv3_style_features(row) for row in rows]
    for row in out:
        ids = row.get("layoutlmv3_style_type_id", [])
        stats["style_words"] += len(ids)
        stats["style_nonzero_type_words"] += sum(1 for value in ids if int(value) != 0)
        stats["missing_font_size_words"] += sum(1 for value in row.get("font_size", []) if float(value) <= 0.0)
        stats["missing_fg_gray_words"] += sum(1 for value in row.get("fg_gray", []) if float(value) < 0.0)
    return out


def row_from_page_with_style(page: Any, doc_id: str, source_file: str | Path) -> dict[str, Any] | None:
    tokens: list[str] = []
    bboxes: list[list[int]] = []
    raw_bboxes: list[list[float]] = []
    word_ids: list[str] = []
    line_ids: list[str] = []
    font_size: list[float] = []
    fg_gray: list[float] = []
    word_height: list[float] = []
    confidence: list[float] = []
    content_type: list[int] = []
    for word in page.words:
        if not word.text.strip() or not is_valid_bbox(word.bbox):
            continue
        tokens.append(word.text)
        bboxes.append(normalize_bbox(word.bbox, page.width, page.height))
        raw_bboxes.append([float(v) for v in word.bbox])
        word_ids.append(word.id)
        line_ids.append(word.line_id)
        font_size.append(round(float(word.font_size), 3))
        fg_gray.append(round(float(word.fg_gray), 3))
        word_height.append(round(float(word.bbox[3] - word.bbox[1]), 3))
        confidence.append(round(float(word.confidence), 5))
        content_type.append(int(word.content_type))
    if not tokens:
        return None
    row = {
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
        "line_ids": line_ids,
        "font_size": font_size,
        "fg_gray": fg_gray,
        "word_height": word_height,
        "confidence": confidence,
        "content_type": content_type,
        "page_width": page.width,
        "page_height": page.height,
    }
    return add_layoutlmv3_style_features(row)


def rows_from_canonical_with_style(
    canonical_json: str | Path,
    selected_pages: list[int] | None = None,
    doc_id: str | None = None,
) -> list[dict[str, Any]]:
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
        row = row_from_page_with_style(page, doc_id=doc_id, source_file=canonical_json)
        if row:
            row["relative_pdf_path"] = raw_doc.get("source_name") or raw_doc.get("source_path") or ""
            rows.append(row)
    return rows


def summarize_dataset_rows(rows_by_split: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    summary = base.summarize_dataset_rows(rows_by_split)
    style: dict[str, Any] = {}
    for split, rows in rows_by_split.items():
        words = sum(len(row.get("tokens", [])) for row in rows)
        nonzero = sum(sum(1 for value in row.get("layoutlmv3_style_type_id", []) if int(value) != 0) for row in rows)
        missing_font = sum(sum(1 for value in row.get("font_size", []) if float(value) <= 0.0) for row in rows)
        missing_gray = sum(sum(1 for value in row.get("fg_gray", []) if float(value) < 0.0) for row in rows)
        style[split] = {
            "words": words,
            "style_nonzero_rate": nonzero / words if words else 0.0,
            "missing_font_size_words": missing_font,
            "missing_fg_gray_words": missing_gray,
        }
    summary["style"] = style
    return summary


def write_style_report(project_root: str | Path) -> None:
    dirs = ensure_project_dirs(project_root)
    manifest_path = dataset_paths(project_root)["manifest"]
    manifest = read_json(manifest_path) if manifest_path.exists() else {}
    sanity_path = dirs["reports"] / "sanity_report.json"
    sanity = read_json(sanity_path) if sanity_path.exists() else None
    training_path = dirs["reports"] / "training_summary.json"
    training = read_json(training_path) if training_path.exists() else None
    eval_reports = sorted(dirs["reports"].glob("evaluation_*.json"))
    eval_payloads = {p.stem.replace("evaluation_", ""): read_json(p) for p in eval_reports}
    onnx_path = dirs["reports"] / "onnx_export_report.json"
    onnx = read_json(onnx_path) if onnx_path.exists() else None

    lines = [
        "# LayoutLMv3 Font/Gray Normalized Style KIE Report",
        "",
        f"Generated: {now_iso()}",
        "",
        "## Feature Contract",
        "- Source: canonical OCR JSON `words` and `lines` metadata.",
        "- Fields: `font_size`, `fg_gray`, `word_height`, `confidence`, `content_type` mapped by exact `word_id`.",
        "- Model input: categorical style id in `token_type_ids`; no page image/pixel input and no sequence-length inflation.",
        "- Normalization: `font_size`, `fg_gray`, and `word_height` are bucketed relative to each page median.",
        f"- `type_vocab_size`: {STYLE_TYPE_VOCAB_SIZE}",
        "",
        "## Dataset",
    ]
    if manifest:
        lines.append(f"- Source labels: `{manifest.get('source_root', '')}`")
        lines.append(f"- Dataset dir: `{manifest.get('dataset_dir', '')}`")
        for split, stats in (manifest.get("summary", {}).get("splits") or {}).items():
            lines.append(
                f"- {split}: docs={stats.get('docs', 0)}, pages={stats.get('pages', 0)}, "
                f"words={stats.get('words', 0)}, O={stats.get('o_label_rate', 0):.3f}"
            )
        for split, stats in (manifest.get("summary", {}).get("style") or {}).items():
            lines.append(
                f"- {split} style: nonzero={stats.get('style_nonzero_rate', 0):.3f}, "
                f"missing_font={stats.get('missing_font_size_words', 0)}, "
                f"missing_gray={stats.get('missing_fg_gray_words', 0)}"
            )
    else:
        lines.append("- Pending dataset build.")
    lines.append("")
    lines.append("## Sanity")
    if sanity:
        lines.append(f"- Style length errors: {sanity.get('style_length_errors', 0)}")
        lines.append(f"- Style id count: {len(sanity.get('style_type_counts', {}))}")
        lines.append(f"- Pages over max length: {sanity.get('pages_over_max_length', 0)}")
    else:
        lines.append("- Pending sanity run.")
    lines.append("")
    lines.append("## Training")
    if training:
        cfg = training.get("config", {})
        lines.append(f"- Model: `{cfg.get('model_name', '')}`")
        lines.append(f"- Output: `{training.get('output_dir', '')}`")
        lines.append(f"- Epochs: {cfg.get('epochs')}, lr={cfg.get('learning_rate')}")
        best = training.get("best_metrics") or {}
        if best:
            lines.append(f"- Best val span F1: {best.get('eval_span_f1', best.get('span_f1', 0)):.4f}")
    else:
        lines.append("- Pending training.")
    lines.append("")
    lines.append("## Evaluation")
    if eval_payloads:
        for split, payload in eval_payloads.items():
            metrics = payload.get("metrics", payload)
            word = metrics.get("word", {}).get("overall", {})
            span = metrics.get("span", {}).get("overall", {})
            lines.append(
                f"- {split}: word_f1={word.get('f1', 0):.4f}, span_f1={span.get('f1', 0):.4f}, "
                f"exact={metrics.get('exact_instance_accuracy', 0):.4f}, "
                f"missing={metrics.get('missing_word_rate', 0):.4f}, extra={metrics.get('extra_word_rate', 0):.4f}"
            )
    else:
        lines.append("- Pending evaluation.")
    lines.append("")
    lines.append("## ONNX")
    if onnx:
        for key, item in (onnx.get("latency") or {}).items():
            lines.append(f"- {key}: {item.get('ms_per_page', 0):.2f} ms/page")
    else:
        lines.append("- Pending ONNX export.")
    lines.append("")
    lines.append("## Baseline")
    lines.append("- Compare against existing LayoutLMv3 run and LightGBM batch27 after retraining.")
    (dirs["reports"] / "layoutlmv3_fontgray_report.md").write_text("\n".join(lines), encoding="utf-8")
