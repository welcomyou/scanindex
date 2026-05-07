from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

from kie_json_utils import upgrade_ocr_data_in_place
from train_kie.common import read_json, utc_now_iso
from train_kie.ontology import (
    BLOCK_LINE_LABELS,
    LABELING_FIELDS,
    LABELING_LABEL_SET,
    LABEL_SET,
    ONTOLOGY_ID,
    RELATION_TYPES,
    RELATION_TYPE_SET,
    annotation_output_schema,
    normalize_value,
    strip_doc_number_symbol_prefix,
)


LABEL_INPUT_SCHEMA = "kie_label_input_v3"
LABEL_RESULT_SCHEMA = "kie_label_result_v3"

SIGNATURE_KEYWORDS = [
    "k/t",
    "t/m",
    "t/l",
    "tl.",
    "tm.",
    "kt.",
    "tuq.",
    "nơi nhận",
    "noi nhan",
    "chánh văn phòng",
    "chủ tịch",
    "bí thư",
    "phó bí thư",
    "thứ trưởng",
    "bộ trưởng",
    "giám đốc",
]


def _strip_accents(text: str) -> str:
    normalized = unicodedata.normalize("NFD", (text or "").replace("đ", "d").replace("Đ", "D"))
    return "".join(char for char in normalized if unicodedata.category(char) != "Mn")


def _normalized_match_text(text: str) -> str:
    return " ".join(_strip_accents(text).lower().split())


SIGNATURE_MARKER_KEYWORDS = [
    "k/t",
    "t/m",
    "t/l",
    "tl.",
    "tm.",
    "kt.",
    "tuq.",
]

RECIPIENT_KEYWORDS = [
    "noi nhan",
    "kinh gui",
]

SIGNER_ROLE_KEYWORDS = [
    "chanh van phong",
    "chu tich",
    "pho chu tich",
    "bi thu",
    "pho bi thu",
    "thu truong",
    "bo truong",
    "giam doc",
    "pho giam doc",
    "tong giam doc",
    "truong ban",
    "pho truong ban",
    "truong phong",
    "pho truong phong",
    "chu tri",
    "thu ky",
    "nguoi ky",
]


def _is_probably_appendix_page(page: dict) -> bool:
    lines = page.get("lines") or []
    if not lines:
        return False
    first_line = lines[0].get("text", "")
    return "phu luc" in _normalized_match_text(first_line)


def _contains_keyword(normalized_text: str, keyword: str) -> bool:
    return keyword in normalized_text


def _fuzzy_signator_match(signator_name: str, text: str) -> str | None:
    """Check if signator name appears in text via accent-stripped matching."""
    n = _normalized_match_text(signator_name)
    t = _normalized_match_text(text)
    if not n:
        return None
    if n in t:
        return "exact"
    parts = n.split()
    if len(parts) >= 2:
        last2 = " ".join(parts[-2:])
        if last2 in t:
            return "partial"
    if parts and len(parts[-1]) > 2 and parts[-1] in t:
        return "partial"
    return None


def _page_selection_candidate(page: dict, total_pages: int) -> dict:
    page_index = page.get("page_index", 0)
    lines = page.get("lines") or []
    normalized_text = _normalized_match_text("\n".join(line.get("text", "") for line in lines))
    is_appendix = _is_probably_appendix_page(page)

    matched_keywords = []
    score = 0

    for keyword in SIGNATURE_MARKER_KEYWORDS:
        if _contains_keyword(normalized_text, keyword):
            matched_keywords.append(keyword)
            score += 5
    for keyword in RECIPIENT_KEYWORDS:
        if _contains_keyword(normalized_text, keyword):
            matched_keywords.append(keyword)
            score += 3
    for keyword in SIGNER_ROLE_KEYWORDS:
        if _contains_keyword(normalized_text, keyword):
            matched_keywords.append(keyword)
            score += 2

    if score > 0 and total_pages > 2 and page_index >= total_pages - 2:
        score += 1

    if is_appendix:
        score -= 4

    return {
        "page_index": page_index,
        "score": score,
        "matched_keywords": matched_keywords,
        "is_appendix": is_appendix,
        "line_count": len(lines),
    }


def _last_non_appendix_page_index(pages: list[dict]) -> int | None:
    for page in reversed(pages):
        if not _is_probably_appendix_page(page):
            return page.get("page_index", 0)
    if not pages:
        return None
    return pages[-1].get("page_index", 0)


