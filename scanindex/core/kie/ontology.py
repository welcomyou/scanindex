"""Unified KIE ontology for Vietnamese official documents (v3)."""
from __future__ import annotations

import re


ONTOLOGY_ID = "kie_vi_official_v3"

DOC_TYPE_REFERENCE_CORE = [
    "NGHI QUYET",
    "QUYET DINH",
    "CHI THI",
    "KET LUAN",
    "QUY CHE",
    "QUY DINH",
    "THONG TRI",
    "HUONG DAN",
    "THONG BAO",
    "THONG CAO",
    "TUYEN BO",
    "LOI KEU GOI",
    "BAO CAO",
    "KE HOACH",
    "QUY HOACH",
    "CHUONG TRINH",
    "DE AN",
    "PHUONG AN",
    "DU AN",
    "TO TRINH",
    "CONG VAN",
    "BIEN BAN",
    "GIAY MOI",
    "GIAY GIOI THIEU",
    "GIAY CHUNG NHAN",
    "GIAY DI DUONG",
    "GIAY NGHI PHEP",
    "PHIEU GUI",
    "PHIEU CHUYEN",
    "PHIEU BAO",
    "THU CONG",
]
DOC_TYPE_REFERENCE_EXTENDED = [
    "SO YEU LY LICH",
    "LY LICH 2C",
    "HO SO CAN BO",
    "KE KHAI TAI SAN",
    "KE KHAI TAI SAN, THU NHAP",
    "DE CUONG",
    "DE CUONG CHI TIET",
    "PHAT BIEU",
    "NOI QUY",
    "HOP DONG",
    "BAN GHI NHO",
    "BAN THOA THUAN",
    "GIAY UY QUYEN",
]


FIELDS = [
    {
        "label": "REGIME_HEADER",
        "vi": "Header che do",
        "description": (
            "Top-right regime header block. Includes party header or state header OCR "
            "surface text exactly as shown on the page."
        ),
        "multi_line": True,
        "multi_instance": False,
    },
    {
        "label": "ISSUE_ORG_SUPERIOR",
        "vi": "Co quan cap tren",
        "description": "Direct superior issuing authority block.",
        "multi_line": True,
        "multi_instance": False,
    },
    {
        "label": "ISSUE_ORG_NAME",
        "vi": "Co quan ban hanh",
        "description": "Issuing authority block.",
        "multi_line": True,
        "multi_instance": False,
    },
    {
        "label": "DOC_NUMBER_SYMBOL",
        "vi": "So ky hieu van ban",
        "description": (
            "Anchored document number/symbol span. Keep full OCR surface, including "
            "prefix such as 'So:' when OCR has it. Allow wrapped OCR continuation lines."
        ),
        "multi_line": True,
        "multi_instance": False,
    },
    {
        "label": "PLACE_DATE",
        "vi": "Dia danh ngay thang",
        "description": "Place/date line of the document.",
        "multi_line": False,
        "multi_instance": False,
    },
    {
        "label": "DOC_SUBJECT",
        "vi": "Trich yeu",
        "description": "Main title/subject block of the document.",
        "multi_line": True,
        "multi_instance": False,
    },
    {
        "label": "ADDRESSEE",
        "vi": "Kinh gui",
        "description": (
            "Addressee block. Keep anchored OCR surface text, including 'Kinh gui:' "
            "or OCR variant when present."
        ),
        "multi_line": True,
        "multi_instance": False,
    },
    {
        "label": "RECIPIENTS",
        "vi": "Noi nhan",
        "description": (
            "Recipients/distribution block. Keep anchored OCR surface text, including "
            "'Noi nhan:' or OCR variant when present."
        ),
        "multi_line": True,
        "multi_instance": False,
    },
    {
        "label": "SIGNER_ROLE",
        "vi": "Vai tro nguoi ky",
        "description": (
            "Signer role block. Merge authority prefix and title into one field. Keep "
            "prefixes like TM./KT./TL./TUQ./Q. if OCR has them."
        ),
        "multi_line": True,
        "multi_instance": True,
    },
    {
        "label": "SIGNER_NAME",
        "vi": "Ten nguoi ky",
        "description": "Signer full name.",
        "multi_line": False,
        "multi_instance": True,
    },
    {
        "label": "URGENCY_MARK",
        "vi": "Muc do khan",
        "description": "Rule-based urgency mark.",
        "multi_line": False,
        "multi_instance": False,
        "train": False,
        "labeling": False,
    },
    {
        "label": "SECRECY_MARK",
        "vi": "Muc do mat",
        "description": "Rule-based secrecy mark.",
        "multi_line": False,
        "multi_instance": False,
        "train": False,
        "labeling": False,
    },
    {
        "label": "CIRCULATION_MARK",
        "vi": "Pham vi luu hanh",
        "description": "Rule-based circulation mark.",
        "multi_line": False,
        "multi_instance": False,
        "train": False,
        "labeling": False,
    },
    {
        "label": "DOC_TYPE",
        "vi": "Loai van ban",
        "description": "Deterministic output-only document type.",
        "multi_line": False,
        "multi_instance": False,
        "train": False,
        "labeling": False,
    },
]


