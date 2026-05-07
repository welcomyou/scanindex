"""
Upload local `models/` to a Hugging Face Hub model repo.

Run ONCE on a machine that has all models downloaded locally.

Prerequisites:
    pip install huggingface_hub
    huggingface-cli login    # paste a WRITE token

Usage:
    python scripts/upload_models_to_hf.py
    python scripts/upload_models_to_hf.py --repo-id <user>/<repo>
    python scripts/upload_models_to_hf.py --private        # make repo private
    python scripts/upload_models_to_hf.py --include-screen-ai

`screen_ai/` is excluded by default — Chrome ScreenAI may have
redistribution restrictions and is fetched at runtime from Google CDN
by `scanindex.core.ocr.screen_ai_downloader`.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_DEFAULT = "welcomyou/scanindex-models"
ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = ROOT / "models"

HF_README = """\
---
license: other
tags:
  - ocr
  - vietnamese
  - kie
  - layout-analysis
  - table-extraction
language:
  - vi
---

# ScanIndex model bundle

Trained / converted weights for [welcomyou/scanindex](https://github.com/welcomyou/scanindex).
The application code expects this bundle to be unpacked under `models/` at the
project root. `scripts/download_offline_models.py` does this automatically.

## Contents

| Subdir | Purpose | Source |
|---|---|---|
| `distilled_ct2/` | Vietnamese text-correction (CTranslate2) | Distilled from protonx-models, beam=1 |
| `doclayout_yolo_onnx_dynamic/` | Page layout — DocStructBench | Re-exported ONNX (dynamic axes) of `juliozhao/DocLayout-YOLO-DocStructBench` |
| `doclayout_yolo_doclaynet_onnx_dynamic/` | Page layout — DocLayNet variant | Re-exported ONNX (dynamic axes) |
| `docling_tableformer_v1_stepcache_onnx/` | Table structure recognition (TableFormer v1) | Re-exported ONNX from Docling |
| `gmft_onnx/` | Table detection + structure (TATR) | Re-exported ONNX of Microsoft Table Transformer |
| `layoutlmv3_fontgray_norm_final_epoch25/` | KIE on Vietnamese admin docs | Fine-tuned LayoutLMv3 (project-specific) |
| `archive_models/e5-small-mix50-v2-onnx-fp32/` | Multilingual passage embedder for Kho lưu trữ search | Fine-tuned E5-small (project-specific) |
| `orientation/` | 4-angle page orientation classifier | PaddleOCR `PP-LCNet_x1_0_doc_ori` (Apache-2.0) |
| `lightgbm_splitter/` | Page-split / signer-page classifier | LightGBM trained on project data |

## Not included

- **`screen_ai/`** — Google Chrome ScreenAI OCR engine. Fetched at runtime
  directly from Google's CDN via `scanindex.core.ocr.screen_ai_downloader` —
  this honours the upstream license terms.

## Licenses

Each subdirectory inherits the license of its upstream source:

- `juliozhao/DocLayout-YOLO-DocStructBench` — Apache-2.0
- Microsoft Table Transformer (TATR) — MIT
- Docling TableFormer — MIT
- PaddleOCR PP-LCNet — Apache-2.0
- LayoutLMv3 base — CC-BY-NC-SA-4.0 (research); fine-tuned weights inherit this
- E5-small base — MIT
- Project-specific fine-tunes (LayoutLMv3, E5, LightGBM) — same as upstream base unless stated otherwise

If you plan to use this bundle commercially, audit each subdirectory's license.

## Loading

```python
from huggingface_hub import snapshot_download
snapshot_download(
    repo_id="{REPO_ID}",
    local_dir="models",
    local_dir_use_symlinks=False,
)
```
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-id", default=REPO_DEFAULT)
    parser.add_argument("--private", action="store_true",
                        help="Create the HF repo as private (default: public)")
    parser.add_argument("--include-screen-ai", action="store_true",
                        help="Also upload models/screen_ai/ (check license first!)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the upload plan without creating/pushing anything")
    args = parser.parse_args()

    try:
        from huggingface_hub import HfApi, create_repo
    except ImportError:
        print("ERROR: pip install huggingface_hub", file=sys.stderr)
        return 1

    if not MODELS_DIR.is_dir():
        print(f"ERROR: {MODELS_DIR} not found", file=sys.stderr)
        return 1

    ignore_patterns = [
        "*.pyc", "__pycache__/*",
        ".cache/*", ".locks/*",
        "*.tmp", "*.partial",
    ]
    if not args.include_screen_ai:
        ignore_patterns.append("screen_ai/*")

    print(f"Repo:        {args.repo_id}")
    print(f"Visibility:  {'private' if args.private else 'public'}")
    print(f"Source dir:  {MODELS_DIR}")
    print(f"Ignored:     {ignore_patterns}")

    if args.dry_run:
        print("\n(dry-run) Skipping create_repo + upload_folder.")
        return 0

    api = HfApi()
    create_repo(args.repo_id, repo_type="model", exist_ok=True,
                private=args.private)

    readme_path = ROOT / ".hf_readme.tmp.md"
    readme_path.write_text(HF_README.replace("{REPO_ID}", args.repo_id),
                           encoding="utf-8")
    try:
        api.upload_file(
            path_or_fileobj=str(readme_path),
            path_in_repo="README.md",
            repo_id=args.repo_id,
            repo_type="model",
            commit_message="docs: README for ScanIndex model bundle",
        )
    finally:
        readme_path.unlink(missing_ok=True)

    api.upload_folder(
        folder_path=str(MODELS_DIR),
        repo_id=args.repo_id,
        repo_type="model",
        commit_message="upload ScanIndex model bundle",
        ignore_patterns=ignore_patterns,
    )

    print(f"\nDone. https://huggingface.co/{args.repo_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