def analyze_page_selection(doc: dict) -> dict:
    pages = doc.get("pages", [])
    if not pages:
        return {
            "selected_pages": [],
            "primary_page": None,
            "signature_page": None,
            "strategy": "no_pages",
            "confidence": "low",
            "needs_review": True,
            "review_reasons": ["document has no pages"],
            "candidates": [],
        }

    page_indices = [page.get("page_index", idx) for idx, page in enumerate(pages)]
    last_non_appendix_page = _last_non_appendix_page_index(pages)
    selected_pages = {page_indices[0]}

    if len(pages) == 1:
        return {
            "selected_pages": [page_indices[0]],
            "primary_page": page_indices[0],
            "signature_page": None,
            "strategy": "single_page_document",
            "confidence": "high",
            "needs_review": False,
            "review_reasons": [],
            "candidates": [],
        }

    if len(pages) <= 3:
        signature_page = last_non_appendix_page if last_non_appendix_page != page_indices[0] else None
        return {
            "selected_pages": sorted(page_indices),
            "primary_page": page_indices[0],
            "signature_page": signature_page,
            "strategy": "all_pages_small_document",
            "confidence": "high",
            "needs_review": False,
            "review_reasons": [],
            "candidates": [],
        }

    candidates = [
        _page_selection_candidate(page, total_pages=len(pages))
        for page in pages[1:]
    ]
    candidates.sort(key=lambda item: (item["score"], item["page_index"]), reverse=True)

    if last_non_appendix_page is not None:
        selected_pages.add(last_non_appendix_page)

    best_keyword_candidate = next(
        (item for item in candidates if item["score"] > 0 and not item["is_appendix"]),
        None,
    )
    if best_keyword_candidate is not None:
        selected_pages.add(best_keyword_candidate["page_index"])
        confidence = "high" if best_keyword_candidate["score"] >= 5 else "medium"
        review_reasons = []
        if confidence != "high":
            review_reasons.append("signature page selected from weak keyword evidence")
        return {
            "selected_pages": sorted(selected_pages),
            "primary_page": page_indices[0],
            "signature_page": best_keyword_candidate["page_index"],
            "strategy": "first_last_plus_keyword_match",
            "confidence": confidence,
            "needs_review": bool(review_reasons),
            "review_reasons": review_reasons,
            "candidates": candidates,
        }

    fallback_page = last_non_appendix_page
    if fallback_page is None:
        fallback_page = page_indices[-1]

    selected_pages.add(fallback_page)
    return {
        "selected_pages": sorted(selected_pages),
        "primary_page": page_indices[0],
        "signature_page": fallback_page if fallback_page != page_indices[0] else None,
        "strategy": "first_last_non_appendix",
        "confidence": "medium",
        "needs_review": False,
        "review_reasons": [],
        "candidates": candidates,
    }


# Backward compat alias
analyze_page_selection_for_hd36 = analyze_page_selection


def boost_page_selection_with_signator(
    page_selection: dict, signator_name: str, doc: dict,
) -> dict:
    """Boost confidence one level if signator name found on the chosen signature page."""
    if not signator_name or not page_selection.get("needs_review"):
        return page_selection
    confidence = page_selection.get("confidence")
    if confidence == "high":
        return page_selection
    sig_page_idx = page_selection.get("signature_page")
    if sig_page_idx is None:
        return page_selection
    for page in doc.get("pages", []):
        if page.get("page_index") != sig_page_idx:
            continue
        text = " ".join(line.get("text", "") for line in page.get("lines", []))
        match = _fuzzy_signator_match(signator_name, text)
        if match:
            if confidence == "medium":
                page_selection["confidence"] = "high"
            elif confidence == "low":
                page_selection["confidence"] = "medium"
            page_selection["needs_review"] = False
            page_selection["review_reasons"] = []
            page_selection["signator_boost"] = {
                "signator_name": signator_name,
                "match_type": match,
                "matched_on_page": sig_page_idx,
            }
        break
    return page_selection


def _line_payload(line, words_by_id):
    return {
        "line_id": line["id"],
        "text": line.get("text", ""),
        "bbox": line.get("bbox", []),
        "word_ids": list(line.get("word_ids") or []),
        "words": [
            {
                "word_id": word["id"],
                "text": word.get("text", ""),
                "bbox": word.get("bbox", []),
            }
            for word_id in line.get("word_ids", [])
            for word in [words_by_id.get(word_id)]
            if word
        ],
    }


def select_pages(doc: dict) -> list[int]:
    return analyze_page_selection(doc)["selected_pages"]


# Backward compat alias
select_pages_for_hd36 = select_pages


def serialize_pages_for_prompt(doc: dict, page_indices: list[int]) -> list[dict]:
    payload = []
    for page in doc.get("pages", []):
        if page.get("page_index") not in page_indices:
            continue
        words_by_id = {word["id"]: word for word in page.get("words", [])}
        payload.append({
            "page_index": page["page_index"],
            "width": page.get("width"),
            "height": page.get("height"),
            "lines": [
                _line_payload(line, words_by_id)
                for line in sorted(page.get("lines", []), key=lambda item: item.get("order", 0))
            ],
        })
    return payload


