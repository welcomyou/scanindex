from __future__ import annotations

import json
import math
import os
import re
import unicodedata
from concurrent.futures import ProcessPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .common import read_json, seeded_rng, write_json
from .config import FIELD_SPECS, LABELS, FieldSpec


_RE_WS = re.compile(r"\s+")
_RE_DOC_NUMBER = re.compile(r"\bso\s*[:.]?\s*[\w/-]+", re.IGNORECASE)
_RE_PLACE_DATE = re.compile(r"\b(?:ngay|thang|nam)\b", re.IGNORECASE)
_RE_KINH_GUI = re.compile(r"\bkinh\s*gui\b", re.IGNORECASE)
_RE_NOI_NHAN = re.compile(r"\bnoi\s*nhan\b", re.IGNORECASE)
_RE_SIGNER_PREFIX = re.compile(r"\b(?:t/?m|k/?t|t/?l|tuq|q\.)\b", re.IGNORECASE)
_RE_REGIME = re.compile(r"(?:cong\s+hoa|dang\s+cong\s+san)", re.IGNORECASE)
_RE_SIGNER_ROLE_WORD = re.compile(
    r"\b(?:pho|truong|chanh|bi\s+thu|chu\s+tich|pho\s+chu\s+tich|giam\s+doc|uy\s+vien|van\s+phong|thu\s+ky)\b",
    re.IGNORECASE,
)
_RE_SIGNER_CONTEXT = re.compile(
    r"\b(?:t/?m|k/?t|t/?l|tuq|q\.|bi\s+thu|chu\s+tich|pho\s+chu\s+tich|chanh\s+van\s+phong|pho\s+chanh|truong\s+ban|pho\s+truong|giam\s+doc|cuc\s+truong)\b",
    re.IGNORECASE,
)
_RE_SIGNER_FOOTER_PREFIX = re.compile(r"\b(?:t/?m|k/?t|t/?l|tuq|q\.)\b", re.IGNORECASE)
_RE_REGIME_MOTTO = re.compile(r"\b(?:doc\s+lap|tu\s+do|hanh\s+phuc)\b", re.IGNORECASE)
_RE_NOISE_ANCHOR = re.compile(
    r"\b(?:noi\s+nhan|kinh\s+gui|ngay|thang|nam|so|cong\s+hoa|dang\s+cong\s+san)\b",
    re.IGNORECASE,
)
STYLE_FEATURES_ENABLED = os.environ.get("LIGHTGBM_ENABLE_STYLE_FEATURES", "").lower() in {"1", "true", "yes", "on"}
STYLE_FEATURE_SET = os.environ.get("LIGHTGBM_STYLE_FEATURE_SET", "v2").lower()
FAST_CANDIDATES_ENABLED = os.environ.get("LIGHTGBM_FAST_CANDIDATES", "1").lower() not in {
    "0",
    "false",
    "no",
    "off",
}


@dataclass(frozen=True)
class OCRWord:
    id: str
    page_index: int
    line_id: str
    order: int
    text: str
    bbox: tuple[float, float, float, float]
    has_space_after: bool
    fg_gray: float


@dataclass(frozen=True)
class OCRLine:
    id: str
    page_index: int
    order: int
    text: str
    bbox: tuple[float, float, float, float]
    font_size: float
    fg_gray: float
    block_id: int | None
    word_ids: tuple[str, ...]


@dataclass(frozen=True)
class OCRPage:
    page_index: int
    width: float
    height: float
    lines: tuple[OCRLine, ...]
    words: tuple[OCRWord, ...]
    font_median: float
    line_height_median: float
    fg_gray_median: float


@dataclass(frozen=True)
class OCRDocument:
    doc_id: str
    relative_pdf_path: str
    split: str
    selected_pages: tuple[int, ...]
    primary_page: int
    signature_page: int | None
    pages: dict[int, OCRPage]
    line_lookup: dict[str, OCRLine]
    word_lookup: dict[str, OCRWord]
    doc_kind: str


@dataclass(frozen=True)
class FieldInstance:
    field_id: str
    label: str
    page_index: int
    line_ids: tuple[str, ...]
    word_ids: tuple[str, ...]
    text: str
    bbox: tuple[float, float, float, float]


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    field: str
    source_kind: str
    page_index: int
    page_role: str
    line_ids: tuple[str, ...]
    word_ids: tuple[str, ...]
    word_ids_set: frozenset[str]
    text: str
    normalized_text: str
    bbox: tuple[float, float, float, float]


def _strip_accents(text: str) -> str:
    text = (text or "").replace("Đ", "D").replace("đ", "d").replace("Ä", "D").replace("Ä‘", "d")
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def normalize_text(text: str) -> str:
    stripped = _strip_accents(text)
    stripped = stripped.lower()
    stripped = _RE_WS.sub(" ", stripped)
    return stripped.strip()


def _bbox_union(boxes: Iterable[tuple[float, float, float, float]]) -> tuple[float, float, float, float]:
    boxes = list(boxes)
    if not boxes:
        return (0.0, 0.0, 0.0, 0.0)
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _line_visual_order(line: OCRLine) -> tuple[float, float, int]:
    x0, y0, _, _ = line.bbox
    return (round(y0, 2), round(x0, 2), line.order)


def _median(values: Iterable[float]) -> float:
    clean = sorted(float(value) for value in values if value is not None and math.isfinite(float(value)))
    if not clean:
        return 0.0
    mid = len(clean) // 2
    if len(clean) % 2:
        return clean[mid]
    return (clean[mid - 1] + clean[mid]) / 2.0


def load_ocr_document(doc_meta: dict) -> OCRDocument:
    canonical = read_json(doc_meta["source_canonical_json"])
    line_lookup: dict[str, OCRLine] = {}
    word_lookup: dict[str, OCRWord] = {}
    pages: dict[int, OCRPage] = {}
    for page in canonical["pages"]:
        words = []
        raw_page_words = list(page.get("words") or [])
        if not raw_page_words:
            for raw_line in page.get("lines", []):
                line_id = raw_line.get("id") or raw_line.get("line_id")
                for order, raw_word in enumerate(raw_line.get("words") or []):
                    item = dict(raw_word)
                    item.setdefault("line_id", line_id)
                    item.setdefault("order", order)
                    raw_page_words.append(item)
        for raw_word in raw_page_words:
            word = OCRWord(
                id=raw_word.get("id") or raw_word["word_id"],
                page_index=page["page_index"],
                line_id=raw_word["line_id"],
                order=int(raw_word.get("order", 0)),
                text=raw_word.get("text") or raw_word.get("ocr_text") or "",
                bbox=tuple(raw_word["bbox"]),
                has_space_after=bool(raw_word.get("has_space_after", True)),
                fg_gray=float(raw_word.get("fg_gray") if raw_word.get("fg_gray") is not None else 0.0),
            )
            words.append(word)
            word_lookup[word.id] = word
        lines = []
        for order, raw_line in enumerate(page.get("lines", [])):
            line_id = raw_line.get("id") or raw_line.get("line_id")
            line = OCRLine(
                id=line_id,
                page_index=page["page_index"],
                order=int(raw_line.get("order", order)),
                text=raw_line.get("text") or raw_line.get("ocr_text") or "",
                bbox=tuple(raw_line["bbox"]),
                font_size=float(raw_line.get("font_size") or 0.0),
                fg_gray=float(raw_line.get("fg_gray") if raw_line.get("fg_gray") is not None else 0.0),
                block_id=raw_line.get("block_id"),
                word_ids=tuple(raw_line.get("word_ids") or ()),
            )
            lines.append(line)
            line_lookup[line.id] = line
        pages[page["page_index"]] = OCRPage(
            page_index=page["page_index"],
            width=float(page["width"]),
            height=float(page["height"]),
            lines=tuple(lines),
            words=tuple(words),
            font_median=_median(line.font_size for line in lines if line.font_size),
            line_height_median=_median(max(0.0, line.bbox[3] - line.bbox[1]) for line in lines),
            fg_gray_median=_median(line.fg_gray for line in lines),
        )
    return OCRDocument(
        doc_id=doc_meta["doc_id"],
        relative_pdf_path=doc_meta["relative_pdf_path"],
        split=doc_meta["split"],
        selected_pages=tuple(doc_meta["selected_pages"]),
        primary_page=int(doc_meta["primary_page"]),
        signature_page=doc_meta.get("signature_page"),
        pages=pages,
        line_lookup=line_lookup,
        word_lookup=word_lookup,
        doc_kind=doc_meta.get("doc_kind", "regular"),
    )


