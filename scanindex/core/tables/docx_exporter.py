"""
PDF to DOCX Position-Preserving Converter v4

Key Features:
1. Creates a NEW blank DOCX from the final OCR PDF
2. Extracts text with positions from PDF using PyMuPDF
3. Recreates text in DOCX with correct positions (using line breaks/paragraphs)
4. All text is Times New Roman 14pt
5. Tables detected by Camelot are preserved as tables

Input: _ocr.pdf (final OCR text with positions)
Output: _final.docx (clean document with preserved layout)
"""

import os
# FORCE CPU ONLY
os.environ["CUDA_VISIBLE_DEVICES"] = ""

import re
import fitz  # PyMuPDF
import unicodedata
from dataclasses import dataclass, field
from typing import List, Tuple, Optional, Dict, Set
from docx import Document
from docx.shared import Pt, Inches, Twips, Cm, Mm
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_PARAGRAPH_ALIGNMENT
from docx.enum.table import WD_TABLE_ALIGNMENT, WD_CELL_VERTICAL_ALIGNMENT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from datetime import datetime



# Img2Table is retained only for historical benchmark scripts. Production table
# extraction is DocLayout bbox + GMFT-ONNX + Docling TableFormer v1 step-cache
# ONNX, so the runtime does not import img2table or use it as a fallback.
PDF = None
img2table_Image = None
IMG2TABLE_AVAILABLE = False

# GMFT-ONNX is part of the production DocLayout-anchored table pipeline.
try:
    import importlib
    _gmft_onnx_engine = importlib.import_module("scanindex.core.tables.gmft_onnx_table_engine")
    detect_tables_gmft_onnx = _gmft_onnx_engine.detect_tables_gmft_onnx
    detect_tables_gmft_onnx_on_layout_regions = _gmft_onnx_engine.detect_tables_gmft_onnx_on_layout_regions
    GMFT_ONNX_AVAILABLE = _gmft_onnx_engine.is_gmft_onnx_available()
except ImportError:
    detect_tables_gmft_onnx = None
    detect_tables_gmft_onnx_on_layout_regions = None
    GMFT_ONNX_AVAILABLE = False

# Docling TableFormer v1 accurate ONNX is used as a PyTorch-free structure recognizer on
# DocLayout table boxes. It is not used as the primary detector in production.
try:
    from scanindex.core.tables.docling_tableformer_v1_onnx_engine import (
        detect_tables_docling_tableformer_v1_onnx,
        is_docling_tableformer_v1_onnx_available,
    )
    DOCLING_TABLEFORMER_AVAILABLE = is_docling_tableformer_v1_onnx_available()
except ImportError:
    detect_tables_docling_tableformer_v1_onnx = None
    DOCLING_TABLEFORMER_AVAILABLE = False

# RapidTable/Wired variants are benchmark-only now. They are intentionally not
# imported by production runtime to keep the PDF-to-DOCX path on the selected
# GMFT + Docling v1 step-cache ONNX pipeline.
detect_tables_rapidtable_slanet = None
RAPIDTABLE_AVAILABLE = False

# Legacy PyTorch GMFT is opt-in for dev comparison only.
GMFT_AVAILABLE = False
if os.environ.get("OCRTOOL_ALLOW_PYTORCH_GMFT") == "1":
    try:
        import sys
        from scanindex.infra.paths import get_base_dir
        legacy_gmft_dir = os.path.join(
            get_base_dir(),
            "temp",
            "legacy_model_train_20260504",
            "root_cleanup",
            "antigravity-gmft",
        )
        sys.path.insert(0, legacy_gmft_dir)
        from gmft_table_engine import detect_tables_gmft, is_gmft_available
        GMFT_AVAILABLE = is_gmft_available()
    except ImportError:
        GMFT_AVAILABLE = False
else:
    detect_tables_gmft = None
    GMFT_AVAILABLE = False


# ============================================================================
# DATA CLASSES
# ============================================================================

@dataclass
class TextSpan:
    """A span of text with formatting info."""
    text: str
    font_size: float
    y: float  # y position
    is_superscript: bool = False
    x: float = 0.0
    width: float = 0.0
    fg_gray: int = 128
    has_space_after: bool = True


@dataclass
class TextLine:
    """A line of text from PDF with position."""
    text: str
    x: float
    y: float
    width: float
    height: float
    page: int
    font_size: float = 12.0  # Average font size of spans
    is_footnote: bool = False
    spans: List[TextSpan] = None  # Individual spans for superscript detection
    block_id: int = 0          # Screen AI layout block group
    paragraph_id: int = 0      # Paragraph within block
    content_type: int = 0      # 0=printed, 1=handwritten, 4=separator, 8=signature
    fg_gray: int = 128         # Foreground grayscale (lower = darker/bolder)
    confidence: float = 0.0    # OCR confidence [0,1]
    semantic_type: str = ""    # From DocLayout-YOLO: title, text, figure, header, footer...
    order: int = 0             # OCR reading order within the page
    source_line_id: str = ""    # Canonical OCR line id, when available
    kie_labels: Set[str] = field(default_factory=set)

    @property
    def y_center(self) -> float:
        return self.y + self.height / 2

    def __post_init__(self):
        if self.spans is None:
            self.spans = []


@dataclass
class TableRegion:
    """A table detected in PDF with precise coordinates."""
    page: int
    y_top: float      # Visual Y from top
    y_bottom: float   # Visual Y from top
    cells: List[List[str]]  # Retain for structure/fallback
    row_count: int
    col_count: int
    cell_bboxes: List[List[Tuple[float, float, float, float]]] = field(default_factory=list) # [x0, y0, x1, y1] Visual coords


# ============================================================================
# LOGGING
# ============================================================================

class Logger:
    def __init__(self, log_path: Optional[str]):
        self.log_path = log_path
        self.lines = []
        
    def log(self, msg: str):
        # timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S") 
        # User requested to remove internal timestamp as GUI adds its own
        line = msg 
        self.lines.append(line)
        try:
            print(line)
        except:
            pass
        
    def save(self):
        if self.log_path:
            with open(self.log_path, 'w', encoding='utf-8') as f:
                f.write("\n".join(self.lines))
            
    def get_log_text(self) -> str:
        return "\n".join(self.lines)


# ============================================================================
# PDF TEXT EXTRACTION WITH POSITIONS
# ============================================================================

