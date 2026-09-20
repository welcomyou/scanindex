"""Tool screen for scanning folders for classified document stamps."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import textwrap
import threading
import time
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from scanindex.infra.paths import get_base_dir
from scanindex.infra.app_log import write as app_log_write
from scanindex.infra.mem_stats import memory_snapshot_text
from scanindex.core import secret_scan_progress as ssp
from scanindex.ui.screens.screen_base import ScreenContent
from scanindex.ui.widgets.pdf_viewer_widget import PdfViewerWidget
from scanindex.ui.theme import (
    COLOR_ACCENT,
    COLOR_ACCENT_HOVER,
    COLOR_BG,
    COLOR_BORDER,
    COLOR_GREEN,
    COLOR_GREEN_HOVER,
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


class _ScanSkip(Exception):
    """File không bao giờ quét được (rỗng / không phải PDF / 0 trang).

    Khác với lỗi tạm thời: ghi journal với trạng thái "skip" để resume
    bỏ qua luôn, không thử lại mỗi lượt quét và không đếm là Lỗi."""
    pass


# Số file tối đa được submit vào ThreadPoolExecutor cùng lúc. Queuing mọi
# file của một thư mục 300k+ file một thể tạo hàng trăm nghìn đối tượng
# Future trong process chính — cửa sổ trượt giữ RAM phẳng trong khi 2
# file-worker vẫn luôn có việc.
_SCAN_MAX_INFLIGHT_FILES = 32

# PyMuPDF 1.26 (rebased bindings) khởi tạo MuPDF ở chế độ single-context
# (reinit_singlethreaded) — các thread chia sẻ 1 context. Stress-test đã
# tái hiện crash native (0xC00000FF) khi 2 thread ĐỒNG THỜI fitz.open()
# các file HỎNG (race trên đường báo lỗi fz_throw của shared context),
# trong khi 1 thread chạy 20s với 45k lượt gọi thì sống, và file HỢP LỆ
# đa luồng thì ổn định. Mọi thao tác fitz trong process chính (2 file-
# worker thread) đều phải giữ lock này; phần chờ OCR pool KHÔNG giữ lock
# (OCR chạy ở process con) nên throughput song song vẫn giữ được.
# Đây là nguyên nhân crash 0xc0000409 trên máy quét (crash lúc 2s sau đợt
# sweep file rác _._/0-byte đầu lượt resume, pid = process chính).
_FITZ_LOCK = threading.RLock()


@dataclass
class SecretScanMatch:
    source_path: str
    relative_path: str
    keyword: str
    page_number: int
    mode: str
    ocr_pdf_path: str
    note: str = ""
    # Giải mật tự động: năm ban hành (0 = không xác định được), thời hạn
    # theo độ mật và cờ "đã quá thời hạn" để bảng/Excel tô xanh ghi chú.
    issue_year: int = 0
    declass_years: int = 0
    declass_due: bool = False


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

    with _FITZ_LOCK:
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
    with _FITZ_LOCK:
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

    with _FITZ_LOCK:
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
    with _FITZ_LOCK:
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

        with _FITZ_LOCK:
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

    with _FITZ_LOCK:
        with fitz.open(input_pdf) as doc:
            page = doc[page_idx]
            page_w = float(page.rect.width)
            page_h = float(page.rect.height)
            mat = fitz.Matrix(dpi / 72, dpi / 72)
            pix = page.get_pixmap(matrix=mat, annots=True)
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

    # perform_ocr NẰM NGOÀI _FITZ_LOCK: DLL OCR chạy chậm (giây/trang),
    # giữ lock ở đây sẽ triệt tiêu song song giữa 2 file-worker.
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

    with _FITZ_LOCK:
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

    with _FITZ_LOCK:
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
            with _FITZ_LOCK:
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


# ---------------------------------------------------------------------------
# Giải mật tự động theo độ mật
# ---------------------------------------------------------------------------

# Quy định giải mật tự động: Mật 10 năm, Tối mật 20 năm, Tuyệt mật 30 năm
# kể từ năm ban hành. Đủ thời hạn thì file VẪN nằm trong danh sách mật,
# chỉ thêm ghi chú "Đáp ứng thời gian giải mật" (tô xanh ở bảng/Excel).
DECLASS_YEARS_BY_KEYWORD = {"TUYỆT MẬT": 30, "TỐI MẬT": 20, "MẬT": 10}
DECLASS_NOTE_LABEL = "Đáp ứng thời gian giải mật"

# "…, ngày 15 tháng 4 năm 2026" — cho phép vài ký tự OCR rác giữa các chữ.
_ISSUE_DATE_RE = re.compile(
    r"NGAY\D{0,4}(\d{1,2})\D{0,4}THANG\D{0,4}(\d{1,2})\D{0,4}NAM\D{0,4}(\d{4})"
)
# Dòng ngày ban hành ("Hà Nội, ngày 04 tháng 4 năm 2016") ngắn và nằm ở
# vùng tiêu đề; trích yếu / văn bản dẫn chứa ngày khác thì dài, ở giữa trang.
_ISSUE_DATE_LINE_MAX_CHARS = 100


def _normalise_for_date(text: str) -> str:
    normalized = unicodedata.normalize(
        "NFD", (text or "").replace("đ", "d").replace("Đ", "D")
    )
    stripped = "".join(
        c for c in normalized if unicodedata.category(c) != "Mn"
    ).upper()
    return " ".join(stripped.split())


def _page_for_index(canonical: dict, page_index: int) -> dict | None:
    pages = canonical.get("pages") or []
    want = int(page_index)
    for page in pages:
        try:
            if int(page.get("page_index", 0) or 0) == want:
                return page
        except (TypeError, ValueError):
            continue
    return pages[0] if want == 0 and pages else None


def _issue_date_candidates(page: dict) -> list[tuple[float, int]]:
    """Các dòng ngày ban hành ứng viên: (tâm y, năm), sắp theo y tăng dần.

    Chỉ nhận dòng ngắn (dòng ngày ban hành luôn ngắn; trích yếu / văn bản
    dẫn chứa ngày khác thì dài) và ngày hợp lệ (ngày 1–31, tháng 1–12).
    """
    candidates: list[tuple[float, int]] = []
    for line in page.get("lines") or []:
        text = (line.get("text", "") or "").strip()
        if not text or len(text) > _ISSUE_DATE_LINE_MAX_CHARS:
            continue
        found = _ISSUE_DATE_RE.search(_normalise_for_date(text))
        if not found:
            continue
        day, month, year = (int(found.group(i)) for i in (1, 2, 3))
        if not (1 <= day <= 31 and 1 <= month <= 12):
            continue
        bbox = line.get("bbox") or [0, 0, 0, 0]
        try:
            y_center = (float(bbox[1]) + float(bbox[3])) / 2
        except (TypeError, ValueError):
            y_center = 0.0
        candidates.append((y_center, year))
    candidates.sort(key=lambda item: item[0])
    return candidates


def _extract_issue_year(canonical: dict, page_index: int = 0) -> int:
    """Năm ban hành suy từ dòng "ngày… tháng… năm…" ngắn gần đầu trang nhất.

    Dùng đúng trang chứa dấu mật (file ghép nhiều văn bản, mỗi trang đầu
    có dòng ngày riêng). Trong các dòng ứng viên, dòng ngày ban hành thuộc
    vùng tiêu đề, phía trên trích yếu / văn bản dẫn có thể chứa ngày của
    văn bản KHÁC — thứ tự dòng trong canonical không đảm bảo theo chiều
    trên-trang nên không lấy dòng đầu tiên khớp. Trả 0 nếu không có.
    """
    page = _page_for_index(canonical, page_index)
    if page is None:
        return 0
    candidates = _issue_date_candidates(page)
    return candidates[0][1] if candidates else 0


# Cascade lấy năm: regex chọn dòng trên-trái là miễn phí; chỉ khi trang có
# NHIỀU hơn 2 dòng ngày ứng viên (nhiễu kiểu "BIÊN BẢN HỌP NGÀY 15 THÁNG 10
# NĂM 2004") mới gọi LayoutLM — model có nhãn PLACE_DATE, gán đúng dòng
# ngày ban hành — chi phí model chỉ phát sinh trên các trang nhiễu.
# KIE KHÔNG dùng để xác định độ mật (model không có nhãn dấu mật).
# Tắt bằng SECRET_SCAN_DISABLE_KIE_YEAR=1.
_DECLASS_KIE_MIN_CANDIDATES = 3
_KIE_YEAR_LOCK = threading.RLock()


def _kie_issue_year(canonical_json_path: str, page_index: int) -> int | None:
    """Năm ban hành theo field PLACE_DATE của LayoutLM trên đúng trang.

    Trả None khi model không có sẵn / lỗi / không gán nhãn được — caller
    fallback về kết quả regex. Chạy dưới lock để 2 file-worker không cùng
    warmup/infer model song song.
    """
    if not canonical_json_path or not os.path.exists(canonical_json_path):
        return None
    try:
        from scanindex.core.kie.engine import _run_layoutlmv3

        with _KIE_YEAR_LOCK:
            payload = _run_layoutlmv3(
                canonical_json_path, selected_pages=[int(page_index)]
            )
    except Exception:
        return None
    for field in payload.get("field_instances") or []:
        if str(field.get("label") or "") != "PLACE_DATE":
            continue
        try:
            if int(field.get("page_index", -1)) != int(page_index):
                continue
        except (TypeError, ValueError):
            continue
        found = _ISSUE_DATE_RE.search(
            _normalise_for_date(field.get("text") or "")
        )
        if not found:
            continue
        day, month, year = (int(found.group(i)) for i in (1, 2, 3))
        if 1 <= day <= 31 and 1 <= month <= 12:
            return year
    return None


def _match_meets_declassification(match: SecretScanMatch) -> bool:
    return bool(match.declass_due) or DECLASS_NOTE_LABEL in (match.note or "")


def _apply_declassification_notes(
    matches: list[SecretScanMatch],
    canonical: dict,
    canonical_json_path: str | None = None,
) -> None:
    """Gắn ghi chú "Đáp ứng thời gian giải mật" khi đã quá thời hạn mật.

    Năm ban hành lấy từ dòng ngày của CHÍNH trang chứa dấu mật (file ghép
    nhiều văn bản), so với năm hiện tại theo độ mật (Mật 10, Tối mật 20,
    Tuyệt mật 30 năm). Không suy được năm ban hành thì bỏ qua.

    Lấy năm theo cascade: regex chọn dòng ngắn gần đầu trang (miễn phí);
    nếu trang có nhiều hơn 2 dòng ngày ứng viên (nhiễu) thì gọi KIE
    (LayoutLM, nhãn PLACE_DATE) chọn đúng dòng — chỉ trên các file đã
    xác định là mật.
    """
    if not matches:
        return
    current_year = time.localtime().tm_year
    kie_enabled = os.environ.get(
        "SECRET_SCAN_DISABLE_KIE_YEAR", ""
    ).lower() not in {"1", "true"}
    year_by_page: dict[int, int] = {}
    for match in matches:
        page_idx = max(0, int(match.page_number or 1) - 1)
        if page_idx not in year_by_page:
            page = _page_for_index(canonical, page_idx)
            candidates = _issue_date_candidates(page) if page else []
            year = candidates[0][1] if candidates else 0
            if (
                kie_enabled
                and canonical_json_path
                and len(candidates) >= _DECLASS_KIE_MIN_CANDIDATES
            ):
                # Nhiều hơn 2 dòng ngày ứng viên → regex không đủ tin cậy,
                # nhờ KIE gán nhãn PLACE_DATE chọn đúng dòng ngày ban hành.
                kie_year = _kie_issue_year(canonical_json_path, page_idx)
                if kie_year is not None:
                    year = kie_year
            year_by_page[page_idx] = year
        year = year_by_page[page_idx]
        if not 1900 <= year <= current_year:
            continue
        years = DECLASS_YEARS_BY_KEYWORD.get((match.keyword or "").strip().upper())
        if not years:
            continue
        match.issue_year = year
        match.declass_years = years
        if current_year - year >= years:
            match.declass_due = True
            detail = (
                f"{DECLASS_NOTE_LABEL} "
                f"(văn bản {year}, {match.keyword} {years} năm)"
            )
            match.note = f"{match.note}; {detail}" if match.note else detail


# File "mới" hơn nảy giây → có thể đang được copy/ghi dở trên share:
# không kết luận vĩnh viễn (skip) mà ghi Lỗi để lượt resume sau thử lại.
_SCAN_FRESH_FILE_GRACE_SEC = 120


def _precheck_source_readable(source_path: str) -> None:
    """Chặn trước các file không bao giờ quét được, phân loại 2 nhánh:

    - **Lỗi tạm thời** → để ngoại lệ gốc bọt lên (không đổi thành _ScanSkip):
      process_one ghi trạng thái "err" → resume THỬ LẠI. Gồm: OSError
      (mất quyền/mạng/đang dời), file biến mất giữa chừng, và mọi kết luận
      hỏng trên file còn "mới" (mtime trong grace window — có thể đang copy dở).
    - **Rác vĩnh viễn** → raise _ScanSkip: ghi trạng thái "skip" — resume
      bỏ qua luôn, không thử lại, không đếm là Lỗi. Gồm: file cũ 0 byte,
      nội dung không phải PDF (FileDataError, kiểu file rác "._*" macOS),
      PDF 0 trang (file cũ).

    PDF thật dù tên có dạng nào (kể cả "._...") vẫn mở được ở đây vì fitz
    mở theo nội dung, không theo tên.
    """
    try:
        st = os.stat(source_path)
    except OSError:
        raise
    fresh = (time.time() - st.st_mtime) < _SCAN_FRESH_FILE_GRACE_SEC

    def verdict(reason: str, exc: BaseException | None = None) -> None:
        if fresh:
            raise RuntimeError(f"File mới/chưa ổn định, thử lại lần sau: {reason}") from exc
        raise _ScanSkip(reason) from exc

    if st.st_size == 0:
        verdict("File rỗng (0 byte)")
    ext = os.path.splitext(source_path)[1].lower()
    if ext != ".pdf":
        return

    # Lớp chặn thứ nhất bằng sniffing thuần Python: PDF (theo spec) phải có
    # header %PDF trong 1KB đầu. File rác không có header (kiểu AppleDouble
    # "._*" của macOS) bị loại NGAY TẠI ĐÂY — không bao giờ chạm vào MuPDF,
    # tránh cả đường lỗi lẫn race đa luồng trong MuPDF.
    try:
        with open(source_path, "rb") as fh:
            head = fh.read(1024)
    except OSError:
        raise
    if b"%PDF" not in head:
        verdict("Không phải PDF hợp lệ (thiếu header %PDF)")

    # Lớp chặn thứ hai: fitz.open thật (bắt lỗi nội dung hỏng dạng PDF-truncated,
    # đếm trang) — LUÔN giữ _FITZ_LOCK: MuPDF single-context, 2 thread đồng
    # thời mở file (đặc biệt file lỗi) làm crash process (đã tái hiện).
    import fitz

    with _FITZ_LOCK:
        try:
            doc = fitz.open(source_path)
        except FileNotFoundError:
            # Biến mất giữa chừng (đang dời/đổi tên) → tạm thời, thử lại.
            raise
        except fitz.EmptyFileError as exc:
            verdict("File rỗng (0 byte)", exc)
        except fitz.FileDataError as exc:
            verdict(f"Không phải PDF hợp lệ (nội dung hỏng): {exc}", exc)
        with doc:
            if len(doc) == 0:
                verdict("PDF không có trang")


def _file_worker_count(total: int) -> int:
    """Số thread xử lý file song song (mặc định 2, clamp 1..total).

    Mỗi thread tự mở PDF riêng bằng PyMuPDF trong cùng process (Document
    không chia sẻ chéo thread). Stress-test trên wheel 1.26.7 ổn định,
    nhưng nếu cần loại trừ nghi vấn xung đột PyMuPDF đa luồng (vd máy
    đang crash 0xc0000409), đặt biến môi trường
    SECRET_SCAN_MAX_FILE_WORKERS=1 — scan chạy tuần hoàn toàn, chậm hơn
    nhưng không còn fitz đa luồng nào.
    """
    try:
        want = int(os.environ.get("SECRET_SCAN_MAX_FILE_WORKERS", "2"))
    except ValueError:
        want = 2
    return max(1, min(want, total if total > 0 else 1))


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
      - PDF: per-file classify digital/scan first. Scan files are
        rotation-corrected (classifier 4 hướng của Số hóa lưu trữ) BEFORE
        OCR — digital files skip this (never rotated). Each page is then
        classified (digital/scan/mixed); digital pages use native text,
        scan/mixed pages are OCR'd at ``_SECRET_SCAN_DPI`` (200). Detection
        assumes upright pages: stamps live in the top-left corner, textual
        "Số .../MẬT" markers are accepted anywhere.

    Modes:
      - ``first_page_only=True``  ("Tìm nhanh"): preprocess hướng trang trang
        đầu, OCR trang đầu, detect the secrecy mark on it, no LightGBM.
      - ``first_page_only=False`` ("Tìm kỹ"): preprocess hướng trang toàn
        file, process ALL pages, detect secrecy on every page (cheap token
        match), then run LightGBM doc-start only on the candidate pages to
        filter false positives. For non-classified files (the common case)
        there are no candidates, so LightGBM is skipped.
    """
    os.makedirs(file_work_dir, exist_ok=True)
    _precheck_source_readable(source_path)
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
        _apply_declassification_notes(
            matches, native_canonical, canonical_json_path
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

    with _FITZ_LOCK:
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

    # Xoay đúng chiều TRƯỚC khi OCR/dò (cùng cơ chế classifier 4 hướng của
    # Số hóa lưu trữ): sau khi trang đã đứng thẳng, dấu mật thật chỉ tồn
    # tại ở góc trên-trái — mọi hit ngoài vùng đó bị coi là mảnh mộc/dấu
    # tròn khác. PDF digital bỏ qua (không bao giờ xoay sai, lại tránh copy
    # thừa). Tắt bằng SECRET_SCAN_DISABLE_ROTATE=1 khi cần tốc độ tối đa.
    rotations: list[int] | None = None
    if os.environ.get("SECRET_SCAN_DISABLE_ROTATE", "").lower() not in {"1", "true"}:
        try:
            from scanindex.core.preprocessing import preprocessing as _pre
            file_kind = _pre.classify_pdf(scan_pdf)
        except Exception:
            file_kind = "scan"
        if file_kind != "digital":
            log_cb("Xoay đúng chiều trang trước khi dò (như Số hóa lưu trữ)...")
            pre_pdf = os.path.join(file_work_dir, "preprocessed.pdf")
            pre_out, rotations = _preprocess_pdf_for_ocr(
                scan_pdf,
                pre_pdf,
                log_cb,
                max_workers=ocr_workers,
            )
            if os.path.abspath(pre_out) != os.path.abspath(scan_pdf):
                scan_pdf = pre_out
                source_note = f"{source_note}; đã xoay đúng chiều"

    canonical = _process_pdf_per_page(
        scan_pdf,
        source_path,
        page_indices,
        cancel_event,
        log_cb,
        dpi=_SECRET_SCAN_DPI,
        json_path=canonical_json_path,
    )

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
        _apply_declassification_notes(matches, canonical, canonical_json_path)
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
    _apply_declassification_notes(matches, canonical, canonical_json_path)
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


EXCEL_HEADERS = [
    "STT",
    "Độ mật",
    "Tên tệp",
    "File (trong thư mục quét)",
    "Đường dẫn đầy đủ",
    "Trang",
    "Chế độ",
    "Ghi chú",
]

_EXCEL_COL_WIDTHS = [6, 12, 30, 42, 62, 8, 12, 42]


def export_matches_to_excel(matches: list[SecretScanMatch], output_path: str) -> str:
    """Write the scan results to a one-sheet .xlsx workbook.

    One row per detected stamp — the same rows shown in the results table —
    plus the absolute path and bare file name so the list stays usable when
    shared outside the app.
    """
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Van ban mat"

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="C0392B")
    center = Alignment(horizontal="center", vertical="center")
    for col, name in enumerate(EXCEL_HEADERS, start=1):
        cell = ws.cell(row=1, column=col, value=name)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = center

    for idx, match in enumerate(matches, start=1):
        row = idx + 1
        ws.cell(row=row, column=1, value=idx).alignment = center
        ws.cell(row=row, column=2, value=match.keyword)
        ws.cell(row=row, column=3, value=os.path.basename(match.source_path))
        ws.cell(row=row, column=4, value=match.relative_path)
        ws.cell(row=row, column=5, value=match.source_path)
        ws.cell(row=row, column=6, value=int(match.page_number)).alignment = center
        ws.cell(row=row, column=7, value=match.mode)
        note_cell = ws.cell(row=row, column=8, value=match.note)
        if _match_meets_declassification(match):
            note_cell.font = Font(color="1D7A34", bold=True)

    for col, width in enumerate(_EXCEL_COL_WIDTHS, start=1):
        ws.column_dimensions[get_column_letter(col)].width = width
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:H{max(1, len(matches) + 1)}"

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    wb.save(output_path)
    return output_path