def build_label_task(doc: dict, *, doc_id: str, relative_pdf_path: str, source_canonical_json: str, signator_name: str | None = None) -> dict:
    page_selection = analyze_page_selection(doc)
    if signator_name:
        boost_page_selection_with_signator(page_selection, signator_name, doc)
    selected_pages = page_selection["selected_pages"]
    return {
        "task_schema": LABEL_INPUT_SCHEMA,
        "ontology_id": ONTOLOGY_ID,
        "doc_id": doc_id,
        "relative_pdf_path": relative_pdf_path,
        "source_canonical_json": str(Path(source_canonical_json).resolve()),
        "selected_pages": selected_pages,
        "page_selection": page_selection,
        "field_definitions": LABELING_FIELDS,
        "relation_types": RELATION_TYPES,
        "instructions": labeling_instructions(),
        "output_contract": {
            "task_schema": LABEL_RESULT_SCHEMA,
            "accepted_shapes": [
                {
                    "field_instances": [],
                    "relations": [],
                },
                {
                    "annotation": {
                        "field_instances": [],
                        "relations": [],
                    }
                },
            ],
            "json_schema": annotation_output_schema(allowed_labels=LABELING_LABEL_SET),
        },
        "pages": serialize_pages_for_prompt(doc, selected_pages),
    }


def build_label_task_from_file(canonical_json_path: str, *, doc_id: str, relative_pdf_path: str, signator_name: str | None = None) -> dict:
    doc = read_json(canonical_json_path)
    if not doc:
        raise FileNotFoundError(f"Canonical JSON not found: {canonical_json_path}")
    doc = upgrade_ocr_data_in_place(doc)
    return build_label_task(
        doc,
        doc_id=doc_id,
        relative_pdf_path=relative_pdf_path,
        source_canonical_json=canonical_json_path,
        signator_name=signator_name,
    )
def build_labeling_readme() -> str:
    guide = [
        "# Huong dan gan nhan local",
        "",
        "Workflow chinh:",
        "1. Mo tung file trong `json_input/` hoac trong cac batch con ben duoi no.",
        "2. Doc `pages`, `field_definitions`, `relation_types`, `instructions`, `output_contract` trong file do.",
        "3. Ghi dung mot file JSON ket qua cuoi cung vao batch tuong ung trong `json_output_labeled/` voi cung ten file input.",
        "",
        "Dang output chap nhan:",
        "```json",
        "{",
        '  "field_instances": [],',
        '  "relations": []',
        "}",
        "```",
        "",
        "hoac:",
        "```json",
        "{",
        '  "annotation": {',
        '    "field_instances": [],',
        '    "relations": []',
        "  }",
        "}",
        "```",
        "",
        "## Prompt Gan Nhan",
        creator_prompt_template(),
    ]
    return "\n".join(guide) + "\n"


def _collect_doc_indexes(doc: dict):
    line_map = {}
    word_map = {}
    line_order = {}
    word_order = {}

    for page in doc.get("pages", []):
        page_index = page["page_index"]
        for line in page.get("lines", []):
            line_map[line["id"]] = line
            line_order[line["id"]] = (page_index, line.get("order", 0))
        for word in page.get("words", []):
            word_map[word["id"]] = word
            word_order[word["id"]] = (page_index, word.get("line_id", ""), word.get("order", 0))

    return line_map, word_map, line_order, word_order


def _ordered_unique(values: list[str], order_map: dict[str, tuple]) -> list[str]:
    seen = set()
    ordered = []
    for value in sorted(values, key=lambda item: order_map.get(item, (10**9, item))):
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def _join_words(word_ids: list[str], word_map: dict[str, dict], word_order: dict[str, tuple]) -> str:
    parts = []
    for word_id in _ordered_unique(word_ids, word_order):
        word = word_map[word_id]
        parts.append(word.get("text", ""))
        if word.get("has_space_after", True):
            parts.append(" ")
    return "".join(parts).strip()


def _join_lines(line_ids: list[str], line_map: dict[str, dict], line_order: dict[str, tuple]) -> str:
    return "\n".join(
        line_map[line_id].get("text", "").strip()
        for line_id in _ordered_unique(line_ids, line_order)
    ).strip()


def _extract_annotation_payload(payload: dict) -> dict:
    annotation = payload.get("annotation")
    if isinstance(annotation, dict):
        return annotation
    return payload


def load_external_label_output(output_json_path: str, canonical_json_path: str, llm_name: str) -> dict:
    output_path = Path(output_json_path)
    payload = read_json(output_path)
    if not payload:
        raise FileNotFoundError(f"Label output not found: {output_path}")

    doc = read_json(canonical_json_path)
    if not doc:
        raise FileNotFoundError(f"Canonical JSON not found: {canonical_json_path}")
    doc = upgrade_ocr_data_in_place(doc)

    selected_pages = payload.get("selected_pages") or select_pages_for_hd36(doc)
    annotation = normalize_label_output(payload, doc, llm_name)
    return {
        "task_schema": LABEL_RESULT_SCHEMA,
        "created_at": utc_now_iso(),
        "model": llm_name,
        "source_canonical_json": str(Path(canonical_json_path).resolve()),
        "source_output_json": str(output_path.resolve()),
        "selected_pages": selected_pages,
        "annotation": annotation,
    }


