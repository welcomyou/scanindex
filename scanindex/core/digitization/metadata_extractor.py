"""
Document Metadata Extractor — Bóc tách thông tin văn bản hành chính.

Supports both Party documents (Đảng - HD 36/VPTW) and State documents (Nhà nước - NĐ 30).
Uses hybrid approach: DocLayout-YOLO regions (hint) + regex + position/style analysis
on OCR companion JSON data.
"""

import json
import re
import unicodedata
import os


# ---------------------------------------------------------------------------
# Accent removal (reused logic from correction_engine.py:140)
# ---------------------------------------------------------------------------

def _remove_accents(s):
    """Remove Vietnamese diacritics for fuzzy matching."""
    s = str(s)
    s = s.replace("Đ", "D").replace("đ", "d")
    s = unicodedata.normalize('NFD', s)
    s = "".join(c for c in s if unicodedata.category(c) != 'Mn')
    return s


# ---------------------------------------------------------------------------
# Document type detection keywords
# ---------------------------------------------------------------------------

_DANG_KEYWORDS = ["DANG CONG SAN VIET NAM", "DANG CONG SAN"]
_NHANUOC_KEYWORDS = [
    "CONG HOA XA HOI CHU NGHIA VIET NAM",
    "DOC LAP - TU DO - HANH PHUC",
    "DOC LAP-TU DO-HANH PHUC",
]

# Lines to exclude when looking for issuing authority
_EXCLUDE_LINES_DANG = _DANG_KEYWORDS
_EXCLUDE_LINES_NHANUOC = _NHANUOC_KEYWORDS + [
    "DOC LAP TU DO HANH PHUC",
]
_EXCLUDE_ALL = _EXCLUDE_LINES_DANG + _EXCLUDE_LINES_NHANUOC

# Prefixes that disqualify a line from being issuing authority
_COQAN_EXCLUDE_PREFIXES = ["KINH GUI", "KINH TRINH", "KINH MOI"]


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Document number - Party: "Số 127-QĐ/VPTW"
_SO_DANG = re.compile(
    r'S[oố]\s*[:\s]*(\d+\s*[\-–—]\s*[A-Za-zĐđÀ-ỹ]+\s*/\s*[A-Za-zĐđÀ-ỹ]+)',
    re.IGNORECASE,
)

# Document number - State: "Số: 123/QĐ-UBND" or "Số: 123/2024/QĐ-UBND"
_SO_NHANUOC = re.compile(
    r'S[oố]\s*[:\s]+(\d+\s*/\s*[\w\-–—/]+)',
    re.IGNORECASE,
)

# Document number - generic fallback
_SO_GENERIC = re.compile(
    r'S[oố]\s*[:\s]*(\d+[\s\-–—/]+[\w\-–—/]+)',
    re.IGNORECASE,
)

# Date: "ngày 03 tháng 04 năm 2018"
_DATE_PATTERN = re.compile(
    r'ng[aàáảãạ]y\s+(\d{1,2})\s+th[aáàảãạ]ng\s+(\d{1,2})\s+n[aăâ]m\s+(\d{4})',
    re.IGNORECASE,
)

# Date fallback (no diacritics at all)
_DATE_FALLBACK = re.compile(
    r'ngay\s+(\d{1,2})\s+thang\s+(\d{1,2})\s+nam\s+(\d{4})',
    re.IGNORECASE,
)

# Full date line with optional city prefix: "Hà Nội, ngày 03 tháng 04 năm 2018"
_DATE_LINE = re.compile(
    r'(.*?ng[aàáảãạ]y\s+\d{1,2}\s+th[aáàảãạ]ng\s+\d{1,2}\s+n[aăâ]m\s+\d{4})',
    re.IGNORECASE,
)

# V/v pattern (Công văn subject) — must NOT be preceded by a letter/digit (avoid CV/VPTU etc.)
_VV_PATTERN = re.compile(r'(?<![A-Za-zĐđÀ-ỹ0-9])V/v\s*[:\s]*(.+)', re.IGNORECASE)

# "Về việc..." subject line (docs without explicit V/v prefix)
_VE_PATTERN = re.compile(r'^V[eề]\s+(.+)', re.IGNORECASE)