def load_field_instances(doc: OCRDocument, label_output_json: str | Path) -> tuple[list[FieldInstance], list[dict]]:
    payload = read_json(label_output_json)
    annotation = payload.get("annotation", payload)
    fields = []
    for raw in annotation.get("field_instances", []):
        line_ids = tuple(raw.get("line_ids") or ())
        word_ids = tuple(raw.get("word_ids") or ())
        if not word_ids and line_ids:
            resolved_words: list[str] = []
            for line_id in line_ids:
                line = doc.line_lookup.get(line_id)
                if line:
                    resolved_words.extend(line.word_ids)
            word_ids = tuple(dict.fromkeys(resolved_words))
        if not line_ids and word_ids:
            resolved_lines: list[str] = []
            for word_id in word_ids:
                word = doc.word_lookup.get(word_id)
                if word:
                    resolved_lines.append(word.line_id)
            line_ids = tuple(dict.fromkeys(resolved_lines))
        boxes = []
        for word_id in word_ids:
            word = doc.word_lookup.get(word_id)
            if word:
                boxes.append(word.bbox)
        if not boxes:
            for line_id in line_ids:
                line = doc.line_lookup.get(line_id)
                if line:
                    boxes.append(line.bbox)
        fields.append(
            FieldInstance(
                field_id=raw["field_id"],
                label=raw["label"],
                page_index=int(raw["page_index"]),
                line_ids=line_ids,
                word_ids=word_ids,
                text=raw.get("text") or "",
                bbox=_bbox_union(boxes),
            )
        )
    return fields, annotation.get("relations", [])


def _candidate_pages(doc: OCRDocument, spec: FieldSpec) -> list[tuple[int, str]]:
    selected = list(doc.selected_pages or (0,))
    if not selected:
        selected = [0]
    if spec.page_preference == "signature":
        # Signature/recipients annotations are occasionally on a page that was
        # not marked as signature by the old manifest. Scan anchor pages too.
        anchor_pages: list[int] = []
        for page_index, page in sorted(doc.pages.items()):
            page_text = "\n".join(line.text for line in page.lines)
            normalized = normalize_text(page_text)
            if spec.label == "RECIPIENTS":
                has_anchor = bool(_RE_NOI_NHAN.search(normalized))
            else:
                bottom_text = "\n".join(
                    line.text
                    for line in page.lines
                    if line.bbox[1] / max(page.height, 1.0) > 0.38
                )
                bottom_normalized = normalize_text(bottom_text)
                has_anchor = bool(_RE_NOI_NHAN.search(normalized) or _RE_SIGNER_FOOTER_PREFIX.search(bottom_normalized))
            if has_anchor and page_index not in selected:
                anchor_pages.append(page_index)
        selected.extend(anchor_pages)
    pairs: list[tuple[int, str]] = []
    for page_index in selected:
        if page_index == doc.primary_page:
            role = "primary"
        elif doc.signature_page is not None and page_index == doc.signature_page:
            role = "signature"
        else:
            role = "selected_other"
        if spec.page_preference == "primary" and role != "primary":
            continue
        pairs.append((page_index, role))
    return pairs or [(doc.primary_page, "primary")]


def _zone_ok(
    field: str,
    bbox: tuple[float, float, float, float],
    page_w: float,
    page_h: float,
    page_role: str,
    normalized_text: str,
) -> bool:
    x0, y0, x1, y1 = bbox
    cx = (x0 + x1) / 2.0 / max(page_w, 1.0)
    top = y0 / max(page_h, 1.0)
    bottom = y1 / max(page_h, 1.0)
    cy = (y0 + y1) / 2.0 / max(page_h, 1.0)
    if field == "REGIME_HEADER":
        return top < 0.32 and cx > 0.42
    if field in {"ISSUE_ORG_SUPERIOR", "ISSUE_ORG_NAME"}:
        return top < 0.36 and cx < 0.72
    if field == "DOC_NUMBER_SYMBOL":
        return top < 0.45
    if field == "PLACE_DATE":
        return top < 0.45 or page_role == "signature"
    if field == "DOC_SUBJECT":
        return cy < 0.60
    if field == "ADDRESSEE":
        return top < 0.75
    if field == "RECIPIENTS":
        return bottom > 0.42 or bool(_RE_NOI_NHAN.search(normalized_text))
    if field == "SIGNER_ROLE":
        return (bottom > 0.40 or page_role == "signature") and (cx > 0.15 or x0 / max(page_w, 1.0) > 0.08)
    if field == "SIGNER_NAME":
        return (
            (bottom > 0.40 or page_role == "signature")
            and (cx > 0.15 or x0 / max(page_w, 1.0) > 0.08)
            and not bool(_RE_NOI_NHAN.search(normalized_text))
        )
    return True


def _reconstruct_words_text(words: list[OCRWord]) -> str:
    if not words:
        return ""
    parts = [words[0].text]
    for prev, word in zip(words, words[1:]):
        if prev.has_space_after:
            parts.append(" ")
        parts.append(word.text)
    return "".join(parts)


def _regex_flags(normalized_text: str) -> dict[str, int]:
    return {
        "rx_doc_number": int(bool(_RE_DOC_NUMBER.search(normalized_text))),
        "rx_place_date": int(bool(_RE_PLACE_DATE.search(normalized_text))),
        "rx_kinh_gui": int(bool(_RE_KINH_GUI.search(normalized_text))),
        "rx_noi_nhan": int(bool(_RE_NOI_NHAN.search(normalized_text))),
        "rx_signer_prefix": int(bool(_RE_SIGNER_PREFIX.search(normalized_text))),
        "rx_signer_role_word": int(bool(_RE_SIGNER_ROLE_WORD.search(normalized_text))),
        "rx_regime": int(bool(_RE_REGIME.search(normalized_text))),
        "rx_regime_motto": int(bool(_RE_REGIME_MOTTO.search(normalized_text))),
    }


def _tokenize_visible_text(text: str) -> list[str]:
    return [token for token in text.replace("\n", " ").split(" ") if token]


def _looks_like_name_text(text: str) -> bool:
    tokens = [token.strip(".,;:()[]{}<>-") for token in _tokenize_visible_text(text)]
    tokens = [token for token in tokens if token]
    if not (2 <= len(tokens) <= 6):
        return False
    if any(any(ch.isdigit() for ch in token) for token in tokens):
        return False
    if any(marker in text for marker in (":", "/", ";", ",")):
        return False
    upper_like = 0
    title_like = 0
    alpha_tokens = 0
    for token in tokens:
        letters = "".join(ch for ch in token if ch.isalpha())
        if not letters:
            continue
        alpha_tokens += 1
        if letters.isupper():
            upper_like += 1
        elif letters[0].isupper():
            title_like += 1
    if alpha_tokens < 2:
        return False
    return (upper_like + title_like) >= max(2, alpha_tokens - 1)


