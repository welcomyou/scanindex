from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from train_layoutlmv3_style.common import (
    FIELDS,
    LINE_POSITION_BUCKET_COUNT,
    STYLE_TYPE_VOCAB_SIZE,
    dataset_paths,
    decode_bio_spans,
    ensure_project_dirs,
    label_field,
    load_dataset_split,
    percentile,
    read_json,
    write_json,
    write_style_report,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sanity-check LayoutLMv3 font/gray style dataset.")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--max-length", type=int, default=512)
    parser.add_argument("--stride", type=int, default=128)
    parser.add_argument("--sample-docs", type=int, default=20)
    return parser.parse_args()


def chunk_ranges(word_count: int, max_length: int, stride: int) -> list[tuple[int, int]]:
    max_words = max(1, max_length - 2)
    stride = max(0, min(stride, max_words - 1))
    ranges: list[tuple[int, int]] = []
    start = 0
    while start < word_count:
        end = min(word_count, start + max_words)
        ranges.append((start, end))
        if end >= word_count:
            break
        start = end - stride
    return ranges


def span_is_contained(span: dict, ranges: list[tuple[int, int]]) -> bool:
    indices = span.get("word_indices") or []
    if not indices:
        return True
    lo = min(indices)
    hi = max(indices) + 1
    return any(start <= lo and hi <= end for start, end in ranges)


def main() -> None:
    args = parse_args()
    dirs = ensure_project_dirs(args.project_root)
    paths = dataset_paths(args.project_root)
    manifest = read_json(paths["manifest"]) if paths["manifest"].exists() else {}
    rows_by_split = {split: load_dataset_split(args.project_root, split) for split in ("train", "val", "test")}

    label_word_counts: Counter = Counter()
    field_word_counts: Counter = Counter()
    field_span_counts: Counter = Counter()
    style_type_counts: Counter = Counter()
    line_bucket_counts: Counter = Counter()
    token_lengths: list[int] = []
    pages_over_max = 0
    split_docs: dict[str, set[str]] = defaultdict(set)
    field_instances_split = 0
    style_length_errors = 0
    missing_font = 0
    missing_gray = 0
    samples: list[dict] = []
    sampled_docs: set[str] = set()

    for split, rows in rows_by_split.items():
        for row in rows:
            split_docs[split].add(row["doc_id"])
            labels = row.get("labels", [])
            tokens = row.get("tokens", [])
            style_ids = row.get("layoutlmv3_style_type_id", [])
            line_bucket_ids = row.get("layoutlmv3_line_bucket_id", [])
            token_lengths.append(len(tokens))
            label_word_counts.update(labels)
            style_type_counts.update(int(v) for v in style_ids)
            line_bucket_counts.update(int(v) for v in line_bucket_ids)
            if len(style_ids) != len(tokens):
                style_length_errors += 1
            for key in ("font_size", "fg_gray", "word_height", "line_ids", "layoutlmv3_style_base_id", "layoutlmv3_line_bucket_id"):
                if len(row.get(key, [])) != len(tokens):
                    style_length_errors += 1
            missing_font += sum(1 for v in row.get("font_size", []) if float(v) <= 0.0)
            missing_gray += sum(1 for v in row.get("fg_gray", []) if float(v) < 0.0)
            for label in labels:
                field = label_field(label)
                if field != "O":
                    field_word_counts[field] += 1
            if len(tokens) > args.max_length:
                pages_over_max += 1
            spans = decode_bio_spans(row, labels, repair=False)
            for span in spans:
                field_span_counts[span["field"]] += 1
            ranges = chunk_ranges(len(tokens), args.max_length, args.stride)
            for span in spans:
                if not span_is_contained(span, ranges):
                    field_instances_split += 1

            if row["doc_id"] not in sampled_docs and len(sampled_docs) < args.sample_docs and spans:
                sampled_docs.add(row["doc_id"])
                samples.append(
                    {
                        "split": split,
                        "doc_id": row["doc_id"],
                        "page_index": row["page_index"],
                        "fields": [
                            {"field": span["field"], "text": span["text"], "word_ids": span["word_ids"]}
                            for span in spans
                        ],
                        "first_words_style": [
                            {
                                "word_id": wid,
                                "text": tok,
                                "font_size": row.get("font_size", [])[idx],
                                "fg_gray": row.get("fg_gray", [])[idx],
                                "line_id": row.get("line_ids", [])[idx],
                                "line_bucket_id": row.get("layoutlmv3_line_bucket_id", [])[idx],
                                "style_base_id": row.get("layoutlmv3_style_base_id", [])[idx],
                                "style_type_id": row.get("layoutlmv3_style_type_id", [])[idx],
                            }
                            for idx, (wid, tok) in enumerate(zip(row.get("word_ids", []), tokens))
                        ][:40],
                    }
                )

    total_labels = sum(label_word_counts.values())
    report = {
        "project_root": str(Path(args.project_root).resolve()),
        "dataset_manifest": str(paths["manifest"]),
        "source_root": manifest.get("source_root"),
        "splits": {
            split: {
                "docs": len(split_docs[split]),
                "pages": len(rows_by_split[split]),
                "words": sum(len(row.get("tokens", [])) for row in rows_by_split[split]),
            }
            for split in ("train", "val", "test")
        },
        "label_counts": dict(label_word_counts),
        "field_word_counts": {field: field_word_counts.get(field, 0) for field in FIELDS},
        "field_span_counts": {field: field_span_counts.get(field, 0) for field in FIELDS},
        "o_label_rate": label_word_counts.get("O", 0) / total_labels if total_labels else 0.0,
        "token_length_distribution": {
            "min": min(token_lengths) if token_lengths else 0,
            "p50": percentile(token_lengths, 0.50),
            "p90": percentile(token_lengths, 0.90),
            "p95": percentile(token_lengths, 0.95),
            "p99": percentile(token_lengths, 0.99),
            "max": max(token_lengths) if token_lengths else 0,
        },
        "style_type_vocab_size": STYLE_TYPE_VOCAB_SIZE,
        "style_type_counts": dict(style_type_counts),
        "line_position_bucket_count": LINE_POSITION_BUCKET_COUNT,
        "line_bucket_counts": dict(line_bucket_counts),
        "style_length_errors": style_length_errors,
        "missing_font_size_words": missing_font,
        "missing_fg_gray_words": missing_gray,
        "pages_over_max_length": pages_over_max,
        "field_instances_split_by_chunks": field_instances_split,
        "max_length": args.max_length,
        "stride": args.stride,
        "sample_output": str((dirs["reports"] / "sanity_samples.json").resolve()),
    }
    write_json(dirs["reports"] / "sanity_report.json", report)
    write_json(dirs["reports"] / "sanity_samples.json", samples)
    write_style_report(args.project_root)

    print(
        {
            "splits": report["splits"],
            "o_label_rate": round(report["o_label_rate"], 4),
            "style_length_errors": style_length_errors,
            "style_type_ids": len(style_type_counts),
            "line_bucket_ids": len(line_bucket_counts),
            "missing_font_size_words": missing_font,
            "missing_fg_gray_words": missing_gray,
            "pages_over_max_length": pages_over_max,
        }
    )


if __name__ == "__main__":
    main()