# Stop tokens for trich_yeu continuation (marks start of body content)
_TRICH_YEU_STOP = re.compile(
    r'^(Kính\s*(gửi|trình|mời)|Căn\s*cứ|Thực\s*hiện|Theo\s*|'
    r'[IVXLC]+\s*[\.\-]|[0-9]+\s*[\.\-\:]\s*[A-ZĐÀÁẢÃẠ])',
    re.IGNORECASE,
)

# Document type names (centered title)
_DOC_TYPE_NAMES = re.compile(
    r'^\s*(NGHỊ QUYẾT|QUYẾT ĐỊNH|QUY ĐỊNH|CHỈ THỊ|KẾT LUẬN|'
    r'THÔNG BÁO|HƯỚNG DẪN|BÁO CÁO|KẾ HOẠCH|QUY CHẾ|'
    r'CHƯƠNG TRÌNH|CÔNG VĂN|TỜ TRÌNH|THÔNG TRI|'
    r'NGHỊ ĐỊNH|THÔNG TƯ)\s*$',
    re.IGNORECASE,
)

# Vietnamese personal name: 2-5 capitalized words
_NAME_PATTERN = re.compile(
    r'^[A-ZÀ-Ỹ][a-zà-ỹ]+(?:\s+[A-ZÀ-Ỹ][a-zà-ỹ]+){1,4}$'
)

# Title/position keywords to exclude when finding signer name
_SIGNER_TITLE_KEYWORDS = [
    # Party (HD 36)
    "T/M", "K/T", "T/L", "TM.", "KT.", "TL.",
    "BÍ THƯ", "PHÓ BÍ THƯ",
    "TRƯỞNG BAN", "PHÓ TRƯỞNG BAN",
    "CHÁNH VĂN PHÒNG", "PHÓ CHÁNH VĂN PHÒNG",
    # State (NĐ 30)
    "CHỦ TỊCH", "PHÓ CHỦ TỊCH",
    "GIÁM ĐỐC", "PHÓ GIÁM ĐỐC",
    "TỔNG GIÁM ĐỐC",
    "CỤC TRƯỞNG", "VỤ TRƯỞNG",
    "TRƯỞNG PHÒNG", "PHÓ TRƯỞNG PHÒNG",
    # Generic
    "Nơi nhận", "NƠI NHẬN",
]

# Document type abbreviation map
_TYPE_MAP = {
    # Party (HD 36)
    "NQ": "Nghị quyết", "QĐ": "Quyết định", "QĐi": "Quy định",
    "CT": "Chỉ thị", "KL": "Kết luận", "TB": "Thông báo",
    "HD": "Hướng dẫn", "BC": "Báo cáo", "CV": "Công văn",
    "KH": "Kế hoạch", "QC": "Quy chế", "CTr": "Chương trình",
    "TT": "Thông tri",
    # State (NĐ 30) additions
    "NĐ": "Nghị định", "TTr": "Tờ trình",
}


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _normalize_for_match(text):
    """Remove accents + uppercase for keyword matching."""
    return _remove_accents(text).upper().strip()


def _is_excluded_line(text):
    """Check if line text matches excluded keywords (quoc hieu, tieu ngu, kinh gui, etc.)."""
    norm = _normalize_for_match(text)
    norm_nospace = norm.replace(" ", "")
    for kw in _EXCLUDE_ALL:
        kw_nospace = kw.replace(" ", "")
        if kw_nospace in norm_nospace:
            return True
    # Exclude "Kính gửi/trình/mời:" lines (recipient/address lines)
    norm_stripped = norm.lstrip()
    for pfx in _COQAN_EXCLUDE_PREFIXES:
        if norm_stripped.startswith(pfx):
            return True
    return False


def _is_centered(line, page_width):
    """Check if a line is approximately centered on the page."""
    line_center = line["x"] + line["w"] / 2
    page_center = page_width / 2
    # Allow 15% deviation from center
    return abs(line_center - page_center) < page_width * 0.15


def _is_left_half(line, page_width):
    """Check if line center is in the left half of the page."""
    line_center = line["x"] + line["w"] / 2
    return line_center < page_width * 0.55  # slight tolerance


def _is_right_half(line, page_width):
    """Check if line center is in the right half of the page."""
    line_center = line["x"] + line["w"] / 2
    return line_center > page_width * 0.45  # slight tolerance


def _has_title_keyword(text):
    """Check if text contains signer title keywords."""
    text_upper = text.upper().strip()
    for kw in _SIGNER_TITLE_KEYWORDS:
        if kw.upper() in text_upper:
            return True
    return False