def _text_stats(text: str) -> dict[str, float]:
    text = text or ""
    chars = len(text)
    if chars == 0:
        return {
            "char_count": 0.0,
            "digit_ratio": 0.0,
            "upper_ratio": 0.0,
            "alpha_ratio": 0.0,
            "punct_ratio": 0.0,
            "has_colon": 0.0,
            "has_slash": 0.0,
            "has_hyphen": 0.0,
            "has_paren": 0.0,
            "has_bullet": 0.0,
            "newline_count": 0.0,
        }
    digit_count = 0
    upper_count = 0
    alpha_count = 0
    punct_count = 0
    for ch in text:
        if ch.isdigit():
            digit_count += 1
        if ch.isupper():
            upper_count += 1
        if ch.isalpha():
            alpha_count += 1
        if (not ch.isalnum()) and (not ch.isspace()):
            punct_count += 1
    tokens = [token for token in text.replace("\n", " ").split(" ") if token]
    token_count = len(tokens)
    comma_count = text.count(",")
    semicolon_count = text.count(";")
    colon_count = text.count(":")
    slash_count = text.count("/")
    title_like = 0
    for token in tokens:
        core = token.strip(".,;:()[]{}<>-")
        if not core:
            continue
        if core[0].isupper():
            title_like += 1
    return {
        "char_count": float(chars),
        "token_count": float(token_count),
        "digit_count": float(digit_count),
        "digit_ratio": digit_count / chars,
        "upper_ratio": upper_count / chars,
        "alpha_ratio": alpha_count / chars,
        "punct_ratio": punct_count / chars,
        "has_colon": float(":" in text),
        "has_slash": float("/" in text),
        "has_hyphen": float("-" in text or "–" in text),
        "has_paren": float("(" in text or ")" in text),
        "has_bullet": float(text.strip().startswith(("-", "•", "*"))),
        "newline_count": float(text.count("\n")),
        "comma_count": float(comma_count),
        "semicolon_count": float(semicolon_count),
        "colon_count": float(colon_count),
        "slash_count": float(slash_count),
        "title_ratio": (title_like / token_count) if token_count else 0.0,
    }


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _stddev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    avg = _mean(values)
    return math.sqrt(sum((value - avg) ** 2 for value in values) / len(values))


def _build_features(
    field: str,
    source_kind: str,
    page: OCRPage,
    page_role: str,
    line_ids: tuple[str, ...],
    word_ids: tuple[str, ...],
    bbox: tuple[float, float, float, float],
    text: str,
    normalized_text: str,
    doc: OCRDocument,
) -> dict[str, float]:
    x0, y0, x1, y1 = bbox
    page_w = max(page.width, 1.0)
    page_h = max(page.height, 1.0)
    width = max(x1 - x0, 0.0)
    height = max(y1 - y0, 0.0)
    cx = (x0 + x1) / 2.0
    cy = (y0 + y1) / 2.0
    line_objs = [doc.line_lookup[line_id] for line_id in line_ids if line_id in doc.line_lookup]
    word_objs = [doc.word_lookup[word_id] for word_id in word_ids if word_id in doc.word_lookup]
    sorted_lines = sorted(line_objs, key=_line_visual_order)
    gaps = []
    for prev, curr in zip(sorted_lines, sorted_lines[1:]):
        gaps.append(max(0.0, curr.bbox[1] - prev.bbox[3]))
    font_sizes = [line.font_size for line in line_objs if line.font_size]
    line_heights = [max(0.0, line.bbox[3] - line.bbox[1]) for line in line_objs]
    word_grays = [word.fg_gray for word in word_objs if word.fg_gray is not None]
    line_grays = [line.fg_gray for line in line_objs if line.fg_gray is not None]
    fg_grays = word_grays or line_grays
    font_avg = _mean(font_sizes)
    font_max = max(font_sizes) if font_sizes else 0.0
    font_min = min(font_sizes) if font_sizes else 0.0
    font_range = font_max - font_min if font_sizes else 0.0
    font_page_median = page.font_median
    line_height_avg = _mean(line_heights)
    line_height_max = max(line_heights) if line_heights else 0.0
    line_height_min = min(line_heights) if line_heights else 0.0
    fg_gray_avg = _mean(fg_grays)
    fg_gray_max = max(fg_grays) if fg_grays else 0.0
    fg_gray_min = min(fg_grays) if fg_grays else 0.0
    darkness_values = [max(0.0, min(1.0, 1.0 - (value / 255.0))) for value in fg_grays]
    darkness_avg = _mean(darkness_values)
    darkness_max = max(darkness_values) if darkness_values else 0.0
    darkness_min = min(darkness_values) if darkness_values else 0.0
    page_darkness_median = max(0.0, min(1.0, 1.0 - (page.fg_gray_median / 255.0)))
    blocks = {line.block_id for line in line_objs if line.block_id is not None}
    features = {
        "page_index": float(page.page_index),
        "is_primary_page": float(page_role == "primary"),
        "is_signature_page": float(page_role == "signature"),
        "is_selected_other_page": float(page_role == "selected_other"),
        "num_lines": float(len(line_ids)),
        "num_words": float(len(word_ids)),
        "x0_norm": x0 / page_w,
        "y0_norm": y0 / page_h,
        "x1_norm": x1 / page_w,
        "y1_norm": y1 / page_h,
        "w_norm": width / page_w,
        "h_norm": height / page_h,
        "cx_norm": cx / page_w,
        "cy_norm": cy / page_h,
        "area_norm": (width * height) / (page_w * page_h),
        "left_margin_norm": x0 / page_w,
        "right_margin_norm": max(0.0, page_w - x1) / page_w,
        "top_margin_norm": y0 / page_h,
        "bottom_margin_norm": max(0.0, page_h - y1) / page_h,
        "aspect_ratio": width / max(height, 1.0),
        "font_avg": font_avg,
        "font_max": font_max,
        "font_min": font_min,
        "line_gap_avg_norm": (sum(gaps) / len(gaps)) / page_h if gaps else 0.0,
        "line_gap_max_norm": max(gaps) / page_h if gaps else 0.0,
        "block_count": float(len(blocks)),
        "same_block": float(len(blocks) <= 1 and len(line_ids) > 0),
        "is_line_span_source": float(source_kind == "line_span"),
        "is_word_window_source": float(source_kind == "word_window"),
        "is_anchor_block_source": float(source_kind == "anchor_block"),
        "is_same_column_block_source": float(source_kind == "same_column_block"),
        "is_top_word_cluster_source": float(source_kind == "top_word_cluster"),
        "is_y_band_window_source": float(source_kind == "y_band_window"),
        "is_signer_role_above_name_source": float(source_kind == "signer_role_above_name"),
        "is_top_band": float((y0 / page_h) < 0.25),
        "is_bottom_band": float((y1 / page_h) > 0.70),
        "is_left_half": float((cx / page_w) < 0.50),
        "is_right_half": float((cx / page_w) >= 0.50),
        "center_bias": max(0.0, 1.0 - min(1.0, abs((cx / page_w) - 0.5) * 2.0)),
        "looks_like_name": float(_looks_like_name_text(text)),
        "starts_kinh_gui": float(normalized_text.startswith("kinh gui")),
        "starts_noi_nhan": float(normalized_text.startswith("noi nhan")),
        "starts_signer_prefix": float(bool(_RE_SIGNER_PREFIX.match(normalized_text))),
    }
    if STYLE_FEATURES_ENABLED:
        style_features = {
            "font_range": font_range,
            "font_std": _stddev(font_sizes),
            "font_avg_rel_page_median": font_avg / max(font_page_median, 1.0) if font_avg else 0.0,
            "font_max_rel_page_median": font_max / max(font_page_median, 1.0) if font_max else 0.0,
            "font_min_rel_page_median": font_min / max(font_page_median, 1.0) if font_min else 0.0,
            "fg_gray_range": fg_gray_max - fg_gray_min if fg_grays else 0.0,
            "fg_gray_avg_delta_page_median": fg_gray_avg - page.fg_gray_median if fg_grays else 0.0,
            "fg_gray_min_delta_page_median": fg_gray_min - page.fg_gray_median if fg_grays else 0.0,
            "fg_gray_max_delta_page_median": fg_gray_max - page.fg_gray_median if fg_grays else 0.0,
            "darkness_range": darkness_max - darkness_min if darkness_values else 0.0,
            "darkness_avg_delta_page_median": darkness_avg - page_darkness_median if darkness_values else 0.0,
            "darkness_min_delta_page_median": darkness_min - page_darkness_median if darkness_values else 0.0,
            "darkness_max_delta_page_median": darkness_max - page_darkness_median if darkness_values else 0.0,
            "is_font_larger_than_page": float(font_avg > font_page_median * 1.08) if font_avg and font_page_median else 0.0,
            "is_font_smaller_than_page": float(font_avg < font_page_median * 0.92) if font_avg and font_page_median else 0.0,
            "is_darker_than_page": float(darkness_avg > page_darkness_median + 0.05) if darkness_values else 0.0,
            "style_emphasis": max(0.0, (font_avg / max(font_page_median, 1.0)) - 1.0)
            + max(0.0, darkness_avg - page_darkness_median),
        }
        if STYLE_FEATURE_SET == "v1":
            style_features.update(
                {
                    "line_height_avg": line_height_avg,
                    "line_height_max": line_height_max,
                    "line_height_min": line_height_min,
                    "line_height_range": line_height_max - line_height_min if line_heights else 0.0,
                    "line_height_avg_rel_page_median": line_height_avg / max(page.line_height_median, 1.0) if line_height_avg else 0.0,
                    "line_height_max_rel_page_median": line_height_max / max(page.line_height_median, 1.0) if line_height_max else 0.0,
                    "fg_gray_avg": fg_gray_avg,
                    "fg_gray_max": fg_gray_max,
                    "fg_gray_min": fg_gray_min,
                    "darkness_avg": darkness_avg,
                    "darkness_max": darkness_max,
                    "darkness_min": darkness_min,
                }
            )
        features.update(style_features)
    features.update(_text_stats(text))
    features.update(_regex_flags(normalized_text))
    return features


