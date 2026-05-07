"""
OFFLINE MODEL DOWNLOADER for ScanIndex
======================================
Run ONCE after `git clone`. Populates `models/` and `drivers/`.

Order of operations:

1. ChromeDriver  -> drivers/
2. Bundle repo   -> manifest.json (welcomyou/scanindex-models)
3. Each standalone repo from the manifest -> models/
4. Chrome ScreenAI -> models/screen_ai/  (fetched directly from Google CDN
   via scanindex.core.ocr.screen_ai_downloader; not in any HF repo)

The HF reranker `BAAI/bge-reranker-v2-m3` is fetched lazily on first use
of the Accurate search mode, not by this script.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = ROOT / "models"
DRIVERS_DIR = ROOT / "drivers"

BUNDLE_REPO_DEFAULT = "welcomyou/scanindex-models"


# ── tasks ───────────────────────────────────────────────────────────
def download_chromedriver() -> bool:
    print("\n[1/4] ChromeDriver -> drivers/")
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


def fetch_manifest(bundle_repo: str) -> dict | None:
    print(f"\n[2/4] Manifest <- {bundle_repo}")
    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        print("  -> ERROR: pip install -U huggingface_hub")
        return None
    try:
        path = hf_hub_download(
            repo_id=bundle_repo, filename="manifest.json",
            repo_type="model",
        )
        manifest = json.loads(Path(path).read_text(encoding="utf-8"))
        print(f"  -> {len(manifest.get('standalone_repos', []))} standalone repos listed")
        return manifest
    except Exception as e:
        print(f"  -> ERROR: {e}")
        return None


def download_bundle_extras(bundle_repo: str) -> bool:
    """Pull non-manifest files from the bundle repo (orientation, etc.) into models/."""
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        return False
    try:
        snapshot_download(
            repo_id=bundle_repo, repo_type="model",
            local_dir=str(MODELS_DIR),
            local_dir_use_symlinks=False,
            ignore_patterns=["manifest.json", "README.md"],
        )
        return True
    except Exception as e:
        print(f"  -> bundle extras ERROR: {e}")
        return False


def download_standalone(manifest: dict) -> tuple[int, int]:
    print("\n[3/4] Standalone model repos -> models/")
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print("  -> ERROR: pip install -U huggingface_hub")
        return 0, 0

    repos = manifest.get("standalone_repos", [])
    ok = 0
    for entry in repos:
        repo_id = entry["repo_id"]
        print(f"  - {repo_id}")
        try:
            snapshot_download(
                repo_id=repo_id, repo_type="model",
                local_dir=str(MODELS_DIR),
                local_dir_use_symlinks=False,
                ignore_patterns=["README.md", ".gitattributes"],
            )
            ok += 1
        except Exception as e:
            print(f"    ERROR: {e}")
    return ok, len(repos)


def download_screen_ai() -> bool:
    print("\n[4/4] Chrome ScreenAI <- Google CDN")
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
                print(
                    f"\r  -> {done/1e6:5.1f}/{total/1e6:5.1f} MB"
                    f"  ({100*done/total:5.1f}%)",
                    end="", flush=True,
                )

        _, model_path, _ = install_screen_ai(
            str(target), status, progress_callback=_progress,
        )
        print(f"\n  -> {model_path}")
        return True
    except Exception as e:
        print(f"\n  -> ERROR: {e}")
        return False


# ── main ────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-repo", default=BUNDLE_REPO_DEFAULT,
                        help="HF bundle repo holding manifest.json (default: %(default)s)")
    parser.add_argument("--skip-driver",     action="store_true")
    parser.add_argument("--skip-bundle",     action="store_true")
    parser.add_argument("--skip-standalone", action="store_true")
    parser.add_argument("--skip-screen-ai",  action="store_true")
    args = parser.parse_args()

    print(f"Project root: {ROOT}")
    results: dict[str, bool] = {}

    if not args.skip_driver:
        results["chromedriver"] = download_chromedriver()

    if not args.skip_bundle:
        manifest = fetch_manifest(args.bundle_repo)
        results["manifest"] = manifest is not None
        if manifest:
            results["bundle_extras"] = download_bundle_extras(args.bundle_repo)
            if not args.skip_standalone:
                ok, total = download_standalone(manifest)
                results["standalone"] = ok == total and total > 0
                print(f"  -> {ok}/{total} standalone repos OK")

    if not args.skip_screen_ai:
        results["screen_ai"] = download_screen_ai()

    print("\nSummary:")
    for k, v in results.items():
        print(f"  {k:14s} {'OK' if v else 'FAILED'}")
    return 0 if all(results.values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