# ---------------------------------------------------------------------------
# Ontology v3 labeling rules, prompt, and validator
# ---------------------------------------------------------------------------


def labeling_instructions() -> list[str]:
    return [
        "Keep `text` exactly as OCR surface text. Do not invent missing prefixes or rewrite OCR surface text.",
        "Truoc khi audit/label mot batch, bat buoc doc `train_kie/AUDIT_GUIDE.md`, `train_kie/README.md`, `train_kie/ontology.py`, `train_kie/labeling_workspace.py` theo dung thu tu do.",
        "Chi gan nhan cho cac trang co trong file json_input.",
        "Ontology v3 chi dung 10 train labels: REGIME_HEADER, ISSUE_ORG_SUPERIOR, ISSUE_ORG_NAME, DOC_NUMBER_SYMBOL, PLACE_DATE, DOC_SUBJECT, ADDRESSEE, RECIPIENTS, SIGNER_ROLE, SIGNER_NAME.",
        "Ba field rule-based khong can label bang tay: URGENCY_MARK, SECRECY_MARK, CIRCULATION_MARK.",
        "DOC_TYPE khong phai field label. Khong gan DOC_TYPE trong output.",
        "Rule chung: label tron anchored OCR span day du. Neu OCR co prefix/anchor thi giu nguyen; neu OCR khong co thi khong tu them.",
        "DOC_NUMBER_SYMBOL la mot field duy nhat cho ca cum so va ky hieu van ban; include `So:` neu OCR co.",
        "ADDRESSEE include `Kinh gui:` neu OCR co. RECIPIENTS include `Noi nhan:` neu OCR co.",
        "SIGNER_ROLE la mot field duy nhat, merge authority prefix va signer title. Giu cac prefix nhu TM., KT., TL., TUQ., Q. neu OCR co.",
        "REGIME_HEADER la toan bo khoi header goc phai phia tren.",
        "Khong bua line_id, word_id, page_index.",
        "Neu field map chinh xac vao OCR words thi phai dien word_ids. Chi de word_ids rong khi OCR word dinh khong tach chinh xac duoc.",
        "Neu OCR word dinh, van phai dien line_ids, text, normalized_value neu co the.",
        "Quan he chi dung `signed_by`: SIGNER_ROLE -> SIGNER_NAME.",
        "Neu khong chac thi giam confidence thay vi bua field.",
        "DOC_SUBJECT la toan bo block tieu de/trich yeu/chu de chinh. Khong lay nham body, can cu, kinh gui, noi nhan, so ky hieu, ngay thang.",
        "PLACE_DATE la dong ngay cua tai lieu; co the kem dia danh.",
        "Neu OCR surface la '-Kinh gui' hoac '-Noi nhan' thi giu nguyen surface text do trong label.",
        "Trong labeled JSON, text cua field phai giu cau truc dong that bang ky tu newline `\\n`; khong dung dau `|` lam separator nhan tao.",
        "Voi tai lieu khong theo mau chuan, chi gan cac field xuat hien ro. Khong ep du field theo mau HD36/ND30.",
    ]