def _make_candidate(
    doc: OCRDocument,
    field: str,
    source_kind: str,
    page_index: int,
    page_role: str,
    line_ids: tuple[str, ...],
    word_ids: tuple[str, ...],
    text: str,
    bbox: tuple[float, float, float, float],
    candidate_suffix: str,
) -> Candidate:
    normalized_text = normalize_text(text)
    return Candidate(
        candidate_id=f"{doc.doc_id}:{field}:{source_kind}:{candidate_suffix}",
        field=field,
        source_kind=source_kind,
        page_index=page_index,
        page_role=page_role,
        line_ids=line_ids,
        word_ids=word_ids,
        word_ids_set=frozenset(word_ids),
        text=text,
        normalized_text=normalized_text,
        bbox=bbox,
    )


def _line_span_candidates(doc: OCRDocument, field: str, spec: FieldSpec) -> list[Candidate]:
    candidates: list[Candidate] = []
    for page_index, page_role in _candidate_pages(doc, spec):
        page = doc.pages.get(page_index)
        if page is None:
            continue
        visual_lines = sorted([line for line in page.lines if (line.text or "").strip()], key=_line_visual_order)
        for start in range(len(visual_lines)):
            if FAST_CANDIDATES_ENABLED and field == "RECIPIENTS":
                # Exact RECIPIENTS candidates in the corpus start at the "Noi nhan"
                # anchor. Starting before the anchor mostly absorbs signer-role
                # lines and creates high-scoring but over-wide noisy candidates.
                if not _RE_NOI_NHAN.search(normalize_text(visual_lines[start].text)):
                    continue
            span_words: list[str] = []
            span_lines: list[str] = []
            span_boxes = []
            texts: list[str] = []
            for end in range(start, min(len(visual_lines), start + spec.max_lines)):
                line = visual_lines[end]
                span_lines.append(line.id)
                span_words.extend(line.word_ids)
                span_boxes.append(line.bbox)
                texts.append(line.text)
                bbox = _bbox_union(span_boxes)
                text = "\n".join(texts)
                normalized_text = normalize_text(text)
                if not _zone_ok(field, bbox, page.width, page.height, page_role, normalized_text):
                    continue
                word_ids = tuple(dict.fromkeys(span_words))
                line_ids = tuple(span_lines)
                candidates.append(
                    _make_candidate(
                        doc,
                        field,
                        "line_span",
                        page_index,
                        page_role,
                        line_ids,
                        word_ids,
                        text,
                        bbox,
                        f"{page_index}:{start}:{end}",
                    )
                )
    return candidates


def _same_column_lines(
    visual_lines: list[OCRLine],
    anchor_line: OCRLine,
    page: OCRPage,
    *,
    strict_top_columns: bool = False,
) -> list[tuple[int, OCRLine]]:
    ax0, ay0, ax1, _ = anchor_line.bbox
    anchor_cx = (ax0 + ax1) / 2.0
    anchor_w = max(ax1 - ax0, 1.0)
    page_w = max(page.width, 1.0)
    max_center_delta = max(anchor_w * 1.25, page_w * 0.18)
    candidates: list[tuple[int, OCRLine]] = []
    for index, line in enumerate(visual_lines):
        x0, y0, x1, _ = line.bbox
        if y0 + 1.0 < ay0:
            continue
        line_w = max(x1 - x0, 1.0)
        cx = (x0 + x1) / 2.0
        center_delta = abs(cx - anchor_cx)
        left_delta = abs(x0 - ax0)
        x_inter = max(0.0, min(ax1, x1) - max(ax0, x0))
        x_overlap = x_inter / max(min(anchor_w, line_w), 1.0)
        if strict_top_columns:
            same_column = (
                x_overlap >= 0.25
                or left_delta <= page_w * 0.10
                or center_delta <= page_w * 0.08
            )
        else:
            same_column = center_delta <= max_center_delta or left_delta <= page_w * 0.16 or x_overlap >= 0.20
        if same_column:
            candidates.append((index, line))
    return candidates


def _regime_header_lines(visual_lines: list[OCRLine], anchor_line: OCRLine, page: OCRPage) -> list[tuple[int, OCRLine]]:
    ax0, ay0, ax1, _ = anchor_line.bbox
    candidates: list[tuple[int, OCRLine]] = []
    for index, line in enumerate(visual_lines):
        x0, y0, x1, _ = line.bbox
        if y0 + 1.0 < ay0:
            continue
        if (y0 - ay0) / max(page.height, 1.0) > 0.09:
            break
        x_inter = max(0.0, min(ax1, x1) - max(ax0, x0))
        x_overlap = x_inter / max(min(ax1 - ax0, x1 - x0), 1.0)
        line_norm = normalize_text(line.text)
        if line.id == anchor_line.id or x_overlap >= 0.35 or _RE_REGIME_MOTTO.search(line_norm):
            candidates.append((index, line))
    return candidates