def _norm(path: str) -> str:
    """Chuẩn hóa key so sánh đường dẫn (cột checkbox / xóa theo file)."""
    return os.path.normpath(os.path.abspath(path))


def _permanent_delete(path: str) -> None:
    """Xóa vĩnh viễn MỘT file: không qua thùng rác, gỡ read-only, chịu
    đường dẫn dài (>260 ký tự) qua tiền tố \\\\?\\ ."""
    p = os.path.abspath(path)
    long_p = "\\\\?\\" + p
    if not os.path.exists(p) and not os.path.exists(long_p):
        raise FileNotFoundError("file không còn tồn tại trên đĩa")
    try:
        os.remove(p)
        return
    except PermissionError:
        # Windows: file read-only → gỡ thuộc tính rồi thử lại.
        try:
            os.chmod(p, stat.S_IWRITE)
        except OSError:
            pass
    except OSError:
        pass
    # Lần hai: sau khi gỡ read-only, hoặc qua \\?\ cho đường dẫn dài.
    try:
        os.remove(p)
    except OSError:
        os.remove(long_p)


def _preview_cache_dir() -> str:
    d = os.path.join(get_base_dir(), "temp", "secret_scan_preview")
    os.makedirs(d, exist_ok=True)
    return d


def _ensure_check_mark_png() -> str:
    """Tạo file PNG dấu ✓ trắng (runtime) cho QSS ``::indicator:checked``.

    QSS thuần không vẽ được glyph; dùng image: url() với PNG tự sinh bằng
    QPainter để ô check hiển thị DẤU CHECK thay vì chỉ đổ đầy màu ô.
    """
    from PySide6.QtGui import QImage, QPen

    out_dir = os.path.join(get_base_dir(), "temp", "secret_scan_ui")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "check_mark.png")
    size = 32
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    painter = QPainter(img)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor("#ffffff"), 5)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.drawLine(8, 17, 14, 23)
    painter.drawLine(14, 23, 24, 10)
    painter.end()
    img.save(out, "PNG")
    return out


