"""Canonical OCR JSON I/O — single point for reading/writing `_ocr.pdf.json`.

A canonical companion is the JSON sidecar that lives next to `_ocr.pdf`. It
holds OCR pages/lines/words, KIE annotations, and document metadata. Every
producer (Tab "Chuyển scan PDF → Word", Số hóa lưu trữ Bước 1+2, Quét file
mật, OCR accuracy benchmark) writes the same schema `ocr_kie_document_v3`
(see `kie/json_utils.py`). Producer-side variance is limited to the canonical
profile (full vs `layoutlmv3_runtime` slim) and the engine label.

Public API:

    load_canonical(path)              transparent for .json and .json.zst
    save_canonical(path, data, *,     upgrade + conditional slim + atomic
                   profile=None,      write — compressed by default
                   compress=True)

    resolve_companion(any_path)       existing companion or None
    companion_for_pdf(pdf_path)       default companion path (may not exist)
    companion_to_pdf(companion)       strip suffix back to PDF path

Profile resolution inside save_canonical: explicit `profile=` arg > value on
`data.pipeline.ocr.canonical_profile` > none. This lets augment-and-rewrite
callers stay slim if the source was slim.

Compression: on by default at zstd level 3. Real-world OCR JSON shrinks to
~13% of plain size (87% saved); decompress+parse is ≈ raw json.load. Opt out
at a specific callsite with `compress=False` — e.g. for end-user output
files that should stay human-inspectable.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional, Union

PathLike = Union[str, os.PathLike]

JSON_SUFFIX = ".json"
ZST_SUFFIX = ".json.zst"

# First 4 bytes of every zstd stream — lets `load_canonical` accept either
# suffix without trusting the filename.
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

# Canonical profile name for the LayoutLMv3 runtime slim variant.
# `slim_canonical_for_layoutlmv3_runtime_in_place` stamps the `_v1` form into
# `pipeline.ocr.canonical_profile` after slimming; callers pass the
# un-suffixed name. Both are accepted by save_canonical's profile resolver.
LAYOUTLMV3_RUNTIME_PROFILE = "layoutlmv3_runtime"
_SLIM_PROFILES = frozenset({"layoutlmv3_runtime", "layoutlmv3_runtime_v1"})


def companion_for_pdf(pdf_path: PathLike) -> Path:
    """Default companion path for a PDF.

    Returns the existing `.json` if present, else the existing `.json.zst`,
    else the default `.json` path (may not exist yet — useful as a destination
    for a fresh write). Never raises for missing files.
    """
    pdf = Path(pdf_path)
    plain = pdf.with_name(pdf.name + JSON_SUFFIX)
    if plain.exists():
        return plain
    zst = pdf.with_name(pdf.name + ZST_SUFFIX)
    if zst.exists():
        return zst
    return plain


def resolve_companion(path: PathLike) -> Optional[Path]:
    """Locate an existing companion. Accepts PDF path or either suffix."""
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
    """Read a canonical companion. Auto-detects `.json` vs `.json.zst`.

    Detection: filename suffix first (fast path), then zstd magic bytes
    (covers files saved with an unexpected name).
    """
    raw = Path(path).read_bytes()
    is_zst = str(path).endswith(ZST_SUFFIX) or raw[:4] == _ZSTD_MAGIC
    if is_zst:
        import zstandard as zstd
        raw = zstd.ZstdDecompressor().decompress(raw)
    return json.loads(raw)


def save_canonical(
    path: PathLike,
    data: dict,
    *,
    profile: Optional[str] = None,
    compress: bool = True,
    level: int = 3,
) -> Path:
    """Run the canonical finishing pipeline and atomic-write.

    1. `upgrade_ocr_data_in_place(data)` — fill defaults, normalize. Idempotent.
    2. If profile resolves to a slim variant, strip runtime-unused fields.
    3. Atomic write — `.json.zst` by default, `.json` if `compress=False`.
       The caller's path suffix is auto-corrected to match.

    Returns the actual path written.
    """
    from scanindex.core.kie.json_utils import (
        slim_canonical_for_layoutlmv3_runtime_in_place,
        upgrade_ocr_data_in_place,
    )

    upgrade_ocr_data_in_place(data)
    resolved = profile or data.get("pipeline", {}).get("ocr", {}).get("canonical_profile")
    if resolved in _SLIM_PROFILES:
        slim_canonical_for_layoutlmv3_runtime_in_place(data)

    s = str(path)
    if compress:
        if s.endswith(JSON_SUFFIX):
            dest = Path(s + ".zst")
        elif s.endswith(ZST_SUFFIX):
            dest = Path(s)
        else:
            dest = Path(s + ZST_SUFFIX)
    else:
        if s.endswith(ZST_SUFFIX):
            dest = Path(s[: -len(".zst")])
        else:
            dest = Path(s)

    payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
    if compress:
        import zstandard as zstd
        payload = zstd.ZstdCompressor(level=level).compress(payload)

    tmp = Path(str(dest) + ".tmp")
    tmp.write_bytes(payload)
    os.replace(tmp, dest)
    return dest
