from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from train_kie.common import write_json
from train_kie.labeling_workspace import validate_label_output_detailed
from train_kie.ontology import ONTOLOGY_ID
from train_kie.semantic_fields import (
    _extract_page_layout_evidence,
    looks_like_date_line,
    looks_like_doc_number_line,
    looks_like_party_title,
    looks_like_state_header,
    norm,
)


SUBORDINATE_START_RE = re.compile(
    r"^(ban\b|to\b|tieu ban\b|doan\b|khoi\b|chi bo\b|ubkt\b|uy ban kiem tra\b|"
    r"bch\b|ban chap hanh\b|ban thuong vu\b|ban kiem phieu\b|doan kiem tra\b|"
    r"doan giam sat\b|hoi dong\b|van phong\b|giua ban\b)",
    re.IGNORECASE,
)
LOCATION_START_RE = re.compile(
    r"^(xa|phuong|thi tran|huyen|tinh|thanh pho|quan|tx\.?|thi xa|ap|khu pho|thon)\b",
    re.IGNORECASE,
)
ROLE_LINE_RE = re.compile(
    r"(t/?m\.?|k/?t\.?|t/?l\.?|tuq\.?|chá»§ tá»‹ch|phÃ³ chá»§ tá»‹ch|bÃ­ thÆ°|phÃ³ bÃ­ thÆ°|"
    r"trÆ°á»Ÿng ban|phÃ³ trÆ°á»Ÿng ban|giÃ¡m Ä‘á»‘c|phÃ³ giÃ¡m Ä‘á»‘c|trÆ°á»Ÿng phÃ²ng|phÃ³ trÆ°á»Ÿng phÃ²ng|"
    r"thá»§ trÆ°á»Ÿng|chÃ¡nh vÄƒn phÃ²ng|phÃ³ chÃ¡nh vÄƒn phÃ²ng|hiá»‡u trÆ°á»Ÿng|phÃ³ hiá»‡u trÆ°á»Ÿng|"
    r"trÆ°á»Ÿng Ä‘oÃ n|phÃ³ trÆ°á»Ÿng Ä‘oÃ n|trÆ°á»Ÿng ban|trÆ°á»Ÿng bá»™ pháº­n|thÆ° kÃ½|trÆ°á»Ÿng ban chá»‰ Ä‘áº¡o)",
    re.IGNORECASE,
)
PREFIX_ONLY_RE = re.compile(r"^(?:t/m|tm\.?|k/t|kt\.?|t/l|tl\.?|tuq\.?|q\.?)$", re.IGNORECASE)
NOISE_LINE_RE = re.compile(r"^[\W_0-9]+$")
NAME_BAD_KEYWORDS = [
    "nÆ¡i nháº­n",
    "kÃ­nh gá»­i",
    "Ä‘áº£ng",
    "á»§y ban",
    "uá»· ban",
    "ban ",
    "cÄƒn cá»©",
    "Ä‘iá»u ",
    "stt",
    "quÃª quÃ¡n",
    "dá»± bá»‹",
    "nam",
    "ná»¯",
]
COLUMN_HEADER_TERMS = {
    "stt",
    "ho va ten",
    "ngay sinh",
    "gioi tinh",
    "vao dang",
    "que quan",
    "chuc vu",
    "don vi",
    "nam",
    "nu",
    "tuoi den",
    "trinh",
    "dong y",
}
ROLE_NORM_KEYWORDS = [
    "chu tich",
    "pho chu tich",
    "bi thu",
    "pho bi thu",
    "truong ban",
    "pho truong ban",
    "giam doc",
    "pho giam doc",
    "truong phong",
    "pho truong phong",
    "thu ky",
    "ghi bien ban",
    "dang vien",
    "chu tri cuoc hop",
    "chu tri hoi nghi",
    "dai dien chi uy",
    "truong doan",
    "pho truong doan",
    "ban kiem phieu",
    "doan chu tich",
    "ban chap hanh",
]
STAMP_NOISE_KEYWORDS = [
    "thoi gian ky",
    "van phong",
    "van ban den",
    "qua mang",
    "chuyen",
    "luu ho so",
]
TITLE_START_PREFIXES = [
    "v/v",
    "ve viec",
    "bao cao",
    "bien ban",
    "to trinh",
    "giay moi",
    "thu moi",
    "thong bao",
    "don xin",
    "ban kiem diem",
    "ke hoach",
    "quyet dinh",
    "nghi quyet",
    "ket luan",
    "chuong trinh",
]