def _derive_scan_root(matches: list[SecretScanMatch]) -> str:
    """Suy thư mục quét gốc từ dòng đầu của danh sách đã xuất:
    "Đường dẫn đầy đủ" = gốc + sep + "File (trong thư mục quét)".
    Trả "" nếu không suy được (thiếu cột quan hệ / đường dẫn ngoài thư mục).
    """
    for m in matches:
        full = os.path.normpath(m.source_path)
        rel = os.path.normpath(m.relative_path or "")
        if rel and full != rel and full.lower().endswith("\\" + rel.lower()):
            return full[: len(full) - len(rel) - 1]
        return ""
    return ""


def load_secret_matches_from_excel(xlsx_path: str) -> list[SecretScanMatch]:
    """Đọc lại danh sách do ``export_matches_to_excel`` ghi ra.

    Chấp nhận thiếu cột "Chế độ"/"Ghi chú" (đặt giá trị mặc định), nhưng
    bắt buộc có "STT" + "Đường dẫn đầy đủ" để tránh nạp nhầm file Excel
    khác. Trả về danh sách match theo đúng thứ tự trong file.
    """
    import openpyxl

    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    try:
        ws = wb.active
        rows = ws.iter_rows(values_only=True)
        header = next(rows, None)
        if not header:
            raise ValueError("File rỗng.")
        names = [str(h).strip() if h is not None else "" for h in header]
        if "STT" not in names or "Đường dẫn đầy đủ" not in names:
            raise ValueError(
                "File không đúng định dạng danh sách văn bản mật "
                "(cần cột 'STT' và 'Đường dẫn đầy đủ')."
            )

        def col(name: str, default: int | None = None) -> int | None:
            return names.index(name) if name in names else default

        c_path = names.index("Đường dẫn đầy đủ")
        c_key = col("Độ mật", 1)
        c_rel = col("File (trong thư mục quét)")
        c_page = col("Trang", 5)
        c_mode = col("Chế độ", 6)
        c_note = col("Ghi chú", 7)

        def cell(row_values, idx):
            if idx is None or idx >= len(row_values):
                return None
            return row_values[idx]

        matches: list[SecretScanMatch] = []
        for r in rows:
            if r is None:
                continue
            raw_path = cell(r, c_path)
            if raw_path is None or not str(raw_path).strip():
                continue
            source = str(raw_path).strip()
            try:
                page = int(float(cell(r, c_page) or 1))
            except (TypeError, ValueError):
                page = 1
            rel = str(cell(r, c_rel) or source).strip() or source
            matches.append(
                SecretScanMatch(
                    source_path=source,
                    relative_path=rel,
                    keyword=str(cell(r, c_key) or "").strip(),
                    page_number=max(1, page),
                    mode=str(cell(r, c_mode) or "load").strip() or "load",
                    ocr_pdf_path="",
                    note=str(cell(r, c_note) or "").strip(),
                )
            )
        return matches
    finally:
        wb.close()


def _split_pending(
    files: list[str],
    folder: str,
    done_rel: set[str],
) -> tuple[list[tuple[int, str]], int]:
    """Split the folder listing into (pending, skipped) for a resumed scan.

    ``files`` keeps its enumerate-from-1 index so progress display and the
    per-file workdir naming stay stable; a file whose relative path is in
    ``done_rel`` is counted as skipped instead of queued.
    """
    pending: list[tuple[int, str]] = []
    skipped = 0
    for idx, path in enumerate(files, start=1):
        if os.path.relpath(path, folder) in done_rel:
            skipped += 1
        else:
            pending.append((idx, path))
    return pending, skipped