def _same_column_block_candidates(doc: OCRDocument, field: str, spec: FieldSpec) -> list[Candidate]:
    if field not in {
        "REGIME_HEADER",
        "ISSUE_ORG_SUPERIOR",
        "ISSUE_ORG_NAME",
        "ADDRESSEE",
        "RECIPIENTS",
        "SIGNER_ROLE",
    }:
        return []
    candidates: list[Candidate] = []
    for page_index, page_role in _candidate_pages(doc, spec):
        page = doc.pages.get(page_index)
        if page is None:
            continue
        visual_lines = sorted([line for line in page.lines if (line.text or "").strip()], key=_line_visual_order)
        for start, anchor_line in enumerate(visual_lines):
            anchor_norm = normalize_text(anchor_line.text)
            ax0, ay0, ax1, _ = anchor_line.bbox
            anchor_cx = (ax0 + ax1) / 2.0 / max(page.width, 1.0)
            anchor_top = ay0 / max(page.height, 1.0)
            if field == "REGIME_HEADER" and not (_RE_REGIME.search(anchor_norm) or _RE_REGIME_MOTTO.search(anchor_norm)):
                continue
            if field in {"ISSUE_ORG_SUPERIOR", "ISSUE_ORG_NAME"} and not (anchor_top < 0.34 and anchor_cx < 0.55):
                continue
            if field == "ADDRESSEE" and not _RE_KINH_GUI.search(anchor_norm):
                continue
            if field == "RECIPIENTS" and not _RE_NOI_NHAN.search(anchor_norm):
                continue
            if field == "SIGNER_ROLE" and not (_RE_SIGNER_PREFIX.search(anchor_norm) or _RE_SIGNER_ROLE_WORD.search(anchor_norm)):
                continue
            span_lines: list[str] = []
            span_words: list[str] = []
            span_boxes: list[tuple[float, float, float, float]] = []
            texts: list[str] = []
            prev_line = None
            if field == "REGIME_HEADER":
                continuation_lines = _regime_header_lines(visual_lines[start:], anchor_line, page)
            else:
                continuation_lines = _same_column_lines(
                    visual_lines[start:],
                    anchor_line,
                    page,
                    strict_top_columns=field in {"ISSUE_ORG_SUPERIOR", "ISSUE_ORG_NAME"},
                )
            for local_end, line in continuation_lines[: spec.max_lines]:
                if prev_line is not None:
                    gap_norm = max(0.0, line.bbox[1] - prev_line.bbox[3]) / max(page.height, 1.0)
                    line_norm = normalize_text(line.text)
                    if field in {"REGIME_HEADER", "SIGNER_ROLE"} and gap_norm > 0.12:
                        break
                    if field in {"ADDRESSEE", "RECIPIENTS"} and gap_norm > 0.10 and not line_norm.startswith(("-", "*")):
                        break
                prev_line = line
                span_lines.append(line.id)
                span_words.extend(line.word_ids)
                span_boxes.append(line.bbox)
                texts.append(line.text)
                bbox = _bbox_union(span_boxes)
                text = "\n".join(texts)
                normalized_text = normalize_text(text)
                if not _zone_ok(field, bbox, page.width, page.height, page_role, normalized_text):
                    continue
                word_ids = tuple(dict.fromkeys(span_words))
                line_ids = tuple(span_lines)
                candidates.append(
                    _make_candidate(
                        doc,
                        field,
                        "same_column_block",
                        page_index,
                        page_role,
                        line_ids,
                        word_ids,
                        text,
                        bbox,
                        f"{page_index}:{start}:{local_end}",
                    )
                )
    return candidates


def _anchored_block_candidates(doc: OCRDocument, field: str, spec: FieldSpec) -> list[Candidate]:
    if field not in {"ADDRESSEE", "RECIPIENTS"}:
        return []
    anchor_pattern = _RE_KINH_GUI if field == "ADDRESSEE" else _RE_NOI_NHAN
    candidates: list[Candidate] = []
    for page_index, page_role in _candidate_pages(doc, spec):
        page = doc.pages.get(page_index)
        if page is None:
            continue
        visual_lines = sorted([line for line in page.lines if (line.text or "").strip()], key=_line_visual_order)
        for start, anchor_line in enumerate(visual_lines):
            anchor_normalized = normalize_text(anchor_line.text)
            if not anchor_pattern.search(anchor_normalized):
                continue
            base_left = anchor_line.bbox[0] / max(page.width, 1.0)
            span_lines: list[str] = []
            span_words: list[str] = []
            span_boxes: list[tuple[float, float, float, float]] = []
            texts: list[str] = []
            prev_line = None
            for end in range(start, min(len(visual_lines), start + spec.max_lines)):
                line = visual_lines[end]
                line_normalized = normalize_text(line.text)
                line_left = line.bbox[0] / max(page.width, 1.0)
                if field == "RECIPIENTS" and line.id != anchor_line.id:
                    # Bottom "Nơi nhận" often interleaves with the signature
                    # block in visual order. Keep the anchor column and skip
                    # signer/right-column lines instead of absorbing them.
                    if base_left < 0.38 and line_left > max(0.42, base_left + 0.28):
                        continue
                    if _RE_SIGNER_CONTEXT.search(line_normalized) and line_left > base_left + 0.18:
                        continue
                if prev_line is not None:
                    gap_norm = max(0.0, line.bbox[1] - prev_line.bbox[3]) / max(page.height, 1.0)
                    left_delta = abs((line.bbox[0] / max(page.width, 1.0)) - base_left)
                    if gap_norm > 0.08 and not line_normalized.startswith(("-", "*")):
                        break
                    if left_delta > 0.20 and not line_normalized.startswith(("-", "*")):
                        break
                prev_line = line
                span_lines.append(line.id)
                span_words.extend(line.word_ids)
                span_boxes.append(line.bbox)
                texts.append(line.text)
                bbox = _bbox_union(span_boxes)
                text = "\n".join(texts)
                normalized_text = normalize_text(text)
                if not _zone_ok(field, bbox, page.width, page.height, page_role, normalized_text):
                    continue
                word_ids = tuple(dict.fromkeys(span_words))
                line_ids = tuple(span_lines)
                candidates.append(
                    _make_candidate(
                        doc,
                        field,
                        "anchor_block",
                        page_index,
                        page_role,
                        line_ids,
                        word_ids,
                        text,
                        bbox,
                        f"{page_index}:{start}:{end}",
                    )
                )
    return candidates


def _signer_name_line_ok(line: OCRLine, page: OCRPage, page_role: str) -> bool:
    if not line.word_ids:
        return False
    if not _looks_like_name_text(line.text):
        return False
    x0, y0, x1, y1 = line.bbox
    cx = (x0 + x1) / 2.0 / max(page.width, 1.0)
    bottom = y1 / max(page.height, 1.0)
    if bottom < 0.42 and page_role != "signature":
        return False
    if cx < 0.12 and x0 / max(page.width, 1.0) < 0.05:
        return False
    return True


def _word_window_candidates(doc: OCRDocument, field: str, spec: FieldSpec) -> list[Candidate]:
    candidates: list[Candidate] = []
    anchor_pattern = None
    if field == "DOC_NUMBER_SYMBOL":
        anchor_pattern = _RE_DOC_NUMBER
    elif field == "PLACE_DATE":
        anchor_pattern = _RE_PLACE_DATE
    for page_index, page_role in _candidate_pages(doc, spec):
        page = doc.pages.get(page_index)
        if page is None:
            continue
        for line in sorted([line for line in page.lines if (line.text or "").strip()], key=_line_visual_order):
            words = sorted(
                [doc.word_lookup[word_id] for word_id in line.word_ids if word_id in doc.word_lookup],
                key=lambda word: (word.order, word.bbox[0], word.bbox[1]),
            )
            if not words:
                continue
            if field == "SIGNER_NAME" and not _signer_name_line_ok(line, page, page_role):
                continue
            anchor_ranges: list[tuple[int, int]] = []
            if anchor_pattern is not None:
                normalized_words = [normalize_text(word.text) for word in words]
                anchor_indices = [index for index, normalized_word in enumerate(normalized_words) if anchor_pattern.search(normalized_word)]
                if not anchor_indices:
                    continue
                for anchor_index in anchor_indices:
                    start_min = max(0, anchor_index - min(4, spec.max_words - 1))
                    start_max = anchor_index
                    end_max = min(len(words) - 1, anchor_index + max(0, spec.max_words - 4))
                    for start in range(start_min, start_max + 1):
                        anchor_ranges.append((start, end_max))
            else:
                anchor_ranges = [(start, min(len(words) - 1, start + spec.max_words - 1)) for start in range(len(words))]

            for start, end_limit in anchor_ranges:
                span_words: list[OCRWord] = []
                for end in range(start, min(len(words), end_limit + 1, start + spec.max_words)):
                    span_words.append(words[end])
                    if field == "SIGNER_NAME" and not (2 <= len(span_words) <= 6):
                        continue
                    word_ids = tuple(word.id for word in span_words)
                    bbox = _bbox_union(word.bbox for word in span_words)
                    text = _reconstruct_words_text(span_words)
                    if field == "SIGNER_NAME" and not _looks_like_name_text(text):
                        continue
                    normalized_text = normalize_text(text)
                    if not _zone_ok(field, bbox, page.width, page.height, page_role, normalized_text):
                        continue
                    line_ids = tuple(dict.fromkeys(word.line_id for word in span_words))
                    candidates.append(
                        _make_candidate(
                            doc,
                            field,
                            "word_window",
                            page_index,
                            page_role,
                            line_ids,
                            word_ids,
                            text,
                            bbox,
                            f"{page_index}:{line.id}:{start}:{end}",
                        )
                    )
    return candidates


