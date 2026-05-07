from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from train_lightgbm.common import build_paths, read_json, write_json
from train_lightgbm.training import tune_thresholds_for_output


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Tune field thresholds against decoded validation output.")
    parser.add_argument("--project-root", required=True, help="LightGBM project root.")
    parser.add_argument("--split", default="val", choices=["val", "test"])
    parser.add_argument("--max-passes", type=int, default=2)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    paths = build_paths(args.project_root)
    models_dir = paths.models_root / "fieldwise"
    base_thresholds = read_json(models_dir / "thresholds.json", default={})
    report = tune_thresholds_for_output(
        args.project_root,
        models_dir,
        base_thresholds,
        split=args.split,
        max_passes=args.max_passes,
    )
    write_json(models_dir / "thresholds.json", report["thresholds"])
    write_json(models_dir / "threshold_tuning.json", report)
    write_json(paths.reports_root / "threshold_tuning_report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
