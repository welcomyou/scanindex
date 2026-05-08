from __future__ import annotations

import argparse
import traceback
from collections import Counter, defaultdict
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from train_layoutlmv3_style.common import (
    LABEL_LIST,
    LINE_EXACT_PREFIX_BUCKETS,
    LINE_POSITION_BUCKET_COUNT,
    PROJECT_SCHEMA_VERSION,
    STYLE_BASE_TYPE_VOCAB_SIZE,
    STYLE_TYPE_VOCAB_SIZE,
    assert_page1_lightgbm_guard_available,
    build_rows_for_doc_with_style,
    dataset_paths,
    ensure_project_dirs,
    iter_label_files,
    load_ocr_document,
    load_source_context,
    now_iso,
    read_json,
    resolve_doc_meta,
    summarize_dataset_rows,
    write_json,
    write_jsonl,
    write_style_report,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build LayoutLMv3 font/gray style KIE JSONL dataset.")
    parser.add_argument("--source-root", required=True, help="Root containing labeled JSON outputs.")
    parser.add_argument("--project-root", required=True, help="Independent LayoutLMv3-style project root.")
    parser.add_argument("--limit-docs", type=int, help="Optional smoke-test limit.")
    parser.add_argument("--drop-serious-conflicts", action="store_true")
    parser.add_argument("--serious-conflict-threshold", type=int, default=10)
    parser.add_argument(
        "--include-selected-negative-pages",
        action="store_true",
        help=(
            "Also export selected_pages that have no KIE labels as all-O negative pages. "
            "Default skips them to avoid training false negatives."
        ),
    )
    parser.add_argument(
        "--no-page1-clean-negative",
        dest="include_page1_clean_negative",
        action="store_false",
        default=True,
        help=(
            "Disable the default clean negative rule: when page 0 and a labeled signer page >= 2 exist, "
            "export page 1 as an all-O body page."
        ),
    )
    parser.add_argument(
        "--no-page1-lightgbm-guard",
        dest="require_page1_not_lightgbm_signer",
        action="store_false",
        default=True,
        help=(
            "Disable the LightGBM signer-page guard for the page-1 clean negative rule. "
            "Default only exports page 1 negative when the signer_page selector does not mark it as a signer page."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    dirs = ensure_project_dirs(args.project_root)
    paths = dataset_paths(args.project_root)
    context = load_source_context(args.source_root)
    if args.include_page1_clean_negative and args.require_page1_not_lightgbm_signer:
        assert_page1_lightgbm_guard_available()
    label_files = iter_label_files(context.source_root, limit_docs=args.limit_docs)

    stats: Counter = Counter()
    rows_by_split: dict[str, list[dict]] = {"train": [], "val": [], "test": []}
    conflict_rows: list[dict] = []
    failure_rows: list[dict] = []
    docs_by_split: dict[str, set[str]] = defaultdict(set)

    for index, label_path in enumerate(label_files, start=1):
        stats["label_files_seen"] += 1
        try:
            meta = resolve_doc_meta(label_path, context)
            if not meta:
                stats["docs_missing_meta"] += 1
                failure_rows.append({"label_file": str(label_path), "error": "missing doc meta/canonical path"})
                continue
            if not meta.source_canonical_json.exists():
                stats["docs_missing_canonical"] += 1
                failure_rows.append(
                    {
                        "doc_id": meta.doc_id,
                        "label_file": str(label_path),
                        "canonical_json": str(meta.source_canonical_json),
                        "error": "canonical JSON not found",
                    }
                )
                continue
            label_payload = read_json(label_path)
            ocr_doc = load_ocr_document(meta.source_canonical_json)
            conflict_before = stats["conflict_words"]
            rows = build_rows_for_doc_with_style(
                meta,
                label_payload,
                ocr_doc,
                stats,
                conflict_rows,
                include_selected_negative_pages=args.include_selected_negative_pages,
                include_page1_clean_negative=args.include_page1_clean_negative,
                require_page1_not_lightgbm_signer=args.require_page1_not_lightgbm_signer,
            )
            doc_conflicts = stats["conflict_words"] - conflict_before
            if args.drop_serious_conflicts and doc_conflicts > args.serious_conflict_threshold:
                stats["docs_dropped_serious_conflicts"] += 1
                continue
            if not rows:
                stats["docs_skipped_no_exported_pages"] += 1
                continue
            rows_by_split[meta.split].extend(rows)
            docs_by_split[meta.split].add(meta.doc_id)
            stats["docs_built"] += 1
            stats["pages_built"] += len(rows)
            stats["words_built"] += sum(len(row["tokens"]) for row in rows)
        except Exception as exc:
            stats["docs_failed"] += 1
            failure_rows.append(
                {
                    "label_file": str(label_path),
                    "error": repr(exc),
                    "traceback": traceback.format_exc(limit=5),
                }
            )
        if index % 100 == 0:
            print(f"processed={index} docs_built={stats['docs_built']} pages={stats['pages_built']}")

    for split, rows in rows_by_split.items():
        write_jsonl(paths[split], rows)

    write_json(paths["label_list"], LABEL_LIST)
    summary = summarize_dataset_rows(rows_by_split)
    manifest = {
        "schema_version": PROJECT_SCHEMA_VERSION,
        "created_at": now_iso(),
        "source_root": str(context.source_root),
        "source_project_root": str(context.source_project_root),
        "source_manifest": str(context.manifest_path) if context.manifest_path else None,
        "project_root": str(Path(args.project_root).resolve()),
        "dataset_dir": str(paths["dataset"].resolve()),
        "label_list": LABEL_LIST,
        "label_count": len(LABEL_LIST),
        "style_feature_schema": {
            "source": "canonical OCR JSON words/lines metadata",
            "mapped_by": "exact word_id from canonical OCR to labeled word_ids",
            "fields": ["font_size", "fg_gray", "word_height", "line_ids", "confidence", "content_type"],
            "model_input": "token_type_ids",
            "style_base_type_vocab_size": STYLE_BASE_TYPE_VOCAB_SIZE,
            "line_position_bucket_count": LINE_POSITION_BUCKET_COUNT,
            "line_exact_prefix_buckets": LINE_EXACT_PREFIX_BUCKETS,
            "type_vocab_size": STYLE_TYPE_VOCAB_SIZE,
            "normalization": "font_size, fg_gray, and word_height are bucketed relative to each page median; line_ids like p0_l3 are converted to stable line-position buckets before building token_type_ids.",
            "note": "No page image/pixel input is used by this style branch.",
        },
        "splits": {
            split: {"docs": len(docs_by_split[split]), "pages": len(rows_by_split[split])}
            for split in ("train", "val", "test")
        },
        "summary": summary,
        "build_stats": dict(stats),
        "page_selection_policy": {
            "default": "only_pages_with_resolved_kie_labels",
            "include_selected_negative_pages": bool(args.include_selected_negative_pages),
            "include_page1_clean_negative": bool(args.include_page1_clean_negative),
            "page1_lightgbm_signer_guard": bool(
                args.include_page1_clean_negative and args.require_page1_not_lightgbm_signer
            ),
        },
        "notes": [
            "BIO labels still come from labeled word_ids first; bbox overlap is fallback only.",
            "By default, selected_pages without resolved KIE labels are skipped so unlabeled signer/recipient pages do not become false O labels.",
            "Use --include-selected-negative-pages only for reviewed pages that truly contain no KIE.",
            "By default, page 1 is added as one clean negative page when page 0 and a labeled signer page >= 2 are both present, and the LightGBM signer_page selector does not mark page 1 as a signer page.",
            "Style metadata is copied from canonical OCR word/line objects by exact word_id.",
            "This normalized output is intentionally separate from the first LayoutLMv3 run and the absolute-gray fontgray run.",
        ],
    }
    write_json(paths["manifest"], manifest)
    write_jsonl(dirs["logs"] / "dataset_conflicts.jsonl", conflict_rows)
    write_jsonl(dirs["logs"] / "dataset_failures.jsonl", failure_rows)
    write_style_report(args.project_root)

    print(
        {
            "docs_built": stats["docs_built"],
            "pages_built": stats["pages_built"],
            "words_built": stats["words_built"],
            "style_words": stats["style_words"],
            "missing_font_size_words": stats["missing_font_size_words"],
            "missing_fg_gray_words": stats["missing_fg_gray_words"],
            "conflict_words": stats["conflict_words"],
            "dataset_dir": str(paths["dataset"]),
        }
    )


if __name__ == "__main__":
    main()
