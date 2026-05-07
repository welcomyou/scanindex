from __future__ import annotations

import re
import unicodedata

from scanindex.core.kie.ontology import normalize_value


DOC_NUMBER_PREFIX_RE = re.compile(
    r"^\s*(?:[A-ZÀ-ỸĐ0-9./-]{1,12}\s+){0,2}s[ốoơô06]?\s*[:.]?\s*(.+)$",
    re.IGNORECASE,
)
BODY_PREFIXES = (
    "kinh gui",
    "kinh thua",
    "a.",
    "b.",
    "c.",
    "d.",
    "can cu",
    "xet",
    "thuc hien",
    "hom nay",
    "vao luc",
    "thoi gian",
    "dia diem",
    "thanh phan",
    "noi dung",
    "dieu ",
    "i.",
    "ii.",
    "iii.",
    "iv.",
    "1-",
    "1.",
    "2-",
    "2.",
    "noi nhan",
    "tong so",
    "chu tich uy ban nhan dan",
)
DISPATCH_SUBJECT_PREFIXES = (
    "v/v",
    "ve viec",
)
CONG_VAN_NORM = "cong van"
KNOWN_DOC_TYPES = {
    "cuong linh chinh tri": "CƯƠNG LĨNH CHÍNH TRỊ",
    "dieu le dang": "ĐIỀU LỆ ĐẢNG",
    "chien luoc": "CHIẾN LƯỢC",
    "nghi quyet": "NGHỊ QUYẾT",
    "bao cao": "BÁO CÁO",
    "bien ban": "BIÊN BẢN",
    "quyet dinh": "QUYẾT ĐỊNH",
    "quy dinh": "QUY ĐỊNH",
    "huong dan": "HƯỚNG DẪN",
    "danh sach": "DANH SÁCH",
    "to trinh": "TỜ TRÌNH",
    "ke hoach": "KẾ HOẠCH",
    "quy hoach": "QUY HOẠCH",
    "ket luan": "KẾT LUẬN",
    "chi thi": "CHỈ THỊ",
    "thong bao": "THÔNG BÁO",
    "thong cao": "THÔNG CÁO",
    "so yeu ly lich": "SƠ YẾU LÝ LỊCH",
    "ly lich 2c": "LÝ LỊCH 2C",
    "ho so can bo": "HỒ SƠ CÁN BỘ",
    "ke khai tai san": "KÊ KHAI TÀI SẢN",
    "ke khai tai san, thu nhap": "KÊ KHAI TÀI SẢN, THU NHẬP",
    "chuong trinh": "CHƯƠNG TRÌNH",
    "de an": "ĐỀ ÁN",
    "de cuong": "ĐỀ CƯƠNG",
    "de cuong chi tiet": "ĐỀ CƯƠNG CHI TIẾT",
    "phuong an": "PHƯƠNG ÁN",
    "du an": "DỰ ÁN",
    "giay moi": "GIẤY MỜI",
    "giay gioi thieu": "GIẤY GIỚI THIỆU",
    "giay chung nhan": "GIẤY CHỨNG NHẬN",
    "giay di duong": "GIẤY ĐI ĐƯỜNG",
    "giay nghi phep": "GIẤY NGHỈ PHÉP",
    "noi quy": "NỘI QUY",
    "phieu": "PHIẾU",
    "phieu gui": "PHIẾU GỬI",
    "phieu chuyen": "PHIẾU CHUYỂN",
    "phieu bao": "PHIẾU BÁO",
    "phat bieu": "PHÁT BIỂU",
    "quy che": "QUY CHẾ",
    "thong tri": "THÔNG TRI",
    "hop dong": "HỢP ĐỒNG",
    "ban ghi nho": "BẢN GHI NHỚ",
    "ban thoa thuan": "BẢN THỎA THUẬN",
    "giay uy quyen": "GIẤY ỦY QUYỀN",
    "thu cong": "THƯ CÔNG",
    "tuyen bo": "TUYÊN BỐ",
    "loi keu goi": "LỜI KÊU GỌI",
    CONG_VAN_NORM: "CÔNG VĂN",
}
DOC_TYPE_PREFIXES = sorted(KNOWN_DOC_TYPES, key=len, reverse=True)


def strip_accents(text: str) -> str:
    normalized = unicodedata.normalize("NFD", (text or "").replace("đ", "d").replace("Đ", "D"))
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")


def norm(text: str) -> str:
    return " ".join(strip_accents(text or "").lower().split())


