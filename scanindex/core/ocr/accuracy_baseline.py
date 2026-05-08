"""Baseline ground truth + cached self-OCR for the accuracy benchmark.

Files in `<base_dir>/ocr_groundtruth/`:
  - groundtruth.pdf   PDF mẫu để người dùng đem cho OCR khác xử lý
  - groundtruth.docx  Text gốc (nguyên bản, không qua OCR)
  - groundtruth.ours.txt  Cache: text mà phần mềm này OCR ra trên groundtruth.pdf
"""
from __future__ import annotations

import os
import threading
from typing import Callable

from scanindex.infra.paths import get_base_dir


GT_DIR_NAME = "ocr_groundtruth"
GT_PDF_NAME = "groundtruth.pdf"
GT_TXT_NAME = "groundtruth.txt"      # ưu tiên: text thuần do người dùng soạn tay
GT_DOCX_NAME = "groundtruth.docx"    # fallback: extract từ docx (paragraphs + tables)
OURS_CACHE_NAME = "groundtruth.ours.txt"


def get_gt_dir() -> str:
    return os.path.join(get_base_dir(), GT_DIR_NAME)


def get_gt_pdf_path() -> str:
    return os.path.join(get_gt_dir(), GT_PDF_NAME)


def get_gt_docx_path() -> str:
    return os.path.join(get_gt_dir(), GT_DOCX_NAME)


def get_gt_txt_path() -> str:
    return os.path.join(get_gt_dir(), GT_TXT_NAME)


def get_ours_cache_path() -> str:
    return os.path.join(get_gt_dir(), OURS_CACHE_NAME)


def gt_pdf_exists() -> bool:
    return os.path.exists(get_gt_pdf_path())


def load_ground_truth_text() -> str:
    """Đọc text gốc. Ưu tiên groundtruth.txt (do người dùng soạn tay khớp với
    PDF mẫu), fallback sang groundtruth.docx nếu chỉ có docx."""
    txt = get_gt_txt_path()
    if os.path.exists(txt):
        with open(txt, "r", encoding="utf-8") as f:
            return f.read()

    docx = get_gt_docx_path()
    if not os.path.exists(docx):
        raise FileNotFoundError(
            f"Không tìm thấy {txt} hoặc {docx}"
        )

    from docx import Document
    from docx.oxml.ns import qn

    doc = Document(docx)
    parts: list[str] = []
    for child in doc.element.body.iterchildren():
        tag = child.tag
        if tag == qn("w:p"):
            text = "".join((t.text or "") for t in child.iter(qn("w:t")))
            if text:
                parts.append(text)
        elif tag == qn("w:tbl"):
            for row in child.iter(qn("w:tr")):
                for cell in row.iter(qn("w:tc")):
                    text = "".join((t.text or "") for t in cell.iter(qn("w:t")))
                    if text:
                        parts.append(text)
    return "\n".join(parts)


def _ours_cache_is_fresh() -> bool:
    """Cache hợp lệ khi tồn tại và mtime >= mtime của groundtruth.pdf."""
    cache = get_ours_cache_path()
    pdf = get_gt_pdf_path()
    if not (os.path.exists(cache) and os.path.exists(pdf)):
        return False
    try:
        return os.path.getmtime(cache) >= os.path.getmtime(pdf)
    except OSError:
        return False


def get_or_compute_our_ocr_text(
    log_cb: Callable[[str], None] | None = None,
    cancel_event: threading.Event | None = None,
    force: bool = False,
) -> str:
    """Trả về text mà phần mềm này OCR trên groundtruth.pdf. Cache lần đầu."""
    log_cb = log_cb or (lambda m: None)

    if not force and _ours_cache_is_fresh():
        with open(get_ours_cache_path(), "r", encoding="utf-8") as f:
            return f.read()

    pdf = get_gt_pdf_path()
    if not os.path.exists(pdf):
        raise FileNotFoundError(f"Không tìm thấy {pdf}")

    log_cb("Lần đầu chạy: đang OCR file mẫu để làm cơ sở so sánh...")
    text = _ocr_groundtruth_pdf(pdf, log_cb, cancel_event)

    cache = get_ours_cache_path()
    os.makedirs(os.path.dirname(cache), exist_ok=True)
    with open(cache, "w", encoding="utf-8") as f:
        f.write(text)
    log_cb(f"Đã lưu cache: {cache}")
    return text


def _ocr_groundtruth_pdf(
    pdf_path: str,
    log_cb: Callable[[str], None],
    cancel_event: threading.Event | None,
) -> str:
    import json
    import tempfile

    from scanindex.core.ocr import direct_engine as direct_ocr_engine

    out_dir = tempfile.mkdtemp(prefix="ocr_baseline_")
    out_pdf = os.path.join(out_dir, "baseline_ocr.pdf")

    res, msg = direct_ocr_engine.process_pdf(
        pdf_path, out_pdf, num_pages=0,
        update_callback=lambda m, lvl="info": log_cb(m),
        wait_per_page=1.0, comparison_interval=1.0,
        source_document_path=pdf_path,
    )
    if not res:
        raise RuntimeError(f"OCR thất bại trên file mẫu: {msg}")

    json_path = out_pdf + ".json"
    if not os.path.exists(json_path):
        raise RuntimeError("OCR không sinh ra JSON")
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    parts: list[str] = []
    for page in data.get("pages", []):
        for line in page.get("lines", []):
            t = line.get("text") or ""
            if t:
                parts.append(t)
    return "\n".join(parts)


def clear_ours_cache() -> bool:
    """Xóa cache để buộc tính lại lần kế tiếp. Trả về True nếu có file để xóa."""
    cache = get_ours_cache_path()
    if os.path.exists(cache):
        os.remove(cache)
        return True
    return False
