#!/usr/bin/env python3
"""
Recompute SHA256 hashes for every runtime-critical model file and emit a
fresh MODELS_CONFIG dict suitable for pasting into
scripts/download_offline_models.py.

Run this once after a retrain + re-upload so the hardcoded supply-chain
anchor in download_offline_models.py reflects the new HF state.

Usage:
    python scripts/refresh_model_hashes.py        # print to stdout
    python scripts/refresh_model_hashes.py --apply  # rewrite the section
                                                    # in download_offline_models.py
                                                    # in place

Strategy:
- For each HF repo, fetch the current commit SHA via HfApi().repo_info().
- For each runtime file expected at models/<local_dir>/..., compute SHA256
  from disk (so the source of truth is what's actually on the user's
  machine and was uploaded).
- Skip optional files (README.md, .gitattributes, *.safetensors) so the
  hardcoded list only covers files the runtime actually loads.

Failure modes are loud: a missing file → ValueError; HF unreachable →
SystemExit. Better to fail than to silently emit an incomplete pin.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = ROOT / "models"
DOWNLOAD_SCRIPT = ROOT / "scripts" / "download_offline_models.py"


# Single source of truth: what each repo holds, mirroring local layout.
# Sources are paths under models/. Excluded patterns are regex strings.
REPOS = [
    {
        "model_id": "layoutlmv3-vn-admin-kie",
        "repo_id":  "welcomyou/layoutlmv3-vn-admin-kie",
        "type":     "huggingface",
        "local_dir": "layoutlmv3_fontgray_norm_final_epoch25",
        "description": "LayoutLMv3 KIE (Vietnamese admin docs, ONNX int8)",
    },
    {
        "model_id": "distilled-protonx-vn-correction-ct2",
        "repo_id":  "welcomyou/distilled-protonx-vn-correction-ct2",
        "type":     "huggingface",
        "local_dir": "distilled_ct2",
        "description": "distilled-protonx-legal-tc CT2 INT8 (Vietnamese OCR correction)",
    },
    {
        "model_id": "lightgbm-vn-page-splitter",
        "repo_id":  "welcomyou/lightgbm-vn-page-splitter",
        "type":     "huggingface",
        "local_dir": "lightgbm_splitter",
        "description": "LightGBM page splitter (doc_start + signer_page)",
    },
    {
        "model_id": "doclayout-yolo-onnx-dynamic",
        "repo_id":  "welcomyou/doclayout-yolo-onnx-dynamic",
        "type":     "huggingface",
        # 2 dirs in one repo; treated as a single snapshot
        "local_dirs": ["doclayout_yolo_onnx_dynamic",
                       "doclayout_yolo_doclaynet_onnx_dynamic"],
        "description": "DocLayout-YOLO ONNX dynamic-axes (DocStructBench + DocLayNet)",
    },
    {
        "model_id": "gmft-tatr-onnx",
        "repo_id":  "welcomyou/gmft-tatr-onnx",
        "type":     "huggingface",
        "local_dir": "gmft_onnx",
        "description": "GMFT-TATR ONNX (table detection + structure)",
    },
    {
        "model_id": "docling-tableformer-v1-onnx-stepcache",
        "repo_id":  "welcomyou/docling-tableformer-v1-onnx-stepcache",
        "type":     "huggingface",
        "local_dir": "docling_tableformer_v1_stepcache_onnx",
        "description": "Docling TableFormer v1 ONNX (stepcache decoder)",
    },
    {
        "model_id": "orientation-paddleocr",
        "repo_id":  "welcomyou/scanindex-models",
        "type":     "huggingface",
        "local_dir": "orientation",
        "description": "PaddleOCR PP-LCNet 4-way page-orientation classifier",
    },
]


# Files we do NOT pin (training artifacts, repo metadata, transient docs).
EXCLUDE_PATTERNS = [
    r"^\.gitattributes$",
    r"^README\.md$",
    r".*\.safetensors$",
    r".*\.pt$",
    r".*\.ckpt$",
    r"^optimizer\.pt$",
    r"^scheduler\.pt$",
    r"^training_args\.bin$",
    r"^rng_state\.pth$",
    r"^trainer_state\.json$",
    r"^manifest\.json$",     # bundle's manifest is not load-bearing now
    r".*/\.cache/.*",
    r".*/\.locks/.*",
    r".*\.tmp$",
    r".*\.partial$",
    r"^__pycache__/.*",
]
_EXCLUDES = [re.compile(p) for p in EXCLUDE_PATTERNS]


def _excluded(rel: str) -> bool:
    rel_norm = rel.replace("\\", "/")
    return any(rx.search(rel_norm) for rx in _EXCLUDES)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as h:
        for chunk in iter(lambda: h.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_subdir(subdir: Path, prefix: str) -> dict[str, str]:
    """Hash every non-excluded file under `subdir`. Keys are paths relative
    to MODELS_DIR (so they match local install layout exactly)."""
    if not subdir.is_dir():
        raise FileNotFoundError(f"Local dir missing for hashing: {subdir}")
    out: dict[str, str] = {}
    for f in sorted(subdir.rglob("*")):
        if not f.is_file():
            continue
        rel_inside = f.relative_to(subdir).as_posix()
        if _excluded(rel_inside):
            continue
        rel_full = f"{prefix}/{rel_inside}".replace("//", "/")
        out[rel_full] = _sha256_file(f)
    return out


def _hf_revision(repo_id: str) -> str:
    from huggingface_hub import HfApi
    api = HfApi()
    info = api.repo_info(repo_id, repo_type="model")
    sha = getattr(info, "sha", None)
    if not sha:
        raise RuntimeError(f"Could not read HEAD revision for {repo_id}")
    return sha


def build_models_config() -> list[dict]:
    config = []
    for entry in REPOS:
        rev = _hf_revision(entry["repo_id"])
        files: dict[str, str] = {}
        if "local_dirs" in entry:
            for d in entry["local_dirs"]:
                files.update(_hash_subdir(MODELS_DIR / d, d))
            sources = entry["local_dirs"]
        else:
            d = entry["local_dir"]
            files.update(_hash_subdir(MODELS_DIR / d, d))
            sources = [d]
        config.append({
            "model_id":    entry["model_id"],
            "repo_id":     entry["repo_id"],
            "type":        entry["type"],
            "sources":     sources,
            "revision":    rev,
            "description": entry["description"],
            "integrity_files": files,
        })
    return config


def render_python(config: list[dict]) -> str:
    lines = ["MODELS_CONFIG = ["]
    for c in config:
        lines.append("    {")
        lines.append(f'        "model_id":    {c["model_id"]!r},')
        lines.append(f'        "repo_id":     {c["repo_id"]!r},')
        lines.append(f'        "type":        {c["type"]!r},')
        lines.append(f'        "sources":     {c["sources"]!r},')
        lines.append(f'        "revision":    {c["revision"]!r},')
        lines.append(f'        "description": {c["description"]!r},')
        lines.append(f'        "integrity_files": {{')
        for path, sha in c["integrity_files"].items():
            lines.append(f'            {path!r}: {sha!r},')
        lines.append("        },")
        lines.append("    },")
    lines.append("]")
    return "\n".join(lines)


_MARK_BEGIN = "# ── BEGIN MODELS_CONFIG (auto-generated by refresh_model_hashes.py) ──"
_MARK_END   = "# ── END MODELS_CONFIG ──"


def apply_in_place(rendered: str) -> None:
    text = DOWNLOAD_SCRIPT.read_text(encoding="utf-8")
    if _MARK_BEGIN not in text or _MARK_END not in text:
        raise SystemExit(
            f"{DOWNLOAD_SCRIPT}: missing block markers\n"
            f"  {_MARK_BEGIN}\n  ...\n  {_MARK_END}\n"
            "Add them around the existing MODELS_CONFIG before running --apply."
        )
    pattern = re.compile(
        re.escape(_MARK_BEGIN) + r".*?" + re.escape(_MARK_END),
        re.DOTALL,
    )
    new_block = f"{_MARK_BEGIN}\n{rendered}\n{_MARK_END}"
    new_text = pattern.sub(new_block, text)
    DOWNLOAD_SCRIPT.write_text(new_text, encoding="utf-8")
    print(f"[ok] Rewrote {DOWNLOAD_SCRIPT.relative_to(ROOT)}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true",
                   help="Rewrite scripts/download_offline_models.py in place")
    args = p.parse_args()

    print(f"[refresh] models dir: {MODELS_DIR}", file=sys.stderr)
    config = build_models_config()
    rendered = render_python(config)

    if args.apply:
        apply_in_place(rendered)
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