def line_text(line: dict) -> str:
    return ((line or {}).get("text") or "").strip()


def uppercase_ratio(text: str) -> float:
    letters = [char for char in (text or "") if char.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for char in letters if char.isupper()) / len(letters)


def looks_like_doc_number_line(text: str) -> bool:
    number_text, symbol_text = split_doc_number_line(text)
    return bool(number_text or symbol_text)


def split_doc_number_line(text: str) -> tuple[str | None, str | None]:
    match = DOC_NUMBER_PREFIX_RE.match(text or "")
    if not match:
        return None, None
    tail = (match.group(1) or "").strip()
    if not tail or not re.match(r"^(\d|[-–—/])", tail):
        return None, None

    year_match = re.match(r"^(\d+\s*/\s*\d{4})\s*/\s*(.+)$", tail)
    if year_match:
        number_text = year_match.group(1)
        symbol_text = year_match.group(2)
    else:
        dash_match = re.match(r"^(\d+)\s*[-–—]\s*(.+)$", tail)
        slash_match = re.match(r"^(\d+)\s*/\s*(.+)$", tail)
        attached_match = re.match(r"^(\d+)([A-Za-zÀ-ỹĐđ].+)$", tail)
        symbol_only_match = re.match(r"^[-–—/]\s*(.+)$", tail)
        number_only_match = re.match(r"^(\d+)$", tail)
        if dash_match:
            number_text = dash_match.group(1)
            symbol_text = dash_match.group(2)
        elif slash_match:
            number_text = slash_match.group(1)
            symbol_text = slash_match.group(2)
        elif attached_match:
            number_text = attached_match.group(1)
            symbol_text = attached_match.group(2)
        elif symbol_only_match:
            number_text = None
            symbol_text = symbol_only_match.group(1)
        elif number_only_match:
            number_text = number_only_match.group(1)
            symbol_text = None
        else:
            return None, None
    return (
        number_text.strip() if number_text else None,
        symbol_text.strip() if symbol_text else None,
    )


def doc_symbol_implies_cong_van(symbol_text: str | None) -> bool:
    candidate = norm(symbol_text or "")
    if not candidate:
        return False
    return bool(re.search(r"(^|[^a-z])cv([^a-z]|$)", candidate))


def subject_implies_cong_van(text: str) -> bool:
    candidate = norm(text)
    return any(candidate.startswith(prefix) for prefix in DISPATCH_SUBJECT_PREFIXES)


def looks_like_body_start(text: str) -> bool:
    candidate = norm(text)
    return any(candidate.startswith(prefix) for prefix in BODY_PREFIXES)


def looks_like_date_line(text: str) -> bool:
    candidate = norm(text)
    return (
        "ngay" in candidate
        and any(ch.isdigit() for ch in text or "")
        and ("," in (text or "") or candidate.startswith("ngay "))
    )


def looks_like_explicit_doc_type_text(text: str) -> bool:
    candidate = norm(text)
    return candidate in KNOWN_DOC_TYPES


def extract_explicit_doc_type_from_text(text: str) -> tuple[str | None, str | None]:
    candidate = norm(text)
    if not candidate:
        return None, None
    for prefix in DOC_TYPE_PREFIXES:
        if candidate == prefix:
            return prefix, KNOWN_DOC_TYPES[prefix]
        if not candidate.startswith(prefix):
            continue
        remainder = candidate[len(prefix):]
        if not remainder:
            return prefix, KNOWN_DOC_TYPES[prefix]
        if remainder[0] in " :-–—/(":
            return prefix, KNOWN_DOC_TYPES[prefix]
    return None, None


def looks_like_party_title(text: str) -> bool:
    candidate = norm(text)
    return "viet nam" in candidate and "dang" in candidate and "cong san" in candidate


def looks_like_state_header(text: str) -> bool:
    candidate = norm(text)
    return (
        ("cong hoa xa hoi chu nghia viet nam" in candidate)
        or ("doc lap" in candidate and "tu do" in candidate and "hanh phuc" in candidate)
    )


def should_skip_between_title_lines(text: str) -> bool:
    return (
        not text
        or looks_like_date_line(text)
        or looks_like_party_title(text)
        or looks_like_state_header(text)
        or looks_like_doc_number_line(text)
    )