def creator_prompt_template() -> str:
    return """Ban la bo gan nhan KIE cuoi cung cho tai lieu tieng Viet theo ontology v3.

Input:
- 1 file trong `json_input/` hoac mot batch con ben duoi no.

Nhiem vu:
- Doc dung `pages`, `field_definitions`, `relation_types`, `instructions`, `output_contract`.
- Tao annotation cuoi cung cho file do.
- Khong giai thich, khong markdown, chi tra JSON.

Quy tac bat buoc:
- Truoc khi lam, phai doc `train_kie/AUDIT_GUIDE.md`, `train_kie/README.md`, `train_kie/ontology.py`, `train_kie/labeling_workspace.py`.
- Chi dung du lieu co trong file input.
- Khong bua line_id, word_id, page_index.
- Ontology v3 chi dung 10 train labels:
  REGIME_HEADER, ISSUE_ORG_SUPERIOR, ISSUE_ORG_NAME, DOC_NUMBER_SYMBOL, PLACE_DATE,
  DOC_SUBJECT, ADDRESSEE, RECIPIENTS, SIGNER_ROLE, SIGNER_NAME.
- Khong gan `DOC_TYPE` trong output label.
- Khong gan 3 mark rule-based bang tay: URGENCY_MARK, SECRECY_MARK, CIRCULATION_MARK.
- Rule chung: label tron anchored OCR span day du. Khong skip prefix neu OCR co. Khong tu them prefix neu OCR khong co.
- DOC_NUMBER_SYMBOL la mot field duy nhat, include `So:` neu OCR co.
- ADDRESSEE include `Kinh gui:` neu OCR co.
- RECIPIENTS include `Noi nhan:` neu OCR co.
- SIGNER_ROLE include authority prefix nhu `TM.`, `KT.`, `TL.`, `TUQ.`, `Q.` neu OCR co.
- REGIME_HEADER la toan bo khoi header goc phai tren cung.
- DOC_SUBJECT la toan bo block tieu de/trich yeu/chu de chinh cua tai lieu. Khong lay nham body, can cu, kinh gui, noi nhan, so ky hieu, ngay thang.
- PLACE_DATE la dong ngay cua tai lieu; co the kem dia danh.
- SIGNER_ROLE va SIGNER_NAME co the co nhieu cum ky. Tao nhieu field instances neu nhin thay ro, va noi chung bang relation `signed_by`.
- Neu OCR word bi dinh, cho phep `word_ids: []`, nhung van phai dien `line_ids`, `text`, `normalized_value` neu co the.
- Giu text OCR surface trong labeled JSON voi newline `\\n`; khong dung `|` lam separator nhan tao.
- Khong tu go lai text bang mat neu co the lay truc tiep tu `words[].text` hoac `lines[].text` trong `json_input`.
- Voi tai lieu khong theo mau chuan, chi gan cac field thuc su xuat hien ro trong OCR.
- Neu khong chac, giam confidence hoac bo field do.

Dau ra:
- Ghi file cung ten vao dung batch tuong ung trong `json_output_labeled/`.
- Vi du: input o `json_input/batch_0002/...` thi output phai o `json_output_labeled/batch_0002/...`.
- Moi file phai la JSON annotation hoan chinh.
"""


# ---------------------------------------------------------------------------
# Public helpers: text rebuild and full-line detection
# ---------------------------------------------------------------------------

AUTOFILL_WHITELIST_NONBLOCK: set[str] = {"ISSUE_ORG_NAME", "SIGNER_NAME", "PLACE_DATE"}


def _norm_ws(text: str | None) -> str:
    return " ".join((text or "").split())


def _full_line_text(line: dict) -> str:
    """Return OCR surface text for a line, preferring raw DLL output."""
    ocr = (line.get("ocr_text") or "").strip()
    if ocr:
        return ocr
    return (line.get("text") or "").strip()


def rebuild_text_from_words(word_ids: list[str], word_map: dict[str, dict], word_order: dict[str, tuple]) -> str:
    """Public helper: rebuild OCR surface text from a sequence of word ids.

    Uses word.ocr_text (raw DLL) when available, respects has_space_after.
    """
    parts: list[str] = []
    for word_id in _ordered_unique(word_ids, word_order):
        word = word_map.get(word_id)
        if not word:
            continue
        parts.append(word.get("ocr_text") or word.get("text", ""))
        if word.get("has_space_after", True):
            parts.append(" ")
    return "".join(parts).strip()


def rebuild_text_from_lines(line_ids: list[str], line_map: dict[str, dict], line_order: dict[str, tuple]) -> str:
    """Public helper: rebuild multi-line text by joining line.ocr_text with newline."""
    return "\n".join(
        _full_line_text(line_map[line_id])
        for line_id in _ordered_unique(line_ids, line_order)
        if line_id in line_map
    ).strip()


def field_covers_full_line(word_ids: list[str], line_id: str, line_map: dict[str, dict]) -> bool:
    """Return True if the given word_ids cover every word of the given line."""
    line = line_map.get(line_id) or {}
    expected = set(line.get("word_ids") or [])
    if not expected:
        return False
    return expected.issubset(set(word_ids))


