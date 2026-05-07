"""Cosmetic text normalizers for KIE field values.

Step 2's metadata form and the Kho importer both consume the same set of
KIE annotations, but they each previously had their own copy of the
"collapse to single line" rules. Concentrating those rules here gives the
form, Kho columns and downstream xlsx export a single source of truth.

The functions are intentionally side-effect-free and operate on plain
strings — they do NOT mutate the canonical JSON. Callers decide where the
cleaned text lands (UI form vs DB column).
"""
from __future__ import annotations

import re
import unicodedata


_SUBJECT_PREFIX_STOP_WORDS = {
    "VE", "VIEC", "CUA", "CHO", "DOI", "VOI", "THEO", "TREN",
}


def single_line_text(text: str) -> str:
    """Collapse runs of whitespace (including newlines) into single spaces."""
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _ascii_upper_word(token: str) -> str:
    token = token.replace("Đ", "D").replace("đ", "d")
    text = "".join(
        ch for ch in unicodedata.normalize("NFKD", token)
        if not unicodedata.combining(ch)
    )
    return re.sub(r"[^A-Z]+", "", text.upper())


def _is_all_caps_word(token: str) -> bool:
    core = token.strip(" \t\r\n.,;:()[]{}\"'“”‘’")
    letters = [ch for ch in core if ch.isalpha()]
    return bool(letters) and all(ch.upper() == ch for ch in letters)


def _sentence_case_text(text: str) -> str:
    """Lowercase everything, then capitalize the first alpha character."""
    chars = list(str(text or "").lower())
    for idx, ch in enumerate(chars):
        if ch.isalpha():
            chars[idx] = ch.upper()
            break
    return "".join(chars)


def normalize_subject_type_prefix(text: str, doc_type_text: str = "") -> str:
    """Preserve the subject exactly, including an uppercase doc-type prefix.

    `doc_type_text` is intentionally ignored. The official subject often starts
    with a standalone uppercase type line such as "KẾ HOẠCH", "QUYẾT ĐỊNH",
    or "BÁO CÁO"; that text is part of the trích yếu and must remain visible.
    """
    raw = str(text or "")
    return raw.strip()


def normalize_subject_for_storage(subject_text: str, doc_type_text: str) -> str:
    """Collapse subject whitespace for form/DB/xlsx while preserving wording."""
    return single_line_text(
        normalize_subject_type_prefix(subject_text, doc_type_text)
    )