@dataclass
class FieldDraft:
    label: str
    page_index: int
    lines: list[dict]


def _line_id(line: dict) -> str:
    return line.get("line_id") or line.get("id") or ""


def _line_text(line: dict) -> str:
    return (line.get("text") or "").strip()


def _line_words(line: dict) -> list[dict]:
    return list(line.get("words") or [])


def _line_word_ids(line: dict) -> list[str]:
    ids = list(line.get("word_ids") or [])
    if ids:
        return ids
    result = []
    for word in _line_words(line):
        word_id = word.get("word_id") or word.get("id")
        if word_id:
            result.append(word_id)
    return result


def _line_center_x(line: dict, page: dict) -> float:
    bbox = line.get("bbox") or [0.0, 0.0, 0.0, 0.0]
    width = float(page.get("width") or 1.0)
    return ((bbox[0] + bbox[2]) / 2.0) / width


def _line_left(line: dict) -> float:
    bbox = line.get("bbox") or [0.0, 0.0, 0.0, 0.0]
    return float(bbox[0])


def _line_top(line: dict) -> float:
    bbox = line.get("bbox") or [0.0, 0.0, 0.0, 0.0]
    return float(bbox[1])


def _line_bottom(line: dict) -> float:
    bbox = line.get("bbox") or [0.0, 0.0, 0.0, 0.0]
    return float(bbox[3])


def _line_height(line: dict) -> float:
    bbox = line.get("bbox") or [0.0, 0.0, 0.0, 0.0]
    return max(1.0, float(bbox[3]) - float(bbox[1]))


def _join_lines(lines: list[dict]) -> str:
    return "\n".join(_line_text(line) for line in lines if _line_text(line)).strip()


def _field_payload(field_id: str, draft: FieldDraft) -> dict:
    word_ids = []
    for line in draft.lines:
        for word_id in _line_word_ids(line):
            if word_id not in word_ids:
                word_ids.append(word_id)
    return {
        "field_id": field_id,
        "label": draft.label,
        "page_index": draft.page_index,
        "line_ids": [_line_id(line) for line in draft.lines if _line_id(line)],
        "word_ids": word_ids,
        "text": _join_lines(draft.lines),
        "normalized_value": None,
        "confidence": None,
    }


def _is_noise_line(text: str) -> bool:
    candidate = (text or "").strip()
    if not candidate:
        return True
    if NOISE_LINE_RE.match(candidate):
        return True
    if re.fullmatch(r"[0-9]{1,4}", candidate):
        return True
    return False


def _is_location_continuation(text: str) -> bool:
    candidate = norm(text)
    return bool(LOCATION_START_RE.match(candidate))


def _is_subordinate_start(text: str) -> bool:
    candidate = norm(text)
    return bool(SUBORDINATE_START_RE.match(candidate))


def _is_name_candidate(text: str) -> bool:
    raw = (text or "").strip()
    if len(raw) < 4 or len(raw) > 80:
        return False
    if raw.startswith(("-", "*", "â€¢", "(")):
        return False
    lower = norm(raw)
    if any(keyword in lower for keyword in NAME_BAD_KEYWORDS):
        return False
    if any(ch.isdigit() for ch in raw):
        return False
    if raw.startswith("(") and raw.endswith(")"):
        return False
    words = [word for word in raw.split() if word]
    if not 2 <= len(words) <= 6:
        return False
    if re.search(r"[.,:;!?/\\]", raw):
        return False
    if sum(1 for ch in raw if ch.islower()) < 2:
        return False
    alpha_words = [word for word in words if any(ch.isalpha() for ch in word)]
    if len(alpha_words) < 2:
        return False
    titleish = sum(1 for word in alpha_words if word[:1].isupper())
    return titleish >= max(2, len(alpha_words) - 1)


