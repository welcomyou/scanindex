from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from train_kie.common import build_manifest, build_paths, write_json
from train_kie.ontology import FIELDS, ONTOLOGY_ID, RELATION_TYPES


def main():
    parser = argparse.ArgumentParser(description="Initialize a KIE training project workspace.")
    parser.add_argument("--input-root", required=True, help="Root folder containing source PDFs.")
    parser.add_argument("--project-root", required=True, help="Workspace folder to create or refresh.")
    args = parser.parse_args()

    manifest = build_manifest(args.input_root, args.project_root)
    paths = build_paths(args.project_root)
    write_json(paths.root / "ontology.json", {
        "ontology_id": ONTOLOGY_ID,
        "fields": FIELDS,
        "relation_types": RELATION_TYPES,
    })

    split_counts = {}
    for doc in manifest["documents"]:
        split_counts[doc["split"]] = split_counts.get(doc["split"], 0) + 1

    print(json.dumps({
        "project_root": str(paths.root),
        "documents": len(manifest["documents"]),
        "splits": split_counts,
        "ontology_id": ONTOLOGY_ID,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()