def should_stop_title_block(text: str) -> bool:
    raw = (text or "").strip()
    candidate = norm(raw)
    if not candidate:
        return False
    candidate = candidate.lstrip("*-• ").strip()
    if raw.startswith(("*", "-", "•")):
        return True
    if candidate.startswith("kinh gui") or candidate.startswith("noi nhan"):
        return True
    if looks_like_body_start(text):
        return True
    if re.match(r"^[a-z]\.\s", candidate):
        return True
    if re.match(r"^[0-9]+\.\s", candidate):
        return True
    return False


def should_skip_leading_dispatch_header_line(current_text: str, next_text: str | None) -> bool:
    candidate = norm(current_text)
    if not candidate:
        return False
    if subject_implies_cong_van(current_text) or extract_explicit_doc_type_from_text(current_text)[1]:
        return False
    if "doan tncs ho chi minh" in candidate:
        return True
    next_text = next_text or ""
    return (
        bool(next_text)
        and subject_implies_cong_van(next_text)
        and uppercase_ratio(current_text) >= 0.8
        and len((current_text or "").strip()) <= 60
    )


def find_contiguous_word_ids_for_text(words: list[dict], target_text: str) -> list[str]:
    target = " ".join((target_text or "").split()).strip()
    if not target or not words:
        return []
    target_compact = re.sub(r"\s+", "", target)
    for start in range(len(words)):
        for end in range(start, len(words)):
            joined = " ".join((word.get("text") or "").strip() for word in words[start:end + 1]).strip()
            if not joined:
                continue
            if joined == target or re.sub(r"\s+", "", joined) == target_compact:
                return [word.get("word_id") or word.get("id") for word in words[start:end + 1] if word.get("word_id") or word.get("id")]
    return []


def extract_explicit_doc_type_from_line(line: dict) -> dict | None:
    text = line_text(line)
    prefix, canonical = extract_explicit_doc_type_from_text(text)
    if not canonical:
        return None
    if norm(text) != prefix and uppercase_ratio(text) < 0.55:
        return None
    words = list(line.get("words") or [])
    word_ids = find_contiguous_word_ids_for_text(words, canonical)
    if not word_ids and line.get("word_ids") and norm(text) == prefix:
        word_ids = list(line.get("word_ids") or [])
    return {
        "prefix": prefix,
        "canonical_text": canonical,
        "line_id": line.get("line_id") or line.get("id"),
        "line_text": text,
        "word_ids": word_ids,
    }


def collect_title_block_lines(lines: list[dict], start_index: int, *, max_lines: int = 8) -> list[dict]:
    collected = []
    seen_ids = set()
    for index in range(start_index, len(lines)):
        line = lines[index]
        text = line_text(line)
        line_id = line.get("line_id") or line.get("id")
        if should_stop_title_block(text):
            break
        if should_skip_between_title_lines(text):
            continue
        if not text or text in {"*", "**", "***"}:
            continue
        if line_id in seen_ids:
            continue
        collected.append(line)
        seen_ids.add(line_id)
        if len(collected) >= max_lines:
            break
    return collected


def first_page(doc: dict) -> dict | None:
    return next((page for page in doc.get("pages", []) if page.get("page_index") == 0), None)


def _line_index_by_id(lines: list[dict]) -> dict[str, int]:
    return {
        (line.get("line_id") or line.get("id") or ""): index
        for index, line in enumerate(lines)
    }


def _annotation_fields_by_label(annotation: dict) -> dict[str, list[dict]]:
    buckets: dict[str, list[dict]] = {}
    for field in annotation.get("field_instances", []):
        label = field.get("label")
        if not label:
            continue
        buckets.setdefault(label, []).append(field)
    return buckets


def _field_text(field: dict | None) -> str:
    if not field:
        return ""
    return (field.get("normalized_value") or field.get("text") or "").strip()


def _subject_anchor_from_annotation(fields_by_label: dict[str, list[dict]]) -> tuple[str | None, str | None]:
    subject_field = next(iter(fields_by_label.get("DOC_SUBJECT", [])), None)
    if not subject_field:
        return None, None
    line_ids = list(subject_field.get("line_ids") or [])
    return (line_ids[0] if line_ids else None), _field_text(subject_field)