def _is_loose_name_candidate(text: str) -> bool:
    raw = (text or "").strip()
    if len(raw) < 4 or len(raw) > 90:
        return False
    if raw.startswith(("-", "*", "Ã¢â‚¬Â¢", "(")):
        return False
    lower = norm(raw)
    if any(keyword in lower for keyword in NAME_BAD_KEYWORDS):
        return False
    if any(ch.isdigit() for ch in raw):
        return False
    if raw.count(",") > 1 or raw.count(".") > 1 or ":" in raw:
        return False
    if _is_role_keyword_line(raw):
        return False
    words = [word for word in raw.split() if word]
    alpha_words = [word for word in words if any(ch.isalpha() for ch in word)]
    if not 2 <= len(alpha_words) <= 6:
        return False
    if len(raw) > 50 and sum(1 for ch in raw if ch.islower()) > 10:
        return False
    return True


def _is_recipient_like_line(text: str) -> bool:
    candidate = norm(text)
    return (
        candidate.startswith("-")
        or "noi nhan" in candidate
        or "nhu tren" in candidate
        or "luu" in candidate
        or "(b/c)" in candidate
        or "b/c" in candidate
        or "kinh gui" in candidate
    )


def _is_regime_or_time_line(text: str) -> bool:
    candidate = norm(text)
    return (
        looks_like_party_title(text)
        or looks_like_state_header(text)
        or "thoi gian ky" in candidate
        or ("doc lap" in candidate and "tu do" in candidate)
    )


def _is_column_header_like(text: str) -> bool:
    candidate = norm(text)
    if not candidate:
        return False
    if candidate in COLUMN_HEADER_TERMS:
        return True
    return any(candidate.startswith(term + " ") for term in COLUMN_HEADER_TERMS)


def _looks_like_title_start(text: str) -> bool:
    raw = (text or "").strip()
    if not raw or _is_noise_line(raw):
        return False
    candidate = norm(raw)
    if any(candidate.startswith(prefix) for prefix in TITLE_START_PREFIXES):
        return True
    if _looks_like_issue_org_line(raw):
        return False
    alpha = [ch for ch in raw if ch.isalpha()]
    if not alpha:
        return False
    upper_ratio = sum(1 for ch in alpha if ch.isupper()) / len(alpha)
    return upper_ratio >= 0.8 and len(raw.split()) <= 8


def _looks_like_body_start(text: str) -> bool:
    candidate = norm(text).lstrip("-â€¢* ").strip()
    if re.match(r"^\d+\s*[-./]", candidate):
        return True
    if re.match(r"^[ivxlcdm]+\s*[-./]", candidate):
        return True
    body_prefixes = [
        "thuc hien",
        "can cu",
        "xet",
        "dieu ",
        "i.",
        "ii.",
        "iii.",
        "1.",
        "1/",
        "1-",
        "2.",
        "2/",
        "2-",
        "ho ten:",
        "ho va ten",
        "don vi cong tac",
        "hien la",
        "ngay vao dang",
        "toi ten",
        "nay toi",
        "vao luc",
        "dia diem",
        "thoi gian:",
        "1. thanh phan",
        "2. noi dung",
        "qua ket qua",
        "to kiem tra bao cao",
        "to giam sat bao cao",
        "nguoi lam don",
    ]
    return any(candidate.startswith(prefix) for prefix in body_prefixes)


def _is_stamp_or_incoming_noise(text: str) -> bool:
    candidate = norm(text)
    if not candidate:
        return False
    if any(keyword in candidate for keyword in STAMP_NOISE_KEYWORDS):
        return True
    if re.search(r"\bso[:.]?\s*\d+.*\bngay\b", candidate):
        return True
    return False


def _is_role_keyword_line(text: str) -> bool:
    candidate = norm(text)
    if PREFIX_ONLY_RE.match(candidate):
        return True
    return any(keyword in candidate for keyword in ROLE_NORM_KEYWORDS)


def _same_column(a: dict, b: dict, page: dict, max_diff: float = 0.18) -> bool:
    return abs(_line_center_x(a, page) - _line_center_x(b, page)) <= max_diff


