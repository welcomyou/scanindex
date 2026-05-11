"""Vietnamese-aware text utilities for the searchable repository.

Runtime indexing uses one tokenizer only: underthesea.
"""
from __future__ import annotations

import unicodedata
from functools import lru_cache
from typing import Iterable, List, Optional


def to_no_diacritic(text: str) -> str:
    """Lowercase-preserving diacritic strip; also maps d-stroke variants."""
    if not text:
        return ""
    nfd = unicodedata.normalize("NFD", text)
    stripped = "".join(c for c in nfd if unicodedata.category(c) != "Mn")
    return stripped.replace("đ", "d").replace("Đ", "D")


_UNDER_SEGMENTER = None
_UNDER_INIT_FAILED = False


def _get_underthesea_segmenter():
    """Lazy import underthesea. Failures are remembered to avoid retry."""
    global _UNDER_SEGMENTER, _UNDER_INIT_FAILED
    if _UNDER_SEGMENTER is not None or _UNDER_INIT_FAILED:
        return _UNDER_SEGMENTER
    try:
        from underthesea import word_tokenize

        _UNDER_SEGMENTER = word_tokenize
    except Exception:
        _UNDER_INIT_FAILED = True
        _UNDER_SEGMENTER = None
    return _UNDER_SEGMENTER


def _segment_underthesea_one(text: str) -> Optional[str]:
    if not text:
        return ""
    seg = _get_underthesea_segmenter()
    if seg is None:
        return None
    try:
        return seg(text, format="text")
    except Exception:
        return None


def segment_many(texts: Iterable[str]) -> List[Optional[str]]:
    """Segment multiple texts using underthesea."""
    return [_segment_underthesea_one(t or "") for t in texts]


def segment(text: str) -> Optional[str]:
    """Return text with multi-syllable Vietnamese words joined by underscore.

    Returns None when underthesea is unavailable. The caller can still index
    text_original + text_no_diacritic.
    """
    if not text:
        return ""
    return segment_many([text])[0]


@lru_cache(maxsize=512)
def segment_query(text: str) -> Optional[str]:
    """Cached query segmentation for Tantivy body_segmented queries."""
    return segment(text)


def get_tokenizer_version() -> str:
    try:
        import underthesea

        return getattr(underthesea, "__version__", "unknown")
    except ImportError:
        return "none"
