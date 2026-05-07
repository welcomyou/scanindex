from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from train_lightgbm.common import utc_now_iso, write_json
from train_lightgbm.config import LABELS


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a self-contained train_kie subset for LightGBM dry runs.")
    parser.add_argument("--source-project-root", required=True, help="Existing *_kie project root.")
    parser.add_argument("--output-root", required=True, help="Target subset root.")
    parser.add_argument("--train-docs", type=int, default=14)
    parser.add_argument("--val-docs", type=int, default=3)
    parser.add_argument("--test-docs", type=int, default=3)
    parser.add_argument("--min-multipage-train", type=int, default=4)
    parser.add_argument("--min-multipage-val", type=int, default=1)
    parser.add_argument("--min-multipage-test", type=int, default=1)
    parser.add_argument("--allow-digitalpdf", action="store_true", help="Include digitalpdf docs if regular docs are insufficient.")
    parser.add_argument("--selection-json", help="Optional exact doc-id selection: {'train': [...], 'val': [...], 'test': [...]}")
    return parser.parse_args()


def _load_annotation_labels(label_output_json: Path) -> set[str]:
    payload = json.loads(label_output_json.read_text(encoding="utf-8"))
    annotation = payload.get("annotation", payload)
    return {field["label"] for field in annotation.get("field_instances", [])}


def _copy_json(src: Path, dst: Path) -> dict:
    dst.parent.mkdir(parents=True, exist_ok=True)
    payload = json.loads(src.read_text(encoding="utf-8"))
    dst.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def _select_docs(
    docs: list[dict],
    count: int,
    min_multipage: int,
) -> list[dict]:
    multipage = sorted(
        [doc for doc in docs if len(doc["selected_pages"]) > 1],
        key=lambda doc: (len(doc["selected_pages"]), doc["relative_pdf_path"]),
    )
    singlepage = sorted(
        [doc for doc in docs if len(doc["selected_pages"]) <= 1],
        key=lambda doc: (len(doc["selected_pages"]), doc["relative_pdf_path"]),
    )
    chosen: list[dict] = []
    chosen_ids: set[str] = set()
    for doc in multipage[: min(min_multipage, len(multipage))]:
        chosen.append(doc)
        chosen_ids.add(doc["doc_id"])
    for bucket in (singlepage, multipage):
        for doc in bucket:
            if len(chosen) >= count:
                break
            if doc["doc_id"] in chosen_ids:
                continue
            chosen.append(doc)
            chosen_ids.add(doc["doc_id"])
        if len(chosen) >= count:
            break
    if len(chosen) != count:
        raise RuntimeError(f"Unable to select {count} docs; only found {len(chosen)} eligible docs")
    return chosen


def main() -> None:
    args = parse_args()
    source_root = Path(args.source_project_root).resolve()
    output_root = Path(args.output_root).resolve()
    source_manifest = json.loads((source_root / "manifest.json").read_text(encoding="utf-8"))
    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "json_input").mkdir(parents=True, exist_ok=True)
    (output_root / "json_output_labeled").mkdir(parents=True, exist_ok=True)
    (output_root / "ocr").mkdir(parents=True, exist_ok=True)

    eligible_by_split: dict[str, list[dict]] = {"train": [], "val": [], "test": []}
    available_by_split: dict[str, list[dict]] = {"train": [], "val": [], "test": []}
    required_labels = set(LABELS)

    for entry in source_manifest["documents"]:
        split = entry["split"]
        doc_kind = "digitalpdf" if Path(entry["relative_pdf_path"]).parts and Path(entry["relative_pdf_path"]).parts[0].lower() == "digitalpdf" else "regular"
        if doc_kind != "regular" and not args.allow_digitalpdf:
            continue
        label_input_json = Path(entry["artifacts"]["label_input_json"])
        rel_input = label_input_json.relative_to(source_root / "json_input")
        label_output_json = source_root / "json_output_labeled" / rel_input
        if not label_output_json.exists():
            continue
        labels = _load_annotation_labels(label_output_json)
        input_payload = json.loads(label_input_json.read_text(encoding="utf-8"))
        selected_pages = input_payload.get("selected_pages") or input_payload.get("page_selection", {}).get("selected_pages") or [0]
        candidate = (
            {
                "entry": entry,
                "doc_id": entry["doc_id"],
                "relative_pdf_path": entry["relative_pdf_path"],
                "label_input_json": label_input_json,
                "label_output_json": label_output_json,
                "selected_pages": selected_pages,
            }
        )
        available_by_split[split].append(candidate)
        if required_labels.issubset(labels):
            eligible_by_split[split].append(candidate)

    if args.selection_json:
        requested = json.loads(Path(args.selection_json).read_text(encoding="utf-8"))
        selected = {}
        for split in ("train", "val", "test"):
            lookup = {doc["doc_id"]: doc for doc in available_by_split[split]}
            selected[split] = [lookup[doc_id] for doc_id in requested.get(split, [])]
    else:
        selected = {
            "train": _select_docs(eligible_by_split["train"], args.train_docs, args.min_multipage_train),
            "val": _select_docs(eligible_by_split["val"], args.val_docs, args.min_multipage_val),
            "test": _select_docs(eligible_by_split["test"], args.test_docs, args.min_multipage_test),
        }

    subset_documents = []
    selection_report = {
        "created_at": utc_now_iso(),
        "source_project_root": str(source_root),
        "output_root": str(output_root),
        "counts": {split: len(rows) for split, rows in selected.items()},
        "documents": [],
    }

    for split in ("train", "val", "test"):
        for row in selected[split]:
            entry = json.loads(json.dumps(row["entry"]))
            rel_input = row["label_input_json"].relative_to(source_root / "json_input")
            rel_output = row["label_output_json"].relative_to(source_root / "json_output_labeled")

            copied_input_path = output_root / "json_input" / rel_input
            copied_output_path = output_root / "json_output_labeled" / rel_output
            copied_input = _copy_json(row["label_input_json"], copied_input_path)
            _copy_json(row["label_output_json"], copied_output_path)

            canonical_src = Path(copied_input["source_canonical_json"])
            canonical_dst = output_root / "ocr" / canonical_src.name
            canonical_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(canonical_src, canonical_dst)
            copied_input["source_canonical_json"] = str(canonical_dst)
            copied_input_path.write_text(json.dumps(copied_input, ensure_ascii=False, indent=2), encoding="utf-8")

            entry["artifacts"]["label_input_json"] = str(copied_input_path)
            if "canonical_json" in entry["artifacts"]:
                entry["artifacts"]["canonical_json"] = str(canonical_dst)
            if "corrected_canonical_json" in entry["artifacts"]:
                entry["artifacts"]["corrected_canonical_json"] = str(canonical_dst)
            subset_documents.append(entry)
            selection_report["documents"].append(
                {
                    "split": split,
                    "doc_id": entry["doc_id"],
                    "relative_pdf_path": entry["relative_pdf_path"],
                    "selected_pages": row["selected_pages"],
                    "copied_label_input_json": str(copied_input_path),
                    "copied_label_output_json": str(copied_output_path),
                    "copied_canonical_json": str(canonical_dst),
                }
            )

    subset_manifest = {
        **source_manifest,
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
        "input_root": str(output_root / "json_input"),
        "project_root": str(output_root),
        "documents": subset_documents,
    }
    write_json(output_root / "manifest.json", subset_manifest)
    write_json(output_root / "subset_selection_report.json", selection_report)
    print(
        json.dumps(
            {
                "output_root": str(output_root),
                "doc_counts": {split: len(rows) for split, rows in selected.items()},
                "total_docs": sum(len(rows) for rows in selected.values()),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
