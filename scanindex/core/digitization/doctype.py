"""Doc-type taxonomy + auto-detection for Vietnamese state documents.

Used by:
  * `kie_postprocess` to fill the missing DOC_TYPE field_instance after
    KIE inference (the trained model never emits DOC_TYPE; it's a
    deterministic post-process derived from DOC_SUBJECT prefix +
    DOC_NUMBER_SYMBOL suffix).
  * Step 2's "Tên loại văn bản" combobox to populate options.
  * Future Kho metadata edit dialog.

Detection rules (applied in order):
  1. DOC_NUMBER_SYMBOL trailing token (e.g. "Số: 123/QĐ-UBND" → "QĐ" → "Quyết định").
  2. DOC_SUBJECT leading uppercase run (e.g. "BÁO CÁO Về việc..." → "BÁO CÁO" → "Báo cáo").
  3. Fallback "Khác".

Match is case + diacritic-insensitive on the canonical form so OCR
spelling drift like "BAO CAO" / "Bao Cao" still hits the right entry.
"""
from __future__ import annotations

import configparser
import json
import re
import unicodedata
from pathlib import Path
from typing import Optional, Tuple

from scanindex.infra.paths import get_base_dir


# ---- Canonical taxonomy ------------------------------------------------
# (display_name, code) — display goes into ComboBox + kie_doc_type DB col,
# code is the symbol that appears after `/` in DOC_NUMBER_SYMBOL.
DOC_TYPES: list[Tuple[str, str]] = [
    ("Nghị quyết",        "NQ"),
    ("Quyết định",        "QĐ"),
    ("Chỉ thị",           "CT"),
    ("Kết luận",          "KL"),
    ("Quy chế",           "QC"),
    ("Quy định",          "QyĐ"),
    ("Thông cáo",         "TC"),
    ("Thông báo",         "TB"),
    ("Hướng dẫn",         "HD"),
    ("Chương trình",      "CTr"),
    ("Kế hoạch",          "KH"),
    ("Phương án",         "PA"),
    ("Đề án",             "ĐA"),
    ("Dự án",             "DA"),
    ("Báo cáo",           "BC"),
    ("Biên bản",          "BB"),
    ("Tờ trình",          "TTr"),
    ("Hợp đồng",          "HĐ"),
    ("Công điện",         "CĐ"),
    ("Bản ghi nhớ",       "BGN"),
    ("Bản thỏa thuận",    "BTT"),
    ("Giấy ủy quyền",     "GUQ"),
    ("Giấy mời",          "GM"),
    ("Giấy giới thiệu",   "GGT"),
    ("Giấy nghỉ phép",    "GNP"),
    ("Phiếu gửi",         "PG"),
    ("Phiếu chuyển",      "PC"),
    ("Phiếu báo",         "PB"),
    ("Bản sao y",         "SY"),
    ("Bản trích sao",     "TrS"),
    ("Bản sao lục",       "SL"),
    ("Công văn",          "CV"),
]
DOC_TYPE_OTHER = "Khác"


# ---- normalisation helpers --------------------------------------------


def _strip_diacritics(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text or "")
    no_acc = "".join(ch for ch in nfkd if not unicodedata.combining(ch))
    return no_acc.replace("đ", "d").replace("Đ", "D")


def _norm(text: str) -> str:
    """lowercase + strip diacritics + collapse whitespace, for matching."""
    return " ".join(_strip_diacritics(text or "").lower().split()).strip()


# Pre-compute lookup tables for fast detection.
_BY_NORM_NAME: dict[str, str] = {_norm(name): name for name, _ in DOC_TYPES}
_BY_CODE: dict[str, str] = {code.lower(): name for name, code in DOC_TYPES}
_BY_NORM_CODE: dict[str, str] = {_norm(code): name for name, code in DOC_TYPES}


# Tokeniser for the symbol — splits on `/`, `-`, or whitespace. Handles
# both Vietnamese state-doc layout `<num>/<type>-<agency>` (e.g.
# "123/QĐ-UBND") and party-doc layout `<num>-<type>/<agency>` (e.g.
# "245-GM/VPTU"), since after splitting the type is always the first
# non-digit token.
_NUM_TOKEN_SPLIT_RE = re.compile(r"[\/\-\s]+")
_NUM_SO_PREFIX_RE = re.compile(r"^\s*s[ốoô][\s:.]*", re.IGNORECASE)


def _detect_from_doc_number_with_code(
    doc_number_symbol: str,
) -> Optional[Tuple[str, str]]:
    """Tokenize the symbol on `/`/`-`/whitespace and return the
    ``(normalised_code, canonical_name)`` for the first token that maps
    to a known doc-type code. Pure digit tokens (number, year) are
    skipped."""
    if not doc_number_symbol:
        return None
    cleaned = _NUM_SO_PREFIX_RE.sub("", doc_number_symbol)
    for tok in _NUM_TOKEN_SPLIT_RE.split(cleaned):
        tok = tok.strip()
        if not tok or tok.isdigit():
            continue
        norm_tok = _norm(tok)
        hit = _BY_NORM_CODE.get(norm_tok) or _BY_CODE.get(tok.lower())
        if hit:
            return norm_tok, hit
    return None


