from __future__ import annotations

import re
import unicodedata
from copy import deepcopy
from typing import Any, Iterable


_RE_WS = re.compile(r"\s+")
_RE_DOC_NUMBER = re.compile(r"\bso\s*[:.]?\s*\d", re.IGNORECASE)
_RE_DOC_NUMBER_SYMBOL_VALUE = re.compile(r"\b\d{1,5}\s*(?:[/.-]\s*[a-z]+[a-z0-9]*)+", re.IGNORECASE)
_RE_DOC_NUMBER_PREFIX = re.compile(r"^\s*so\b\s*[:.]?\s*(?P<tail>.+)$", re.IGNORECASE)
_RE_STANDALONE_DOC_NUMBER_SYMBOL = re.compile(
    r"^\s*(?:s[o06]?\s*)?\d{1,6}\s*(?:[/.-]\s*|\s+)[a-z][a-z0-9/\-]*(?:\s|$)",
    re.IGNORECASE,
)
_RE_STANDALONE_DOC_NUMBER_ONLY = re.compile(r"^\s*(?:s[o06]?\s*)?\d{1,6}\s*$", re.IGNORECASE)
_RE_DOC_NUMBER_PREFIX_ONLY = re.compile(r"^\s*s[o06]?\s*[:.]?\s*$", re.IGNORECASE)
_RE_NUMBER_ONLY = re.compile(r"^\s*\d{1,6}\s*$", re.IGNORECASE)
_RE_DOC_SYMBOL_CONTINUATION = re.compile(
    r"^\s*-?\s*(?:\d{1,6}\s*[-/]\s*)?[a-zđ]{1,12}[a-zđ0-9]*"
    r"(?:\s*[-/]\s*[a-zđ0-9]{1,20})+\s*$",
    re.IGNORECASE,
)
_RE_DOC_NUMBER_ABBREV = re.compile(
    r"\b(?:cv|qd|tb|kh|bc|pc|tu|vptu|ubnd|hdnd|dubtc|tccb)\b",
    re.IGNORECASE,
)
_RE_STAMP_MARK_LINE = re.compile(
    r"^(?:khan|thuong\s+khan|hoa\s+toc|mat|toi\s+mat|tuyet\s+mat)$",
    re.IGNORECASE,
)
_RE_DATE = re.compile(r"\b(?:ngay\s*\d{1,2}\s*thang\s*\d{1,2}|tp\.?\s*ho\s+chi\s+minh|ha\s+noi)", re.IGNORECASE)
_RE_PLACE_DATE_STRICT = re.compile(
    r"(?:\b(?:ha\s+noi|tp\.?\s*ho\s+chi\s+minh|ho\s+chi\s+minh)\b.*\bngay\b)"
    r"|(?:\bngay\b.*(?:thang|nam|\d{1,2}[/-]\d{1,2}))",
    re.IGNORECASE,
)
_RE_REGIME = re.compile(r"\b(?:dang\s+cong\s+san|cong\s+hoa|doc\s+lap|tu\s+do|hanh\s+phuc)\b", re.IGNORECASE)
_RE_LINE_NUM_ID = re.compile(r"(?:^|[_\-.])(?:l|line)[_\-.]?(\d+)(?=$|[_\-.])", re.IGNORECASE)
_RE_TITLE_NOISE = re.compile(
    r"\b(?:ke\s+hoach|bao\s+cao|ket\s+luan|quy\s+che|quyet\s+dinh|to\s+trinh|cong\s+van|chuong\s+trinh|thong\s+bao|giay\s+moi|phuong\s+an)\b",
    re.IGNORECASE,
)
_RE_STANDALONE_DOC_TITLE = re.compile(
    r"^(?:ke\s+hoach|bao\s+cao|ket\s+luan|quy\s+che|quyet\s+dinh|to\s+trinh|cong\s+van|chuong\s+trinh|thong\s+bao|giay\s+moi|phuong\s+an)$",
    re.IGNORECASE,
)
_RE_SIGNER_ROLE_MARKER = re.compile(r"\b(?:tm\.?|t/m|kt\.?|k/t)\b", re.IGNORECASE)
_RE_ORG_NAME_START = re.compile(
    r"\b(?:tieu\s+ban|van\s+phong|chi\s+bo|bch|so\s+(?:noi|tu|giao|y|xay|tai|ke|lao|cong|van|nong|du|thong|khoa)|ban\s+(?!chi\s+dao|thuong\s+vu)|dang\s+uy|hoi\s+dong|uy\s+ban)\b",
    re.IGNORECASE,
)
_RE_SUPERIOR_HINT = re.compile(
    r"\b(?:ban\s+chi\s+dao|phat\s+trien|khoa\s+hoc|cong\s+nghe|doi\s+moi|sang\s+tao|chuyen\s+doi|thanh\s+pho|tphcm)\b",
    re.IGNORECASE,
)
_INCOMPLETE_HEADER_TAILS = {"va", "cua", "ve", "khoa", "khoa hoc", "doi moi", "sang tao"}
_ISSUE_ORG_FIELDS = {"ISSUE_ORG_SUPERIOR", "ISSUE_ORG_NAME"}
_MULTI_INSTANCE_FIELDS = {"SIGNER_ROLE", "SIGNER_NAME"}
_SINGLE_LINE_FIELDS = {"DOC_NUMBER_SYMBOL", "PLACE_DATE"}
_PRIMARY_METADATA_PAGE = 0
_NON_PRIMARY_PAGE_ALLOWED_LABELS = {"RECIPIENTS", "SIGNER_ROLE", "SIGNER_NAME"}
_ANCHOR_TERMS = {
    "REGIME_HEADER": ("dang cong san", "cong hoa", "doc lap", "tu do", "hanh phuc"),
    "DOC_NUMBER_SYMBOL": ("so ", "so:", "so."),
    "PLACE_DATE": ("ngay", "thang", "nam", "ha noi", "tp. ho chi minh", "ho chi minh"),
    "DOC_SUBJECT": (
        "ve ",
        "ke hoach",
        "thong bao",
        "nghi dinh",
        "quyet dinh",
        "to trinh",
        "bao cao",
        "cong van",
        "giay moi",
        "chuong trinh",
    ),
    "ADDRESSEE": ("kinh gui",),
    "RECIPIENTS": ("noi nhan",),
    "SIGNER_ROLE": ("t/m", "tm.", "kt.", "chu tich", "bi thu", "chanh van phong", "truong ban"),
}
_MAX_SUPERIOR_LINES = 3
_MAX_ORG_NAME_LINES = 3


def normalize_vn_text(text: str) -> str:
    text = (text or "").replace("\u0110", "D").replace("\u0111", "d")
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _RE_WS.sub(" ", stripped.lower()).strip()


def _looks_like_doc_number_norm(norm: str) -> bool:
    norm = str(norm or "").strip()
    if not norm:
        return False
    if _RE_DOC_NUMBER.search(norm) or _RE_DOC_NUMBER_SYMBOL_VALUE.search(norm):
        return True
    match = _RE_DOC_NUMBER_PREFIX.match(norm)
    if not match:
        return False
    tail = (match.group("tail") or "").strip()
    if not tail or len(tail.split()) > 6:
        return False
    if any(sep in tail for sep in ("/", "-", ".")) and re.search(r"[a-z0-9]", tail, re.IGNORECASE):
        return True
    return bool(_RE_DOC_NUMBER_ABBREV.search(tail))


def _looks_like_standalone_doc_number_norm(norm: str) -> bool:
    norm = str(norm or "").strip()
    if not norm:
        return False
    if len(norm.split()) > 5:
        return False
    if _RE_DOC_NUMBER_PREFIX.match(norm):
        return _looks_like_doc_number_norm(norm)
    if _RE_STANDALONE_DOC_NUMBER_ONLY.match(norm):
        return True
    return bool(_RE_STANDALONE_DOC_NUMBER_SYMBOL.match(norm))


def _looks_like_doc_symbol_continuation_norm(norm: str) -> bool:
    norm = str(norm or "").strip()
    if not norm or len(norm.split()) > 3:
        return False
    if norm in {"v/v", "vv"}:
        return False
    return bool(_RE_DOC_SYMBOL_CONTINUATION.fullmatch(norm))


def _word_id(word: dict[str, Any]) -> str | None:
    value = word.get("id") or word.get("word_id")
    return str(value) if value is not None else None


def _word_bbox(word: dict[str, Any]) -> list[float]:
    bbox = word.get("bbox")
    if bbox and len(bbox) >= 4:
        return [float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])]
    if all(key in word for key in ("x", "y", "w", "h")):
        x = float(word.get("x") or 0.0)
        y = float(word.get("y") or 0.0)
        return [x, y, x + float(word.get("w") or 0.0), y + float(word.get("h") or 0.0)]
    return [0.0, 0.0, 0.0, 0.0]


def _bbox_union(boxes: Iterable[list[float]]) -> list[float]:
    clean = [box for box in boxes if box and len(box) >= 4]
    if not clean:
        return [0.0, 0.0, 0.0, 0.0]
    return [
        min(float(box[0]) for box in clean),
        min(float(box[1]) for box in clean),
        max(float(box[2]) for box in clean),
        max(float(box[3]) for box in clean),
    ]