def _looks_like_issue_org_line(text: str) -> bool:
    candidate = norm(text)
    if not candidate:
        return False
    org_keywords = [
        "dang uy", "huyen uy", "dang bo", "chi bo", "ban ", "to ", "doan ",
        "ubkt", "uy ban", "bch", "hoi ", "ubnd", "ubmttq", "mat tran",
        "doan tncs", "hoi ccb", "hoi lhpn", "truong", "phong",
    ]
    if any(keyword in candidate for keyword in org_keywords):
        return True
    alpha = [ch for ch in text if ch.isalpha()]
    if not alpha:
        return False
    upper_ratio = sum(1 for ch in alpha if ch.isupper()) / len(alpha)
    return upper_ratio >= 0.72


def _looks_like_role_line(text: str) -> bool:
    raw = (text or "").strip()
    if not raw or _is_noise_line(raw):
        return False
    if _is_column_header_like(raw):
        return False
    if _is_recipient_like_line(raw):
        return False
    if _is_stamp_or_incoming_noise(raw):
        return False
    if len(raw) > 70 and sum(1 for ch in raw if ch.islower()) > 10:
        return False
    if raw.startswith("(") and raw.endswith(")"):
        return True
    upper_ratio = sum(1 for ch in raw if ch.isupper()) / max(1, sum(1 for ch in raw if ch.isalpha()))
    return _is_role_keyword_line(raw) or bool(ROLE_LINE_RE.search(raw)) or (upper_ratio >= 0.8 and len(raw.split()) <= 8 and len(raw) >= 8)


def _strong_role_line(text: str) -> bool:
    raw = (text or "").strip()
    if not raw or _is_noise_line(raw):
        return False
    if _is_column_header_like(raw):
        return False
    if _is_recipient_like_line(raw):
        return False
    if _is_stamp_or_incoming_noise(raw):
        return False
    if len(raw) > 60 and sum(1 for ch in raw if ch.islower()) > 8:
        return False
    if raw.startswith("(") and raw.endswith(")"):
        return True
    if _is_role_keyword_line(raw):
        return True
    upper_ratio = sum(1 for ch in raw if ch.isupper()) / max(1, sum(1 for ch in raw if ch.isalpha()))
    return bool(ROLE_LINE_RE.search(raw)) or (upper_ratio >= 0.85 and len(raw.split()) <= 7)


def _find_anchor_index(lines: list[dict], prefix: str) -> int | None:
    for index, line in enumerate(lines):
        if norm(_line_text(line)).startswith(prefix):
            return index
    return None


def _collect_block_from_anchor(lines: list[dict], anchor_index: int, *, stop_prefixes: tuple[str, ...]) -> list[dict]:
    result = [lines[anchor_index]]
    anchor_line = lines[anchor_index]
    anchor_bottom = _line_bottom(anchor_line)
    anchor_top = _line_top(anchor_line)
    page_height_guess = max(_line_bottom(line) for line in lines) if lines else 0.0
    anchor_prefix = norm(_line_text(anchor_line))
    for line in lines[anchor_index + 1:]:
        text = _line_text(line)
        if not text:
            break
        if _line_top(line) + 2.0 < anchor_top:
            continue
        candidate = norm(text)
        if any(candidate.startswith(prefix) for prefix in stop_prefixes):
            break
        if _is_regime_or_time_line(text):
            break
        if _is_stamp_or_incoming_noise(text):
            break
        if looks_like_doc_number_line(text) or looks_like_date_line(text):
            break
        if _looks_like_body_start(text):
            break
        if anchor_prefix.startswith("kinh gui"):
            candidate = norm(text)
            if not candidate.startswith("-") and ":" in text:
                break
        if _line_top(line) - anchor_bottom > max(20.0, _line_height(anchor_line) * 1.8):
            break
        if _line_top(line) > page_height_guess * 0.92 and re.fullmatch(r"\d{1,4}", text.strip()):
            break
        result.append(line)
        anchor_bottom = _line_bottom(line)
        if len(result) >= 8:
            break
    return result


def _regime_header(page: dict, lines: list[dict]) -> list[dict]:
    result = []
    for line in lines:
        text = _line_text(line)
        if _line_center_x(line, page) < 0.52:
            continue
        if looks_like_party_title(text) or looks_like_state_header(text):
            result.append(line)
            continue
        candidate = norm(text)
        if "doc lap" in candidate and "tu do" in candidate and "hanh phuc" in candidate:
            result.append(line)
    return result