def detect_from_doc_number(doc_number_symbol: str) -> Optional[str]:
    """Return canonical display name if any token in the symbol matches
    a known doc-type code (case + diacritic insensitive)."""
    match = _detect_from_doc_number_with_code(doc_number_symbol)
    return match[1] if match else None


# Codes that collide after diacritic stripping. When the symbol resolves
# to one of these, defer to DOC_SUBJECT for disambiguation: the issuer
# almost always types the type as a bold uppercase line at the top of the
# body, so the subject prefix is the tie-breaker.
#
# Canonical example: "QyĐ" (Quy định) drops the tiny "y" under OCR and
# becomes indistinguishable from "QĐ" (Quyết định).
_AMBIGUOUS_CODE_NAMES: dict[str, tuple[str, ...]] = {
    "qd": ("Quyết định", "Quy định"),
}


# Regex to pull the leading all-uppercase run from a subject line.
_UPPER_PREFIX_RE = re.compile(r"^[\s]*([A-ZĐÁẢÃẠÀÂẤẨẪẬẦĂẮẲẴẶẰÉẺẼẸÈÊẾỂỄỆỀÍỈĨỊÌÓỎÕỌÒÔỐỔỖỘỒƠỚỞỠỢỜÚỦŨỤÙƯỨỬỮỰỪÝỶỸỴỲ\s]{2,40})")


def detect_from_doc_subject(doc_subject: str) -> Optional[str]:
    """Return canonical display name if the leading uppercase prefix of
    the subject (2-5 words) matches a known doc-type name."""
    if not doc_subject:
        return None
    m = _UPPER_PREFIX_RE.match(doc_subject)
    if not m:
        return None
    prefix = m.group(1).strip()
    # Try progressively shorter prefixes (5..2 words) so we match the
    # longest possible name — e.g. "BÁO CÁO TỔNG KẾT" should match
    # "Báo cáo" not be confused.
    words = prefix.split()
    for n in range(min(5, len(words)), 1, -1):
        candidate = " ".join(words[:n])
        normed = _norm(candidate)
        hit = _BY_NORM_NAME.get(normed)
        if hit:
            return hit
    # Fallback: 1-word match for short names like "TB", "BC" if they
    # somehow appear standalone (rare but possible).
    if words:
        normed = _norm(words[0])
        return _BY_NORM_NAME.get(normed)
    return None


def detect_doc_type(doc_subject: str = "",
                    doc_number_symbol: str = "") -> str:
    """Return the canonical Vietnamese name for the document type, or
    the literal "Khác" when no rule matches.

    Order:
      1. Match a known code in DOC_NUMBER_SYMBOL — the issuing authority
         types this explicitly so it's the strongest signal.
      2. If the matched code is ambiguous after diacritic stripping
         (see ``_AMBIGUOUS_CODE_NAMES``), defer to the subject prefix:
         the body always restates the type as a bold uppercase line.
      3. Fall back to the subject prefix when the symbol yielded
         nothing.
    """
    via_num = _detect_from_doc_number_with_code(doc_number_symbol)
    if via_num:
        code_norm, name = via_num
        siblings = _AMBIGUOUS_CODE_NAMES.get(code_norm)
        if siblings:
            via_subj = detect_from_doc_subject(doc_subject)
            if via_subj and via_subj in siblings:
                return via_subj
        return name
    via_subj = detect_from_doc_subject(doc_subject)
    if via_subj:
        return via_subj
    return DOC_TYPE_OTHER


def default_display_names() -> list[str]:
    """Built-in taxonomy order, with 'Khác' as the explicit fallback."""
    return normalize_display_names([name for name, _ in DOC_TYPES])


def normalize_display_names(names: list[str] | tuple[str, ...]) -> list[str]:
    """Clean user-editable category values and sort A-Z for UI stability."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in names or []:
        name = " ".join(str(raw or "").split()).strip()
        if not name:
            continue
        key = _norm(name)
        if key == _norm(DOC_TYPE_OTHER):
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    out.sort(key=_norm)
    out.append(DOC_TYPE_OTHER)
    return out


def _settings_path() -> Path:
    return Path(get_base_dir()) / "settings.ini"


def _read_configured_display_names() -> Optional[list[str]]:
    cfg_path = _settings_path()
    if not cfg_path.exists():
        return None
    cfg = configparser.ConfigParser()
    cfg.read(cfg_path, encoding="utf-8")
    if "Catalog" not in cfg:
        return None
    raw = cfg["Catalog"].get("DocumentTypesJson", "").strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except Exception:
        return None
    if not isinstance(parsed, list):
        return None
    names = normalize_display_names([str(x) for x in parsed])
    return names or None


def serialize_display_names(names: list[str] | tuple[str, ...]) -> str:
    return json.dumps(normalize_display_names(list(names)), ensure_ascii=False)


def all_display_names() -> list[str]:
    """ComboBox population order: configured taxonomy + 'Khác' tail."""
    return _read_configured_display_names() or default_display_names()