RELATION_TYPES = [
    {
        "type": "signed_by",
        "description": "Link SIGNER_ROLE to SIGNER_NAME.",
    },
]


LABEL_SET = {item["label"] for item in FIELDS}
LABELING_FIELDS = [item for item in FIELDS if item.get("labeling", True)]
LABELING_LABEL_SET = {item["label"] for item in LABELING_FIELDS}
RELATION_TYPE_SET = {item["type"] for item in RELATION_TYPES}

RULE_BASED_LABELS = {"URGENCY_MARK", "SECRECY_MARK", "CIRCULATION_MARK"}
METADATA_ONLY_LABELS = {
    item["label"]
    for item in FIELDS
    if not item.get("train", True)
}
TRAINING_EXCLUDED_LABELS = RULE_BASED_LABELS | METADATA_ONLY_LABELS
CORE_LABELS = {item["label"] for item in FIELDS if item["label"] not in TRAINING_EXCLUDED_LABELS}
MULTI_LINE_LABELS = {item["label"] for item in FIELDS if item.get("multi_line")}
HYBRID_SEMANTIC_LABELS: set[str] = set()
BLOCK_LINE_LABELS = {
    "REGIME_HEADER",
    "ISSUE_ORG_SUPERIOR",
    "ISSUE_ORG_NAME",
    "DOC_NUMBER_SYMBOL",
    "DOC_SUBJECT",
    "ADDRESSEE",
    "RECIPIENTS",
    "SIGNER_ROLE",
}

PADDLE_COMBINED_LABELS: dict[frozenset[str], str] = {}
PADDLE_COMBINED_LABELS_REVERSE: dict[str, tuple[str, ...]] = {}

DOC_NUMBER_SYMBOL_PREFIX_RE = re.compile(
    r"^\s*(?:[A-Z0-9./-]{1,12}\s+){0,2}(?:S[O0Oo6\u00D3\u00D2\u00D4\u00D5\u1ED0\u1ED2\u1ED4\u1ED6\u1ED8\u1EDA\u1EDC\u1EDE\u1EE0\u1EE2\u00F3\u00F2\u00F4\u00F5\u1ED1\u1ED3\u1ED5\u1ED7\u1ED9\u1EDB\u1EDD\u1EDF\u1EE1\u1EE3]?|S\u1ed0)\s*[:.]?\s*",
    re.IGNORECASE,
)
ADDRESSEE_PREFIX_RE = re.compile(
    r"^\s*(?:-+\s*)?(?:K[iI\u00CD\u00CC\u0128\u1EC8\u1ECA]?nh\s+g(?:\u1eedi|ui|oi)|KINH\s+GUI)\s*:?\s*",
    re.IGNORECASE,
)
RECIPIENTS_PREFIX_RE = re.compile(
    r"^\s*(?:-+\s*)?(?:N[oO0\u00D3\u00D2\u00D4\u00D5\u1ED0\u1ED2\u1ED4\u1ED6\u1ED8]?i\s+nh(?:\u1eadn|an|an)|NOI\s+NHAN)\s*:?\s*",
    re.IGNORECASE,
)
SIGNER_ROLE_PREFIX_RE = re.compile(
    r"^\s*(?P<auth>(?:T/M|TM\.?|K/T|KT\.?|T/L|TL\.?|TUQ\.?|Q\.?))\s*(?P<rest>.*)$",
    re.IGNORECASE,
)
DOC_NUMBER_SYMBOL_PARSE_RE = re.compile(
    r"^(?:S[O0Oo6\u00D3\u00D2\u00D4\u00D5\u1ED0\u1ED2\u1ED4\u1ED6\u1ED8\u1EDA\u1EDC\u1EDE\u1EE0\u1EE2]?[:.]?\s*)?"
    r"(?P<num>\d+)"
    r"(?:\s*[-/]\s*|\s+)"
    r"(?:(?P<year>\d{4})\s*/\s*)?"
    r"(?P<sym>[A-Z\u0110/\-]+.*)$",
    re.IGNORECASE,
)


def ontology_lines() -> list[str]:
    return [f"- {field['label']}: {field['description']}" for field in FIELDS]


def relation_lines() -> list[str]:
    return [f"- {item['type']}: {item['description']}" for item in RELATION_TYPES]