def _issue_org_fields(page: dict, lines: list[dict], stop_index: int | None) -> tuple[list[dict], list[dict]]:
    if stop_index is None:
        stop_index = min(len(lines), 12)
    header_lines = []
    for line in lines[:stop_index]:
        text = _line_text(line)
        if not text or _is_noise_line(text):
            continue
        if _looks_like_title_start(text) or _looks_like_body_start(text):
            break
        if norm(text).startswith("kinh gui"):
            break
        if looks_like_doc_number_line(text) or looks_like_date_line(text):
            continue
        if _is_regime_or_time_line(text):
            continue
        if _line_center_x(line, page) > 0.55:
            continue
        if text.strip() in {"*", "**", "***"}:
            continue
        header_lines.append(line)

    if not header_lines:
        return [], []
    while header_lines and not _looks_like_issue_org_line(_line_text(header_lines[-1])):
        header_lines.pop()
    if not header_lines:
        return [], []
    if len(header_lines) == 1:
        return [], header_lines

    split_idx = None
    for index in range(1, len(header_lines)):
        text = _line_text(header_lines[index])
        if _is_subordinate_start(text):
            split_idx = index
            break

    if split_idx is None and _is_location_continuation(_line_text(header_lines[1])):
        split_idx = None
    elif split_idx is None:
        first_text = _line_text(header_lines[0])
        if any(token in f" {norm(first_text)} " for token in [" xa ", " huyen ", " thi tran ", " phuong ", " tinh ", " quan "]):
            split_idx = 1

    if split_idx is None and len(header_lines) == 2:
        second_text = _line_text(header_lines[1])
        if _looks_like_issue_org_line(second_text) and not _is_location_continuation(second_text):
            split_idx = 1

    if split_idx is None:
        return [], header_lines
    return header_lines[:split_idx], header_lines[split_idx:]


def _doc_number_symbol(lines: list[dict]) -> list[dict]:
    for index, line in enumerate(lines[:12]):
        text = _line_text(line)
        if not looks_like_doc_number_line(text):
            continue
        if len(text.split()) > 8:
            continue
        candidate = norm(text)
        if any(bad in candidate for bad in ("dong chi", "noi dung", "thoi gian", "kinh gui", "noi nhan")):
            continue
        block = [line]
        last_bottom = _line_bottom(line)
        for nxt in lines[index + 1:index + 3]:
            nxt_text = _line_text(nxt)
            if not nxt_text:
                break
            candidate = norm(nxt_text)
            if "ngay" in candidate or candidate.startswith("kinh gui") or candidate.startswith("noi nhan"):
                break
            if _line_top(nxt) - last_bottom > max(18.0, _line_height(line) * 1.6):
                break
            if re.match(r"^[-â€“â€”/]", nxt_text.strip()) or nxt_text.strip().startswith("("):
                block.append(nxt)
                last_bottom = _line_bottom(nxt)
                continue
            break
        return block
    return []


def _place_date(page: dict, lines: list[dict]) -> list[dict]:
    for line in lines:
        text = _line_text(line)
        if _line_center_x(line, page) < 0.52:
            continue
        if looks_like_date_line(text):
            return [line]
    return []


