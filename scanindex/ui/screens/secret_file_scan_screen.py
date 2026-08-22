"""Tool screen for scanning folders for classified document stamps."""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import textwrap
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from scanindex.infra.paths import get_base_dir
from scanindex.ui.screens.screen_base import ScreenContent
from scanindex.ui.theme import (
    COLOR_ACCENT,
    COLOR_ACCENT_HOVER,
    COLOR_BG,
    COLOR_BORDER,
    COLOR_INPUT,
    COLOR_PANEL,
    COLOR_RED,
    COLOR_SURFACE,
    COLOR_TEXT,
    COLOR_TEXT_SECONDARY,
    FONT_UI,
    RADIUS_MD,
    SP,
)
from scanindex.infra import translations


SUPPORTED_EXTS = {".pdf", ".png", ".jpg", ".jpeg", ".doc", ".docx"}
_MIN_NATIVE_TEXT_CHARS = 40
_DOC_NATIVE_FIRST_PAGE_LINE_LIMIT = 70
# DPI cho riêng chức năng tìm văn bản mật. Dấu MẬT/TỐI MẬT/TUYỆT MẬT là stamp
# lớn, font to → 200 DPI là đủ để nhận diện, nhanh hơn 240 DPI của engine chung.
_SECRET_SCAN_DPI = 200


class _ScanCancelled(Exception):
    pass


@dataclass
class SecretScanMatch:
    source_path: str
    relative_path: str
    keyword: str
    page_number: int
    mode: str
    ocr_pdf_path: str
    note: str = ""


@dataclass
class SecretScanArtifact:
    matches: list[SecretScanMatch]
    source_pdf: str
    canonical: dict
    rotations: list[int] | None = None


def _iter_supported_files(folder: str) -> Iterable[str]:
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in {".git", "__pycache__"}]
        for name in sorted(files):
            ext = os.path.splitext(name)[1].lower()
            if ext in SUPPORTED_EXTS:
                yield os.path.join(root, name)