def _line_in_layout_region(line, regions, region_type):
    """Check if a line falls within a layout region of given type."""
    if not regions:
        return False
    lx = line["x"]
    ly = line["y"]
    lx2 = lx + line["w"]
    ly2 = ly + line["h"]

    for r in regions:
        if r.get("type") != region_type:
            continue
        bbox = r.get("bbox_pdf") or r.get("bbox")
        if not bbox:
            continue
        rx0, ry0, rx1, ry1 = bbox
        # Check overlap
        if lx2 > rx0 and lx < rx1 and ly2 > ry0 and ly < ry1:
            return True
    return False


# ---------------------------------------------------------------------------
# Main extraction logic
# ---------------------------------------------------------------------------

def _detect_doc_type(lines):
    """
    Detect document type: 'dang' (Party) or 'nhanuoc' (State) or 'unknown'.
    Scans first page lines for distinctive keywords.
    """
    for line in lines:
        norm = _normalize_for_match(line["text"])
        norm_nospace = norm.replace(" ", "")
        for kw in _DANG_KEYWORDS:
            if kw.replace(" ", "") in norm_nospace:
                return "dang"
        for kw in _NHANUOC_KEYWORDS:
            if kw.replace(" ", "") in norm_nospace:
                return "nhanuoc"
    return "unknown"


def _extract_so_ky_hieu(lines, doc_type, page_height):
    """Extract document number/symbol."""
    # Choose pattern order based on doc_type
    if doc_type == "dang":
        patterns = [_SO_DANG, _SO_NHANUOC, _SO_GENERIC]
    elif doc_type == "nhanuoc":
        patterns = [_SO_NHANUOC, _SO_DANG, _SO_GENERIC]
    else:
        patterns = [_SO_DANG, _SO_NHANUOC, _SO_GENERIC]

    # Search in top 35% of page
    top_lines = [l for l in lines if l["y"] < page_height * 0.35]

    for pat in patterns:
        for line in top_lines:
            m = pat.search(line["text"])
            if m:
                raw = m.group(1).strip()
                # Clean up whitespace around separators
                raw = re.sub(r'\s*([/\-–—])\s*', r'\1', raw)
                return f"Số {raw}"
    return None


def _split_so_ky_hieu(so_ky_hieu):
    """Split combined document number/symbol into separate number and symbol fields."""
    if not so_ky_hieu:
        return None, None

    text = " ".join(str(so_ky_hieu).strip().split())
    m = re.search(r'([0-9]+)\s*[-–—]\s*([^\s].+?)\s*$', text)
    if m:
        return m.group(1).strip(), m.group(2).strip()

    m = re.search(r'([0-9]+)\s*$', text)
    if m:
        return m.group(1).strip(), None

    return None, None


def _extract_ngay_ban_hanh(lines, page_height):
    """Extract issuance date."""
    top_lines = [l for l in lines if l["y"] < page_height * 0.35]

    for line in top_lines:
        text = line["text"]
        m = _DATE_PATTERN.search(text)
        if m:
            # Return the full date line (with optional city prefix)
            m_full = _DATE_LINE.search(text)
            if m_full:
                return m_full.group(1).strip()
            return m.group(0).strip()

    # Fallback: no-accent matching
    for line in top_lines:
        text_no_accent = _remove_accents(line["text"])
        m = _DATE_FALLBACK.search(text_no_accent)
        if m:
            # Return original text of this line (with accents)
            m_orig = _DATE_LINE.search(line["text"])
            if m_orig:
                return m_orig.group(1).strip()
            return line["text"].strip()

    return None


