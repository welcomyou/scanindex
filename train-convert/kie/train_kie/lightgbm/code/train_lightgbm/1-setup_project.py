from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from train_lightgbm.common import build_paths, ensure_project_layout, write_json
from train_lightgbm.dataset import build_lightgbm_manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create LightGBM KIE project manifest from existing train_kie project.")
    parser.add_argument("--source-project-root", required=True, help="Existing *_kie project root.")
    parser.add_argument("--project-root", required=True, help="Target LightGBM project root.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    paths = build_paths(args.project_root)
    ensure_project_layout(paths)
    manifest = build_lightgbm_manifest(args.source_project_root, args.project_root)
    write_json(paths.manifest, manifest)
    print(f"Created manifest with {len(manifest['documents'])} docs at {paths.manifest}")


if __name__ == "__main__":
    main()