class _BusySpinner(QWidget):
    """Icon "đang tải" xoay — hiển thị trong vùng xem trước khi convert."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._angle = 0
        self._timer = QTimer(self)
        self._timer.setInterval(70)
        self._timer.timeout.connect(self._tick)
        self.setFixedSize(44, 44)

    def start(self) -> None:
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()
        self.update()

    def _tick(self) -> None:
        self._angle = (self._angle + 30) % 360
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        center_x = self.width() / 2
        center_y = self.height() / 2
        for i in range(12):
            angle = math.radians((self._angle + i * 30) % 360)
            alpha = int(255 * (i + 1) / 12)
            dot = QColor(235, 235, 235, alpha)
            painter.setPen(dot)
            painter.setBrush(dot)
            x = center_x + 17 * math.cos(angle) - 2
            y = center_y + 17 * math.sin(angle) - 2
            painter.drawEllipse(int(x), int(y), 4, 4)
        painter.end()


class _ResultsTable(QTableWidget):
    """Bảng kết quả phát thêm tín hiệu điều hướng bàn phím.

    Click chuột đã có itemClicked xử lý (bấm cột checkbox không đổi preview);
    phím mũi tên / PageUp / PageDown / Home / End đổi dòng chọn mà KHÔNG phát
    itemClicked — phát signal riêng để màn hình đồng bộ panel xem trước với
    dòng đang chọn.
    """

    keyboard_row_activated = Signal(int)

    _NAV_KEYS = frozenset({
        Qt.Key.Key_Up,
        Qt.Key.Key_Down,
        Qt.Key.Key_PageUp,
        Qt.Key.Key_PageDown,
        Qt.Key.Key_Home,
        Qt.Key.Key_End,
    })

    def keyPressEvent(self, event) -> None:
        super().keyPressEvent(event)
        if event.key() in self._NAV_KEYS:
            row = self.currentRow()
            if 0 <= row < self.rowCount():
                self.keyboard_row_activated.emit(row)


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
    # Kết quả convert preview từ thread nền: dict {gen, key, out_pdf, error, note}
    _preview_ready = Signal(object)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setStyleSheet(f"background: {COLOR_BG};")
        self._busy = False
        self._cancel_event = threading.Event()
        self._pool_warm_started = False
        self._pool_ready_flag = False
        # Dấu "không phải mật" tồn tại qua các phiên app: load 1 lần, dùng chung.
        self._not_secret = ssp.NotSecretMarks.load()
        self._loaded_from: str | None = None
        self._checked_paths: set[str] = set()
        self._preview_current: SecretScanMatch | None = None
        self._preview_convert_gen = 0
        self._preview_cache: dict[tuple, str] = {}
        # PDF tạm cho preview chỉ là cache phiên — xoá sạch của lần chạy trước.
        shutil.rmtree(
            os.path.join(get_base_dir(), "temp", "secret_scan_preview"),
            ignore_errors=True,
        )
        self._build_ui()
        self._status_changed.connect(self._set_status)
        self._progress_changed.connect(self._set_progress)
        self._result_found.connect(self._add_result)
        self._scan_finished.connect(self._on_finished)
        self._pool_ready.connect(self._on_pool_ready)
        self._preview_ready.connect(self._on_preview_ready)

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

        self.btn_browse = QPushButton("📁  Chọn thư mục")
        self.btn_browse.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_browse.setStyleSheet(self._secondary_btn_qss())
        self.btn_browse.clicked.connect(self._browse_folder)
        row.addWidget(self.btn_browse)

        self.btn_load_file = QPushButton("📊  Load từ file")
        self.btn_load_file.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_load_file.setStyleSheet(self._secondary_btn_qss())
        self.btn_load_file.setToolTip(
            "Nạp lại danh sách đã xuất ra file Excel (.xlsx) để tiếp tục xem,\n"
            "xóa file hoặc xác nhận \"không phải mật\", rồi xuất ra file MỚI\n"
            "(file đang load không bao giờ bị ghi đè)."
        )
        self.btn_load_file.clicked.connect(self._load_from_file_clicked)
        row.addWidget(self.btn_load_file)

        self.btn_run = QPushButton("Bắt đầu quét")
        self.btn_run.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_run.setStyleSheet(self._primary_btn_qss())
        self.btn_run.clicked.connect(self._run_clicked)
        row.addWidget(self.btn_run)

        self.btn_export = QPushButton("📊  Xuất Excel")
        self.btn_export.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_export.setStyleSheet(self._secondary_btn_qss())
        self.btn_export.setEnabled(False)
        self.btn_export.setToolTip("Xuất danh sách văn bản mật đang hiển thị ra file Excel (.xlsx)")
        self.btn_export.clicked.connect(self._export_clicked)
        row.addWidget(self.btn_export)

        self.btn_stop = QPushButton("Dừng")
        self.btn_stop.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_stop.setStyleSheet(self._danger_btn_qss())
        self.btn_stop.clicked.connect(self._stop_clicked)
        self.btn_stop.setVisible(False)
        row.addWidget(self.btn_stop)

        self.btn_clear_history = QPushButton("Xóa lịch sử quét")
        self.btn_clear_history.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_clear_history.setStyleSheet(self._secondary_btn_qss())
        self.btn_clear_history.setToolTip(
            "Xóa toàn bộ tiến độ quét dở và lịch sử \"đã quét\" — lượt quét sau "
            "sẽ quét lại mọi file từ đầu."
        )
        self.btn_clear_history.clicked.connect(self._clear_history_clicked)
        row.addWidget(self.btn_clear_history)
        picker_layout.addLayout(row)

        # Hàng 2 (gọn): trạng thái + tiến độ + tuỳ chọn quét — đúng theo
        # mockup: bớt một hàng riêng cho checkbox, tiết kiệm chiều cao.
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

        self.history_checkbox = QCheckBox("Tận dụng kết quả đã quét")
        self.history_checkbox.setChecked(True)
        self.history_checkbox.setCursor(Qt.CursorShape.PointingHandCursor)
        self.history_checkbox.setStyleSheet(
            f"QCheckBox {{ color: {COLOR_TEXT}; font: 13px '{FONT_UI}';"
            f" padding: 4px 8px; }}"
            f"QCheckBox::indicator {{ width: 16px; height: 16px; }}"
        )
        self.history_checkbox.setToolTip(
            "Bật: file đã quét thành công mà KHÔNG đổi (kích thước, ngày sửa\n"
            "giữ nguyên) sẽ được bỏ qua ở mọi lượt quét sau — kể cả khi quét\n"
            "thư mục cha chứa nó, hay quét lại sau khi quét dở. File đổi hoặc\n"
            "mới luôn được quét. Nâng cấp phần mềm sẽ tự quét lại toàn bộ."
        )
        status_row.addWidget(self.history_checkbox)

        self.fast_checkbox = QCheckBox("Tìm nhanh (trang đầu)")
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
        status_row.addWidget(self.fast_checkbox)
        picker_layout.addLayout(status_row)
        layout.addWidget(picker)

        self.table = _ResultsTable(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["", "Độ mật", "File", "Trang", "Chế độ", "Ghi chú"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.itemDoubleClicked.connect(self._open_result_file)
        # Click thường (không vào cột checkbox) → xem trước bên phải;
        # double-click vẫn mở file bằng app ngoài như trước.
        self.table.itemClicked.connect(self._on_row_clicked)
        self.table.itemChanged.connect(self._on_item_changed)
        # Phím mũi tên / PageUp / Home… đổi dòng chọn → preview theo file đó.
        self.table.keyboard_row_activated.connect(self._show_preview)
        header = self.table.horizontalHeader()
        # Cột Interactive: kéo rộng/hẹp từng cột bằng chuột (để thấy được tên
        # file dài); cột cuối (Ghi chú) tự chiếm phần bề ngang còn lại.
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        header.setMinimumSectionSize(28)
        self.table.setWordWrap(False)
        for col, width in ((0, 30), (1, 90), (2, 330), (3, 64), (4, 100)):
            self.table.setColumnWidth(col, width)
        self.table.setStyleSheet(
            f"QTableWidget {{ background-color: {COLOR_SURFACE}; color: {COLOR_TEXT};"
            f" border: 1px solid {COLOR_BORDER}; border-radius: {RADIUS_MD}px;"
            f" gridline-color: #555555; }}"
            f"QHeaderView::section {{ background-color: {COLOR_PANEL}; color: {COLOR_TEXT};"
            f" border: 1px solid {COLOR_BORDER}; padding: 6px;"
            f" font-size: 12px; font-weight: 600; font-family: '{FONT_UI}'; }}"
            f"QTableWidget::item:selected {{ background-color: {COLOR_ACCENT}; }}"
            # Ô check có viền nổi ở cả hai theme (mặc định gần như vô hình);
            # khi check: nền accent + dấu ✓ trắng (PNG tự sinh ở runtime).
            f"QTableWidget::indicator {{ width: 15px; height: 15px;"
            f" background: {COLOR_INPUT}; border: 1px solid #9a9a9a;"
            f" border-radius: 3px; }}"
            f"QTableWidget::indicator:checked {{ background: {COLOR_ACCENT};"
            f" border: 1px solid white;"
            f' image: url("{_ensure_check_mark_png().replace(os.sep, "/")}"); }}'
        )

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(SP[2])

        results_header = QHBoxLayout()
        self.lbl_total = QLabel("Tổng: 0 văn bản mật")
        self.lbl_total.setStyleSheet(
            f"color: {COLOR_TEXT}; font: 600 13px '{FONT_UI}';"
        )
        results_header.addWidget(self.lbl_total)
        results_header.addStretch(1)
        self.btn_batch_not_secret = QPushButton("Không phải mật (đã chọn)")
        self.btn_batch_not_secret.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_batch_not_secret.setStyleSheet(self._success_btn_qss())
        self.btn_batch_not_secret.setEnabled(False)
        self.btn_batch_not_secret.setToolTip(
            "Loại các file đã check (theo file, không theo từng dòng) khỏi danh sách.\n"
            "KHÔNG xóa file trên đĩa. Các lượt quét sau sẽ tự bỏ qua chúng\n"
            "cho đến khi file thay đổi nội dung."
        )
        self.btn_batch_not_secret.clicked.connect(self._batch_not_secret_clicked)
        results_header.addWidget(self.btn_batch_not_secret)
        self.btn_batch_delete = QPushButton("🗑  Xóa file (đã chọn)")
        self.btn_batch_delete.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_batch_delete.setStyleSheet(self._danger_btn_qss())
        self.btn_batch_delete.setEnabled(False)
        self.btn_batch_delete.setToolTip(
            "Xóa vĩnh viễn các file đã check khỏi đĩa — KHÔNG qua thùng rác,\n"
            "không thể hoàn tác."
        )
        self.btn_batch_delete.clicked.connect(self._batch_delete_clicked)
        results_header.addWidget(self.btn_batch_delete)
        left_layout.addLayout(results_header)
        left_layout.addWidget(self.table, 1)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(self._build_preview_panel())
        splitter.setCollapsible(0, False)
        splitter.setCollapsible(1, False)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([760, 440])
        # Tay nắm 8px + đổi màu khi rê chuột: kéo tự do đổi tỷ lệ
        # bảng danh sách / khung xem trước bằng chuột.
        splitter.setHandleWidth(8)
        splitter.setStyleSheet(
            f"QSplitter::handle {{ background: {COLOR_BORDER}; border-radius: 2px; }}"
            f"QSplitter::handle:hover {{ background: {COLOR_ACCENT}; }}"
        )
        layout.addWidget(splitter, 1)

    def _build_preview_panel(self) -> QWidget:
        panel = QFrame()
        panel.setStyleSheet(
            f"QFrame {{ background: {COLOR_PANEL}; border: 1px solid {COLOR_BORDER};"
            f" border-radius: {RADIUS_MD}px; }}"
        )
        vbox = QVBoxLayout(panel)
        vbox.setContentsMargins(SP[3], SP[3], SP[3], SP[3])
        vbox.setSpacing(SP[2])

        self.lbl_preview_path = QLabel("")
        self.lbl_preview_path.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; font: 12px '{FONT_UI}';"
        )
        self.lbl_preview_path.setWordWrap(True)
        vbox.addWidget(self.lbl_preview_path)

        self.lbl_preview_banner = QLabel("")
        self.lbl_preview_banner.setWordWrap(True)
        self.lbl_preview_banner.setVisible(False)
        vbox.addWidget(self.lbl_preview_banner)

        self.preview_stack = QStackedWidget()
        self.lbl_preview_empty = QLabel(
            "Chọn một dòng trong bảng để xem trước nội dung file."
        )
        self.lbl_preview_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_preview_empty.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; font: 13px '{FONT_UI}';"
        )
        self.lbl_preview_empty.setWordWrap(True)
        self.preview_stack.addWidget(self.lbl_preview_empty)  # trang 0
        self.pdf_viewer = PdfViewerWidget()
        self.preview_stack.addWidget(self.pdf_viewer)  # trang 1

        # Nút thao tác trên file đang xem đặt NGAY CẠNH thanh điều hướng trang
        # của khung xem trước (thay vì chiếm một hàng header riêng).
        self.btn_preview_not_secret = QPushButton("Không phải mật")
        self.btn_preview_not_secret.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_preview_not_secret.setStyleSheet(
            self._preview_action_btn_qss(COLOR_GREEN, COLOR_GREEN_HOVER)
        )
        self.btn_preview_not_secret.setEnabled(False)
        self.btn_preview_not_secret.setToolTip(
            "Xác nhận file đang xem là KHÔNG phải văn bản mật:\n"
            "loại khỏi danh sách, KHÔNG xóa file trên đĩa, lượt quét sau\n"
            "tự bỏ qua (đến khi file thay đổi nội dung)."
        )
        self.btn_preview_not_secret.clicked.connect(self._preview_not_secret_clicked)
        self.pdf_viewer.add_toolbar_widget(self.btn_preview_not_secret)
        self.btn_preview_delete = QPushButton("🗑  Xóa file")
        self.btn_preview_delete.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_preview_delete.setStyleSheet(
            self._preview_action_btn_qss(COLOR_RED, "#bd2130")
        )
        self.btn_preview_delete.setEnabled(False)
        self.btn_preview_delete.setToolTip(
            "Xóa vĩnh viễn file đang xem khỏi đĩa — KHÔNG qua thùng rác,\n"
            "không thể hoàn tác. Tự chuyển sang dòng kế sau khi xóa."
        )
        self.btn_preview_delete.clicked.connect(self._preview_delete_clicked)
        self.pdf_viewer.add_toolbar_widget(self.btn_preview_delete)

        # Lớp phủ "đang chuyển đổi" đè lên đúng vùng xem PDF: nền tối mờ +
        # icon xoay + dòng chữ. Màu tự thân (không theo theme sáng/tối) để
        # luôn tương phản: theme sáng từng làm chữ COLOR_TEXT đen-on-đen.
        self.preview_overlay = QFrame()
        self.preview_overlay.setObjectName("previewBusyOverlay")
        self.preview_overlay.setStyleSheet(
            "QFrame#previewBusyOverlay { background: rgba(20, 20, 20, 170); }"
        )
        overlay_layout = QVBoxLayout(self.preview_overlay)
        overlay_layout.addStretch(1)
        self.preview_spinner = _BusySpinner()
        overlay_layout.addWidget(
            self.preview_spinner, 0, Qt.AlignmentFlag.AlignHCenter
        )
        self.lbl_preview_busy = QLabel(
            translations.localize_text(
                "Đang chuyển đổi để xem trước, vui lòng chờ..."
            )
        )
        self.lbl_preview_busy.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.lbl_preview_busy.setStyleSheet(
            f"QLabel {{ color: #ffffff; font: 14px '{FONT_UI}';"
            f" background: transparent; }}"
        )
        overlay_layout.addWidget(self.lbl_preview_busy)
        overlay_layout.addStretch(1)
        self.preview_overlay.setVisible(False)

        stack_wrap = QWidget()
        stack_grid = QGridLayout(stack_wrap)
        stack_grid.setContentsMargins(0, 0, 0, 0)
        stack_grid.setSpacing(0)
        stack_grid.addWidget(self.preview_stack, 0, 0)
        stack_grid.addWidget(self.preview_overlay, 0, 0)
        vbox.addWidget(stack_wrap, 1)
        return panel

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
            "QPushButton:disabled { background: #555; color: #aaa; }"
        )

    def _success_btn_qss(self) -> str:
        return (
            f"QPushButton {{ background: {COLOR_GREEN}; color: white;"
            f" border: none; padding: 8px 16px; border-radius: {RADIUS_MD}px;"
            f" font: 600 13px '{FONT_UI}'; }}"
            f"QPushButton:hover {{ background: {COLOR_GREEN_HOVER}; }}"
            f"QPushButton:disabled {{ background: #555; color: #aaa; }}"
        )

    def _preview_action_btn_qss(self, bg: str, hover: str) -> str:
        """Nút gọn trong thanh công cụ 32px của khung xem trước PDF."""
        return (
            f"QPushButton {{ background: {bg}; color: white; border: none;"
            f" padding: 0 10px; border-radius: 4px;"
            f" font: 600 12px '{FONT_UI}'; min-height: 22px; max-height: 22px; }}"
            f"QPushButton:hover {{ background: {hover}; }}"
            f"QPushButton:disabled {{ background: #555; color: #aaa; }}"
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

        first_page_only = self.fast_checkbox.isChecked()
        resume_prog = self._offer_resume(folder, first_page_only)

        if (
            resume_prog is None
            and self._loaded_from
            and self.table.rowCount() > 0
        ):
            answer = QMessageBox.question(
                self,
                "Thay danh sách đang load?",
                (
                    "Danh sách đang hiển thị được load từ file Excel.\n"
                    "Bắt đầu quét mới sẽ thay nó — các chỉnh sửa chưa xuất\n"
                    "ra Excel sẽ mất. Tiếp tục?"
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        self._busy = True
        self._cancel_event.clear()
        self._reset_results()
        if resume_prog is not None:
            # Khôi phục các dòng mật đã phát hiện ở lượt trước lên bảng.
            for match_dict in resume_prog.matches:
                try:
                    self._add_result(SecretScanMatch(**match_dict))
                except (TypeError, ValueError):
                    continue
        self._set_running_ui(True)
        self._set_status("Đang chuẩn bị...")
        self.progress.setValue(0)
        self.progress.setMaximum(1)

        thread = threading.Thread(
            target=self._run_worker,
            args=(folder, first_page_only, resume_prog),
            daemon=True,
            name="secret-file-scan",
        )
        thread.start()

    def _offer_resume(
        self, folder: str, first_page_only: bool
    ) -> "ssp.SecretScanProgress | None":
        """Detect an unfinished scan for this folder+mode and ask the user.

        Returns the progress object to resume from, or None for a fresh
        scan. Also prunes abandoned journals older than 30 days.
        """
        try:
            ssp.prune_stale()
        except Exception:
            pass
        mode_key = "fast" if first_page_only else "thorough"
        try:
            prog = ssp.SecretScanProgress.load(folder, mode_key)
        except Exception:
            return None
        if prog is None:
            return None
        done_count, error_count, skip_count, found = prog.stats()
        if done_count == 0 and error_count == 0 and skip_count == 0 and found == 0:
            return None
        answer = QMessageBox.question(
            self,
            "Tiếp tục quét?",
            (
                "Thư mục này có lượt quét chưa hoàn tất:\n"
                f"• Đã quét xong: {done_count} file\n"
                f"• Lỗi lần trước (sẽ thử lại): {error_count} file\n"
                f"• File hỏng (sẽ bỏ qua): {skip_count} file\n"
                f"• Dòng mật đã phát hiện: {found}\n\n"
                "Tiếp tục từ nơi dừng không?\n"
                "Chọn \"No\" để quét lại toàn bộ từ đầu."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        return prog if answer == QMessageBox.StandardButton.Yes else None

    def _stop_clicked(self) -> None:
        self._cancel_event.set()
        self._set_status("Đang dừng...")

    def _set_running_ui(self, running: bool) -> None:
        self.btn_browse.setEnabled(not running)
        self.folder_edit.setEnabled(not running)
        self.fast_checkbox.setEnabled(not running)
        self.history_checkbox.setEnabled(not running)
        self.btn_clear_history.setEnabled(not running)
        self.btn_load_file.setEnabled(not running)
        # Chỉ bật lại nút xuất khi hết bận VÀ đang có kết quả để xuất.
        self.btn_export.setEnabled(not running and self.table.rowCount() > 0)
        self.btn_run.setVisible(not running)
        self.btn_stop.setVisible(running)
        # Xóa file / "không phải mật" cấm chạy trong lúc quét (file đang bị
        # engine đọc); xem trước thì vẫn cho phép.
        self._refresh_action_buttons()

    def _run_worker(
        self,
        folder: str,
        first_page_only: bool,
        resume_prog: "ssp.SecretScanProgress | None" = None,
    ) -> None:
        from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

        started = time.strftime("%Y%m%d_%H%M%S")
        work_root = os.path.join(get_base_dir(), "temp", f"secret_scan_{started}")
        os.makedirs(work_root, exist_ok=True)
        mode_key = "fast" if first_page_only else "thorough"
        # resume_prog tới từ _offer_resume (đã hỏi người dùng); None = quét mới.
        # Journal ghi tiếp theo từng file nên chi phí không đổi dù 1 triệu file.
        prog = resume_prog if resume_prog is not None else ssp.SecretScanProgress.create(
            folder, mode_key
        )

        files = list(_iter_supported_files(folder))
        total = len(files)
        pending, skipped = _split_pending(files, folder, prog.done_files())

        # Sổ file toàn cục: file đã quét thành công mà không đổi (size +
        # mtime + version + cùng chế độ) được bỏ qua ở MỌI lượt quét sau,
        # kể cả khi quét thư mục cha chứa nó.
        use_history = self.history_checkbox.isChecked()
        registry = ssp.FileRegistry.load() if use_history else None
        from scanindex.infra.version import get_version

        app_version = get_version()
        if registry is None:
            self.log_message.emit("Lịch sử quét: TẮT — quét lại toàn bộ", "info")
        cache_skipped = [0]

        if skipped:
            self._progress_changed.emit(skipped, max(1, total))
            self._status_changed.emit(f"Tiếp tục: còn {len(pending)} file cần quét")
            self.log_message.emit(
                f"Tiếp tục quét: bỏ qua {skipped} file đã quét lần trước, "
                f"còn {len(pending)} file cần quét (tổng {total} file)",
                "info",
            )
        else:
            self._progress_changed.emit(0, max(1, total))
            self.log_message.emit(f"Quét file mật: tìm thấy {total} file hỗ trợ", "info")

        # File-level parallelism: each worker thread processes one file. The
        # OCR pool (when used inside _process_pdf_per_page) is a shared global,
        # so scan pages from multiple files are distributed across its workers
        # automatically — this is what makes "2 pages at a time regardless of
        # file" work in Tìm kỹ mode. In Tìm nhanh mode, two files' first pages
        # are OCR'd concurrently.
        max_file_workers = _file_worker_count(total)
        progress_lock = threading.Lock()
        # state_lock: record_*/save trên prog dùng chung phải nguyên tố —
        # hai worker ghi xen kẽ vào cùng file handle sẽ làm hỏng dòng journal.
        state_lock = threading.Lock()
        done = [skipped]
        scanned = [0]
        failures = [0]
        junk = [0]
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
                # Lookup lịch sử TRƯỚC khi quét: file không đổi → dùng lại
                # kết quả cũ, không tốn OCR.
                cached_dicts: list[dict] | None = None
                if registry is not None:
                    try:
                        st = os.stat(path)
                        cached_dicts = registry.lookup(
                            path,
                            st.st_size,
                            st.st_mtime,
                            mode_key,
                            app_version,
                        )
                    except OSError:
                        cached_dicts = None
                if cached_dicts is not None:
                    for match_dict in cached_dicts:
                        try:
                            self._result_found.emit(
                                SecretScanMatch(**match_dict)
                            )
                        except (TypeError, ValueError):
                            continue
                    with state_lock:
                        prog.record_file(rel, "ok")
                        prog.record_matches(cached_dicts)
                        cache_skipped[0] += 1
                    self._status_changed.emit(
                        f"Đã quét trước đây, bỏ qua: {rel}"
                    )
                    return

                matches = scan_one_file_for_secret(
                    path,
                    rel,
                    file_work,
                    first_page_only,
                    self._cancel_event,
                    file_log,
                )
                match_dicts = [asdict(m) for m in matches]
                for match in matches:
                    self._result_found.emit(match)
                with state_lock:
                    prog.record_file(rel, "ok")
                    prog.record_matches(match_dicts)
                    if registry is not None:
                        try:
                            st = os.stat(path)
                            registry.record(
                                path,
                                st.st_size,
                                st.st_mtime,
                                mode_key,
                                app_version,
                                match_dicts,
                            )
                        except OSError:
                            pass
                with progress_lock:
                    scanned[0] += 1
            except _ScanCancelled:
                cancelled[0] = True
            except _ScanSkip as exc:
                with state_lock:
                    prog.record_file(rel, "skip", str(exc))
                with progress_lock:
                    junk[0] += 1
                self.log_message.emit(f"[{rel}] Bỏ qua: {exc}", "info")
            except Exception as exc:
                with state_lock:
                    prog.record_file(rel, "err", str(exc))
                with progress_lock:
                    failures[0] += 1
                self.log_message.emit(f"[{rel}] Lỗi: {exc}", "err")
            finally:
                if os.path.isdir(file_work):
                    shutil.rmtree(file_work, ignore_errors=True)
                with state_lock:
                    prog.save()
                    if registry is not None:
                        registry.save()
                with progress_lock:
                    done[0] += 1
                    self._progress_changed.emit(done[0], max(1, total))
                    periodic_mem = done[0] % 500 == 0
                if periodic_mem:
                    # Đo RAM định kỳ: bằng chứng cho các lần crash native
                    # (0xc0000409) — xem RAM có phình trước lúc chết không.
                    mem = memory_snapshot_text()
                    if mem:
                        app_log_write(f"[mem] {mem}", "info")

        mem = memory_snapshot_text()
        if mem:
            app_log_write(f"[mem] đầu lượt quét: {mem}", "info")
        try:
            with ThreadPoolExecutor(
                max_workers=max_file_workers, thread_name_prefix="secret-scan-file"
            ) as executor:
                # Cửa sổ trượt: chỉ giữ tối đa _SCAN_MAX_INFLIGHT_FILES future
                # thay vì submit toàn bộ `pending` (có thể là 300k+ file) —
                # chặn việc process chính phình RAM chỉ vì hàng đợi.
                futures: set = set()
                next_task = 0

                def submit_window() -> None:
                    nonlocal next_task
                    while (
                        next_task < len(pending)
                        and len(futures) < _SCAN_MAX_INFLIGHT_FILES
                    ):
                        futures.add(
                            executor.submit(process_one, *pending[next_task])
                        )
                        next_task += 1

                submit_window()
                try:
                    while futures and not self._cancel_event.is_set():
                        done_set, futures = wait(futures, timeout=0.2, return_when=FIRST_COMPLETED)
                        for fut in done_set:
                            # Re-raise any exception from the worker (except
                            # _ScanCancelled, which only flags cancellation).
                            try:
                                fut.result()
                            except _ScanCancelled:
                                cancelled[0] = True
                        submit_window()
                except _ScanCancelled:
                    cancelled[0] = True
                if self._cancel_event.is_set():
                    cancelled[0] = True
                    for fut in futures:
                        fut.cancel()
        finally:
            with state_lock:
                done_n, err_n, skip_n, found_n = prog.stats()
                if (
                    (done_n or err_n or skip_n or found_n)
                    and (cancelled[0] or failures[0])
                ):
                    # Chưa xong hẳn (dừng tay hoặc còn file lỗi) → giữ journal
                    # để lần sau chọn lại thư mục này được hỏi tiếp tục;
                    # file lỗi sẽ được thử lại, file hỏng thì không.
                    prog.save()
                else:
                    # Hoàn tất sạch (hoặc không có gì đáng tiếp) → xóa journal.
                    prog.discard()
                # Đóng handle ngay — file mở để lâu sẽ chặn "Xóa lịch sử
                # quét" và backup trên Windows.
                prog.close()
                if registry is not None:
                    registry.close()
            mem = memory_snapshot_text()
            if mem:
                app_log_write(f"[mem] cuối lượt quét: {mem}", "info")
            self._scan_finished.emit(
                {
                    "total": total,
                    "scanned": scanned[0],
                    "skipped": skipped,
                    "cache_skipped": cache_skipped[0],
                    "junk": junk[0],
                    "failures": failures[0],
                    "cancelled": cancelled[0],
                    "work_root": work_root,
                }
            )

    # ------------------------------------------------------------------
    # Xem trước (panel phải)
    # ------------------------------------------------------------------

    def _set_preview_banner(self, text: str, kind: str | None) -> None:
        if not text or not kind:
            self.lbl_preview_banner.setVisible(False)
            self.lbl_preview_banner.setText("")
            return
        if kind == "warn":
            bg, border = "#3a3120", "#b8860b"
        else:
            bg, border = "#1f2c3d", COLOR_BORDER
        self.lbl_preview_banner.setText(text)
        # Chữ trắng cố định: cả hai nền banner đều tối, và COLOR_TEXT ở
        # theme sáng là màu đen → sợ lại ra chữ đen trên nền đen.
        self.lbl_preview_banner.setStyleSheet(
            f"QLabel {{ background: {bg}; border: 1px solid {border};"
            f" border-radius: {RADIUS_MD}px; padding: 6px 8px;"
            f" color: #ffffff; font: 12px '{FONT_UI}'; }}"
        )
        self.lbl_preview_banner.setVisible(True)

    def _set_preview_busy(self, busy: bool) -> None:
        """Bật/tắt lớp phủ icon xoay trên vùng xem trong lúc convert."""
        self.preview_overlay.setVisible(busy)
        if busy:
            self.preview_spinner.start()
        else:
            self.preview_spinner.stop()

    def _show_preview(self, row: int) -> None:
        item = self.table.item(row, 0)
        if item is None:
            return
        match = item.data(Qt.ItemDataRole.UserRole + 1)
        if not isinstance(match, SecretScanMatch):
            return
        self._preview_current = match
        self.lbl_preview_path.setText(match.source_path)
        self.lbl_preview_path.setToolTip(match.source_path)
        self._refresh_action_buttons()
        self._set_preview_busy(False)

        path = match.source_path
        ext = os.path.splitext(path)[1].lower()
        if not os.path.exists(path):
            self.pdf_viewer.clear()
            self.lbl_preview_empty.setText(
                translations.localize_text(
                    f"File không còn tồn tại trên đĩa:\n{path}"
                )
            )
            self.preview_stack.setCurrentIndex(0)
            self._set_preview_banner("", None)
            return
        if ext == ".pdf":
            self._set_preview_banner("", None)
            self.preview_stack.setCurrentIndex(1)
            self.pdf_viewer.show_pdf(
                path, page=max(1, int(match.page_number or 1))
            )
            return
        if ext in {".png", ".jpg", ".jpeg", ".doc", ".docx"}:
            self._preview_converted(path, match)
            return
        self.lbl_preview_empty.setText(
            translations.localize_text(
                f"Không hỗ trợ xem trước định dạng: {ext}"
            )
        )
        self.preview_stack.setCurrentIndex(0)
        self._set_preview_banner("", None)

    def _preview_converted(self, path: str, match: SecretScanMatch) -> None:
        """Ảnh/doc/docx: chuyển sang PDF tạm (cache theo size+mtime) rồi xem.

        Convert chạy trong thread nền với số generation — bấm sang file khác
        thì kết quả cũ bị vứt bỏ. .docx không có Word/LibreOffice rơi vào
        nhánh text fallback (đúng như engine đã quét) và hiện banner cảnh báo.
        """
        try:
            st = os.stat(path)
            key = (_norm(path), int(st.st_size), int(st.st_mtime))
        except OSError:
            return
        cached = self._preview_cache.get(key)
        if cached and os.path.exists(cached):
            self._set_preview_banner("", None)
            self.preview_stack.setCurrentIndex(1)
            self.pdf_viewer.show_pdf(
                cached, page=max(1, int(match.page_number or 1))
            )
            return
        gen = self._preview_convert_gen = self._preview_convert_gen + 1
        digest = hashlib.sha1("|".join(map(str, key)).encode("utf-8")).hexdigest()
        out_pdf = os.path.join(_preview_cache_dir(), f"{digest}.pdf")
        ext = os.path.splitext(path)[1].lower()
        self.preview_stack.setCurrentIndex(1)
        self.pdf_viewer.clear()
        self._set_preview_busy(True)

        def work(
            gen=gen, path=path, out_pdf=out_pdf, key=key, ext=ext
        ) -> None:
            pythoncom = None
            try:
                import pythoncom  # type: ignore

                pythoncom.CoInitialize()  # trả HRESULT — không gán lại biến
            except Exception:
                pythoncom = None
            error = ""
            note = ""
            try:
                if ext in {".png", ".jpg", ".jpeg"}:
                    _image_to_pdf(path, out_pdf)
                    note = "image"
                elif _convert_doc_with_word(
                    path, out_pdf, first_page_only=False
                ) or _convert_doc_with_soffice(path, out_pdf):
                    note = "doc-render"
                elif ext == ".docx":
                    # Thứ tự fallback y hệt _convert_document_to_pdf của
                    # engine: bản text này chính là cái engine đã dùng để
                    # tìm từ khóa khi máy không có Word/LibreOffice.
                    _convert_docx_text_fallback(
                        path, out_pdf, first_page_only=False
                    )
                    note = "doc-text"
                else:
                    raise RuntimeError(
                        "Cần Microsoft Word hoặc LibreOffice để xem trước "
                        "file .doc trên máy này."
                    )
                if not os.path.exists(out_pdf):
                    raise RuntimeError("Không tạo được file PDF tạm")
            except Exception as exc:
                error = str(exc) or type(exc).__name__
            finally:
                if pythoncom is not None:
                    try:
                        pythoncom.CoUninitialize()
                    except Exception:
                        pass
            self._preview_ready.emit(
                {"gen": gen, "key": key, "out_pdf": out_pdf,
                 "error": error, "note": note}
            )

        threading.Thread(
            target=work, daemon=True, name="secret-preview-convert"
        ).start()

    def _on_preview_ready(self, payload: dict) -> None:
        if payload.get("gen") != self._preview_convert_gen:
            return  # người dùng đã bấm sang file khác
        self._set_preview_busy(False)
        if payload.get("error"):
            self._set_preview_banner("", None)
            self.lbl_preview_empty.setText(
                translations.localize_text(
                    f"Không xem trước được file này:\n{payload['error']}"
                )
            )
            self.preview_stack.setCurrentIndex(0)
            return
        if payload.get("key"):
            self._preview_cache[payload["key"]] = payload["out_pdf"]
        if payload.get("note") == "doc-text":
            self._set_preview_banner(
                translations.localize_text(
                    "⚠ Bản xem trước dạng TEXT (trích từ .docx, máy không có "
                    "Word/LibreOffice) — KHÔNG hiển thị ảnh, mộc, con dấu. "
                    "Không căn cứ vào đây để kết luận \"không phải mật\" khi dấu "
                    "mật nghi ở dạng hình."
                ),
                "warn",
            )
        else:
            self._set_preview_banner("", None)
        page = 1
        if self._preview_current is not None:
            page = max(1, int(self._preview_current.page_number or 1))
        self.preview_stack.setCurrentIndex(1)
        self.pdf_viewer.show_pdf(payload["out_pdf"], page=page)

    # ------------------------------------------------------------------
    # Xóa file vĩnh viễn / Không phải mật
    # ------------------------------------------------------------------

    def _batch_delete_clicked(self) -> None:
        self._delete_files(sorted(self._checked_paths))

    def _batch_not_secret_clicked(self) -> None:
        self._mark_not_secret(sorted(self._checked_paths))

    def _preview_delete_clicked(self) -> None:
        if self._busy or self._preview_current is None:
            return
        path = self._preview_current.source_path
        row = self._first_row_of(path)
        self._delete_files([path])
        # Tự chuyển sang dòng kế (hoặc dòng cuối) như đã hứa trong tooltip.
        if row is not None and self.table.rowCount() > 0:
            target = min(row, self.table.rowCount() - 1)
            self._select_result_row(target)
            self._show_preview(target)

    def _preview_not_secret_clicked(self) -> None:
        if self._busy or self._preview_current is None:
            return
        path = self._preview_current.source_path
        row = self._first_row_of(path)
        self._mark_not_secret([path])
        if row is not None and self.table.rowCount() > 0:
            target = min(row, self.table.rowCount() - 1)
            self._select_result_row(target)
            self._show_preview(target)

    def _select_result_row(self, row: int) -> None:
        """Chọn dòng (cột File) và cuộn vào giữa bảng để phím mũi tên
        tiếp tục điều hướng từ đúng vị trí mới."""
        if not 0 <= row < self.table.rowCount():
            return
        self.table.setCurrentCell(row, 2)
        item = self.table.item(row, 2)
        if item is not None:
            self.table.scrollToItem(
                item, QAbstractItemView.ScrollHint.PositionAtCenter
            )

    def _first_row_of(self, path: str) -> int | None:
        key = _norm(path)
        for r in range(self.table.rowCount()):
            if _norm(self._row_path(r)) == key:
                return r
        return None

    def _delete_files(self, paths: list[str]) -> None:
        if self._busy:
            return
        targets = list(dict.fromkeys(p for p in paths if p))
        if not targets:
            return
        if not self._confirm_paths(
            "Xóa vĩnh viễn file?",
            translations.localize_text(
                f"Sẽ XÓA VĨNH VIỄN {len(targets)} file khỏi đĩa:\n"
                "• KHÔNG qua thùng rác — KHÔNG thể hoàn tác.\n"
                "• File đang bị chương trình khác khóa sẽ báo lỗi và giữ nguyên dòng."
            ),
            targets,
            "Xóa vĩnh viễn",
        ):
            return
        deleted = 0
        errors: list[str] = []
        for path in targets:
            # fitz giữ handle file đang xem → phải thả trước khi xóa.
            if (
                self._preview_current is not None
                and _norm(self._preview_current.source_path) == _norm(path)
            ):
                self.pdf_viewer.release_file_handles()
            try:
                _permanent_delete(path)
            except OSError as exc:
                errors.append(f"{path}\n→ {exc}")
            else:
                deleted += 1
                self._remove_file_rows(path)
        if deleted:
            self.log_message.emit(
                f"Quét file mật: đã xóa vĩnh viễn {deleted} file khỏi đĩa.",
                "success",
            )
        if errors:
            self._show_path_errors(
                translations.localize_text(
                    f"Không xóa được {len(errors)}/{len(targets)} file "
                    "(dòng tương ứng được giữ lại):"
                ),
                errors,
            )
            self.log_message.emit(
                f"Quét file mật: {len(errors)} file xóa lỗi — "
                + " | ".join(e.replace("\n", " ") for e in errors),
                "err",
            )
        self._refresh_totals()

    def _mark_not_secret(self, paths: list[str]) -> None:
        if self._busy:
            return
        targets = list(dict.fromkeys(p for p in paths if p))
        if not targets:
            return
        if not self._confirm_paths(
            "Xác nhận không phải văn bản mật?",
            translations.localize_text(
                f"Xác nhận {len(targets)} file dưới đây là KHÔNG phải văn bản mật?\n"
                "• Chỉ loại khỏi danh sách — KHÔNG xóa file trên đĩa.\n"
                "• Các lượt quét sau sẽ tự bỏ qua các file này; đến khi file\n"
                "  thay đổi nội dung thì dòng sẽ hiện lại để xem xét lại."
            ),
            targets,
            "Xác nhận không phải mật",
        ):
            return
        for path in targets:
            self._not_secret.mark(path)
            self._remove_file_rows(path)
        self.log_message.emit(
            f"Quét file mật: xác nhận không phải mật, loại {len(targets)} file "
            "khỏi danh sách (file trên đĩa giữ nguyên).",
            "success",
        )
        self._refresh_totals()

    def _confirm_paths(
        self, title: str, intro: str, paths: list[str], accept_label: str
    ) -> bool:
        """Hộp thoại xác nhận liệt kê đầy đủ đường dẫn (cuộn được khi dài)."""
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(intro)
        listw = QListWidget(box)
        listw.addItems(paths)
        listw.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        listw.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        listw.setWordWrap(True)
        listw.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        listw.setFixedHeight(min(220, 26 * len(paths) + 6))
        grid = box.layout()
        if grid is not None:
            grid.addWidget(listw, 1, 0, 1, grid.columnCount())
        accept = box.addButton(
            accept_label, QMessageBox.ButtonRole.AcceptRole
        )
        box.addButton("Hủy", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        return box.clickedButton() is accept

    def _show_path_errors(self, intro: str, errors: list[str]) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("Có lỗi")
        box.setIcon(QMessageBox.Icon.Critical)
        box.setText(intro)
        detail = QPlainTextEdit(box)
        detail.setPlainText("\n\n".join(errors))
        detail.setReadOnly(True)
        detail.setFixedHeight(min(280, 22 * len(errors) + 12))
        grid = box.layout()
        if grid is not None:
            grid.addWidget(detail, 1, 0, 1, grid.columnCount())
        box.addButton("Đóng", QMessageBox.ButtonRole.AcceptRole)
        box.exec()

    # ------------------------------------------------------------------
    # Load danh sách từ file Excel
    # ------------------------------------------------------------------

    def _load_from_file_clicked(self) -> None:
        if self._busy:
            return
        last_dir = (
            os.path.dirname(self._loaded_from)
            if self._loaded_from
            else ""
        )
        if last_dir and os.path.isdir(last_dir):
            start_dir = last_dir
        else:
            folder = self.folder_edit.text().strip()
            start_dir = (
                folder if folder and os.path.isdir(folder)
                else os.path.expanduser("~")
            )
        path, _ = QFileDialog.getOpenFileName(
            self,
            translations.localize_text(
                "Load danh sách văn bản mật từ file Excel"
            ),
            start_dir,
            translations.localize_text("Excel (*.xlsx)"),
        )
        if not path:
            return
        try:
            matches = load_secret_matches_from_excel(path)
        except Exception as exc:
            QMessageBox.critical(self, "Không đọc được file", f"{path}\n\n{exc}")
            return
        if not matches:
            QMessageBox.information(
                self, "Không có dòng nào",
                "File không chứa dòng văn bản mật nào.",
            )
            return
        if self.table.rowCount() > 0:
            answer = QMessageBox.question(
                self,
                "Thay danh sách hiện tại?",
                (
                    "Danh sách đang hiển thị sẽ bị thay bằng nội dung file "
                    "vừa load.\nCác chỉnh sửa chưa xuất ra Excel sẽ mất. "
                    "Tiếp tục?"
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._reset_results()
        self._loaded_from = os.path.abspath(path)
        # Gán lại thư mục quét gốc (suy từ chính file Excel) để nút
        # "Bắt đầu quét" sau đó chạy trên đúng thư mục đã xuất danh sách.
        scan_root = _derive_scan_root(matches)
        if scan_root:
            self.folder_edit.setText(scan_root)
        missing = 0
        shown = 0
        for m in matches:
            gone = not os.path.exists(m.source_path)
            if gone:
                missing += 1
            before = self.table.rowCount()
            self._add_result(m, missing=gone)
            if self.table.rowCount() > before:
                shown += 1
        skipped = len(matches) - shown
        bits = [f"Đã load {shown} dòng từ: {self._loaded_from}"]
        if missing:
            bits.append(
                f"{missing} dòng có file không còn trên đĩa (hiển thị mờ)"
            )
        if skipped:
            bits.append(
                f"{skipped} dòng bị bỏ qua do đã xác nhận \"không phải mật\""
            )
        self._set_status(" • ".join(bits))
        self.log_message.emit("Quét file mật: " + " - ".join(bits), "info")
        QMessageBox.information(
            self, "Đã load danh sách", "\n\n".join(bits)
        )

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)

    def _set_progress(self, current: int, total: int) -> None:
        self.progress.setMaximum(max(1, total))
        self.progress.setValue(max(0, min(current, max(1, total))))

    def _add_result(self, match: SecretScanMatch, missing: bool = False) -> None:
        # Cửa chặn duy nhất cho CẢ BA nguồn dòng mật (quét mới, resume dở,
        # cache "Tận dụng kết quả đã quét"): file đã được xác nhận "không
        # phải mật" và chưa đổi nội dung thì không bao giờ hiện lại.
        if self._not_secret.is_marked(match.source_path):
            return
        row = self.table.rowCount()
        self.table.insertRow(row)
        check_item = QTableWidgetItem()
        check_item.setFlags(
            Qt.ItemFlag.ItemIsUserCheckable
            | Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
        )
        check_item.setCheckState(Qt.CheckState.Unchecked)
        check_item.setData(Qt.ItemDataRole.UserRole, match.source_path)
        check_item.setData(Qt.ItemDataRole.UserRole + 1, match)
        self.table.setItem(row, 0, check_item)
        values = [
            match.keyword,
            match.relative_path,
            str(match.page_number),
            match.mode,
            match.note,
        ]
        for col, value in enumerate(values, start=1):
            item = QTableWidgetItem(value)
            if col == 4:
                translations.set_translatable_item_text(
                    item, value, sync_tooltip=True
                )
            elif col == 5:
                translations.set_translatable_item_text(
                    item, value, context="secret_note", sync_tooltip=True
                )
            item.setData(Qt.ItemDataRole.UserRole, match.source_path)
            item.setData(Qt.ItemDataRole.UserRole + 1, match)
            if col not in (4, 5):
                item.setToolTip(match.source_path if col == 2 else value)
            if col == 1:
                item.setForeground(QColor(COLOR_RED))
            if missing:
                item.setForeground(QColor(COLOR_TEXT_SECONDARY))
            self.table.setItem(row, col, item)
        if not missing and _match_meets_declassification(match):
            note_item = self.table.item(row, 5)
            if note_item is not None:
                note_item.setForeground(QColor(COLOR_GREEN))
        if missing:
            file_item = self.table.item(row, 2)
            if file_item is not None:
                file_item.setToolTip(
                    translations.localize_text(
                        f"{match.source_path}\n⚠ File không còn tồn tại trên đĩa"
                    )
                )
        self.btn_export.setEnabled(True)
        self._refresh_totals()

    def _row_path(self, row: int) -> str:
        item = self.table.item(row, 0)
        if item is None:
            return ""
        return item.data(Qt.ItemDataRole.UserRole) or ""

    def _current_matches(self) -> list[SecretScanMatch]:
        """Danh sách match đang hiển thị — bảng là nguồn chân lý duy nhất."""
        matches = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is None:
                continue
            match = item.data(Qt.ItemDataRole.UserRole + 1)
            if isinstance(match, SecretScanMatch):
                matches.append(match)
        return matches

    def _refresh_totals(self) -> None:
        rows = self.table.rowCount()
        files = len(
            {
                _norm(self._row_path(r))
                for r in range(rows)
                if self._row_path(r)
            }
        )
        if rows == 0:
            self.lbl_total.setText(
                translations.localize_text("Tổng: 0 văn bản mật")
            )
        elif rows == files:
            self.lbl_total.setText(
                translations.localize_text(f"Tổng: {files} văn bản mật")
            )
        else:
            self.lbl_total.setText(
                translations.localize_text(
                    f"Tổng: {files} văn bản mật ({rows} dòng)"
                )
            )
        self.btn_export.setEnabled(not self._busy and rows > 0)
        self._refresh_action_buttons()

    def _refresh_action_buttons(self) -> None:
        busy = self._busy
        has_check = bool(self._checked_paths)
        self.btn_batch_delete.setEnabled(not busy and has_check)
        self.btn_batch_not_secret.setEnabled(not busy and has_check)
        has_preview = self._preview_current is not None
        self.btn_preview_delete.setEnabled(not busy and has_preview)
        self.btn_preview_not_secret.setEnabled(not busy and has_preview)

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != 0:
            return
        path = item.data(Qt.ItemDataRole.UserRole)
        if not path:
            return
        if item.checkState() == Qt.CheckState.Checked:
            self._checked_paths.add(_norm(path))
        else:
            self._checked_paths.discard(_norm(path))
        self._refresh_action_buttons()

    def _on_row_clicked(self, item: QTableWidgetItem) -> None:
        if item.column() == 0:
            return  # cột checkbox: chỉ check, không đổi preview
        self._show_preview(item.row())

    def _remove_file_rows(self, path: str) -> None:
        key = _norm(path)
        rows = [
            r
            for r in range(self.table.rowCount())
            if _norm(self._row_path(r)) == key
        ]
        for r in sorted(rows, reverse=True):
            self.table.removeRow(r)
        self._checked_paths.discard(key)
        if (
            self._preview_current is not None
            and _norm(self._preview_current.source_path) == key
        ):
            self._preview_current = None
            self.pdf_viewer.clear()
            self.preview_stack.setCurrentIndex(0)
            self.lbl_preview_empty.setText(
                "Chọn một dòng trong bảng để xem trước nội dung file."
            )
            self._set_preview_banner("", None)
            self._set_preview_busy(False)
        self._refresh_totals()

    def _reset_results(self) -> None:
        self._loaded_from = None
        self._checked_paths.clear()
        self._preview_current = None
        self._preview_convert_gen += 1  # vô hiệu hoá kết quả convert cũ
        self.table.setRowCount(0)
        self.pdf_viewer.clear()
        self.preview_stack.setCurrentIndex(0)
        self.lbl_preview_empty.setText(
            "Chọn một dòng trong bảng để xem trước nội dung file."
        )
        self.lbl_preview_path.setText("")
        self.lbl_preview_path.setToolTip("")
        self._set_preview_banner("", None)
        self._set_preview_busy(False)
        self.btn_export.setEnabled(False)
        self._refresh_totals()

    def _on_finished(self, payload: dict) -> None:
        self._busy = False
        self._set_running_ui(False)
        total = int(payload.get("total") or 0)
        scanned = int(payload.get("scanned") or 0)
        skipped = int(payload.get("skipped") or 0)
        cache_skipped = int(payload.get("cache_skipped") or 0)
        junk = int(payload.get("junk") or 0)
        failures = int(payload.get("failures") or 0)
        cancelled = bool(payload.get("cancelled"))
        found = self.table.rowCount()
        prefix = "Đã dừng" if cancelled else "Hoàn tất"
        detail = f"quét {scanned + skipped + cache_skipped}/{total} file"
        notes = []
        if skipped:
            notes.append(f"bỏ qua {skipped} file đã quét lần trước")
        if cache_skipped:
            notes.append(f"tận dụng {cache_skipped} file không đổi")
        if junk:
            notes.append(f"bỏ qua {junk} file hỏng")
        if notes:
            detail += " (" + "; ".join(notes) + ")"
        self._set_status(
            f"{prefix}: {detail}, phát hiện {found} dòng mật, lỗi {failures}"
        )
        self.log_message.emit(
            f"Quét file mật: {prefix.lower()} - {detail}, "
            f"{found} dòng mật, lỗi {failures}.",
            "success" if not cancelled else "info",
        )
        if cancelled:
            self.log_message.emit(
                "Tiến độ quét đã được lưu — chọn lại đúng thư mục này rồi bấm "
                "\"Bắt đầu quét\" để tiếp tục từ nơi dừng.",
                "info",
            )

        work_root = payload.get("work_root")
        if work_root and os.path.isdir(work_root):
            shutil.rmtree(work_root, ignore_errors=True)

    def _clear_history_clicked(self) -> None:
        if self._busy:
            return
        answer = QMessageBox.question(
            self,
            "Xóa lịch sử quét?",
            (
                "Sẽ xóa toàn bộ:\n"
                "• Tiến độ các lượt quét chưa hoàn tất (không tiếp tục được nữa)\n"
                "• Lịch sử \"đã quét\" — lượt quét sau sẽ quét lại mọi file từ đầu\n\n"
                "Kết quả đã xuất Excel, log trong logs/ và các xác nhận\n"
                "\"không phải mật\" không bị ảnh hưởng.\n"
                "Xóa ngay?"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            removed = ssp.clear_all()
        except Exception as exc:
            QMessageBox.critical(
                self, "Lỗi", f"Không xóa được lịch sử quét:\n{exc}"
            )
            return
        self._set_status(f"Đã xóa {removed} file lịch sử quét")
        self.log_message.emit(
            f"Đã xóa lịch sử quét văn bản mật ({removed} file trong scan_progress/).",
            "success",
        )

    def _open_result_file(self, item: QTableWidgetItem) -> None:
        if item.column() == 0:
            return  # double-click vào ô checkbox: chỉ check, không mở file
        source_path = item.data(Qt.ItemDataRole.UserRole)
        if source_path and os.path.exists(source_path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(source_path))

    def _export_clicked(self) -> None:
        matches = self._current_matches()
        if not matches:
            QMessageBox.information(
                self,
                "Chưa có kết quả",
                "Chưa có văn bản mật nào được phát hiện để xuất.",
            )
            return
        loaded_dir = (
            os.path.dirname(self._loaded_from) if self._loaded_from else ""
        )
        folder = self.folder_edit.text().strip()
        if loaded_dir and os.path.isdir(loaded_dir):
            start_dir = loaded_dir
        elif folder and os.path.isdir(folder):
            start_dir = folder
        else:
            start_dir = os.path.expanduser("~")
        default_name = f"DanhSachVanBanMat_{time.strftime('%Y%m%d_%H%M%S')}.xlsx"
        dest, _ = QFileDialog.getSaveFileName(
            self,
            translations.localize_text("Xuất danh sách văn bản mật"),
            os.path.join(start_dir, default_name),
            translations.localize_text("Excel (*.xlsx)"),
        )
        if not dest:
            return
        if not dest.lower().endswith(".xlsx"):
            dest += ".xlsx"
        if self._loaded_from and os.path.abspath(dest) == self._loaded_from:
            # Không bao giờ ghi đè file đang load — tự đổi tên kèm mốc giờ.
            dest = (
                os.path.splitext(dest)[0]
                + f"_moi_{time.strftime('%Y%m%d_%H%M%S')}.xlsx"
            )
            QMessageBox.information(
                self,
                "Không ghi đè file đang load",
                translations.localize_text(
                    f"File đang load không được ghi đè.\n"
                    f"Danh sách sẽ xuất ra:\n{dest}"
                ),
            )
        try:
            export_matches_to_excel(matches, dest)
        except Exception as exc:
            QMessageBox.critical(
                self, "Lỗi", f"Không thể xuất file Excel:\n{exc}"
            )
            return
        self._set_status(f"Đã xuất {len(matches)} dòng mật ra: {dest}")
        self.log_message.emit(
            f"Đã xuất danh sách văn bản mật ({len(matches)} dòng): {dest}",
            "success",
        )
        QMessageBox.information(
            self,
            "Đã xuất Excel",
            translations.localize_text(
                f"Đã lưu danh sách {len(matches)} dòng văn bản mật vào:\n{dest}"
            ),
        )
