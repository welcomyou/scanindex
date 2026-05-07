from __future__ import annotations

import re
import unicodedata


OCR_TEXT_NORMALIZATION = "latin_vi_canonical_v2"

_LATIN_VIET_RE = re.compile(r"[A-Za-zÀ-ỹĐđ]")
_DISALLOWED_SCRIPT_PREFIXES = {"CYRILLIC", "GREEK", "ARABIC"}
_CONFUSABLE_LATIN_MAP = {
    # Cyrillic -> Latin
    "\u0410": "A", "\u0430": "a",
    "\u0412": "B", "\u0432": "b",
    "\u0421": "C", "\u0441": "c",
    "\u0415": "E", "\u0435": "e",
    "\u041D": "H", "\u043D": "h",
    "\u0406": "I", "\u0456": "i",
    "\u0408": "J", "\u0458": "j",
    "\u041A": "K", "\u043A": "k",
    "\u041C": "M", "\u043C": "m",
    "\u041E": "O", "\u043E": "o",
    "\u0420": "P", "\u0440": "p",
    "\u0405": "S", "\u0455": "s",
    "\u0422": "T", "\u0442": "t",
    "\u0423": "Y", "\u0443": "y",
    "\u0425": "X", "\u0445": "x",
    # Greek -> Latin
    "\u0391": "A", "\u03B1": "a",
    "\u0392": "B",
    "\u0395": "E", "\u03B5": "e",
    "\u0397": "H", "\u03B7": "n",
    "\u0399": "I", "\u03B9": "i",
    "\u039A": "K", "\u03BA": "k",
    "\u039C": "M",
    "\u039D": "N",
    "\u039F": "O", "\u03BF": "o",
    "\u03A1": "P", "\u03C1": "p",
    "\u03A4": "T",
    "\u03A5": "Y", "\u03C5": "y",
    "\u03A7": "X", "\u03C7": "x",
    "\u0396": "Z",
}


def normalize_ocr_lookalikes(text: str) -> str:
    if not text:
        return text
    normalized = unicodedata.normalize("NFKC", text)
    if not _LATIN_VIET_RE.search(normalized):
        return normalized
    if not any(ch in _CONFUSABLE_LATIN_MAP for ch in normalized):
        return normalized
    return "".join(_CONFUSABLE_LATIN_MAP.get(ch, ch) for ch in normalized)


def has_disallowed_script_letters(text: str) -> bool:
    for ch in text or "":
        if not ch.isalpha():
            continue
        script_prefix = unicodedata.name(ch, "").split(" ")[0]
        if script_prefix in _DISALLOWED_SCRIPT_PREFIXES:
            return True
    return False


def sanitize_ocr_surface_text(text: str) -> str:
    normalized = normalize_ocr_lookalikes(text)
    if not normalized:
        return normalized
    if has_disallowed_script_letters(normalized):
        return ""
    return normalized