def doc_type_reference_lines() -> list[str]:
    return [
        "DOC_TYPE hau xu ly tham khao nhom pho bien: " + ", ".join(DOC_TYPE_REFERENCE_CORE) + ".",
        "DOC_TYPE hau xu ly mo rong thuong gap: " + ", ".join(DOC_TYPE_REFERENCE_EXTENDED) + ".",
    ]


def strip_doc_number_symbol_prefix(text: str | None) -> str | None:
    raw = (text or "").strip()
    if not raw:
        return None
    value = DOC_NUMBER_SYMBOL_PREFIX_RE.sub("", raw, count=1)
    return value.strip() or raw


def strip_addressee_prefix(text: str | None) -> str | None:
    raw = (text or "").strip()
    if not raw:
        return None
    value = ADDRESSEE_PREFIX_RE.sub("", raw, count=1)
    return value.strip() or raw


def strip_recipients_prefix(text: str | None) -> str | None:
    raw = (text or "").strip()
    if not raw:
        return None
    value = RECIPIENTS_PREFIX_RE.sub("", raw, count=1)
    return value.strip() or raw


def split_signer_authority_title_text(text: str) -> tuple[str | None, str | None]:
    raw = " ".join((text or "").split())
    if not raw:
        return None, None
    match = SIGNER_ROLE_PREFIX_RE.match(raw)
    if not match:
        return None, raw or None
    authority = (match.group("auth") or "").strip() or None
    title = (match.group("rest") or "").strip() or None
    return authority, title


def strip_signer_role_prefix(text: str | None) -> str | None:
    raw = (text or "").strip()
    if not raw:
        return None
    _, title = split_signer_authority_title_text(raw)
    return title or raw


def classify_regime_header(text: str | None) -> str:
    raw = " ".join((text or "").upper().split())
    if not raw:
        return "other"
    if "DANG CONG SAN" in raw or "DẢNG CỘNG SẢN" in raw:
        return "party"
    if "CONG HOA XA HOI CHU NGHIA" in raw or "CỘNG HÒA XÃ HỘI CHỦ NGHĨA" in raw:
        return "state"
    return "other"


def parse_doc_number_symbol(text: str | None) -> dict[str, str | None]:
    raw = " ".join((text or "").split())
    if not raw:
        return {"full": None, "number": None, "year": None, "symbol": None}

    stripped = strip_doc_number_symbol_prefix(raw) or raw
    match = DOC_NUMBER_SYMBOL_PARSE_RE.match(raw) or DOC_NUMBER_SYMBOL_PARSE_RE.match(stripped)
    if not match:
        return {"full": stripped, "number": None, "year": None, "symbol": None}

    return {
        "full": stripped,
        "number": (match.group("num") or "").strip() or None,
        "year": (match.group("year") or "").strip() or None,
        "symbol": (match.group("sym") or "").strip() or None,
    }


def split_doc_number_symbol_text(text: str) -> tuple[str | None, str | None]:
    parsed = parse_doc_number_symbol(text)
    return parsed["number"], parsed["symbol"]


def normalize_value(label: str, text: str | None) -> str | None:
    raw = " ".join((text or "").split())
    if not raw:
        return None

    if label in {"URGENCY_MARK", "SECRECY_MARK", "CIRCULATION_MARK"}:
        return raw.upper()

    return raw


def paddle_export_label_for_fields(labels: set[str]) -> str:
    cleaned = {label for label in labels if label and label in LABEL_SET}
    cleaned -= TRAINING_EXCLUDED_LABELS
    if not cleaned:
        return "OTHER"
    if len(cleaned) == 1:
        return next(iter(cleaned))
    return "OTHER"


def annotation_output_schema(*, allowed_labels: set[str] | None = None) -> dict:
    label_enum = sorted(allowed_labels or LABEL_SET)
    return {
        "type": "object",
        "properties": {
            "field_instances": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "field_id": {"type": "string"},
                        "label": {"type": "string", "enum": label_enum},
                        "page_index": {"type": "integer", "minimum": 0},
                        "line_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "word_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "text": {"type": "string"},
                        "normalized_value": {
                            "anyOf": [
                                {"type": "string"},
                                {"type": "null"},
                            ]
                        },
                        "confidence": {
                            "anyOf": [
                                {"type": "number", "minimum": 0.0, "maximum": 1.0},
                                {"type": "null"},
                            ]
                        },
                    },
                    "required": ["field_id", "label", "page_index", "line_ids", "word_ids", "text"],
                    "additionalProperties": False,
                },
            },
            "relations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "relation_id": {"type": "string"},
                        "type": {"type": "string", "enum": sorted(RELATION_TYPE_SET)},
                        "from_field_id": {"type": "string"},
                        "to_field_id": {"type": "string"},
                    },
                    "required": ["relation_id", "type", "from_field_id", "to_field_id"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["field_instances", "relations"],
        "additionalProperties": False,
    }
