"""Canonical OCR JSON I/O — single point for reading/writing `_ocr.pdf.json`.

A canonical companion is the JSON sidecar that lives next to `_ocr.pdf`. It
holds OCR pages/lines/words, KIE annotations, and document metadata. Every
producer in the app — Tab "Chuyển scan PDF → Word", Số hóa lưu trữ Bước 1,
Số hóa lưu trữ Bước 2 (per-segment + signer re-OCR + digital merge + KIE
augment), Quét file mật, OCR accuracy benchmark — writes the **same** schema
(`ocr_kie_document_v3` in `kie/json_utils.py`). The only producer-side
variance is the canonical profile (full vs `layoutlmv3_runtime` slim) and
the engine label.

Two write entrypoints:

  * `save_canonical(path, data, compress=, level=)` — low-level: atomic
    write, optional zstd. Caller controls every byte.

  * `finalize_and_save_canonical(path, data, profile=, compress=, level=)`
    — high-level: runs the shared finishing pipeline before write —

        upgrade_ocr_data_in_place(data)
        if profile resolves to a slim profile:
            slim_canonical_for_layoutlmv3_runtime_in_place(data)
        save_canonical(...)

    Profile resolution: explicit `profile=` arg, else
    `data["pipeline"]["ocr"]["canonical_profile"]` already on the document.
    This matches `replace_canonical_page_with_page_result`'s detection so
    augment-and-rewrite callers stay slim if the source was slim.

Two read helpers:

  * `load_canonical(path)` — auto-detects `.json` vs `.json.zst` by suffix or
    zstd magic bytes. Dual-read works while a directory has both variants.
  * `resolve_existing_companion(any_related_path)` — find the on-disk
    companion given the PDF path or either companion suffix.

Compression is OFF by default; switching it on is a per-callsite or
per-config flag flip — disk format only changes when a caller opts in.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional, Union

PathLike = Union[str, os.PathLike]

JSON_SUFFIX = ".json"
ZST_SUFFIX = ".json.zst"

# Zstandard frame magic — first 4 bytes of every zstd stream. Lets
# `load_canonical` accept either suffix without trusting the filename.
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

# Canonical profile names that mean "this is the LayoutLMv3 runtime slim
# variant". `slim_canonical_for_layoutlmv3_runtime_in_place` writes the `_v1`
# suffix into pipeline.ocr.canonical_profile on the document, but callers
# still pass the un-suffixed name as the profile argument. Both are accepted.
LAYOUTLMV3_RUNTIME_PROFILE = "layoutlmv3_runtime"
LAYOUTLMV3_RUNTIME_PROFILES = frozenset({
    "layoutlmv3_runtime",
    "layoutlmv3_runtime_v1",
})


def companion_for_pdf(pdf_path: PathLike) -> Path:
    """Resolve the canonical companion for a PDF path.

    Returns the existing `.json` if present, else the existing `.json.zst`,
    else the default `.json` path (which may not exist yet — useful as a
    destination for a fresh write). Never raises for missing files.
    """
    pdf = Path(pdf_path)
    plain = pdf.with_name(pdf.name + JSON_SUFFIX)
    if plain.exists():
        return plain
    zst = pdf.with_name(pdf.name + ZST_SUFFIX)
    if zst.exists():
        return zst
    return plain


def resolve_existing_companion(path: PathLike) -> Optional[Path]:
    """Locate an existing companion given any related path.

    Accepts the PDF path, a `.json` path, or a `.json.zst` path. Returns the
    actually-on-disk file, or None if neither variant exists.
    """
    p = Path(path)
    s = str(p)
    if s.endswith(ZST_SUFFIX) or s.endswith(JSON_SUFFIX):
        return p if p.exists() else None
    found = companion_for_pdf(p)
    return found if found.exists() else None


def companion_to_pdf(companion_path: PathLike) -> Path:
    """Inverse of `companion_for_pdf`. Strips `.json` or `.json.zst`."""
    p = Path(companion_path)
    s = str(p)
    if s.endswith(ZST_SUFFIX):
        return Path(s[: -len(ZST_SUFFIX)])
    if s.endswith(JSON_SUFFIX):
        return Path(s[: -len(JSON_SUFFIX)])
    raise ValueError(f"Not a canonical companion path: {p}")


def load_canonical(path: PathLike) -> dict:
    """Read a canonical companion. Transparent for `.json` and `.json.zst`.

    Detection order: filename suffix first (fast path), then zstd magic
    bytes (handles files saved with an unexpected name).
    """
    p = Path(path)
    raw = p.read_bytes()
    looks_zst = str(p).endswith(ZST_SUFFIX) or raw[:4] == _ZSTD_MAGIC
    if looks_zst:
        import zstandard as zstd  # lazy: keeps zstandard optional at import time
        raw = zstd.ZstdDecompressor().decompress(raw)
    return json.loads(raw)


def save_canonical(
    path: PathLike,
    data: dict,
    *,
    compress: bool = False,
    level: int = 3,
) -> Path:
    """Atomic write of canonical JSON. Low-level: no upgrade/slim.

    When `compress=True`, a caller-supplied `.json` path is auto-rewritten to
    `.json.zst`. Level 3 is the benchmarked sweet spot (~13% ratio, ~4ms per
    file on representative OCR JSON). Otherwise behavior mirrors the pre-
    existing write pattern: `<dest>.tmp` then `os.replace`.
    """
    p = Path(path)
    s = str(p)
    if compress:
        if s.endswith(JSON_SUFFIX) and not s.endswith(ZST_SUFFIX):
            p = Path(s + ".zst")
        elif not s.endswith(ZST_SUFFIX):
            p = Path(s + ZST_SUFFIX)
    payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
    if compress:
        import zstandard as zstd
        payload = zstd.ZstdCompressor(level=level).compress(payload)
    tmp = Path(str(p) + ".tmp")
    tmp.write_bytes(payload)
    os.replace(tmp, p)
    return p


def _resolve_profile(data: dict, profile: Optional[str]) -> Optional[str]:
    """Resolve canonical profile: explicit arg > value already on the doc."""
    if profile:
        return profile
    try:
        return data.get("pipeline", {}).get("ocr", {}).get("canonical_profile")
    except AttributeError:
        return None


def finalize_and_save_canonical(
    path: PathLike,
    data: dict,
    *,
    profile: Optional[str] = None,
    compress: bool = False,
    level: int = 3,
) -> Path:
    """Run the shared finishing pipeline, then atomic-save.

    Steps:
      1. `upgrade_ocr_data_in_place(data)` — fill defaults, normalize older
         schema variants.
      2. If the resolved profile names the LayoutLMv3 runtime slim variant
         (`layoutlmv3_runtime` / `_v1`), strip runtime-unused fields.
      3. Atomic write via `save_canonical(compress=…)`.

    `upgrade_ocr_data_in_place` and `slim_canonical_for_layoutlmv3_runtime_in_place`
    are imported lazily so this module stays light at import time and free of
    cycles with `scanindex.core.kie.json_utils`.
    """
    from scanindex.core.kie.json_utils import (
        slim_canonical_for_layoutlmv3_runtime_in_place,
        upgrade_ocr_data_in_place,
    )

    upgrade_ocr_data_in_place(data)
    resolved = _resolve_profile(data, profile)
    if resolved in LAYOUTLMV3_RUNTIME_PROFILES:
        slim_canonical_for_layoutlmv3_runtime_in_place(data)
    return save_canonical(path, data, compress=compress, level=level)