def _extract_page_layout_evidence(page: dict) -> dict:
    lines = sorted(page.get("lines", []), key=lambda item: item.get("order", 0))
    if not lines:
        return {
            "number_line_id": None,
            "number_line_index": None,
            "number_line_text": None,
            "doc_number_symbol_from_layout": None,
            "subject_line_ids": [],
            "subject_text": None,
            "subject_index": None,
            "subject_below_number_line": False,
            "explicit_doc_type_index": None,
            "has_addressee_line": False,
        }

    line_index = _line_index_by_id(lines)
    number_index = next(
        (index for index, line in enumerate(lines) if looks_like_doc_number_line(line_text(line))),
        None,
    )
    number_line = lines[number_index] if number_index is not None else None
    _, symbol_text = split_doc_number_line(line_text(number_line or {})) if number_line else (None, None)

    explicit_doc_type_line = None
    explicit_doc_type_index = None
    for index, line in enumerate(lines):
        text = line_text(line)
        if not text:
            continue
        if looks_like_party_title(text) or looks_like_state_header(text):
            continue
        if number_index is not None and index <= number_index:
            continue
        if number_index is not None and index - number_index > 8:
            break
        if extract_explicit_doc_type_from_line(line):
            explicit_doc_type_line = line
            explicit_doc_type_index = index
            break

    if explicit_doc_type_index is not None:
        subject_parts = collect_title_block_lines(lines, explicit_doc_type_index, max_lines=8)
    else:
        start_index = number_index + 1 if number_index is not None else 0
        subject_parts = [
            line
            for line in collect_title_block_lines(lines, start_index, max_lines=8)
            if not extract_explicit_doc_type_from_line(line)
        ]
        while subject_parts:
            next_text = line_text(subject_parts[1]) if len(subject_parts) > 1 else None
            if not should_skip_leading_dispatch_header_line(line_text(subject_parts[0]), next_text):
                break
            subject_parts = subject_parts[1:]

    subject_line_ids = [line.get("id") or line.get("line_id") for line in subject_parts if line.get("id") or line.get("line_id")]
    subject_text = "\n".join(line_text(line) for line in subject_parts).strip() or None
    subject_index = line_index.get(subject_line_ids[0]) if subject_line_ids else None
    return {
        "number_line_id": (number_line.get("line_id") or number_line.get("id")) if number_line else None,
        "number_line_index": number_index,
        "number_line_text": line_text(number_line or {}) or None,
        "doc_number_symbol_from_layout": symbol_text,
        "subject_line_ids": subject_line_ids,
        "subject_text": subject_text,
        "subject_index": subject_index,
        "subject_below_number_line": (
            number_index is not None
            and subject_index is not None
            and 0 < (subject_index - number_index) <= 6
        ),
        "explicit_doc_type_index": explicit_doc_type_index,
        "explicit_doc_type_text": line_text(explicit_doc_type_line) or None,
        "explicit_doc_type_line_id": (explicit_doc_type_line.get("line_id") or explicit_doc_type_line.get("id")) if explicit_doc_type_line else None,
        "explicit_doc_type_word_ids": list(explicit_doc_type_line.get("word_ids") or []) if explicit_doc_type_line else [],
        "has_addressee_line": any(norm(line_text(line)).startswith("kinh gui") for line in lines),
    }