def _doc_subject(page: dict, lines: list[dict]) -> list[dict]:
    addressee_idx = _find_anchor_index(lines, "kinh gui")
    explicit_start = None
    for idx, line in enumerate(lines[:18]):
        text = _line_text(line)
        candidate = norm(text)
        if candidate.startswith("v/v") or candidate.startswith("ve viec"):
            explicit_start = idx
            break

    if explicit_start is not None:
        subject_lines = []
        for line in lines[explicit_start:]:
            text = _line_text(line)
            candidate = norm(text)
            if not text:
                break
            if subject_lines and _is_column_header_like(text):
                break
            if candidate.startswith("kinh gui") or candidate.startswith("noi nhan"):
                break
            if _is_regime_or_time_line(text) or looks_like_doc_number_line(text) or looks_like_date_line(text):
                continue
            if _looks_like_body_start(text):
                break
            subject_lines.append(line)
            if len(subject_lines) >= 6:
                break
        return [line for line in subject_lines if not _is_noise_line(_line_text(line))]

    start_idx = None
    for idx, line in enumerate(lines[:24]):
        text = _line_text(line)
        if _looks_like_title_start(text):
            if norm(text).startswith("kinh gui"):
                continue
            start_idx = idx
            break
    if start_idx is None:
        return []

    subject_lines = [lines[start_idx]]
    for line in lines[start_idx + 1:]:
        text = _line_text(line)
        candidate = norm(text)
        if not text:
            break
        if subject_lines and _is_column_header_like(text):
            break
        if candidate.startswith("kinh gui") or candidate.startswith("noi nhan"):
            break
        if _looks_like_body_start(text):
            break
        if looks_like_doc_number_line(text):
            continue
        if _is_regime_or_time_line(text) or looks_like_date_line(text):
            continue
        if _looks_like_issue_org_line(text) and not _looks_like_title_start(text):
            continue
        subject_lines.append(line)
        if len(subject_lines) >= 8:
            break

    if addressee_idx is not None:
        subject_lines = [line for line in subject_lines if lines.index(line) < addressee_idx or _line_id(line) == _line_id(lines[start_idx])]
    return [line for line in subject_lines if not _is_noise_line(_line_text(line))]


def _addressee(lines: list[dict]) -> list[dict]:
    anchor_index = _find_anchor_index(lines, "kinh gui")
    if anchor_index is None:
        return []
    return _collect_block_from_anchor(
        lines,
        anchor_index,
        stop_prefixes=("noi nhan", "can cu", "dieu ", "i.", "ii.", "iii.", "iv.", "1.", "1-", "a.", "b."),
    )


def _recipients(page: dict, lines: list[dict]) -> list[dict]:
    anchor_index = _find_anchor_index(lines, "noi nhan")
    if anchor_index is None:
        return []
    block = [lines[anchor_index]]
    last_bottom = _line_bottom(lines[anchor_index])
    for line in lines[anchor_index + 1:]:
        text = _line_text(line)
        if not text:
            break
        candidate = norm(text)
        if candidate.startswith("kinh gui"):
            break
        if _line_center_x(line, page) > 0.58 and _looks_like_role_line(text):
            break
        if _line_top(line) - last_bottom > max(22.0, _line_height(line) * 1.8):
            break
        if _is_noise_line(text):
            break
        block.append(line)
        last_bottom = _line_bottom(line)
        if len(block) >= 8:
            break
    return block


def _signature_pages(task_pages: list[dict], signature_page: int | None = None) -> list[dict]:
    pages = sorted(task_pages, key=lambda page: page.get("page_index", 0))
    if signature_page is None:
        return pages
    preferred = [page for page in pages if page.get("page_index") == signature_page]
    later = [page for page in pages if page.get("page_index", 0) > signature_page]
    earlier = [page for page in pages if page.get("page_index", 0) < signature_page]
    return preferred + later + earlier