def _extract_co_quan(lines, doc_type, page_width, page_height, layout_regions):
    """Extract issuing authority name."""
    # Collect header lines: top 20% of page, left half
    header_lines = []
    for line in lines:
        if line["y"] > page_height * 0.22:
            continue
        if not _is_left_half(line, page_width):
            continue
        if _is_excluded_line(line["text"]):
            continue
        # Skip very short lines (separators, stars)
        text_stripped = line["text"].strip()
        if len(text_stripped) <= 2:
            continue
        header_lines.append(line)

    if not header_lines:
        return None

    # Exclude document type name lines — they're doc titles, never an org name
    header_lines = [l for l in header_lines if not _DOC_TYPE_NAMES.match(l["text"].strip())]
    if not header_lines:
        return None
    # Exclude "Số ..." number/symbol lines
    header_lines = [l for l in header_lines
                    if not re.match(r'^S[oố]\s*[\d\-–—/]', l["text"].strip(), re.IGNORECASE)]
    if not header_lines:
        return None
    # Exclude "Về việc..." subject lines (document subject, not org name)
    header_lines = [l for l in header_lines
                    if not re.match(r'^V[eề]\s+vi[eệ]c', l["text"].strip(), re.IGNORECASE)]
    if not header_lines:
        return None

    # Use YOLO regions: org header appears as "title" or "abandon" in the top area.
    # DocLayout-YOLO has no "header" type — org names land in "title"/"abandon".
    # IMPORTANT: restrict to regions whose bottom edge is within top 22% of page
    # so we don't confuse the document type name (TỜ TRÌNH at 30-40%) with the org.
    if layout_regions:
        top_left_types = ("title", "abandon")
        top_org_regions = []
        for r in layout_regions:
            if r.get("type") not in top_left_types:
                continue
            bbox = r.get("bbox_pdf") or r.get("bbox", [0, 0, 0, 0])
            # Region must end within top 22% of page
            if bbox[3] < page_height * 0.22:
                top_org_regions.append(r)
        if top_org_regions:
            in_region = [l for l in header_lines
                         if any(_line_in_layout_region(l, [r], r["type"])
                                for r in top_org_regions)
                         and _is_left_half(l, page_width)]
            if in_region:
                header_lines = in_region

    # Sort by Y position (top to bottom)
    header_lines.sort(key=lambda l: l["y"])

    # Look for separator (content_type=4) or star (*) to find boundary
    sep_y = None
    for line in lines:
        if line["y"] > page_height * 0.22:
            continue
        if not _is_left_half(line, page_width):
            continue
        text_stripped = line["text"].strip()
        if line.get("content_type") == 4 or text_stripped in ("*", "＊"):
            sep_y = line["y"]
            break

    # Get lines above separator
    if sep_y is not None:
        above_sep = [l for l in header_lines if l["y"] < sep_y - 1]
        if above_sep:
            header_lines = above_sep

    # Pick the most prominent line(s) — prefer darker/bolder (lower fg_gray)
    # The main authority is typically the last/boldest line before separator
    if not header_lines:
        return None

    # If multiple lines, the last one (closest to separator) is typically the authority
    # The ones above it are parent org
    # Return the main authority (boldest or last)
    if len(header_lines) == 1:
        return header_lines[0]["text"].strip()

    # Find the line with lowest fg_gray (boldest) or largest font
    best = min(header_lines, key=lambda l: l.get("fg_gray", 128))
    # Also check: if last line is bold enough, prefer it
    last = header_lines[-1]
    if last.get("fg_gray", 128) <= best.get("fg_gray", 128) + 20:
        best = last

    # Join multi-line if they share the same block_id
    result_lines = []
    for l in header_lines:
        if l.get("block_id") == best.get("block_id"):
            result_lines.append(l)

    if result_lines:
        result_lines.sort(key=lambda l: l["y"])
        return " ".join(l["text"].strip() for l in result_lines)

    return best["text"].strip()


def _is_body_stop_line(text):
    """Return True if this line marks the start of body content (stop collecting subject)."""
    return bool(_TRICH_YEU_STOP.match(text.strip()))


def _collect_subject_continuations(anchor_line, all_lines, page_height, max_lines=4):
    """
    Collect subject continuation lines after anchor_line.
    Stops at body-content markers, separators, or after max_lines.
    Returns list of continuation texts (not including anchor).
    """
    line_y = anchor_line["y"]
    line_h = max(anchor_line.get("h", 14), 8)
    continuations = []

    sorted_after = sorted(
        [l for l in all_lines if l["y"] > line_y],
        key=lambda l: l["y"]
    )

    # Build a set of y-positions that have stop lines (e.g., "Kính gửi:" at same y as recipients)
    stop_ys = set()
    for l in all_lines:
        if _is_body_stop_line(l["text"].strip()):
            stop_ys.add(l["y"])

    for next_line in sorted_after:
        dy = next_line["y"] - line_y
        # Stop if gap > 2.5× line height — title lines are tight, body paragraphs have larger gaps
        if dy > line_h * 2.5:
            break
        next_text = next_line["text"].strip()
        if not next_text:
            continue
        # Stop at body-content markers
        if _is_body_stop_line(next_text):
            break
        # Skip lines that are at the same y-level as a stop line (right-column recipients etc.)
        if any(abs(next_line["y"] - sy) < 8 for sy in stop_ys):
            continue
        # Stop at separator lines
        if re.match(r'^[\-–—\.]{3,}$', next_text) or next_line.get("content_type") == 4:
            break
        # Stop if we've gone past subject zone
        if next_line["y"] > page_height * 0.55:
            break
        continuations.append(next_text)
        line_y = next_line["y"]
        if len(continuations) >= max_lines:
            break

    return continuations