def _validate_doc_number_symbol_span(
    raw_id: str,
    line_ids: list[str],
    line_map: dict[str, dict],
    line_order: dict[str, tuple],
) -> None:
    if len(line_ids) <= 1:
        return

    orders = [line_order[line_id] for line_id in line_ids]
    page_indexes = {order[0] for order in orders}
    if len(page_indexes) != 1:
        raise ValueError(f"DOC_NUMBER_SYMBOL spans multiple pages for {raw_id}")

    sorted_orders = [order[1] for order in orders]
    expected = list(range(sorted_orders[0], sorted_orders[0] + len(sorted_orders)))
    if sorted_orders != expected:
        raise ValueError(f"DOC_NUMBER_SYMBOL line_ids must be contiguous for {raw_id}: {line_ids}")

    # Re-sort line_ids by VISUAL position (Y-bucket then X) for the
    # continuation check. Rationale: when a stamp/seal interrupts the
    # middle of the number line ("Số 07 -BC/TGSUBKT"), OCR sometimes
    # splits the row into two line-fragments AND assigns reading-order
    # in reverse (right fragment ends up with the lower order). Trusting
    # OCR's line.order then labels the leading "Số" as a continuation
    # and rejects it via the symbol-like regex even though the user's
    # label is semantically correct. Visual ordering recovers the
    # natural left-to-right / top-to-bottom sequence.
    def _line_topleft(_lid):
        bbox = line_map[_lid].get("bbox") or [0, 0, 0, 0]
        y_top, x_left = bbox[1], bbox[0]
        # Bucket Y by ~half the line's own height so same-row fragments
        # land together even when their tops differ by a few pt (a stamp
        # often pushes one fragment's bbox a couple of points up/down).
        # Floor of (y / bucket): same row → same bucket.
        height = max(1.0, bbox[3] - bbox[1])
        bucket = max(12.0, height * 0.6)
        return (int(y_top // bucket), x_left)

    visual_order_ids = sorted(line_ids, key=_line_topleft)

    forbidden_prefixes = ("ngay ", "thang ", "nam ", "tai ", "kinh gui", "noi nhan")
    for index, line_id in enumerate(visual_order_ids[1:], 1):
        text = (line_map[line_id].get("text") or "").strip()
        # Some scans insert a standalone separator line between the number line
        # and the symbol line. Keep the span contiguous but ignore that marker.
        if re.match(r"^[*._~]+$", text):
            continue
        lowered = " ".join(text.lower().split())
        if any(lowered.startswith(prefix) for prefix in forbidden_prefixes):
            raise ValueError(f"DOC_NUMBER_SYMBOL continuation line looks invalid for {raw_id}: {text}")
        # Allow ASCII letters/digits, Vietnamese diacritics, slash, hyphen,
        # parentheses, colon, comma, period, whitespace. \w with re.UNICODE
        # covers accented Vietnamese (e.g. "S\u1ed1") in a wrapped continuation.
        # Forbidden prefix check above still catches body lines.
        if not re.match(r"^[\w/\-().,:\s]+$", text, re.UNICODE):
            raise ValueError(f"DOC_NUMBER_SYMBOL continuation line is not symbol-like for {raw_id}: {text}")


ADDRESSEE_ANCHOR_RE = re.compile(
    r"k[iíì]?nh\s*g[uưứừự]i",
    re.IGNORECASE,
)
RECIPIENTS_ANCHOR_RE = re.compile(
    r"n[oơờớởỡợ]i\s*nh[aâấầẩẫậ]n",
    re.IGNORECASE,
)


def _normalized_contains_anchor(text: str, anchor_re: re.Pattern) -> bool:
    normalized = _norm_ws(_strip_accents(text).lower())
    # Anchors stripped of accents match plain "kinh gui" / "noi nhan"
    plain_addressee = re.search(r"kinh\s*gui", normalized)
    plain_recipients = re.search(r"noi\s*nhan", normalized)
    if anchor_re is ADDRESSEE_ANCHOR_RE:
        return plain_addressee is not None
    if anchor_re is RECIPIENTS_ANCHOR_RE:
        return plain_recipients is not None
    return bool(anchor_re.search(text))


def validate_label_output_detailed(
    output_payload: dict,
    canonical_doc: dict,
    llm_name: str,
) -> dict:
    """Validate + normalize a label output. Returns {errors, warnings, normalized}.

    Unlike ``normalize_label_output``, this never raises on annotation problems.
    ``errors`` are hard issues that make the payload unsafe to import.
    ``warnings`` are soft issues worth reviewing but not blocking.
    ``normalized`` is the canonicalised annotation if there are no errors,
    otherwise ``None``.
    """
    errors: list[str] = []
    warnings_list: list[dict] = []

    annotation = _extract_annotation_payload(output_payload)
    raw_fields = annotation.get("field_instances", []) or []
    raw_relations = annotation.get("relations", []) or []

    line_map, word_map, line_order, word_order = _collect_doc_indexes(canonical_doc)
    normalized_fields: list[dict] = []
    field_id_map: dict[str, str] = {}
    label_by_new_id: dict[str, str] = {}
    seen_raw_field_ids: set[str] = set()

    for index, raw_field in enumerate(raw_fields, 1):
        raw_id = raw_field.get("field_id") or f"f{index}"
        if raw_id in seen_raw_field_ids:
            errors.append(f"Duplicate field_id in label output: {raw_id}")
            continue
        seen_raw_field_ids.add(raw_id)

        label = (raw_field.get("label") or "").strip().upper()
        if label not in LABEL_SET:
            errors.append(f"Unknown field label: {label} (raw_id={raw_id})")
            continue
        if label in {"DOC_TYPE", "URGENCY_MARK", "SECRECY_MARK", "CIRCULATION_MARK"}:
            continue

        line_ids = list(raw_field.get("line_ids") or [])
        word_ids = list(raw_field.get("word_ids") or [])

        invalid_line_ids = [line_id for line_id in line_ids if line_id not in line_map]
        if invalid_line_ids:
            errors.append(f"Unknown line_ids for {raw_id}: {invalid_line_ids}")
            continue

        invalid_word_ids = [word_id for word_id in word_ids if word_id not in word_map]
        if invalid_word_ids:
            errors.append(f"Unknown word_ids for {raw_id}: {invalid_word_ids}")
            continue

        field_had_word_ids = bool(word_ids)

        # Auto-fill line_ids from word_ids (existing behavior).
        #
        # DOC_NUMBER_SYMBOL is a special case: some OCR outputs split a single
        # visual doc-number row into non-contiguous line ids (for example
        # "Số: 10" and "-BB/ĐU"). When the annotator provides precise
        # word_ids-only selection, keep that form and do not synthesize a
        # misleading multi-line span that later fails contiguous validation.
        if word_ids and not line_ids:
            if label == "DOC_NUMBER_SYMBOL":
                line_ids = []
            else:
                line_ids = [word_map[word_id].get("line_id") for word_id in word_ids if word_map[word_id].get("line_id")]

        # Auto-fill word_ids from line_ids under policy (b+):
        # - Block labels: always auto-fill.
        # - Whitelisted non-block (ISSUE_ORG_NAME, SIGNER_NAME, PLACE_DATE):
        #     only fill if field.text is empty or matches full-line OCR text.
        # - Other non-block labels: reject as AMBIGUOUS_LINE_ONLY_FIELD.
        if line_ids and not word_ids:
            raw_text = (raw_field.get("text") or "").strip()
            if label in BLOCK_LINE_LABELS:
                word_ids = [
                    wid
                    for lid in line_ids
                    for wid in (line_map[lid].get("word_ids") or [])
                ]
            elif label in AUTOFILL_WHITELIST_NONBLOCK:
                full_line = "\n".join(_full_line_text(line_map[lid]) for lid in line_ids if lid in line_map).strip()
                if raw_text == "" or _norm_ws(raw_text) == _norm_ws(full_line):
                    word_ids = [
                        wid
                        for lid in line_ids
                        for wid in (line_map[lid].get("word_ids") or [])
                    ]
                else:
                    errors.append(
                        f"AMBIGUOUS_LINE_ONLY_FIELD for {raw_id} label={label}: "
                        f"line_ids set but word_ids empty and text does not match full-line OCR text"
                    )
                    continue
            else:
                errors.append(
                    f"AMBIGUOUS_LINE_ONLY_FIELD for {raw_id} label={label}: "
                    f"non-whitelist label cannot be auto-filled from line_ids alone"
                )
                continue

        line_ids = _ordered_unique(line_ids, line_order)
        word_ids = _ordered_unique(word_ids, word_order)

        if not line_ids and not word_ids:
            errors.append(f"Field {raw_id} must contain at least one line_id or word_id")
            continue

        if label == "DOC_NUMBER_SYMBOL":
            try:
                _validate_doc_number_symbol_span(raw_id, line_ids, line_map, line_order)
            except ValueError as exc:
                errors.append(str(exc))
                continue

        if word_ids and line_ids:
            invalid_word_links = [
                word_id
                for word_id in word_ids
                if word_map[word_id].get("line_id") not in set(line_ids)
            ]
            if invalid_word_links:
                errors.append(f"word_ids do not belong to line_ids for {raw_id}: {invalid_word_links}")
                continue

        page_candidates: set[int] = set()
        for line_id in line_ids:
            page_candidates.add(line_order[line_id][0])
        for word_id in word_ids:
            page_candidates.add(word_order[word_id][0])
        if len(page_candidates) > 1:
            errors.append(f"Field {raw_id} spans multiple pages: {sorted(page_candidates)}")
            continue

        inferred_page_index = next(iter(page_candidates)) if page_candidates else None
        page_index = raw_field.get("page_index", inferred_page_index)
        if page_index is None:
            errors.append(f"Could not infer page_index for field {raw_id}")
            continue
        if inferred_page_index is not None and page_index != inferred_page_index:
            errors.append(f"page_index mismatch for field {raw_id}: {page_index} vs {inferred_page_index}")
            continue

        canonical_word_text = _join_words(word_ids, word_map, word_order) if word_ids else ""
        canonical_line_text = _join_lines(line_ids, line_map, line_order) if line_ids else ""

        text = (raw_field.get("text") or "").strip()
        if label in BLOCK_LINE_LABELS and line_ids and not field_had_word_ids:
            text = canonical_line_text
        elif not text:
            text = canonical_word_text or canonical_line_text
        if not text:
            errors.append(f"Field {raw_id} has empty text")
            continue

        normalized_value = raw_field.get("normalized_value")
        if normalized_value is None:
            normalized_value = normalize_value(label, text)

        confidence = raw_field.get("confidence")
        if confidence is not None:
            try:
                confidence = float(confidence)
            except (TypeError, ValueError):
                errors.append(f"confidence not numeric for field {raw_id}: {confidence}")
                continue
            if not 0.0 <= confidence <= 1.0:
                errors.append(f"confidence out of range for field {raw_id}: {confidence}")
                continue

        # Warning: ADDRESSEE/RECIPIENTS lost anchor that OCR line contains.
        if label in {"ADDRESSEE", "RECIPIENTS"} and line_ids:
            anchor_re = ADDRESSEE_ANCHOR_RE if label == "ADDRESSEE" else RECIPIENTS_ANCHOR_RE
            ocr_line_text = "\n".join(_full_line_text(line_map[lid]) for lid in line_ids if lid in line_map)
            if _normalized_contains_anchor(ocr_line_text, anchor_re) and not _normalized_contains_anchor(text, anchor_re):
                anchor_label = "Kính gửi" if label == "ADDRESSEE" else "Nơi nhận"
                warnings_list.append({
                    "code": "MISSING_ANCHOR",
                    "field_id": raw_id,
                    "label": label,
                    "message": f"{label} line contains '{anchor_label}' in OCR but field text does not include it",
                })

        new_field_id = f"f{index}"
        field_id_map[raw_id] = new_field_id
        label_by_new_id[new_field_id] = label
        normalized_fields.append({
            "field_id": new_field_id,
            "label": label,
            "page_index": page_index,
            "line_ids": line_ids,
            "word_ids": word_ids,
            "text": text,
            "normalized_value": normalized_value,
            "confidence": confidence,
            "source": llm_name,
            "review_status": "draft",
        })

    # Warning: overlap >= 50% word_ids between two fields of different labels.
    for i, a in enumerate(normalized_fields):
        set_a = set(a["word_ids"])
        if not set_a:
            continue
        for b in normalized_fields[i + 1:]:
            if a["label"] == b["label"]:
                continue
            set_b = set(b["word_ids"])
            if not set_b:
                continue
            inter = set_a & set_b
            if not inter:
                continue
            ratio = len(inter) / min(len(set_a), len(set_b))
            if ratio >= 0.5:
                warnings_list.append({
                    "code": "FIELD_OVERLAP",
                    "field_id": a["field_id"],
                    "other_field_id": b["field_id"],
                    "labels": [a["label"], b["label"]],
                    "overlap_ratio": round(ratio, 2),
                    "message": f"{a['field_id']} ({a['label']}) and {b['field_id']} ({b['label']}) share {len(inter)} word_ids ({int(ratio*100)}%)",
                })

    normalized_relations: list[dict] = []
    for index, raw_relation in enumerate(raw_relations, 1):
        relation_type = (raw_relation.get("type") or "").strip()
        if relation_type not in RELATION_TYPE_SET:
            errors.append(f"Unknown relation type: {relation_type}")
            continue

        from_raw = raw_relation.get("from_field_id")
        to_raw = raw_relation.get("to_field_id")
        from_field_id = field_id_map.get(from_raw)
        to_field_id = field_id_map.get(to_raw)
        if not from_field_id or not to_field_id:
            errors.append(f"Relation references unknown field ids: {raw_relation}")
            continue

        # New rule: signed_by endpoint types must be SIGNER_ROLE -> SIGNER_NAME.
        if relation_type == "signed_by":
            from_label = label_by_new_id.get(from_field_id)
            to_label = label_by_new_id.get(to_field_id)
            if from_label != "SIGNER_ROLE" or to_label != "SIGNER_NAME":
                errors.append(
                    f"signed_by relation must link SIGNER_ROLE -> SIGNER_NAME, got {from_label} -> {to_label} "
                    f"(raw from={from_raw} to={to_raw})"
                )
                continue

        confidence = raw_relation.get("confidence")
        if confidence is not None:
            try:
                confidence = float(confidence)
            except (TypeError, ValueError):
                errors.append(f"confidence not numeric for relation {index}: {confidence}")
                continue
            if not 0.0 <= confidence <= 1.0:
                errors.append(f"confidence out of range for relation {index}: {confidence}")
                continue

        normalized_relations.append({
            "relation_id": f"r{index}",
            "type": relation_type,
            "from_field_id": from_field_id,
            "to_field_id": to_field_id,
            "confidence": confidence,
            "source": llm_name,
            "review_status": "draft",
        })

    normalized = None
    if not errors:
        normalized = {
            "schema": ONTOLOGY_ID,
            "field_instances": normalized_fields,
            "relations": normalized_relations,
        }

    return {
        "errors": errors,
        "warnings": warnings_list,
        "normalized": normalized,
    }


def normalize_label_output(output_payload: dict, canonical_doc: dict, llm_name: str) -> dict:
    result = validate_label_output_detailed(output_payload, canonical_doc, llm_name)
    if result["errors"]:
        raise ValueError("; ".join(result["errors"]))
    return result["normalized"]