def _words_for_line(doc: OCRDocument, line: OCRLine) -> list[OCRWord]:
    return sorted(
        [doc.word_lookup[word_id] for word_id in line.word_ids if word_id in doc.word_lookup],
        key=lambda word: (word.bbox[0], word.order, word.bbox[1]),
    )


def _word_clusters_by_gap(words: list[OCRWord], page: OCRPage) -> list[list[OCRWord]]:
    if not words:
        return []
    clusters: list[list[OCRWord]] = [[words[0]]]
    prev = words[0]
    page_w = max(page.width, 1.0)
    for word in words[1:]:
        gap = word.bbox[0] - prev.bbox[2]
        crosses_midline = prev.bbox[2] < page_w * 0.50 <= word.bbox[0]
        if gap > page_w * 0.055 or crosses_midline:
            clusters.append([])
        clusters[-1].append(word)
        prev = word
    return [cluster for cluster in clusters if cluster]


def _semantic_top_line_clusters(words: list[OCRWord]) -> list[list[OCRWord]]:
    normalized = [normalize_text(word.text).replace("đ", "d").strip(".,;:()[]{}<>-*") for word in words]
    split_indices: list[int] = []
    for index in range(1, len(normalized) - 1):
        tail = " ".join(normalized[index : index + 3])
        if tail.startswith("dang cong san") or tail.startswith("cong hoa"):
            split_indices.append(index)
            break
    if not split_indices:
        return []
    clusters: list[list[OCRWord]] = []
    start = 0
    for index in split_indices + [len(words)]:
        if index > start:
            clusters.append(words[start:index])
        start = index
    return clusters


def _top_word_cluster_candidates(doc: OCRDocument, field: str, spec: FieldSpec) -> list[Candidate]:
    if field not in {"REGIME_HEADER", "ISSUE_ORG_SUPERIOR", "ISSUE_ORG_NAME", "PLACE_DATE"}:
        return []
    candidates: list[Candidate] = []
    for page_index, page_role in _candidate_pages(doc, spec):
        page = doc.pages.get(page_index)
        if page is None:
            continue
        top_lines = [
            line
            for line in sorted([line for line in page.lines if (line.text or "").strip()], key=_line_visual_order)
            if line.bbox[1] / max(page.height, 1.0) < 0.28
        ]
        for line in top_lines:
            words = _words_for_line(doc, line)
            if field in {"ISSUE_ORG_SUPERIOR", "ISSUE_ORG_NAME"}:
                # Word clusters are only useful when OCR merges the left header
                # with the right regime/date column. On a clean left-column line,
                # cluster splitting can create harmful partial candidates such as
                # "BAN ... KHOA" without the trailing "HOC," word.
                line_norm = normalize_text(line.text)
                line_right = line.bbox[2] / max(page.width, 1.0)
                looks_mixed_with_right_header = line_right > 0.58 or bool(_RE_REGIME.search(line_norm) or _RE_PLACE_DATE.search(line_norm))
                if not looks_mixed_with_right_header:
                    continue
            clusters = _word_clusters_by_gap(words, page) + _semantic_top_line_clusters(words)
            seen_clusters: set[tuple[str, ...]] = set()
            for cluster_index, words in enumerate(clusters):
                cluster_key = tuple(word.id for word in words)
                if cluster_key in seen_clusters:
                    continue
                seen_clusters.add(cluster_key)
                if len(words) == len(line.word_ids):
                    continue
                text = _reconstruct_words_text(words)
                normalized_text = normalize_text(text)
                regex_text = normalized_text.replace("đ", "d")
                bbox = _bbox_union(word.bbox for word in words)
                cx = (bbox[0] + bbox[2]) / 2.0 / max(page.width, 1.0)
                if field == "REGIME_HEADER" and not (cx > 0.42 and (_RE_REGIME.search(regex_text) or _RE_REGIME_MOTTO.search(regex_text))):
                    continue
                if field in {"ISSUE_ORG_SUPERIOR", "ISSUE_ORG_NAME"} and not (cx < 0.58 and not _RE_PLACE_DATE.search(regex_text)):
                    continue
                if field == "PLACE_DATE" and not _RE_PLACE_DATE.search(regex_text):
                    continue
                if not _zone_ok(field, bbox, page.width, page.height, page_role, normalized_text):
                    continue
                candidates.append(
                    _make_candidate(
                        doc,
                        field,
                        "top_word_cluster",
                        page_index,
                        page_role,
                        (line.id,),
                        tuple(word.id for word in words),
                        text,
                        bbox,
                        f"{page_index}:{line.id}:{cluster_index}",
                    )
                )
    return candidates


def _same_y_band_words(page: OCRPage, anchor: OCRWord) -> list[OCRWord]:
    ax0, ay0, ax1, ay1 = anchor.bbox
    anchor_cy = (ay0 + ay1) / 2.0
    anchor_h = max(ay1 - ay0, 1.0)
    max_dy = max(anchor_h * 0.85, 8.0)
    words = [
        word
        for word in page.words
        if abs(((word.bbox[1] + word.bbox[3]) / 2.0) - anchor_cy) <= max_dy
        and word.bbox[0] >= ax0 - 8.0
        and word.bbox[0] <= ax0 + 260.0
    ]
    return sorted(words, key=lambda word: (word.bbox[0], word.order))


def _y_band_window_candidates(doc: OCRDocument, field: str, spec: FieldSpec) -> list[Candidate]:
    if field not in {"DOC_NUMBER_SYMBOL", "PLACE_DATE"}:
        return []
    candidates: list[Candidate] = []
    anchor_pattern = _RE_DOC_NUMBER if field == "DOC_NUMBER_SYMBOL" else _RE_PLACE_DATE
    for page_index, page_role in _candidate_pages(doc, spec):
        page = doc.pages.get(page_index)
        if page is None:
            continue
        for anchor in sorted(page.words, key=lambda word: (word.bbox[1], word.bbox[0], word.order)):
            anchor_norm = normalize_text(anchor.text)
            if not anchor_pattern.search(anchor_norm):
                continue
            band_words = _same_y_band_words(page, anchor)
            if not band_words:
                continue
            try:
                anchor_pos = next(index for index, word in enumerate(band_words) if word.id == anchor.id)
            except StopIteration:
                continue
            for end in range(anchor_pos, min(len(band_words), anchor_pos + spec.max_words)):
                words = band_words[anchor_pos : end + 1]
                text = _reconstruct_words_text(words)
                normalized_text = normalize_text(text)
                if field == "DOC_NUMBER_SYMBOL" and not _RE_DOC_NUMBER.search(normalized_text):
                    continue
                if field == "PLACE_DATE" and not _RE_PLACE_DATE.search(normalized_text):
                    continue
                bbox = _bbox_union(word.bbox for word in words)
                if not _zone_ok(field, bbox, page.width, page.height, page_role, normalized_text):
                    continue
                candidates.append(
                    _make_candidate(
                        doc,
                        field,
                        "y_band_window",
                        page_index,
                        page_role,
                        tuple(dict.fromkeys(word.line_id for word in words)),
                        tuple(word.id for word in words),
                        text,
                        bbox,
                        f"{page_index}:{anchor.id}:{end}",
                    )
                )
    return candidates