def _clean_extracted_text(text: str, strip: bool = True) -> str:
    """Normalize PDF/Word extraction artifacts without changing OCR wording."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = (
        text.replace("\u00a0", " ")
            .replace("\u202f", " ")
            .replace("\ufeff", "")
            .replace("\u00ad", "-")
            .replace("\u2010", "-")
            .replace("\u2011", "-")
            .replace("\u2012", "-")
            .replace("\u2013", "-")
            .replace("\u2014", "-")
    )
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip() if strip else text


def _json_line_xywh(line: dict) -> Tuple[float, float, float, float]:
    if all(k in line and line.get(k) is not None for k in ("x", "y", "w", "h")):
        try:
            return (
                float(line.get("x", 0.0)),
                float(line.get("y", 0.0)),
                float(line.get("w", 0.0)),
                float(line.get("h", 0.0)),
            )
        except (TypeError, ValueError):
            pass

    bbox = line.get("bbox") or [0.0, 0.0, 0.0, 0.0]
    if len(bbox) < 4:
        return (0.0, 0.0, 0.0, 0.0)
    try:
        x0, y0, x1, y1 = (float(v) for v in bbox[:4])
    except (TypeError, ValueError):
        return (0.0, 0.0, 0.0, 0.0)
    return (x0, y0, max(0.0, x1 - x0), max(0.0, y1 - y0))


def load_lines_from_companion_json(json_path: str, logger: Logger) -> Optional[List[TextLine]]:
    """Load OCR text/positions directly from the canonical JSON when present."""
    if not os.path.exists(json_path):
        return None

    try:
        import json
        with open(json_path, "r", encoding="utf-8") as f:
            ocr_data = json.load(f)
    except Exception as e:
        logger.log(f"Could not load OCR JSON as text source: {e}")
        return None

    all_lines: List[TextLine] = []
    kie_labels_by_line = _line_labels_from_annotations(ocr_data)
    for page_idx, page in enumerate(ocr_data.get("pages", []), 1):
        words_by_line: Dict[str, List[dict]] = {}
        for word in page.get("words", []) or []:
            line_id = str(word.get("line_id") or "")
            if not line_id:
                continue
            wx, wy, ww, wh = _json_line_xywh(word)
            if ww <= 0 or wh <= 0:
                continue
            words_by_line.setdefault(line_id, []).append({
                "text": _clean_extracted_text(word.get("text") or word.get("ocr_text") or ""),
                "x": wx,
                "y": wy,
                "w": ww,
                "h": wh,
                "order": int(word.get("order", 0) or 0),
                "has_space_after": bool(word.get("has_space_after", True)),
                "fg_gray": int(word.get("fg_gray", 128) or 128),
                "confidence": float(word.get("confidence", 0.0) or 0.0),
            })

        for line in page.get("lines", []):
            text = _clean_extracted_text(line.get("text") or line.get("ocr_text") or "")
            if not text:
                continue
            source_line_id = str(line.get("id") or "")
            x, y, w, h = _json_line_xywh(line)
            if w <= 0 or h <= 0:
                continue
            font_size = float(line.get("font_size") or max(h * 0.78, 4.0))
            text_line = TextLine(
                text=text,
                x=x,
                y=y,
                width=w,
                height=h,
                page=page_idx,
                font_size=font_size,
                spans=[TextSpan(text=text, font_size=font_size, y=y)],
                block_id=int(line.get("block_id", 0) or 0),
                paragraph_id=int(line.get("paragraph_id", 0) or 0),
                content_type=int(line.get("content_type", 0) or 0),
                fg_gray=int(line.get("fg_gray", 128) or 128),
                confidence=float(line.get("confidence", 0.0) or 0.0),
                order=int(line.get("order", len(all_lines)) or 0),
                source_line_id=source_line_id,
                kie_labels=set(kie_labels_by_line.get(source_line_id, set())),
            )
            line_words = words_by_line.get(str(line.get("id") or ""), [])
            if line_words:
                line_words.sort(key=lambda item: item["order"])
                setattr(text_line, "word_items", line_words)
                text_line.spans = [
                    TextSpan(
                        text=item["text"],
                        font_size=font_size,
                        y=item["y"],
                        x=item["x"],
                        width=item["w"],
                        fg_gray=int(item.get("fg_gray", 128) or 128),
                        has_space_after=bool(item.get("has_space_after", True)),
                    )
                    for item in line_words
                    if item.get("text")
                ]
            all_lines.append(text_line)

    if not all_lines:
        return None

    all_lines.sort(key=lambda l: (l.page, l.order, l.y, l.x))
    logger.log(f"Using companion OCR JSON text source: {len(all_lines)} lines")
    return all_lines


def _line_bbox(line: TextLine) -> Tuple[float, float, float, float]:
    return (line.x, line.y, line.x + line.width, line.y + line.height)


def _bbox_overlap_ratio(a: Tuple[float, float, float, float],
                        b: Tuple[float, float, float, float]) -> float:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0 = max(ax0, bx0)
    iy0 = max(ay0, by0)
    ix1 = min(ax1, bx1)
    iy1 = min(ay1, by1)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    area_a = max((ax1 - ax0) * (ay1 - ay0), 1e-6)
    return ((ix1 - ix0) * (iy1 - iy0)) / area_a


def _unaccent_upper(text: str) -> str:
    text = (text or "").upper().replace("Đ", "D")
    text = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in text if unicodedata.category(ch) != "Mn")


def _line_labels_from_annotations(ocr_data: dict) -> Dict[str, Set[str]]:
    """Map canonical OCR line ids to KIE labels from annotations."""
    labels_by_line: Dict[str, Set[str]] = {}
    ann = ocr_data.get("annotations") or {}
    fields = ann.get("field_instances") or []
    if not fields:
        return labels_by_line

    word_to_line: Dict[str, str] = {}
    for page in ocr_data.get("pages", []) or []:
        for word in page.get("words", []) or []:
            word_id = str(word.get("id") or "")
            line_id = str(word.get("line_id") or "")
            if word_id and line_id:
                word_to_line[word_id] = line_id

    for inst in fields:
        label = str(inst.get("label") or "").strip()
        if not label:
            continue
        line_ids = {str(line_id) for line_id in (inst.get("line_ids") or []) if str(line_id)}
        for word_id in inst.get("word_ids") or []:
            line_id = word_to_line.get(str(word_id))
            if line_id:
                line_ids.add(line_id)
        for line_id in line_ids:
            labels_by_line.setdefault(line_id, set()).add(label)
    return labels_by_line


def _mostly_uppercase_text(text: str, threshold: float = 0.65) -> bool:
    letters = [ch for ch in text if ch.isalpha()]
    if len(letters) < 3:
        return False
    upper = sum(1 for ch in letters if ch.upper() == ch and ch.lower() != ch)
    lower = sum(1 for ch in letters if ch.lower() == ch and ch.upper() != ch)
    return upper > 0 and upper / max(upper + lower, 1) >= threshold


def _looks_like_numbered_heading(text: str) -> bool:
    """Detect left-aligned section headings such as "II. ...", "4. ..." or "5.1. ..."."""
    stripped = _clean_extracted_text(text)
    roman_match = re.match(r"^\s*([IVXLCDM]{1,8}\.)\s+(.+)$", stripped)
    match = roman_match or re.match(r"^\s*(\d{1,3}(?:\.\d{1,3}){0,4}\.?)\s+(.+)$", stripped)
    if not match:
        return False
    marker = match.group(1).strip()
    body = match.group(2).strip()
    if not body or len(body) > 160:
        return False
    if body.endswith(":"):
        return True
    if _mostly_uppercase_text(body):
        return True
    words = [w for w in re.split(r"\s+", body) if w]
    if body.endswith((".", ";", ",")):
        return False
    if roman_match:
        return len(words) <= 14
    if "." in marker.rstrip("."):
        return len(words) <= 14 and len(body) <= 120
    return len(words) <= 14 and len(body) <= 90


def _looks_like_list_item(text: str) -> bool:
    stripped = _clean_extracted_text(text)
    return is_numbered_bullet(stripped)


def _starts_with_dash_marker(text: str) -> bool:
    return bool(re.match(r"^\s*[-–]\s+", text or ""))


def _dash_marker_can_continue_previous_sentence(prev_text: str, next_text: str) -> bool:
    prev_text = (prev_text or "").rstrip()
    next_text = (next_text or "").lstrip()
    if not prev_text or not _starts_with_dash_marker(next_text):
        return False
    if prev_text[-1] in '.?!:;)]}"':
        return False
    after_dash = re.sub(r"^\s*[-–]\s+", "", next_text, count=1).strip()
    return bool(after_dash and after_dash[0].isalpha())


def _looks_like_visual_bold_heading(
    text: str,
    semantic_type: str,
    numbered_heading: bool,
    is_doc_subject: bool,
    is_centered: bool,
) -> bool:
    """Conservative paragraph-level bold detection for OCR-exported DOCX."""
    stripped = _clean_extracted_text(text)
    if not stripped:
        return False
    if numbered_heading or is_doc_subject:
        return True

    sem = (semantic_type or "").strip().lower()
    if sem == "title":
        if len(stripped) > 160:
            return False
        if _looks_like_list_item(stripped) and len(stripped) > 120:
            return False
        return is_centered or _mostly_uppercase_text(stripped)

    return is_centered and len(stripped) <= 140 and _mostly_uppercase_text(stripped)


def _looks_like_header_emphasis_line(text: str) -> bool:
    """Detect short formal header lines by typography, not by wording."""
    stripped = _clean_extracted_text(text)
    if not stripped:
        return False
    letters = [ch for ch in stripped if ch.isalpha()]
    if len(letters) < 4:
        return False
    if not _mostly_uppercase_text(stripped, threshold=0.55):
        return False
    words = [w for w in re.split(r"\s+", stripped) if re.search(r"[A-Za-zÀ-Ỵà-ỵĐđ]", w)]
    if len(words) > 8:
        return False
    digit_count = sum(1 for ch in stripped if ch.isdigit())
    if digit_count and digit_count >= max(2, len(letters) * 0.15):
        return False
    if stripped.endswith((".", ",", ";", ":")):
        return False
    return True


def _looks_like_person_name(text: str) -> bool:
    stripped = _clean_extracted_text(text)
    if not stripped or re.search(r"\d", stripped):
        return False
    if any(ch in stripped for ch in ":;,/\\"):
        return False
    letters = [ch for ch in stripped if ch.isalpha()]
    if letters and not any(ch.islower() for ch in letters):
        return False
    words = [w for w in re.split(r"\s+", stripped) if re.search(r"[A-Za-zÀ-Ỵà-ỵĐđ]", w)]
    if not (2 <= len(words) <= 5):
        return False
    titlecase_words = 0
    for word in words:
        clean = word.strip("().,")
        is_initial = len(clean) == 1 and clean.isalpha() and clean.upper() == clean
        if len(clean) < 2 and not is_initial:
            return False
        first_alpha = next((ch for ch in clean if ch.isalpha()), "")
        if first_alpha and first_alpha.upper() == first_alpha:
            titlecase_words += 1
    return titlecase_words >= max(2, len(words) - 1)


def _is_signature_footer_line(text: str) -> bool:
    return _looks_like_header_emphasis_line(text) or _looks_like_person_name(text)


def _looks_like_qr_access_artifact(text: str) -> bool:
    normalized = _unaccent_upper(_clean_extracted_text(text))
    if "QR" not in normalized:
        return False
    return any(token in normalized for token in ("MA QR", "TRUY CAP", "DUONG LINK", "LINK", "THONG KE"))


def _line_overlaps_layout_type(line: TextLine,
                               layout_regions_by_page: Dict[int, List[dict]],
                               region_type: str,
                               min_overlap: float = 0.35) -> bool:
    for region in layout_regions_by_page.get(line.page, []):
        if region.get("type") != region_type:
            continue
        bbox = region.get("bbox_pdf")
        if not bbox or len(bbox) < 4:
            continue
        if _bbox_overlap_ratio(_line_bbox(line), tuple(float(v) for v in bbox[:4])) >= min_overlap:
            return True
    return False


def filter_figure_ocr_noise(lines: List[TextLine],
                            layout_regions_by_page: Dict[int, List[dict]],
                            logger: Logger) -> List[TextLine]:
    return lines


def enrich_lines_from_json(lines: List[TextLine], json_path: str, logger: Logger):
    """
    Enrich TextLine objects with Screen AI metadata from companion JSON.
    Matches lines by bbox overlap/proximity and assigns:
    block_id, paragraph_id, content_type, confidence, fg_gray.
    """
    if not os.path.exists(json_path):
        return

    try:
        import json
        with open(json_path, "r", encoding="utf-8") as f:
            ocr_data = json.load(f)
    except Exception as e:
        logger.log(f"Could not load OCR JSON for enrichment: {e}")
        return

    pages = ocr_data.get("pages", [])
    kie_labels_by_line = _line_labels_from_annotations(ocr_data)
    enriched = 0

    for tl in lines:
        page_idx = tl.page - 1  # TextLine.page is 1-based
        if page_idx < 0 or page_idx >= len(pages):
            continue

        json_lines = pages[page_idx].get("lines", [])
        if not json_lines:
            continue

        best_match = None
        best_score = None
        tl_bbox = _line_bbox(tl)
        tl_cx = tl.x + tl.width / 2.0
        tl_cy = tl.y + tl.height / 2.0
        for jl in json_lines:
            jx, jy, jw, jh = _json_line_xywh(jl)
            if jw <= 0 or jh <= 0:
                continue
            jb = (jx, jy, jx + jw, jy + jh)
            overlap = _bbox_overlap_ratio(tl_bbox, jb)
            jcx = jx + jw / 2.0
            jcy = jy + jh / 2.0
            x_dist = abs(tl_cx - jcx) / max(tl.width, jw, 1.0)
            y_dist = abs(tl_cy - jcy) / max(tl.height, jh, 1.0)

            if overlap > 0:
                score = 10.0 * overlap - x_dist - y_dist
            elif x_dist <= 0.35 and y_dist <= 0.75:
                score = 1.0 - x_dist - y_dist
            else:
                continue

            if best_score is None or score > best_score:
                best_score = score
                best_match = jl

        if best_match and best_score is not None and best_score > 0:
            json_text = _clean_extracted_text(best_match.get("text") or "")
            if json_text:
                tl.text = json_text
            source_line_id = str(best_match.get("id") or "")
            tl.source_line_id = source_line_id
            tl.kie_labels = set(kie_labels_by_line.get(source_line_id, set()))
            tl.block_id = best_match.get("block_id", 0)
            tl.paragraph_id = best_match.get("paragraph_id", 0)
            tl.content_type = best_match.get("content_type", 0)
            tl.confidence = best_match.get("confidence", 0.0)
            tl.fg_gray = best_match.get("fg_gray", 128)
            tl.order = int(best_match.get("order", tl.order) or tl.order)
            # Override geometry with OCR bbox (more accurate than rendered text).
            # direct_ocr_engine renders line as single span with approximate font size,
            # so PyMuPDF's span bbox may not match the real visual extent.
            ocr_x, ocr_y, ocr_w, ocr_h = _json_line_xywh(best_match)
            if ocr_w > 0 and ocr_h > 0:
                tl.x = ocr_x
                tl.y = ocr_y
                tl.width = ocr_w
                tl.height = ocr_h
            enriched += 1

    if enriched > 0:
        logger.log(f"Enriched {enriched}/{len(lines)} lines with Screen AI metadata")


def extract_pdf_lines(pdf_path: str, logger: Logger) -> Tuple[List[TextLine], dict]:
    """
    Extract all text lines from PDF with their positions.
    Returns: (lines, page_info) where page_info contains dimensions.
    """
    doc = fitz.open(pdf_path)
    all_lines = []
    page_info = {}
    
    for page_num, page in enumerate(doc, 1):
        page_info[page_num] = {
            "width": page.rect.width,
            "height": page.rect.height
        }

    json_lines = load_lines_from_companion_json(pdf_path + ".json", logger)
    if json_lines is not None:
        doc.close()
        logger.log(f"PDF: {len(page_info)} pages, {len(json_lines)} OCR JSON text lines loaded")
        return json_lines, page_info

    for page_num, page in enumerate(doc, 1):
        
        # Extract text as dict for detailed position info
        blocks = page.get_text("dict")["blocks"]
        
        for block in blocks:
            if "lines" not in block:
                continue
            
            for line in block["lines"]:
                line_text = ""
                x0, y0, x1, y1 = float('inf'), float('inf'), 0, 0
                font_sizes = []
                text_spans = []
                
                for span in line["spans"]:
                    text = _clean_extracted_text(span["text"], strip=False)
                    line_text += text
                    if text.strip():
                        bbox = span["bbox"]
                        x0 = min(x0, bbox[0])
                        y0 = min(y0, bbox[1])
                        x1 = max(x1, bbox[2])
                        y1 = max(y1, bbox[3])
                        span_size = span.get("size", 12)
                        span_y = bbox[1]
                        font_sizes.append(span_size)
                        text_spans.append(TextSpan(
                            text=text,
                            font_size=span_size,
                            y=span_y
                        ))
                
                avg_font_size = sum(font_sizes) / len(font_sizes) if font_sizes else 12
                
                # Detect superscripts: significantly smaller font + only digits
                # Compare each span's font to the LINE's average (not doc average)
                for tspan in text_spans:
                    is_small = tspan.font_size < avg_font_size * 0.85  # 90% threshold
                    is_digits = tspan.text.strip().isdigit()
                    if is_small and is_digits:
                        tspan.is_superscript = True
                
                # Collect all lines
                if line_text.strip():
                    all_lines.append(TextLine(
                        text=line_text.strip(),
                        x=x0,
                        y=y0,
                        width=x1 - x0,
                        height=y1 - y0,
                        page=page_num,
                        font_size=avg_font_size,
                        spans=text_spans
                    ))
    
    doc.close()

    # Sort by page, then by reading order (XY-Cut handles multi-column)
    sorted_lines = []
    pages = sorted(set(l.page for l in all_lines))
    for pg in pages:
        pg_lines = [l for l in all_lines if l.page == pg]
        sorted_lines.extend(xy_cut_sort(pg_lines))
    all_lines = sorted_lines

    logger.log(f"PDF: {len(page_info)} pages, {len(all_lines)} text lines extracted")

    return all_lines, page_info


def xy_cut_sort(lines, depth=0):
    """
    Recursive XY-Cut algorithm for correct reading order.
    Handles multi-column layouts by detecting vertical gaps (columns)
    and horizontal gaps (sections).
    """
    if len(lines) <= 1:
        return lines
    if depth > 20:  # prevent infinite recursion
        return sorted(lines, key=lambda l: (l.y, l.x))

    # Compute bounding region
    min_x = min(l.x for l in lines)
    max_x = max(l.x + l.width for l in lines)
    region_width = max_x - min_x

    # Find largest horizontal gap (split top/bottom)
    h_gap, h_pos = _find_largest_gap([l.y for l in lines], [l.height for l in lines])

    # Find largest vertical gap (split left/right = multi-column)
    # Exclude narrow outliers (< 10% of region width) to avoid page numbers skewing
    main_lines = [l for l in lines if l.width > region_width * 0.1] if region_width > 0 else lines
    if main_lines:
        v_gap, v_pos = _find_largest_gap([l.x for l in main_lines], [l.width for l in main_lines])
    else:
        v_gap, v_pos = 0, 0

    min_gap = 5.0  # minimum gap to consider a split (in PDF points)

    if max(h_gap, v_gap) < min_gap:
        return sorted(lines, key=lambda l: (l.y, l.x))

    if h_gap >= v_gap:
        # Horizontal cut: split into top and bottom
        top = [l for l in lines if l.y + l.height / 2 < h_pos]
        bottom = [l for l in lines if l.y + l.height / 2 >= h_pos]
        if not top or not bottom:
            return sorted(lines, key=lambda l: (l.y, l.x))
        return xy_cut_sort(top, depth + 1) + xy_cut_sort(bottom, depth + 1)
    else:
        # Vertical cut: split into left and right (multi-column!)
        left = [l for l in lines if l.x + l.width / 2 < v_pos]
        right = [l for l in lines if l.x + l.width / 2 >= v_pos]
        if not left or not right:
            return sorted(lines, key=lambda l: (l.y, l.x))
        return xy_cut_sort(left, depth + 1) + xy_cut_sort(right, depth + 1)


def _find_largest_gap(positions, sizes):
    """
    Find the largest gap between elements along one axis.
    Returns (gap_size, gap_midpoint).
    """
    if not positions:
        return 0, 0
    # Create sorted list of (start, end) intervals
    intervals = sorted(zip(positions, sizes), key=lambda x: x[0])
    best_gap = 0
    best_pos = 0
    for i in range(1, len(intervals)):
        prev_end = intervals[i - 1][0] + intervals[i - 1][1]
        curr_start = intervals[i][0]
        gap = curr_start - prev_end
        if gap > best_gap:
            best_gap = gap
            best_pos = (prev_end + curr_start) / 2
    return best_gap, best_pos


# ============================================================================
# LINE MERGING INTO PARAGRAPHS
# ============================================================================

def is_numbered_bullet(text: str) -> bool:
    """
    Check if text starts with a numbered bullet:
    - 1., 2., 3. (but NOT 18.000 which is a number)
    - I., II., IV. (roman numerals)
    - a), b), c) or 1), 2), 3)
    """
    stripped = _clean_extracted_text(text or "")
    if re.match(r'^\d{4}\.$', stripped):
        return False

    # Pattern for "1.", "2.", "1.1.", "1.2.3." etc - must be followed by space or end
    # Distinguish from "18.000" by checking what follows the dot
    if re.match(r'^\d+(?:\.\d+)*\.\s', stripped) or re.match(r'^\d+(?:\.\d+)*\.$', stripped):
        return True
    
    # Roman numerals: I., II., III., IV., V., VI., etc.
    if re.match(r'^[IVX]+\.\s', stripped) or re.match(r'^[IVX]+\.$', stripped):
        return True
    
    # a), b), c), đ) style. [^\W\d_] is a single Unicode letter.
    if re.match(r'^[^\W\d_]\)\s', stripped) or re.match(r'^[^\W\d_]\)$', stripped):
        return True
    
    # 1), 2), 3) style
    if re.match(r'^\d+\)\s', stripped) or re.match(r'^\d+\)$', stripped):
        return True

    # (1), (2), (a) style used in Vietnamese administrative lists
    if re.match(r'^\(\d+\)\s', stripped) or re.match(r'^\([^\W\d_]\)\s', stripped):
        return True
    
    return False


@dataclass
class ParagraphEdgeDecision:
    split: bool
    split_score: float
    merge_score: float
    reasons: List[str] = field(default_factory=list)
    features: Dict[str, object] = field(default_factory=dict)


def _terminal_punctuation(text: str) -> bool:
    return bool((text or "").rstrip().endswith(tuple('.?!:;)]}"')))


def _hard_terminal_punctuation(text: str) -> bool:
    return bool((text or "").rstrip().endswith(tuple('?!:')))


def _content_width(base_x: float, right_margin: float) -> float:
    return max(right_margin - base_x, 1.0)


def _median(values: List[float], default: float = 0.0) -> float:
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return default
    mid = len(vals) // 2
    if len(vals) % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2.0


def _percentile(values: List[float], q: float, default: float = 0.0) -> float:
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return default
    q = max(0.0, min(1.0, float(q)))
    idx = q * (len(vals) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(vals) - 1)
    frac = idx - lo
    return vals[lo] * (1.0 - frac) + vals[hi] * frac


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _line_centered_in_content_band(line: TextLine, left_margin: float, right_margin: float) -> bool:
    content_width = _content_width(left_margin, right_margin)
    line_center = line.x + line.width / 2.0
    page_center = left_margin + content_width / 2.0
    left_gap = max(0.0, line.x - left_margin)
    right_gap = max(0.0, right_margin - (line.x + line.width))
    return (
        line.width < content_width * 0.82
        and abs(line_center - page_center) < content_width * 0.10
        and abs(left_gap - right_gap) < content_width * 0.18
    )


def _looks_like_standalone_centered_title_line(
    text: str,
    line: TextLine,
    left_margin: float,
    right_margin: float,
) -> bool:
    stripped = _clean_extracted_text(text)
    if not stripped or len(stripped) > 80:
        return False
    content_width = _content_width(left_margin, right_margin)
    words = [w for w in re.split(r"\s+", stripped) if w]
    return (
        len(words) <= 6
        and line.width < content_width * 0.42
        and _mostly_uppercase_text(stripped)
        and _line_centered_in_content_band(line, left_margin, right_margin)
    )


def _line_style_compatible_for_heading_wrap(line1: TextLine, line2: TextLine) -> bool:
    h1 = max(float(getattr(line1, "height", 0.0) or 0.0), 1.0)
    h2 = max(float(getattr(line2, "height", 0.0) or 0.0), 1.0)
    height_ratio = min(h1, h2) / max(h1, h2)
    try:
        gray1 = int(getattr(line1, "fg_gray", 128))
        gray2 = int(getattr(line2, "fg_gray", 128))
        gray_compatible = (
            not (_has_known_gray(gray1) and _has_known_gray(gray2))
            or abs(gray1 - gray2) <= 48
        )
    except Exception:
        gray_compatible = True
    return height_ratio >= 0.72 and gray_compatible


def _looks_like_short_heading_tail(line: TextLine, next_width: float) -> bool:
    text = _clean_extracted_text(line.text or "")
    if not text:
        return False
    words = [word for word in re.split(r"\s+", text) if word]
    return len(words) <= 3 and line.width <= next_width * 0.28


def _looks_like_structural_heading_continuation(
    line1: TextLine,
    line2: TextLine,
    prev_base: float,
    prev_right: float,
    next_base: float,
    next_right: float,
    numbered_heading_prev: bool,
    numbered_next: bool,
    numbered_heading_next: bool,
    dot_bullet_next: bool,
    dash_next: bool,
) -> bool:
    """Detect wrapped heading lines using OCR block/style/layout, not fixed wording."""
    if not numbered_heading_prev:
        return False
    if getattr(line1, "block_id", 0) <= 0 or line1.block_id != getattr(line2, "block_id", 0):
        return False
    if line1.page != line2.page:
        return False
    text1 = (line1.text or "").rstrip()
    text2 = (line2.text or "").strip()
    if not text1 or not text2:
        return False
    if _hard_terminal_punctuation(text1):
        return False
    if numbered_next or numbered_heading_next or dot_bullet_next or dash_next:
        return False

    gap = line2.y - (line1.y + line1.height)
    compact_gap = gap <= max(line1.height, line2.height) * 0.75
    if not compact_gap:
        return False

    prev_width = _content_width(prev_base, prev_right)
    next_width = _content_width(next_base, next_right)
    prev_reaches_right = (line1.x + line1.width) >= prev_right - max(30.0, prev_width * 0.075)
    next_has_heading_width = line2.width >= next_width * 0.45
    next_in_content_band = (
        abs(line2.x - next_base) <= max(42.0, next_width * 0.10)
        or abs(line2.x - line1.x) <= max(42.0, next_width * 0.10)
    )
    if not (prev_reaches_right or next_has_heading_width):
        return False
    if not next_in_content_band:
        return False

    if _looks_like_short_heading_tail(line2, next_width):
        return True

    return _line_style_compatible_for_heading_wrap(line1, line2)


def score_paragraph_edge(
    line1: TextLine,
    line2: TextLine,
    base_x: float,
    right_margin: float,
    logger: Logger = None,
    page_info: dict = None,
    margin_map: Dict[int, Tuple[float, float]] = None,
) -> ParagraphEdgeDecision:
    """
    Score whether the edge between two OCR text lines is a paragraph break.

    This keeps the old public behavior deterministic, but makes the decision
    inspectable: geometry, lexical markers, and page-boundary evidence vote
    toward split or merge instead of relying on a single brittle condition.
    """
    text1 = (line1.text or "").rstrip()
    text2 = (line2.text or "").strip()
    reasons: List[str] = []
    features: Dict[str, object] = {}
    split_score = 0.0
    merge_score = 0.0

    if not text1 or not text2:
        return ParagraphEdgeDecision(True, 100.0, 0.0, ["empty_text"], features)

    prev_base, prev_right = (margin_map or {}).get(line1.page, (base_x, right_margin))
    next_base, next_right = (margin_map or {}).get(line2.page, (base_x, right_margin))
    prev_width = _content_width(prev_base, prev_right)
    next_width = _content_width(next_base, next_right)
    indent_threshold = max(24.0, next_width * 0.055)
    right_threshold = max(30.0, prev_width * 0.075)

    line1_right_edge = line1.x + line1.width
    same_page = line1.page == line2.page
    adjacent_page = line2.page == line1.page + 1
    prev_reaches_right = line1_right_edge >= prev_right - right_threshold
    prev_short = line1.width / prev_width < 0.25
    near_same_left = abs(line2.x - line1.x) <= indent_threshold
    next_body_left = (
        abs(line2.x - next_base) <= max(36.0, next_width * 0.08)
        or abs(line2.x - line1.x) <= max(42.0, next_width * 0.08)
    )
    has_start_indent = line2.x > next_base + indent_threshold
    terminal = _terminal_punctuation(text1)
    hard_terminal = _hard_terminal_punctuation(text1)
    numbered_next = is_numbered_bullet(text2)
    numbered_heading_next = _looks_like_numbered_heading(text2)
    numbered_heading_prev = _looks_like_numbered_heading(text1)
    dash_next = _starts_with_dash_marker(text2)
    dash_continuation = (
        _dash_marker_can_continue_previous_sentence(text1, text2)
        and not numbered_heading_prev
    )
    dot_bullet_next = text2.startswith("\u2022")
    starts_lower = text2[0].islower()
    starts_alpha = text2[0].isalpha()
    starts_digit = text2[0].isdigit()
    prev_centered_title = _looks_like_standalone_centered_title_line(text1, line1, prev_base, prev_right)
    next_centered = _line_centered_in_content_band(line2, next_base, next_right)
    structural_heading_wrap = _looks_like_structural_heading_continuation(
        line1,
        line2,
        prev_base,
        prev_right,
        next_base,
        next_right,
        numbered_heading_prev,
        numbered_next,
        numbered_heading_next,
        dot_bullet_next,
        dash_next,
    )

    features.update(
        {
            "same_page": same_page,
            "adjacent_page": adjacent_page,
            "prev_reaches_right": prev_reaches_right,
            "prev_short": prev_short,
            "near_same_left": near_same_left,
            "next_body_left": next_body_left,
            "has_start_indent": has_start_indent,
            "terminal": terminal,
            "hard_terminal": hard_terminal,
            "numbered_next": numbered_next,
            "numbered_heading_next": numbered_heading_next,
            "numbered_heading_prev": numbered_heading_prev,
            "dash_next": dash_next,
            "dash_continuation": dash_continuation,
            "dot_bullet_next": dot_bullet_next,
            "prev_centered_title": prev_centered_title,
            "next_centered": next_centered,
            "structural_heading_wrap": structural_heading_wrap,
        }
    )

    if numbered_next or numbered_heading_next:
        split_score += 8.0
        reasons.append("next_line_is_list_or_numbered_heading")
    if dot_bullet_next:
        split_score += 8.0
        reasons.append("next_line_is_dot_bullet")
    if dash_next:
        if numbered_heading_prev:
            split_score += 8.0
            reasons.append("dash_list_after_numbered_heading")
        elif dash_continuation:
            merge_score += 6.0
            reasons.append("dash_continues_unfinished_sentence")
        else:
            split_score += 6.0
            reasons.append("dash_starts_list_item")

    if prev_centered_title and (next_centered or line2.width >= next_width * 0.35):
        split_score += 6.0
        reasons.append("standalone_centered_title")

    if numbered_heading_prev:
        same_heading_wrap = (
            structural_heading_wrap
            or (
                near_same_left
                and _mostly_uppercase_text(text1)
                and _mostly_uppercase_text(text2)
                and not _terminal_punctuation(text2)
            )
        )
        if same_heading_wrap:
            merge_score += 4.0
            reasons.append("numbered_heading_wrap")
        else:
            split_score += 5.0
            reasons.append("previous_line_is_complete_numbered_heading")

    if not same_page:
        if not adjacent_page or not page_info:
            split_score += 6.0
            reasons.append("non_adjacent_or_unknown_page_boundary")
        else:
            prev_page_height = page_info.get(line1.page, {}).get("height", 842)
            next_page_height = page_info.get(line2.page, {}).get("height", 842)
            prev_footnote_top = page_info.get(line1.page, {}).get("footnote_top_y")
            prev_before_footnote_band = False
            if prev_footnote_top:
                footnote_gap = max(line1.height * 2.0, prev_page_height * 0.035)
                prev_before_footnote_band = (
                    line1.y < prev_footnote_top
                    and (line1.y + line1.height) >= prev_footnote_top - footnote_gap
                )
            prev_near_bottom = (
                (line1.y + line1.height) >= prev_page_height * 0.68
                or prev_before_footnote_band
            )
            next_near_top = line2.y <= next_page_height * 0.35
            features["prev_near_bottom"] = prev_near_bottom
            features["next_near_top"] = next_near_top
            features["prev_before_footnote_band"] = prev_before_footnote_band
            if prev_near_bottom and next_near_top:
                merge_score += 2.0
                reasons.append(
                    "adjacent_page_boundary_before_footnote"
                    if prev_before_footnote_band
                    else "adjacent_page_boundary_geometry"
                )
            else:
                split_score += 4.0
                reasons.append("not_page_boundary_continuation_band")

            if terminal:
                split_score += 5.0
                reasons.append("previous_text_has_terminal_punctuation")
            else:
                merge_score += 2.0
                reasons.append("previous_text_unfinished")

            if prev_reaches_right:
                merge_score += 3.0
                reasons.append("previous_line_reaches_body_right")
            else:
                split_score += 1.5
                reasons.append("previous_line_ends_short")

            if next_body_left:
                merge_score += 2.0
                reasons.append("next_line_starts_in_body_band")
            else:
                split_score += 2.5
                reasons.append("next_line_outside_body_band")

            if starts_lower:
                merge_score += 2.5
                reasons.append("next_line_starts_lowercase")
            elif starts_alpha and not terminal:
                merge_score += 1.0
                reasons.append("next_line_alpha_after_unfinished_text")
            elif starts_digit:
                split_score += 1.0
                reasons.append("next_line_starts_digit")
    else:
        gap = line2.y - (line1.y + line1.height)
        gap_ratio = gap / max(line1.height, 1.0)
        features["vertical_gap"] = gap
        features["vertical_gap_ratio"] = gap_ratio

        if gap_ratio > 1.7:
            split_score += 4.0
            reasons.append("large_vertical_gap")
        elif gap_ratio <= 0.9:
            merge_score += 0.75
            reasons.append("normal_line_gap")

        if not has_start_indent and prev_reaches_right:
            merge_score += 3.0
            reasons.append("block_wrap_geometry")
        if near_same_left and prev_reaches_right:
            merge_score += 1.5
            reasons.append("same_left_and_previous_full")

        if has_start_indent:
            if starts_lower or (starts_digit and not numbered_next and not terminal):
                merge_score += 2.0
                reasons.append("indented_continuation_after_unfinished_text")
            else:
                split_score += 2.5
                reasons.append("new_start_indent")

        if not prev_reaches_right:
            if hard_terminal:
                split_score += 3.0
                reasons.append("short_previous_line_with_hard_terminal")
            elif terminal:
                split_score += 2.0
                reasons.append("short_previous_line_with_terminal")
            elif prev_short:
                split_score += 2.5
                reasons.append("very_short_previous_line")
            else:
                merge_score += 2.0
                reasons.append("unfinished_short_previous_line")

        if hard_terminal:
            split_score += 1.5
            reasons.append("hard_terminal_punctuation")
        elif not terminal:
            merge_score += 1.0
            reasons.append("no_terminal_punctuation")
        if starts_lower:
            merge_score += 2.0
            reasons.append("next_line_starts_lowercase")

    split = split_score > merge_score
    decision = ParagraphEdgeDecision(split, split_score, merge_score, reasons, features)
    if logger and os.environ.get("OCRTOOL_DEBUG_PARAGRAPH_EDGE") == "1":
        logger.log(
            "Paragraph edge: "
            f"{'SPLIT' if decision.split else 'MERGE'} "
            f"split={decision.split_score:.2f} merge={decision.merge_score:.2f} "
            f"reasons={','.join(decision.reasons)}"
        )
    return decision


def should_split_paragraph(line1: TextLine, line2: TextLine, base_x: float, right_margin: float, logger: Logger = None) -> bool:
    """Compatibility wrapper for callers that only need the split/merge bool."""
    return score_paragraph_edge(line1, line2, base_x, right_margin, logger).split


def _set_merged_last_line(first_line: TextLine, last_line: TextLine) -> None:
    setattr(first_line, "_merged_last_line", last_line)


def _get_merged_last_line(first_line: TextLine) -> TextLine:
    return getattr(first_line, "_merged_last_line", first_line)


def _set_merged_lines(first_line: TextLine, lines: List[TextLine]) -> None:
    setattr(first_line, "_merged_lines", list(lines or [first_line]))


def _get_merged_lines(first_line: TextLine) -> List[TextLine]:
    lines = getattr(first_line, "_merged_lines", None)
    if lines:
        return list(lines)
    return [first_line]


def _should_heal_paragraph_fragment(
    prev_text: str,
    prev_first_line: TextLine,
    next_text: str,
    next_first_line: TextLine,
    margin_map: Dict[int, Tuple[float, float]],
    page_info: dict,
) -> bool:
    """Merge OCR/block fragments that are clearly one running sentence."""
    prev_text = (prev_text or "").rstrip()
    next_text = (next_text or "").lstrip()
    if not prev_text or not next_text:
        return False
    if getattr(prev_first_line, "is_footnote", False) or getattr(next_first_line, "is_footnote", False):
        return False
    if is_numbered_bullet(next_text) or next_text[0] in '•':
        return False
    if next_text[0] in '-–' and not _dash_marker_can_continue_previous_sentence(prev_text, next_text):
        return False
    if _looks_like_numbered_heading(next_text):
        return False

    prev_last_line = _get_merged_last_line(prev_first_line)
    base_x, right_margin = margin_map.get(next_first_line.page, (0, 500))
    decision = score_paragraph_edge(
        prev_last_line,
        next_first_line,
        base_x,
        right_margin,
        page_info=page_info,
        margin_map=margin_map,
    )
    return not decision.split


def _heal_fragmented_paragraphs(
    paragraphs: List[Tuple[str, TextLine, bool]],
    margin_map: Dict[int, Tuple[float, float]],
    page_info: dict,
    logger: Logger = None,
) -> List[Tuple[str, TextLine, bool]]:
    if not paragraphs:
        return paragraphs

    healed: List[Tuple[str, TextLine, bool]] = []
    merged_count = 0
    for para in paragraphs:
        if not healed:
            healed.append(para)
            continue

        prev_text, prev_first_line, prev_is_footnote = healed[-1]
        next_text, next_first_line, next_is_footnote = para
        if (
            not prev_is_footnote
            and not next_is_footnote
            and _should_heal_paragraph_fragment(
                prev_text,
                prev_first_line,
                next_text,
                next_first_line,
                margin_map,
                page_info,
            )
        ):
            healed[-1] = (
                prev_text.rstrip() + " " + next_text.lstrip(),
                prev_first_line,
                prev_is_footnote,
            )
            _set_merged_lines(
                prev_first_line,
                _get_merged_lines(prev_first_line) + _get_merged_lines(next_first_line),
            )
            _set_merged_last_line(prev_first_line, _get_merged_last_line(next_first_line))
            merged_count += 1
        else:
            healed.append(para)

    if logger and merged_count:
        logger.log(f"Healed {merged_count} OCR paragraph fragments across block/page boundaries")
    return healed


def is_page_number_text(text: str) -> bool:
    """Check if text looks like a page number."""
    text = text.strip()
    if not text:
        return False
    if len(text) >= 5:
        return False
    # Only digits, or digits + special chars
    if re.match(r'^[\d!@#$%^&*():;,.\-]+$', text):
        # Must have at least one digit
        return bool(re.search(r'\d', text))
    return False


def is_page_number(line: TextLine, page_lines: List[TextLine]) -> bool:
    """
    Check if line is a page number:
    - Only digits, or digits + special chars (!@#$%^&*():...)
    - Less than 5 characters
    - First or last line of the page
    """
    text = line.text.strip()
    
    if not is_page_number_text(text):
        return False
    
    # Must have at least one digit
    if not re.search(r'\d', text):
        return False
    
    # Relaxed rule: if it's very short (< 3 chars) and looks like a number/symbol, allow it to be filtered
    # regardless of position (catches noise like "7:", "1:")
    if len(text) < 3:
        return True

    # Must be first or last line of the page
    same_page_lines = [l for l in page_lines if l.page == line.page]
    if not same_page_lines:
        return False
    
    same_page_lines.sort(key=lambda l: l.y)
    is_first = line == same_page_lines[0]
    is_last = line == same_page_lines[-1]
    
    return is_first or is_last


def filter_page_numbers(lines: List[TextLine], logger: Logger) -> List[TextLine]:
    """Remove page number lines."""
    filtered = [line for line in lines if not is_page_number(line, lines)]
    removed = len(lines) - len(filtered)
    if removed > 0:
        logger.log(f"Removed {removed} page number lines")
    return filtered


def _has_spanning_header_title_region(page: int,
                                      page_width: float,
                                      page_height: float,
                                      layout_regions_by_page: Optional[Dict[int, List[dict]]]) -> bool:
    for region in (layout_regions_by_page or {}).get(page, []):
        if region.get("type") != "title":
            continue
        bbox = region.get("bbox_pdf")
        if not bbox or len(bbox) < 4:
            continue
        x0, y0, x1, y1 = (float(v) for v in bbox[:4])
        if (
            y0 <= page_height * 0.14
            and x0 <= page_width * 0.18
            and x1 >= page_width * 0.82
            and (y1 - y0) <= page_height * 0.08
        ):
            return True
    return False


def detect_dual_column_headers(lines: List[TextLine],
                               page_info: dict,
                               logger: Logger,
                               layout_regions_by_page: Optional[Dict[int, List[dict]]] = None) -> Dict[int, Tuple[List[TextLine], List[TextLine]]]:
    """
    Detect dual-column headers at top of pages (common in VN government docs).
    Heuristic: two groups of lines in top 20% of page, one left of center, one right,
    with overlapping Y ranges.
    Returns: {page_num: (left_lines, right_lines)}
    """
    def looks_like_dual_header(left_lines: List[TextLine], right_lines: List[TextLine]) -> bool:
        left_emphasis = [l for l in left_lines if _looks_like_header_emphasis_line(l.text)]
        right_emphasis = [l for l in right_lines if _looks_like_header_emphasis_line(l.text)]
        if not left_emphasis or not right_emphasis:
            return False

        left_y0 = min(l.y for l in left_emphasis)
        left_y1 = max(l.y + l.height for l in left_emphasis)
        right_y0 = min(l.y for l in right_emphasis)
        right_y1 = max(l.y + l.height for l in right_emphasis)
        overlaps_vertically = min(left_y1, right_y1) >= max(left_y0, right_y0) - 24
        starts_near_same_band = abs(left_y0 - right_y0) <= 60
        return overlaps_vertically and starts_near_same_band

    result = {}
    pages = set(l.page for l in lines)
    for pg in pages:
        pg_height = page_info.get(pg, {}).get("height", 842)
        pg_width = page_info.get(pg, {}).get("width", 595)
        page_center = pg_width / 2
        half_width = page_center
        if _has_spanning_header_title_region(pg, pg_width, pg_height, layout_regions_by_page):
            continue

        # Step 1: Find all lines in the top area
        y_threshold = pg_height * 0.20
        top_lines = [l for l in lines if l.page == pg and l.y < y_threshold]
        if len(top_lines) < 3:
            continue

        # Step 2: Split into true left/right header zones. Centered titles
        # can cross the page center, so require right-column lines to start
        # near the right half instead of using center-point alone.
        all_left = sorted([
            l for l in top_lines
            if l.x + l.width / 2 < page_center - 8 and l.width < half_width * 0.95
        ], key=lambda l: l.y)
        all_right = sorted([
            l for l in top_lines
            if l.x >= page_center - 15 and l.width < half_width * 0.95
        ], key=lambda l: l.y)

        if not all_left or not all_right:
            continue
        if not looks_like_dual_header(all_left, all_right):
            continue

        # Step 3: Only keep left lines that overlap Y range with right group
        # with a small allowance for left-only "Số..." lines under the header.
        ry_max = max(l.y + l.height for l in all_right)
        y_tolerance = 30
        left = [l for l in all_left if l.y < ry_max + y_tolerance]
        right = all_right

        if not left or not right:
            continue

        # Step 4: Verify this is a real dual-column HEADER (not body text)
        # Both groups must have SHORT lines (< 60% of their half-width)
        # Long body text lines would span most of their half
        left_avg_ratio = sum(l.width / half_width for l in left) / len(left) if left else 1
        right_avg_ratio = sum(l.width / half_width for l in right) / len(right) if right else 1
        if left_avg_ratio > 0.85 or right_avg_ratio > 0.85:
            continue  # Lines too wide — this is body text, not header

        ry_min = min(l.y for l in right)
        ly_max = max(l.y + l.height for l in left)
        overlap = min(ly_max, ry_max) - max(min(l.y for l in left), ry_min)
        if overlap > 0:
            result[pg] = (left, right)
            logger.log(f"Detected dual-column header on page {pg}: {len(left)}L + {len(right)}R lines")

    return result


def detect_dual_column_footers(lines: List[TextLine], page_info: dict, logger: Logger) -> Dict[int, Tuple[List[TextLine], List[TextLine]]]:
    """
    Detect bottom recipient/signature blocks rendered as two columns.
    """
    def norm_text(text: str) -> str:
        text = unicodedata.normalize("NFD", text.upper())
        return "".join(ch for ch in text if unicodedata.category(ch) != "Mn")

    result = {}
    pages = set(l.page for l in lines)
    for pg in pages:
        pg_height = page_info.get(pg, {}).get("height", 842)
        pg_width = page_info.get(pg, {}).get("width", 595)
        page_center = pg_width / 2
        page_lines = [l for l in lines if l.page == pg]
        anchors = [l for l in page_lines if norm_text(l.text).startswith("NOI NHAN")]
        if not anchors:
            continue

        anchor = sorted(anchors, key=lambda l: l.y)[-1]
        top_y = max(0, anchor.y - 12)
        bottom_y = min(pg_height * 0.95, anchor.y + 180)
        candidates = [l for l in page_lines if top_y <= l.y <= bottom_y]

        left = sorted(
            [l for l in candidates if l.x + l.width / 2 < page_center],
            key=lambda l: (l.y, l.x)
        )
        right = sorted(
            [l for l in candidates if l.x + l.width / 2 >= page_center],
            key=lambda l: (l.y, l.x)
        )
        right = [l for l in right if _is_signature_footer_line(l.text)]
        if not left or not right:
            continue

        has_signature_title = any(_looks_like_header_emphasis_line(l.text) for l in right)
        has_signature_name = any(_looks_like_person_name(l.text) for l in right)
        if not has_signature_title or not has_signature_name:
            continue

        result[pg] = (left, right)
        logger.log(f"Detected dual-column footer on page {pg}: {len(left)}L + {len(right)}R lines")

    return result


def _borderless_table_xml(table) -> None:
    """Remove table/cell borders that Word may inherit from default styles."""
    tbl = table._tbl
    tblPr = tbl.tblPr
    if tblPr is None:
        tblPr = OxmlElement("w:tblPr")
        tbl.insert(0, tblPr)
    for existing in list(tblPr.findall(qn("w:tblBorders"))):
        tblPr.remove(existing)

    borders = OxmlElement("w:tblBorders")
    for edge in ('top', 'left', 'bottom', 'right', 'insideH', 'insideV'):
        el = OxmlElement(f'w:{edge}')
        el.set(qn('w:val'), 'none')
        el.set(qn('w:sz'), '0')
        el.set(qn('w:space'), '0')
        el.set(qn('w:color'), 'auto')
        borders.append(el)
    tblPr.append(borders)

    for row in table.rows:
        for cell in row.cells:
            tcPr = cell._tc.get_or_add_tcPr()
            for existing in list(tcPr.findall(qn("w:tcBorders"))):
                tcPr.remove(existing)
            tc_borders = OxmlElement("w:tcBorders")
            for edge in ('top', 'left', 'bottom', 'right'):
                el = OxmlElement(f'w:{edge}')
                el.set(qn('w:val'), 'none')
                el.set(qn('w:sz'), '0')
                el.set(qn('w:space'), '0')
                el.set(qn('w:color'), 'auto')
                tc_borders.append(el)
            tcPr.append(tc_borders)


def add_dual_header_table(doc: Document, left_lines: List[TextLine], right_lines: List[TextLine],
                          page_width_pt: float, logger: Logger):
    """Render dual-column header as a borderless 1x2 table."""
    table = doc.add_table(rows=1, cols=2)
    _borderless_table_xml(table)

    # Column widths: derive from actual text extents of left and right groups
    left_max_x = max(l.x + l.width for l in left_lines) if left_lines else page_width_pt / 2
    right_min_x = min(l.x for l in right_lines) if right_lines else page_width_pt / 2
    # Gap between columns = right_start - left_end
    gap = max(right_min_x - left_max_x, 0)
    left_w = left_max_x + gap / 2  # each side takes half the gap
    right_w = page_width_pt - right_min_x + gap / 2
    total = left_w + right_w
    if total > 0:
        for row in table.rows:
            row.cells[0].width = Pt(left_w / total * page_width_pt)
            row.cells[1].width = Pt(right_w / total * page_width_pt)

    right_emphasis_indices = [
        idx for idx, line in enumerate(right_lines)
        if _looks_like_header_emphasis_line(line.text)
    ]
    right_underline_idx = right_emphasis_indices[0] if right_emphasis_indices else None

    # Left cell
    cell_l = table.cell(0, 0)
    cell_l.text = ""
    for i, line in enumerate(left_lines):
        para = cell_l.paragraphs[0] if i == 0 else cell_l.add_paragraph()
        para.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
        run = para.add_run(line.text)
        run.font.name = "Times New Roman"
        run.font.size = Pt(14)
        if _looks_like_header_emphasis_line(line.text):
            run.font.bold = True

    # Right cell
    cell_r = table.cell(0, 1)
    cell_r.text = ""
    for i, line in enumerate(right_lines):
        para = cell_r.paragraphs[0] if i == 0 else cell_r.add_paragraph()
        para.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
        run = para.add_run(line.text)
        run.font.name = "Times New Roman"
        run.font.size = Pt(14)
        if _looks_like_header_emphasis_line(line.text):
            run.font.bold = True
        if i == right_underline_idx:
            run.font.underline = True


def detect_footnotes(lines: List[TextLine], page_info: dict, logger: Logger) -> List[TextLine]:
    """
    Detect footnotes conservatively.

    OCR often reports wrapped tail fragments with smaller bboxes/font sizes,
    such as the last word of a normal paragraph. Those fragments must stay
    body text, not footnotes.
    """
    if not lines:
        return lines
    
    font_sizes = [line.font_size for line in lines if line.font_size > 0]
    avg_font = sum(font_sizes) / len(font_sizes) if font_sizes else 12
    
    footnote_count = 0

    def has_footnote_marker(text: str) -> bool:
        stripped = text.strip()
        return bool(
            re.match(r'^\d{1,2}\s+\S', stripped)
            or re.match(r'^[*]\s+\S', stripped)
        )

    def is_wrapped_body_fragment(prev_line: Optional[TextLine], line: TextLine) -> bool:
        if prev_line is None or prev_line.page != line.page:
            return False
        stripped = line.text.strip()
        if not stripped or has_footnote_marker(stripped) or is_numbered_bullet(stripped):
            return False
        if stripped.startswith(("-", "+")):
            return False

        if (
            line.block_id > 0
            and prev_line.block_id == line.block_id
            and prev_line.paragraph_id == line.paragraph_id
        ):
            return True

        prev_text = prev_line.text.rstrip()
        if not prev_text or prev_text.endswith((".", ":", ";", "?", "!", ")")):
            return False
        gap = line.y - (prev_line.y + prev_line.height)
        max_h = max(prev_line.height, line.height, 1.0)
        return -max_h * 0.25 <= gap <= max_h * 1.5

    prev_by_page: Dict[int, Optional[TextLine]] = {}
    
    for line in lines:
        page_height = page_info.get(line.page, {}).get("height", 800)
        page_width = page_info.get(line.page, {}).get("width", 595)
        prev_line = prev_by_page.get(line.page)
        prev_by_page[line.page] = line
        sem = (line.semantic_type or "").strip().lower()
        text_stripped = line.text.strip()
        
        # Primary criteria
        is_very_small_font = line.font_size < avg_font * 0.75
        is_small_font = line.font_size < avg_font * 0.88
        
        in_lower_part = line.y > page_height * 0.70
        in_bottom_band = line.y > page_height * 0.82
        
        # Bonus criteria
        starts_with_num = has_footnote_marker(line.text)
        if sem == "footnote":
            if line.y > page_height * 0.55:
                line.is_footnote = True
                footnote_count += 1
                continue
            line.is_footnote = False
            continue
        if prev_line is not None and getattr(prev_line, "is_footnote", False):
            gap = line.y - (prev_line.y + prev_line.height)
            max_h = max(prev_line.height, line.height, 1.0)
            x_close = abs(line.x - prev_line.x) <= page_width * 0.12
            x_continuation_indent = prev_line.x <= line.x <= prev_line.x + page_width * 0.16
            if (
                is_small_font
                and in_lower_part
                and -max_h * 0.25 <= gap <= max_h * 1.8
                and line.x <= page_width * 0.55
                and (x_close or x_continuation_indent)
            ):
                line.is_footnote = True
                footnote_count += 1
                continue
        text_lower = text_stripped.lower()
        is_recipient_footer = (
            text_lower.startswith("nơi ")
            or text_lower.startswith("noi ")
            or text_stripped.startswith("-")
            or text_lower == "trân trọng."
        )

        if (
            is_recipient_footer
            or is_numbered_bullet(text_stripped)
            or (
                sem in {
                    "title",
                    "plain text",
                    "table",
                    "table_caption",
                    "figure_caption",
                    "page-header",
                    "page-footer",
                }
                and not starts_with_num
            )
            or is_wrapped_body_fragment(prev_line, line)
        ):
            line.is_footnote = False
            continue

        if line.x > page_width * 0.50 and not starts_with_num:
            line.is_footnote = False
            continue

        if sem == "table_footnote" and is_small_font and in_lower_part:
            line.is_footnote = True
            footnote_count += 1
            continue
        
        # Score-based detection
        score = 0
        if is_very_small_font:
            score += 60
        elif is_small_font:
            score += 40
            
        if in_bottom_band:
            score += 35
        elif in_lower_part:
            score += 30
            
        if starts_with_num:
            score += 35
        elif not in_bottom_band:
            score -= 35
            
        # Logging for debug
        if "Quản lý văn bản điều hành" in line.text:
             logger.log(f"DEBUG FN CHECK: '{line.text[:30]}...'")
             logger.log(f"  Global Avg Font: {avg_font:.2f}")
             logger.log(f"  Line Font: {line.font_size:.2f} ({line.font_size/avg_font*100:.1f}%)")
             logger.log(f"  Is Small (<88%): {is_small_font}")
             logger.log(f"  Score: {score}")
        
        # Require either an explicit marker or true bottom-band placement; small
        # font alone is too noisy for OCR-generated PDFs.
        if score >= 90 and (starts_with_num or in_bottom_band):
            line.is_footnote = True
            footnote_count += 1
    
    if footnote_count > 0:
        logger.log(f"Detected {footnote_count} footnote lines (avg font: {avg_font:.1f})")
    
    return lines




def _normalize_text_for_compare(text: str) -> str:
    return re.sub(r"\s+", " ", _clean_extracted_text(text or "")).strip().lower()


def _has_known_gray(value: object) -> bool:
    try:
        gray = int(value)
    except Exception:
        return False
    return 0 <= gray <= 255 and gray != 128


def _gray_z(gray: float, median: float, iqr: float) -> float:
    return (float(median) - float(gray)) / max(float(iqr), 1.0)


def _line_is_visually_bold(line: TextLine) -> bool:
    """Infer whether the whole OCR line is visually bold."""
    try:
        line_gray = int(line.fg_gray)
    except Exception:
        return False
    if not _has_known_gray(line_gray):
        return False

    stats = getattr(line, "_page_style_stats", {}) or {}
    if stats.get("doc_known_gray_count", 0) < 80:
        return False
    cutoff = stats.get("doc_bold_gray_cutoff")
    if cutoff is None:
        return False

    median = float(stats.get("doc_gray_median", 128.0))
    iqr = max(float(stats.get("doc_gray_iqr", 0.0)), 1.0)
    z_cutoff = float(stats.get("doc_bold_z_cutoff", _gray_z(float(cutoff), median, iqr)))
    return line_gray <= float(cutoff) and _gray_z(line_gray, median, iqr) >= z_cutoff


def _word_is_visually_bold(
    word: dict,
    line: TextLine,
    *,
    allow_line_bold: bool = True,
    allow_word_bold: bool = True,
) -> bool:
    """Infer local bold from robustly-normalized foreground gray."""
    if not word:
        return False
    if allow_line_bold and _line_is_visually_bold(line):
        return True
    if not allow_word_bold:
        return False
    try:
        gray = int(word.get("fg_gray", 128) or 128)
    except Exception:
        return False
    if not _has_known_gray(gray):
        return False

    stats = getattr(line, "_page_style_stats", {}) or {}
    if stats.get("doc_known_gray_count", 0) < 80:
        return False

    doc_median = float(stats.get("doc_gray_median", 128.0))
    doc_iqr = max(float(stats.get("doc_gray_iqr", 0.0)), 1.0)
    doc_cutoff = stats.get("doc_bold_gray_cutoff")
    if doc_cutoff is None:
        return False

    # Groundtruth calibration showed document-level robust normalization is the
    # strongest signal. The cutoff itself remains data-driven: it is the dark
    # tail of the current document, expressed as a z score for comparability.
    doc_z = _gray_z(gray, doc_median, doc_iqr)
    doc_z_cutoff = float(stats.get("doc_bold_z_cutoff", _gray_z(float(doc_cutoff), doc_median, doc_iqr)))
    if gray > float(doc_cutoff) or doc_z < doc_z_cutoff:
        return False

    page_median = float(stats.get("gray_median", doc_median))
    page_iqr = max(float(stats.get("gray_iqr", doc_iqr)), 1.0)
    page_z = _gray_z(gray, page_median, page_iqr)
    page_cutoff = stats.get("page_bold_gray_cutoff")
    page_supports_dark = page_cutoff is None or gray <= float(page_cutoff) or page_z >= 0.75

    return page_supports_dark


def _line_visual_bold_ratio(line: TextLine) -> float:
    words = _word_items_for_line(line)
    if not words:
        return 0.0
    known = [word for word in words if _has_known_gray(word.get("fg_gray", 128))]
    if not known:
        return 0.0
    bold_count = sum(1 for word in known if _word_is_visually_bold(word, line))
    return bold_count / max(len(known), 1)


def _word_items_for_line(line: TextLine) -> List[dict]:
    items = getattr(line, "word_items", None)
    if items:
        return [item for item in items if _clean_extracted_text(str(item.get("text") or ""))]
    return []


def _merged_word_tokens(
    first_line: TextLine,
    *,
    allow_line_bold: bool = True,
    allow_word_bold: bool = True,
) -> List[Tuple[str, bool, bool]]:
    """Return (text, has_space_before, is_bold) tokens for a merged paragraph."""
    tokens: List[Tuple[str, bool, bool]] = []
    previous_has_space_after = False
    for line_idx, line in enumerate(_get_merged_lines(first_line)):
        words = _word_items_for_line(line)
        if not words:
            previous_has_space_after = True
            continue
        for word_idx, word in enumerate(words):
            text = _clean_extracted_text(str(word.get("text") or ""))
            if not text:
                continue
            has_space_before = bool(tokens) and (previous_has_space_after or word_idx == 0 or line_idx > 0)
            tokens.append((
                text,
                has_space_before,
                _word_is_visually_bold(
                    word,
                    line,
                    allow_line_bold=allow_line_bold,
                    allow_word_bold=allow_word_bold,
                ),
            ))
            previous_has_space_after = bool(word.get("has_space_after", True))
        previous_has_space_after = True
    return tokens


def _token_letter_count(text: str) -> int:
    return sum(1 for ch in text if ch.isalpha())


def _is_content_word_token(text: str) -> bool:
    return _token_letter_count(text) >= 2


def _token_ends_sentence(text: str) -> bool:
    return bool(re.search(r"[.!?][)\"']*$", (text or "").strip()))


def _token_ends_clause(text: str) -> bool:
    return bool(re.search(r"[.,:;!?][)\"']*$", (text or "").strip()))


def _looks_like_lead_list_token(text: str) -> bool:
    stripped = (text or "").strip()
    return stripped == "-" or bool(re.match(r"^\(?[0-9A-Za-z]+[\).]$", stripped))


def _next_bold_run_length(tokens: List[Tuple[str, bool, bool]], flags: List[bool], start: int) -> int:
    length = 0
    for idx in range(start, len(tokens)):
        if not flags[idx]:
            break
        length += 1
    return length


def _smooth_bold_tokens(tokens: List[Tuple[str, bool, bool]]) -> List[Tuple[str, bool, bool]]:
    """Stabilize OCR bold decisions using local run and sentence evidence."""
    if not tokens:
        return tokens

    flags = [bool(token[2]) for token in tokens]

    # Very short function-like words are noisy when they are the only dark
    # token in their immediate neighborhood. Clear them before using bold runs
    # as phrase evidence.
    for idx, (token_text, _, _) in enumerate(tokens):
        letters = _token_letter_count(token_text)
        if not flags[idx] or letters == 0 or letters > 3:
            continue
        original_neighbor_bold = (
            (idx > 0 and bool(tokens[idx - 1][2]))
            or (idx + 1 < len(tokens) and bool(tokens[idx + 1][2]))
        )
        if not original_neighbor_bold:
            flags[idx] = False

    # Administrative bullet/list items often bold the lead sentence, but OCR
    # foreground gray can miss a few words inside that sentence. Promote the
    # first sentence only when its own words already show a strong bold majority.
    first_idx = next((idx for idx, (text, _, _) in enumerate(tokens) if (text or "").strip()), 0)
    if first_idx < len(tokens) and _looks_like_lead_list_token(tokens[first_idx][0]):
        sentence_end = None
        for idx in range(first_idx + 1, len(tokens)):
            if _token_ends_sentence(tokens[idx][0]):
                sentence_end = idx
                break
        if sentence_end is not None and sentence_end > first_idx:
            content_indices = [
                idx for idx in range(first_idx + 1, sentence_end + 1)
                if _is_content_word_token(tokens[idx][0])
            ]
            if content_indices:
                bold_count = sum(1 for idx in content_indices if flags[idx])
                if bold_count >= 2 and bold_count / max(len(content_indices), 1) >= 0.45:
                    for idx in range(first_idx, sentence_end + 1):
                        flags[idx] = True

    # Fill short OCR misses inside an otherwise bold phrase.
    for idx, (token_text, _, _) in enumerate(tokens):
        if flags[idx] or _token_letter_count(token_text) < 4:
            continue
        prev_bold = idx > 0 and flags[idx - 1]
        next_bold = idx + 1 < len(tokens) and flags[idx + 1]
        prev2_bold = idx > 1 and flags[idx - 2]
        if prev_bold and next_bold:
            flags[idx] = True
        elif prev_bold and prev2_bold and _token_ends_clause(token_text):
            flags[idx] = True

    # Backfill the first word of a bold phrase when OCR missed it. Keep this
    # conservative: the candidate must be a real content word, and either the
    # following bold run is substantial or the pair forms a short clause.
    for idx, (token_text, _, _) in enumerate(tokens):
        if flags[idx] or _token_letter_count(token_text) < 4:
            continue
        next_run = _next_bold_run_length(tokens, flags, idx + 1)
        boundary_before = (
            idx == 0
            or flags[idx - 1]
            or _token_ends_clause(tokens[idx - 1][0])
            or not _is_content_word_token(tokens[idx - 1][0])
        )
        if next_run >= 3 or (next_run >= 2 and boundary_before):
            flags[idx] = True
        elif (
            next_run == 1
            and idx + 1 < len(tokens)
            and _token_ends_clause(tokens[idx + 1][0])
            and (idx == 0 or _token_ends_clause(tokens[idx - 1][0]) or not _is_content_word_token(tokens[idx - 1][0]))
        ):
            flags[idx] = True

    return [(text, has_space_before, flags[idx]) for idx, (text, has_space_before, _) in enumerate(tokens)]


def _try_add_word_formatted_text(
    para,
    text: str,
    first_line: TextLine,
    apply_run_format,
    *,
    bold: bool,
) -> bool:
    tokens = _merged_word_tokens(first_line, allow_line_bold=bold, allow_word_bold=bold)
    if not tokens:
        return False

    reconstructed_parts = []
    for token_text, has_space_before, _ in tokens:
        if has_space_before and reconstructed_parts:
            reconstructed_parts.append(" ")
        reconstructed_parts.append(token_text)
    reconstructed = "".join(reconstructed_parts)

    expected_norm = _normalize_text_for_compare(text)
    reconstructed_norm = _normalize_text_for_compare(reconstructed)
    if not expected_norm or not reconstructed_norm:
        return False

    if reconstructed_norm != expected_norm:
        return False

    smoothed_tokens: List[Tuple[str, bool, bool]] = []
    run_smoothed_tokens = _smooth_bold_tokens(tokens)
    for idx, (token_text, has_space_before, token_bold) in enumerate(run_smoothed_tokens):
        if token_bold:
            has_digit = any(ch.isdigit() for ch in token_text)
            letters_only = "".join(ch for ch in token_text if ch.isalpha())
            is_short_plain_word = (
                not has_digit
                and bool(letters_only)
                and len(letters_only) <= 12
                and not _mostly_uppercase_text(letters_only, threshold=0.85)
            )
            neighbor_bold = (
                (idx > 0 and run_smoothed_tokens[idx - 1][2])
                or (idx + 1 < len(run_smoothed_tokens) and run_smoothed_tokens[idx + 1][2])
            )
            if is_short_plain_word and not neighbor_bold:
                token_bold = False
        smoothed_tokens.append((token_text, has_space_before, token_bold))

    for token_text, has_space_before, token_bold in smoothed_tokens:
        run_text = (" " if has_space_before else "") + token_text
        run = para.add_run(run_text)
        apply_run_format(run, is_bold=True if bold or token_bold else False)
    return True


def add_text_with_superscripts(para, text: str, first_line: TextLine, is_footnote: bool,
                               bold: bool = False, font_size_pt: int = 14, italic: bool = False):
    """Add text to paragraph, preserving superscripts and OCR-inferred emphasis."""

    def _apply_run_format(run, size=None, is_bold=None, is_italic=None):
        """Apply common formatting to a run."""
        run.font.name = "Times New Roman"
        run.font.size = Pt(size if size else font_size_pt)
        if is_bold is not None:
            run.font.bold = is_bold
        elif bold:
            run.font.bold = True
        if is_italic is not None:
            run.font.italic = is_italic
        elif italic:
            run.font.italic = True

    # Collect superscript positions from spans
    superscript_texts = set()
    if first_line.spans:
        for span in first_line.spans:
            if span.is_superscript and span.text.strip():
                superscript_texts.add(span.text.strip())

    # For footnotes: italic, size 11, leading number superscript
    if is_footnote:
        match = re.match(r'^(\d+)\s*', text)
        if match:
            run = para.add_run(match.group(1))
            _apply_run_format(run, size=11, is_bold=False, is_italic=False)
            run.font.superscript = True
            rest = text[len(match.group(0)):]
            if rest:
                run = para.add_run(" " + rest)
                _apply_run_format(run, size=11, is_bold=False, is_italic=True)
        else:
            run = para.add_run(text)
            _apply_run_format(run, size=11, is_bold=False, is_italic=True)
        return

    # Normal paragraph - check for inline superscripts
    if not superscript_texts:
        if _try_add_word_formatted_text(para, text, first_line, _apply_run_format, bold=bold):
            return
        run = para.add_run(text)
        _apply_run_format(run)
        return

    # Parse text and format superscripts
    i = 0
    while i < len(text):
        found = False
        for sup in superscript_texts:
            if text[i:i+len(sup)] == sup:
                before_ok = (i == 0 or not text[i-1].isdigit())
                after_ok = (i + len(sup) >= len(text) or not text[i + len(sup)].isdigit())
                if before_ok and after_ok:
                    run = para.add_run(sup)
                    _apply_run_format(run, size=9)
                    run.font.superscript = True
                    i += len(sup)
                    found = True
                    break
        if not found:
            next_pos = len(text)
            for sup in superscript_texts:
                pos = text.find(sup, i + 1)
                if pos != -1 and pos < next_pos:
                    next_pos = pos
            normal = text[i:next_pos]
            if normal:
                run = para.add_run(normal)
                _apply_run_format(run)
            i = next_pos if next_pos > i else i + 1

def merge_raw_paragraphs(
    lines: List[TextLine],
    margin_map: Dict[int, Tuple[float, float]],
    logger: Logger = None,
    page_info: dict = None,
) -> List[Tuple[str, TextLine, bool]]:
    """
    Core merging logic: split or merge lines based on indentation and gap.
    Returns: List of (text, first_line, is_footnote).
    """
    if not lines:
        return []
        
    raw_paragraphs = []
    current_text = lines[0].text
    current_first_line = lines[0]
    current_lines = [lines[0]]
    
    for i in range(1, len(lines)):
        # Get margins for current line's page
        # Prioritize line 2 (current line) page for margin context
        page = lines[i].page
        base_x, max_right = margin_map.get(page, (0, 500))
        
        decision = score_paragraph_edge(
            lines[i - 1],
            lines[i],
            base_x,
            max_right,
            logger,
            page_info=page_info,
            margin_map=margin_map,
        )
        if decision.split:
            _set_merged_last_line(current_first_line, lines[i - 1])
            _set_merged_lines(current_first_line, current_lines)
            raw_paragraphs.append((current_text, current_first_line, getattr(current_first_line, 'is_footnote', False)))
            current_text = lines[i].text
            current_first_line = lines[i]
            current_lines = [lines[i]]
        else:
            current_text += " " + lines[i].text
            current_lines.append(lines[i])
    
    _set_merged_last_line(current_first_line, lines[-1])
    _set_merged_lines(current_first_line, current_lines)
    raw_paragraphs.append((current_text, current_first_line, getattr(current_first_line, 'is_footnote', False)))
    return raw_paragraphs


def is_numeric_cell(text: str) -> bool:
    """
    Check if cell content should be treated as numeric (Center Aligned).
    Rule: Contains NO alphabet characters (A-Z, a-z, Vietnamese).
    Allowed: Digits, punctuation, symbols.
    """
    if not text:
        return False # Empty -> Default Left? Or irrelevant.
        
    for char in text:
        if char.isalpha():
            return False
    return True


def merge_lines_to_paragraphs(lines: List[TextLine], page_info: dict, logger: Logger) -> List[Tuple[str, TextLine, bool]]:
    """
    Merge lines into paragraphs, handling footnote reordering.
    Returns: List of (merged_text, first_line, is_footnote)
    """
    if not lines:
        return []
    
    # First filter out page numbers
    lines = filter_page_numbers(lines, logger)
    
    # Then detect footnotes
    lines = detect_footnotes(lines, page_info, logger)
    
    margin_map = {}
    
    # Calculate margins per page
    # Group lines by page first
    page_lines_map = {}
    for line in lines:
        if line.page not in page_lines_map:
            page_lines_map[line.page] = []
        page_lines_map[line.page].append(line)
        
    for page, p_lines in page_lines_map.items():
        x_positions = [l.x for l in p_lines]
        if not x_positions:
            margin_map[page] = (0, 500)
            continue
            
        # Left Margin (Base X): Use MODE (Most Frequent) instead of min/percentile
        # This avoids headers/footers/sidebars skewing the "Body Text" margin.
        from collections import Counter
        
        all_lefts = sorted(x_positions)
        
        # Round to nearest 2.0 to group similar indentations
        x_rounded = [round(x / 2.0) * 2.0 for x in all_lefts]
        common = Counter(x_rounded).most_common(1)
        
        if common:
            mode_base_x = common[0][0]
            # Use mode as base_x. 
            # Note: We want the "Main Body" left. 
            # If there are valid paragraphs to the left of the mode (e.g. outdented headers), 
            # using mode might make them look "negative indented"? code usually handles x > base.
            # But for SPLIT logic, `has_start_indent` checks `line.x > base + threshold`.
            # If line.x == mode, and base == mode, then indent is 0. -> Merge. Correct.
            base_x = mode_base_x
        else:
             sorted_lefts = sorted(all_lefts)
             base_x = sorted_lefts[int(len(sorted_lefts) * 0.05)]

        # Right Margin: Use a combination of mode and robust max
        all_rights = sorted([l.x + l.width for l in p_lines])
        robust_max = all_rights[int(len(all_rights) * 0.98)] if all_rights else 500
        
        # Calculate mode of right edges (rounded to 5.0)
        from collections import Counter
        rights_rounded = [round(r / 5.0) * 5.0 for r in all_rights]
        common_rights = Counter(rights_rounded).most_common(3)
        
        # If the most common right edge is significant and slightly less than robust_max,
        # it's likely the "Main Body" justified margin, not the absolute max (which could be a header).
        max_right = robust_max
        if common_rights:
            mode_right, count = common_rights[0]
            # If mode has ≥ 3 lines and is within 100 units of max, and more frequent than absolute max
            if count >= 3 and mode_right > robust_max - 50:
                max_right = mode_right
            elif robust_max > 0:
                max_right = robust_max
        
        margin_map[page] = (base_x, max_right)
        logger.log(f"Page {page} Margins: Left={base_x:.1f}, Right={max_right:.1f} (RobustMax={robust_max:.1f})")

    footnote_lines = [l for l in lines if l.is_footnote]
    merge_page_info = {
        pg: dict(info or {})
        for pg, info in (page_info or {}).items()
    }
    if footnote_lines:
        footnote_tops: Dict[int, float] = {}
        for line in footnote_lines:
            footnote_tops[line.page] = min(footnote_tops.get(line.page, line.y), line.y)
        for pg, top_y in footnote_tops.items():
            merge_page_info.setdefault(pg, dict((page_info or {}).get(pg, {})))
            merge_page_info[pg]["footnote_top_y"] = top_y

    def merge_subset(subset: List[TextLine]) -> List[Tuple[str, TextLine, bool]]:
        if not subset:
            return []
        # If block_id metadata available, pre-group by (page, block_id, paragraph_id).
        # Lines in different blocks should NOT merge (Screen AI already grouped them).
        if any(l.block_id > 0 for l in subset):
            from itertools import groupby
            merged = []
            for key, group in groupby(subset, key=lambda l: (l.page, l.block_id, l.paragraph_id)):
                group_lines = list(group)
                merged.extend(merge_raw_paragraphs(group_lines, margin_map, logger, merge_page_info))
            return _heal_fragmented_paragraphs(merged, margin_map, merge_page_info, logger)
        return _heal_fragmented_paragraphs(
            merge_raw_paragraphs(subset, margin_map, logger, merge_page_info),
            margin_map,
            merge_page_info,
            logger,
        )

    if footnote_lines:
        # Footnotes must not sit between two body fragments. Merge body text
        # without footnotes first, then insert each footnote after the nearest
        # preceding paragraph on the same page.
        body_paragraphs = merge_subset([l for l in lines if not l.is_footnote])
        footnote_paragraphs = merge_subset(footnote_lines)

        insertions: Dict[int, List[Tuple[str, TextLine, bool]]] = {}
        for fn_para in footnote_paragraphs:
            _, fn_line, _ = fn_para
            target_idx = -1
            for idx, (_, body_line, _) in enumerate(body_paragraphs):
                body_last = _get_merged_last_line(body_line)
                starts_before_footnote = (
                    body_line.page < fn_line.page
                    or (body_line.page == fn_line.page and body_line.y <= fn_line.y)
                )
                spans_footnote_page = body_line.page <= fn_line.page <= body_last.page
                ends_before_footnote = (
                    body_last.page < fn_line.page
                    or (body_last.page == fn_line.page and body_last.y <= fn_line.y)
                )
                if starts_before_footnote and (spans_footnote_page or ends_before_footnote):
                    target_idx = idx
            if target_idx < 0:
                for idx, (_, body_line, _) in enumerate(body_paragraphs):
                    body_last = _get_merged_last_line(body_line)
                    if body_last.page <= fn_line.page:
                        target_idx = idx
            if target_idx < 0:
                target_idx = 0
            insertions.setdefault(target_idx, []).append(fn_para)

        final_paragraphs = []
        for idx, para in enumerate(body_paragraphs):
            final_paragraphs.append(para)
            final_paragraphs.extend(insertions.get(idx, []))
    else:
        final_paragraphs = merge_subset(lines)

    logger.log(f"Merged {len(lines)} lines into {len(final_paragraphs)} paragraphs")

    return final_paragraphs


def clean_ocr_cell_text(text: str) -> str:
    """
    Clean OCR text:
    1. Remove leading pipe characters.
    2. Smart merge lines (auto-heal wrapped text).
    """
    text = text.strip()
    # Remove leading pipes from start of text (legacy check)
    while text.startswith('|'):
        text = text[1:].strip()
        
    # Split by newline to handle internal lines
    if '\n' in text:
        lines = [l.strip() for l in text.split('\n')]
        # Filter empty lines? No, keep structure but clean pipes
        cleaned_lines = []
        for l in lines:
            l = l.strip()
            while l.startswith('|'):
                l = l[1:].strip()
            if l:
                cleaned_lines.append(l)
        lines = cleaned_lines
    else:
        # Single line case
        lines = [text]
        
    if not lines:
        return ""
        
    # Smart merge
    merged = [lines[0]]
    import re
    
    for line in lines[1:]:
        prev = merged[-1]
        should_merge = False
        
        # Rule 1: Starts with lowercase -> Merge
        if line and line[0].islower():
            should_merge = True
        
        # Rule 2: Prev line indicates continuation (no punctuation)
        # AND line doesn't look like a list item
        elif prev and prev[-1] not in '.!?;:':
            is_list_item = re.match(r'^(\d+\.|[a-zA-Z]\)|[-+•➢])', line)
            if not is_list_item:
                should_merge = True
        
        if should_merge:
            merged[-1] = prev + " " + line
        else:
            merged.append(line)
    
    text = "\n".join(merged)
        
    return text


# ============================================================================
# CAMELOT TABLE DETECTION
# ============================================================================




def sort_lines_reading_order(lines: List[TextLine]) -> List[TextLine]:
    """
    Sort lines by reading order: Top-to-Bottom, Left-to-Right.
    Handles lines that are roughly on the same vertical level (row).
    """
    if not lines:
        return []

    # 1. Sort by Y top first to allow linear processing
    lines_sorted = sorted(lines, key=lambda l: l.y)
    
    rows = []
    current_row = []
    current_row_bottom = -1.0
    
    for line in lines_sorted:
        if not current_row:
            current_row.append(line)
            current_row_bottom = line.y + line.height
            continue
            
        # Check if line belongs to current row
        # Criteria: The line's top is significantly above the current row's bottom.
        # Implies vertical overlap.
        # Use a generous overlap check logic.
        
        # If line starts above the bottom of the previous cluster (with tolerance)
        # Tolerance: 20% of line height?
        tolerance = line.height * 0.2
        if line.y < current_row_bottom - tolerance:
            current_row.append(line)
            # Update bottom to the max of the cluster
            current_row_bottom = max(current_row_bottom, line.y + line.height)
        else:
            # New row
            rows.append(current_row)
            current_row = [line]
            current_row_bottom = line.y + line.height
            
    if current_row:
        rows.append(current_row)
        
    # 2. Sort each row by X and flatten
    final_lines = []
    for row in rows:
        row.sort(key=lambda l: l.x)
        final_lines.extend(row)
        
    return final_lines


def get_lines_in_rect(rect: Tuple[float, float, float, float], lines: List[TextLine]) -> List[TextLine]:
    """
    Find all lines that fall within the rect (visual coords).
    Uses strict center-point inclusion for X axis to prevent duplicates across columns.
    rect: (x0, y0, x1, y1)
    """
    rx0, ry0, rx1, ry1 = rect
    found = []
    
    for line in lines:
        # Check intersection/containment
        # Broad logic: y center of line is within y range of rect
        ly = line.y + line.height/2
        
        # Relaxed Y check slightly to catch lines just on the edge
        # But cell top/bottoms are usually precise.
        if ry0 <= ly <= ry1:
             # Check x: STRICT center point inclusion
             # This ensures a line is assigned to exactly one column (unless columns overlap)
             lx_center = line.x + line.width / 2
             
             if rx0 <= lx_center <= rx1:
                 found.append(line)
    
    # Sort: Y then X using robust reading order (clustering)
    return sort_lines_reading_order(found)


def _norm_table_key(text: str) -> str:
    text = unicodedata.normalize("NFD", (text or "").upper())
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return re.sub(r"\s+", "", text)


def _table_text(table: TableRegion) -> str:
    return " ".join(str(cell) for row in getattr(table, "cells", []) for cell in row)


def _looks_like_table_header_text(text: str) -> bool:
    compact = _norm_table_key(text)
    return (
        ("STT" in compact or "SOTTT" in compact)
        and (
            "NOIDUNG" in compact
            or "TEN" in compact
            or "TENCO" in compact
            or "CONGTAC" in compact
            or "SOLUONG" in compact
            or "GHICHU" in compact
        )
    )


def _row_looks_like_table_header(row: List[str]) -> bool:
    texts = [_clean_extracted_text(str(cell)) for cell in row if _clean_extracted_text(str(cell))]
    if len(texts) < 2:
        return False
    if any(len(text) > 90 for text in texts):
        return False
    avg_len = sum(len(text) for text in texts) / len(texts)
    if avg_len > 45:
        return False
    sentence_like = sum(1 for text in texts if re.search(r"[.;:]\s+\S", text))
    return sentence_like == 0


def _table_has_header(table: TableRegion) -> bool:
    head_rows = getattr(table, "cells", [])[:3]
    if _looks_like_table_header_text(" ".join(str(c) for row in head_rows for c in row)):
        return True
    return any(_row_looks_like_table_header(row) for row in head_rows[:2])


def _table_column_intervals(table: TableRegion) -> List[Tuple[float, float]]:
    cols = getattr(table, "col_count", 0) or 0
    if cols <= 0:
        return []

    best: List[Tuple[float, float]] = []
    for row_boxes in getattr(table, "cell_bboxes", []) or []:
        if len(row_boxes) < cols:
            continue
        intervals = []
        seen = set()
        ok = True
        for c in range(cols):
            bx = row_boxes[c]
            if not any(bx) or bx[2] <= bx[0]:
                ok = False
                break
            key = tuple(round(v, 1) for v in bx)
            if key in seen:
                ok = False
                break
            seen.add(key)
            intervals.append((float(bx[0]), float(bx[2])))
        if ok and len(intervals) > len(best):
            best = intervals
    return best


def _table_bbox(table: TableRegion) -> Tuple[float, float, float, float]:
    x_left = getattr(table, "x_left", None)
    x_right = getattr(table, "x_right", None)
    if x_left is None or x_right is None:
        xs = []
        for row in getattr(table, "cell_bboxes", []) or []:
            for bx in row:
                if any(bx):
                    xs.extend([bx[0], bx[2]])
        x_left = min(xs) if xs else 0.0
        x_right = max(xs) if xs else 0.0
    return (float(x_left), float(table.y_top), float(x_right), float(table.y_bottom))


def _bbox_intersection_ratio(a: Tuple[float, float, float, float],
                             b: Tuple[float, float, float, float]) -> float:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    area = max((ax1 - ax0) * (ay1 - ay0), 1e-6)
    return ((ix1 - ix0) * (iy1 - iy0)) / area


def _line_in_bbox(line: TextLine, bbox: Tuple[float, float, float, float]) -> bool:
    x0, y0, x1, y1 = bbox
    lx0, ly0, lx1, ly1 = _line_bbox_for_assignment(line)
    line_w = max(lx1 - lx0, 1e-6)
    line_h = max(ly1 - ly0, 1e-6)
    cx = line.x + line.width / 2
    cy = line.y + line.height / 2
    pad = max(line_h * 0.75, 2.0)
    if lx1 < x0 - pad or lx0 > x1 + pad or ly1 < y0 - pad or ly0 > y1 + pad:
        return False
    overlap_x = _axis_overlap_for_assignment(lx0, lx1, x0, x1)
    overlap_y = _axis_overlap_for_assignment(ly0, ly1, y0, y1)
    x_ok = x0 <= cx <= x1 or overlap_x / line_w >= 0.15 or min(abs(lx1 - x0), abs(lx0 - x1)) <= pad
    y_ok = y0 <= cy <= y1 or overlap_y / line_h >= 0.35
    return x_ok and y_ok


def _cluster_y_positions(values: List[float], threshold: float = 42.0) -> List[float]:
    if not values:
        return []
    values = sorted(values)
    clusters = [[values[0]]]
    for value in values[1:]:
        if value - clusters[-1][-1] <= threshold:
            clusters[-1].append(value)
        else:
            clusters.append([value])
    return [min(cluster) for cluster in clusters]


def _is_table_row_key_line(line: TextLine, col_idx: int) -> bool:
    if col_idx > 1:
        return False
    key = _norm_table_key(line.text)
    if not key:
        return False
    if key in {"S", "T", "TT", "STT", "TEN", "TENCO", "TENCOQUAN", "QUAN"}:
        return False
    if bool(re.fullmatch(r"\d{1,3}", key)):
        return True

    words = [w for w in re.split(r"\s+", _clean_extracted_text(line.text)) if w]
    if not words:
        return False
    if len(words) > 8 or len(line.text) > 90:
        return False
    # Row labels normally live in the leftmost columns and are short noun
    # phrases, not sentence continuations. This is geometric/content-shape
    # based and does not depend on a specific agency name.
    return not line.text.strip().endswith((".", ";", ":"))


def _text_from_table_cell_lines(cell_lines: List[TextLine],
                                page: int,
                                x0: float,
                                x1: float,
                                logger: Logger) -> str:
    cell_lines = sort_lines_reading_order(cell_lines)
    if not cell_lines:
        return ""
    try:
        paras = merge_raw_paragraphs(cell_lines, {page: (x0, x1)}, logger)
        text = "\n".join(p[0] for p in paras if p[0])
    except Exception:
        text = "\n".join(l.text for l in cell_lines)
    return clean_ocr_cell_text(text)


def _line_bbox_for_assignment(line: TextLine) -> Tuple[float, float, float, float]:
    return (
        float(line.x),
        float(line.y),
        float(line.x + line.width),
        float(line.y + line.height),
    )


def _bbox_area_for_assignment(bbox: Tuple[float, float, float, float]) -> float:
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


def _axis_overlap_for_assignment(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def _cell_line_assignment_score(
    line: TextLine,
    cell_bbox: Tuple[float, float, float, float],
    table_bbox: Tuple[float, float, float, float],
) -> Optional[float]:
    return _text_bbox_cell_assignment_score(_line_bbox_for_assignment(line), cell_bbox, table_bbox)


def _text_bbox_cell_assignment_score(
    text_bbox: Tuple[float, float, float, float],
    cell_bbox: Tuple[float, float, float, float],
    table_bbox: Tuple[float, float, float, float],
) -> Optional[float]:
    bx0, by0, bx1, by1 = cell_bbox
    if bx1 <= bx0 or by1 <= by0:
        return None

    lx0, ly0, lx1, ly1 = text_bbox
    line_w = max(lx1 - lx0, 1e-6)
    line_h = max(ly1 - ly0, 1e-6)
    cell_w = max(bx1 - bx0, 1e-6)
    cell_h = max(by1 - by0, 1e-6)
    lx_center = (lx0 + lx1) / 2.0
    ly_center = (ly0 + ly1) / 2.0

    table_x0, table_y0, table_x1, table_y1 = table_bbox
    table_pad = max(line_h * 0.65, 2.0)
    if (
        lx1 < table_x0 - table_pad
        or lx0 > table_x1 + table_pad
        or ly1 < table_y0 - table_pad
        or ly0 > table_y1 + table_pad
    ):
        return None
    if not (table_y0 <= ly_center <= table_y1):
        return None
    if not (table_x0 - table_pad <= lx_center <= table_x1 + table_pad):
        return None

    overlap_x = _axis_overlap_for_assignment(lx0, lx1, bx0, bx1)
    overlap_y = _axis_overlap_for_assignment(ly0, ly1, by0, by1)
    line_area = max(_bbox_area_for_assignment((lx0, ly0, lx1, ly1)), 1e-6)
    overlap_area = overlap_x * overlap_y
    line_cover = overlap_area / line_area
    x_cover = overlap_x / line_w
    y_cover = overlap_y / line_h

    center_inside = bx0 <= lx_center <= bx1 and by0 <= ly_center <= by1

    x_gap = max(bx0 - lx1, lx0 - bx1, 0.0)
    y_gap = max(by0 - ly1, ly0 - by1, 0.0)
    max_x_gap = max(line_h * 1.25, min(cell_w, table_x1 - table_x0) * 0.035)
    max_y_gap = max(line_h * 0.45, cell_h * 0.12)

    if center_inside:
        base = 4.0 + line_cover + x_cover + y_cover
    elif line_cover >= 0.18 and y_cover >= 0.35:
        base = 3.0 + 2.0 * line_cover + x_cover + y_cover
    elif overlap_x > 0.0 and y_cover >= 0.55:
        base = 2.0 + x_cover + y_cover
    elif overlap_y > 0.0 and x_cover >= 0.55:
        base = 1.8 + x_cover + y_cover
    elif y_cover >= 0.55 and x_gap <= max_x_gap:
        base = 1.2 + y_cover + max(0.0, 1.0 - x_gap / max(max_x_gap, 1e-6))
    elif x_cover >= 0.55 and y_gap <= max_y_gap:
        base = 1.0 + x_cover + max(0.0, 1.0 - y_gap / max(max_y_gap, 1e-6))
    else:
        return None

    cell_cx = (bx0 + bx1) / 2.0
    cell_cy = (by0 + by1) / 2.0
    norm_dist = (
        abs(lx_center - cell_cx) / max(cell_w, line_w, 1.0)
        + abs(ly_center - cell_cy) / max(cell_h, line_h, 1.0)
    )
    return base - norm_dist * 0.05


def _cell_bbox_key_for_assignment(bbox: Tuple[float, float, float, float]) -> Tuple[float, float, float, float]:
    return tuple(round(float(v), 2) for v in bbox[:4])


def _iter_table_cell_bboxes(table: TableRegion):
    rows = int(getattr(table, "row_count", 0) or 0)
    cols = int(getattr(table, "col_count", 0) or 0)
    for r in range(rows):
        for c in range(cols):
            if r >= len(table.cell_bboxes) or c >= len(table.cell_bboxes[r]):
                continue
            bbox = tuple(float(v) for v in table.cell_bboxes[r][c][:4])
            if any(bbox) and bbox[2] > bbox[0] and bbox[3] > bbox[1]:
                yield r, c, bbox


def _best_cell_for_text_bbox(
    text_bbox: Tuple[float, float, float, float],
    page: int,
    table_regions: List[TableRegion],
    table_bboxes: Dict[int, Tuple[float, float, float, float]],
) -> Optional[Tuple[int, int, int]]:
    best_key = None
    best_score = None
    for t_idx, table in enumerate(table_regions):
        if getattr(table, "skip_render", False) or table.page != page:
            continue
        table_bbox = table_bboxes.get(t_idx)
        if table_bbox is None:
            continue
        for r, c, bbox in _iter_table_cell_bboxes(table):
            score = _text_bbox_cell_assignment_score(text_bbox, bbox, table_bbox)
            if score is None:
                continue
            if best_score is None or score > best_score:
                best_score = score
                best_key = (t_idx, r, c)
    return best_key


def _word_bbox_for_assignment(word: dict) -> Tuple[float, float, float, float]:
    x = float(word.get("x", 0.0) or 0.0)
    y = float(word.get("y", 0.0) or 0.0)
    w = float(word.get("w", 0.0) or 0.0)
    h = float(word.get("h", 0.0) or 0.0)
    return (x, y, x + w, y + h)


def _text_from_word_items(words: List[dict]) -> str:
    parts = [_clean_extracted_text(str(word.get("text") or "")) for word in words]
    return " ".join(part for part in parts if part)


def _line_fragment_from_words(parent: TextLine, words: List[dict]) -> Optional[TextLine]:
    if not words:
        return None
    text = _text_from_word_items(words)
    if not text:
        return None
    boxes = [_word_bbox_for_assignment(word) for word in words]
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[2] for b in boxes)
    y1 = max(b[3] for b in boxes)
    fragment = TextLine(
        text=text,
        x=x0,
        y=y0,
        width=max(0.0, x1 - x0),
        height=max(0.0, y1 - y0),
        page=parent.page,
        font_size=parent.font_size,
        spans=[TextSpan(text=text, font_size=parent.font_size, y=y0)],
        block_id=parent.block_id,
        paragraph_id=parent.paragraph_id,
        content_type=parent.content_type,
        fg_gray=parent.fg_gray,
        confidence=parent.confidence,
        semantic_type=parent.semantic_type,
        order=parent.order,
        source_line_id=parent.source_line_id,
        kie_labels=set(getattr(parent, "kie_labels", set())),
    )
    return fragment


def assign_ocr_lines_to_table_cells_by_geometry(
    table_regions: List[TableRegion],
    pdf_lines: List[TextLine],
    logger: Logger,
    candidate_lines: Optional[List[TextLine]] = None,
    rebuild_cells: bool = True,
    preserve_header_rows: bool = False,
) -> set:
    """
    Assign OCR text inside table bboxes to exactly one best cell by geometry.
    Scoring uses text bbox overlap, row/column band overlap, and small boundary
    gaps. If OCR word boxes are available, split a line by word only when its
    words truly fall into different cells.
    """
    if not table_regions or not pdf_lines:
        return set()

    candidates = candidate_lines if candidate_lines is not None else pdf_lines
    table_bboxes: Dict[int, Tuple[float, float, float, float]] = {}
    duplicate_cell_targets: Dict[Tuple[int, Tuple[float, float, float, float]], List[Tuple[int, int, int]]] = {}
    for t_idx, table in enumerate(table_regions):
        if getattr(table, "skip_render", False):
            continue
        table_bboxes[t_idx] = _table_bbox(table)
        for r, c, bbox in _iter_table_cell_bboxes(table):
            duplicate_cell_targets.setdefault((t_idx, _cell_bbox_key_for_assignment(bbox)), []).append((t_idx, r, c))

    cell_lines: Dict[Tuple[int, int, int], List[TextLine]] = {}
    assigned_source_ids = set()

    def add_to_cell(key: Tuple[int, int, int], line: TextLine):
        t_idx, r, c = key
        bbox = table_regions[t_idx].cell_bboxes[r][c]
        targets = duplicate_cell_targets.get((t_idx, _cell_bbox_key_for_assignment(tuple(bbox[:4]))), [key])
        for target in targets:
            cell_lines.setdefault(target, []).append(line)

    for line in candidates:
        words = [word for word in getattr(line, "word_items", []) or [] if _clean_extracted_text(str(word.get("text") or ""))]
        if words:
            grouped_words: Dict[Tuple[int, int, int], List[dict]] = {}
            for word in words:
                key = _best_cell_for_text_bbox(_word_bbox_for_assignment(word), line.page, table_regions, table_bboxes)
                if key is not None:
                    grouped_words.setdefault(key, []).append(word)
            if grouped_words:
                assigned_source_ids.add(id(line))
                if len(grouped_words) == 1:
                    add_to_cell(next(iter(grouped_words.keys())), line)
                else:
                    for key, group in grouped_words.items():
                        group.sort(key=lambda item: int(item.get("order", 0) or 0))
                        fragment = _line_fragment_from_words(line, group)
                        if fragment is not None:
                            add_to_cell(key, fragment)
                continue

        key = _best_cell_for_text_bbox(_line_bbox_for_assignment(line), line.page, table_regions, table_bboxes)
        if key is not None:
            assigned_source_ids.add(id(line))
            add_to_cell(key, line)

    if rebuild_cells:
        for t_idx, table in enumerate(table_regions):
            if getattr(table, "skip_render", False):
                continue
            if getattr(table, "source_segments", None):
                continue
            rows = int(getattr(table, "row_count", 0) or 0)
            cols = int(getattr(table, "col_count", 0) or 0)
            existing_cells = getattr(table, "cells", []) or []
            preserve_until = -1
            if preserve_header_rows:
                for idx, row in enumerate(existing_cells[:4]):
                    if _is_numeric_header_row(list(row)):
                        preserve_until = idx
                        break
            rebuilt_cells = [[""] * cols for _ in range(rows)]
            for r in range(rows):
                for c in range(cols):
                    if preserve_until >= 0 and r <= preserve_until and r < len(existing_cells) and c < len(existing_cells[r]):
                        rebuilt_cells[r][c] = str(existing_cells[r][c])
                        continue
                    key = (t_idx, r, c)
                    lines_for_cell = _unique_lines_for_cell(cell_lines.get(key, []))
                    if lines_for_cell:
                        bbox = table.cell_bboxes[r][c]
                        x0 = min(float(bbox[0]), *(line.x for line in lines_for_cell))
                        x1 = max(float(bbox[2]), *(line.x + line.width for line in lines_for_cell))
                        rebuilt_cells[r][c] = _text_from_table_cell_lines(lines_for_cell, table.page, x0, x1, logger)
                    elif r < len(existing_cells) and c < len(existing_cells[r]):
                        rebuilt_cells[r][c] = str(existing_cells[r][c])
            table.cells = rebuilt_cells

    if assigned_source_ids:
        logger.log(f"Assigned {len(assigned_source_ids)} OCR table lines by geometry cell mapping")
    return assigned_source_ids


def _unique_lines_for_cell(lines: List[TextLine]) -> List[TextLine]:
    seen = set()
    unique = []
    for line in lines:
        line_id = id(line)
        if line_id in seen:
            continue
        seen.add(line_id)
        unique.append(line)
    return sort_lines_reading_order(unique)


def _compact_cell_text_for_compare(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").casefold()
    return re.sub(r"\s+", "", text)


def _merge_rescued_cell_text(existing: str, rebuilt: str, rescued_lines: List[TextLine]) -> str:
    existing = clean_ocr_cell_text(existing or "")
    rebuilt = clean_ocr_cell_text(rebuilt or "")
    if not existing:
        return rebuilt
    if not rebuilt:
        return existing

    compact_existing = _compact_cell_text_for_compare(existing)
    compact_rebuilt = _compact_cell_text_for_compare(rebuilt)
    if compact_existing and compact_existing in compact_rebuilt:
        return rebuilt
    return existing


def assign_orphan_ocr_lines_to_table_cells(
    table_regions: List[TableRegion],
    pdf_lines: List[TextLine],
    logger: Logger,
    candidate_lines: Optional[List[TextLine]] = None,
) -> set:
    """
    Recover OCR lines that are inside a detected table but missed by strict
    center-point cell mapping. Assignment is geometry-only: bbox overlap,
    shared row band, and small boundary gaps for text that sits on grid lines.
    """
    if not table_regions or not pdf_lines:
        return set()

    page_lines: Dict[int, List[TextLine]] = {}
    for line in pdf_lines:
        page_lines.setdefault(line.page, []).append(line)

    candidates = candidate_lines if candidate_lines is not None else pdf_lines
    assigned_ids = set()
    cell_lines: Dict[Tuple[int, int, int], List[TextLine]] = {}
    table_bboxes: Dict[int, Tuple[float, float, float, float]] = {}

    for t_idx, table in enumerate(table_regions):
        if getattr(table, "skip_render", False):
            continue
        rows = int(getattr(table, "row_count", 0) or 0)
        cols = int(getattr(table, "col_count", 0) or 0)
        if rows <= 0 or cols <= 0:
            continue
        table_bbox = _table_bbox(table)
        table_bboxes[t_idx] = table_bbox
        table_page_lines = page_lines.get(table.page, [])
        for r in range(rows):
            for c in range(cols):
                if r >= len(table.cell_bboxes) or c >= len(table.cell_bboxes[r]):
                    continue
                bbox = tuple(float(v) for v in table.cell_bboxes[r][c][:4])
                if not any(bbox):
                    continue
                lines = get_lines_in_rect(bbox, table_page_lines)
                if lines:
                    cell_lines[(t_idx, r, c)] = list(lines)
                    assigned_ids.update(id(line) for line in lines)

    rescued_by_cell: Dict[Tuple[int, int, int], List[TextLine]] = {}
    rescued_ids = set()
    for line in candidates:
        line_id = id(line)
        if line_id in assigned_ids:
            continue

        best_key = None
        best_score = None
        for t_idx, table in enumerate(table_regions):
            if getattr(table, "skip_render", False) or table.page != line.page:
                continue
            table_bbox = table_bboxes.get(t_idx)
            if table_bbox is None:
                continue
            rows = int(getattr(table, "row_count", 0) or 0)
            cols = int(getattr(table, "col_count", 0) or 0)
            for r in range(rows):
                for c in range(cols):
                    if r >= len(table.cell_bboxes) or c >= len(table.cell_bboxes[r]):
                        continue
                    bbox = tuple(float(v) for v in table.cell_bboxes[r][c][:4])
                    score = _cell_line_assignment_score(line, bbox, table_bbox)
                    if score is None:
                        continue
                    if best_score is None or score > best_score:
                        best_score = score
                        best_key = (t_idx, r, c)

        if best_key is not None:
            rescued_by_cell.setdefault(best_key, []).append(line)
            rescued_ids.add(line_id)

    for key, rescued_lines in rescued_by_cell.items():
        t_idx, r, c = key
        table = table_regions[t_idx]
        original_lines = cell_lines.get(key, [])
        merged_lines = _unique_lines_for_cell(original_lines + rescued_lines)
        if not merged_lines:
            continue
        bx0, _by0, bx1, _by1 = table.cell_bboxes[r][c]
        x0 = min(float(bx0), *(line.x for line in merged_lines))
        x1 = max(float(bx1), *(line.x + line.width for line in merged_lines))
        rebuilt = _text_from_table_cell_lines(merged_lines, table.page, x0, x1, logger)
        existing = ""
        if r < len(table.cells) and c < len(table.cells[r]):
            existing = str(table.cells[r][c])
        table.cells[r][c] = _merge_rescued_cell_text(existing, rebuilt, rescued_lines)

    if rescued_ids:
        logger.log(f"Assigned {len(rescued_ids)} OCR table lines by geometry-overlap fallback")
    return rescued_ids


def _build_table_rows_from_bbox(page: int,
                                bbox: Tuple[float, float, float, float],
                                col_intervals: List[Tuple[float, float]],
                                pdf_lines: List[TextLine],
                                logger: Logger) -> Tuple[List[List[str]], List[List[Tuple[float, float, float, float]]]]:
    x0, y0, x1, y1 = bbox
    col_count = len(col_intervals)
    if col_count <= 0:
        return [], []

    lines = [l for l in pdf_lines if l.page == page and _line_in_bbox(l, bbox)]
    if not lines:
        return [], []

    line_cols: Dict[int, List[TextLine]] = {i: [] for i in range(col_count)}
    key_y = []
    for line in lines:
        cx = line.x + line.width / 2
        col_idx = None
        for i, (cx0, cx1) in enumerate(col_intervals):
            if cx0 <= cx <= cx1:
                col_idx = i
                break
        if col_idx is None:
            nearest = min(range(col_count), key=lambda i: abs(cx - ((col_intervals[i][0] + col_intervals[i][1]) / 2)))
            if abs(cx - ((col_intervals[nearest][0] + col_intervals[nearest][1]) / 2)) < 80:
                col_idx = nearest
        if col_idx is None:
            continue
        line_cols[col_idx].append(line)
        if _is_table_row_key_line(line, col_idx):
            key_y.append(line.y)

    starts = _cluster_y_positions(key_y)
    if starts and starts[0] <= y0 + 45:
        starts[0] = y0
    else:
        starts = [y0] + starts
    starts = sorted(set(round(v, 2) for v in starts if y0 - 2 <= v <= y1 + 2))
    if not starts:
        starts = [y0]

    bands = []
    for idx, start in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else y1
        if end - start >= 8:
            bands.append((float(start), float(end)))

    rows: List[List[str]] = []
    row_boxes: List[List[Tuple[float, float, float, float]]] = []
    for ry0, ry1 in bands:
        row = []
        boxes = []
        for col_idx, (cx0, cx1) in enumerate(col_intervals):
            cell_lines = [
                l for l in line_cols[col_idx]
                if ry0 <= l.y + l.height / 2 <= ry1
            ]
            row.append(_text_from_table_cell_lines(cell_lines, page, cx0, cx1, logger))
            boxes.append((cx0, ry0, cx1, ry1))
        if any(cell.strip() for cell in row):
            rows.append(row)
            row_boxes.append(boxes)

    return rows, row_boxes


def _is_continuation_table_row(row: List[str]) -> bool:
    if not row:
        return False
    key = _norm_table_key(" ".join(row[:2]))
    if re.fullmatch(r"\d{1,3}", key or ""):
        return False
    leading = _clean_extracted_text(" ".join(str(cell) for cell in row[:2]))
    if leading:
        words = [w for w in re.split(r"\s+", leading) if w]
        if len(words) <= 8 and len(leading) <= 90 and not leading.endswith((".", ";", ":")):
            return False
    return any((cell or "").strip() for cell in row[2:])


def _append_row_text(dst: str, src: str) -> str:
    dst = (dst or "").strip()
    src = (src or "").strip()
    if not src:
        return dst
    if not dst:
        return src
    if src in dst:
        return dst
    return dst + "\n" + src


def _normalize_table_key_cells(row: List[str]) -> List[str]:
    # Keep OCR content as-is. Earlier versions tried to synthesize a normalized
    # organization name from row fragments; that is too case-specific and can
    # invent text that was not recognized by OCR.
    if len(row) >= 2 and not _clean_extracted_text(str(row[0])):
        second = _clean_extracted_text(str(row[1]))
        match = re.match(r"^(\d{1,3})\s+(.+)$", second)
        if match and re.search(r"[A-Za-zÀ-Ỵà-ỵĐđ]", match.group(2)):
            row[0] = match.group(1)
            row[1] = match.group(2).strip()
    return row


def _merge_table_rows(base_rows: List[List[str]], incoming_rows: List[List[str]], col_count: int) -> List[List[str]]:
    rows = [list(row[:col_count]) + [""] * max(0, col_count - len(row)) for row in base_rows]
    for raw_row in incoming_rows:
        row = list(raw_row[:col_count]) + [""] * max(0, col_count - len(raw_row))
        row = _normalize_table_key_cells(row)
        if rows and _is_continuation_table_row(row):
            for c in range(col_count):
                rows[-1][c] = _append_row_text(rows[-1][c], row[c])
        else:
            rows.append(row)
    return rows


def _numeric_header_sequence(row: List[str], expected_count: int) -> bool:
    if expected_count <= 0 or len(row) < expected_count:
        return False
    values = [_norm_table_key(cell) for cell in row[:expected_count]]
    digits = [re.sub(r"\D+", "", value) for value in values]
    expected = [str(i) for i in range(1, expected_count + 1)]
    return digits == expected or (
        expected_count >= 5
        and digits[:5] in (["1", "2", "3", "4", "5"], ["", "2", "3", "4", "5"])
    )


def _is_numeric_header_row(row: List[str]) -> bool:
    values = [_norm_table_key(cell) for cell in row[:5]]
    digits = [re.sub(r"\D+", "", value) for value in values]
    if len(digits) < 5:
        return False
    if digits[:5] in (["1", "2", "3", "4", "5"], ["", "2", "3", "4", "5"]):
        return True
    # OCR sometimes reads column number 4 as 5 in tiny header rows.
    return digits[0] in ("", "1") and digits[1:3] == ["2", "3"] and digits[3] in ("4", "5") and digits[4] == "5"


def _collapse_spaced_acronym(text: str) -> str:
    stripped = _clean_extracted_text(text)
    compact = _norm_table_key(stripped)
    if compact == "STT":
        return "STT"
    return stripped


def _remove_prefix_by_norm(text: str, prefix: str) -> str:
    text = _clean_extracted_text(text)
    prefix_norm = _norm_table_key(prefix)
    if not text or not prefix_norm:
        return text
    words = text.split()
    for idx in range(1, len(words) + 1):
        if _norm_table_key(" ".join(words[:idx])) == prefix_norm:
            return " ".join(words[idx:]).strip()
    return text


def _extract_group_header_text(header_cells: List[str]) -> str:
    candidates = []
    for cell in header_cells:
        text = _clean_extracted_text(cell)
        norm = _unaccent_upper(text)
        if "CONG TAC" not in norm:
            continue
        # Use text that is already present in OCR. Stop before common lower-level
        # header phrases so a merged cell can be split into group/subheader.
        stop_positions = []
        for stop in ("SO LUONG", "GHI CHU", "STT", "TEN "):
            pos = norm.find(stop, norm.find("CONG TAC") + len("CONG TAC"))
            if pos > 0:
                stop_positions.append(pos)
        start = norm.find("CONG TAC")
        end = min(stop_positions) if stop_positions else len(text)
        phrase = text[start:end].strip(" :-")
        if phrase:
            candidates.append(phrase)
    if not candidates:
        return ""
    return min(candidates, key=len)


def _normalize_continued_table_header_rows(rows: List[List[str]], col_count: int) -> List[List[str]]:
    if col_count < 2 or not rows:
        return rows

    numeric_idx = None
    for idx, row in enumerate(rows[:4]):
        if _is_numeric_header_row(row):
            numeric_idx = idx
            break
    if numeric_idx is None:
        return rows

    header_rows = [
        list(row[:col_count]) + [""] * max(0, col_count - len(row))
        for row in rows[:numeric_idx]
    ]
    body_rows = [
        list(row[:col_count]) + [""] * max(0, col_count - len(row))
        for row in rows[numeric_idx + 1:]
    ]
    number_row = [str(i) for i in range(1, col_count + 1)]

    if not header_rows:
        return [number_row] + body_rows

    primary = [_collapse_spaced_acronym(cell) for cell in header_rows[0]]
    group_title = _extract_group_header_text([cell for row in header_rows for cell in row])

    if group_title and len(header_rows) == 1:
        lower = list(primary)
        for c in range(2, col_count):
            lower[c] = _remove_prefix_by_norm(lower[c], group_title)
        top = [""] * col_count
        top[0] = primary[0]
        top[1] = primary[1]
        for c in range(2, col_count):
            top[c] = group_title if c < col_count - 1 or not lower[c] else ""
        return [top, lower, number_row] + body_rows

    normalized_headers = []
    for row in header_rows:
        normalized_headers.append([_collapse_spaced_acronym(cell) for cell in row])
    if len(normalized_headers) >= 2:
        for c in range(min(2, col_count)):
            top = _clean_extracted_text(normalized_headers[0][c])
            lower = _clean_extracted_text(normalized_headers[1][c])
            combined = _collapse_spaced_acronym(f"{top} {lower}".strip())
            word_count = len([w for w in combined.split() if w])
            if (
                top and lower
                and word_count <= 4
                and len(top) <= 24
                and len(lower) <= 24
                and not re.search(r"[.;:]", combined)
            ):
                normalized_headers[0][c] = combined
                normalized_headers[1][c] = combined
    return normalized_headers + [number_row] + body_rows


def _repair_continued_table_headers(rows: List[List[str]], col_count: int) -> List[List[str]]:
    return _normalize_continued_table_header_rows(rows, col_count)


def _matching_layout_bbox_for_table(table: TableRegion,
                                    layout_regions_by_page: Dict[int, List[dict]]) -> Optional[Tuple[float, float, float, float]]:
    best_bbox = None
    best_overlap = 0.0
    table_bbox = _table_bbox(table)
    for region in (layout_regions_by_page or {}).get(table.page, []):
        if region.get("type") != "table":
            continue
        bbox = region.get("bbox_pdf")
        if not bbox or len(bbox) < 4:
            continue
        layout_bbox = tuple(float(v) for v in bbox[:4])
        overlap = _bbox_intersection_ratio(table_bbox, layout_bbox)
        if overlap > best_overlap:
            best_overlap = overlap
            best_bbox = layout_bbox
    return best_bbox if best_overlap >= 0.45 else None


def _median_interval_width(intervals: List[Tuple[float, float]]) -> float:
    widths = sorted(max(0.0, x1 - x0) for x0, x1 in intervals if x1 > x0)
    if not widths:
        return 0.0
    return widths[len(widths) // 2]


def _append_empty_column(table: TableRegion, x0: float, x1: float):
    if x1 <= x0:
        return
    for row in table.cells:
        row.append("")
    for r_idx, row_boxes in enumerate(table.cell_bboxes):
        ys = [(bbox[1], bbox[3]) for bbox in row_boxes if any(bbox) and bbox[3] > bbox[1]]
        if ys:
            y0 = min(y[0] for y in ys)
            y1 = max(y[1] for y in ys)
        else:
            row_height = max((table.y_bottom - table.y_top) / max(table.row_count, 1), 12.0)
            y0 = table.y_top + r_idx * row_height
            y1 = y0 + row_height
        row_boxes.append((x0, y0, x1, y1))
    table.col_count += 1
    setattr(table, "x_right", max(float(getattr(table, "x_right", x1) or x1), x1))


def _maybe_append_group_grid_column(table: TableRegion) -> bool:
    """
    Some structure engines emit only the visible logical columns for a grouped
    header, while DOCX needs one extra narrow grid column so the top-level group
    cell can span over a lower "Ghi chú/Note" header without shifting body
    cells. Detect this from the header geometry/text pattern rather than from a
    specific document.
    """
    if table.row_count < 3 or table.col_count < 5 or table.col_count > 8:
        return False

    rows = table.cells
    first = list(rows[0][:table.col_count]) + [""] * max(0, table.col_count - len(rows[0]))
    second = list(rows[1][:table.col_count]) + [""] * max(0, table.col_count - len(rows[1]))
    third = list(rows[2][:table.col_count]) + [""] * max(0, table.col_count - len(rows[2]))

    grouped = [_clean_extracted_text(cell) for cell in first[2:]]
    if len(grouped) < 3 or any(not cell for cell in grouped):
        return False
    group_keys = {_norm_table_key(cell) for cell in grouped if cell}
    if len(group_keys) != 1:
        return False

    trailing_lower_header = _clean_extracted_text(second[-1])
    if not trailing_lower_header:
        return False

    lower_header_count = sum(1 for cell in second[2:] if _clean_extracted_text(cell))
    if lower_header_count < max(2, table.col_count - 3):
        return False

    body_rows = rows[3:]
    if body_rows:
        trailing_body_filled = sum(
            1
            for row in body_rows
            if len(row) >= table.col_count and _clean_extracted_text(row[-1])
        )
        if trailing_body_filled / max(len(body_rows), 1) > 0.35:
            return False

    numbered = [_clean_extracted_text(cell) for cell in third]
    numeric_labels = sum(1 for cell in numbered if re.fullmatch(r"\d{1,3}", cell or ""))
    if numeric_labels and numeric_labels < max(2, table.col_count - 2):
        return False

    intervals = _table_column_intervals(table)
    if len(intervals) != table.col_count:
        return False
    widths = [max(0.0, x1 - x0) for x0, x1 in intervals if x1 > x0]
    if not widths:
        return False
    narrow_width = max(6.0, min(sorted(widths)[len(widths) // 2] * 0.12, 12.0))
    x0 = intervals[-1][1]
    x1 = x0 + narrow_width

    group_text = grouped[0]
    _append_empty_column(table, x0, x1)
    if table.cells and table.cells[0]:
        table.cells[0][-1] = group_text
    return True


def _append_empty_row(table: TableRegion, col_intervals: List[Tuple[float, float]], y0: float, y1: float):
    if y1 <= y0 or len(col_intervals) != table.col_count:
        return
    table.cells.append([""] * table.col_count)
    table.cell_bboxes.append([(x0, y0, x1, y1) for x0, x1 in col_intervals])
    table.row_count += 1
    table.y_bottom = max(float(table.y_bottom), y1)


def _normalize_grouped_header_rows(table: TableRegion) -> bool:
    """
    Normalize generic two-level table headers when the detector places the
    group label in the visual center column and leaves leading group cells
    empty. The content is only moved, never invented.
    """
    if table.row_count < 2 or table.col_count < 4:
        return False
    rows = table.cells
    first = list(rows[0][:table.col_count]) + [""] * max(0, table.col_count - len(rows[0]))
    second = list(rows[1][:table.col_count]) + [""] * max(0, table.col_count - len(rows[1]))

    prefix = 2 if table.col_count >= 5 else 1
    if any(_clean_extracted_text(first[c]) for c in range(min(prefix, len(first)))):
        return False

    fixed_headers = [_clean_extracted_text(second[c]) for c in range(min(prefix, len(second)))]
    if len([h for h in fixed_headers if h and len(h) <= 40]) < min(prefix, len(fixed_headers)):
        return False

    group_cells = [
        (idx, _clean_extracted_text(cell))
        for idx, cell in enumerate(first[prefix:], prefix)
        if _clean_extracted_text(cell)
    ]
    if len(group_cells) != 1:
        return False

    group_idx, group_text = group_cells[0]
    if group_idx <= prefix:
        return False

    for c in range(prefix):
        first[c] = _collapse_spaced_acronym(second[c])
    for c in range(prefix, table.col_count):
        first[c] = ""
    first[prefix] = group_text
    rows[0] = first
    return True


def _has_sparse_group_header(table: TableRegion) -> bool:
    if table.row_count < 2 or table.col_count < 5:
        return False
    first = list(table.cells[0][:table.col_count]) + [""] * max(0, table.col_count - len(table.cells[0]))
    second = list(table.cells[1][:table.col_count]) + [""] * max(0, table.col_count - len(table.cells[1]))
    top_nonempty = _row_nonempty_count(first)
    if top_nonempty <= 0 or top_nonempty > table.col_count - 2:
        return False
    if not any(not _clean_extracted_text(cell) for cell in first[2:]):
        return False
    lower_labels = sum(1 for cell in second if _clean_extracted_text(cell))
    return lower_labels >= max(3, table.col_count - 2)


def postprocess_table_layout_grids(table_regions: List[TableRegion],
                                   layout_regions_by_page: Dict[int, List[dict]],
                                   logger: Logger) -> List[TableRegion]:
    """
    Use document-layout table boxes to repair detector grids that stop just
    inside the actual ruled table border. This covers narrow empty edge columns
    and blank trailing rows without relying on document-specific text.
    """
    added_cols = 0
    added_rows = 0
    normalized_headers = 0
    added_grid_cols = 0

    for table in table_regions:
        if getattr(table, "skip_render", False):
            continue
        if getattr(table, "source", "") == "docling_tableformer":
            # Docling TableFormer already runs its own table-structure matching.
            # The generic layout-gap repair below was designed for detector
            # grids that stop inside ruled borders; applying it to Docling can
            # create extra columns/rows from harmless bbox padding.
            continue
        if table.row_count <= 0 or table.col_count <= 0:
            continue

        layout_bbox = _matching_layout_bbox_for_table(table, layout_regions_by_page)
        intervals = _table_column_intervals(table)
        if layout_bbox and len(intervals) == table.col_count:
            median_width = _median_interval_width(intervals)
            right_gap = layout_bbox[2] - intervals[-1][1]
            header_like = _table_has_header(table) or _looks_like_table_header_text(_table_text(table))
            if (
                header_like
                and _has_sparse_group_header(table)
                and table.col_count >= 4
                and right_gap >= 10.0
                and right_gap <= max(36.0, median_width * 0.65)
            ):
                _append_empty_column(table, intervals[-1][1], layout_bbox[2])
                intervals = _table_column_intervals(table)
                added_cols += 1

            bottom_gap = layout_bbox[3] - table.y_bottom
            if (
                header_like
                and table.row_count <= 2
                and table.col_count >= 3
                and len(intervals) == table.col_count
                and 8.0 <= bottom_gap <= 40.0
            ):
                _append_empty_row(table, intervals, table.y_bottom, layout_bbox[3])
                added_rows += 1

        if _maybe_append_group_grid_column(table):
            added_grid_cols += 1

        if _normalize_grouped_header_rows(table):
            normalized_headers += 1

    if added_cols:
        logger.log(f"Added {added_cols} narrow trailing table column(s) from layout grid")
    if added_rows:
        logger.log(f"Added {added_rows} trailing blank table row(s) from layout grid")
    if added_grid_cols:
        logger.log(f"Added {added_grid_cols} grouped-header grid column(s)")
    if normalized_headers:
        logger.log(f"Normalized {normalized_headers} grouped table header row(s)")
    return table_regions


def _dummy_table_bboxes(rows: int,
                        col_intervals: List[Tuple[float, float]],
                        y_top: float,
                        row_height: float = 24.0) -> List[List[Tuple[float, float, float, float]]]:
    out = []
    for r in range(rows):
        ry0 = y_top + r * row_height
        ry1 = ry0 + row_height
        out.append([(x0, ry0, x1, ry1) for x0, x1 in col_intervals])
    return out


def repair_continued_tables(table_regions: List[TableRegion],
                            layout_regions_by_page: Dict[int, List[dict]],
                            pdf_lines: List[TextLine],
                            page_info: dict,
                            logger: Logger) -> List[TableRegion]:
    """
    Merge multi-page ruled tables before DOCX rendering. GMFT/TATR often
    detects continuation pages with fewer columns or misses a continuation
    segment; layout table regions plus the first page's column grid give a
    more stable representation.
    """
    if not table_regions:
        return table_regions

    detected = list(table_regions)
    layout_items = []
    for page, regions in (layout_regions_by_page or {}).items():
        for region in regions:
            if region.get("type") != "table":
                continue
            bbox = region.get("bbox_pdf")
            if not bbox or len(bbox) < 4:
                continue
            layout_items.append({
                "page": page,
                "bbox": tuple(float(v) for v in bbox[:4]),
                "detected": None,
            })

    for item in layout_items:
        best = None
        best_overlap = 0.0
        for table in detected:
            if table.page != item["page"]:
                continue
            overlap = _bbox_intersection_ratio(_table_bbox(table), item["bbox"])
            if overlap > best_overlap:
                best = table
                best_overlap = overlap
        if best is not None and best_overlap >= 0.45:
            item["detected"] = best

    # Include detected tables when no layout region was available.
    for table in detected:
        if any(item.get("detected") is table for item in layout_items):
            continue
        layout_items.append({"page": table.page, "bbox": _table_bbox(table), "detected": table})

    layout_items.sort(key=lambda i: (i["page"], i["bbox"][1]))
    if not layout_items:
        return table_regions

    groups = []
    current = None
    for item in layout_items:
        table = item.get("detected")
        header_like = _table_has_header(table) if table is not None else False
        if not header_like:
            lines_in_region = [l for l in pdf_lines if l.page == item["page"] and _line_in_bbox(l, item["bbox"])]
            header_like = _looks_like_table_header_text(" ".join(l.text for l in lines_in_region[:20]))

        starts_near_top = item["bbox"][1] < page_info.get(item["page"], {}).get("height", 842) * 0.18
        prev_item = current["items"][-1] if current is not None and current["items"] else None
        continues_next_page = prev_item is not None and item["page"] == prev_item["page"] + 1
        should_continue = current is not None and continues_next_page and starts_near_top and not header_like
        if header_like or current is None or not should_continue:
            current = {"items": [], "col_intervals": None, "first": item}
            groups.append(current)
        current["items"].append(item)
        if current["col_intervals"] is None and table is not None:
            intervals = _table_column_intervals(table)
            if len(intervals) >= 2:
                current["col_intervals"] = intervals

    repaired: List[TableRegion] = list(table_regions)
    synthetic_count = 0

    for group in groups:
        if len(group["items"]) <= 1:
            continue
        col_intervals = group.get("col_intervals") or []
        if len(col_intervals) < 2:
            continue
        col_count = len(col_intervals)

        combined_rows: List[List[str]] = []
        first_render_item = group["first"]
        source_segments = []

        for idx, item in enumerate(group["items"]):
            table = item.get("detected")
            use_detected_rows = (
                idx == 0
                and table is not None
                and table.col_count == col_count
            )
            if use_detected_rows:
                assign_ocr_lines_to_table_cells_by_geometry(
                    [table],
                    pdf_lines,
                    logger,
                    preserve_header_rows=True,
                )
                rows = [list(row) for row in table.cells]
                boxes = table.cell_bboxes
            else:
                rows, boxes = _build_table_rows_from_bbox(
                    item["page"], item["bbox"], col_intervals, pdf_lines, logger
                )
                if table is None and rows:
                    fallback = TableRegion(
                        page=item["page"],
                        y_top=item["bbox"][1],
                        y_bottom=item["bbox"][3],
                        cells=rows,
                        row_count=len(rows),
                        col_count=col_count,
                        cell_bboxes=boxes,
                    )
                    repaired.append(fallback)
                    table = fallback
                    item["detected"] = table

            if rows:
                combined_rows = _merge_table_rows(combined_rows, rows, col_count)
            if table is not None:
                table.y_top = item["bbox"][1]
                table.y_bottom = item["bbox"][3]
                source_segments.append(table)

        if not combined_rows or first_render_item is None:
            continue

        combined_rows = _repair_continued_table_headers(combined_rows, col_count)
        first_table = first_render_item.get("detected")
        first_bbox = first_render_item["bbox"]
        synthetic = TableRegion(
            page=first_render_item["page"],
            y_top=first_bbox[1],
            y_bottom=first_bbox[3],
            cells=combined_rows,
            row_count=len(combined_rows),
            col_count=col_count,
            cell_bboxes=_dummy_table_bboxes(len(combined_rows), col_intervals, first_bbox[1]),
        )
        setattr(synthetic, "disable_vertical_merge", True)
        setattr(synthetic, "source_segments", source_segments)
        if first_table is not None:
            setattr(synthetic, "x_left", getattr(first_table, "x_left", first_bbox[0]))
            setattr(synthetic, "x_right", getattr(first_table, "x_right", first_bbox[2]))
        for source in source_segments:
            setattr(source, "skip_render", True)
        synthetic_count += 1
        repaired.append(synthetic)

    if synthetic_count:
        logger.log(f"Repaired {synthetic_count} continued multi-page table(s)")
    return repaired


def filter_false_positive_tables(table_regions: List[TableRegion],
                                 layout_regions_by_page: Dict[int, List[dict]],
                                 logger: Logger) -> List[TableRegion]:
    """
    Remove detector artifacts that are just wrapped text blocks. These are
    especially damaging because the paragraph text disappears into a 1-column
    DOCX table and creates extra tables compared with the ground truth.
    """
    kept: List[TableRegion] = []
    removed = 0
    for table in table_regions:
        if getattr(table, "skip_render", False):
            kept.append(table)
            continue
        if (getattr(table, "col_count", 0) or 0) <= 1 and (getattr(table, "row_count", 0) or 0) > 1:
            removed += 1
            continue
        kept.append(table)

    if removed:
        logger.log(f"Removed {removed} false-positive 1-column table(s)")
    return kept


def _row_y_bounds(row_boxes: List[Tuple[float, float, float, float]]) -> Tuple[float, float]:
    ys0 = [bbox[1] for bbox in row_boxes if any(bbox)]
    ys1 = [bbox[3] for bbox in row_boxes if any(bbox)]
    if not ys0 or not ys1:
        return (0.0, 0.0)
    return (min(ys0), max(ys1))


def _copy_table_slice(table: TableRegion, start: int, end: int) -> Optional[TableRegion]:
    cells = [list(row) for row in table.cells[start:end]]
    boxes = [list(row) for row in table.cell_bboxes[start:end]]
    if not cells:
        return None
    y_top, _ = _row_y_bounds(boxes[0])
    _, y_bottom = _row_y_bounds(boxes[-1])
    if y_top <= 0:
        y_top = table.y_top
    if y_bottom <= 0:
        y_bottom = table.y_bottom
    out = TableRegion(
        page=table.page,
        y_top=y_top,
        y_bottom=y_bottom,
        cells=cells,
        row_count=len(cells),
        col_count=table.col_count,
        cell_bboxes=boxes,
    )
    for attr in ("x_left", "x_right", "disable_vertical_merge"):
        if hasattr(table, attr):
            setattr(out, attr, getattr(table, attr))
    return out


def _row_signature(row: List[str]) -> str:
    parts = []
    for cell in row:
        key = _norm_table_key(cell)
        key = re.sub(r"\d+", "#", key)
        if key:
            parts.append(key)
    return "|".join(parts)


def _row_nonempty_count(row: List[str]) -> int:
    return sum(1 for cell in row if _clean_extracted_text(str(cell)))


def _row_similarity(a: List[str], b: List[str]) -> float:
    from difflib import SequenceMatcher
    return SequenceMatcher(None, _row_signature(a), _row_signature(b)).ratio()


def split_stacked_tables(table_regions: List[TableRegion], logger: Logger) -> List[TableRegion]:
    """
    Split two logical tables that the detector merged into one tall region.
    This happens when two ruled tables with the same grid are stacked on one
    page and a caption row sits between them.
    """
    out: List[TableRegion] = []
    split_count = 0
    for table in table_regions:
        if getattr(table, "skip_render", False) or table.row_count < 9 or table.col_count < 3:
            out.append(table)
            continue

        split_start = None
        for idx in range(3, table.row_count - 2):
            if _row_nonempty_count(table.cells[idx]) < 1 or _row_nonempty_count(table.cells[idx + 1]) < 2:
                continue
            previous_is_caption = _row_nonempty_count(table.cells[idx - 1]) <= 2
            first_header_repeat = _row_similarity(table.cells[idx], table.cells[0])
            second_header_repeat = _row_similarity(table.cells[idx + 1], table.cells[1])
            paired_repeat = (first_header_repeat + second_header_repeat) / 2.0
            if previous_is_caption and paired_repeat >= 0.55:
                split_start = idx
                break

        if split_start is None:
            out.append(table)
            continue

        first_end = split_start
        if split_start > 0 and _row_nonempty_count(table.cells[split_start - 1]) <= 2:
            first_end = split_start - 1

        first = _copy_table_slice(table, 0, first_end)
        second = _copy_table_slice(table, split_start, table.row_count)
        if first is None or second is None:
            out.append(table)
            continue
        out.extend([first, second])
        split_count += 1

    if split_count:
        logger.log(f"Split {split_count} stacked table(s)")
    return out


def detect_tables_img2table(pdf_path: str, logger: Logger, page_info: dict, pdf_lines: List[TextLine]) -> List[TableRegion]:
    """
    Detect tables using img2table by processing the PDF directly.
    Uses pdf_lines to accurately populate cell content.
    """
    if not IMG2TABLE_AVAILABLE:
        logger.log("img2table not available, skipping table detection")
        return []

    tables = []
    try:
        # Use simple import inside function to avoid circular dependency if any
        from img2table.document import PDF
        
        # Initialize PDF object
        # Note: img2table uses 200 DPI by default for PDF conversion
        pdf = PDF(src=pdf_path)
        
        # Check total pixels for safety
        max_pixels_safe = 40_000_000 # 40MP (Safe for 3x Zoom A4, but blocks 66MP)
        should_skip = False
        
        for p_idx in range(len(page_info)):
            p_num = p_idx + 1
            if p_num in page_info:
                w_pt = page_info[p_num].get("width", 0)
                h_pt = page_info[p_num].get("height", 0)
                # Convert to 200 DPI pixels
                w_px = w_pt * (200/72)
                h_px = h_pt * (200/72)
                total_px = w_px * h_px
                
                if total_px > max_pixels_safe:
                    logger.log(f"  img2table: Page {p_num} is too large (~{total_px/1e6:.1f}MP). Skipping img2table to prevent hang.")
                    should_skip = True
                    break
        
        if should_skip:
            return []

        logger.log("  img2table: Starting extraction (this may take time for large files)...")

        # Extract tables with optimized parameters
        # borderless_tables=False: avoid false positives on text-only layouts
        # (e.g. "Noi nhan" + signature blocks detected as borderless table)
        extracted_tables = pdf.extract_tables(
            implicit_rows=False,
            borderless_tables=False,
            min_confidence=50
        )
        
        # We need to calculate scale factors. 
        # img2table converts PDF to images at 200 DPI.
        # Coordinates in extracted_tables are in pixels relative to that 200 DPI image.
        # We need to convert them to PDF Points (72 DPI).
        # Scale Factor = 72 / 200 = 0.36
        
        scale_x = 72.0 / 200.0
        scale_y = 72.0 / 200.0
        
        # Iterate through results
        # extracted_tables is a dict {page_idx: [Table, ...]} where page_idx is 0-based
        for page_idx in sorted(extracted_tables.keys()):
            page_tables = extracted_tables[page_idx]
            page_num = page_idx + 1 # Convert to 1-based for our system
            
            # Get lines for this page once
            pdf_lines_page = [l for l in pdf_lines if l.page == page_num]
            
            for i, t in enumerate(page_tables):
                # t is img2table Table object
                
                # Scale BBox to Points
                # t.bbox is (x1, y1, x2, y2)
                x1_view = t.bbox.x1 * scale_x
                y1_view = t.bbox.y1 * scale_y
                x2_view = t.bbox.x2 * scale_x
                y2_view = t.bbox.y2 * scale_y
                
                # Restore BBox extraction for PDF Verification
                # We need BBoxes to know WHERE the row is.
                
                # Hybrid Approach:
                # 1. Text from DataFrame (Reliable)
                # 2. BBoxes from Content (Required for verification)
                
                # Dimensions
                if t.df is not None:
                    # Prepare DF
                    df_clean = t.df.fillna("")
                    rows, cols = df_clean.shape
                elif t.content:
                    rows = max(t.content.keys()) + 1
                    cols = max(len(row) for row in t.content.values()) if t.content else 0
                else:
                    rows, cols = 0, 0
                
                # Initialize grids
                cells_grid = [[""] * cols for _ in range(rows)]
                vis_cell_bboxes = [[(0.0, 0.0, 0.0, 0.0)] * cols for _ in range(rows)]
                
                try:
                    # 1. Populate BBoxes from Content (First pass needed for geometric text)
                    if t.content:
                        for r_idx, row_obj in t.content.items():
                            if r_idx < rows:
                                if isinstance(row_obj, dict):
                                    iterator = row_obj.items()
                                else:
                                    iterator = enumerate(row_obj)
                                
                                for c_idx, cell_obj in iterator:
                                    if c_idx < cols:
                                        cb = cell_obj.bbox
                                        scaled_bbox = (
                                            cb.x1 * scale_x,
                                            cb.y1 * scale_y,
                                            cb.x2 * scale_x,
                                            cb.y2 * scale_y
                                        )
                                        vis_cell_bboxes[r_idx][c_idx] = scaled_bbox

                    # 2. Populate Text: Prioritize Geometric Extraction with Smart Merging
                    for r in range(rows):
                        for c in range(cols):
                            bbox = vis_cell_bboxes[r][c]
                            if any(bbox) and pdf_lines_page:
                                # Get all lines in this cell
                                cell_lines = get_lines_in_rect(bbox, pdf_lines_page)
                                if cell_lines:
                                    # Merge them using the same logic as paragraphs
                                    # Use cell boundaries for merge logic
                                    # But merge_raw_paragraphs now expects margin_map.
                                    # Create a fake margin map for this single cell context.
                                    # The "Page" of these lines is p_idx+1.
                                    # cell_base_x = bbox[0]
                                    # cell_max_right = bbox[2]
                                    
                                    cell_margin_map = {page_num: (bbox[0], bbox[2])}
                                    
                                    merged_paras = merge_raw_paragraphs(cell_lines, cell_margin_map, logger)
                                    
                                    # Join paragraphs with newline (standard cell behavior)
                                    # But user requested: "Nội dung trong một ô là một paragraph" (Content in a cell is A paragraph).
                                    # "Qua ô khác thì áp dụng thuật toán lại từ đầu." (Move to next cell, apply again).
                                    # If the algorithm yields multiple paragraphs (e.g. explicitly split), we join them with newlines or spaces?
                                    # "Nội dung trong một ô là một paragraph" implies result should be ONE paragraph?
                                    # Or maybe it means "Treat content as paragraphs and merge accordingly".
                                    # Let's join with "\n" if multiple paragraphs are detected.
                                    
                                    full_cell_text = "\n".join([p[0] for p in merged_paras])
                                    geo_text = clean_ocr_cell_text(full_cell_text)

                                    # Cross-validate with DataFrame text:
                                    # If geometric extraction has EXTRA text that DataFrame
                                    # doesn't (e.g. text from adjacent cell bleeding in),
                                    # prefer DataFrame text as it's more reliable
                                    df_text = ""
                                    if t.df is not None and r < len(df_clean) and c < len(df_clean.columns):
                                        df_text = str(df_clean.iloc[r, c]).strip()
                                        while df_text.startswith('|'):
                                            df_text = df_text[1:].strip()

                                    if df_text and geo_text and geo_text != df_text:
                                        # Cross-validate: if DataFrame text is a clean
                                        # substring of geometric text, the extra content
                                        # in geometric is likely bleed from adjacent cell.
                                        # Prefer DataFrame in that case.
                                        # Normalize all whitespace (PDF uses \xa0 non-breaking space)
                                        geo_flat = re.sub(r'[\xa0\s]+', ' ', geo_text).strip()
                                        df_flat = re.sub(r'[\xa0\s]+', ' ', df_text).strip()
                                        if df_flat and df_flat in geo_flat and geo_flat != df_flat:
                                            cells_grid[r][c] = df_text
                                        else:
                                            cells_grid[r][c] = geo_text
                                    else:
                                        cells_grid[r][c] = geo_text
                                    continue

                            # Fallback to img2table DF if geometric failed
                            if t.df is not None and r < len(df_clean) and c < len(df_clean.columns):
                                val = str(df_clean.iloc[r, c]).strip()
                                while val.startswith('|'):
                                    val = val[1:].strip()
                                cells_grid[r][c] = val

                                        
                except Exception as e:
                    logger.log(f"  Warning: Could not extract cell data for p{page_num} t{i}: {e}")

                tables.append(TableRegion(
                    page=page_num,
                    y_top=y1_view,
                    y_bottom=y2_view,
                    cells=cells_grid,
                    row_count=rows,
                    col_count=cols,
                    cell_bboxes=vis_cell_bboxes
                ))
                
                logger.log(f"  img2table (Hybrid Mode): p{page_num} Table {i+1} {rows}x{cols} @ y={y1_view:.1f}-{y2_view:.1f}")

    except Exception as e:
        logger.log(f"img2table error: {e}")
        import traceback
        traceback.print_exc()
        
    return tables


# ============================================================================
# TABLE ENGINE CONFIGURATION
# ============================================================================

def get_table_config() -> dict:
    """
    Read table extraction settings from settings.ini.
    Returns dict with 'engine' and 'device' keys.
    """
    import configparser
    from scanindex.infra.paths import get_base_dir
    config = configparser.ConfigParser()
    config.read(os.path.join(get_base_dir(), "settings.ini"))
    
    return {
        "engine": "hybrid",
        "device": "cpu"
    }



def _mark_table_source(tables: List[TableRegion], source: str) -> List[TableRegion]:
    for table in tables:
        setattr(table, "source", source)
    return tables


def _table_nonempty_ratio(table: TableRegion) -> float:
    cells = [str(cell).strip() for row in getattr(table, "cells", []) for cell in row]
    if not cells:
        return 0.0
    return sum(1 for cell in cells if cell) / len(cells)


def _has_stacked_header_repeat(table: TableRegion) -> bool:
    rows = getattr(table, "cells", []) or []
    if len(rows) < 8 or getattr(table, "col_count", 0) < 3:
        return False
    for idx in range(3, len(rows) - 2):
        if _row_nonempty_count(rows[idx]) < 1 or _row_nonempty_count(rows[idx + 1]) < 2:
            continue
        if _row_nonempty_count(rows[idx - 1]) > 2:
            continue
        paired = (_row_similarity(rows[idx], rows[0]) + _row_similarity(rows[idx + 1], rows[1])) / 2.0
        if paired >= 0.55:
            return True
    return False


def _fragmented_body_row_ratio(table: TableRegion) -> float:
    rows = getattr(table, "cells", []) or []
    cols = getattr(table, "col_count", 0) or 0
    if len(rows) < 6 or cols < 3:
        return 0.0

    start_idx = 1 if _table_has_header(table) else 0
    fragments = 0
    candidates = 0
    for row in rows[start_idx:]:
        row = list(row[:cols]) + [""] * max(0, cols - len(row))
        if _row_nonempty_count(row) == 0:
            continue
        candidates += 1
        leading_empty = (
            not _clean_extracted_text(row[0])
            and (cols < 2 or not _clean_extracted_text(row[1]))
        )
        has_body_text = any(_clean_extracted_text(cell) for cell in row[2:])
        if leading_empty and has_body_text:
            fragments += 1
    if not candidates:
        return 0.0
    return fragments / candidates


def _layout_table_bboxes(layout_regions_by_page: Optional[Dict[int, List[dict]]], page: int) -> List[Tuple[float, float, float, float]]:
    out = []
    for region in (layout_regions_by_page or {}).get(page, []):
        if region.get("type") != "table":
            continue
        bbox = region.get("bbox_pdf")
        if bbox and len(bbox) >= 4:
            out.append(tuple(float(v) for v in bbox[:4]))
    return out


def _candidate_table_score(table: TableRegion,
                           layout_bboxes: List[Tuple[float, float, float, float]]) -> float:
    rows = getattr(table, "row_count", 0) or 0
    cols = getattr(table, "col_count", 0) or 0
    if rows <= 0 or cols <= 0:
        return -100.0

    score = 0.0
    ratio = _table_nonempty_ratio(table)
    bbox = _table_bbox(table)

    # Strongly reject detector artifacts that turn paragraphs into very wide
    # or one-column tables. Real administrative tables in this pipeline have a
    # stable ruled grid and moderate column count.
    if cols == 1 and rows > 1:
        score -= 12.0
    if cols > 14:
        score -= 10.0 + (cols - 14) * 0.8
    elif cols > 10:
        score -= (cols - 10) * 0.7
    if rows <= 2 and cols > 12:
        score -= 8.0

    score += min(rows, 8) * 0.18
    score += min(cols, 8) * 0.35
    score += ratio * 1.2

    if _table_has_header(table):
        score += 1.6
    if _has_stacked_header_repeat(table):
        # A single detector region that contains a repeated header can be split
        # deterministically later. Prefer it over multiple partial detections
        # that may miss the first logical table's body rows.
        score += 12.0
    else:
        fragmentation = _fragmented_body_row_ratio(table)
        if fragmentation >= 0.25:
            score -= 1.0 + fragmentation * 4.0

    if layout_bboxes:
        best_overlap = max((_bbox_intersection_ratio(_table_bbox(table), lb) for lb in layout_bboxes), default=0.0)
        if best_overlap >= 0.35:
            score += 3.0 * best_overlap
        else:
            score -= 3.0

        # Penalize candidates that cover much more vertical area than the
        # layout table regions on the same page; this catches text-block false
        # positives while still allowing slight detector expansion.
        table_h = max(bbox[3] - bbox[1], 1.0)
        best_h = max((min(bbox[3], lb[3]) - max(bbox[1], lb[1]) for lb in layout_bboxes), default=0.0)
        if best_h > 0 and table_h > best_h * 1.8:
            score -= 2.0

    return score


def _candidate_set_score(tables: List[TableRegion],
                         layout_bboxes: List[Tuple[float, float, float, float]]) -> float:
    if not tables:
        return -2.0 * len(layout_bboxes)
    score = sum(_candidate_table_score(table, layout_bboxes) for table in tables)
    if layout_bboxes:
        matched = 0
        for lb in layout_bboxes:
            if max((_bbox_intersection_ratio(_table_bbox(table), lb) for table in tables), default=0.0) >= 0.35:
                matched += 1
        score += matched * 1.5
        score -= (len(layout_bboxes) - matched) * 1.2
        if len(tables) > len(layout_bboxes):
            score -= (len(tables) - len(layout_bboxes)) * 2.0
    return score


def _candidate_set_grid_cells(tables: List[TableRegion]) -> int:
    total = 0
    for table in tables:
        rows = max(0, int(getattr(table, "row_count", 0) or 0))
        cols = max(0, int(getattr(table, "col_count", 0) or 0))
        total += rows * cols
    return total


def _candidate_set_nonempty_cells(tables: List[TableRegion]) -> int:
    total = 0
    for table in tables:
        rows = int(getattr(table, "row_count", 0) or 0)
        cols = int(getattr(table, "col_count", 0) or 0)
        raw_cells = getattr(table, "cells", []) or []
        for r in range(rows):
            for c in range(cols):
                if r < len(raw_cells) and c < len(raw_cells[r]) and _clean_extracted_text(str(raw_cells[r][c])):
                    total += 1
    return total


def _candidate_set_layout_matches(tables: List[TableRegion],
                                  layout_bboxes: List[Tuple[float, float, float, float]]) -> int:
    if not layout_bboxes:
        return len(tables)
    matched = 0
    for lb in layout_bboxes:
        if max((_bbox_intersection_ratio(_table_bbox(table), lb) for table in tables), default=0.0) >= 0.35:
            matched += 1
    return matched


def _candidate_set_structurally_usable(tables: List[TableRegion]) -> bool:
    if not tables:
        return False
    for table in tables:
        rows = getattr(table, "row_count", 0) or 0
        cols = getattr(table, "col_count", 0) or 0
        if rows <= 0 or cols <= 0:
            return False
        if cols == 1 and rows > 1:
            return False
        if cols > 14:
            return False
    return True


def _select_table_candidates_for_page(page: int,
                                      candidate_sets: Dict[str, List[TableRegion]],
                                      layout_regions_by_page: Optional[Dict[int, List[dict]]],
                                      logger: Logger) -> List[TableRegion]:
    layout_bboxes = _layout_table_bboxes(layout_regions_by_page, page)
    scored = []
    for source, tables in candidate_sets.items():
        if not tables:
            continue
        score = _candidate_set_score(tables, layout_bboxes)
        scored.append((score, source, tables))
    if not scored:
        return []

    scored.sort(key=lambda item: item[0], reverse=True)
    best_score, best_source, best_tables = scored[0]

    # Conservative tie-break: when Img2Table and GMFT-ONNX find the same number
    # of tables with the same column grid, prefer the candidate with more row
    # boundaries if scores are close. This keeps extra header/number rows that
    # can be normalized later instead of losing them permanently.
    score_by_source = {source: (score, tables) for score, source, tables in scored}
    if "gmft_onnx" in score_by_source:
        onnx_score, onnx_tables = score_by_source["gmft_onnx"]
        if any(_has_stacked_header_repeat(table) for table in onnx_tables) and best_score - onnx_score <= 3.0:
            best_score, best_source, best_tables = onnx_score, "gmft_onnx", onnx_tables

    if best_source == "img2table" and "gmft_onnx" in score_by_source:
        onnx_score, onnx_tables = score_by_source["gmft_onnx"]
        if len(onnx_tables) == len(best_tables) and best_score - onnx_score <= 2.0:
            onnx_sorted = sorted(onnx_tables, key=lambda t: (t.y_top, t.y_bottom))
            best_sorted = sorted(best_tables, key=lambda t: (t.y_top, t.y_bottom))
            comparable = all(o.col_count == b.col_count for o, b in zip(onnx_sorted, best_sorted))
            row_better = any(o.row_count > b.row_count for o, b in zip(onnx_sorted, best_sorted))
            onnx_fragmented = max((_fragmented_body_row_ratio(t) for t in onnx_sorted), default=0.0) >= 0.25
            if comparable and row_better and not onnx_fragmented:
                best_score, best_source, best_tables = onnx_score, "gmft_onnx", onnx_tables

    if best_source == "rapidtable_slanet":
        established = [
            (score, source, tables)
            for score, source, tables in scored
            if source in {"gmft_onnx", "img2table", "legacy_gmft"}
        ]
        if established:
            est_score, est_source, est_tables = max(established, key=lambda item: item[0])
            rapid_matches = _candidate_set_layout_matches(best_tables, layout_bboxes)
            est_matches = _candidate_set_layout_matches(est_tables, layout_bboxes)
            rapid_cells = _candidate_set_grid_cells(best_tables)
            est_cells = _candidate_set_grid_cells(est_tables)
            rapid_is_richer_grid = rapid_cells >= max(est_cells * 1.4, est_cells + 6)
            if (
                est_matches >= rapid_matches
                and best_score - est_score <= 1.0
                and _candidate_set_structurally_usable(est_tables)
                and not rapid_is_richer_grid
            ):
                best_score, best_source, best_tables = est_score, est_source, est_tables
    elif "rapidtable_slanet" in score_by_source:
        rapid_score, rapid_tables = score_by_source["rapidtable_slanet"]
        rapid_matches = _candidate_set_layout_matches(rapid_tables, layout_bboxes)
        best_matches = _candidate_set_layout_matches(best_tables, layout_bboxes)
        rapid_cells = _candidate_set_grid_cells(rapid_tables)
        best_cells = _candidate_set_grid_cells(best_tables)
        if (
            rapid_matches >= best_matches
            and best_score - rapid_score <= 1.5
            and rapid_cells >= max(best_cells * 1.45, best_cells + 8)
        ):
            best_score, best_source, best_tables = rapid_score, "rapidtable_slanet", rapid_tables

    summary = ", ".join(f"{src}:{score:.2f}/{len(tbls)}" for score, src, tbls in scored)
    logger.log(f"  Page {page}: Selected {best_source} by quality score ({summary})")
    return best_tables


def detect_tables_hybrid(
    pdf_path: str, 
    logger: Logger, 
    page_info: dict, 
    pdf_lines: List[TextLine],
    device: str = "auto",
    layout_regions_by_page: Optional[Dict[int, List[dict]]] = None
) -> List[TableRegion]:
    """
    Hybrid detection: run available table engines, then choose page candidates
    by table-likeness and layout overlap rather than raw area.
    """
    logger.log("Running Hybrid Table Detection (GMFT-ONNX + Img2Table + RapidTable ensemble)...")
    
    import concurrent.futures

    # Define wrappers to handle availability and execution
    def _run_gmft_onnx():
        if GMFT_ONNX_AVAILABLE:
            try:
                logger.log("Hybrid: Starting GMFT-ONNX thread...")
                res = detect_tables_gmft_onnx(pdf_path, logger, page_info, pdf_lines, device)
                logger.log(f"Hybrid: GMFT-ONNX thread finished. Found {len(res)} tables.")
                return _mark_table_source(res, "gmft_onnx")
            except Exception as e:
                logger.log(f"Hybrid: GMFT-ONNX failed: {e}")
                return []
        logger.log("Hybrid: GMFT-ONNX not available")
        return []

    def _run_legacy_gmft():
        if GMFT_AVAILABLE and detect_tables_gmft is not None:
            try:
                logger.log("Hybrid: Starting legacy PyTorch GMFT thread...")
                res = detect_tables_gmft(pdf_path, logger, page_info, pdf_lines, device)
                logger.log(f"Hybrid: legacy PyTorch GMFT thread finished. Found {len(res)} tables.")
                return _mark_table_source(res, "legacy_gmft")
            except Exception as e:
                logger.log(f"Hybrid: legacy PyTorch GMFT failed: {e}")
                return []
        logger.log("Hybrid: legacy PyTorch GMFT not enabled")
        return []

    def _run_img2table():
        if IMG2TABLE_AVAILABLE:
            try:
                logger.log("Hybrid: Starting Img2Table thread...")
                res = detect_tables_img2table(pdf_path, logger, page_info, pdf_lines)
                logger.log(f"Hybrid: Img2Table thread finished. Found {len(res)} tables.")
                return _mark_table_source(res, "img2table")
            except Exception as e:
                logger.log(f"Hybrid: Img2Table failed: {e}")
                return []
        else:
            logger.log("Hybrid: Img2Table not available")
            return []

    def _run_rapidtable():
        if not RAPIDTABLE_AVAILABLE or detect_tables_rapidtable_slanet is None:
            logger.log("Hybrid: RapidTable SLANet+ not available")
            return []
        if not layout_regions_by_page:
            logger.log("Hybrid: RapidTable SLANet+ skipped because layout table boxes are unavailable")
            return []
        try:
            logger.log("Hybrid: Starting RapidTable SLANet+ thread...")
            lines_by_page: Dict[int, List[TextLine]] = {}
            for line in pdf_lines or []:
                lines_by_page.setdefault(line.page, []).append(line)

            def _resolve_cell_text(page_num: int, bbox: Tuple[float, float, float, float]) -> str:
                cell_lines = get_lines_in_rect(bbox, lines_by_page.get(page_num, []))
                return _text_from_table_cell_lines(cell_lines, page_num, bbox[0], bbox[2], logger)

            res = detect_tables_rapidtable_slanet(
                pdf_path,
                logger,
                page_info,
                pdf_lines,
                layout_regions_by_page,
                text_resolver=_resolve_cell_text,
            )
            logger.log(f"Hybrid: RapidTable SLANet+ thread finished. Found {len(res)} tables.")
            return _mark_table_source(res, "rapidtable_slanet")
        except Exception as e:
            logger.log(f"Hybrid: RapidTable SLANet+ failed: {e}")
            return []

    # Run in parallel
    tables_onnx = []
    tables_img2table = []
    tables_legacy = []
    tables_rapidtable = []
    
    workers = 2
    if RAPIDTABLE_AVAILABLE and detect_tables_rapidtable_slanet is not None:
        workers += 1
    if GMFT_AVAILABLE and detect_tables_gmft is not None:
        workers += 1
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        future_onnx = executor.submit(_run_gmft_onnx)
        future_img = executor.submit(_run_img2table)
        future_rapid = executor.submit(_run_rapidtable)
        future_legacy = executor.submit(_run_legacy_gmft) if GMFT_AVAILABLE and detect_tables_gmft is not None else None
        
        tables_onnx = future_onnx.result()
        tables_img2table = future_img.result()
        tables_rapidtable = future_rapid.result()
        tables_legacy = future_legacy.result() if future_legacy is not None else []

    # Group by Page
    onnx_by_page = {}
    for t in tables_onnx:
        if t.page not in onnx_by_page: onnx_by_page[t.page] = []
        onnx_by_page[t.page].append(t)
        
    img2_by_page = {}
    for t in tables_img2table:
        if t.page not in img2_by_page: img2_by_page[t.page] = []
        img2_by_page[t.page].append(t)

    legacy_by_page = {}
    for t in tables_legacy:
        if t.page not in legacy_by_page: legacy_by_page[t.page] = []
        legacy_by_page[t.page].append(t)

    rapid_by_page = {}
    for t in tables_rapidtable:
        if t.page not in rapid_by_page: rapid_by_page[t.page] = []
        rapid_by_page[t.page].append(t)
        
    final_tables = []
    all_pages = set(onnx_by_page.keys()) | set(img2_by_page.keys()) | set(legacy_by_page.keys()) | set(rapid_by_page.keys())
    
    for page in sorted(all_pages):
        chosen = _select_table_candidates_for_page(
            page,
            {
                "gmft_onnx": onnx_by_page.get(page, []),
                "img2table": img2_by_page.get(page, []),
                "legacy_gmft": legacy_by_page.get(page, []),
                "rapidtable_slanet": rapid_by_page.get(page, []),
            },
            layout_regions_by_page,
            logger,
        )
        final_tables.extend(chosen)
            
    return final_tables


def _group_tables_by_page(tables: List[TableRegion]) -> Dict[int, List[TableRegion]]:
    by_page: Dict[int, List[TableRegion]] = {}
    for table in tables or []:
        by_page.setdefault(int(getattr(table, "page", 0) or 0), []).append(table)
    for page_tables in by_page.values():
        page_tables.sort(
            key=lambda t: (
                float(getattr(t, "y_top", 0.0) or 0.0),
                float(getattr(t, "x_left", 0.0) or 0.0),
            )
        )
    return by_page


def _layout_pages_with_tables(layout_regions_by_page: Optional[Dict[int, List[dict]]]) -> List[int]:
    pages = []
    for page, regions in (layout_regions_by_page or {}).items():
        if any(region.get("type") == "table" and region.get("bbox_pdf") for region in regions or []):
            pages.append(int(page))
    return sorted(set(pages))


def _same_table_shapes(left: List[TableRegion], right: List[TableRegion]) -> bool:
    if len(left) != len(right) or not left:
        return False
    left_shapes = [
        (int(getattr(t, "row_count", 0) or 0), int(getattr(t, "col_count", 0) or 0))
        for t in sorted(left, key=lambda t: (float(getattr(t, "y_top", 0.0) or 0.0), float(getattr(t, "x_left", 0.0) or 0.0)))
    ]
    right_shapes = [
        (int(getattr(t, "row_count", 0) or 0), int(getattr(t, "col_count", 0) or 0))
        for t in sorted(right, key=lambda t: (float(getattr(t, "y_top", 0.0) or 0.0), float(getattr(t, "x_left", 0.0) or 0.0)))
    ]
    return all(lr > 0 and lc > 0 and (lr, lc) == (rr, rc) for (lr, lc), (rr, rc) in zip(left_shapes, right_shapes))


def _candidate_set_empty_cell_ratio(tables: List[TableRegion]) -> float:
    total = 0
    empty = 0
    for table in tables or []:
        rows = int(getattr(table, "row_count", 0) or 0)
        cols = int(getattr(table, "col_count", 0) or 0)
        raw_cells = getattr(table, "cells", []) or []
        for r in range(rows):
            for c in range(cols):
                total += 1
                text = ""
                if r < len(raw_cells) and c < len(raw_cells[r]):
                    text = _clean_extracted_text(str(raw_cells[r][c]))
                if not text:
                    empty += 1
    return empty / total if total else 1.0


def _candidate_set_average_ocr_fit(tables: List[TableRegion], pdf_lines: List[TextLine]) -> float:
    if not tables:
        return -5.0
    try:
        from scanindex.core.tables.postprocess_v2 import table_ocr_fit_score
    except Exception:
        return 0.0
    return sum(table_ocr_fit_score(table, pdf_lines) for table in tables) / max(len(tables), 1)


def _choose_docling_first_candidates(
    page: int,
    candidate_sets: Dict[str, List[TableRegion]],
    layout_regions_by_page: Optional[Dict[int, List[dict]]],
    pdf_lines: List[TextLine],
    logger: Logger,
) -> List[TableRegion]:
    """
    Geometry/OCR consensus selector for GMFT and Docling TableFormer v1 ONNX.

    Both engines run on the same DocLayout table boxes. Selection is based on
    layout overlap, OCR-box fit, and grid richness. Raw empty-cell ratio is kept
    as a diagnostic only because some engines assign text later in the common
    geometry postprocess.
    """
    docling_tables = candidate_sets.get("docling_tableformer", []) or []
    gmft_tables = candidate_sets.get("gmft_onnx_layout", []) or []
    layout_bboxes = _layout_table_bboxes(layout_regions_by_page, page)

    if not docling_tables:
        return gmft_tables
    if not gmft_tables:
        return docling_tables

    docling_score = _candidate_set_score(docling_tables, layout_bboxes)
    gmft_score = _candidate_set_score(gmft_tables, layout_bboxes)
    docling_matches = _candidate_set_layout_matches(docling_tables, layout_bboxes)
    gmft_matches = _candidate_set_layout_matches(gmft_tables, layout_bboxes)
    docling_usable = _candidate_set_structurally_usable(docling_tables)
    gmft_usable = _candidate_set_structurally_usable(gmft_tables)
    docling_ocr = _candidate_set_average_ocr_fit(docling_tables, pdf_lines)
    gmft_ocr = _candidate_set_average_ocr_fit(gmft_tables, pdf_lines)
    docling_empty = _candidate_set_empty_cell_ratio(docling_tables)
    gmft_empty = _candidate_set_empty_cell_ratio(gmft_tables)
    docling_cells = _candidate_set_grid_cells(docling_tables)
    gmft_cells = _candidate_set_grid_cells(gmft_tables)
    docling_nonempty = _candidate_set_nonempty_cells(docling_tables)
    gmft_nonempty = _candidate_set_nonempty_cells(gmft_tables)

    use_gmft = False
    reason = ""
    if not docling_usable and gmft_usable:
        use_gmft = True
        reason = "docling unusable"
    elif gmft_matches > docling_matches:
        use_gmft = True
        reason = "gmft matches more layout boxes"
    elif gmft_usable and gmft_ocr >= docling_ocr + 0.70 and gmft_score >= docling_score - 1.50:
        use_gmft = True
        reason = "gmft has materially better OCR coverage"
    elif (
        gmft_usable
        and gmft_matches == docling_matches
        and gmft_ocr >= docling_ocr + 0.03
        and gmft_score >= docling_score - 0.50
        and gmft_cells >= max(1, int(docling_cells * 0.70))
    ):
        use_gmft = True
        reason = "gmft has cleaner OCR fit with comparable geometry"
    elif (
        gmft_usable
        and gmft_matches == docling_matches
        and gmft_cells >= docling_cells + max(4, int(docling_cells * 0.12))
        and gmft_ocr >= docling_ocr - 0.35
        and gmft_score >= docling_score - 1.00
    ):
        use_gmft = True
        reason = "gmft provides richer grid with comparable geometry/OCR fit"

    summary = (
        f"docling_v1_onnx:S{docling_score:.2f}/O{docling_ocr:.2f}/E{docling_empty:.2f}/"
        f"M{docling_matches}/C{docling_cells}/N{docling_nonempty}; "
        f"gmft:S{gmft_score:.2f}/O{gmft_ocr:.2f}/E{gmft_empty:.2f}/"
        f"M{gmft_matches}/C{gmft_cells}/N{gmft_nonempty}"
    )
    if use_gmft:
        logger.log(f"  Page {page}: Selected gmft_onnx_layout by GMFT+Docling selector ({reason}; {summary})")
        return gmft_tables

    logger.log(f"  Page {page}: Selected docling_tableformer by Docling-first selector ({summary})")
    return docling_tables


def detect_tables_doclayout_gmft_docling(
    pdf_path: str,
    logger: Logger,
    page_info: dict,
    pdf_lines: List[TextLine],
    layout_regions_by_page: Optional[Dict[int, List[dict]]] = None,
) -> List[TableRegion]:
    """
    Production table pipeline:
    DocLayout provides table bboxes, GMFT-ONNX and Docling TableFormer v1 ONNX
    recognize structure on those same regions, then a geometry/OCR-fit scorer
    chooses the best page candidate set. No text-specific hardcoding is used.
    """
    if not layout_regions_by_page:
        return []

    layout_pages = _layout_pages_with_tables(layout_regions_by_page)
    if not layout_pages:
        return []

    logger.log("Running DocLayout-anchored tables (DocLayout bbox + GMFT-ONNX + Docling TableFormer v1 ONNX)...")

    import concurrent.futures

    def _run_gmft_layout():
        if not GMFT_ONNX_AVAILABLE or detect_tables_gmft_onnx_on_layout_regions is None:
            logger.log("DocLayout table pipeline: GMFT-ONNX structure recognizer not available")
            return []
        try:
            logger.log("DocLayout table pipeline: Starting GMFT-ONNX structure recognizer...")
            res = detect_tables_gmft_onnx_on_layout_regions(
                pdf_path,
                logger,
                page_info,
                pdf_lines,
                layout_regions_by_page,
                "cpu",
            )
            logger.log(f"DocLayout table pipeline: GMFT-ONNX finished. Found {len(res)} tables.")
            return _mark_table_source(res, "gmft_onnx_layout")
        except Exception as exc:
            logger.log(f"DocLayout table pipeline: GMFT-ONNX failed: {exc}")
            return []

    def _run_docling():
        if not DOCLING_TABLEFORMER_AVAILABLE or detect_tables_docling_tableformer_v1_onnx is None:
            logger.log("DocLayout table pipeline: Docling TableFormer v1 ONNX not available")
            return []
        try:
            logger.log("DocLayout table pipeline: Starting Docling TableFormer v1 ONNX...")
            res = detect_tables_docling_tableformer_v1_onnx(
                pdf_path,
                logger,
                page_info,
                pdf_lines,
                layout_regions_by_page,
                dpi=144,
                pad_pt=0.0,
                num_threads=4,
            )
            logger.log(f"DocLayout table pipeline: Docling TableFormer v1 ONNX finished. Found {len(res)} tables.")
            return _mark_table_source(res, "docling_tableformer")
        except Exception as exc:
            logger.log(f"DocLayout table pipeline: Docling TableFormer v1 ONNX failed: {exc}")
            return []

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        future_gmft = executor.submit(_run_gmft_layout)
        future_docling = executor.submit(_run_docling)
        tables_gmft = future_gmft.result()
        tables_docling = future_docling.result()

    gmft_by_page = _group_tables_by_page(tables_gmft)
    docling_by_page = _group_tables_by_page(tables_docling)

    final_tables: List[TableRegion] = []
    for page in layout_pages:
        candidate_sets = {
            "gmft_onnx_layout": gmft_by_page.get(page, []),
            "docling_tableformer": docling_by_page.get(page, []),
        }
        chosen_tables = _choose_docling_first_candidates(
            page,
            candidate_sets,
            layout_regions_by_page,
            pdf_lines,
            logger,
        )
        final_tables.extend(chosen_tables)

    logger.log(f"DocLayout table pipeline: selected {len(final_tables)} tables")
    return final_tables


def detect_tables_doclayout_rapidtable(
    pdf_path: str,
    logger: Logger,
    page_info: dict,
    pdf_lines: List[TextLine],
    layout_regions_by_page: Optional[Dict[int, List[dict]]] = None,
) -> List[TableRegion]:
    """
    Portable-first table pipeline:
    DocLayout provides table bboxes and RapidTable SLANet+ recognizes structure.
    The common geometry/OCR postprocess still assigns and repairs cell text.
    """
    if not layout_regions_by_page:
        return []
    if not RAPIDTABLE_AVAILABLE or detect_tables_rapidtable_slanet is None:
        logger.log("RapidTable primary pipeline: RapidTable SLANet+ not available")
        return []

    layout_pages = _layout_pages_with_tables(layout_regions_by_page)
    if not layout_pages:
        return []

    logger.log("Running DocLayout-anchored tables (DocLayout bbox + RapidTable SLANet+ primary)...")
    try:
        lines_by_page: Dict[int, List[TextLine]] = {}
        for line in pdf_lines or []:
            lines_by_page.setdefault(line.page, []).append(line)

        def _resolve_cell_text(page_num: int, bbox: Tuple[float, float, float, float]) -> str:
            cell_lines = get_lines_in_rect(bbox, lines_by_page.get(page_num, []))
            return _text_from_table_cell_lines(cell_lines, page_num, bbox[0], bbox[2], logger)

        tables = detect_tables_rapidtable_slanet(
            pdf_path,
            logger,
            page_info,
            pdf_lines,
            layout_regions_by_page,
            text_resolver=_resolve_cell_text,
        )
        tables = _mark_table_source(tables, "rapidtable_slanet")
        logger.log(f"RapidTable primary pipeline: selected {len(tables)} tables")
        return tables
    except Exception as exc:
        logger.log(f"RapidTable primary pipeline failed: {exc}")
        return []


def detect_tables(
    pdf_path: str, 
    logger: Logger, 
    page_info: dict, 
    pdf_lines: List[TextLine],
    layout_regions_by_page: Optional[Dict[int, List[dict]]] = None
) -> List[TableRegion]:
    """
    Unified table detection function.
    """
    doclayout_tables = detect_tables_doclayout_gmft_docling(
        pdf_path,
        logger,
        page_info,
        pdf_lines,
        layout_regions_by_page,
    )
    if not doclayout_tables:
        logger.log("DocLayout GMFT + Docling TableFormer v1 ONNX produced no tables; no alternate table fallback is enabled")
    return doclayout_tables


# ============================================================================
# CREATE DOCX WITH POSITIONS
# ============================================================================

def set_paragraph_font(para, font_name="Times New Roman", font_size=14, bold: Optional[bool] = None):
    """Set font for all runs in paragraph."""
    for run in para.runs:
        run.font.name = font_name
        run.font.size = Pt(font_size)
        if bold is not None:
            run.font.bold = bool(bold)
        # Set for Asian text too
        run._element.rPr.rFonts.set(qn('w:eastAsia'), font_name)


# def get_lines_in_rect moved up


def _same_visual_cell_region(
    bbox_a: Tuple[float, float, float, float],
    bbox_b: Tuple[float, float, float, float],
) -> bool:
    """True only when the structure engine duplicated the same visual cell bbox."""
    if not (any(bbox_a) and any(bbox_b)):
        return False
    area_a = max((bbox_a[2] - bbox_a[0]) * (bbox_a[3] - bbox_a[1]), 1e-6)
    area_b = max((bbox_b[2] - bbox_b[0]) * (bbox_b[3] - bbox_b[1]), 1e-6)
    ix0 = max(bbox_a[0], bbox_b[0])
    iy0 = max(bbox_a[1], bbox_b[1])
    ix1 = min(bbox_a[2], bbox_b[2])
    iy1 = min(bbox_a[3], bbox_b[3])
    inter = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
    return inter / area_a >= 0.88 and inter / area_b >= 0.88


def _word_center_in_bbox(word: dict, bbox: Tuple[float, float, float, float]) -> bool:
    try:
        x = float(word.get("x", 0.0))
        y = float(word.get("y", 0.0))
        w = float(word.get("w", 0.0))
        h = float(word.get("h", 0.0))
    except Exception:
        return False
    cx = x + w / 2.0
    cy = y + h / 2.0
    x0, y0, x1, y1 = bbox
    return x0 <= cx <= x1 and y0 <= cy <= y1


def _cell_words_for_bbox(
    bbox: Tuple[float, float, float, float],
    pdf_lines_page: List[TextLine],
) -> List[Tuple[dict, TextLine]]:
    words: List[Tuple[dict, TextLine]] = []
    for line in pdf_lines_page:
        if _bbox_overlap_ratio(_line_bbox(line), bbox) <= 0 and not (
            bbox[0] <= line.x + line.width / 2.0 <= bbox[2]
            and bbox[1] <= line.y + line.height / 2.0 <= bbox[3]
        ):
            continue
        for word in _word_items_for_line(line):
            if _word_center_in_bbox(word, bbox):
                words.append((word, line))
    return words


def _cell_text_should_be_bold(
    bbox: Tuple[float, float, float, float],
    pdf_lines_page: List[TextLine],
) -> bool:
    cell_words = _cell_words_for_bbox(bbox, pdf_lines_page)
    if not cell_words:
        return False
    known = [(word, line) for word, line in cell_words if _has_known_gray(word.get("fg_gray", 128))]
    if len(known) < max(1, int(len(cell_words) * 0.45)):
        return False
    bold_count = sum(1 for word, line in known if _word_is_visually_bold(word, line))
    return bold_count / max(len(known), 1) >= 0.45



def add_table_to_doc(doc: Document, table_region: TableRegion, pdf_lines_page: List[TextLine], logger: Logger, page_info: dict = None):
    """Add a table to the document with PDF-verified horizontal merging."""
    rows = table_region.row_count
    cols = table_region.col_count
    pg_info = (page_info or {}).get(table_region.page, {})
    table_font_size = int(round(float(pg_info.get("docx_table_font_pt", pg_info.get("docx_body_font_pt", 13)))))
    
    table = doc.add_table(rows=rows, cols=cols)
    table.style = 'Table Grid'
    table.alignment = WD_TABLE_ALIGNMENT.CENTER

    # Set proportional column widths from cell bboxes
    # Find a row with distinct (non-merged) column bboxes for width reference
    col_widths_pt = [0.0] * cols
    for ref_row in range(rows):
        widths = []
        for c in range(cols):
            if ref_row < len(table_region.cell_bboxes) and c < len(table_region.cell_bboxes[ref_row]):
                bx = table_region.cell_bboxes[ref_row][c]
                widths.append(bx[2] - bx[0] if any(bx) else 0)
            else:
                widths.append(0)
        # Good reference row: all widths > 0 AND no adjacent cells share same bbox (not merged)
        all_positive = all(w > 0 for w in widths)
        no_merges = True
        if all_positive and ref_row < len(table_region.cell_bboxes):
            row_bboxes = table_region.cell_bboxes[ref_row]
            for c in range(len(row_bboxes) - 1):
                if row_bboxes[c] == row_bboxes[c + 1]:
                    no_merges = False
                    break
        if all_positive and no_merges:
            col_widths_pt = widths
            break

    total_w = sum(col_widths_pt)
    if total_w > 0:
        # Set fixed layout to prevent Word from auto-resizing
        tbl_xml = table._tbl
        tblPr = tbl_xml.tblPr if tbl_xml.tblPr is not None else OxmlElement('w:tblPr')
        # Remove existing tblLayout if any
        existing_layout = tblPr.find(qn('w:tblLayout'))
        if existing_layout is not None:
            tblPr.remove(existing_layout)
        tblLayout = OxmlElement('w:tblLayout')
        tblLayout.set(qn('w:type'), 'fixed')
        tblPr.append(tblLayout)
        # Also disable autofit
        autofit = OxmlElement('w:tblW')
        autofit.set(qn('w:w'), '0')
        autofit.set(qn('w:type'), 'auto')
        existing_w = tblPr.find(qn('w:tblW'))
        if existing_w is not None:
            tblPr.remove(existing_w)
        tblPr.append(autofit)

        # Apply proportional widths using Twips for precision
        # Use actual DOCX content area width (depends on page orientation)
        pg_num = table_region.page
        _pi = page_info or {}
        pg_w = _pi.get(pg_num, {}).get("width", 595)
        pg_info = _pi.get(pg_num, {})
        left_margin_pt = float(pg_info.get("docx_left_margin_pt", pg_w * 0.10))
        right_margin_pt = float(pg_info.get("docx_right_margin_pt", pg_w * 0.07))
        content_pt = max(pg_w - left_margin_pt - right_margin_pt, pg_w * 0.45)
        content_cm = content_pt / 72.0 * 2.54

        for c in range(cols):
            col_ratio = col_widths_pt[c] / total_w
            col_cm = col_ratio * content_cm
            col_width = Cm(col_cm)
            for row_obj in table.rows:
                row_obj.cells[c].width = col_width

    # Pre-calculate row y-ranges and PDF text for verification
    row_pdf_texts = {}
    for r in range(rows):
        # Find min y and max y for this row based on cell bboxes
        min_y = 10000
        max_y = 0
        has_bbox = False
        for c in range(cols):
            bbox = table_region.cell_bboxes[r][c]
            if any(bbox):
                min_y = min(min_y, bbox[1])
                max_y = max(max_y, bbox[3])
                has_bbox = True
        
        if has_bbox:
            # Extract text lines from PDF that fall within this row's Y range
            # Add some tolerance
            tolerance = 2.0
            row_lines = [l.text for l in pdf_lines_page if min_y - tolerance <= l.y + l.height/2 <= max_y + tolerance]
            row_pdf_texts[r] = " ".join(row_lines)
        else:
            row_pdf_texts[r] = ""

    visited = set()
    
    for row_idx in range(rows):
        # Calculate table occurrences for this row 
        # (How many times each string appears in the table row)
        # We need this to compare with PDF counts.
        table_row_counts = {}
        if row_idx < len(table_region.cells):
             for val in table_region.cells[row_idx]:
                 val_str = str(val)
                 table_row_counts[val_str] = table_row_counts.get(val_str, 0) + 1
        
        pdf_row_text = row_pdf_texts.get(row_idx, "")
        
        for col_idx in range(cols):
            if (row_idx, col_idx) in visited:
                continue
                
            # Get current text
            current_text = ""
            if row_idx < len(table_region.cells) and col_idx < len(table_region.cells[row_idx]):
                 current_text = str(table_region.cells[row_idx][col_idx])
             
            colspan = 1
            rowspan = 1 # Always 1 as requested (No vertical merging)
            row_vals = table_region.cells[row_idx] if row_idx < len(table_region.cells) else []

            # Header cells from TATR sometimes land in the visual center of a
            # multi-column span with empty neighbor cells. Start the merge from
            # the first empty neighbor so the DOCX has one logical header cell
            # instead of a blank + text + blank sequence.
            if not current_text.strip() and row_idx == 0 and row_idx < len(table_region.cells):
                next_col = col_idx + 1
                while next_col < cols:
                    next_text = str(row_vals[next_col]) if next_col < len(row_vals) else ""
                    if next_text.strip():
                        if "CONGTAC" in _norm_table_key(next_text):
                            current_text = next_text
                            colspan = next_col - col_idx + 1
                            while col_idx + colspan < cols:
                                tail_text = str(row_vals[col_idx + colspan]) if col_idx + colspan < len(row_vals) else ""
                                if tail_text.strip():
                                    break
                                colspan += 1
                        break
                    next_col += 1

            # Section rows often have a numeric marker in the first column and
            # one label spanning every remaining column. Some detectors assign
            # the label only to the columns its glyphs touch and leave trailing
            # cells empty, so merge the whole row tail when the tail contains
            # only one distinct non-empty text value.
            if current_text.strip() and col_idx == 1 and row_vals:
                marker = _clean_extracted_text(str(row_vals[0])) if row_vals else ""
                tail = [
                    _clean_extracted_text(str(row_vals[c])) if c < len(row_vals) else ""
                    for c in range(1, cols)
                ]
                non_empty_tail = [value for value in tail if value]
                if (
                    re.fullmatch(r"\d{1,3}", marker or "")
                    and non_empty_tail
                    and len(set(non_empty_tail)) == 1
                    and _clean_extracted_text(current_text) == non_empty_tail[0]
                ):
                    colspan = cols - col_idx
             
            # Merge Logic: Horizontal Only + PDF Verification
            if current_text.strip():
                # Count in PDF
                # Use simple substring count. 
                # Normalize spaces for robust check?
                # For now simple count.
                pdf_count = pdf_row_text.count(current_text)
                
                # Count in Table Row
                table_count = table_row_counts.get(current_text, 1)
                
                # Check duplication
                if table_count > pdf_count and table_count > 1 and not is_numeric_cell(current_text):
                    # Likely an artifact -> Merge allowed
                    should_merge = True
                else:
                    # Real data duplicate -> Do not merge
                    should_merge = False
                
                if should_merge and colspan == 1:
                    # Calculate max mergeable span based on content match
                    while col_idx + colspan < cols:
                        next_text = ""
                        if row_idx < len(table_region.cells) and (col_idx + colspan) < len(table_region.cells[row_idx]):
                            next_text = str(table_region.cells[row_idx][col_idx + colspan])
                        
                        if next_text == current_text:
                            colspan += 1
                        else:
                            break
            
            # Mark visited
            for c in range(col_idx, col_idx + colspan):
                visited.add((row_idx, c))
            
            # Apply Merge
            cell = table.cell(row_idx, col_idx)
            
            if colspan > 1:
                try:
                    # Clear cells before merge
                    for mc in range(col_idx, col_idx + colspan):
                        table.cell(row_idx, mc).text = ""
                    # Merge Horizontal
                    right_cell = table.cell(row_idx, col_idx + colspan - 1)
                    cell.merge(right_cell)
                    # Remove extra paragraphs from merge
                    merged = table.cell(row_idx, col_idx)
                    while len(merged.paragraphs) > 1:
                        p_el = merged.paragraphs[-1]._element
                        p_el.getparent().remove(p_el)
                    logger.log(f"  Merged {colspan} cells at r{row_idx}c{col_idx} (Text: '{current_text}')")
                except Exception as e:
                    logger.log(f"  Merge failed at r{row_idx}c{col_idx}: {e}")
            
            # Set Text
            cell.text = current_text
            cell_bbox = (0.0, 0.0, 0.0, 0.0)
            if row_idx < len(table_region.cell_bboxes) and col_idx < len(table_region.cell_bboxes[row_idx]):
                cell_bbox = tuple(table_region.cell_bboxes[row_idx][col_idx])
            cell_bold = _cell_text_should_be_bold(cell_bbox, pdf_lines_page) if any(cell_bbox) else False
            
            # Formatting
            for para in cell.paragraphs:
                # Logic: Numeric -> Center, Text -> Left
                if is_numeric_cell(current_text):
                    para.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
                else:
                    para.alignment = WD_PARAGRAPH_ALIGNMENT.LEFT
                    
                if para.runs:
                    set_paragraph_font(para, font_size=table_font_size, bold=cell_bold if cell_bold else None)
                else:
                    run = para.add_run()
                    run.font.name = "Times New Roman"
                    run.font.size = Pt(table_font_size)
                    if cell_bold:
                        run.font.bold = True
    
    # Vertical merge detection: merge cells in same column with identical text
    # where cell bboxes are vertically aligned (same x0, x1)
    if not getattr(table_region, "disable_vertical_merge", False):
      for c in range(cols):
        r = 0
        while r < rows - 1:
            text_r = table_region.cells[r][c].strip() if r < len(table_region.cells) and c < len(table_region.cells[r]) else ""
            text_r1 = table_region.cells[r + 1][c].strip() if r + 1 < len(table_region.cells) and c < len(table_region.cells[r + 1]) else ""
            if not text_r or not text_r1 or text_r != text_r1:
                r += 1
                continue
            # Check bbox alignment (same column boundaries)
            bbox_r = table_region.cell_bboxes[r][c] if r < len(table_region.cell_bboxes) and c < len(table_region.cell_bboxes[r]) else (0, 0, 0, 0)
            bbox_r1 = table_region.cell_bboxes[r + 1][c] if r + 1 < len(table_region.cell_bboxes) and c < len(table_region.cell_bboxes[r + 1]) else (0, 0, 0, 0)
            if not _same_visual_cell_region(tuple(bbox_r), tuple(bbox_r1)):
                r += 1
                continue
            # Count span
            span = 1
            while r + span < rows:
                next_text = table_region.cells[r + span][c].strip() if r + span < len(table_region.cells) and c < len(table_region.cells[r + span]) else ""
                if next_text != text_r:
                    break
                span += 1
            if span > 1:
                try:
                    # Clear ALL cells in merge range
                    for mr in range(r, r + span):
                        table.cell(mr, c).text = ""
                    top_cell = table.cell(r, c)
                    bottom_cell = table.cell(r + span - 1, c)
                    top_cell.merge(bottom_cell)
                    # Remove extra paragraphs from merge (keeps only first)
                    merged_cell = table.cell(r, c)
                    while len(merged_cell.paragraphs) > 1:
                        p_el = merged_cell.paragraphs[-1]._element
                        p_el.getparent().remove(p_el)
                    merged_cell.paragraphs[0].text = text_r
                    logger.log(f"  Vertical merge {span} cells at r{r}c{c} (Text: '{text_r[:30]}')")
                except Exception as e:
                    logger.log(f"  Vertical merge failed at r{r}c{c}: {e}")
            r += span

    logger.log(f"  Added table: {rows}x{cols}")


def refine_table_structure(tables: List[TableRegion], logger: Logger):
    """
    Refines table structure by enforcing geometric column consistency.
    Specifically targets rows where Column 0 and Column 1 are incorrectly merged
    (e.g., '2. Cong ty...' should be '2.' | 'Cong ty...').
    """
    for t_idx, table in enumerate(tables):
        # We need at least 2 columns to split Col 0 and Col 1
        if table.col_count < 2:
            continue
            
        # 1. Calculate Dominant Boundary for Column 0
        # Collect x2 of Col 0 for rows where Col 0 and Col 1 are DISTINCT
        col0_boundaries = []
        
        rows = table.row_count
        cols = table.col_count
        
        for r in range(rows):
            # Check if Col 0 and Col 1 are distinct
            # We use cell_bboxes to check.
            # cells_grid might have been populated or not.
            
            bbox0 = table.cell_bboxes[r][0]
            bbox1 = table.cell_bboxes[r][1]
            
            # If bbox0 != bbox1, they are distinct cells (visually)
            # And bbox0 must be valid
            if any(bbox0) and any(bbox1) and bbox0 != bbox1:
                # Store x2 of col 0
                col0_boundaries.append(bbox0[2])
                
        if not col0_boundaries:
            continue
            
        # Median boundary
        col0_boundaries.sort()
        dominant_x2 = col0_boundaries[len(col0_boundaries) // 2]
        
        # Tolerance ? say 5 points
        
        # 2. Iterate rows and checking for Merges crossing this boundary
        candidates = 0
        
        for r in range(rows):
            bbox0 = table.cell_bboxes[r][0]
            bbox1 = table.cell_bboxes[r][1]
            
            # Check if merged (same bbox)
            if any(bbox0) and bbox0 == bbox1:
                # This is a merge across Col 0 and 1.
                # Check if it SHOULD be split.
                
                # Condition A: Text implies a split? (e.g. "1. Text")
                # Condition B: The cell spans significantly across the dominant_x2
                # bbox0 is (x1, y1, x2, y2)
                
                cell_x1, cell_y1, cell_x2, cell_y2 = bbox0
                
                # Check coverage
                # If the cell starts before the boundary and ends well after it
                if cell_x1 < dominant_x2 - 10 and cell_x2 > dominant_x2 + 20: 
                    # Likely a missing separator
                    
                    # Split it!
                    candidates += 1
                    
                    # Update BBoxes
                    # New BBox 0: (x1, y1, dominant_x2, y2)
                    new_bbox0 = (cell_x1, cell_y1, dominant_x2, cell_y2)
                    
                    # New BBox 1: (dominant_x2, y1, x2, y2)
                    new_bbox1 = (dominant_x2, cell_y1, cell_x2, cell_y2)
                    
                    table.cell_bboxes[r][0] = new_bbox0
                    table.cell_bboxes[r][1] = new_bbox1
                    
                    # Also update subsequent columns if they were merged?
                    # If Col 2 was also merged (triple merge), we set Col 1 to be the rest?
                    # For now, just split Col 0 from the blob.
                    # If the blob was Col 0+1+2, now Col 0 is small, Col 1 is (Old - Small). Col 2 is (Old - Small) (still merged with 1)
                    
                    # Propagate changes to underlying logic (Text Extraction)
                    # We need to re-extract text for these two new cells.
                    # But we don't have the original PDF TextLines here easily accessible?
                    # Wait, we prefer to use the existing text if possible. 
                    
                    current_text = table.cells[r][0] # The merged text
                    
                    # Regex split heuristic
                    # Look for "Digits." or "Roman." at start
                    match = re.match(r'^(\d+\.|[IVX]+\.)\s+(.+)$', current_text, re.DOTALL)
                    if match:
                        txt0 = match.group(1)
                        txt1 = match.group(2)
                        table.cells[r][0] = txt0
                        table.cells[r][1] = txt1
                    else:
                        # Fallback: Just put it all in Col 1? Or keep in Col 0 (overflow)?
                        # If we split the cell, we should try to split text. 
                        # If regex fails, maybe it IS a real merge (header)?
                        # But geometry said it spans column 0.
                        # Leave text in Col 0, empty in Col 1? -> Result: Col 0 expands visuals? No, we fixed width.
                        # Let's start with Regex only.
                        pass
        
        if candidates > 0:
            logger.log(f"  Refined Table {t_idx+1}: Force-split {candidates} rows at x={dominant_x2:.1f}")


def create_docx_from_pdf(
    pdf_path: str,
    output_path: str,
    log_path: str = None,
    no_log_file: bool = False,
    metadata: dict = None
) -> Tuple[bool, str, str]:
    """
    Create a new DOCX from PDF text with positions preserved.
    Returns: (Success, Message, LogContent)
    """
    layout_profiles: Dict[str, dict] = {}
    estimated_first_line_indent_pt = 0.0

    def _default_layout_profile(orientation: str = "portrait") -> dict:
        if orientation == "landscape":
            page_w, page_h = 841.89, 595.28
        else:
            page_w, page_h = 595.28, 841.89
        return {
            "page_width_pt": page_w,
            "page_height_pt": page_h,
            "left_margin_pt": page_w * 0.12,
            "right_margin_pt": page_w * 0.08,
            "top_margin_pt": page_h * 0.07,
            "bottom_margin_pt": page_h * 0.07,
            "body_font_pt": 14.0,
            "table_font_pt": 13.0,
            "first_line_indent_pt": page_w * 0.045,
        }

    def _apply_standard_section_layout(section, orientation: str = "portrait") -> None:
        """Apply detected page size/margins; fall back to proportional A4."""
        profile = layout_profiles.get(orientation) or _default_layout_profile(orientation)
        section.page_width = Pt(profile["page_width_pt"])
        section.page_height = Pt(profile["page_height_pt"])
        section.top_margin = Pt(profile["top_margin_pt"])
        section.bottom_margin = Pt(profile["bottom_margin_pt"])
        section.left_margin = Pt(profile["left_margin_pt"])
        section.right_margin = Pt(profile["right_margin_pt"])

    def _estimate_docx_layout_profiles(
        page_info_local: dict,
        lines: List[TextLine],
        tables: List[TableRegion],
        paragraph_items: List[Tuple[str, TextLine, bool]],
    ) -> Dict[str, dict]:
        grouped: Dict[str, Dict[str, List[float]]] = {}
        table_bboxes_by_page: Dict[int, List[Tuple[float, float, float, float]]] = {}
        for table in tables or []:
            table_bboxes_by_page.setdefault(table.page, []).append(_table_bbox(table))

        for pg, info in (page_info_local or {}).items():
            page_w = float(info.get("width", 595.28) or 595.28)
            page_h = float(info.get("height", 841.89) or 841.89)
            orientation = "landscape" if page_w > page_h else "portrait"
            group = grouped.setdefault(
                orientation,
                {
                    "page_width": [],
                    "page_height": [],
                    "left": [],
                    "right_gap": [],
                    "top": [],
                    "bottom_gap": [],
                    "font": [],
                    "table_font": [],
                    "indent": [],
                },
            )
            group["page_width"].append(page_w)
            group["page_height"].append(page_h)

            page_lines = [
                line for line in lines
                if line.page == pg
                and not line.is_footnote
                and line.content_type != 4
                and not is_page_number_text(line.text)
                and line.width >= page_w * 0.08
            ]
            body_band = [
                line for line in page_lines
                if page_h * 0.04 <= line.y <= page_h * 0.96
            ]
            lefts = [line.x for line in body_band]
            rights = [line.x + line.width for line in body_band]
            tops = [line.y for line in body_band]
            bottoms = [line.y + line.height for line in body_band]
            fonts = [line.font_size for line in body_band if line.font_size > 0]
            for bbox in table_bboxes_by_page.get(pg, []):
                lefts.append(bbox[0])
                rights.append(bbox[2])
                tops.append(bbox[1])
                bottoms.append(bbox[3])

            if lefts and rights:
                left = _percentile(lefts, 0.10, page_w * 0.12)
                right = _percentile(rights, 0.90, page_w * 0.90)
                group["left"].append(_clamp(left, page_w * 0.025, page_w * 0.24))
                group["right_gap"].append(_clamp(page_w - right, page_w * 0.025, page_w * 0.24))
            if tops:
                group["top"].append(_clamp(_percentile(tops, 0.05, page_h * 0.07), page_h * 0.025, page_h * 0.18))
            if bottoms:
                bottom_gap = page_h - _percentile(bottoms, 0.95, page_h * 0.93)
                group["bottom_gap"].append(_clamp(bottom_gap, page_h * 0.025, page_h * 0.18))
            if fonts:
                group["font"].append(_median(fonts, 14.0))

        page_left_by_page: Dict[int, float] = {}
        for pg, info in (page_info_local or {}).items():
            page_w = float(info.get("width", 595.28) or 595.28)
            page_lines = [
                line for line in lines
                if line.page == pg
                and not line.is_footnote
                and line.content_type != 4
                and line.width >= page_w * 0.08
            ]
            if page_lines:
                page_left_by_page[pg] = _percentile([line.x for line in page_lines], 0.10, page_w * 0.12)

        for _, first_line, is_footnote in paragraph_items or []:
            if is_footnote:
                continue
            merged_lines = _get_merged_lines(first_line)
            continuation_lefts = [
                line.x for line in merged_lines[1:]
                if line.page == first_line.page and abs(line.x - first_line.x) > 1.0
            ]
            base_left = min(continuation_lefts) if continuation_lefts else page_left_by_page.get(first_line.page)
            if base_left is None:
                continue
            page_w = float((page_info_local or {}).get(first_line.page, {}).get("width", 595.28) or 595.28)
            indent = first_line.x - base_left
            if page_w * 0.012 <= indent <= page_w * 0.16:
                orientation = "landscape" if page_info_local.get(first_line.page, {}).get("width", 0) > page_info_local.get(first_line.page, {}).get("height", 1) else "portrait"
                if orientation not in grouped:
                    grouped[orientation] = {
                        "page_width": [],
                        "page_height": [],
                        "left": [],
                        "right_gap": [],
                        "top": [],
                        "bottom_gap": [],
                        "font": [],
                        "table_font": [],
                        "indent": [],
                    }
                grouped[orientation]["indent"].append(indent)

        profiles: Dict[str, dict] = {}
        for orientation in ("portrait", "landscape"):
            defaults = _default_layout_profile(orientation)
            group = grouped.get(orientation)
            if not group:
                profiles[orientation] = defaults
                continue
            page_w = _median(group.get("page_width", []), defaults["page_width_pt"])
            page_h = _median(group.get("page_height", []), defaults["page_height_pt"])
            body_font = _clamp(round(_median(group.get("font", []), 14.0)), 12.0, 14.0)
            table_font = _clamp(body_font - 1.0, 11.0, 13.0)
            profiles[orientation] = {
                "page_width_pt": page_w,
                "page_height_pt": page_h,
                "left_margin_pt": _median(group.get("left", []), defaults["left_margin_pt"]),
                "right_margin_pt": _median(group.get("right_gap", []), defaults["right_margin_pt"]),
                "top_margin_pt": defaults["top_margin_pt"],
                "bottom_margin_pt": defaults["bottom_margin_pt"],
                "body_font_pt": body_font,
                "table_font_pt": table_font,
                "first_line_indent_pt": _median(group.get("indent", []), defaults["first_line_indent_pt"]),
            }
        return profiles

    if not no_log_file and log_path is None:
        base = os.path.splitext(output_path)[0]
        log_path = base.replace("_final", "_merge") + ".log"
    
    if no_log_file:
        log_path = None

    logger = Logger(log_path)
    logger.log("=" * 60)
    logger.log("PDF to DOCX Position-Preserving Converter v4")
    logger.log("=" * 60)
    logger.log(f"PDF: {pdf_path}")
    logger.log(f"Output: {output_path}")
    
    try:
        # Step 1: Extract text lines with positions
        pdf_lines, page_info = extract_pdf_lines(pdf_path, logger)

        # Step 1b: Enrich lines with Screen AI metadata + layout regions from companion JSON
        layout_regions_by_page = {}  # page_num -> [regions]
        for json_candidate in [pdf_path + ".json"]:
            if os.path.exists(json_candidate):
                enrich_lines_from_json(pdf_lines, json_candidate, logger)
                # Also load layout regions if present
                try:
                    import json as _json
                    with open(json_candidate, "r", encoding="utf-8") as _f:
                        _jdata = _json.load(_f)
                    for _pi, _pg in enumerate(_jdata.get("pages", [])):
                        _lr = _pg.get("layout_regions", [])
                        if _lr:
                            layout_regions_by_page[_pi + 1] = _lr
                    if layout_regions_by_page:
                        logger.log(f"Loaded layout regions for {len(layout_regions_by_page)} pages")
                except Exception:
                    pass
                break

        # Step 1c: Match lines to layout regions (assigns semantic_type)
        if layout_regions_by_page:
            try:
                from scanindex.core.tables.layout_analyzer import match_lines_to_regions
                for pg_num, regions in layout_regions_by_page.items():
                    pg_lines = [l for l in pdf_lines if l.page == pg_num]
                    pw = page_info.get(pg_num, {}).get("width", 595)
                    ph = page_info.get(pg_num, {}).get("height", 842)
                    # Prefer bbox_pdf written by OCR JSON decoration. This
                    # keeps semantic matching independent from render DPI.
                    if any(region.get("bbox_pdf") for region in regions):
                        match_regions = []
                        for region in regions:
                            bbox_pdf = region.get("bbox_pdf")
                            if not bbox_pdf:
                                continue
                            item = dict(region)
                            item["bbox"] = bbox_pdf
                            match_regions.append(item)
                        match_lines_to_regions(pg_lines, match_regions, 1.0, 1.0)
                    else:
                        # Legacy JSON fallback: bbox is in image pixels.
                        scale_x = pw / (pw / (72.0 / 200.0))  # = 72/200
                        scale_y = ph / (ph / (72.0 / 200.0))
                        match_lines_to_regions(pg_lines, regions, scale_x, scale_y)
                tagged = sum(1 for l in pdf_lines if l.semantic_type)
                logger.log(f"Tagged {tagged}/{len(pdf_lines)} lines with semantic types")
            except ImportError:
                logger.log("layout_analyzer not available, skipping semantic tagging")
            except Exception as e:
                logger.log(f"Semantic tagging failed: {e}")

        pdf_lines = filter_figure_ocr_noise(pdf_lines, layout_regions_by_page, logger)

        # Step 2: Detect tables (using unified function with config)
        table_regions = detect_tables(pdf_path, logger, page_info, pdf_lines, layout_regions_by_page)
        table_regions = repair_continued_tables(
            table_regions, layout_regions_by_page, pdf_lines, page_info, logger
        )
        table_regions = split_stacked_tables(table_regions, logger)
        table_regions = postprocess_table_layout_grids(table_regions, layout_regions_by_page, logger)
        table_regions = filter_false_positive_tables(table_regions, layout_regions_by_page, logger)
        table_assigned_ids = assign_ocr_lines_to_table_cells_by_geometry(
            table_regions,
            pdf_lines,
            logger,
        )
        try:
            from scanindex.core.tables.postprocess_v2 import postprocess_tables_v2

            postprocess_tables_v2(table_regions, pdf_lines, logger)
        except Exception as exc:
            logger.log(f"V2 table postprocess failed, keeping V1 cells: {exc}")

        # --- NEW: Refine Structure ---
        # User requested to disable custom geometric refinement.
        # refine_table_structure(table_regions, logger)

        # Create table lookup by page and y-range
        table_map = {}  # page -> [(y_top, y_bottom, table)]
        for table in table_regions:
            if table.page not in table_map:
                table_map[table.page] = []
            table_map[table.page].append((table.y_top, table.y_bottom, table))

        # Step 3: Create new document
        doc = Document()

        # Set A4 page and administrative-document margins.
        for section in doc.sections:
            _apply_standard_section_layout(section, "portrait")

        # Set default font and paragraph spacing
        style = doc.styles['Normal']
        style.font.name = "Times New Roman"
        style.font.size = Pt(14)
        style.paragraph_format.space_after = Pt(0)
        style.paragraph_format.space_before = Pt(0)
        style.paragraph_format.line_spacing = Pt(20)

        def _apply_word_flow_paragraph_format(
            para,
            *,
            is_footnote: bool = False,
            is_centered: bool = False,
            is_caption: bool = False,
            use_first_line_indent: bool = False,
        ) -> None:
            pf = para.paragraph_format
            if is_footnote:
                pf.line_spacing = Pt(14)
                pf.space_before = Pt(0)
                pf.space_after = Pt(0)
                pf.first_line_indent = None
                return

            if is_centered or is_caption:
                pf.line_spacing = Pt(18)
                pf.space_before = Pt(3)
                pf.space_after = Pt(3)
                pf.first_line_indent = None
                return

            pf.line_spacing = Pt(20)
            pf.space_before = Pt(6)
            pf.space_after = Pt(6)
            pf.first_line_indent = (
                Pt(estimated_first_line_indent_pt)
                if use_first_line_indent and estimated_first_line_indent_pt > 0.5
                else None
            )

        # Step 4: Filter out table-assigned lines, then merge the rest into paragraphs
        filtered_lines = []
        unresolved_table_lines = []
        for line in pdf_lines:
            if id(line) in table_assigned_ids:
                continue
            if _looks_like_qr_access_artifact(line.text):
                logger.log(f"Skipped QR/link access artifact line on page {line.page}")
                continue

            inside_rendered_table = False
            inside_skipped_table = False
            line_cy = line.y + line.height / 2.0
            if line.page in table_map:
                for y_top, y_bottom, table in table_map[line.page]:
                    if y_top <= line_cy <= y_bottom:
                        if getattr(table, "skip_render", False):
                            inside_skipped_table = True
                        else:
                            inside_rendered_table = True
                        break
            if inside_skipped_table:
                continue
            if inside_rendered_table:
                unresolved_table_lines.append(line)
                continue
            filtered_lines.append(line)

        if unresolved_table_lines:
            logger.log(f"Skipped {len(unresolved_table_lines)} unresolved table-overlap lines after table mapping")

        # Detect dual-column headers before merging
        dual_headers = detect_dual_column_headers(filtered_lines, page_info, logger, layout_regions_by_page)
        dual_footers = detect_dual_column_footers(filtered_lines, page_info, logger)
        dual_header_line_ids = set()
        for pg, (left, right) in dual_headers.items():
            for l in left + right:
                dual_header_line_ids.add(id(l))
        dual_footer_line_ids = set()
        for pg, (left, right) in dual_footers.items():
            for l in left + right:
                dual_footer_line_ids.add(id(l))
        # Remove dual-column header/footer lines from paragraph merging
        dual_column_line_ids = dual_header_line_ids | dual_footer_line_ids
        if dual_column_line_ids:
            filtered_lines = [l for l in filtered_lines if id(l) not in dual_column_line_ids]

        # Merge lines into paragraphs
        paragraphs = merge_lines_to_paragraphs(filtered_lines, page_info, logger)

        # Step 4b: Compute statistics from actual data for adaptive formatting
        body_heights = [l.height for l in filtered_lines if not l.is_footnote and l.height > 0]
        median_h = sorted(body_heights)[len(body_heights) // 2] if body_heights else 20

        layout_profiles = _estimate_docx_layout_profiles(page_info, filtered_lines, table_regions, paragraphs)
        portrait_profile = layout_profiles.get("portrait") or _default_layout_profile("portrait")
        estimated_first_line_indent_pt = float(portrait_profile.get("first_line_indent_pt", 0.0))
        for section in doc.sections:
            _apply_standard_section_layout(section, "portrait")
        style.font.size = Pt(float(portrait_profile.get("body_font_pt", 14.0)))
        for pg, info in page_info.items():
            orientation = "landscape" if info.get("width", 0) > info.get("height", 1) else "portrait"
            profile = layout_profiles.get(orientation) or _default_layout_profile(orientation)
            info["docx_left_margin_pt"] = profile["left_margin_pt"]
            info["docx_right_margin_pt"] = profile["right_margin_pt"]
            info["docx_body_font_pt"] = profile["body_font_pt"]
            info["docx_table_font_pt"] = profile["table_font_pt"]
        logger.log(
            "Estimated DOCX layout: "
            + "; ".join(
                f"{name} page={profile['page_width_pt']:.1f}x{profile['page_height_pt']:.1f} "
                f"margins L/R/T/B={profile['left_margin_pt']:.1f}/"
                f"{profile['right_margin_pt']:.1f}/{profile['top_margin_pt']:.1f}/"
                f"{profile['bottom_margin_pt']:.1f}, indent={profile['first_line_indent_pt']:.1f}"
                for name, profile in sorted(layout_profiles.items())
            )
        )

        # OCR foreground gray is normalized at document level for word-level
        # emphasis. Page stats are kept as a secondary guard for uneven scans.
        gray_values_by_page: Dict[int, List[float]] = {}
        doc_gray_values: List[float] = []
        for line in pdf_lines:
            for word in _word_items_for_line(line):
                gray = word.get("fg_gray", 128)
                if _has_known_gray(gray):
                    gray_f = float(gray)
                    gray_values_by_page.setdefault(line.page, []).append(gray_f)
                    doc_gray_values.append(gray_f)
        style_stats_by_page: Dict[int, dict] = {}
        doc_dark_cutoff = _percentile(doc_gray_values, 0.12, 128.0)
        doc_q25 = _percentile(doc_gray_values, 0.25, 128.0)
        doc_q75 = _percentile(doc_gray_values, 0.75, 128.0)
        doc_median = _median(doc_gray_values, 128.0)
        doc_iqr = max(doc_q75 - doc_q25, 1.0)
        doc_style_stats = {
            "doc_known_gray_count": len(doc_gray_values),
            "doc_gray_median": doc_median,
            "doc_gray_iqr": doc_iqr,
            "doc_bold_gray_cutoff": doc_dark_cutoff if len(doc_gray_values) >= 80 else None,
            "doc_bold_z_cutoff": _gray_z(doc_dark_cutoff, doc_median, doc_iqr) if len(doc_gray_values) >= 80 else 0.0,
        }
        for pg, values in gray_values_by_page.items():
            page_dark_cutoff = _percentile(values, 0.12, 128.0)
            q25 = _percentile(values, 0.25, 128.0)
            q75 = _percentile(values, 0.75, 128.0)
            style_stats_by_page[pg] = {
                **doc_style_stats,
                "known_gray_count": len(values),
                "gray_median": _median(values, 128.0),
                "page_bold_gray_cutoff": page_dark_cutoff if len(values) >= 40 else None,
                "gray_q25": q25,
                "gray_iqr": max(q75 - q25, 1.0),
            }
        for line in pdf_lines:
            setattr(line, "_page_style_stats", style_stats_by_page.get(line.page, doc_style_stats))

        # Margins: reuse margin_map computed by merge_lines_to_paragraphs
        # (same logic already used for paragraph merging)
        computed_margins = {}  # page -> (left, right)
        page_lines_map = {}
        for line in filtered_lines:
            if line.page not in page_lines_map:
                page_lines_map[line.page] = []
            page_lines_map[line.page].append(line)
        for pg, p_lines in page_lines_map.items():
            from collections import Counter
            x_rounded = [round(l.x / 2.0) * 2.0 for l in p_lines]
            common = Counter(x_rounded).most_common(1)
            left = common[0][0] if common else 0
            all_rights = sorted([l.x + l.width for l in p_lines])
            right = all_rights[int(len(all_rights) * 0.98)] if all_rights else page_info.get(pg, {}).get("width", 595)
            computed_margins[pg] = (left, right)

        # Step 5: Add paragraphs and tables to document using a unified sorted list
        # This ensures strict order based on Y position

        doc_elements = []
        use_reading_order = any(getattr(l, "order", 0) > 0 for l in pdf_lines)

        # Add paragraphs
        for para_text, first_line, is_footnote in paragraphs:
            doc_elements.append({
                "type": "para",
                "page": first_line.page,
                "y": first_line.y,
                "order": first_line.order,
                "data": (para_text, first_line, is_footnote)
            })

        # Add tables
        for table in table_regions:
            if getattr(table, "skip_render", False):
                continue
            table_lines = [
                l for l in pdf_lines
                if l.page == table.page and table.y_top <= l.y_center <= table.y_bottom
            ]
            table_order = min((l.order for l in table_lines), default=1000000)
            doc_elements.append({
                "type": "table",
                "page": table.page,
                "y": table.y_top,
                "order": table_order,
                "data": table
            })

        # Add figure regions from layout analysis
        for pg_num, regions in layout_regions_by_page.items():
            for r_idx, region in enumerate(regions):
                if region["type"] == "figure":
                    bbox_pdf = region.get("bbox_pdf", region["bbox"])
                    fig_bbox = tuple(float(v) for v in bbox_pdf[:4])
                    nearby_qr_text = False
                    for line in pdf_lines:
                        if line.page != pg_num or not _looks_like_qr_access_artifact(line.text):
                            continue
                        lx0, ly0, lx1, ly1 = _line_bbox(line)
                        x_overlap = max(0.0, min(lx1, fig_bbox[2]) - max(lx0, fig_bbox[0]))
                        near_y = ly0 <= fig_bbox[3] + 45.0 and ly1 >= fig_bbox[1] - 20.0
                        if _bbox_overlap_ratio((lx0, ly0, lx1, ly1), fig_bbox) > 0.01 or (near_y and x_overlap > 0):
                            nearby_qr_text = True
                            break
                    if nearby_qr_text:
                        logger.log(f"Skipped QR/link access figure on page {pg_num}")
                        continue
                    pg_width = page_info.get(pg_num, {}).get("width", 595)
                    fig_center_x = (fig_bbox[0] + fig_bbox[2]) / 2
                    if pg_num in dual_footers:
                        footer_left, footer_right = dual_footers[pg_num]
                        footer_lines = footer_left + footer_right
                        footer_y0 = min((line.y for line in footer_lines), default=fig_bbox[1]) - 30
                        footer_y1 = max((line.y + line.height for line in footer_lines), default=fig_bbox[3]) + 50
                        overlaps_footer_band = fig_bbox[1] <= footer_y1 and fig_bbox[3] >= footer_y0
                        if (
                            any(_bbox_overlap_ratio(_line_bbox(line), fig_bbox) > 0.05 for line in footer_right)
                            or (fig_center_x >= pg_width * 0.45 and overlaps_footer_band)
                        ):
                            logger.log(f"Skipped signature/stamp figure on page {pg_num}")
                            continue
                    else:
                        anchors = [
                            line for line in pdf_lines
                            if line.page == pg_num and _unaccent_upper(line.text).startswith("NOI NHAN")
                        ]
                        if anchors:
                            anchor = sorted(anchors, key=lambda line: line.y)[-1]
                            if fig_center_x >= pg_width * 0.45 and fig_bbox[1] >= anchor.y - 30:
                                logger.log(f"Skipped signature/stamp figure on page {pg_num}")
                                continue
                    doc_elements.append({
                        "type": "figure",
                        "page": pg_num,
                        "y": bbox_pdf[1],
                        "order": 1000000 + int(bbox_pdf[1]),
                        "data": {"bbox": region["bbox"], "page": pg_num, "idx": r_idx}
                    })

        # Add dual-column headers
        for pg, (left, right) in dual_headers.items():
            top_y = min(l.y for l in left + right)
            doc_elements.append({
                "type": "dual_header",
                "page": pg,
                "y": top_y,
                "order": min(l.order for l in left + right),
                "data": (left, right)
            })

        # Add dual-column recipient/signature footers
        for pg, (left, right) in dual_footers.items():
            top_y = min(l.y for l in left + right)
            doc_elements.append({
                "type": "dual_footer",
                "page": pg,
                "y": top_y,
                "order": min(l.order for l in left + right),
                "data": (left, right)
            })

        # Sort by OCR reading order when canonical JSON is available. Pure Y sorting
        # interleaves two-column signature/recipient blocks and hurts editable text
        # accuracy; fallback PDFs without JSON keep the original coordinate sort.
        if use_reading_order:
            doc_elements.sort(key=lambda x: (x["page"], x.get("order", 1000000), x["y"]))
        else:
            doc_elements.sort(key=lambda x: (x["page"], x["y"]))

        # Open source PDF for figure extraction (if layout regions have figures)
        doc_pdf = None
        has_figures = any(e["type"] == "figure" for e in doc_elements)
        if has_figures:
            try:
                # Prefer original input (higher quality than OCR overlay)
                original_pdf = pdf_path.replace("_ocr.pdf", ".pdf")
                doc_pdf = fitz.open(original_pdf if os.path.exists(original_pdf) else pdf_path)
            except Exception:
                doc_pdf = None

        # Detect page orientations from PDF page dimensions
        page_orientations = {}  # page_num -> 'portrait' or 'landscape'
        for pg, info in page_info.items():
            page_orientations[pg] = 'landscape' if info["width"] > info["height"] else 'portrait'

        tables_added_count = 0
        first_elem_page = doc_elements[0]["page"] if doc_elements else 1
        current_orientation = page_orientations.get(first_elem_page, 'portrait')
        for section in doc.sections:
            _apply_standard_section_layout(section, current_orientation)
        last_rendered_page = None

        for elem_index, elem in enumerate(doc_elements):
            elem_page = elem["page"]
            needed_orient = page_orientations.get(elem_page, 'portrait')

            page_changed = last_rendered_page is not None and elem_page != last_rendered_page
            orientation_changed = needed_orient != current_orientation
            if (
                page_changed
                and not orientation_changed
                and os.environ.get("OCRTOOL_PRESERVE_SOURCE_PAGE_BREAKS") == "1"
            ):
                doc.add_page_break()
                logger.log(f"Inserted page break before source page {elem_page}")
            elif orientation_changed:
                from docx.enum.section import WD_ORIENT, WD_SECTION
                new_section = doc.add_section(WD_SECTION.NEW_PAGE)
                if needed_orient == 'landscape':
                    new_section.orientation = WD_ORIENT.LANDSCAPE
                    _apply_standard_section_layout(new_section, "landscape")
                else:
                    new_section.orientation = WD_ORIENT.PORTRAIT
                    _apply_standard_section_layout(new_section, "portrait")
                current_orientation = needed_orient
                logger.log(f"Page {elem_page}: switched to {needed_orient}")
            last_rendered_page = elem_page

            if elem["type"] == "para":
                para_text, first_line, is_footnote = elem["data"]

                # Skip separators (horizontal rules)
                if first_line.content_type == 4:
                    continue

                # Note: "abandon" from layout model is unreliable (low conf, false positives)
                # e.g. "ĐẢNG CỘNG SẢN VIỆT NAM" falsely tagged as abandon
                # → don't skip any text based on layout model alone

                # Use layout/KIE signals for emphasis, but keep alignment
                # governed by the original line geometry.
                sem = (first_line.semantic_type or "").strip().lower()
                numbered_heading = _looks_like_numbered_heading(para_text)
                is_list_item = _looks_like_list_item(para_text)
                is_doc_subject = "DOC_SUBJECT" in getattr(first_line, "kie_labels", set())

                # Detect centered from geometry only. Layout "title" is a style
                # hint; section headings can be tagged title while still being
                # visually left-aligned in the source.
                left_m, right_m = computed_margins.get(first_line.page, (0, page_info.get(first_line.page, {}).get("width", 595)))
                content_width = right_m - left_m
                if content_width > 0:
                    line_center = first_line.x + first_line.width / 2
                    page_center = left_m + content_width / 2
                    is_short = first_line.width < content_width * 0.75
                    left_gap = max(0.0, first_line.x - left_m)
                    right_gap = max(0.0, right_m - (first_line.x + first_line.width))
                    is_centered = (
                        is_short
                        and abs(line_center - page_center) < content_width * 0.08
                        and abs(left_gap - right_gap) < content_width * 0.14
                    )
                else:
                    is_short = False
                    is_centered = False

                # Relative font size from line height
                # Only heading-like lines get larger font
                is_bold = _looks_like_visual_bold_heading(
                    para_text,
                    sem,
                    numbered_heading,
                    is_doc_subject,
                    is_centered,
                )
                if (
                    not is_bold
                    and is_centered
                    and not is_list_item
                    and len(_clean_extracted_text(para_text)) <= 180
                    and _line_visual_bold_ratio(first_line) >= 0.65
                ):
                    is_bold = True
                ratio = first_line.height / median_h if median_h > 0 else 1.0
                base_font_size = float(page_info.get(first_line.page, {}).get("docx_body_font_pt", 14.0))
                is_heading = (
                    numbered_heading
                    or is_doc_subject
                    or (is_bold and (sem == "title" or _mostly_uppercase_text(para_text)))
                )
                if is_heading and ratio > 1.4:
                    para_font_size = base_font_size + 4
                elif is_heading and ratio > 1.15:
                    para_font_size = base_font_size + 2
                else:
                    para_font_size = base_font_size

                # Determine alignment
                para = doc.add_paragraph()
                if (sem == "figure_caption" or sem == "table_caption"):
                    para.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
                    _apply_word_flow_paragraph_format(
                        para,
                        is_footnote=is_footnote,
                        is_centered=True,
                        is_caption=True,
                    )
                elif is_centered and not numbered_heading:
                    para.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
                    _apply_word_flow_paragraph_format(
                        para,
                        is_footnote=is_footnote,
                        is_centered=True,
                    )
                elif is_heading:
                    para.alignment = WD_PARAGRAPH_ALIGNMENT.JUSTIFY
                    _apply_word_flow_paragraph_format(
                        para,
                        is_footnote=is_footnote,
                        use_first_line_indent=True,
                    )
                elif is_list_item:
                    para.alignment = WD_PARAGRAPH_ALIGNMENT.JUSTIFY
                    _apply_word_flow_paragraph_format(
                        para,
                        is_footnote=is_footnote,
                        use_first_line_indent=True,
                    )
                else:
                    para.alignment = WD_PARAGRAPH_ALIGNMENT.JUSTIFY
                    _apply_word_flow_paragraph_format(
                        para,
                        is_footnote=is_footnote,
                        use_first_line_indent=True,
                    )

                # Italic detection:
                # - Handwritten/signature content type
                # - Parenthesized text like "(Kèm theo Công văn...)" = subtitle/caption
                is_handwritten = first_line.content_type in (1, 8)
                # Italic: only from content_type (handwritten/signature)
                is_italic = is_handwritten

                # Format text with superscripts
                add_text_with_superscripts(para, para_text, first_line, is_footnote,
                                          bold=is_bold, font_size_pt=para_font_size,
                                          italic=is_italic)

            elif elem["type"] in ("dual_header", "dual_footer"):
                left_lines, right_lines = elem["data"]
                pw = page_info.get(elem["page"], {}).get("width", 595)
                add_dual_header_table(doc, left_lines, right_lines, pw, logger)

            elif elem["type"] == "figure":
                # Extract figure image from PDF page
                try:
                    fig_data = elem["data"]
                    fig_page = doc_pdf[fig_data["page"] - 1]  # 0-based
                    bx = fig_data["bbox"]  # image pixel coords
                    # Render page at 200 DPI and crop
                    fig_pix = fig_page.get_pixmap(dpi=200)
                    from PIL import Image as PILImage
                    fig_img = PILImage.frombytes("RGB", [fig_pix.width, fig_pix.height], fig_pix.samples)
                    # Crop with padding
                    pad = 5
                    crop_box = (
                        max(0, int(bx[0]) - pad), max(0, int(bx[1]) - pad),
                        min(fig_img.width, int(bx[2]) + pad), min(fig_img.height, int(bx[3]) + pad)
                    )
                    cropped = fig_img.crop(crop_box)
                    # Save temp and insert
                    import tempfile
                    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
                    cropped.save(tmp.name)
                    tmp.close()
                    # Size: fit within page width
                    fig_w_inches = cropped.width / 200  # 200 DPI
                    max_w = 5.5  # inches, safe for portrait
                    if fig_w_inches > max_w:
                        fig_w_inches = max_w
                    doc.add_picture(tmp.name, width=Inches(fig_w_inches))
                    os.unlink(tmp.name)
                    logger.log(f"  Added figure from page {fig_data['page']}")
                except Exception as e:
                    logger.log(f"  Figure extraction failed: {e}")

            elif elem["type"] == "table":
                table = elem["data"]
                page_lines = [l for l in pdf_lines if l.page == table.page]
                add_table_to_doc(doc, table, page_lines, logger, page_info)
                tables_added_count += 1
                if elem_index < len(doc_elements) - 1:
                    doc.add_paragraph()  # Space after table when more content follows
        
        # Cleanup
        if doc_pdf:
            doc_pdf.close()

        # Step 5: Embed metadata into DOCX properties
        if metadata:
            try:
                props = doc.core_properties
                if metadata.get("co_quan_ban_hanh"):
                    props.author = metadata["co_quan_ban_hanh"]
                if metadata.get("trich_yeu"):
                    props.title = metadata["trich_yeu"]
                if metadata.get("loai_van_ban"):
                    props.subject = metadata["loai_van_ban"]
                if metadata.get("so_ky_hieu"):
                    props.keywords = metadata["so_ky_hieu"]
            except Exception as e:
                logger.log(f"Warning: Failed to set DOCX properties: {e}")

        # Step 6: Save
        doc.save(output_path)
        
        # Summary
        logger.log("\n" + "=" * 60)
        logger.log("SUMMARY")
        logger.log("=" * 60)
        logger.log(f"Text lines: {len(pdf_lines)}")
        logger.log(f"Tables: {tables_added_count}")
        logger.log(f"Output: {output_path}")
        logger.save()
        
        return True, f"Success: {output_path}", logger.get_log_text()
        
    except Exception as e:
        import traceback
        logger.log(f"ERROR: {traceback.format_exc()}")
        logger.save()
        return False, str(e), logger.get_log_text()


def create_final_docx_v2(base_path: str) -> Tuple[bool, str, str]:
    """Convenience function."""
    for suffix in ["_ocr.pdf", "_final.docx"]:
        if base_path.endswith(suffix):
            base_path = base_path[:-len(suffix)]
            break
    
    return create_docx_from_pdf(
        base_path + "_ocr.pdf",
        base_path + "_final.docx"
    )


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 2:
        success, msg, logs = create_final_docx_v2(sys.argv[1])
        print(f"\n{'SUCCESS' if success else 'FAILED'}: {msg}")
    else:
        print("Usage: python table_anchored_merger.py <base_path>")
