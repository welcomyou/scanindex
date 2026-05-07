from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from train_lightgbm.dataset import export_fieldwise_dataset


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build field-wise candidate dataset for LightGBM KIE.")
    parser.add_argument("--project-root", required=True, help="LightGBM project root.")
    parser.add_argument("--include-autolabel-reports", action="store_true", help="Include _autolabel_report docs.")
    parser.add_argument("--max-workers", type=int, default=0, help="Process workers. 0 = auto.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    max_workers = None if args.max_workers == 0 else args.max_workers
    report = export_fieldwise_dataset(
        args.project_root,
        include_autolabel_reports=args.include_autolabel_reports,
        max_workers=max_workers,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