def _signer_role_above_name_candidates(doc: OCRDocument, field: str, spec: FieldSpec) -> list[Candidate]:
    if field != "SIGNER_ROLE":
        return []
    candidates: list[Candidate] = []
    for page_index, page_role in _candidate_pages(doc, spec):
        page = doc.pages.get(page_index)
        if page is None:
            continue
        visual_lines = sorted([line for line in page.lines if (line.text or "").strip()], key=_line_visual_order)
        for name_index, name_line in enumerate(visual_lines):
            if not _signer_name_line_ok(name_line, page, page_role):
                continue
            nx0, ny0, nx1, _ = name_line.bbox
            name_cx = (nx0 + nx1) / 2.0
            name_w = max(nx1 - nx0, 1.0)
            max_center_delta = max(name_w * 1.8, page.width * 0.20)
            role_lines: list[OCRLine] = []
            for line in visual_lines[:name_index]:
                x0, y0, x1, y1 = line.bbox
                if y1 > ny0:
                    continue
                if y1 / max(page.height, 1.0) < 0.40 and page_role != "signature":
                    continue
                line_norm = normalize_text(line.text)
                if _RE_NOI_NHAN.search(line_norm) or _RE_KINH_GUI.search(line_norm):
                    continue
                cx = (x0 + x1) / 2.0
                x_overlap = max(0.0, min(x1, nx1) - max(x0, nx0)) / max(min(x1 - x0, nx1 - nx0), 1.0)
                center_delta = abs(cx - name_cx)
                if center_delta <= max_center_delta or x_overlap >= 0.20:
                    role_lines.append(line)
            if not role_lines:
                continue
            role_lines = role_lines[-spec.max_lines :]
            for take in range(1, len(role_lines) + 1):
                span = role_lines[-take:]
                span_words = [word_id for line in span for word_id in line.word_ids]
                span_boxes = [line.bbox for line in span]
                text = "\n".join(line.text for line in span)
                normalized_text = normalize_text(text)
                bbox = _bbox_union(span_boxes)
                if not _zone_ok(field, bbox, page.width, page.height, page_role, normalized_text):
                    continue
                candidates.append(
                    _make_candidate(
                        doc,
                        field,
                        "signer_role_above_name",
                        page_index,
                        page_role,
                        tuple(line.id for line in span),
                        tuple(dict.fromkeys(span_words)),
                        text,
                        bbox,
                        f"{page_index}:{name_line.id}:{take}",
                    )
                )
    return candidates


def generate_candidates(doc: OCRDocument, field: str) -> list[Candidate]:
    spec = FIELD_SPECS[field]
    candidates: list[Candidate] = []
    if spec.use_line_spans:
        candidates.extend(_line_span_candidates(doc, field, spec))
    candidates.extend(_same_column_block_candidates(doc, field, spec))
    candidates.extend(_top_word_cluster_candidates(doc, field, spec))
    candidates.extend(_signer_role_above_name_candidates(doc, field, spec))
    if spec.use_word_windows:
        candidates.extend(_word_window_candidates(doc, field, spec))
    candidates.extend(_y_band_window_candidates(doc, field, spec))
    dedup: dict[tuple[str, ...], Candidate] = {}
    for cand in candidates:
        key = tuple(cand.word_ids)
        existing = dedup.get(key)
        if existing is None or len(cand.word_ids) < len(existing.word_ids):
            dedup[key] = cand
    candidates = list(dedup.values())
    if FAST_CANDIDATES_ENABLED and field == "RECIPIENTS":
        # RECIPIENTS ground truth in the current corpus includes the "Noi nhan"
        # anchor. Oracle analysis showed this removes ~99% of RECIPIENTS rows
        # without losing any exact/good oracle candidate on val/test.
        candidates = [cand for cand in candidates if "noi nhan" in cand.normalized_text]
    if FAST_CANDIDATES_ENABLED and field == "ADDRESSEE":
        # When a selected page has an explicit "Kinh gui" anchor, exact/good
        # ADDRESSEE candidates also include it. For pages without that anchor
        # we keep all candidates, because some legacy templates omit it.
        pages_with_anchor = {
            cand.page_index
            for cand in candidates
            if "kinh gui" in cand.normalized_text
        }
        if pages_with_anchor:
            candidates = [
                cand
                for cand in candidates
                if cand.page_index not in pages_with_anchor or "kinh gui" in cand.normalized_text
            ]
    return candidates


def _candidate_match(candidate: Candidate, gt_instances: list[tuple[FieldInstance, set[str]]]) -> tuple[int | None, dict]:
    cand_words = candidate.word_ids_set
    best = {
        "field_id": None,
        "overlap_words": 0,
        "precision": 0.0,
        "recall": 0.0,
        "f1": 0.0,
    }
    for gt, gt_words in gt_instances:
        if not gt_words:
            continue
        overlap = len(cand_words & gt_words)
        if overlap == 0:
            continue
        precision = overlap / max(len(cand_words), 1)
        recall = overlap / max(len(gt_words), 1)
        f1 = 2.0 * precision * recall / max(precision + recall, 1e-9)
        if f1 > best["f1"]:
            best = {
                "field_id": gt.field_id,
                "overlap_words": overlap,
                "precision": precision,
                "recall": recall,
                "f1": f1,
            }
    spec = FIELD_SPECS[candidate.field]
    if best["f1"] >= spec.positive_f1 and best["recall"] >= spec.positive_recall:
        return 1, best
    if best["f1"] > spec.ignore_f1:
        return None, best
    return 0, best


def _doc_kind(relative_pdf_path: str) -> str:
    stem = Path(relative_pdf_path).stem.lower()
    if stem.startswith("digitalpdf"):
        return "digitalpdf"
    if stem.startswith("_autolabel_report"):
        return "autolabel_report"
    return "regular"


def _resolve_source_artifact_path(raw_path: str | os.PathLike[str] | None, source_project_root: Path, anchor: str) -> Path | None:
    if not raw_path:
        return None
    direct = Path(raw_path)
    if direct.exists():
        return direct
    normalized = str(raw_path).replace("\\", "/")
    marker = f"/{anchor}/"
    lower = normalized.lower()
    idx = lower.find(marker.lower())
    if idx >= 0:
        rel = normalized[idx + len(marker):]
        return source_project_root / anchor / rel
    marker = f"{anchor}/"
    idx = lower.find(marker.lower())
    if idx >= 0:
        rel = normalized[idx + len(marker):]
        return source_project_root / anchor / rel
    return direct


def build_lightgbm_manifest(source_project_root: str | Path, project_root: str | Path) -> dict:
    source_project_root = Path(source_project_root).resolve()
    project_root = Path(project_root).resolve()
    source_manifest = read_json(source_project_root / "manifest.json")
    docs = []
    for entry in source_manifest.get("documents", []):
        label_input_json = _resolve_source_artifact_path(entry.get("artifacts", {}).get("label_input_json"), source_project_root, "json_input")
        if label_input_json is None:
            continue
        if not label_input_json.exists():
            continue
        rel = label_input_json.relative_to(source_project_root / "json_input")
        label_output_json = source_project_root / "json_output_labeled" / rel
        if not label_output_json.exists():
            continue
        input_payload = read_json(label_input_json)
        source_canonical_json = _resolve_source_artifact_path(input_payload.get("source_canonical_json"), source_project_root, "ocr")
        if source_canonical_json is None or not source_canonical_json.exists():
            source_canonical_json = label_input_json
        selected_pages = input_payload.get("selected_pages") or input_payload.get("page_selection", {}).get("selected_pages") or [0]
        page_selection = input_payload.get("page_selection", {})
        docs.append(
            {
                "doc_id": entry["doc_id"],
                "relative_pdf_path": entry["relative_pdf_path"],
                "split": entry["split"],
                "source_pdf_path": entry["source_pdf_path"],
                "source_canonical_json": str(source_canonical_json),
                "label_input_json": str(label_input_json),
                "label_output_json": str(label_output_json),
                "selected_pages": selected_pages,
                "primary_page": page_selection.get("primary_page", selected_pages[0] if selected_pages else 0),
                "signature_page": page_selection.get("signature_page"),
                "doc_kind": _doc_kind(entry["relative_pdf_path"]),
                "source_project_root": str(source_project_root),
            }
        )
    return {
        "schema_version": "train_lightgbm_project_v1",
        "source_project_root": str(source_project_root),
        "project_root": str(project_root),
        "documents": docs,
    }