def _signers_for_page(page: dict, lines: list[dict]) -> list[tuple[list[dict], list[dict]]]:
    min_role_top = float(page.get("height") or 842.0) * 0.55
    role_anchor_indexes: list[int] = []
    for index, line in enumerate(lines):
        text = _line_text(line)
        if _line_top(line) < min_role_top:
            continue
        if not _strong_role_line(text):
            continue
        role_anchor_indexes.append(index)

    if not role_anchor_indexes:
        return []

    def build_role_block(anchor_index: int) -> list[dict]:
        anchor = lines[anchor_index]
        block = [anchor]
        for probe in range(anchor_index - 1, max(-1, anchor_index - 4), -1):
            candidate = lines[probe]
            candidate_text = _line_text(candidate)
            candidate_norm = norm(candidate_text)
            if not candidate_text:
                continue
            if not _same_column(candidate, anchor, page, max_diff=0.2):
                continue
            if _line_top(anchor) - _line_bottom(candidate) > max(26.0, _line_height(anchor) * 1.8):
                break
            if PREFIX_ONLY_RE.match(candidate_norm) or _strong_role_line(candidate_text):
                block.insert(0, candidate)
                continue
            break

        last_line = block[-1]
        for probe in range(anchor_index + 1, min(len(lines), anchor_index + 5)):
            candidate = lines[probe]
            candidate_text = _line_text(candidate)
            candidate_norm = norm(candidate_text)
            if not candidate_text:
                continue
            if _line_top(candidate) - _line_bottom(last_line) > max(28.0, _line_height(last_line) * 2.0):
                break
            if not _same_column(candidate, anchor, page, max_diff=0.2):
                continue
            if _is_name_candidate(candidate_text) or _is_loose_name_candidate(candidate_text):
                break
            if candidate_norm == "kiem" or _strong_role_line(candidate_text):
                block.append(candidate)
                last_line = candidate
                continue
            break
        return block

    def find_name_line(role_block: list[dict]) -> list[dict]:
        if not role_block:
            return []
        ref_line = role_block[-1]
        ref_x = _line_center_x(ref_line, page)
        ref_bottom = _line_bottom(ref_line)
        candidates: list[tuple[float, dict]] = []
        for candidate in lines:
            candidate_text = _line_text(candidate)
            if not candidate_text or _is_noise_line(candidate_text):
                continue
            if _line_top(candidate) <= ref_bottom:
                continue
            if _line_top(candidate) - ref_bottom > 150.0:
                continue
            if abs(_line_center_x(candidate, page) - ref_x) > 0.22:
                continue
            if _strong_role_line(candidate_text) or _is_recipient_like_line(candidate_text) or _is_stamp_or_incoming_noise(candidate_text):
                continue
            if not (_is_name_candidate(candidate_text) or _is_loose_name_candidate(candidate_text)):
                continue
            gap = _line_top(candidate) - ref_bottom
            col_penalty = abs(_line_center_x(candidate, page) - ref_x) * 100.0
            candidates.append((gap + col_penalty, candidate))
        if not candidates:
            return []
        candidates.sort(key=lambda item: item[0])
        return [candidates[0][1]]

    signers: list[tuple[list[dict], list[dict]]] = []
    seen_role_sets: set[tuple[str, ...]] = set()
    seen_name_ids: set[str] = set()
    for anchor_index in role_anchor_indexes:
        role_block = build_role_block(anchor_index)
        role_ids = tuple(_line_id(line) for line in role_block if _line_id(line))
        if not role_ids or role_ids in seen_role_sets:
            continue
        name_block = find_name_line(role_block)
        if not name_block:
            continue
        name_id = _line_id(name_block[0])
        if name_id in seen_name_ids:
            continue
        seen_role_sets.add(role_ids)
        seen_name_ids.add(name_id)
        signers.append((role_block, name_block))

    signers.sort(
        key=lambda item: (
            round(_line_top(item[0][0]), 2),
            round(_line_center_x(item[0][0], page), 3),
        )
    )
    return signers


