from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from train_kie.common import build_paths, load_manifest
from train_kie.exporters import export_lilt_phobert, export_lilt_xlmr, export_paddle_kie


def ground_truth_paths_for_split(manifest: dict, split: str, include_review: bool) -> list[str]:
    accepted = {"done"}
    if include_review:
        accepted.add("needs_review")
    paths = []
    for entry in manifest.get("documents", []):
        if entry.get("split") != split:
            continue
        if entry.get("status", {}).get("ground_truth") not in accepted:
            continue
        gt = entry.get("artifacts", {}).get("ground_truth_json")
        if gt and Path(gt).exists():
            paths.append(gt)
    return paths


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Export consensus canonical JSON into train-ready datasets.\n\n"
            "Dryrun-20 smoke test (run both LiLT+XLMR and LiLT+PhoBERT quickly on\n"
            "~20 docs to verify the pipeline works end-to-end without waiting for\n"
            "the full dataset):\n\n"
            "  python 5-export_training_sets.py \\\n"
            "      --project-root <PROJECT_ROOT> \\\n"
            "      --tracks lilt_xlmr lilt_phobert \\\n"
            "      --limit-train 16 --limit-val 2 --limit-test 2\n\n"
            "This writes 20 docs total (16/2/2) into exports/, which is what\n"
            "train_kie/dryrun/{xlmr,phobert}/*.sh expect when you want an actual\n"
            "'dryrun on 20 docs'. PhoBERT track always uses the underthesea word\n"
            "segmenter (default; matches how PhoBERT was pretrained).\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--project-root", required=True)
    parser.add_argument(
        "--segmenter", default="underthesea",
        choices=["whitespace", "underthesea"],
        help="Word segmenter for the PhoBERT track. Default underthesea (required for PhoBERT).",
    )
    parser.add_argument("--render-dpi", type=int, default=200)
    parser.add_argument("--include-review", action="store_true")
    parser.add_argument(
        "--tracks",
        nargs="+",
        default=["lilt_xlmr", "lilt_phobert", "paddle_kie"],
        choices=["lilt_xlmr", "lilt_phobert", "paddle_kie"],
        help="Subset of export tracks to generate.",
    )
    parser.add_argument(
        "--limit-train", type=int, default=None,
        help="Export only the first N train docs (for dryrun20 smoke test).",
    )
    parser.add_argument(
        "--limit-val", type=int, default=None,
        help="Export only the first N val docs (for dryrun20 smoke test).",
    )
    parser.add_argument(
        "--limit-test", type=int, default=None,
        help="Export only the first N test docs (for dryrun20 smoke test).",
    )
    args = parser.parse_args()

    paths = build_paths(args.project_root)
    manifest = load_manifest(paths)
    selected_tracks = set(args.tracks)

    split_limits = {
        "train": args.limit_train,
        "val": args.limit_val,
        "test": args.limit_test,
    }

    for split in ["train", "val", "test"]:
        input_paths = ground_truth_paths_for_split(manifest, split, include_review=args.include_review)
        if not input_paths:
            continue
        limit = split_limits.get(split)
        if limit is not None and limit >= 0:
            input_paths = input_paths[:limit]

        if "lilt_xlmr" in selected_tracks:
            export_lilt_xlmr(
                input_paths,
                paths.exports_root / "lilt_xlmr" / f"{split}.jsonl",
            )
        if "lilt_phobert" in selected_tracks:
            export_lilt_phobert(
                input_paths,
                paths.exports_root / "lilt_phobert" / f"{split}.jsonl",
                segmenter_mode=args.segmenter,
            )
        if "paddle_kie" in selected_tracks:
            export_paddle_kie(
                input_paths,
                paths.exports_root / "paddle_kie" / split,
                render_dpi=args.render_dpi,
            )
        print(f"EXPORTED {split}: {len(input_paths)} documents")


if __name__ == "__main__":
    main()