def collect_doc_type_semantic_evidence(doc: dict, annotation: dict | None = None) -> dict:
    annotation = annotation or {"field_instances": [], "relations": []}
    fields_by_label = _annotation_fields_by_label(annotation)
    page = first_page(doc)
    layout = _extract_page_layout_evidence(page or {"lines": []})

    doc_number_symbol_field = next(iter(fields_by_label.get("DOC_NUMBER_SYMBOL", [])), None)
    doc_subject_field = next(iter(fields_by_label.get("DOC_SUBJECT", [])), None)
    doc_type_field = next(iter(fields_by_label.get("DOC_TYPE", [])), None)
    subject_line_id, subject_text_from_annotation = _subject_anchor_from_annotation(fields_by_label)

    subject_text = subject_text_from_annotation or layout["subject_text"] or ""
    doc_symbol_text = _field_text(doc_number_symbol_field) or (layout["doc_number_symbol_from_layout"] or "")

    if subject_line_id and layout["subject_index"] is None and page:
        index_map = _line_index_by_id(sorted(page.get("lines", []), key=lambda item: item.get("order", 0)))
        layout["subject_index"] = index_map.get(subject_line_id)
        if layout["number_line_index"] is not None and layout["subject_index"] is not None:
            layout["subject_below_number_line"] = 0 < (layout["subject_index"] - layout["number_line_index"]) <= 6

    explicit_doc_type = None
    if doc_type_field:
        explicit_doc_type = _field_text(doc_type_field)

    evidence = {
        "doc_type_present": bool(explicit_doc_type),
        "doc_type_text": explicit_doc_type,
        "doc_type_is_explicit_printed": bool(
            explicit_doc_type
            and norm(explicit_doc_type) == CONG_VAN_NORM
            and bool(doc_type_field.get("word_ids"))
        ) if doc_type_field else False,
        "explicit_doc_type_candidate_text": layout["explicit_doc_type_text"],
        "explicit_doc_type_candidate_line_id": layout["explicit_doc_type_line_id"],
        "explicit_doc_type_candidate_word_ids": layout["explicit_doc_type_word_ids"],
        "doc_symbol_text": doc_symbol_text or None,
        "doc_symbol_has_cv": doc_symbol_implies_cong_van(doc_symbol_text),
        "doc_subject_text": subject_text or None,
        "doc_subject_dispatch_prefix": subject_implies_cong_van(subject_text),
        "subject_below_number_line": layout["subject_below_number_line"],
        "has_doc_number_line": layout["number_line_index"] is not None,
        "number_line_text": layout["number_line_text"],
        "subject_line_ids": list(doc_subject_field.get("line_ids") or layout["subject_line_ids"]) if doc_subject_field else list(layout["subject_line_ids"]),
        "page_index": 0 if page else None,
    }
    evidence["explicit_doc_type_predicted"] = bool(layout["explicit_doc_type_text"])
    evidence["cong_van_reasons"] = []
    if evidence["doc_symbol_has_cv"]:
        evidence["cong_van_reasons"].append("doc_symbol_has_cv")
    if evidence["doc_subject_dispatch_prefix"] and evidence["subject_below_number_line"]:
        evidence["cong_van_reasons"].append("subject_prefix_below_number_line")
    evidence["cong_van_predicted"] = bool(
        evidence["doc_symbol_has_cv"]
        or (
            evidence["doc_subject_dispatch_prefix"]
            and evidence["subject_below_number_line"]
            and evidence["has_doc_number_line"]
        )
    )
    return evidence


def infer_semantic_doc_type_field(
    doc: dict,
    annotation: dict,
    *,
    model_name: str,
    existing_field_ids: set[str] | None = None,
) -> dict | None:
    fields_by_label = _annotation_fields_by_label(annotation)
    if fields_by_label.get("DOC_TYPE"):
        return None

    evidence = collect_doc_type_semantic_evidence(doc, annotation)
    next_index = 1
    existing_field_ids = existing_field_ids or set()
    while f"f{next_index}" in existing_field_ids:
        next_index += 1

    if evidence["explicit_doc_type_predicted"]:
        candidate_text = evidence.get("explicit_doc_type_candidate_text") or ""
        _, canonical_text = extract_explicit_doc_type_from_text(candidate_text)
        if canonical_text:
            return {
                "field_id": f"f{next_index}",
                "label": "DOC_TYPE",
                "page_index": evidence.get("page_index") or 0,
                "line_ids": [evidence["explicit_doc_type_candidate_line_id"]] if evidence.get("explicit_doc_type_candidate_line_id") else [],
                "word_ids": list(evidence.get("explicit_doc_type_candidate_word_ids") or []),
                "text": canonical_text,
                "normalized_value": normalize_value("DOC_TYPE", canonical_text),
                "confidence": 0.94,
                "source": model_name,
                "review_status": "predicted",
                "semantic_source": "deterministic_doc_type",
                "semantic_evidence": {
                    "rule": "explicit_uppercase_doc_type_line",
                    "ocr_text": candidate_text,
                },
            }

    if not evidence["cong_van_predicted"]:
        return None

    line_ids = list(evidence.get("subject_line_ids") or [])
    return {
        "field_id": f"f{next_index}",
        "label": "DOC_TYPE",
        "page_index": evidence.get("page_index") or 0,
        "line_ids": line_ids[:1],
        "word_ids": [],
        "text": "CÔNG VĂN",
        "normalized_value": normalize_value("DOC_TYPE", "CÔNG VĂN"),
        "confidence": 0.9,
        "source": model_name,
        "review_status": "predicted",
        "semantic_source": "deterministic_doc_type",
        "semantic_evidence": {
            "doc_symbol_text": evidence.get("doc_symbol_text"),
            "doc_subject_text": evidence.get("doc_subject_text"),
            "cong_van_reasons": evidence.get("cong_van_reasons"),
        },
    }