def _extract_trich_yeu(lines, doc_type, page_width, page_height, layout_regions, date_y):
    """Extract document subject/summary."""
    anchor_y = date_y if date_y else page_height * 0.20

    # Method 1: Look for "V/v:" or "Về ..." pattern in top 55%
    top_half = sorted(
        [l for l in lines if l["y"] < page_height * 0.55],
        key=lambda l: l["y"]
    )
    for line in top_half:
        # Try explicit V/v: prefix first
        m = _VV_PATTERN.search(line["text"])
        if not m:
            # Try "Về ..." subject line (docs without explicit V/v prefix)
            m = _VE_PATTERN.match(line["text"].strip())
        if m:
            subject = m.group(1).strip()
            if subject and len(subject) > 4:
                conts = _collect_subject_continuations(line, lines, page_height, max_lines=3)
                if conts:
                    subject = subject + " " + " ".join(conts)
                return subject.strip()

    # Method 2: Find document type name (centered) then lines below it.
    # Search from 5% to 60% — do NOT use anchor_y as lower bound because the date
    # might be extracted from a referenced document (e.g. "Kết luận số X ngày Y"),
    # pushing anchor_y below the actual DOC_TYPE_NAME line.
    subject_zone = sorted(
        [l for l in lines if l["y"] > page_height * 0.05 and l["y"] < page_height * 0.60],
        key=lambda l: l["y"]
    )

    def _normalize_ocr_lookalikes(text):
        """Replace Cyrillic/Greek lookalike chars that ScreenAI sometimes emits."""
        return (text
                .replace('\u041E', 'O').replace('\u043E', 'o')  # Cyrillic О/о → O/o
                .replace('\u0410', 'A').replace('\u0430', 'a')  # Cyrillic А/а → A/a
                .replace('\u0421', 'C').replace('\u0441', 'c')  # Cyrillic С/с → C/c
                .replace('\u0395', 'E').replace('\u03B5', 'e')  # Greek Ε/ε
                )

    doc_type_line = None
    for line in subject_zone:
        norm_text = _normalize_ocr_lookalikes(line["text"].strip())
        if _DOC_TYPE_NAMES.match(norm_text):
            doc_type_line = line
            break

    if doc_type_line:
        # Include the DOC_TYPE_NAME itself as the prefix of the subject.
        # Use normalized text so "BÁО CÁО" (with Cyrillic О) becomes clean "BÁO CÁO"
        subject_parts = [_normalize_ocr_lookalikes(doc_type_line["text"].strip())]
        conts = _collect_subject_continuations(doc_type_line, lines, page_height, max_lines=5)
        for text in conts:
            if not _DOC_TYPE_NAMES.match(text):
                subject_parts.append(text)
        return " ".join(subject_parts).strip()

    # Method 3: Use layout_regions type="title" hint
    if layout_regions:
        title_lines = [l for l in subject_zone
                       if _line_in_layout_region(l, layout_regions, "title")]
        if title_lines:
            # Filter out doc type names, separator lines, and body-stop content
            content = []
            for l in sorted(title_lines, key=lambda x: x["y"]):
                text = l["text"].strip()
                if not text:
                    continue
                if re.match(r'^[\-–—]{3,}', text):
                    break
                if _DOC_TYPE_NAMES.match(text):
                    continue
                if _is_body_stop_line(text):
                    break
                # Skip pure numbering/fragment lines (e.g. "7:", "1: 2:")
                if re.match(r'^[\d\s\:\.\-]+$', text):
                    continue
                content.append(text)
            if content:
                return " ".join(content).strip()

    return None


