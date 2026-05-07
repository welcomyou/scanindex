from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from train_kie.common import (
    build_paths,
    ensure_project_layout,
    iter_documents,
    label_task_stem,
    load_manifest,
    read_json,
    save_manifest,
    write_json,
)
from train_kie.labeling_workspace import build_label_task_from_file, build_labeling_readme

BATCH_SIZE = 100


def _label_input_sort_key(entry: dict) -> tuple[str, str, str]:
    rel_path = Path(entry["relative_pdf_path"].replace("\\", "/"))
    return (rel_path.name.lower(), str(rel_path).lower(), entry["doc_id"])


def _batched_label_input_path(paths, entry: dict, sorted_index: int) -> tuple[Path, str]:
    batch_number = (sorted_index // BATCH_SIZE) + 1
    batch_name = f"batch_{batch_number:04d}"
    output_path = paths.json_input_root / batch_name / f"{label_task_stem(entry)}.json"
    return output_path, batch_name


def _ensure_stage_guides(paths) -> None:
    guide_text = (
        "ÄÃ¢y lÃ  thÆ° má»¥c output LLM duy nháº¥t. "
        "Má»—i file JSON á»Ÿ Ä‘Ã¢y pháº£i cÃ¹ng tÃªn vá»›i file trong json_input vÃ  lÃ  annotation cuá»‘i cÃ¹ng."
    )
    (paths.json_output_labeled_root / "README.md").write_text(guide_text + "\n", encoding="utf-8")


def _reset_json_input_root(paths) -> None:
    for child in paths.json_input_root.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def _ensure_output_batch_dir(paths, batch_name: str) -> None:
    (paths.json_output_labeled_root / batch_name).mkdir(parents=True, exist_ok=True)


def _ensure_stage_guides(paths) -> None:
    guide_text = (
        "This is the single labeled output folder. "
        "Write each output JSON into the matching batch subfolder under json_output_labeled/, "
        "using the same filename as its input JSON."
    )
    (paths.json_output_labeled_root / "README.md").write_text(guide_text + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Prepare local JSON labeling tasks for human/LLM agents.")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--split", choices=["train", "val", "test"])
    parser.add_argument("--subdir")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--signator-csv", help="CSV with PathFile and Signator columns for confidence boosting")
    args = parser.parse_args()

    signator_map: dict[str, str] = {}
    if args.signator_csv:
        import csv

        with open(args.signator_csv, "r", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                pdf_name = (row.get("PathFile") or "").strip()
                signator = (row.get("Signator") or "").strip()
                if pdf_name and signator:
                    signator_map[pdf_name] = signator
        print(f"Loaded {len(signator_map)} signator entries from CSV")

    paths = build_paths(args.project_root)
    ensure_project_layout(paths)
    if args.force:
        _reset_json_input_root(paths)
    manifest = load_manifest(paths)
    selected = iter_documents(manifest, split=args.split, subdir=args.subdir, limit=None)
    selected = sorted(selected, key=_label_input_sort_key)
    if args.limit is not None:
        selected = selected[:args.limit]

    prepared = 0
    page_selection_rows = []
    guide_path = paths.json_input_root / "README.md"
    guide_path.write_text(
        build_labeling_readme()
        + "\n## Batch layout\n"
        + f"- Files are sorted A-Z by PDF filename, then written into batch folders of {BATCH_SIZE} files each.\n"
        + "- Example: `json_input/batch_0001/`, `json_input/batch_0002/`, ...\n",
        encoding="utf-8",
    )
    _ensure_stage_guides(paths)

    for sorted_index, entry in enumerate(selected):
        canonical_json = Path(entry["artifacts"]["canonical_json"])
        output_path, batch_name = _batched_label_input_path(paths, entry, sorted_index)
        _ensure_output_batch_dir(paths, batch_name)
        if entry["status"].get("ocr") != "done" or not canonical_json.exists():
            print(f"SKIP INPUT {entry['relative_pdf_path']} (missing OCR)")
            continue

        if output_path.exists() and not args.force:
            entry["artifacts"]["label_input_json"] = str(output_path.resolve())
            entry["meta"]["label_input_batch"] = batch_name
            task_payload = read_json(output_path, default={}) or {}
            selection = task_payload.get("page_selection", {})
            entry["meta"]["page_selection_confidence"] = selection.get("confidence")
            entry["meta"]["page_selection_strategy"] = selection.get("strategy")
            page_selection_rows.append({
                "doc_id": entry["doc_id"],
                "relative_pdf_path": entry["relative_pdf_path"],
                "batch": batch_name,
                "selected_pages": list(task_payload.get("selected_pages") or []),
                "confidence": selection.get("confidence"),
                "strategy": selection.get("strategy"),
                "needs_review": bool(selection.get("needs_review")),
                "review_reasons": list(selection.get("review_reasons") or []),
                "signature_page": selection.get("signature_page"),
                "signator_boost": selection.get("signator_boost"),
                "candidates": list(selection.get("candidates") or []),
            })
            continue

        pdf_name = Path(entry["relative_pdf_path"]).name
        task_payload = build_label_task_from_file(
            str(canonical_json),
            doc_id=entry["doc_id"],
            relative_pdf_path=entry["relative_pdf_path"],
            signator_name=signator_map.get(pdf_name),
        )
        write_json(output_path, task_payload)
        entry["artifacts"]["label_input_json"] = str(output_path.resolve())
        entry["meta"]["label_input_batch"] = batch_name
        selection = task_payload.get("page_selection", {})
        entry["meta"]["page_selection_confidence"] = selection.get("confidence")
        entry["meta"]["page_selection_strategy"] = selection.get("strategy")
        page_selection_rows.append({
            "doc_id": entry["doc_id"],
            "relative_pdf_path": entry["relative_pdf_path"],
            "batch": batch_name,
            "selected_pages": list(task_payload.get("selected_pages") or []),
            "confidence": selection.get("confidence"),
            "strategy": selection.get("strategy"),
            "needs_review": bool(selection.get("needs_review")),
            "review_reasons": list(selection.get("review_reasons") or []),
            "signature_page": selection.get("signature_page"),
            "signator_boost": selection.get("signator_boost"),
            "candidates": list(selection.get("candidates") or []),
        })
        prepared += 1

    write_json(paths.logs / "page_selection_report.json", {
        "documents": len(page_selection_rows),
        "needs_review": sum(1 for row in page_selection_rows if row["needs_review"]),
        "rows": page_selection_rows,
    })
    save_manifest(paths, manifest)
    print(f"PREPARED {prepared} label input file(s)")
    print(f"JSON INPUT DIR: {paths.json_input_root}")
    print(f"PAGE SELECTION REPORT: {paths.logs / 'page_selection_report.json'}")


if __name__ == "__main__":
    main()

