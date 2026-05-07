from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from train_kie.common import (
    artifact_label_output,
    artifact_raw_label,
    build_paths,
    iter_documents,
    load_manifest,
    save_manifest,
    write_json,
)
from train_kie.labeling_workspace import load_external_label_output


OUTPUT_STAGE = "labeled"


def _import_labeled_output(paths, entry, required=False):
    external_output_path = artifact_label_output(paths, OUTPUT_STAGE, entry)
    if not external_output_path.exists():
        if required:
            raise FileNotFoundError(f"Missing required labeled output: {external_output_path}")
        return None

    normalized_payload = load_external_label_output(
        str(external_output_path),
        entry["artifacts"]["canonical_json"],
        llm_name=OUTPUT_STAGE,
    )
    raw_output_path = artifact_raw_label(paths, OUTPUT_STAGE, entry)
    write_json(raw_output_path, normalized_payload)
    return {
        "external_path": str(external_output_path.resolve()),
        "raw_path": str(raw_output_path.resolve()),
        "payload": normalized_payload,
    }


def _write_review_summary(paths, entry, chosen_source: str, labeled_info: dict):
    review_path = paths.review_root / f"{entry['doc_id']}.json"
    write_json(review_path, {
        "doc_id": entry["doc_id"],
        "relative_pdf_path": entry["relative_pdf_path"],
        "chosen_source": chosen_source,
        "labeled_source": labeled_info["external_path"],
        "needs_final_review": False,
    })


def main():
    parser = argparse.ArgumentParser(description="Import final labeled JSON output into canonical ground truth.")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--split", choices=["train", "val", "test"])
    parser.add_argument("--subdir")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--strict", action="store_true", help="Raise immediately if one label output is invalid.")
    args = parser.parse_args()

    paths = build_paths(args.project_root)
    manifest = load_manifest(paths)
    selected = iter_documents(manifest, split=args.split, subdir=args.subdir, limit=args.limit)

    for entry in selected:
        llm_status = entry["status"].setdefault("llm_labels", {})
        labels_artifacts = entry["artifacts"].setdefault("labels", {})

        try:
            labeled_info = _import_labeled_output(paths, entry, required=False)
            if labeled_info is None:
                continue
            llm_status.clear()
            llm_status[OUTPUT_STAGE] = "done"
            labels_artifacts.clear()
            labels_artifacts[OUTPUT_STAGE] = labeled_info["raw_path"]
        except Exception as exc:
            llm_status[OUTPUT_STAGE] = "failed"
            entry["status"]["ground_truth"] = "pending"
            entry["meta"]["last_error"] = str(exc)
            print(f"FAILED IMPORT {entry['relative_pdf_path']} / {OUTPUT_STAGE}: {exc}")
            save_manifest(paths, manifest)
            if args.strict:
                raise
            continue

        canonical_doc = Path(entry["artifacts"]["canonical_json"])
        from train_kie.adjudication import inject_consensus_into_canonical

        consensus = {
            "annotation": labeled_info["payload"]["annotation"],
            "conflicts": [],
        }
        consensus_doc = inject_consensus_into_canonical(str(canonical_doc), consensus)
        output_path = Path(entry["artifacts"]["ground_truth_json"])
        write_json(output_path, consensus_doc)
        _write_review_summary(paths, entry, OUTPUT_STAGE, labeled_info)

        entry["status"]["ground_truth"] = "done"
        entry["meta"]["ground_truth_source"] = OUTPUT_STAGE
        entry["meta"]["last_error"] = None
        save_manifest(paths, manifest)
        print(f"GROUNDTRUTH {entry['relative_pdf_path']} -> done via {OUTPUT_STAGE}")


if __name__ == "__main__":
    main()