def _extract_nguoi_ky(lines, page_width, page_height):
    """Extract signer name from bottom of page."""
    # Search bottom 40% of page
    bottom_lines = [l for l in lines if l["y"] > page_height * 0.60]

    if not bottom_lines:
        return None

    # Sort by Y descending (bottom first)
    bottom_lines.sort(key=lambda l: l["y"], reverse=True)

    # Strategy 1: Find signature content_type=8, name is line below it
    sig_lines = [l for l in bottom_lines if l.get("content_type") == 8]
    if sig_lines:
        sig_y = min(l["y"] for l in sig_lines)
        # Look for name line BELOW signature (within 3x line height)
        for line in sorted(bottom_lines, key=lambda l: l["y"]):
            if line["y"] > sig_y and line["y"] - sig_y < 50:
                text = line["text"].strip()
                if _NAME_PATTERN.match(text) and not _has_title_keyword(text):
                    return text

    # Strategy 2: Find name pattern in right half, excluding title keywords
    candidates = []
    for line in bottom_lines:
        text = line["text"].strip()
        if not text or len(text) < 4:
            continue
        if _has_title_keyword(text):
            continue
        # Check if it looks like a Vietnamese name
        if _NAME_PATTERN.match(text):
            candidates.append(line)
            continue
        # Also check: ALL CAPS name (common in formal docs: NGUYỄN VĂN A)
        # Must contain only word chars and spaces — no digits, dots, colons, slashes
        if not re.search(r'[0-9\.\,\:\;\!\?\(\)\[\]/\\]', text):
            words = text.split()
            if 2 <= len(words) <= 5 and all(w[0].isupper() for w in words if w):
                # Exclude section headings (contain known org/admin keywords)
                if not any(kw.upper() in text.upper() for kw in
                           ["BAN", "PHÒNG", "SỞ", "VĂN PHÒNG", "ỦY BAN",
                            "MỤC ĐÍCH", "YÊU CẦU", "NỘI DUNG",
                            "NƠI NHẬN", "NƠI GỬI", "KÍNH GỬI"]):
                    candidates.append(line)

    if candidates:
        # Prefer candidates in right half
        right_candidates = [c for c in candidates if _is_right_half(c, page_width)]
        if right_candidates:
            # Return the last (lowest) right-aligned name candidate
            return right_candidates[0]["text"].strip()
        return candidates[0]["text"].strip()

    return None


