"""Vietnamese-aware text utilities for the searchable repository.

Runtime indexing uses one tokenizer only: underthesea.
"""
from __future__ import annotations

import re
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


# Word tokenizer shared by search matching and the filter-text column.
# Mirrors search_engine._tokens exactly — keep the two in sync via this
# single implementation (search_engine aliases it).
_WORD_RE = re.compile(r"\w+", re.UNICODE)


def norm_tokens(text: str) -> List[str]:
    """Diacritic-stripped, lowercased word tokens."""
    return _WORD_RE.findall(to_no_diacritic(text or "").lower())


def filter_tokens(text: str) -> List[str]:
    """norm_tokens kept for filter matching: >=2 chars, or any digit."""
    return [
        t for t in norm_tokens(text)
        if len(t) >= 2 or any(ch.isdigit() for ch in t)
    ]


def build_filter_text(*field_values: Optional[str]) -> str:
    """Normalized, space-padded token soup of every filterable field.

    Stored in documents.doc_filter_text (schema v10) and queried with
    ``instr(doc_filter_text, ' tok ')`` — a cheap necessary condition the
    SQL prefilter applies before the Python fuzzy matcher re-verifies.
    Padded with spaces so ' tok ' matches whole tokens only.
    """
    toks: List[str] = []
    for v in field_values:
        toks.extend(filter_tokens(v or ""))
    return " " + " ".join(toks) + " " if toks else " "


# ---------------------------------------------------------------------------
# Canonical phrase-search stream (indexer v3)
# ---------------------------------------------------------------------------
# One normalization shared by BOTH sides of phrase search: the document
# records' norm fields are built with it, and the Python verifier counts
# occurrences on it — so "nơi: nhận", "thực-hiện" and line-wrapped phrases
# all normalize to the same "noi nhan" / "thuc hien" token stream on both
# sides. Single source of truth; do not hand-roll variants.

# Inserted between pages inside a document's body_norm stream so a phrase
# query with slop=0 can never match across a page boundary. Alphanumeric
# so the default tokenizer keeps it as one token; distinctive enough that
# real OCR text never produces it.
PAGE_SENTINEL_TOKEN = "zpgbrkz9"

# Inserted between KIE fields inside a document's meta_norm stream so a
# phrase query can never match ACROSS two fields (issue_org "alpha" +
# signer_name "beta" must not surface for the exact query "alpha beta").
FIELD_SENTINEL_TOKEN = "zfldbrkz7"


def search_norm(text: str) -> str:
    """Canonical phrase-search stream: tokens joined by single spaces."""
    return " ".join(norm_tokens(text or ""))


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
