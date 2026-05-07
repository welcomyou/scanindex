"""
OFFLINE MODEL DOWNLOADER for ScanIndex
======================================
Run ONCE after `git clone`. Populates `models/` and `drivers/`.

Models are pulled from a single Hugging Face bundle repo (default
`welcomyou/scanindex-models`) plus Google's CDN for the Chrome ScreenAI
OCR engine.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = ROOT / "models"
DRIVERS_DIR = ROOT / "drivers"

HF_REPO_DEFAULT = "welcomyou/scanindex-models"


def download_chromedriver() -> bool:
    print("\n[task] ChromeDriver -> drivers/")
    try:
        from webdriver_manager.chrome import ChromeDriverManager
    except ImportError:
        print("  -> SKIP (pip install webdriver-manager)")
        return False
    try:
        src = ChromeDriverManager().install()
        DRIVERS_DIR.mkdir(parents=True, exist_ok=True)
        dst = DRIVERS_DIR / "chromedriver.exe"
        shutil.copy2(src, dst)
        print(f"  -> {dst}")
        return True
    except Exception as e:
        print(f"  -> ERROR: {e}")
        return False


def download_model_bundle(repo_id: str) -> bool:
    print(f"\n[task] Model bundle <- {repo_id}")
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print("  -> SKIP (pip install huggingface_hub)")
        return False
    try:
        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        path = snapshot_download(
            repo_id=repo_id,
            repo_type="model",
            local_dir=str(MODELS_DIR),
            local_dir_use_symlinks=False,
        )
        print(f"  -> {path}")
        return True
    except Exception as e:
        print(f"  -> ERROR: {e}")
        return False


def download_screen_ai() -> bool:
    print("\n[task] Chrome ScreenAI <- Google CDN")
    try:
        sys.path.insert(0, str(ROOT))
        from scanindex.core.ocr.screen_ai_downloader import (
            check_screen_ai, install_screen_ai,
        )
    except Exception as e:
        print(f"  -> SKIP (cannot import downloader: {e})")
        return False
    try:
        target = MODELS_DIR / "screen_ai"
        target.mkdir(parents=True, exist_ok=True)
        status = check_screen_ai(str(target))

        def _progress(done, total):
            if total:
                pct = 100 * done / total
                print(f"\r  -> downloading {done/1e6:5.1f}/{total/1e6:5.1f} MB  {pct:5.1f}%",
                      end="", flush=True)

        lib_path, model_path, _ = install_screen_ai(
            str(target), status, progress_callback=_progress,
        )
        print(f"\n  -> {model_path}")
        return True
    except Exception as e:
        print(f"\n  -> ERROR: {e}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-id", default=HF_REPO_DEFAULT,
                        help="HF model bundle repo (default: %(default)s)")
    parser.add_argument("--skip-driver", action="store_true")
    parser.add_argument("--skip-bundle", action="store_true")
    parser.add_argument("--skip-screen-ai", action="store_true")
    args = parser.parse_args()

    print(f"Project root: {ROOT}")

    results = {}
    if not args.skip_driver:
        results["chromedriver"] = download_chromedriver()
    if not args.skip_bundle:
        results["model bundle"] = download_model_bundle(args.repo_id)
    if not args.skip_screen_ai:
        results["screen_ai"] = download_screen_ai()

    print("\nSummary:")
    for k, v in results.items():
        print(f"  {k:14s} {'OK' if v else 'FAILED'}")
    return 0 if all(results.values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
