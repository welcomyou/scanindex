"""Canonical OCR JSON I/O — single point for reading/writing `_ocr.pdf.json`.

A canonical companion is the JSON sidecar that lives next to `_ocr.pdf`. It
holds OCR pages/lines/words, KIE annotations, and document metadata. Many
sites in the codebase read and write this file; routing them through this
module keeps two invariants in one place:

  1. Atomic writes (tmp + os.replace).
  2. Optional zstd compression — write `.json.zst`, read either suffix
     transparently. Compression is OFF by default so behavior is unchanged
     until a caller opts in. Decompression auto-detects either by file
     extension or zstd magic bytes, so dual-read works while a directory
     contains a mix of plain and compressed companions during migration.

The companion is always referenced by the partner PDF path. Use
`companion_for_pdf(pdf_path)` to resolve the existing sidecar (preferring
plain `.json` over `.json.zst` if both exist) or `companion_to_pdf` to go
back the other way.
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
    actually-on-disk file, or None if neither variant exists. Used by sites
    that previously did `os.path.exists(pdf + ".json")`.
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
    """Atomic write of a canonical companion. Returns the actual path written.

    When `compress=True`, a caller-supplied `.json` path is auto-rewritten to
    `.json.zst`. Level 3 is the benchmarked sweet spot (~13% ratio, ~4ms per
    file on representative OCR JSON). Behavior with `compress=False` mirrors
    the pre-existing write pattern: `<dest>.tmp` then `os.replace`.
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