def _page_word_maps(page: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    words: list[dict[str, Any]] = []
    for order, raw in enumerate(page.get("words") or []):
        word = dict(raw)
        word.setdefault("order", order)
        words.append(word)
    if not words:
        for line_order, line in enumerate(page.get("lines") or []):
            line_id = line.get("id") or line.get("line_id")
            for order, raw in enumerate(line.get("words") or []):
                word = dict(raw)
                word.setdefault("line_id", line_id)
                word.setdefault("order", order)
                word.setdefault("_line_order", line_order)
                words.append(word)
    by_id = {word_id: word for word in words for word_id in [_word_id(word)] if word_id is not None}
    return words, by_id


def _left_to_right_line_key(word: dict[str, Any]) -> tuple[float, int]:
    try:
        order = int(word.get("order", 0) or 0)
    except (TypeError, ValueError):
        order = 0
    return (_word_bbox(word)[0], order)


def _line_words(page: dict[str, Any], line: dict[str, Any], words_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    word_ids = [str(item) for item in (line.get("word_ids") or [])]
    words = [words_by_id[word_id] for word_id in word_ids if word_id in words_by_id]
    if words:
        return sorted(words, key=_left_to_right_line_key)
    line_id = str(line.get("id") or line.get("line_id") or "")
    all_words, _ = _page_word_maps(page)
    return sorted(
        [word for word in all_words if str(word.get("line_id") or "") == line_id],
        key=_left_to_right_line_key,
    )


def _ordered_page_lines(page: dict[str, Any], right_bound_x: float | None = None) -> list[dict[str, Any]]:
    page_w = max(float(page.get("width") or page.get("page_width") or page.get("render_width") or 1.0), 1.0)
    page_h = max(float(page.get("height") or page.get("page_height") or page.get("render_height") or 1.0), 1.0)
    _words, words_by_id = _page_word_maps(page)
    out: list[dict[str, Any]] = []
    for fallback_order, line in enumerate(page.get("lines") or []):
        line_id = str(line.get("id") or line.get("line_id") or f"line_{fallback_order}")
        selected_words = []
        for word in _line_words(page, line, words_by_id):
            bbox = _word_bbox(word)
            if right_bound_x is not None and float(bbox[0]) >= right_bound_x:
                continue
            selected_words.append(word)
        if not selected_words:
            continue
        boxes = [_word_bbox(word) for word in selected_words]
        bbox = _bbox_union(boxes)
        text = " ".join((str(word.get("text") or word.get("ocr_text") or "")).strip() for word in selected_words).strip()
        norm = normalize_vn_text(text)
        if not text:
            continue
        out.append(
            {
                "line_id": line_id,
                "words": selected_words,
                "word_ids": [_word_id(word) for word in selected_words if _word_id(word) is not None],
                "text": text,
                "norm": norm,
                "bbox": bbox,
                "top": float(bbox[1]) / page_h,
                "bottom": float(bbox[3]) / page_h,
                "cx": ((float(bbox[0]) + float(bbox[2])) / 2.0) / page_w,
                "x0": float(bbox[0]) / page_w,
                "x1": float(bbox[2]) / page_w,
                "order": int(line.get("order", fallback_order) or fallback_order),
            }
        )
    return sorted(out, key=lambda item: (item["bbox"][1], item["bbox"][0], item["order"]))


def _is_header_stop_norm(norm: str) -> bool:
    return bool(_looks_like_doc_number_norm(norm) or _RE_DATE.search(norm) or _RE_REGIME.search(norm) or _RE_TITLE_NOISE.search(norm))


def _instances_by_label(annotation: dict[str, Any], label: str, page_index: int | None = None) -> list[dict[str, Any]]:
    out = []
    for inst in annotation.get("field_instances") or []:
        if inst.get("label") != label:
            continue
        if page_index is not None and int(inst.get("page_index") or 0) != int(page_index):
            continue
        out.append(inst)
    return out


def _first_word_x0_for_instance(inst: dict[str, Any], words_by_id: dict[str, dict[str, Any]]) -> float | None:
    for word_id in inst.get("word_ids") or []:
        word = words_by_id.get(str(word_id))
        if word is not None:
            return float(_word_bbox(word)[0])
    bbox = inst.get("bbox")
    if bbox and len(bbox) >= 4:
        return float(bbox[0])
    return None


def _right_header_bound_x(page: dict[str, Any], annotation: dict[str, Any], page_index: int) -> float | None:
    _words, words_by_id = _page_word_maps(page)
    bounds: list[float] = []
    for label in ("REGIME_HEADER", "PLACE_DATE"):
        for inst in _instances_by_label(annotation, label, page_index):
            x0 = _first_word_x0_for_instance(inst, words_by_id)
            if x0 is not None:
                bounds.append(x0)
    if bounds:
        return min(bounds)

    for line in _ordered_page_lines(page):
        if _RE_REGIME.search(line["norm"]) or _RE_DATE.search(line["norm"]):
            if line["words"]:
                bounds.append(float(_word_bbox(line["words"][0])[0]))
    return min(bounds) if bounds else None


def _header_candidate_lines(page: dict[str, Any], right_bound_x: float) -> list[dict[str, Any]]:
    lines = _ordered_page_lines(page, right_bound_x=right_bound_x)
    stop_top = None
    for line in lines:
        if line["top"] > 0.34:
            continue
        if _is_header_stop_norm(line["norm"]):
            stop_top = line["top"]
            break

    out: list[dict[str, Any]] = []
    for line in lines:
        norm = line["norm"]
        if not norm or norm in {"*", "-", "--", "***"}:
            continue
        if stop_top is not None and line["top"] >= stop_top - 0.002:
            continue
        if line["top"] > 0.22 or line["cx"] > 0.62:
            continue
        if _is_header_stop_norm(norm):
            continue
        out.append(line)
    return out


def _has_large_header_gap(prev_line: dict[str, Any], next_line: dict[str, Any]) -> bool:
    return float(next_line["bbox"][1]) - float(prev_line["bbox"][3]) > 72.0


def _issue_org_header_candidate(
    page: dict[str, Any],
    annotation: dict[str, Any],
    page_index: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]] | None:
    right_bound_x = _right_header_bound_x(page, annotation, page_index)
    if right_bound_x is None:
        return None
    lines = _header_candidate_lines(page, right_bound_x)
    if len(lines) < 2:
        return None
    org_start_idx = None
    for idx, line in enumerate(lines[1:], start=1):
        if _RE_ORG_NAME_START.search(line["norm"]):
            org_start_idx = idx
            break
    if org_start_idx is None:
        return None

    superior_lines = lines[:org_start_idx]
    if not (1 <= len(superior_lines) <= _MAX_SUPERIOR_LINES):
        return None

    org_lines: list[dict[str, Any]] = []
    previous = None
    for line in lines[org_start_idx:]:
        if len(org_lines) >= _MAX_ORG_NAME_LINES:
            break
        if _is_header_stop_norm(line["norm"]):
            break
        if previous is not None and _has_large_header_gap(previous, line):
            break
        org_lines.append(line)
        previous = line
    if not (1 <= len(org_lines) <= _MAX_ORG_NAME_LINES):
        return None
    return superior_lines, org_lines, {"right_bound_x": right_bound_x}


def _line_ids_for_instance(inst: dict[str, Any], words_by_id: dict[str, dict[str, Any]]) -> set[str]:
    out = {str(line_id) for line_id in (inst.get("line_ids") or []) if line_id is not None}
    if out:
        return out
    for word_id in inst.get("word_ids") or []:
        word = words_by_id.get(str(word_id))
        if word is not None and word.get("line_id") is not None:
            out.add(str(word.get("line_id")))
    return out


def _word_ids_for_lines(lines: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        for word_id in line["word_ids"]:
            if word_id not in seen:
                seen.add(word_id)
                out.append(word_id)
    return out


def _line_order_map(page: dict[str, Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for order, line in enumerate(page.get("lines") or []):
        line_id = line.get("id") or line.get("line_id")
        if line_id is not None:
            try:
                out[str(line_id)] = int(line.get("order", order) or order)
            except (TypeError, ValueError):
                out[str(line_id)] = order
    return out


def _line_number_from_id(line_id: object) -> int | None:
    if line_id is None:
        return None
    match = _RE_LINE_NUM_ID.search(str(line_id))
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def _word_reading_key(
    word: dict[str, Any],
    page: dict[str, Any],
    line_order: dict[str, int],
    fallback_order: int,
) -> tuple[int, int, float, int, int]:
    bbox = _word_bbox(word)
    line_id = word.get("line_id")
    if line_id is not None and str(line_id) in line_order:
        line_key = (0, line_order[str(line_id)])
    else:
        parsed = _line_number_from_id(line_id)
        if parsed is not None:
            line_key = (1, parsed)
        else:
            cy = (float(bbox[1]) + float(bbox[3])) / 2.0
            line_key = (2, round(cy / 10.0))
    try:
        word_order = int(word.get("order", fallback_order) or fallback_order)
    except (TypeError, ValueError):
        word_order = fallback_order
    return (line_key[0], line_key[1], float(bbox[0]), word_order, fallback_order)


def _ordered_word_ids_for_page(
    word_ids: Iterable[str],
    page: dict[str, Any],
    words_by_id: dict[str, dict[str, Any]],
) -> list[str]:
    line_order = _line_order_map(page)
    seen: set[str] = set()
    indexed: list[tuple[int, str, dict[str, Any]]] = []
    for fallback_order, raw_word_id in enumerate(word_ids or []):
        word_id = str(raw_word_id)
        if word_id in seen:
            continue
        word = words_by_id.get(word_id)
        if word is None:
            continue
        seen.add(word_id)
        indexed.append((fallback_order, word_id, word))
    indexed.sort(key=lambda item: _word_reading_key(item[2], page, line_order, item[0]))
    return [word_id for _fallback_order, word_id, _word in indexed]


def _normalize_field_instances_reading_order(canonical: dict[str, Any], annotation: dict[str, Any]) -> dict[str, Any]:
    pages = canonical.get("pages") or []
    if not pages or not annotation.get("field_instances"):
        return annotation
    pages_by_index = {int(page.get("page_index", idx)): page for idx, page in enumerate(pages)}
    out = deepcopy(annotation)
    for inst in out.get("field_instances") or []:
        page_index = _instance_page_index(inst)
        page = pages_by_index.get(page_index)
        if page is None:
            continue
        _words, words_by_id = _page_word_maps(page)
        word_ids = _ordered_word_ids_for_page(inst.get("word_ids") or [], page, words_by_id)
        if not word_ids:
            continue
        boxes = [_word_bbox(words_by_id[word_id]) for word_id in word_ids if word_id in words_by_id]
        inst["word_ids"] = word_ids
        inst["line_ids"] = _line_ids_for_word_ids(word_ids, words_by_id, _line_order_map(page))
        inst["bbox"] = _bbox_union(boxes)
        inst["text"] = _text_for_word_ids(word_ids, words_by_id)
    return out


def _instance_page_index(inst: dict[str, Any]) -> int:
    try:
        return int(inst.get("page_index") or 0)
    except (TypeError, ValueError):
        return 0


def _instance_bbox(inst: dict[str, Any]) -> list[float]:
    bbox = inst.get("bbox")
    if bbox and len(bbox) >= 4:
        return [float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])]
    return [0.0, 0.0, 0.0, 0.0]


def _bbox_width(bbox: list[float]) -> float:
    return max(0.0, float(bbox[2]) - float(bbox[0]))


def _bbox_height(bbox: list[float]) -> float:
    return max(0.0, float(bbox[3]) - float(bbox[1]))


def _x_overlap_ratio(a: list[float], b: list[float]) -> float:
    overlap = max(0.0, min(float(a[2]), float(b[2])) - max(float(a[0]), float(b[0])))
    denom = max(1.0, min(_bbox_width(a), _bbox_width(b)))
    return overlap / denom


def _y_overlap_ratio(a: list[float], b: list[float]) -> float:
    overlap = max(0.0, min(float(a[3]), float(b[3])) - max(float(a[1]), float(b[1])))
    denom = max(1.0, min(_bbox_height(a), _bbox_height(b)))
    return overlap / denom


def _median_word_height(words: list[dict[str, Any]]) -> float:
    heights = sorted(_bbox_height(_word_bbox(word)) for word in words if _bbox_height(_word_bbox(word)) > 0.0)
    if not heights:
        return 12.0
    mid = len(heights) // 2
    if len(heights) % 2:
        return heights[mid]
    return (heights[mid - 1] + heights[mid]) / 2.0


def _ordered_instance_word_ids(
    instances: Iterable[dict[str, Any]],
    page: dict[str, Any],
    words_by_id: dict[str, dict[str, Any]],
) -> list[str]:
    seen: set[str] = set()
    word_ids: list[str] = []
    ordered_instances = sorted(instances, key=lambda inst: (_instance_page_index(inst), _instance_bbox(inst)[1], _instance_bbox(inst)[0]))
    for inst in ordered_instances:
        for word_id in inst.get("word_ids") or []:
            word_id = str(word_id)
            if word_id in seen:
                continue
            seen.add(word_id)
            word_ids.append(word_id)
    return _ordered_word_ids_for_page(word_ids, page, words_by_id)


def _line_ids_for_word_ids(word_ids: list[str], words_by_id: dict[str, dict[str, Any]], line_order: dict[str, int]) -> list[str]:
    seen: set[str] = set()
    line_ids: list[str] = []
    for word_id in word_ids:
        word = words_by_id.get(str(word_id))
        if word is None:
            continue
        line_id = word.get("line_id")
        if line_id is None:
            continue
        line_id = str(line_id)
        if line_id not in seen:
            seen.add(line_id)
            line_ids.append(line_id)
    return sorted(line_ids, key=lambda lid: line_order.get(lid, 10**9))


def _text_for_word_ids(word_ids: list[str], words_by_id: dict[str, dict[str, Any]]) -> str:
    parts: list[str] = []
    last_line_id = None
    for word_id in word_ids:
        word = words_by_id.get(str(word_id))
        if word is None:
            continue
        text = str(word.get("text") or word.get("ocr_text") or "").strip()
        if not text:
            continue
        line_id = word.get("line_id")
        if parts and line_id is not None and last_line_id is not None and str(line_id) != str(last_line_id):
            parts.append("\n")
        elif parts and parts[-1] != "\n":
            parts.append(" ")
        parts.append(text)
        last_line_id = line_id
    return "".join(parts).strip()


def _text_for_instance_group(group: list[dict[str, Any]]) -> str:
    ordered = sorted(group, key=lambda inst: (_instance_page_index(inst), _instance_bbox(inst)[1], _instance_bbox(inst)[0]))
    parts: list[str] = []
    previous_bbox: list[float] | None = None
    for inst in ordered:
        text = str(inst.get("text") or "").strip()
        if not text:
            continue
        if parts:
            bbox = _instance_bbox(inst)
            if previous_bbox is not None and _y_overlap_ratio(previous_bbox, bbox) >= 0.45:
                parts.append(" ")
            else:
                parts.append("\n")
        parts.append(text)
        previous_bbox = _instance_bbox(inst)
    return "".join(parts).strip()


def _merge_instance_group(
    label: str,
    group: list[dict[str, Any]],
    page: dict[str, Any],
    words_by_id: dict[str, dict[str, Any]],
    sequence: int,
) -> dict[str, Any]:
    line_order = _line_order_map(page)
    word_ids = _ordered_instance_word_ids(group, page, words_by_id)
    boxes = [_word_bbox(words_by_id[word_id]) for word_id in word_ids if word_id in words_by_id]
    if not boxes:
        boxes = [_instance_bbox(inst) for inst in group]
    field_id = group[0].get("field_id") or f"schema:{label}:{sequence}"
    if len(group) > 1:
        field_id = str(field_id)
    merged = {
        "field_id": field_id,
        "label": label,
        "page_index": _instance_page_index(group[0]),
        "line_ids": _line_ids_for_word_ids(word_ids, words_by_id, line_order),
        "word_ids": word_ids,
        "bbox": _bbox_union(boxes),
        "text": _text_for_instance_group(group) or _text_for_word_ids(word_ids, words_by_id),
    }
    conf = _confidence(group)
    if conf is not None:
        merged["confidence"] = conf
    if len(group) > 1:
        merged["schema_merged"] = True
        merged["merged_field_ids"] = [inst.get("field_id") for inst in group if inst.get("field_id") is not None]
    return merged


def _instances_are_close(
    label: str,
    left: dict[str, Any],
    right: dict[str, Any],
    page: dict[str, Any],
    words_by_id: dict[str, dict[str, Any]],
) -> bool:
    if _instance_page_index(left) != _instance_page_index(right):
        return False
    left_words = {str(word_id) for word_id in left.get("word_ids") or []}
    right_words = {str(word_id) for word_id in right.get("word_ids") or []}
    if left_words & right_words:
        return True

    a = _instance_bbox(left)
    b = _instance_bbox(right)
    if a[2] <= a[0] or a[3] <= a[1] or b[2] <= b[0] or b[3] <= b[1]:
        return False

    page_w = max(float(page.get("width") or page.get("page_width") or page.get("render_width") or 1.0), 1.0)
    page_words, _ = _page_word_maps(page)
    line_h = max(8.0, _median_word_height(page_words))
    vertical_gap = max(0.0, max(float(a[1]), float(b[1])) - min(float(a[3]), float(b[3])))
    horizontal_gap = max(0.0, max(float(a[0]), float(b[0])) - min(float(a[2]), float(b[2])))
    same_line = _y_overlap_ratio(a, b) >= 0.45
    if same_line and horizontal_gap <= max(42.0, page_w * 0.08):
        return True

    x_overlap = _x_overlap_ratio(a, b)
    center_gap = abs((float(a[0]) + float(a[2]) - float(b[0]) - float(b[2])) / 2.0)
    column_compatible = x_overlap >= 0.05 or center_gap <= page_w * 0.34
    if not column_compatible:
        return False

    max_gap_factor = 2.8
    if label in {"DOC_SUBJECT", "RECIPIENTS", "ADDRESSEE"}:
        max_gap_factor = 4.2
    elif label in {"REGIME_HEADER", "SIGNER_ROLE", "SIGNER_NAME"}:
        max_gap_factor = 3.2
    return vertical_gap <= max(24.0, line_h * max_gap_factor)


def _merge_close_instances_for_label(
    label: str,
    instances: list[dict[str, Any]],
    pages_by_index: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    if len(instances) <= 1:
        return list(instances)
    ordered = sorted(instances, key=lambda inst: (_instance_page_index(inst), _instance_bbox(inst)[1], _instance_bbox(inst)[0]))
    groups: list[list[dict[str, Any]]] = []
    for inst in ordered:
        page = pages_by_index.get(_instance_page_index(inst))
        if page is None or not groups:
            groups.append([inst])
            continue
        _words, words_by_id = _page_word_maps(page)
        if any(_instances_are_close(label, member, inst, page, words_by_id) for member in groups[-1]):
            groups[-1].append(inst)
        else:
            groups.append([inst])

    merged: list[dict[str, Any]] = []
    for sequence, group in enumerate(groups, start=1):
        page = pages_by_index.get(_instance_page_index(group[0]))
        if page is None:
            merged.extend(group)
            continue
        _words, words_by_id = _page_word_maps(page)
        merged.append(_merge_instance_group(label, group, page, words_by_id, sequence))
    return merged


def _signer_fragments_are_close(
    label: str,
    left: dict[str, Any],
    right: dict[str, Any],
    page: dict[str, Any],
    words_by_id: dict[str, dict[str, Any]],
) -> bool:
    if label not in _MULTI_INSTANCE_FIELDS:
        return False
    if _instance_page_index(left) != _instance_page_index(right):
        return False
    left_words = {str(word_id) for word_id in left.get("word_ids") or []}
    right_words = {str(word_id) for word_id in right.get("word_ids") or []}
    if left_words & right_words:
        return True

    a = _instance_bbox(left)
    b = _instance_bbox(right)
    if a[2] <= a[0] or a[3] <= a[1] or b[2] <= b[0] or b[3] <= b[1]:
        return False

    page_w = max(float(page.get("width") or page.get("page_width") or page.get("render_width") or 1.0), 1.0)
    page_words, _ = _page_word_maps(page)
    line_h = max(8.0, _median_word_height(page_words))
    horizontal_gap = max(0.0, max(float(a[0]), float(b[0])) - min(float(a[2]), float(b[2])))
    vertical_gap = max(0.0, max(float(a[1]), float(b[1])) - min(float(a[3]), float(b[3])))

    same_line = _y_overlap_ratio(a, b) >= 0.50
    if same_line:
        return horizontal_gap <= max(28.0, page_w * 0.045)

    x_overlap = _x_overlap_ratio(a, b)
    center_gap = abs((float(a[0]) + float(a[2]) - float(b[0]) - float(b[2])) / 2.0)
    same_column = x_overlap >= 0.30 or center_gap <= page_w * 0.12
    if not same_column:
        return False
    return vertical_gap <= max(14.0, line_h * 1.35)


def _merge_close_signer_instances_for_label(
    label: str,
    instances: list[dict[str, Any]],
    pages_by_index: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    if label not in _MULTI_INSTANCE_FIELDS or len(instances) <= 1:
        return list(instances)
    ordered = sorted(instances, key=lambda inst: (_instance_page_index(inst), _instance_bbox(inst)[1], _instance_bbox(inst)[0]))
    groups: list[list[dict[str, Any]]] = []
    for inst in ordered:
        page = pages_by_index.get(_instance_page_index(inst))
        if page is None or not groups:
            groups.append([inst])
            continue
        _words, words_by_id = _page_word_maps(page)
        if any(_signer_fragments_are_close(label, member, inst, page, words_by_id) for member in groups[-1]):
            groups[-1].append(inst)
        else:
            groups.append([inst])

    merged: list[dict[str, Any]] = []
    for sequence, group in enumerate(groups, start=1):
        page = pages_by_index.get(_instance_page_index(group[0]))
        if page is None:
            merged.extend(group)
            continue
        _words, words_by_id = _page_word_maps(page)
        merged.append(_merge_instance_group(label, group, page, words_by_id, sequence))
    return merged


def _instance_score(label: str, inst: dict[str, Any], page_count: int) -> float:
    text = str(inst.get("text") or "")
    normalized = normalize_vn_text(text)
    bbox = _instance_bbox(inst)
    word_count = len(inst.get("word_ids") or normalized.split())
    line_count = max(1, len(inst.get("line_ids") or []))
    score = min(2.5, word_count * 0.08) + min(1.0, line_count * 0.15)
    try:
        if inst.get("confidence") is not None:
            score += min(2.0, max(-2.0, float(inst.get("confidence"))))
    except (TypeError, ValueError):
        pass

    for term in _ANCHOR_TERMS.get(label, ()):
        if term in normalized:
            score += 1.2
            break

    if label in {"REGIME_HEADER", "ISSUE_ORG_SUPERIOR", "ISSUE_ORG_NAME", "DOC_NUMBER_SYMBOL", "PLACE_DATE", "DOC_SUBJECT", "ADDRESSEE"}:
        score += max(0.0, 1.0 - _instance_page_index(inst) * 0.35)
    if label == "RECIPIENTS":
        score += min(1.2, _instance_page_index(inst) * 0.15)
    if label == "DOC_SUBJECT":
        if len(normalized) < 10:
            score -= 2.5
        if _RE_DOC_NUMBER.search(normalized) or _RE_DATE.search(normalized):
            score -= 1.5
    if label == "DOC_NUMBER_SYMBOL" and not _RE_DOC_NUMBER.search(normalized):
        score -= 1.0
    if label == "PLACE_DATE" and not _RE_DATE.search(normalized):
        score -= 1.0
    if label == "RECIPIENTS" and "noi nhan" not in normalized:
        score -= 0.7
    if page_count > 0 and _instance_page_index(inst) >= page_count:
        score -= 2.0
    if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        score -= 1.0
    return score


def _sort_field_instances(instances: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(instances, key=lambda inst: (_instance_page_index(inst), _instance_bbox(inst)[1], _instance_bbox(inst)[0], str(inst.get("label") or "")))


def apply_primary_page_field_policy(canonical: dict[str, Any], annotation: dict[str, Any]) -> dict[str, Any]:
    """Keep document metadata anchored to page 0.

    KIE is allowed to run on page 0 plus a signer page. Non-primary pages are
    only authoritative for trailing fields: recipients and signer name/role.
    """
    del canonical
    instances = annotation.get("field_instances") or []
    if not instances:
        return annotation
    page_indices = {_instance_page_index(inst) for inst in instances}
    if _PRIMARY_METADATA_PAGE not in page_indices or not any(page != _PRIMARY_METADATA_PAGE for page in page_indices):
        return annotation

    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for inst in instances:
        page_index = _instance_page_index(inst)
        label = str(inst.get("label") or "")
        if page_index == _PRIMARY_METADATA_PAGE or label in _NON_PRIMARY_PAGE_ALLOWED_LABELS:
            kept.append(inst)
        else:
            dropped.append(inst)

    if not dropped:
        return annotation

    out = deepcopy(annotation)
    out["field_instances"] = kept
    post = dict(out.get("postprocess") or {})
    detail = post.setdefault("primary_page_field_policy", [])
    detail.append(
        {
            "primary_page": _PRIMARY_METADATA_PAGE,
            "non_primary_allowed_labels": sorted(_NON_PRIMARY_PAGE_ALLOWED_LABELS),
            "dropped": [
                {
                    "label": inst.get("label"),
                    "page_index": _instance_page_index(inst),
                    "text": inst.get("text"),
                }
                for inst in dropped
            ],
        }
    )
    out["postprocess"] = post
    return out


def _drop_unpaired_signer_roles(instances: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    roles = [inst for inst in instances if inst.get("label") == "SIGNER_ROLE"]
    names = [inst for inst in instances if inst.get("label") == "SIGNER_NAME"]
    if not roles or not names:
        return instances, []
    name_pages = {_instance_page_index(inst) for inst in names}
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for inst in instances:
        if inst.get("label") == "SIGNER_ROLE" and _instance_page_index(inst) not in name_pages:
            dropped.append(inst)
            continue
        kept.append(inst)
    if not dropped:
        return instances, []
    return kept, [
        {
            "label": "SIGNER_ROLE",
            "action": "drop_unpaired_signer_role",
            "dropped_texts": [inst.get("text") for inst in dropped],
        }
    ]


def _word_coverage(source: set[str], target: set[str]) -> float:
    return len(source & target) / len(target) if target else 0.0


def _line_coverage(source: set[str], target: set[str]) -> float:
    return len(source & target) / len(target) if target else 0.0


def _looks_incomplete_header(text: str) -> bool:
    normalized = normalize_vn_text(text)
    if not normalized:
        return False
    if normalized.endswith((",", ";", "-")):
        return True
    parts = normalized.split()
    tail = " ".join(parts[-2:])
    last = parts[-1] if parts else ""
    return tail in _INCOMPLETE_HEADER_TAILS or last in _INCOMPLETE_HEADER_TAILS


def _needs_issue_org_repair(
    current_sup: list[dict[str, Any]],
    current_name: list[dict[str, Any]],
    existing_sup_lines: set[str],
    existing_name_lines: set[str],
    existing_sup_words: set[str],
    existing_name_words: set[str],
    candidate_sup_lines: set[str],
    candidate_name_lines: set[str],
    candidate_sup_words: set[str],
    candidate_name_words: set[str],
    existing_sup_text: str,
    existing_name_text: str,
) -> tuple[bool, list[str]]:
    candidate_lines = candidate_sup_lines | candidate_name_lines
    existing_issue_lines = existing_sup_lines | existing_name_lines
    if not existing_issue_lines:
        return False, []
    if (
        existing_sup_lines == candidate_sup_lines
        and existing_name_lines == candidate_name_lines
        and _word_coverage(existing_sup_words, candidate_sup_words) >= 0.98
        and _word_coverage(existing_name_words, candidate_name_words) >= 0.98
    ):
        return False, []
    if _line_coverage(existing_issue_lines, candidate_lines) < 0.80:
        return False, []

    reasons: list[str] = []
    if not current_sup or not current_name:
        merged_cover = _line_coverage(existing_sup_lines, candidate_lines) >= 0.80 or _line_coverage(existing_name_lines, candidate_lines) >= 0.80
        if merged_cover:
            reasons.append("missing_field_but_other_issue_org_span_covers_header")
        else:
            return False, []
    if existing_sup_lines & existing_name_lines:
        reasons.append("issue_org_spans_overlap")
    if existing_name_lines & candidate_sup_lines:
        reasons.append("org_name_contains_superior_lines")
    if existing_sup_lines & candidate_name_lines:
        reasons.append("superior_contains_org_name_lines")
    if existing_sup_lines == candidate_sup_lines and _word_coverage(existing_sup_words, candidate_sup_words) < 0.98:
        reasons.append("superior_line_not_full")
    if existing_name_lines == candidate_name_lines and _word_coverage(existing_name_words, candidate_name_words) < 0.98:
        reasons.append("org_name_line_not_full")
    if _looks_incomplete_header(existing_sup_text) and _line_coverage(existing_issue_lines, candidate_lines) >= 0.80:
        reasons.append("superior_text_looks_incomplete")
    if _RE_SUPERIOR_HINT.search(normalize_vn_text(existing_name_text)) and (existing_name_lines & candidate_sup_lines):
        reasons.append("org_name_text_contains_superior_hints")
    return bool(reasons), reasons


def _confidence(instances: list[dict[str, Any]]) -> float | None:
    values = []
    for inst in instances:
        value = inst.get("confidence")
        if value is not None:
            try:
                values.append(float(value))
            except (TypeError, ValueError):
                pass
    return sum(values) / len(values) if values else None


def _instance_from_lines(
    label: str,
    lines: list[dict[str, Any]],
    words_by_id: dict[str, dict[str, Any]],
    page_index: int,
    existing: list[dict[str, Any]],
) -> dict[str, Any]:
    word_ids = _word_ids_for_lines(lines)
    boxes = [_word_bbox(words_by_id[word_id]) for word_id in word_ids if word_id in words_by_id]
    payload = {
        "field_id": existing[0].get("field_id") if existing else f"postprocess:{label}",
        "label": label,
        "page_index": page_index,
        "line_ids": [line["line_id"] for line in lines],
        "word_ids": word_ids,
        "bbox": _bbox_union(boxes),
        "text": "\n".join(line["text"] for line in lines),
        "constraint_applied": True,
    }
    conf = _confidence(existing)
    if conf is not None:
        payload["confidence"] = conf
    return payload


def _line_block_close(left: dict[str, Any], right: dict[str, Any], page: dict[str, Any]) -> bool:
    page_w = max(float(page.get("width") or page.get("page_width") or page.get("render_width") or 1.0), 1.0)
    page_words, _ = _page_word_maps(page)
    line_h = max(8.0, _median_word_height(page_words))
    a = left["bbox"]
    b = right["bbox"]
    vertical_gap = max(0.0, float(b[1]) - float(a[3]))
    if vertical_gap > max(18.0, line_h * 2.0):
        return False
    x_overlap = _x_overlap_ratio(a, b)
    center_gap = abs((float(a[0]) + float(a[2]) - float(b[0]) - float(b[2])) / 2.0)
    return x_overlap >= 0.05 or center_gap <= page_w * 0.24


def _is_decorative_line_text(text: str) -> bool:
    raw = str(text or "").strip()
    if not raw:
        return True
    compact = re.sub(r"\s+", "", raw)
    if re.fullmatch(r"[*]+", compact):
        return True
    return bool(re.fullmatch(r"[-–—_=*\.]{2,}", compact))


def _looks_like_signer_role_line_norm(norm: str) -> bool:
    norm = str(norm or "").strip()
    if not norm:
        return False
    return bool(_RE_SIGNER_ROLE_MARKER.search(norm))


def _is_standalone_doc_title_line(line: dict[str, Any]) -> bool:
    norm = str(line.get("norm") or normalize_vn_text(line.get("text") or ""))
    return bool(_RE_STANDALONE_DOC_TITLE.fullmatch(norm))


def _has_near_doc_number_prefix_context(lines: list[dict[str, Any]], line_index: int) -> bool:
    if line_index <= 0:
        return False
    prev_norms = [
        str(line.get("norm") or normalize_vn_text(line.get("text") or ""))
        for line in lines[max(0, line_index - 3):line_index]
    ]
    prev_norms = [norm for norm in prev_norms if norm and not _is_decorative_line_text(norm)]
    if not prev_norms:
        return False
    joined = " ".join(prev_norms[-3:])
    if _looks_like_doc_number_norm(joined):
        return True
    has_prefix = any(_RE_DOC_NUMBER_PREFIX_ONLY.fullmatch(norm) or _RE_DOC_NUMBER_PREFIX.match(norm) for norm in prev_norms)
    has_number = any(_RE_NUMBER_ONLY.fullmatch(norm) or _RE_STANDALONE_DOC_NUMBER_ONLY.fullmatch(norm) for norm in prev_norms)
    return has_prefix and has_number


def _is_leading_subject_noise_line(line: dict[str, Any], lines: list[dict[str, Any]], line_index: int) -> bool:
    norm = str(line.get("norm") or normalize_vn_text(line.get("text") or ""))
    if _looks_like_standalone_doc_number_norm(norm):
        return True
    if _looks_like_doc_symbol_continuation_norm(norm):
        return True
    return False


def _format_doc_subject_text(text: str) -> str:
    lines = [part.strip() for part in re.split(r"[\r\n]+", str(text or "")) if part.strip()]
    if not lines:
        return str(text or "").strip()

    def _looks_all_caps_line(value: str) -> bool:
        letters = [ch for ch in value if ch.isalpha()]
        if len(letters) < 3:
            return False
        upper = sum(1 for ch in letters if ch.upper() == ch and ch.lower() != ch)
        lower = sum(1 for ch in letters if ch.lower() == ch and ch.upper() != ch)
        return upper > 0 and lower == 0

    formatted: list[str] = []
    for idx, line in enumerate(lines):
        norm = normalize_vn_text(line)
        if idx == 0 and _RE_STANDALONE_DOC_TITLE.fullmatch(norm):
            try:
                from scanindex.core.digitization.doctype import detect_from_doc_subject
                line = detect_from_doc_subject(line) or line
            except Exception:
                line = line[:1].upper() + line[1:].lower() if line else line
        elif _looks_all_caps_line(line):
            line = line.lower()
        formatted.append(line)
    joined = _RE_WS.sub(" ", " ".join(formatted)).strip()
    return joined[:1].upper() + joined[1:] if joined else joined


def apply_doc_subject_text_format(annotation: dict[str, Any]) -> dict[str, Any]:
    fields = annotation.get("field_instances") or []
    if not fields:
        return annotation
    out = None
    out_fields: list[dict[str, Any]] = []
    details: list[dict[str, Any]] = []
    for inst in fields:
        new_inst = inst
        if inst.get("label") == "DOC_SUBJECT":
            old_text = str(inst.get("text") or "")
            new_text = _format_doc_subject_text(old_text)
            if new_text != old_text:
                if out is None:
                    out = deepcopy(annotation)
                new_inst = dict(inst)
                new_inst["text"] = new_text
                details.append({
                    "page_index": _instance_page_index(inst),
                    "old_text": old_text,
                    "new_text": new_text,
                })
        out_fields.append(new_inst)
    if out is None:
        return annotation
    out["field_instances"] = out_fields
    post = dict(out.get("postprocess") or {})
    post["doc_subject_text_format"] = details
    out["postprocess"] = post
    return out


def _subject_line_allowed(line: dict[str, Any]) -> bool:
    text = str(line.get("text") or "").strip()
    if _is_decorative_line_text(text):
        return False
    norm = str(line.get("norm") or normalize_vn_text(text))
    if not norm:
        return False
    if _RE_DATE.search(norm) or _RE_REGIME.search(norm):
        return False
    if _RE_STAMP_MARK_LINE.fullmatch(norm):
        return False
    if re.fullmatch(r"[\d\s/.-]+", norm):
        return False
    if norm.startswith("- ") and not (norm.startswith("- ve ") or _RE_TITLE_NOISE.search(norm)):
        return False
    if norm.startswith(("nham ", "can cu ")):
        return False
    if "kinh gui" in norm or "noi nhan" in norm or _looks_like_signer_role_line_norm(norm):
        return False
    if re.match(r"^\s*(?:\d+|[ivx]+)\s*[\.\)]\s+", norm, re.IGNORECASE):
        return False
    try:
        if float(line.get("top", 0.0) or 0.0) > 0.58:
            return False
    except (TypeError, ValueError):
        pass
    return True


def _subject_expand_allowed(seed_lines: list[dict[str, Any]], candidate: dict[str, Any], page: dict[str, Any]) -> bool:
    if not _subject_line_allowed(candidate):
        return False
    if not seed_lines:
        return True
    norm = str(candidate.get("norm") or normalize_vn_text(candidate.get("text") or ""))
    if norm.startswith(("ve ", "va ", "cua ", "doi ", "den ", "tai ", "cho ")):
        return True
    page_words, _ = _page_word_maps(page)
    line_h = max(8.0, _median_word_height(page_words))
    seed_boxes = [line["bbox"] for line in seed_lines if line.get("bbox")]
    if seed_boxes:
        seed_bbox = _bbox_union(seed_boxes)
        cand_bbox = candidate.get("bbox") or [0.0, 0.0, 0.0, 0.0]
        vertical_gap = max(0.0, max(float(seed_bbox[1]), float(cand_bbox[1])) - min(float(seed_bbox[3]), float(cand_bbox[3])))
        if vertical_gap > max(28.0, line_h * 2.2):
            return False
        seed_cx = (float(seed_bbox[0]) + float(seed_bbox[2])) / 2.0
        cand_cx = (float(cand_bbox[0]) + float(cand_bbox[2])) / 2.0
        page_w = max(float(page.get("width") or page.get("page_width") or page.get("render_width") or 1.0), 1.0)
        if abs(seed_cx - cand_cx) > page_w * 0.28 and _x_overlap_ratio(seed_bbox, cand_bbox) < 0.05:
            return False
    return True


def _subject_block_from_prediction(page: dict[str, Any], instances: list[dict[str, Any]]) -> list[dict[str, Any]]:
    lines = _ordered_page_lines(page)
    if not lines:
        return []
    selected_word_ids = {
        str(word_id)
        for inst in instances
        for word_id in (inst.get("word_ids") or [])
    }
    if not selected_word_ids:
        return []
    selected_indices: list[int] = []
    for idx, line in enumerate(lines):
        line_word_ids = {str(word_id) for word_id in (line.get("word_ids") or [])}
        if not (selected_word_ids & line_word_ids):
            continue
        if _subject_line_allowed(line):
            selected_indices.append(idx)
    if not selected_indices:
        return []

    segments: list[list[int]] = []
    current: list[int] = []
    last_idx: int | None = None
    for idx in selected_indices:
        has_stop_between = (
            last_idx is not None
            and any(not _subject_line_allowed(lines[mid]) for mid in range(last_idx + 1, idx))
        )
        if current and has_stop_between:
            segments.append(current)
            current = []
        current.append(idx)
        last_idx = idx
    if current:
        segments.append(current)

    def segment_score(segment: list[int]) -> float:
        text = _line_block_text([lines[idx] for idx in segment])
        norm = normalize_vn_text(text)
        score = float(len(segment))
        if _RE_TITLE_NOISE.search(norm):
            score += 2.0
        if norm.startswith("ve ") or " ve " in f" {norm} ":
            score += 1.0
        try:
            score += max(0.0, 1.0 - float(lines[segment[0]].get("top", 0.0) or 0.0))
        except (TypeError, ValueError, IndexError):
            pass
        return score

    selected_indices = max(segments, key=segment_score)

    start = min(selected_indices)
    end = max(selected_indices)
    while start > 0:
        prev_line = lines[start - 1]
        if _is_decorative_line_text(prev_line.get("text")):
            break
        seed = [lines[idx] for idx in range(start, end + 1)]
        if not _line_block_close(prev_line, lines[start], page):
            break
        if not _subject_expand_allowed(seed, prev_line, page):
            break
        start -= 1
        if end - start + 1 >= 6:
            break

    while end + 1 < len(lines):
        next_line = lines[end + 1]
        if _is_decorative_line_text(next_line.get("text")):
            break
        seed = [lines[idx] for idx in range(start, end + 1)]
        if not _line_block_close(lines[end], next_line, page):
            break
        if not _subject_expand_allowed(seed, next_line, page):
            break
        end += 1
        if end - start + 1 >= 6:
            break

    result = [line for line in lines[start:end + 1] if _subject_line_allowed(line)]
    line_index_by_id = {str(line.get("line_id") or ""): idx for idx, line in enumerate(lines)}
    while len(result) > 1:
        first = result[0]
        first_idx = line_index_by_id.get(str(first.get("line_id") or ""), start)
        if not _is_leading_subject_noise_line(first, lines, first_idx):
            break
        result = result[1:]
    return result


def apply_doc_subject_line_block_constraints(canonical: dict[str, Any], annotation: dict[str, Any]) -> dict[str, Any]:
    pages = canonical.get("pages") or []
    instances = annotation.get("field_instances") or []
    if not pages or not instances:
        return annotation

    out = deepcopy(annotation)
    pages_by_index = {int(page.get("page_index", idx)): page for idx, page in enumerate(pages)}
    grouped: dict[int, list[dict[str, Any]]] = {}
    for inst in out.get("field_instances") or []:
        if inst.get("label") == "DOC_SUBJECT":
            grouped.setdefault(_instance_page_index(inst), []).append(inst)
    if not grouped:
        return annotation

    replacements: dict[int, dict[str, Any]] = {}
    details: list[dict[str, Any]] = []
    for page_index, subject_instances in grouped.items():
        page = pages_by_index.get(page_index)
        if page is None:
            continue
        lines = _subject_block_from_prediction(page, subject_instances)
        if not lines:
            continue
        _words, words_by_id = _page_word_maps(page)
        repaired = _instance_from_lines("DOC_SUBJECT", lines, words_by_id, page_index, subject_instances)
        old_word_ids = [str(word_id) for inst in subject_instances for word_id in (inst.get("word_ids") or [])]
        new_word_ids = [str(word_id) for word_id in (repaired.get("word_ids") or [])]
        if old_word_ids == new_word_ids:
            continue
        replacements[page_index] = repaired
        details.append(
            {
                "label": "DOC_SUBJECT",
                "page_index": page_index,
                "action": "complete_subject_line_block",
                "old_texts": [inst.get("text") for inst in subject_instances],
                "new_text": repaired.get("text"),
            }
        )

    if not replacements:
        return annotation

    final_instances: list[dict[str, Any]] = []
    for inst in out.get("field_instances") or []:
        page_index = _instance_page_index(inst)
        if inst.get("label") == "DOC_SUBJECT" and page_index in replacements:
            continue
        final_instances.append(inst)
    final_instances.extend(replacements.values())
    out["field_instances"] = _sort_field_instances(final_instances)
    post = dict(out.get("postprocess") or {})
    post["doc_subject_line_block_constraints"] = details
    out["postprocess"] = post
    return out


def _line_block_text(lines: list[dict[str, Any]]) -> str:
    return " ".join(str(line.get("text") or "").strip() for line in lines if str(line.get("text") or "").strip()).strip()


def _line_block_word_overlap(lines: list[dict[str, Any]], predicted_word_ids: set[str]) -> int:
    return sum(1 for word_id in _word_ids_for_lines(lines) if str(word_id) in predicted_word_ids)


def _doc_number_no_digit_line_allowed(norm: str) -> bool:
    if re.fullmatch(r"so\s*[:.]?", norm):
        return True
    return bool(re.search(r"[/.-]\s*[a-z]", norm))


def _doc_number_block_score(lines: list[dict[str, Any]], predicted_word_ids: set[str]) -> float | None:
    overlap = _line_block_word_overlap(lines, predicted_word_ids)
    if overlap <= 0:
        return None
    norms = [str(line.get("norm") or normalize_vn_text(line.get("text") or "")) for line in lines]
    norm = normalize_vn_text(_line_block_text(lines))
    has_digit = any(ch.isdigit() for ch in norm)
    if not has_digit:
        return None

    # If OCR splits "Số:" and the number into two near lines, accept the two-line
    # block. Do not accept "SỞ ..." as that no-digit companion line.
    for line_norm in norms:
        if any(ch.isdigit() for ch in line_norm):
            continue
        if not _doc_number_no_digit_line_allowed(line_norm):
            return None

    has_so_number = bool(_RE_DOC_NUMBER.search(norm))
    has_symbol_value = bool(_RE_DOC_NUMBER_SYMBOL_VALUE.search(norm))
    if not (has_so_number or has_symbol_value):
        return None

    score = float(overlap * 2)
    if has_so_number:
        score += 8.0
    if has_symbol_value:
        score += 7.0
    if norm.startswith("so"):
        score += 1.0
    if "ngay" in norm or "thang" in norm or "nam" in norm:
        score -= 5.0
    score -= max(0, len(lines) - 1) * 1.2
    return score


def _place_date_block_score(lines: list[dict[str, Any]], predicted_word_ids: set[str]) -> float | None:
    overlap = _line_block_word_overlap(lines, predicted_word_ids)
    if overlap <= 0:
        return None
    norm = normalize_vn_text(_line_block_text(lines))
    if "kinh gui" in norm or "noi nhan" in norm:
        return None
    strict = bool(_RE_PLACE_DATE_STRICT.search(norm))
    loose = bool(_RE_DATE.search(norm) and ("ngay" in norm or "thang" in norm or "nam" in norm))
    if not (strict or loose):
        return None

    score = float(overlap * 2)
    if strict:
        score += 9.0
    if "ngay" in norm:
        score += 2.0
    if "thang" in norm:
        score += 1.0
    if "nam" in norm:
        score += 1.0
    score -= max(0, len(lines) - 1) * 0.8
    return score


def _best_single_line_block(
    label: str,
    page: dict[str, Any],
    existing: list[dict[str, Any]],
) -> tuple[float, list[dict[str, Any]]] | None:
    predicted_word_ids = {str(word_id) for inst in existing for word_id in (inst.get("word_ids") or [])}
    if not predicted_word_ids:
        return None
    lines = _ordered_page_lines(page)
    candidates: list[tuple[float, list[dict[str, Any]]]] = []
    for idx, line in enumerate(lines):
        blocks = [[line]]
        if idx + 1 < len(lines) and _line_block_close(line, lines[idx + 1], page):
            blocks.append([line, lines[idx + 1]])
        for block in blocks:
            if label == "DOC_NUMBER_SYMBOL":
                score = _doc_number_block_score(block, predicted_word_ids)
            elif label == "PLACE_DATE":
                score = _place_date_block_score(block, predicted_word_ids)
            else:
                score = None
            if score is not None:
                candidates.append((score, block))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])


def _predicted_line_ids_for_instances(instances: list[dict[str, Any]], words_by_id: dict[str, dict[str, Any]]) -> set[str]:
    line_ids: set[str] = set()
    for inst in instances:
        line_ids.update(_line_ids_for_instance(inst, words_by_id))
    return line_ids


def _predicted_word_ids_for_instances(instances: list[dict[str, Any]]) -> set[str]:
    return {str(word_id) for inst in instances for word_id in (inst.get("word_ids") or [])}


def _single_line_block_repair_needed(
    label: str,
    page: dict[str, Any],
    existing: list[dict[str, Any]],
    words_by_id: dict[str, dict[str, Any]],
) -> bool:
    predicted_word_ids = _predicted_word_ids_for_instances(existing)
    if not predicted_word_ids:
        return False
    lines = _ordered_page_lines(page)
    line_by_id = {str(line["line_id"]): line for line in lines}
    predicted_line_ids = _predicted_line_ids_for_instances(existing, words_by_id)
    if not predicted_line_ids:
        return False

    if label == "PLACE_DATE":
        # Keep OCR-split date blocks intact. Only repair one-line date fragments,
        # e.g. a word in the same visual date line was assigned to ADDRESSEE.
        if len(predicted_line_ids) > 1:
            return False
        line = line_by_id.get(next(iter(predicted_line_ids)))
        if line is None or _place_date_block_score([line], predicted_word_ids) is None:
            return False
        line_word_ids = {str(word_id) for word_id in line.get("word_ids") or []}
        coverage = len(predicted_word_ids & line_word_ids) / len(line_word_ids) if line_word_ids else 1.0
        return len(existing) > 1 or coverage < 0.98

    if label == "DOC_NUMBER_SYMBOL":
        if len(predicted_line_ids) > 2:
            return False
        invalid_no_digit_line = False
        for line_id in predicted_line_ids:
            line = line_by_id.get(str(line_id))
            if line is None:
                continue
            line_norm = str(line.get("norm") or normalize_vn_text(line.get("text") or ""))
            if any(ch.isdigit() for ch in line_norm):
                continue
            if not _doc_number_no_digit_line_allowed(line_norm):
                invalid_no_digit_line = True
                break
        if invalid_no_digit_line:
            return True
        if len(predicted_line_ids) == 1:
            line = line_by_id.get(next(iter(predicted_line_ids)))
            if line is None or _doc_number_block_score([line], predicted_word_ids) is None:
                return False
            line_word_ids = {str(word_id) for word_id in line.get("word_ids") or []}
            coverage = len(predicted_word_ids & line_word_ids) / len(line_word_ids) if line_word_ids else 1.0
            return len(existing) > 1 or coverage < 0.98
        return False

    return False


def _rebuild_instance_from_word_ids(
    inst: dict[str, Any],
    word_ids: list[str],
    page: dict[str, Any],
    words_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    word_ids = _ordered_word_ids_for_page(word_ids, page, words_by_id)
    boxes = [_word_bbox(words_by_id[word_id]) for word_id in word_ids if word_id in words_by_id]
    if not boxes:
        return None
    rebuilt = deepcopy(inst)
    rebuilt["word_ids"] = word_ids
    rebuilt["line_ids"] = _line_ids_for_word_ids(word_ids, words_by_id, _line_order_map(page))
    rebuilt["bbox"] = _bbox_union(boxes)
    rebuilt["text"] = _text_for_word_ids(word_ids, words_by_id)
    rebuilt["line_block_trimmed"] = True
    return rebuilt


def apply_single_line_block_constraints(canonical: dict[str, Any], annotation: dict[str, Any]) -> dict[str, Any]:
    """Keep DOC_NUMBER_SYMBOL and PLACE_DATE inside a short visual line block.

    These fields are normally one OCR line, but we allow two close lines when OCR
    or document layout splits the anchor and value. The selected block must have
    field-specific evidence, so stray tokens like "SỞ" cannot join doc number and
    stray date words cannot join ADDRESSEE.
    """
    pages = canonical.get("pages") or []
    instances = annotation.get("field_instances") or []
    if not pages or not instances:
        return annotation

    out = deepcopy(annotation)
    pages_by_index = {int(page.get("page_index", idx)): page for idx, page in enumerate(pages)}
    grouped: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for inst in out.get("field_instances") or []:
        label = str(inst.get("label") or "")
        if label in _SINGLE_LINE_FIELDS:
            grouped.setdefault((label, _instance_page_index(inst)), []).append(inst)

    replacements: dict[tuple[str, int], dict[str, Any]] = {}
    locked_by_page: dict[int, set[str]] = {}
    details: list[dict[str, Any]] = []
    for (label, page_index), label_instances in grouped.items():
        page = pages_by_index.get(page_index)
        if page is None:
            continue
        _words, words_by_id = _page_word_maps(page)
        if not _single_line_block_repair_needed(label, page, label_instances, words_by_id):
            continue
        best = _best_single_line_block(label, page, label_instances)
        if best is None:
            continue
        lines, score = best[1], best[0]
        repaired = _instance_from_lines(label, lines, words_by_id, page_index, label_instances)
        old_texts = [str(inst.get("text") or "") for inst in label_instances]
        if [text.strip() for text in old_texts] == [str(repaired.get("text") or "").strip()]:
            continue
        replacements[(label, page_index)] = repaired
        locked_by_page.setdefault(page_index, set()).update(str(word_id) for word_id in repaired.get("word_ids") or [])
        details.append(
            {
                "label": label,
                "page_index": page_index,
                "action": "select_short_line_block",
                "score": score,
                "old_texts": old_texts,
                "new_text": repaired.get("text"),
            }
        )

    if not replacements:
        return annotation

    final_instances: list[dict[str, Any]] = []
    for inst in out.get("field_instances") or []:
        label = str(inst.get("label") or "")
        page_index = _instance_page_index(inst)
        if (label, page_index) in replacements:
            continue
        locked = locked_by_page.get(page_index)
        if locked and label not in _SINGLE_LINE_FIELDS:
            word_ids = [str(word_id) for word_id in (inst.get("word_ids") or []) if str(word_id) not in locked]
            if len(word_ids) != len(inst.get("word_ids") or []):
                page = pages_by_index.get(page_index)
                if page is None:
                    continue
                _words, words_by_id = _page_word_maps(page)
                rebuilt = _rebuild_instance_from_word_ids(inst, word_ids, page, words_by_id)
                if rebuilt is None:
                    details.append(
                        {
                            "label": label,
                            "page_index": page_index,
                            "action": "drop_overlap_with_short_line_block",
                            "old_text": inst.get("text"),
                        }
                    )
                    continue
                details.append(
                    {
                        "label": label,
                        "page_index": page_index,
                        "action": "trim_overlap_with_short_line_block",
                        "old_text": inst.get("text"),
                        "new_text": rebuilt.get("text"),
                    }
                )
                final_instances.append(rebuilt)
                continue
        final_instances.append(inst)

    final_instances.extend(replacements.values())
    out["field_instances"] = _sort_field_instances(final_instances)
    post = dict(out.get("postprocess") or {})
    post["single_line_block_constraints"] = details
    out["postprocess"] = post
    return out


def apply_issue_org_header_constraints(canonical: dict[str, Any], annotation: dict[str, Any]) -> dict[str, Any]:
    """Repair top-left issue-org fields using the decoded right-column anchor.

    The rule only touches ISSUE_ORG_SUPERIOR and ISSUE_ORG_NAME, and only when
    the current prediction has clear evidence of overlap, truncation, or merged
    superior/name spans. It filters words, not whole OCR lines, against the
    leftmost x0 of REGIME_HEADER or PLACE_DATE.
    """
    pages = canonical.get("pages") or []
    if not pages or not annotation.get("field_instances"):
        return annotation
    out = deepcopy(annotation)
    details: list[dict[str, Any]] = []

    candidate_pages = sorted(
        {
            int(inst.get("page_index") or 0)
            for inst in out.get("field_instances", [])
            if inst.get("label") in _ISSUE_ORG_FIELDS
        }
        or {0}
    )
    page_by_index = {int(page.get("page_index", idx)): page for idx, page in enumerate(pages)}

    for page_index in candidate_pages:
        page = page_by_index.get(page_index)
        if page is None:
            continue
        current_sup = _instances_by_label(out, "ISSUE_ORG_SUPERIOR", page_index)
        current_name = _instances_by_label(out, "ISSUE_ORG_NAME", page_index)
        if not current_sup and not current_name:
            continue
        candidate = _issue_org_header_candidate(page, out, page_index)
        if candidate is None:
            continue
        superior_lines, org_lines, detail = candidate
        _words, words_by_id = _page_word_maps(page)

        existing_sup_lines = set().union(*[_line_ids_for_instance(inst, words_by_id) for inst in current_sup]) if current_sup else set()
        existing_name_lines = set().union(*[_line_ids_for_instance(inst, words_by_id) for inst in current_name]) if current_name else set()
        existing_sup_words = {str(word_id) for inst in current_sup for word_id in (inst.get("word_ids") or [])}
        existing_name_words = {str(word_id) for inst in current_name for word_id in (inst.get("word_ids") or [])}
        candidate_sup_lines = {line["line_id"] for line in superior_lines}
        candidate_name_lines = {line["line_id"] for line in org_lines}
        candidate_sup_words = set(_word_ids_for_lines(superior_lines))
        candidate_name_words = set(_word_ids_for_lines(org_lines))
        existing_sup_text = " ".join(str(inst.get("text") or "") for inst in current_sup)
        existing_name_text = " ".join(str(inst.get("text") or "") for inst in current_name)

        need_repair, reasons = _needs_issue_org_repair(
            current_sup,
            current_name,
            existing_sup_lines,
            existing_name_lines,
            existing_sup_words,
            existing_name_words,
            candidate_sup_lines,
            candidate_name_lines,
            candidate_sup_words,
            candidate_name_words,
            existing_sup_text,
            existing_name_text,
        )
        if not need_repair:
            continue

        repaired_sup = _instance_from_lines("ISSUE_ORG_SUPERIOR", superior_lines, words_by_id, page_index, current_sup)
        repaired_name = _instance_from_lines("ISSUE_ORG_NAME", org_lines, words_by_id, page_index, current_name)
        kept = [
            inst
            for inst in out.get("field_instances", [])
            if not (int(inst.get("page_index") or 0) == page_index and inst.get("label") in _ISSUE_ORG_FIELDS)
        ]
        kept.extend([repaired_sup, repaired_name])
        out["field_instances"] = kept
        details.append(
            {
                "page_index": page_index,
                "reason": "issue_org_right_bound_constraint",
                "reasons": reasons,
                "right_bound_x": detail.get("right_bound_x"),
                "old_issue_org_superior": existing_sup_text,
                "old_issue_org_name": existing_name_text,
                "new_issue_org_superior": repaired_sup["text"],
                "new_issue_org_name": repaired_name["text"],
            }
        )

    if details:
        post = dict(out.get("postprocess") or {})
        post["issue_org_header_constraints"] = details
        out["postprocess"] = post
    return out


def apply_schema_cardinality_constraints(canonical: dict[str, Any], annotation: dict[str, Any]) -> dict[str, Any]:
    """Make token-classification output obey single-instance field cardinality.

    ISSUE_ORG_SUPERIOR/ISSUE_ORG_NAME are included here, but their layout-aware
    header repair should run first. SIGNER_ROLE/SIGNER_NAME are intentionally
    skipped and handled by signer-specific fragment logic.
    """
    pages = canonical.get("pages") or []
    instances = annotation.get("field_instances") or []
    if not pages or not instances:
        return annotation

    out = deepcopy(annotation)
    pages_by_index = {int(page.get("page_index", idx)): page for idx, page in enumerate(pages)}
    by_label: dict[str, list[dict[str, Any]]] = {}
    passthrough: list[dict[str, Any]] = []
    for inst in out.get("field_instances") or []:
        label = inst.get("label")
        if not label:
            passthrough.append(inst)
            continue
        by_label.setdefault(str(label), []).append(inst)

    final_instances: list[dict[str, Any]] = list(passthrough)
    details: list[dict[str, Any]] = []
    page_count = len(pages)

    for label, label_instances in by_label.items():
        if label in _MULTI_INSTANCE_FIELDS:
            final_instances.extend(label_instances)
            continue
        before_count = len(label_instances)
        merged = _merge_close_instances_for_label(label, label_instances, pages_by_index)
        after_merge_count = len(merged)

        if not merged:
            continue
        if len(merged) == 1:
            final_instances.append(merged[0])
            if after_merge_count != before_count:
                details.append(
                    {
                        "label": label,
                        "action": "merge_nearby_single_instance",
                        "before": before_count,
                        "after": 1,
                    }
                )
            continue

        best = max(merged, key=lambda inst: _instance_score(label, inst, page_count))
        final_instances.append(best)
        details.append(
            {
                "label": label,
                "action": "select_best_single_instance",
                "before": before_count,
                "after_merge": after_merge_count,
                "kept_text": best.get("text"),
                "dropped_texts": [inst.get("text") for inst in merged if inst is not best],
            }
        )

    out["field_instances"] = _sort_field_instances(final_instances)
    if details:
        post = dict(out.get("postprocess") or {})
        post["schema_cardinality_constraints"] = details
        out["postprocess"] = post
    return out


def apply_signer_fragment_constraints(canonical: dict[str, Any], annotation: dict[str, Any]) -> dict[str, Any]:
    """Merge only adjacent signer fragments; keep distant signers separate."""
    pages = canonical.get("pages") or []
    instances = annotation.get("field_instances") or []
    if not pages or not instances:
        return annotation

    out = deepcopy(annotation)
    pages_by_index = {int(page.get("page_index", idx)): page for idx, page in enumerate(pages)}
    by_label: dict[str, list[dict[str, Any]]] = {label: [] for label in _MULTI_INSTANCE_FIELDS}
    passthrough: list[dict[str, Any]] = []
    for inst in out.get("field_instances") or []:
        label = inst.get("label")
        if label in _MULTI_INSTANCE_FIELDS:
            by_label[str(label)].append(inst)
        else:
            passthrough.append(inst)

    details: list[dict[str, Any]] = []
    final_instances = list(passthrough)
    for label in sorted(_MULTI_INSTANCE_FIELDS):
        label_instances = by_label.get(label, [])
        before_count = len(label_instances)
        merged = _merge_close_signer_instances_for_label(label, label_instances, pages_by_index)
        final_instances.extend(merged)
        if len(merged) != before_count:
            details.append(
                {
                    "label": label,
                    "action": "merge_nearby_signer_fragments",
                    "before": before_count,
                    "after": len(merged),
                }
            )

    final_instances, signer_details = _drop_unpaired_signer_roles(final_instances)
    details.extend(signer_details)
    out["field_instances"] = _sort_field_instances(final_instances)
    if details:
        post = dict(out.get("postprocess") or {})
        post["signer_fragment_constraints"] = details
        out["postprocess"] = post
    return out


def apply_layoutlmv3_schema_postprocess(canonical: dict[str, Any], annotation: dict[str, Any]) -> dict[str, Any]:
    """Run the full LayoutLMv3 runtime decoder cleanup in schema order."""
    out = apply_primary_page_field_policy(canonical, annotation)
    out = _normalize_field_instances_reading_order(canonical, out)
    out = apply_single_line_block_constraints(canonical, out)
    out = apply_issue_org_header_constraints(canonical, out)
    out = apply_schema_cardinality_constraints(canonical, out)
    out = apply_doc_subject_line_block_constraints(canonical, out)
    out = apply_signer_fragment_constraints(canonical, out)
    out = _normalize_field_instances_reading_order(canonical, out)
    out = apply_doc_subject_text_format(out)
    out = _ensure_doc_number_symbol(canonical, out)
    out = _ensure_doc_type(out)
    out = _ensure_rule_based_marks(canonical, out)
    out = apply_primary_page_field_policy(canonical, out)
    return out


def _ensure_doc_number_symbol(canonical: dict[str, Any], annotation: dict[str, Any]) -> dict[str, Any]:
    """Add DOC_NUMBER_SYMBOL from the visual first-page line when the model
    misses it entirely.

    This is intentionally conservative: it only accepts early page-0 lines
    whose normalized text starts with the document-number prefix ``so`` and
    then matches the existing doc-number shape. Body text such as
    "Hướng dẫn số 16-..." must not become the document number.
    """
    fields = annotation.get("field_instances") or []
    if any(
        inst.get("label") == "DOC_NUMBER_SYMBOL"
        and (inst.get("text") or "").strip()
        for inst in fields
    ):
        return annotation

    pages = canonical.get("pages") or []
    page = next((p for p in pages if int(p.get("page_index") or 0) == 0), None)
    if page is None:
        return annotation

    _words, words_by_id = _page_word_maps(page)
    candidates = []
    for line in _ordered_page_lines(page):
        norm = str(line.get("norm") or normalize_vn_text(line.get("text") or ""))
        if not norm.startswith("so"):
            continue
        if float(line.get("top") or 0.0) > 0.35:
            continue
        if not _looks_like_doc_number_norm(norm):
            continue
        candidates.append(line)
    if not candidates:
        return annotation

    chosen = min(candidates, key=lambda item: (float(item.get("top") or 0.0), float(item.get("x0") or 0.0)))
    out = deepcopy(annotation)
    inst = _instance_from_lines(
        "DOC_NUMBER_SYMBOL",
        [chosen],
        words_by_id,
        int(chosen.get("page_index") or page.get("page_index") or 0),
        [],
    )
    inst["field_id"] = "doc_number_symbol_auto"
    inst["confidence"] = 1.0
    inst["source"] = "postprocess"
    inst["doc_number_symbol_fallback"] = True
    out.setdefault("field_instances", []).append(inst)
    post = dict(out.get("postprocess") or {})
    post["doc_number_symbol_fallback"] = {
        "line_id": chosen.get("line_id"),
        "text": chosen.get("text"),
    }
    out["postprocess"] = post
    out["field_instances"] = _sort_field_instances(out.get("field_instances") or [])
    return out


def _ensure_rule_based_marks(canonical: dict[str, Any], annotation: dict[str, Any]) -> dict[str, Any]:
    """Append SECRECY_MARK / URGENCY_MARK / CIRCULATION_MARK by ROI rule.
    The LayoutLMv3 head doesn't predict these stamp-style labels, so this
    required post-process fills them with the same shared rule helper used by
    the LiLT decoder script."""
    from scanindex.core.kie.inference_pipeline import apply_rule_based_marks
    return apply_rule_based_marks(canonical, annotation)


def _ensure_doc_type(annotation: dict[str, Any]) -> dict[str, Any]:
    """Add a DOC_TYPE field_instance if KIE didn't emit one. The trained
    model treats DOC_TYPE as a deterministic post-process derived from
    DOC_SUBJECT prefix + DOC_NUMBER_SYMBOL suffix — not a learned label."""
    fields = annotation.get("field_instances") or []
    if any((f.get("label") == "DOC_TYPE"
            and (f.get("text") or "").strip())
           for f in fields):
        return annotation
    try:
        from scanindex.core.digitization.doctype import detect_doc_type
    except Exception:
        return annotation
    subj = ""
    num = ""
    for f in fields:
        lbl = f.get("label")
        if lbl == "DOC_SUBJECT" and not subj:
            subj = (f.get("text") or "").strip()
        elif lbl == "DOC_NUMBER_SYMBOL" and not num:
            num = (f.get("text") or "").strip()
    detected = detect_doc_type(subj, num)
    if not detected or detected == "Khác":
        # Don't pollute the annotation with the catch-all bucket.
        return annotation
    out = deepcopy(annotation)
    out_fields = out.setdefault("field_instances", [])
    out_fields.append({
        "label": "DOC_TYPE",
        "text": detected,
        "page_index": 0,
        "bbox": [0, 0, 0, 0],
        "field_id": "doc_type_auto",
        "score": 1.0,
        "source": "postprocess",
    })
    return out