def _extract_loai_van_ban(so_ky_hieu, trich_yeu, doc_type):
    """Infer document type from number/symbol or subject."""
    # Method 1: Parse from document number
    if so_ky_hieu:
        # Party format: "Số 127-QĐ/VPTW" → extract "QĐ" between - and /
        m = re.search(r'[\-–—]\s*([A-Za-zĐđÀ-ỹ]+)\s*/', so_ky_hieu)
        if m:
            abbr = m.group(1).strip()
            if abbr in _TYPE_MAP:
                return _TYPE_MAP[abbr]

        # State format: "Số 123/QĐ-UBND" → extract "QĐ" between / and -
        m = re.search(r'/\s*([A-Za-zĐđÀ-ỹ]+)\s*[\-–—]', so_ky_hieu)
        if m:
            abbr = m.group(1).strip()
            if abbr in _TYPE_MAP:
                return _TYPE_MAP[abbr]

        # Try just after last /
        m = re.search(r'/\s*([A-Za-zĐđÀ-ỹ]+)\s*$', so_ky_hieu)
        if m:
            abbr = m.group(1).strip()
            if abbr in _TYPE_MAP:
                return _TYPE_MAP[abbr]

    # Method 2: Match from subject/title text
    if trich_yeu:
        m = _DOC_TYPE_NAMES.match(trich_yeu.strip())
        if m:
            name = m.group(1).strip().upper()
            for abbr, full_name in _TYPE_MAP.items():
                if full_name.upper() == name:
                    return full_name

    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_metadata(json_path, log_callback=None):
    """
    Extract structured metadata from OCR companion JSON.
    Reads FIRST page data only.

    Args:
        json_path: Path to _ocr.pdf.json companion file
        log_callback: Optional callback(msg, level) for logging

    Returns:
        dict with keys:
            "doc_type"          - "dang" | "nhanuoc" | "unknown"
            "co_quan_ban_hanh"  - Issuing authority (str or None)
            "ngay_ban_hanh"     - Issuance date string (str or None)
            "so_van_ban"        - Document number only (str or None)
            "ky_hieu"           - Document symbol only (str or None)
            "so_ky_hieu"        - Document number/symbol (str or None)
            "trich_yeu"         - Document subject/summary (str or None)
            "nguoi_ky"          - Signer name (str or None)
            "loai_van_ban"      - Document type inferred (str or None)
    """
    def _log(msg, level="info"):
        if log_callback:
            try:
                log_callback(msg, level)
            except TypeError:
                log_callback(msg)

    result = {
        "doc_type": "unknown",
        "co_quan_ban_hanh": None,
        "ngay_ban_hanh": None,
        "so_van_ban": None,
        "ky_hieu": None,
        "so_ky_hieu": None,
        "trich_yeu": None,
        "nguoi_ky": None,
        "loai_van_ban": None,
    }

    if not json_path or not os.path.exists(json_path):
        _log(f"Metadata: JSON not found: {json_path}", "err")
        return result

    try:
        with open(json_path, "r", encoding="utf-8") as f:
            ocr_data = json.load(f)
    except Exception as e:
        _log(f"Metadata: Failed to load JSON: {e}", "err")
        return result

    pages = ocr_data.get("pages", [])
    if not pages:
        _log("Metadata: No pages in JSON", "err")
        return result

    # Use first page only
    page = pages[0]
    lines = page.get("lines", [])
    layout_regions = page.get("layout_regions", [])
    page_width = page.get("width", 595.0)
    page_height = page.get("height", 842.0)

    if not lines:
        _log("Metadata: No lines on page 1", "err")
        return result

    # Step 0: Detect doc type (Đảng vs Nhà nước)
    doc_type = _detect_doc_type(lines)
    result["doc_type"] = doc_type
    _log(f"Metadata: Detected doc type = {doc_type}")

    # Step 1: Extract Số, Ký hiệu
    result["so_ky_hieu"] = _extract_so_ky_hieu(lines, doc_type, page_height)
    result["so_van_ban"], result["ky_hieu"] = _split_so_ky_hieu(result["so_ky_hieu"])

    # Step 2: Extract Ngày tháng ban hành
    result["ngay_ban_hanh"] = _extract_ngay_ban_hanh(lines, page_height)

    # Find date Y position for anchoring subject extraction
    date_y = None
    if result["ngay_ban_hanh"]:
        for line in lines:
            if _DATE_PATTERN.search(line["text"]) or _DATE_FALLBACK.search(
                    _remove_accents(line["text"])):
                date_y = line["y"]
                break

    # Step 3: Extract Cơ quan ban hành
    result["co_quan_ban_hanh"] = _extract_co_quan(
        lines, doc_type, page_width, page_height, layout_regions)

    # Step 4: Extract Trích yếu
    result["trich_yeu"] = _extract_trich_yeu(
        lines, doc_type, page_width, page_height, layout_regions, date_y)

    # Step 5: Extract Tên người ký
    result["nguoi_ky"] = _extract_nguoi_ky(lines, page_width, page_height)

    # Step 6: Infer Loại văn bản
    result["loai_van_ban"] = _extract_loai_van_ban(
        result["so_ky_hieu"], result["trich_yeu"], doc_type)

    # Log summary
    parts = []
    if result["so_ky_hieu"]:
        parts.append(result["so_ky_hieu"])
    if result["ngay_ban_hanh"]:
        parts.append(result["ngay_ban_hanh"])
    if result["co_quan_ban_hanh"]:
        parts.append(result["co_quan_ban_hanh"])
    summary = ", ".join(parts) if parts else "(no fields extracted)"
    _log(f"Metadata: {summary}", "success")

    return result


def _page_is_probably_appendix(page):
    lines = page.get("lines", [])
    if not lines:
        return False
    first_text = _normalize_for_match(lines[0].get("text", ""))
    return "PHU LUC" in first_text


