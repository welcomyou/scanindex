from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from train_lightgbm.common import build_paths, read_json, write_json
from train_lightgbm.training import evaluate_split


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate field-wise LightGBM models.")
    parser.add_argument("--project-root", required=True, help="LightGBM project root.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    paths = build_paths(args.project_root)
    thresholds = read_json(paths.models_root / "fieldwise" / "thresholds.json", default={})
    report = {
        "val": evaluate_split(args.project_root, paths.models_root / "fieldwise", thresholds, "val"),
        "test": evaluate_split(args.project_root, paths.models_root / "fieldwise", thresholds, "test"),
    }
    write_json(paths.reports_root / "eval_report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