def _safe_name(text: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", text).strip(" .")
    return value or "file"


def _windows_font_path() -> str | None:
    windir = os.environ.get("WINDIR") or r"C:\Windows"
    for name in ("arial.ttf", "calibri.ttf", "times.ttf"):
        path = os.path.join(windir, "Fonts", name)
        if os.path.exists(path):
            return path
    bundled = os.path.join(get_base_dir(), "models", "fonts", "NotoSans-Regular.ttf")
    return bundled if os.path.exists(bundled) else None


def _extract_pdf_pages(src_pdf: str, dst_pdf: str, page_indices: list[int]) -> str:
    import fitz

    with fitz.open(src_pdf) as src:
        if len(src) == 0:
            raise RuntimeError("PDF không có trang")
        doc = fitz.open()
        try:
            for idx in page_indices:
                if 0 <= idx < len(src):
                    doc.insert_pdf(src, from_page=idx, to_page=idx)
            if len(doc) == 0:
                raise RuntimeError("Không trích xuất được trang PDF")
            doc.save(dst_pdf, deflate=True, garbage=4)
        finally:
            doc.close()
    return dst_pdf


def _image_to_pdf(image_path: str, out_pdf: str) -> str:
    from PIL import Image

    with Image.open(image_path) as image:
        if image.mode in {"RGBA", "LA", "P"}:
            image = image.convert("RGB")
        elif image.mode != "RGB":
            image = image.convert("RGB")
        image.save(out_pdf, "PDF", resolution=300.0)
    return out_pdf


def _convert_docx_text_fallback(
    source_path: str,
    out_pdf: str,
    *,
    first_page_only: bool,
) -> str:
    """Last-resort DOCX-to-PDF text rendering when Word/LibreOffice is absent."""
    import fitz
    from docx import Document

    document = Document(source_path)
    parts = list(_iter_docx_body_texts(document))

    wrapped: list[str] = []
    for part in parts or [""]:
        lines = textwrap.wrap(part, width=88) or [""]
        wrapped.extend(lines)
        wrapped.append("")

    font_path = _windows_font_path()
    doc = fitz.open()
    try:
        page_lines: list[str] = []
        for line in wrapped:
            page_lines.append(line)
            if len(page_lines) >= 48:
                _append_text_page(doc, page_lines, font_path)
                if first_page_only:
                    break
                page_lines = []
        if (page_lines and not first_page_only) or len(doc) == 0:
            _append_text_page(doc, page_lines, font_path)
        doc.save(out_pdf, deflate=True, garbage=4)
    finally:
        doc.close()
    return out_pdf


def _iter_docx_body_texts(document) -> Iterable[str]:
    from docx.oxml.table import CT_Tbl
    from docx.oxml.text.paragraph import CT_P
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    body = document.element.body
    for child in body.iterchildren():
        if isinstance(child, CT_P):
            text = Paragraph(child, document).text.strip()
            if text:
                yield text
        elif isinstance(child, CT_Tbl):
            table = Table(child, document)
            for row in table.rows:
                cells = [" ".join(cell.text.split()) for cell in row.cells]
                text = " | ".join(cell for cell in cells if cell)
                if text:
                    yield text


def _append_text_page(doc, lines: list[str], font_path: str | None) -> None:
    import fitz

    page = doc.new_page(width=595, height=842)
    text = "\n".join(lines)
    rect = fitz.Rect(50, 50, 545, 792)
    if font_path:
        try:
            page.insert_textbox(
                rect,
                text,
                fontsize=11,
                fontname="SecretScanFont",
                fontfile=font_path,
                color=(0, 0, 0),
            )
            return
        except Exception:
            pass
    page.insert_textbox(rect, text, fontsize=11, fontname="helv", color=(0, 0, 0))


def _export_word_pdf(doc, out_pdf: str, *, first_page_only: bool) -> None:
    if first_page_only:
        # Word constants: wdExportFormatPDF=17, wdExportFromTo=3.
        doc.ExportAsFixedFormat(os.path.abspath(out_pdf), 17, False, 0, 3, 1, 1)
    else:
        doc.ExportAsFixedFormat(os.path.abspath(out_pdf), 17)


def _convert_doc_with_word(
    source_path: str,
    out_pdf: str,
    *,
    first_page_only: bool,
) -> bool:
    try:
        import win32com.client  # type: ignore
    except Exception:
        win32com = None

    if win32com is not None:
        word = None
        doc = None
        try:
            word = win32com.client.DispatchEx("Word.Application")
            word.Visible = False
            word.DisplayAlerts = 0
            doc = word.Documents.Open(os.path.abspath(source_path), ReadOnly=True)
            _export_word_pdf(doc, out_pdf, first_page_only=first_page_only)
            return os.path.exists(out_pdf)
        except Exception:
            pass
        finally:
            try:
                if doc is not None:
                    doc.Close(False)
            except Exception:
                pass
            try:
                if word is not None:
                    word.Quit()
            except Exception:
                pass

    try:
        import comtypes.client  # type: ignore
    except Exception:
        return False

    word = None
    doc = None
    try:
        word = comtypes.client.CreateObject("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0
        doc = word.Documents.Open(os.path.abspath(source_path), ReadOnly=True)
        _export_word_pdf(doc, out_pdf, first_page_only=first_page_only)
        return os.path.exists(out_pdf)
    except Exception:
        return False
    finally:
        try:
            if doc is not None:
                doc.Close(False)
        except Exception:
            pass
        try:
            if word is not None:
                word.Quit()
        except Exception:
            pass


def _find_soffice() -> str | None:
    candidate = shutil.which("soffice") or shutil.which("libreoffice")
    if candidate:
        return candidate
    for path in (
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    ):
        if os.path.exists(path):
            return path
    return None


def _convert_doc_with_soffice(source_path: str, out_pdf: str) -> bool:
    soffice = _find_soffice()
    if not soffice:
        return False
    out_dir = os.path.dirname(out_pdf)
    before = set(os.listdir(out_dir)) if os.path.isdir(out_dir) else set()
    try:
        proc = subprocess.run(
            [
                soffice,
                "--headless",
                "--convert-to",
                "pdf",
                "--outdir",
                out_dir,
                source_path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=180,
            check=False,
        )
    except Exception:
        return False
    if proc.returncode != 0:
        return False
    expected = os.path.join(out_dir, f"{Path(source_path).stem}.pdf")
    if os.path.exists(expected):
        os.replace(expected, out_pdf)
        return os.path.exists(out_pdf)
    after = set(os.listdir(out_dir)) if os.path.isdir(out_dir) else set()
    for name in after - before:
        if name.lower().endswith(".pdf"):
            os.replace(os.path.join(out_dir, name), out_pdf)
            return os.path.exists(out_pdf)
    return False


def _convert_document_to_pdf(
    source_path: str,
    out_pdf: str,
    *,
    first_page_only: bool,
) -> str:
    if _convert_doc_with_word(source_path, out_pdf, first_page_only=first_page_only):
        return out_pdf
    if not first_page_only and _convert_doc_with_soffice(source_path, out_pdf):
        return out_pdf
    if source_path.lower().endswith(".docx"):
        return _convert_docx_text_fallback(
            source_path,
            out_pdf,
            first_page_only=first_page_only,
        )
    raise RuntimeError(
        "Không chuyển được DOC/DOCX sang PDF. Cần Microsoft Word hoặc LibreOffice."
    )


def _source_to_pdf(
    source_path: str,
    file_work_dir: str,
    *,
    first_page_only: bool,
) -> tuple[str, str]:
    ext = os.path.splitext(source_path)[1].lower()
    if ext == ".pdf":
        return source_path, "PDF gốc"
    out_pdf = os.path.join(file_work_dir, "converted.pdf")
    if ext in {".png", ".jpg", ".jpeg"}:
        return _image_to_pdf(source_path, out_pdf), "Ảnh chuyển sang PDF"
    if ext in {".doc", ".docx"}:
        return (
            _convert_document_to_pdf(
                source_path,
                out_pdf,
                first_page_only=first_page_only,
            ),
            "DOC/DOCX chuyển sang PDF" + (" trang 1" if first_page_only else ""),
        )
    raise RuntimeError(f"Không hỗ trợ định dạng: {ext}")


def _finalize_canonical(canonical: dict, json_path: str | None = None) -> dict:
    """Run upgrade + slim + atomic write via the shared canonical_io helper.

    Always emits the LayoutLMv3 runtime slim variant — secret-file-scan output
    is consumed by the same downstream tools as Số hóa lưu trữ Step 2.
    """
    from scanindex.core.canonical_io import (
        LAYOUTLMV3_RUNTIME_PROFILE,
        save_canonical,
    )

    if json_path:
        save_canonical(json_path, canonical, profile=LAYOUTLMV3_RUNTIME_PROFILE)
    return canonical


def _native_text_stats(canonical: dict) -> tuple[int, int]:
    chars = 0
    lines = 0
    for page in canonical.get("pages") or []:
        for line in page.get("lines") or []:
            text = (line.get("text") or "").strip()
            if text:
                lines += 1
                chars += len(text)
    return chars, lines


def _native_text_is_usable(canonical: dict) -> bool:
    chars, lines = _native_text_stats(canonical)
    return chars >= _MIN_NATIVE_TEXT_CHARS or lines >= 3


def _canonical_from_pdf_text(
    pdf_path: str,
    source_path: str,
    page_indices: list[int],
    *,
    json_path: str | None = None,
) -> dict:
    import fitz
    from scanindex.core.kie.json_utils import (
        make_document_stub,
        make_line_record,
        make_page_record,
    )

    canonical = make_document_stub(
        input_path=pdf_path,
        engine="native_pdf_text",
        ocr_dpi=None,
        source_path=source_path,
        text_normalization="native",
        raw_text_preserved=True,
    )
    with fitz.open(pdf_path) as doc:
        for page_idx in page_indices:
            if page_idx < 0 or page_idx >= len(doc):
                continue
            page = doc[page_idx]
            page_record = make_page_record(
                page_index=page_idx,
                width=float(page.rect.width),
                height=float(page.rect.height),
                render_width=int(page.rect.width),
                render_height=int(page.rect.height),
            )
            page_record["coord_origin"] = "top-left"
            line_index = 0
            data = page.get_text("dict") or {}
            for block_index, block in enumerate(data.get("blocks") or []):
                if block.get("type", 0) != 0:
                    continue
                for raw_line in block.get("lines") or []:
                    spans = raw_line.get("spans") or []
                    text = "".join(span.get("text") or "" for span in spans).strip()
                    if not text:
                        continue
                    bbox = raw_line.get("bbox") or (0, 0, 0, 0)
                    x0, y0, x1, y1 = [float(v or 0) for v in bbox[:4]]
                    font_size = 11.0
                    for span in spans:
                        try:
                            font_size = max(font_size, float(span.get("size") or 0))
                        except Exception:
                            pass
                    page_record["lines"].append(
                        make_line_record(
                            page_idx,
                            line_index,
                            text,
                            x0,
                            y0,
                            max(0.0, x1 - x0),
                            max(0.0, y1 - y0),
                            font_size,
                            f"b{block_index}",
                            f"p{block_index}",
                            1.0,
                            "native_text",
                            0,
                            [],
                            ocr_text=text,
                        )
                    )
                    line_index += 1
            canonical["pages"].append(page_record)
    return _finalize_canonical(canonical, json_path)


def _plain_text_lines(text: str) -> list[str]:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\v", "\n").replace("\x07", " ")
    return [line.strip() for line in text.split("\n") if line.strip()]


def _docx_text_lines(source_path: str, *, first_page_only: bool) -> list[str]:
    from docx import Document

    document = Document(source_path)
    lines: list[str] = []

    def add_text(text: str) -> None:
        for line in _plain_text_lines(text):
            lines.append(line)

    def add_container(container) -> None:
        for paragraph in getattr(container, "paragraphs", []) or []:
            add_text(paragraph.text)
        for table in getattr(container, "tables", []) or []:
            for row in table.rows:
                cells = [" ".join(cell.text.split()) for cell in row.cells]
                add_text(" | ".join(cell for cell in cells if cell))

    for section in document.sections:
        for header_name in ("first_page_header", "header", "even_page_header"):
            header = getattr(section, header_name, None)
            if header is not None:
                add_container(header)
    for text in _iter_docx_body_texts(document):
        add_text(text)

    if first_page_only:
        return lines[:_DOC_NATIVE_FIRST_PAGE_LINE_LIMIT]
    return lines


def _doc_text_lines_with_word(source_path: str, *, first_page_only: bool) -> list[str]:
    try:
        import win32com.client  # type: ignore
    except Exception:
        return []

    word = None
    doc = None
    try:
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0
        doc = word.Documents.Open(
            os.path.abspath(source_path),
            ReadOnly=True,
            AddToRecentFiles=False,
            ConfirmConversions=False,
        )
        if first_page_only:
            try:
                # Word constants: wdGoToPage=1, wdGoToAbsolute=1.
                start = doc.GoTo(What=1, Which=1, Count=1).Start
                try:
                    end = doc.GoTo(What=1, Which=1, Count=2).Start
                except Exception:
                    end = doc.Content.End
                text = doc.Range(Start=start, End=end).Text
            except Exception:
                text = doc.Range(0, min(int(doc.Content.End), 5000)).Text
        else:
            text = doc.Content.Text
        return _plain_text_lines(text)
    except Exception:
        return []
    finally:
        try:
            if doc is not None:
                doc.Close(False)
        except Exception:
            pass
        try:
            if word is not None:
                word.Quit()
        except Exception:
            pass


def _canonical_from_text_lines(
    source_path: str,
    lines: list[str],
    *,
    json_path: str | None = None,
) -> dict:
    from scanindex.core.kie.json_utils import (
        make_document_stub,
        make_line_record,
        make_page_record,
    )

    width = 595.28
    height = 841.89
    left = 50.0
    top = 48.0
    line_h = 16.0
    lines_per_page = 48
    canonical = make_document_stub(
        input_path=source_path,
        engine="native_word_text",
        ocr_dpi=None,
        source_path=source_path,
        text_normalization="native",
        raw_text_preserved=True,
    )
    for page_idx, start in enumerate(range(0, len(lines), lines_per_page)):
        page_lines = lines[start:start + lines_per_page]
        page_record = make_page_record(
            page_index=page_idx,
            width=width,
            height=height,
            render_width=int(width),
            render_height=int(height),
        )
        page_record["coord_origin"] = "top-left"
        for line_index, text in enumerate(page_lines):
            y = top + line_index * line_h
            page_record["lines"].append(
                make_line_record(
                    page_idx,
                    line_index,
                    text,
                    left,
                    y,
                    width - left * 2,
                    line_h,
                    11.0,
                    "b0",
                    f"p{line_index}",
                    1.0,
                    "native_text",
                    0,
                    [],
                    ocr_text=text,
                )
            )
        canonical["pages"].append(page_record)
    if not canonical["pages"]:
        canonical["pages"].append(
            make_page_record(
                page_index=0,
                width=width,
                height=height,
                render_width=int(width),
                render_height=int(height),
            )
        )
    return _finalize_canonical(canonical, json_path)


def _native_canonical_for_source(
    source_path: str,
    *,
    thorough: bool,
    json_path: str | None = None,
) -> tuple[dict | None, str]:
    ext = os.path.splitext(source_path)[1].lower()
    first_page_only = not thorough
    if ext == ".pdf":
        import fitz

        with fitz.open(source_path) as doc:
            if len(doc) == 0:
                return None, "PDF không có trang"
            page_indices = list(range(len(doc))) if thorough else [0]
        return (
            _canonical_from_pdf_text(
                source_path,
                source_path,
                page_indices,
                json_path=json_path,
            ),
            "PDF text sẵn có",
        )
    if ext == ".docx":
        lines = _docx_text_lines(source_path, first_page_only=first_page_only)
        return (
            _canonical_from_text_lines(source_path, lines, json_path=json_path),
            "DOCX text trực tiếp" + (" trang đầu" if first_page_only else ""),
        )
    if ext == ".doc":
        lines = _doc_text_lines_with_word(source_path, first_page_only=first_page_only)
        if not lines:
            return None, "DOC không đọc được text trực tiếp"
        return (
            _canonical_from_text_lines(source_path, lines, json_path=json_path),
            "DOC text trực tiếp" + (" trang đầu" if first_page_only else ""),
        )
    return None, ""


def _preprocess_pdf_for_ocr(
    pdf_path: str,
    out_pdf: str,
    log_cb: Callable[[str], None],
    max_workers: int | None = None,
) -> tuple[str, list[int] | None]:
    try:
        from scanindex.core.preprocessing import preprocessing

        result = preprocessing.pre_process_pdf(
            pdf_path,
            out_pdf,
            update_callback=lambda m, lvl="info": (
                log_cb(str(m)) if str(lvl).lower() != "debug" else None
            ),
            debug_mode=False,
            max_workers=max(1, int(max_workers or min(4, os.cpu_count() or 4))),
            return_metadata=True,
        )
        if isinstance(result, tuple) and len(result) == 3:
            ok, _msg, meta = result
        else:
            ok = bool(result[0]) if isinstance(result, tuple) else bool(result)
            meta = {}
        if ok and os.path.exists(out_pdf):
            rotations = (meta or {}).get("page_rotations") or None
            return out_pdf, rotations
    except Exception as exc:
        log_cb(f"Preprocess bỏ qua: {exc}")
    return pdf_path, None


def _ocr_one_page_single_worker(
    direct_ocr_engine,
    input_pdf: str,
    page_idx: int,
    *,
    dpi: int = _SECRET_SCAN_DPI,
) -> dict | None:
    """OCR one page through the in-process ScreenAI singleton.

    The shared pool is useful for many pages, but expensive to start for this
    tool's default first-page scan. This path initializes one DLL instance only.
    ``dpi`` defaults to _SECRET_SCAN_DPI (200) instead of the engine-wide 240 —
    stamps are large enough to read at 200 DPI and this is noticeably faster.
    """
    import fitz
    from PIL import Image

    with fitz.open(input_pdf) as doc:
        page = doc[page_idx]
        page_w = float(page.rect.width)
        page_h = float(page.rect.height)
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=mat, annots=True)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

    scale_x = page_w / pix.width if pix.width else 1.0
    scale_y = page_h / pix.height if pix.height else 1.0
    ocr = direct_ocr_engine._get_ocr()
    with direct_ocr_engine._ocr_lock:
        result = ocr.perform_ocr(img)
    ocr_lines = result.get("lines", []) if isinstance(result, dict) else []
    lines_data, words_data = direct_ocr_engine._ocr_result_to_page_data(
        page_idx,
        ocr_lines,
        scale_x,
        scale_y,
    )
    return {
        "lines_data": lines_data,
        "words_data": words_data,
        "render_width": pix.width,
        "render_height": pix.height,
    }


def _ocr_pdf_to_canonical(
    input_pdf: str,
    source_path: str,
    rotations: list[int] | None,
    cancel_event: threading.Event,
    log_cb: Callable[[str], None],
    *,
    json_path: str | None = None,
    use_pool: bool = False,
) -> dict:
    import fitz
    from scanindex.core.ocr import direct_engine as direct_ocr_engine
    from scanindex.core.kie.json_utils import (
        make_document_stub,
        make_page_record,
    )
    from scanindex.core.ocr.text_normalizer import OCR_TEXT_NORMALIZATION

    with fitz.open(input_pdf) as doc:
        page_count = len(doc)
        page_rects = [
            (float(page.rect.width), float(page.rect.height))
            for page in doc
        ]
    if page_count <= 0:
        raise RuntimeError("PDF không có trang để OCR")

    canonical = make_document_stub(
        input_path=input_pdf,
        engine="direct_screen_ai",
        ocr_dpi=direct_ocr_engine.OCR_DPI,
        source_path=source_path,
        text_normalization=OCR_TEXT_NORMALIZATION,
        raw_text_preserved=True,
    )
    all_page_results: dict[int, dict | None] = {}
    if use_pool and page_count >= 2 and direct_ocr_engine.get_parallel_capacity() > 1:
        if cancel_event.is_set():
            raise _ScanCancelled()
        log_cb(
            f"Parallel OCR: {direct_ocr_engine.get_parallel_capacity()} page workers "
            f"for {page_count} pages"
        )
        all_page_results = direct_ocr_engine._process_pages_parallel(
            input_pdf,
            page_count,
            lambda msg, level="info": (
                log_cb(str(msg)) if str(level).lower() != "debug" else None
            ),
            page_timeout=180.0,
        )
    elif not use_pool and getattr(direct_ocr_engine, "_ocr_instance", None) is None:
        log_cb("Khởi động ScreenAI OCR đơn (không dùng pool)...")
    if not use_pool:
        log_cb(f"OCR {page_count} trang...")
    for page_idx in range(page_count):
        if cancel_event.is_set():
            raise _ScanCancelled()
        result = all_page_results.get(page_idx)
        if result is None:
            result = _ocr_one_page_single_worker(direct_ocr_engine, input_pdf, page_idx)
        if result is None:
            raise RuntimeError(f"OCR thất bại ở trang {page_idx + 1}")
        page_w, page_h = page_rects[page_idx]
        lines_data = result.get("lines_data") or []
        words_data = result.get("words_data") or []
        coord_flipped = direct_ocr_engine._normalize_page_coord_to_top_left(
            lines_data,
            words_data,
            page_h,
        )
        applied_rotation = 0
        if rotations and page_idx < len(rotations):
            try:
                applied_rotation = int(rotations[page_idx] or 0) % 360
            except (TypeError, ValueError):
                applied_rotation = 0
        page = make_page_record(
            page_index=page_idx,
            width=page_w,
            height=page_h,
            render_width=result.get("render_width", 0),
            render_height=result.get("render_height", 0),
            applied_rotation=applied_rotation,
        )
        page["coord_origin"] = "top-left"
        if coord_flipped:
            page["coord_origin_source"] = "normalized_from_bottom_left"
        page["lines"] = lines_data
        page["words"] = words_data
        canonical["pages"].append(page)

    return _finalize_canonical(canonical, json_path)


def _build_native_page_record_from_pdf(
    page,
    page_idx: int,
) -> dict:
    """Build a canonical page record from native (digital) PDF text.

    Mirrors ``_canonical_from_pdf_text`` but for a single already-open page,
    so the per-page classifier can reuse it without re-opening the document.
    """
    import fitz  # noqa: F401  — type hint clarity; fitz already imported by caller

    from scanindex.core.kie.json_utils import (
        make_line_record,
        make_page_record,
    )

    page_record = make_page_record(
        page_index=page_idx,
        width=float(page.rect.width),
        height=float(page.rect.height),
        render_width=int(page.rect.width),
        render_height=int(page.rect.height),
    )
    page_record["coord_origin"] = "top-left"
    line_index = 0
    data = page.get_text("dict") or {}
    for block_index, block in enumerate(data.get("blocks") or []):
        if block.get("type", 0) != 0:
            continue
        for raw_line in block.get("lines") or []:
            spans = raw_line.get("spans") or []
            text = "".join(span.get("text") or "" for span in spans).strip()
            if not text:
                continue
            bbox = raw_line.get("bbox") or (0, 0, 0, 0)
            x0, y0, x1, y1 = [float(v or 0) for v in bbox[:4]]
            font_size = 11.0
            for span in spans:
                try:
                    font_size = max(font_size, float(span.get("size") or 0))
                except Exception:
                    pass
            page_record["lines"].append(
                make_line_record(
                    page_idx,
                    line_index,
                    text,
                    x0,
                    y0,
                    max(0.0, x1 - x0),
                    max(0.0, y1 - y0),
                    font_size,
                    f"b{block_index}",
                    f"p{block_index}",
                    1.0,
                    "native_text",
                    0,
                    [],
                    ocr_text=text,
                )
            )
            line_index += 1
    return page_record


def _process_pdf_per_page(
    pdf_path: str,
    source_path: str,
    page_indices: list[int],
    cancel_event: threading.Event,
    log_cb: Callable[[str], None],
    *,
    dpi: int = _SECRET_SCAN_DPI,
    json_path: str | None = None,
) -> dict:
    """Build a canonical document from a PDF, classifying each page.

    Reuses ``classify_pdf_page`` (the same per-page classifier the PDF-to-Word
    feature uses) so each page is handled correctly:
      - ``digital``  → native text via ``page.get_text`` (OCR skipped)
      - ``scan``/``mixed`` → ScreenAI OCR at ``dpi``

    This avoids the false negative the old fast-path had (scan_ocr_low text
    layer accepted blindly) and the wasted OCR of purely-digital pages.
    """
    import fitz

    from scanindex.core.kie.json_utils import make_document_stub, make_page_record
    from scanindex.core.ocr import direct_engine as direct_ocr_engine
    from scanindex.core.ocr.text_normalizer import OCR_TEXT_NORMALIZATION
    from scanindex.core.pdf.docx_page_manifest import classify_pdf_page

    with fitz.open(pdf_path) as doc:
        total = len(doc)
        page_rects = [
            (float(page.rect.width), float(page.rect.height)) for page in doc
        ]
        # Pre-open pages we need; classify once.
        classifications: dict[int, str] = {}
        for page_idx in page_indices:
            if page_idx < 0 or page_idx >= total:
                continue
            try:
                info = classify_pdf_page(doc, page_idx)
                classifications[page_idx] = info.get("source_mode") or "scan"
            except Exception as exc:
                log_cb(f"Phân loại trang {page_idx + 1} lỗi: {exc} → OCR")
                classifications[page_idx] = "scan"

    canonical = make_document_stub(
        input_path=pdf_path,
        engine="direct_screen_ai",
        ocr_dpi=dpi,
        source_path=source_path,
        text_normalization=OCR_TEXT_NORMALIZATION,
        raw_text_preserved=True,
    )

    # OCR scan/mixed pages. Always route through the shared page-level pool so
    # multiple files (run in parallel file-worker threads) share its workers —
    # this is what gives real 2-page concurrency. The screen pre-warms the pool
    # on show, so the DLL load cost is paid up-front. DPI override keeps
    # _SECRET_SCAN_DPI (200) instead of the engine-wide 240. The single-worker
    # path is only a fallback if the pool itself fails.
    ocr_pages = [p for p in page_indices if classifications.get(p, "scan") != "digital"]
    ocr_results: dict[int, dict | None] = {}
    if ocr_pages:
        use_pool = direct_ocr_engine.get_parallel_capacity() > 1
        if use_pool:
            log_cb(
                f"OCR {len(ocr_pages)}/{len(page_indices)} trang (scan/mixed) @ {dpi} DPI "
                f"qua pool {direct_ocr_engine.get_parallel_capacity()} worker..."
            )
            try:
                ocr_results = direct_ocr_engine._process_selected_pages_parallel(
                    pdf_path,
                    ocr_pages,
                    total,
                    lambda msg, lvl="info": (
                        log_cb(str(msg)) if str(lvl).lower() != "debug" else None
                    ),
                    dpi=dpi,
                )
            except Exception as exc:
                log_cb(f"Pool OCR lỗi, chuyển single-worker: {exc}")
                ocr_results = {}
        if not ocr_results:
            log_cb(f"OCR {len(ocr_pages)} trang (scan/mixed) @ {dpi} DPI single-worker...")
            for pidx in ocr_pages:
                if cancel_event.is_set():
                    raise _ScanCancelled()
                ocr_results[pidx] = _ocr_one_page_single_worker(
                    direct_ocr_engine, pdf_path, pidx, dpi=dpi
                )
    else:
        log_cb(f"Tất cả {len(page_indices)} trang là digital — bỏ OCR")

    # Assemble canonical pages in the requested order. Digital pages use
    # native text; scan/mixed pages consume the OCR results above.
    for page_idx in page_indices:
        if cancel_event.is_set():
            raise _ScanCancelled()
        if page_idx < 0 or page_idx >= total:
            continue
        page_w, page_h = page_rects[page_idx]
        source_mode = classifications.get(page_idx, "scan")

        if source_mode == "digital":
            with fitz.open(pdf_path) as doc:
                page = doc[page_idx]
                page_record = _build_native_page_record_from_pdf(page, page_idx)
            page_record["source_mode"] = "digital"
            canonical["pages"].append(page_record)
            continue

        result = ocr_results.get(page_idx)
        if result is None:
            raise RuntimeError(f"OCR thất bại ở trang {page_idx + 1}")
        lines_data = result.get("lines_data") or []
        words_data = result.get("words_data") or []
        coord_flipped = direct_ocr_engine._normalize_page_coord_to_top_left(
            lines_data,
            words_data,
            page_h,
        )
        page_record = make_page_record(
            page_index=page_idx,
            width=page_w,
            height=page_h,
            render_width=result.get("render_width", 0),
            render_height=result.get("render_height", 0),
        )
        page_record["coord_origin"] = "top-left"
        if coord_flipped:
            page_record["coord_origin_source"] = "normalized_from_bottom_left"
        page_record["lines"] = lines_data
        page_record["words"] = words_data
        page_record["source_mode"] = source_mode
        canonical["pages"].append(page_record)

    return _finalize_canonical(canonical, json_path)


def _detect_secret_on_page(canonical_doc: dict, page_index: int) -> str | None:
    from scanindex.core.kie.inference_pipeline import detect_secrecy_mark

    pages = canonical_doc.get("pages") or []
    for ordinal, page in enumerate(pages):
        try:
            idx = int(page.get("page_index", ordinal))
        except Exception:
            idx = ordinal
        if idx != int(page_index):
            continue
        clone = dict(page)
        clone["page_index"] = 0
        return detect_secrecy_mark({"pages": [clone]})
    return None


def _doc_start_pages(canonical_json_path: str) -> tuple[list[int], str]:
    from scanindex.core.digitization import page_splitter

    result = page_splitter.predict_doc_starts(canonical_json_path)
    pages = [int(p) for p in (result.get("start_pages") or [])]
    pages = sorted(set(p for p in pages if p >= 0))
    return pages or [0], f"LightGBM: {len(pages or [0])} trang đầu văn bản"


def _all_page_indices(canonical: dict) -> list[int]:
    page_indices: list[int] = []
    for ordinal, page in enumerate(canonical.get("pages") or []):
        try:
            page_indices.append(int(page.get("page_index", ordinal)))
        except (TypeError, ValueError):
            page_indices.append(ordinal)
    page_indices = sorted(set(idx for idx in page_indices if idx >= 0))
    return page_indices or [0]


def _canonical_has_text(canonical: dict) -> bool:
    for page in canonical.get("pages") or []:
        for line in page.get("lines") or []:
            if (line.get("text") or "").strip():
                return True
        for word in page.get("words") or []:
            if (word.get("text") or "").strip():
                return True
    return False


def page_results_from_canonical(canonical: dict) -> dict[int, dict]:
    results: dict[int, dict] = {}
    for ordinal, page in enumerate(canonical.get("pages") or []):
        try:
            page_index = int(page.get("page_index", ordinal))
        except (TypeError, ValueError):
            page_index = ordinal
        results[page_index] = {
            "lines_data": copy.deepcopy(page.get("lines") or []),
            "words_data": copy.deepcopy(page.get("words") or []),
            "render_width": int(page.get("render_width") or 0),
            "render_height": int(page.get("render_height") or 0),
        }
    return results


def _page_indices_to_check(
    canonical: dict,
    *,
    thorough: bool,
    note: str,
) -> tuple[list[int], str]:
    if not thorough:
        return [0], note
    page_indices = _all_page_indices(canonical)
    return page_indices, f"{note}; kiểm tất cả {len(page_indices)} trang"


def _collect_secret_matches(
    canonical: dict,
    page_indices: list[int],
    *,
    source_path: str,
    relative_path: str,
    mode: str,
    artifact_path: str,
    note: str,
    cancel_event: threading.Event,
) -> list[SecretScanMatch]:
    matches: list[SecretScanMatch] = []
    for page_index in page_indices:
        if cancel_event.is_set():
            raise _ScanCancelled()
        keyword = _detect_secret_on_page(canonical, page_index)
        if keyword:
            matches.append(
                SecretScanMatch(
                    source_path=source_path,
                    relative_path=relative_path,
                    keyword=keyword,
                    page_number=int(page_index) + 1,
                    mode=mode,
                    ocr_pdf_path=artifact_path,
                    note=note,
                )
            )
    return matches


def _filter_matches_by_doc_start(
    matches: list[SecretScanMatch],
    canonical_json_path: str,
    log_cb: Callable[[str], None],
) -> list[SecretScanMatch]:
    if not matches:
        return []
    try:
        doc_start_pages, lgbm_note = _doc_start_pages(canonical_json_path)
    except Exception as exc:
        log_cb(f"LightGBM lỗi, không xác nhận trang đầu văn bản: {exc}")
        return []

    starts = set(doc_start_pages)
    accepted: list[SecretScanMatch] = []
    for match in matches:
        page_index = max(0, int(match.page_number) - 1)
        if page_index in starts:
            match.note = f"{match.note}; {lgbm_note}; xác nhận trang đầu"
            accepted.append(match)
        else:
            log_cb(
                f"Bỏ qua dấu {match.keyword} ở trang {match.page_number}: "
                "LightGBM không xác định là trang đầu văn bản"
            )
    return accepted


def scan_one_file_for_secret_artifact(
    source_path: str,
    relative_path: str,
    file_work_dir: str,
    first_page_only: bool,
    cancel_event: threading.Event,
    log_cb: Callable[[str], None],
    ocr_workers: int | None = None,
) -> SecretScanArtifact:
    """Scan one file for classified-document stamps.

    Routing:
      - Word (.doc/.docx): always treated as digital — native text only, no OCR.
      - PDF: each page is classified (digital/scan/mixed) via ``classify_pdf_page``
        (same classifier as PDF-to-Word). Digital pages use native text; scan/mixed
        pages are OCR'd at ``_SECRET_SCAN_DPI`` (200).

    Modes:
      - ``first_page_only=True``  ("Tìm nhanh"): process only page 0, detect the
        secrecy mark on it, no LightGBM, no other pages.
      - ``first_page_only=False`` ("Tìm kỹ"): process ALL pages, detect secrecy
        on every page (cheap token match), then run LightGBM doc-start only on
        the candidate pages to filter false positives. For non-classified files
        (the common case) there are no candidates, so LightGBM is skipped.
    """
    os.makedirs(file_work_dir, exist_ok=True)
    ext = os.path.splitext(source_path)[1].lower()
    word_document = ext in {".doc", ".docx"}
    mode = "Trang đầu" if first_page_only else "Tìm kỹ"
    canonical_json_path = os.path.join(file_work_dir, "ocr.json.zst")

    # ── Word: always digital, native text, never OCR ──────────────────────
    if word_document:
        native_canonical, native_note = _native_canonical_for_source(
            source_path,
            thorough=not first_page_only,
            json_path=canonical_json_path,
        )
        if native_canonical is None:
            raise RuntimeError("Không đọc được DOC/DOCX — cần Word hoặc LibreOffice")
        chars, lines = _native_text_stats(native_canonical)
        log_cb(f"DOC/DOCX digital: dùng text trực tiếp ({lines} dòng, {chars} ký tự)")
        note = native_note
        page_indices = [0] if first_page_only else _all_page_indices(native_canonical)
        if first_page_only:
            note = f"{note}; DOC/DOCX trang đầu"
        matches = _collect_secret_matches(
            native_canonical,
            page_indices,
            source_path=source_path,
            relative_path=relative_path,
            mode=mode,
            artifact_path=canonical_json_path,
            note=note,
            cancel_event=cancel_event,
        )
        return SecretScanArtifact(
            matches=matches,
            source_pdf="",
            canonical=native_canonical,
            rotations=None,
        )

    # ── PDF: per-page classify (digital / scan / mixed) ───────────────────
    source_pdf, source_note = _source_to_pdf(
        source_path,
        file_work_dir,
        first_page_only=first_page_only,
    )
    if cancel_event.is_set():
        raise _ScanCancelled()

    # Determine which pages to process.
    import fitz

    with fitz.open(source_pdf) as doc:
        total_pages = len(doc)
    if total_pages <= 0:
        raise RuntimeError("PDF không có trang")
    if first_page_only:
        page_indices = [0]
    else:
        page_indices = list(range(total_pages))

    # For single-page PDFs, no need to extract a sub-PDF — classify + OCR in place.
    scan_pdf = source_pdf
    if first_page_only and ext == ".pdf" and total_pages > 1:
        first_pdf = os.path.join(file_work_dir, "first_page.pdf")
        scan_pdf = _extract_pdf_pages(source_pdf, first_pdf, [0])
        page_indices = [0]

    canonical = _process_pdf_per_page(
        scan_pdf,
        source_path,
        page_indices,
        cancel_event,
        log_cb,
        dpi=_SECRET_SCAN_DPI,
        json_path=canonical_json_path,
    )

    # If OCR produced no text at all on a scan page, try preprocess (deskew/rotate).
    if not _canonical_has_text(canonical) and not first_page_only:
        log_cb("OCR không ra chữ, thử preprocess xoay/nghiêng...")
        pre_pdf = os.path.join(file_work_dir, "preprocessed.pdf")
        ocr_input_pdf, rotations = _preprocess_pdf_for_ocr(
            scan_pdf,
            pre_pdf,
            log_cb,
            max_workers=ocr_workers,
        )
        if os.path.abspath(ocr_input_pdf) != os.path.abspath(scan_pdf):
            canonical = _process_pdf_per_page(
                ocr_input_pdf,
                source_path,
                page_indices,
                cancel_event,
                log_cb,
                dpi=_SECRET_SCAN_DPI,
                json_path=canonical_json_path,
            )
        else:
            rotations = None
    else:
        rotations = None

    note = source_note
    if first_page_only:
        # "Tìm nhanh": detect secrecy on page 0 only, no LightGBM.
        matches = _collect_secret_matches(
            canonical,
            [0],
            source_path=source_path,
            relative_path=relative_path,
            mode=mode,
            artifact_path=canonical_json_path,
            note=f"{note}; trang đầu",
            cancel_event=cancel_event,
        )
        return SecretScanArtifact(
            matches=matches,
            source_pdf=scan_pdf,
            canonical=canonical,
            rotations=rotations,
        )

    # "Tìm kỹ": detect secrecy on ALL pages first (cheap token match), then
    # run LightGBM doc-start only on the candidate pages that matched a secrecy
    # keyword. This is faster than running LightGBM across the whole document:
    # detection is near-free regex, while LightGBM scores 8 features per page.
    # For the common case (file is NOT classified) there are zero candidates,
    # so LightGBM is skipped entirely via the early-return in
    # _filter_matches_by_doc_start.
    note = f"{note}; kiểm tất cả {len(page_indices)} trang"
    matches = _collect_secret_matches(
        canonical,
        page_indices,
        source_path=source_path,
        relative_path=relative_path,
        mode=mode,
        artifact_path=canonical_json_path,
        note=note,
        cancel_event=cancel_event,
    )
    matches = _filter_matches_by_doc_start(matches, canonical_json_path, log_cb)
    return SecretScanArtifact(
        matches=matches,
        source_pdf=scan_pdf,
        canonical=canonical,
        rotations=rotations,
    )


def scan_one_file_for_secret(
    source_path: str,
    relative_path: str,
    file_work_dir: str,
    first_page_only: bool,
    cancel_event: threading.Event,
    log_cb: Callable[[str], None],
    ocr_workers: int | None = None,
) -> list[SecretScanMatch]:
    return scan_one_file_for_secret_artifact(
        source_path=source_path,
        relative_path=relative_path,
        file_work_dir=file_work_dir,
        first_page_only=first_page_only,
        cancel_event=cancel_event,
        log_cb=log_cb,
        ocr_workers=ocr_workers,
    ).matches


class SecretFileScanScreen(ScreenContent):
    """Find classified-document stamps in supported files inside a folder."""

    log_message = Signal(str, str)
    _status_changed = Signal(str)
    _progress_changed = Signal(int, int)
    _result_found = Signal(object)
    _scan_finished = Signal(object)
    # Emitted from the background warm-up thread once the OCR pool is ready
    # (or failed); handled on the main thread by _on_pool_ready.
    _pool_ready = Signal(bool, str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setStyleSheet(f"background: {COLOR_BG};")
        self._busy = False
        self._cancel_event = threading.Event()
        self._results: list[SecretScanMatch] = []
        self._pool_warm_started = False
        self._pool_ready_flag = False
        self._build_ui()
        self._status_changed.connect(self._set_status)
        self._progress_changed.connect(self._set_progress)
        self._result_found.connect(self._add_result)
        self._scan_finished.connect(self._on_finished)
        self._pool_ready.connect(self._on_pool_ready)

    def is_busy(self) -> bool:
        return self._busy

    def request_cancel(self) -> None:
        self._cancel_event.set()

    def showEvent(self, event):
        """Pre-warm the OCR pool the first time this screen is shown so the
        DLL load cost is paid up-front instead of at the first scan."""
        super().showEvent(event)
        self._maybe_warm_pool()

    def _maybe_warm_pool(self) -> None:
        if self._pool_warm_started or self._pool_ready_flag:
            return
        self._pool_warm_started = True
        self._status_changed.emit("Đang khởi tạo OCR...")
        # Nút Bắt đầu quét mờ cho đến khi pool sẵn sàng (xem _on_pool_ready).
        # _set_running_ui(False) luôn bật lại nút, nên ta disable trực tiếp
        # và để _on_pool_ready bật lại khi xong.
        self.btn_run.setEnabled(False)

        def _warm():
            ok = False
            message = ""
            try:
                from scanindex.core.ocr import direct_engine
                direct_engine.get_page_pool()
                ok = True
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
            self._pool_ready.emit(ok, message)

        threading.Thread(
            target=_warm, daemon=True, name="secret-scan-pool-warm"
        ).start()

    def _on_pool_ready(self, ok: bool, message: str) -> None:
        self._pool_ready_flag = True
        if self._busy:
            return  # Một lượt quét đang chạy; không đụng vào UI.
        if ok:
            self._status_changed.emit("Sẵn sàng")
        else:
            self.log_message.emit(
                f"Khởi tạo OCR thất bại: {message}", "err"
            )
            self._status_changed.emit("OCR chưa sẵn sàng")
        self.btn_run.setEnabled(True)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(SP[5], SP[5], SP[5], SP[5])
        layout.setSpacing(SP[3])

        picker = QFrame()
        picker.setStyleSheet(
            f"QFrame {{ background: {COLOR_PANEL}; border: 1px solid {COLOR_BORDER};"
            f" border-radius: {RADIUS_MD}px; }}"
        )
        picker_layout = QVBoxLayout(picker)
        picker_layout.setContentsMargins(SP[3], SP[3], SP[3], SP[3])
        picker_layout.setSpacing(SP[2])

        row = QHBoxLayout()
        self.folder_edit = QLineEdit()
        self.folder_edit.setReadOnly(True)
        self.folder_edit.setPlaceholderText("Chọn thư mục cần quét")
        self.folder_edit.setStyleSheet(
            f"QLineEdit {{ background: {COLOR_INPUT}; color: {COLOR_TEXT};"
            f" border: 1px solid {COLOR_BORDER}; border-radius: {RADIUS_MD}px;"
            f" padding: 7px 9px; font: 13px '{FONT_UI}'; }}"
        )
        row.addWidget(self.folder_edit, 1)

        self.btn_browse = QPushButton("Chọn thư mục")
        self.btn_browse.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_browse.setStyleSheet(self._secondary_btn_qss())
        self.btn_browse.clicked.connect(self._browse_folder)
        row.addWidget(self.btn_browse)
        picker_layout.addLayout(row)

        opts = QHBoxLayout()
        opts.addStretch(1)

        self.fast_checkbox = QCheckBox("Tìm nhanh — chỉ trang đầu")
        self.fast_checkbox.setChecked(True)
        self.fast_checkbox.setCursor(Qt.CursorShape.PointingHandCursor)
        self.fast_checkbox.setStyleSheet(
            f"QCheckBox {{ color: {COLOR_TEXT}; font: 13px '{FONT_UI}';"
            f" padding: 4px 8px; }}"
            f"QCheckBox::indicator {{ width: 16px; height: 16px; }}"
        )
        self.fast_checkbox.setToolTip(
            "Bật: chỉ kiểm tra trang đầu mỗi tài liệu (rất nhanh).\n"
            "Tắt: Tìm kỹ — quét toàn bộ trang, phát hiện dấu mật trên mọi trang "
            "rồi chạy LightGBM để lọc các trang không phải trang đầu văn bản."
        )
        opts.addWidget(self.fast_checkbox)

        self.btn_run = QPushButton("Bắt đầu quét")
        self.btn_run.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_run.setStyleSheet(self._primary_btn_qss())
        self.btn_run.clicked.connect(self._run_clicked)
        opts.addWidget(self.btn_run)

        self.btn_stop = QPushButton("Dừng")
        self.btn_stop.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_stop.setStyleSheet(self._danger_btn_qss())
        self.btn_stop.clicked.connect(self._stop_clicked)
        self.btn_stop.setVisible(False)
        opts.addWidget(self.btn_stop)
        picker_layout.addLayout(opts)
        layout.addWidget(picker)

        status_row = QHBoxLayout()
        self.status_label = QLabel("Chưa chạy")
        self.status_label.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; font: 13px '{FONT_UI}';"
        )
        status_row.addWidget(self.status_label, 1)
        self.progress = QProgressBar()
        self.progress.setMinimum(0)
        self.progress.setMaximum(1)
        self.progress.setValue(0)
        self.progress.setFixedWidth(220)
        status_row.addWidget(self.progress)
        layout.addLayout(status_row)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["Độ mật", "File", "Trang", "Chế độ", "Ghi chú"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.itemDoubleClicked.connect(self._open_result_file)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.setStyleSheet(
            f"QTableWidget {{ background-color: {COLOR_SURFACE}; color: {COLOR_TEXT};"
            f" border: 1px solid {COLOR_BORDER}; border-radius: {RADIUS_MD}px;"
            " gridline-color: #555555; }}"
            f"QHeaderView::section {{ background-color: {COLOR_PANEL}; color: {COLOR_TEXT};"
            f" border: 1px solid {COLOR_BORDER}; padding: 6px;"
            f" font-size: 12px; font-weight: 600; font-family: '{FONT_UI}'; }}"
            f"QTableWidget::item:selected {{ background-color: {COLOR_ACCENT}; }}"
        )
        layout.addWidget(self.table, 1)

    def _primary_btn_qss(self) -> str:
        return (
            f"QPushButton {{ background: {COLOR_ACCENT}; color: white;"
            f" border: none; padding: 8px 16px; border-radius: {RADIUS_MD}px;"
            f" font: 600 13px '{FONT_UI}'; }}"
            f"QPushButton:hover {{ background: {COLOR_ACCENT_HOVER}; }}"
            f"QPushButton:disabled {{ background: #555; color: #aaa; }}"
        )

    def _secondary_btn_qss(self) -> str:
        return (
            f"QPushButton {{ background: transparent; color: {COLOR_TEXT};"
            f" border: 1px solid {COLOR_BORDER}; padding: 8px 14px;"
            f" border-radius: {RADIUS_MD}px; font: 600 13px '{FONT_UI}'; }}"
            f"QPushButton:hover {{ background: {COLOR_SURFACE}; }}"
            f"QPushButton:disabled {{ color: #888; }}"
        )

    def _danger_btn_qss(self) -> str:
        return (
            f"QPushButton {{ background: {COLOR_RED}; color: white;"
            f" border: none; padding: 8px 16px; border-radius: {RADIUS_MD}px;"
            f" font: 600 13px '{FONT_UI}'; }}"
            "QPushButton:hover { background: #bd2130; }"
        )

    def _browse_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, translations.localize_text("Chọn thư mục cần quét")
        )
        if folder:
            self.folder_edit.setText(folder)

    def _run_clicked(self) -> None:
        folder = self.folder_edit.text().strip()
        if not folder or not os.path.isdir(folder):
            QMessageBox.information(self, "Chưa có thư mục", "Vui lòng chọn thư mục cần quét.")
            return
        if self._busy:
            return

        self._busy = True
        self._cancel_event.clear()
        self._results = []
        self.table.setRowCount(0)
        self._set_running_ui(True)
        self._set_status("Đang chuẩn bị...")
        self.progress.setValue(0)
        self.progress.setMaximum(1)

        first_page_only = self.fast_checkbox.isChecked()
        thread = threading.Thread(
            target=self._run_worker,
            args=(folder, first_page_only),
            daemon=True,
            name="secret-file-scan",
        )
        thread.start()

    def _stop_clicked(self) -> None:
        self._cancel_event.set()
        self._set_status("Đang dừng...")

    def _set_running_ui(self, running: bool) -> None:
        self.btn_browse.setEnabled(not running)
        self.folder_edit.setEnabled(not running)
        self.fast_checkbox.setEnabled(not running)
        self.btn_run.setVisible(not running)
        self.btn_stop.setVisible(running)

    def _run_worker(self, folder: str, first_page_only: bool) -> None:
        from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

        started = time.strftime("%Y%m%d_%H%M%S")
        work_root = os.path.join(get_base_dir(), "temp", f"secret_scan_{started}")
        os.makedirs(work_root, exist_ok=True)
        files = list(_iter_supported_files(folder))
        total = len(files)
        self._progress_changed.emit(0, max(1, total))
        self.log_message.emit(f"Quét file mật: tìm thấy {total} file hỗ trợ", "info")

        # File-level parallelism: each worker thread processes one file. The
        # OCR pool (when used inside _process_pdf_per_page) is a shared global,
        # so scan pages from multiple files are distributed across its workers
        # automatically — this is what makes "2 pages at a time regardless of
        # file" work in Tìm kỹ mode. In Tìm nhanh mode, two files' first pages
        # are OCR'd concurrently.
        max_file_workers = max(1, min(2, total))
        progress_lock = threading.Lock()
        done = [0]
        scanned = [0]
        failures = [0]
        cancelled = [False]

        def process_one(idx: int, path: str) -> None:
            if self._cancel_event.is_set():
                return
            rel = os.path.relpath(path, folder)
            self._status_changed.emit(f"Đang quét {idx}/{total}: {rel}")
            file_work = os.path.join(work_root, f"{idx:05d}_{_safe_name(Path(path).stem)}")

            def file_log(message: str, rel_path=rel) -> None:
                self.log_message.emit(f"[{rel_path}] {message}", "info")

            try:
                matches = scan_one_file_for_secret(
                    path,
                    rel,
                    file_work,
                    first_page_only,
                    self._cancel_event,
                    file_log,
                )
                for match in matches:
                    self._result_found.emit(match)
                with progress_lock:
                    scanned[0] += 1
            except _ScanCancelled:
                cancelled[0] = True
            except Exception as exc:
                with progress_lock:
                    failures[0] += 1
                self.log_message.emit(f"[{rel}] Lỗi: {exc}", "err")
            finally:
                if os.path.isdir(file_work):
                    shutil.rmtree(file_work, ignore_errors=True)
                with progress_lock:
                    done[0] += 1
                    self._progress_changed.emit(done[0], max(1, total))

        try:
            with ThreadPoolExecutor(
                max_workers=max_file_workers, thread_name_prefix="secret-scan-file"
            ) as executor:
                futures = {
                    executor.submit(process_one, idx, path): idx
                    for idx, path in enumerate(files, start=1)
                }
                pending = set(futures)
                try:
                    while pending and not self._cancel_event.is_set():
                        done_set, pending = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
                        for fut in done_set:
                            # Re-raise any exception from the worker (except
                            # _ScanCancelled, which only flags cancellation).
                            try:
                                fut.result()
                            except _ScanCancelled:
                                cancelled[0] = True
                except _ScanCancelled:
                    cancelled[0] = True
                if self._cancel_event.is_set():
                    cancelled[0] = True
                    for fut in futures:
                        fut.cancel()
        finally:
            self._scan_finished.emit(
                {
                    "total": total,
                    "scanned": scanned[0],
                    "failures": failures[0],
                    "cancelled": cancelled[0],
                    "work_root": work_root,
                }
            )

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)

    def _set_progress(self, current: int, total: int) -> None:
        self.progress.setMaximum(max(1, total))
        self.progress.setValue(max(0, min(current, max(1, total))))

    def _add_result(self, match: SecretScanMatch) -> None:
        self._results.append(match)
        row = self.table.rowCount()
        self.table.insertRow(row)
        values = [
            match.keyword,
            match.relative_path,
            str(match.page_number),
            match.mode,
            match.note,
        ]
        for col, value in enumerate(values):
            item = QTableWidgetItem(value)
            if col == 3:
                translations.set_translatable_item_text(
                    item, value, sync_tooltip=True
                )
            elif col == 4:
                translations.set_translatable_item_text(
                    item, value, context="secret_note", sync_tooltip=True
                )
            item.setData(Qt.ItemDataRole.UserRole, match.source_path)
            if col not in (3, 4):
                item.setToolTip(match.source_path if col == 1 else value)
            if col == 0:
                item.setForeground(QColor(COLOR_RED))
            self.table.setItem(row, col, item)

    def _on_finished(self, payload: dict) -> None:
        self._busy = False
        self._set_running_ui(False)
        total = int(payload.get("total") or 0)
        scanned = int(payload.get("scanned") or 0)
        failures = int(payload.get("failures") or 0)
        cancelled = bool(payload.get("cancelled"))
        found = len(self._results)
        prefix = "Đã dừng" if cancelled else "Hoàn tất"
        self._set_status(
            f"{prefix}: quét {scanned}/{total} file, phát hiện {found} dòng mật, lỗi {failures}"
        )
        self.log_message.emit(
            f"Quét file mật: {prefix.lower()} - {scanned}/{total} file, "
            f"{found} dòng mật, lỗi {failures}.",
            "success" if not cancelled else "info",
        )

        # Wipe per-run workdir — UI keeps results in memory; nothing on disk
        # in temp/secret_scan_<ts>/ is needed after the scan completes. Match
        # behavior of cleanup_stale_temp_dirs() at startup.
        work_root = payload.get("work_root") if isinstance(payload, dict) else None
        if work_root and os.path.isdir(work_root):
            try:
                shutil.rmtree(work_root, ignore_errors=True)
            except Exception:
                pass

    def _open_result_file(self, item: QTableWidgetItem) -> None:
        source_path = item.data(Qt.ItemDataRole.UserRole)
        if source_path and os.path.exists(source_path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(source_path))