def extract_metadata(json_path, log_callback=None):
    """
    Extract structured metadata from OCR companion JSON.
    Prefers the first 1-2 pages for header fields and the last 1-2 pages
    for signer detection.

    Args:
        json_path: Path to _ocr.pdf.json companion file
        log_callback: Optional callback(msg, level) for logging

    Returns:
        dict with keys:
            "doc_type"          - "dang" | "nhanuoc" | "unknown"
            "co_quan_ban_hanh"  - Issuing authority (str or None)
            "ngay_ban_hanh"     - Issuance date string (str or None)
            "so_van_ban"        - Document number only (str or None)
            "ky_hieu"           - Document symbol only (str or None)
            "so_ky_hieu"        - Document number/symbol (str or None)
            "trich_yeu"         - Document subject/summary (str or None)
            "nguoi_ky"          - Signer name (str or None)
            "loai_van_ban"      - Document type inferred (str or None)
    """
    def _log(msg, level="info"):
        if log_callback:
            try:
                log_callback(msg, level)
            except TypeError:
                log_callback(msg)

    result = {
        "doc_type": "unknown",
        "co_quan_ban_hanh": None,
        "ngay_ban_hanh": None,
        "so_van_ban": None,
        "ky_hieu": None,
        "so_ky_hieu": None,
        "trich_yeu": None,
        "nguoi_ky": None,
        "loai_van_ban": None,
    }

    if not json_path or not os.path.exists(json_path):
        _log(f"Metadata: JSON not found: {json_path}", "err")
        return result

    try:
        with open(json_path, "r", encoding="utf-8") as f:
            ocr_data = json.load(f)
    except Exception as e:
        _log(f"Metadata: Failed to load JSON: {e}", "err")
        return result

    pages = ocr_data.get("pages", [])
    if not pages:
        _log("Metadata: No pages in JSON", "err")
        return result

    header_pages = pages[:2] if len(pages) > 1 else pages[:1]
    signer_pages = [page for page in pages[-2:] if not _page_is_probably_appendix(page)]
    if not signer_pages:
        signer_pages = pages[-2:] if len(pages) > 1 else pages[:1]
    signer_pages = list(reversed(signer_pages))

    doc_type = "unknown"
    for page in header_pages:
        lines = page.get("lines", [])
        if not lines:
            continue
        detected = _detect_doc_type(lines)
        if detected != "unknown":
            doc_type = detected
            break
    result["doc_type"] = doc_type
    _log(f"Metadata: Detected doc type = {doc_type}")

    header_page_found = False
    for page in header_pages:
        lines = page.get("lines", [])
        if not lines:
            continue
        header_page_found = True
        layout_regions = page.get("layout_regions", [])
        page_width = page.get("width", 595.0)
        page_height = page.get("height", 842.0)

        if not result["so_ky_hieu"]:
            result["so_ky_hieu"] = _extract_so_ky_hieu(lines, doc_type, page_height)
            result["so_van_ban"], result["ky_hieu"] = _split_so_ky_hieu(result["so_ky_hieu"])

        if not result["ngay_ban_hanh"]:
            result["ngay_ban_hanh"] = _extract_ngay_ban_hanh(lines, page_height)

        date_y = None
        if result["ngay_ban_hanh"]:
            for line in lines:
                if _DATE_PATTERN.search(line["text"]) or _DATE_FALLBACK.search(_remove_accents(line["text"])):
                    date_y = line["y"]
                    break

        if not result["co_quan_ban_hanh"]:
            result["co_quan_ban_hanh"] = _extract_co_quan(
                lines, doc_type, page_width, page_height, layout_regions)

        if not result["trich_yeu"]:
            result["trich_yeu"] = _extract_trich_yeu(
                lines, doc_type, page_width, page_height, layout_regions, date_y)

        if result["so_ky_hieu"] and result["ngay_ban_hanh"] and result["co_quan_ban_hanh"] and result["trich_yeu"]:
            break

    if not header_page_found:
        _log("Metadata: No lines on header pages", "err")
        return result

    for page in signer_pages:
        lines = page.get("lines", [])
        if not lines:
            continue
        page_width = page.get("width", 595.0)
        page_height = page.get("height", 842.0)
        signer_name = _extract_nguoi_ky(lines, page_width, page_height)
        if signer_name:
            result["nguoi_ky"] = signer_name
            break

    if not result["nguoi_ky"]:
        for page in header_pages:
            lines = page.get("lines", [])
            if not lines:
                continue
            page_width = page.get("width", 595.0)
            page_height = page.get("height", 842.0)
            signer_name = _extract_nguoi_ky(lines, page_width, page_height)
            if signer_name:
                result["nguoi_ky"] = signer_name
                break

    result["loai_van_ban"] = _extract_loai_van_ban(
        result["so_ky_hieu"], result["trich_yeu"], doc_type)

    parts = []
    if result["so_ky_hieu"]:
        parts.append(result["so_ky_hieu"])
    if result["ngay_ban_hanh"]:
        parts.append(result["ngay_ban_hanh"])
    if result["co_quan_ban_hanh"]:
        parts.append(result["co_quan_ban_hanh"])
    summary = ", ".join(parts) if parts else "(no fields extracted)"
    _log(f"Metadata: {summary}", "success")

    return result
