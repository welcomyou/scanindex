from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from train_lightgbm.common import build_paths, write_json
from train_lightgbm.training import candidate_oracle_report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Measure candidate-generation oracle ceiling.")
    parser.add_argument("--project-root", required=True, help="LightGBM project root.")
    parser.add_argument("--splits", nargs="+", default=["val", "test"], choices=["train", "val", "test"])
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = candidate_oracle_report(args.project_root, splits=args.splits)
    paths = build_paths(args.project_root)
    write_json(paths.reports_root / "candidate_oracle_report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
