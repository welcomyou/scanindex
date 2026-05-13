from __future__ import annotations

import hashlib
from typing import List, Optional

import fitz


_FULL_PAGE_IMAGE_THRESHOLD = 0.80
_MIXED_IMAGE_THRESHOLD = 0.18
_MEANINGFUL_TEXT_WORDS = 12
_OCR_FONT_MARKERS = ("ocr", "hidden", "invisible", "tesseract")


def _page_text_metrics(doc, page) -> dict:
    text = (page.get_text() or "").strip()
    word_count = len(text.split()) if text else 0
    fonts = page.get_fonts() or []
    has_ocr_font = False
    has_real_font = False
    for font in fonts:
        basefont = (font[3] or "").lower()
        if any(marker in basefont for marker in _OCR_FONT_MARKERS):
            has_ocr_font = True
        elif basefont:
            has_real_font = True

    has_text_ops = False
    try:
        contents = page.get_contents()
        if contents:
            raw = doc.xref_stream(contents[0])
            if raw:
                stream = raw.decode("latin-1", errors="replace")
                has_text_ops = stream.count("BT") > 0 and (stream.count("Tj") + stream.count("TJ")) > 0
    except Exception:
        pass

    return {
        "word_count": word_count,
        "has_meaningful_text": word_count > _MEANINGFUL_TEXT_WORDS,
        "has_ocr_font": has_ocr_font,
        "has_real_font": has_real_font,
        "has_text_ops": has_text_ops,
    }


def _page_image_metrics(page) -> dict:
    page_area = max(float(page.rect.width * page.rect.height), 1.0)
    max_coverage = 0.0
    total_coverage = 0.0
    image_count = 0
    try:
        infos = page.get_image_info()
    except Exception:
        infos = []
    for info in infos or []:
        try:
            bbox = fitz.Rect(info["bbox"])
        except Exception:
            continue
        coverage = max(0.0, float(bbox.width * bbox.height) / page_area)
        max_coverage = max(max_coverage, coverage)
        total_coverage += coverage
        image_count += 1
    return {
        "image_count": image_count,
        "max_image_coverage": round(max_coverage, 6),
        "total_image_coverage": round(min(total_coverage, 1.0), 6),
        "has_fullpage_image": max_coverage >= _FULL_PAGE_IMAGE_THRESHOLD,
    }


def classify_pdf_page(doc, page_index: int) -> dict:
    page = doc[page_index]
    text = _page_text_metrics(doc, page)
    images = _page_image_metrics(page)
    has_native_text = bool(text["has_meaningful_text"] and (text["has_text_ops"] or text["has_real_font"]))

    if images["has_fullpage_image"]:
        source_mode = "scan"
        text_source = "ocr"
    elif has_native_text and images["total_image_coverage"] >= _MIXED_IMAGE_THRESHOLD:
        source_mode = "mixed"
        text_source = "native+ocr"
    elif has_native_text and not (text["has_ocr_font"] and not text["has_real_font"]):
        source_mode = "digital"
        text_source = "native"
    else:
        source_mode = "scan"
        text_source = "ocr"

    return {
        "source_mode": source_mode,
        "text_source": text_source,
        "text_metrics": text,
        "image_metrics": images,
    }


def page_visual_sha256(page, *, dpi: int = 96) -> str:
    mat = fitz.Matrix(float(dpi) / 72.0, float(dpi) / 72.0)
    pix = page.get_pixmap(matrix=mat, annots=True)
    h = hashlib.sha256()
    h.update(str((pix.width, pix.height, pix.n, round(float(page.rect.width), 3), round(float(page.rect.height), 3), int(page.rotation))).encode("ascii"))
    h.update(pix.samples)
    return h.hexdigest()


def _normalise_preprocess_record(page_index: int, raw: Optional[dict]) -> dict:
    raw = dict(raw or {})
    branch = raw.get("branch") or "unknown"
    rotation = int(raw.get("rotation", raw.get("rotate_angle", 0)) or 0) % 360
    skew_angle = float(raw.get("skew_angle", 0.0) or 0.0)
    rasterized = bool(raw.get("rasterized", branch in {"C", "D", "raster_deskew"}))
    if branch in {"A", "B", "C", "D"}:
        branch = {
            "A": "insert_pdf",
            "B": "set_rotation",
            "C": "raster_deskew",
            "D": "raster_deskew",
        }[branch]
    return {
        "page_index": int(page_index),
        "branch": branch,
        "rotation": rotation,
        "skew_angle": round(skew_angle, 4),
        "rasterized": rasterized,
        "coord_transform": raw.get("coord_transform") or [1, 0, 0, 1, 0, 0],
    }


def build_page_source_manifest(
    *,
    original_pdf_path: str,
    visual_pdf_path: str,
    preprocess_metadata: Optional[dict] = None,
) -> List[dict]:
    """Build the page-wise source manifest for DOCX export.

    The manifest deliberately classifies pages from the original PDF, while
    visual fingerprints come from the page actually used by OCR/layout/table.
    """
    original_doc = fitz.open(original_pdf_path)
    visual_doc = fitz.open(visual_pdf_path)
    try:
        total_pages = min(len(original_doc), len(visual_doc))
        pre_records = list((preprocess_metadata or {}).get("page_preprocess") or [])
        if not pre_records:
            rotations = list((preprocess_metadata or {}).get("page_rotations") or [])
            pre_records = [
                {"branch": "set_rotation" if (idx < len(rotations) and rotations[idx]) else "insert_pdf", "rotation": rotations[idx] if idx < len(rotations) else 0}
                for idx in range(total_pages)
            ]
        manifest = []
        for page_index in range(total_pages):
            classification = classify_pdf_page(original_doc, page_index)
            pre = _normalise_preprocess_record(
                page_index,
                pre_records[page_index] if page_index < len(pre_records) else None,
            )
            if classification["source_mode"] == "digital" and pre["branch"] in {"insert_pdf", "unknown"}:
                pre["branch"] = "digital_passthrough"
            visual_page = visual_doc[page_index]
            visual_kind = (
                "rasterized_page"
                if pre["rasterized"]
                else ("original_pdf_page" if classification["source_mode"] == "digital" else "preprocessed_pdf_page")
            )
            manifest.append({
                "page_index": page_index,
                "source_mode": classification["source_mode"],
                "text_source": classification["text_source"],
                "visual_source": {
                    "kind": visual_kind,
                    "pdf_path_role": "original" if visual_kind == "original_pdf_page" else "preprocessed",
                    "page_index": page_index,
                    "sha256": page_visual_sha256(visual_page),
                    "width": round(float(visual_page.rect.width), 2),
                    "height": round(float(visual_page.rect.height), 2),
                },
                "preprocess": pre,
                "classification": {
                    "text_metrics": classification["text_metrics"],
                    "image_metrics": classification["image_metrics"],
                },
            })
        return manifest
    finally:
        original_doc.close()
        visual_doc.close()


def manifest_source_mode(manifest: List[dict]) -> str:
    modes = {str(item.get("source_mode") or "scan") for item in manifest or []}
    if not modes:
        return "scan"
    if len(modes) == 1:
        return next(iter(modes))
    return "mixed"