def build_output_for_task(task: dict) -> dict:
    pages = sorted(task.get("pages") or [], key=lambda item: item.get("page_index", 0))
    drafts: list[FieldDraft] = []
    relations = []

    if not pages:
        return {"field_instances": [], "relations": []}

    first_page = pages[0]
    first_lines = list(first_page.get("lines") or [])
    stop_index = None
    for index, line in enumerate(first_lines):
        text = _line_text(line)
        if looks_like_doc_number_line(text) or looks_like_date_line(text):
            stop_index = index
            break

    number_lines = _doc_number_symbol(first_lines)
    if number_lines:
        drafts.append(FieldDraft("DOC_NUMBER_SYMBOL", first_page["page_index"], number_lines))

    regime_lines = _regime_header(first_page, first_lines)
    if regime_lines:
        drafts.append(FieldDraft("REGIME_HEADER", first_page["page_index"], regime_lines))

    place_date_lines = _place_date(first_page, first_lines)
    if place_date_lines:
        drafts.append(FieldDraft("PLACE_DATE", first_page["page_index"], place_date_lines))

    subject_lines = _doc_subject(first_page, first_lines)
    superior_lines, name_lines = _issue_org_fields(first_page, first_lines, stop_index)

    excluded_subject_ids = {
        _line_id(line)
        for line in regime_lines + place_date_lines + number_lines + superior_lines + name_lines
    }
    cleaned_subject_lines = []
    for line in subject_lines:
        line_id = _line_id(line)
        if line_id in excluded_subject_ids:
            continue
        text = _line_text(line)
        if "thá»i gian kÃ½" in norm(text):
            continue
        if _line_center_x(line, first_page) > 0.62 and not looks_like_doc_number_line(text):
            continue
        cleaned_subject_lines.append(line)
    subject_lines = cleaned_subject_lines

    if superior_lines:
        drafts.append(FieldDraft("ISSUE_ORG_SUPERIOR", first_page["page_index"], superior_lines))
    if name_lines:
        drafts.append(FieldDraft("ISSUE_ORG_NAME", first_page["page_index"], name_lines))
    if subject_lines:
        drafts.append(FieldDraft("DOC_SUBJECT", first_page["page_index"], subject_lines))

    addressee_lines = _addressee(first_lines)
    if addressee_lines:
        drafts.append(FieldDraft("ADDRESSEE", first_page["page_index"], addressee_lines))

    for page in reversed(pages):
        recipient_lines = _recipients(page, list(page.get("lines") or []))
        if recipient_lines:
            drafts.append(FieldDraft("RECIPIENTS", page["page_index"], recipient_lines))
            break

    signature_page = (task.get("page_selection") or {}).get("signature_page")
    for page in _signature_pages(pages, signature_page=signature_page):
        signer_pairs = _signers_for_page(page, list(page.get("lines") or []))
        if not signer_pairs:
            continue
        if drafts and drafts[-1].label == "RECIPIENTS" and drafts[-1].page_index == page["page_index"]:
            signer_line_ids = {
                _line_id(line)
                for role_lines, name_lines in signer_pairs
                for line in (role_lines + name_lines)
            }
            recipient_filtered = [
                line for line in drafts[-1].lines
                if _line_id(line) not in signer_line_ids and not _looks_like_role_line(_line_text(line))
            ]
            drafts[-1].lines = recipient_filtered
        for role_lines, name_lines in signer_pairs:
            role_field_idx = len(drafts) + 1
            drafts.append(FieldDraft("SIGNER_ROLE", page["page_index"], role_lines))
            drafts.append(FieldDraft("SIGNER_NAME", page["page_index"], name_lines))
            relations.append((role_field_idx, role_field_idx + 1))
        break

    drafts = [draft for draft in drafts if draft.lines]

    field_instances = []
    for index, draft in enumerate(drafts, 1):
        field_instances.append(_field_payload(f"f{index}", draft))

    relation_payloads = []
    for rel_index, (role_idx, name_idx) in enumerate(relations, 1):
        relation_payloads.append({
            "relation_id": f"r{rel_index}",
            "type": "signed_by",
            "from_field_id": f"f{role_idx}",
            "to_field_id": f"f{name_idx}",
            "confidence": None,
        })

    return {
        "schema": ONTOLOGY_ID,
        "field_instances": field_instances,
        "relations": relation_payloads,
    }


def run_batch(input_dir: Path, output_dir: Path, resume: bool) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    review_files = []
    failures = []
    processed = 0

    for input_path in sorted(input_dir.glob("*.json")):
        output_path = output_dir / input_path.name
        if resume and output_path.exists():
            continue
        task = json.loads(input_path.read_text(encoding="utf-8"))
        canonical_path = Path(task["source_canonical_json"])
        canonical_doc = json.loads(canonical_path.read_text(encoding="utf-8"))
        payload = build_output_for_task(task)
        result = validate_label_output_detailed(payload, canonical_doc, llm_name="autolabel_v3")
        if result["errors"]:
            failures.append({
                "file": input_path.name,
                "errors": result["errors"],
            })
            continue
        if result["warnings"]:
            review_files.append({
                "file": input_path.name,
                "warnings": result["warnings"],
            })
        write_json(output_path, result["normalized"])
        processed += 1

    report = {
        "processed": processed,
        "review_files": review_files,
        "failures": failures,
    }
    write_json(output_dir / "_autolabel_report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not failures else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Heuristic autolabel generator for ontology v3 batch json_input.")
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    return run_batch(Path(args.input_dir), Path(args.output_dir), resume=args.resume)


if __name__ == "__main__":
    raise SystemExit(main())