def _build_gt_row(doc: OCRDocument, fields: list[FieldInstance], relations: list[dict]) -> dict:
    return {
        "doc_id": doc.doc_id,
        "relative_pdf_path": doc.relative_pdf_path,
        "split": doc.split,
        "selected_pages": list(doc.selected_pages),
        "primary_page": doc.primary_page,
        "signature_page": doc.signature_page,
        "doc_kind": doc.doc_kind,
        "field_instances": [
            {
                "field_id": field.field_id,
                "label": field.label,
                "page_index": field.page_index,
                "line_ids": list(field.line_ids),
                "word_ids": list(field.word_ids),
                "text": field.text,
                "bbox": list(field.bbox),
            }
            for field in fields
        ],
        "relations": relations,
    }


def _build_row(doc: OCRDocument, cand: Candidate, target: int, match: dict) -> dict:
    page = doc.pages[cand.page_index]
    features = _build_features(
        cand.field,
        cand.source_kind,
        page,
        cand.page_role,
        cand.line_ids,
        cand.word_ids,
        cand.bbox,
        cand.text,
        cand.normalized_text,
        doc,
    )
    return {
        "doc_id": doc.doc_id,
        "relative_pdf_path": doc.relative_pdf_path,
        "split": doc.split,
        "doc_kind": doc.doc_kind,
        "field": cand.field,
        "candidate_id": cand.candidate_id,
        "page_index": cand.page_index,
        "page_role": cand.page_role,
        "line_ids": list(cand.line_ids),
        "word_ids": list(cand.word_ids),
        "bbox": list(cand.bbox),
        "text": cand.text,
        "target": target,
        "relevance": float(match.get("f1", 0.0)),
        "match": match,
        "features": features,
    }


def _process_document_for_export(doc_meta: dict) -> dict:
    doc = load_ocr_document(doc_meta)
    fields, relations = load_field_instances(doc, doc_meta["label_output_json"])
    gt_row = _build_gt_row(doc, fields, relations)
    field_to_gt = {
        label: [(field, set(field.word_ids)) for field in fields if field.label == label]
        for label in LABELS
    }
    field_rows: dict[str, list[dict]] = {}
    candidate_counts = {label: 0 for label in LABELS}
    positive_counts = {label: 0 for label in LABELS}
    ignored_counts = {label: 0 for label in LABELS}

    for field in LABELS:
        labeled_candidates: list[tuple[Candidate, int, dict]] = []
        for cand in generate_candidates(doc, field):
            target, match = _candidate_match(cand, field_to_gt[field])
            if target is None and doc.split == "train" and match.get("f1", 0.0) < 0.35:
                ignored_counts[field] += 1
                continue
            if target is None:
                ignored_counts[field] += 1
                target = 0
            labeled_candidates.append((cand, int(target), match))

        if doc.split == "train":
            positives = [entry for entry in labeled_candidates if entry[1] == 1]
            partials = [entry for entry in labeled_candidates if entry[1] == 0 and entry[2].get("f1", 0.0) >= 0.20]
            negatives = [entry for entry in labeled_candidates if entry[1] == 0 and entry[2].get("f1", 0.0) < 0.20]
            keep_neg = max(24, min(len(negatives), max(64, len(positives) * 20)))
            if len(negatives) > keep_neg:
                rng = seeded_rng(doc.doc_id, field, "neg")
                negatives = rng.sample(negatives, keep_neg)
            keep_partial = max(16, min(len(partials), max(64, len(positives) * 8)))
            if len(partials) > keep_partial:
                rng = seeded_rng(doc.doc_id, field, "partial")
                partials = rng.sample(partials, keep_partial)
            labeled_candidates = positives + partials + negatives

        rows = [_build_row(doc, cand, target, match) for cand, target, match in labeled_candidates]
        field_rows[field] = rows
        candidate_counts[field] = len(rows)
        positive_counts[field] = sum(1 for _, target, _ in labeled_candidates if target == 1)

    return {
        "doc_id": doc.doc_id,
        "split": doc.split,
        "doc_kind": doc.doc_kind,
        "gt_row": gt_row,
        "field_rows": field_rows,
        "candidate_counts": candidate_counts,
        "positive_counts": positive_counts,
        "ignored_counts": ignored_counts,
    }


def export_fieldwise_dataset(
    project_root: str | Path,
    include_autolabel_reports: bool = False,
    max_workers: int | None = None,
) -> dict:
    from .common import build_paths, ensure_project_layout

    paths = build_paths(project_root)
    ensure_project_layout(paths)
    manifest = read_json(paths.manifest)
    export_root = paths.exports_root / "fieldwise"
    gt_root = paths.exports_root / "ground_truth"
    gt_root.mkdir(parents=True, exist_ok=True)
    for field in LABELS:
        (export_root / field).mkdir(parents=True, exist_ok=True)
    report = {
        "doc_count": 0,
        "docs_by_kind": {},
        "candidate_rows": {label: {"train": 0, "val": 0, "test": 0} for label in LABELS},
        "positive_rows": {label: {"train": 0, "val": 0, "test": 0} for label in LABELS},
        "ignored_rows": {label: 0 for label in LABELS},
    }
    doc_metas = [
        doc_meta
        for doc_meta in manifest["documents"]
        if include_autolabel_reports or doc_meta["doc_kind"] != "autolabel_report"
    ]
    if max_workers is None:
        max_workers = max(1, min(8, (os.cpu_count() or 2) - 1))
    max_workers = max(1, int(max_workers))

    with ExitStack() as stack:
        gt_handles = {
            split: stack.enter_context((gt_root / f"{split}.jsonl").open("w", encoding="utf-8"))
            for split in ("train", "val", "test")
        }
        field_handles = {
            (field, split): stack.enter_context((export_root / field / f"{split}.jsonl").open("w", encoding="utf-8"))
            for field in LABELS
            for split in ("train", "val", "test")
        }

        if max_workers == 1:
            results = map(_process_document_for_export, doc_metas)
        else:
            executor = stack.enter_context(ProcessPoolExecutor(max_workers=max_workers))
            chunk_size = max(1, len(doc_metas) // max(1, max_workers * 4))
            results = executor.map(_process_document_for_export, doc_metas, chunksize=chunk_size)

        total_docs = len(doc_metas)
        for index, result in enumerate(results, start=1):
            report["doc_count"] += 1
            report["docs_by_kind"][result["doc_kind"]] = report["docs_by_kind"].get(result["doc_kind"], 0) + 1
            split = result["split"]
            gt_handles[split].write(json.dumps(result["gt_row"], ensure_ascii=False) + "\n")
            for field in LABELS:
                for row in result["field_rows"][field]:
                    field_handles[(field, split)].write(json.dumps(row, ensure_ascii=False) + "\n")
                report["candidate_rows"][field][split] += result["candidate_counts"][field]
                report["positive_rows"][field][split] += result["positive_counts"][field]
                report["ignored_rows"][field] += result["ignored_counts"][field]
            if index % 25 == 0 or index == total_docs:
                print(f"[train_lightgbm] built {index}/{total_docs} docs (workers={max_workers})", flush=True)

    write_json(paths.reports_root / "dataset_build_report.json", report)
    return report
