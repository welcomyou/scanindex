"""Canonical OCR JSON I/O — single point for reading/writing `_ocr.pdf.json.zst`.

A canonical companion is the JSON sidecar that lives next to `_ocr.pdf`. It
holds OCR pages/lines/words, KIE annotations, and document metadata. Every
producer (Tab "Chuyển scan PDF → Word", Số hóa lưu trữ Bước 1+2, Quét file
mật, OCR accuracy benchmark) writes the same schema `ocr_kie_document_v3`
(see `kie/json_utils.py`). Producer-side variance is limited to the canonical
profile (full vs `layoutlmv3_runtime` slim) and the engine label.

Public API:

    load_canonical(path)              read canonical data; `.json.zst` is canonical
    save_canonical(path, data, *,     upgrade + conditional slim + atomic
                   profile=None,      write — compressed by default
                   compress=True)

    resolve_companion(any_path)       existing `.json.zst` companion or None
    companion_for_pdf(pdf_path)       default `.json.zst` companion path
    companion_to_pdf(companion)       strip suffix back to PDF path

Profile resolution inside save_canonical: explicit `profile=` arg > value on
`data.pipeline.ocr.canonical_profile` > none. This lets augment-and-rewrite
callers stay slim if the source was slim.

Compression: on by default at zstd level 3. Real-world OCR JSON shrinks to
~13% of plain size (87% saved); decompress+parse is ≈ raw json.load.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional, Union

PathLike = Union[str, os.PathLike]

JSON_SUFFIX = ".json"
ZST_SUFFIX = ".json.zst"

# First 4 bytes of every zstd stream.
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

# Public profile aliases. The stored value is always the versioned form.
LAYOUTLMV3_RUNTIME_PROFILE = "layoutlmv3_runtime"
LAYOUTLMV3_RUNTIME_PROFILE_V1 = "layoutlmv3_runtime_v1"
LAYOUTLMV3_TRAINING_PROFILE = "layoutlmv3_training"
LAYOUTLMV3_TRAINING_PROFILE_V1 = "layoutlmv3_training_v1"
DOCX_EXPORT_PROFILE = "docx_export"
DOCX_EXPORT_PROFILE_V1 = "docx_export_v1"

_PROFILE_ALIASES = {
    LAYOUTLMV3_RUNTIME_PROFILE: LAYOUTLMV3_RUNTIME_PROFILE_V1,
    LAYOUTLMV3_RUNTIME_PROFILE_V1: LAYOUTLMV3_RUNTIME_PROFILE_V1,
    LAYOUTLMV3_TRAINING_PROFILE: LAYOUTLMV3_TRAINING_PROFILE_V1,
    LAYOUTLMV3_TRAINING_PROFILE_V1: LAYOUTLMV3_TRAINING_PROFILE_V1,
    DOCX_EXPORT_PROFILE: DOCX_EXPORT_PROFILE_V1,
    DOCX_EXPORT_PROFILE_V1: DOCX_EXPORT_PROFILE_V1,
    # Historical internal name from earlier PDF-to-Word cache experiments.
    "docx_export_full": DOCX_EXPORT_PROFILE_V1,
}

KNOWN_CANONICAL_PROFILES = frozenset(_PROFILE_ALIASES.values())


def resolve_profile(profile: Optional[str]) -> Optional[str]:
    """Resolve a public profile alias to the stored versioned profile name."""
    if profile is None:
        return None
    value = str(profile).strip()
    if not value:
        return None
    return _PROFILE_ALIASES.get(value, value)


def _infer_source_mode(data: dict) -> str:
    """Best-effort source-mode metadata for old callers/files."""
    ocr = data.get("pipeline", {}).get("ocr", {}) if isinstance(data, dict) else {}
    engine = (
        ocr.get("engine")
        or data.get("document", {}).get("engine")
        or data.get("engine")
        or ""
    )
    if ocr.get("digital_layer_merge"):
        return "mixed"
    if str(engine).lower() == "digital_pdf_text":
        return "digital"
    return "scan"


def _stamp_profile_metadata(data: dict, profile: Optional[str]) -> None:
    pipeline = data.setdefault("pipeline", {})
    ocr = pipeline.setdefault("ocr", {})
    if profile:
        ocr["canonical_profile"] = profile
    source_mode = str(ocr.get("source_mode") or "").strip().lower()
    if source_mode not in {"scan", "digital", "mixed"}:
        ocr["source_mode"] = _infer_source_mode(data)


def validate_canonical_profile(data: dict, profile: Optional[str]) -> list[str]:
    """Lightweight profile contract checks used by tests and diagnostics."""
    resolved = resolve_profile(profile)
    warnings: list[str] = []
    pages = data.get("pages") or [] if isinstance(data, dict) else []
    if resolved == LAYOUTLMV3_RUNTIME_PROFILE_V1:
        if any(page.get("kie_tokens") for page in pages if isinstance(page, dict)):
            warnings.append("layoutlmv3_runtime_v1 must not contain page.kie_tokens")
        if any(page.get("layout_regions") for page in pages if isinstance(page, dict)):
            warnings.append("layoutlmv3_runtime_v1 must not contain page.layout_regions")
        if any(page.get("table_structures") for page in pages if isinstance(page, dict)):
            warnings.append("layoutlmv3_runtime_v1 must not contain page.table_structures")
    elif resolved == LAYOUTLMV3_TRAINING_PROFILE_V1:
        if any("kie_tokens" not in page for page in pages if isinstance(page, dict)):
            warnings.append("layoutlmv3_training_v1 should contain page.kie_tokens")
        if any(page.get("table_structures") for page in pages if isinstance(page, dict)):
            warnings.append("layoutlmv3_training_v1 must not contain page.table_structures")
    elif resolved == DOCX_EXPORT_PROFILE_V1:
        if any(page.get("kie_tokens") for page in pages if isinstance(page, dict)):
            warnings.append("docx_export_v1 must not contain page.kie_tokens")
    return warnings


def companion_for_pdf(pdf_path: PathLike) -> Path:
    """Default companion path for a PDF.

    `.json.zst` is the only canonical companion suffix from this point on.
    The path may not exist yet, which is useful as a destination for fresh
    writes. Never raises for missing files.
    """
    pdf = Path(pdf_path)
    return pdf.with_name(pdf.name + ZST_SUFFIX)


def resolve_companion(path: PathLike) -> Optional[Path]:
    """Locate an existing canonical `.json.zst` companion.

    A legacy `.json` argument is treated as an alias for `.json.zst`; plain
    `.json` files are not resolved implicitly.
    """
    p = Path(path)
    s = str(p)
    if s.endswith(ZST_SUFFIX):
        return p if p.exists() else None
    if s.endswith(JSON_SUFFIX):
        zst = Path(s + ".zst")
        return zst if zst.exists() else None
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
    """Read a canonical companion.

    `.json.zst` is the production format. Plain JSON is still parseable when
    explicitly passed for developer migration/debugging, but `resolve_companion`
    never selects it implicitly.
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
        build_kie_tokens_in_place,
        prune_canonical_for_docx_export_in_place,
        prune_canonical_for_layoutlmv3_training_in_place,
        slim_canonical_for_layoutlmv3_runtime_in_place,
        upgrade_ocr_data_in_place,
    )

    upgrade_ocr_data_in_place(data)
    resolved = resolve_profile(
        profile or data.get("pipeline", {}).get("ocr", {}).get("canonical_profile")
    )
    _stamp_profile_metadata(data, resolved)

    if resolved == LAYOUTLMV3_RUNTIME_PROFILE_V1:
        slim_canonical_for_layoutlmv3_runtime_in_place(data)
    elif resolved == LAYOUTLMV3_TRAINING_PROFILE_V1:
        build_kie_tokens_in_place(data)
        prune_canonical_for_layoutlmv3_training_in_place(data)
    elif resolved == DOCX_EXPORT_PROFILE_V1:
        prune_canonical_for_docx_export_in_place(data)

    _stamp_profile_metadata(data, resolved)

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
