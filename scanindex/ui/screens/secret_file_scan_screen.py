"""Tool screen for scanning folders for classified document stamps."""
from __future__ import annotations

import copy
import hashlib
import json
import logging
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
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from PySide6.QtCore import QRect, QSignalBlocker, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
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
    QStyle,
    QStyleOptionButton,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from scanindex.infra.paths import get_base_dir, get_resource_path
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


# Alias (KHÔNG subclass) — engine bắt `CoreScanCancelled` để truyền nguyên
# trạng lệnh hủy qua các wrapper `except Exception`; nếu tạo subclass tại đây
# thì instance lớp do engine raise sẽ không bị `except _ScanCancelled` bắt.
from scanindex.core.ocr.cancellation import CoreScanCancelled as _ScanCancelled


class _ScanSkip(Exception):
    """File không bao giờ quét được (rỗng / không phải PDF / 0 trang).

    Khác với lỗi tạm thời: ghi journal với trạng thái "skip" để resume
    bỏ qua luôn, không thử lại mỗi lượt quét và không đếm là Lỗi."""
    pass


class _PrepProcessError(RuntimeError):
    """Managed prep failed; retry the file without starting native in parent."""


class _ArtifactPersistError(RuntimeError):
    """R3-01: kết quả KHÔNG nằm bền vững trên đĩa (registry record lỗi) —
    caller phải coi file là CHƯA hoàn tất (vào cơ chế retry / đếm lỗi),
    không được tăng counter thành công hay discard journal."""


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
    # Version app đã SINH RA dòng này. Journal/cache bản cũ không có field
    # ("" = kế thừa) — dùng phân biệt "chưa cập nhật" với "đã quét bằng
    # bản mới nhưng không tìm được năm" (R8 review).
    source_version: str = ""
    # Tên cơ quan dự phòng "<cơ quan ban hành> <cơ quan cấp trên>*" do KIE
    # (nhãn ISSUE_ORG_NAME / ISSUE_ORG_SUPERIOR) bóc từ chính văn bản mật.
    # CHỈ dùng khi mã định danh trong tên file KHÔNG tra được tên ở
    # madinhdanh_lookup.json; dấu "*" ở cuối đánh dấu đây là cơ quan theo
    # văn bản, không phải cơ quan xác định rõ qua danh mục.
    kie_org_name: str = ""
    # Sửa tay trực tiếp trên bảng: mã/tên cơ quan người dùng gõ đè. Rỗng =
    # chưa sửa, hiển thị & xuất Excel theo suy động (tên file → danh mục →
    # dự phòng KIE). Dùng để gộp danh sách nhiều thư mục vẫn sửa được sai
    # sót nhận dạng ngay trên bảng mà không cần sửa Excel.
    org_code_override: str = ""
    org_name_override: str = ""
    # Ngày cập nhật file (epoch giây) — chụp lúc quét để hiển thị lại cho
    # dòng có file đã bị xóa khỏi đĩa; file còn tồn tại thì hiển thị luôn
    # mtime hiện thời (xem _file_mtime_for_display).
    file_mtime: float = 0.0


@dataclass
class ResumeDecision:
    """Kết quả hộp thoại resume — quyết định chính sách cache & hàng đợi.

    mode:
      - ``restart``       : quét lại từ đầu — BỎ QUA cache lookup toàn lượt
                           (R5) nhưng vẫn ghi lịch sử mới.
      - ``resume``        : quét tiếp; kế thừa + đóng dấu version mới cho
                           nhóm đã quét (lựa chọn 2).
      - ``resume_rescan`` : như ``resume`` + quét lại đúng các file mật cũ
                           (lựa chọn 3), thay thế hàng từng file.
    ``rescan_abs``: danh sách đường dẫn tuyệt đối ĐÃ CHUẨN HÓA của các file
    mật cũ (định danh theo source_path — R6, không theo rel kế thừa).
    """

    RESTART = "restart"
    RESUME = "resume"
    RESUME_RESCAN = "resume_rescan"

    mode: str
    prog: object | None = None
    rescan_abs: list[str] = field(default_factory=list)


@dataclass
class SecretScanArtifact:
    matches: list[SecretScanMatch]
    source_pdf: str
    canonical: dict
    rotations: list[int] | None = None
    # clean | confirmed_positive | unconfirmed (LightGBM lỗi — dương có cảnh
    # báo) | error. Đi xuyên suốt journal/registry/Excel, không chỉ nằm ở note.
    status: str = "clean"
    # GĐ K: True khi metadata (cơ quan/năm) đang chạy deferred trên pool KIE —
    # caller HOÃN commit file tới khi coordinator drain (kết thúc lượt tính
    # đến metadata cuối). Kết luận về dấu KHÔNG phụ thuộc cờ này.
    metadata_pending: bool = False
    # GĐ K (P1-07/P1-09): trạng thái metadata deferred — {"applied": bool,
    # ...} do `_try_deferred_kie` tạo; commit đọc cờ này để KHÔNG cache-final
    # khi metadata chưa áp xong (drain timeout / KIE lỗi).
    kie_state: dict | None = None


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
    bundled = get_resource_path("models/fonts/NotoSans-Regular.ttf")
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
    cancel_event: threading.Event | None = None,
) -> tuple[str, list[int] | None]:
    # GĐ P (P-b, mặc định OFF): OCRTOOL_PREP_MODE=prep-process → xoay/deskew
    # chạy trong process riêng (isolate fitz + gối với OCR). Cùng hàm, cùng
    # tham số với đường inline → parity theo cấu trúc; lỗi executor → fallback
    # inline, không fail lượt quét. P2-3 (review 2): truyền context hủy của
    # lượt xuống run_prep — Stop phải ngắt được cả prep đang chờ/chạy.
    if os.environ.get("OCRTOOL_PREP_MODE", "").strip().lower() == "prep-process":
        try:
            from scanindex.core.preprocessing import prep_worker as _pw

            handled, meta, err = _pw.run_prep(
                pdf_path, out_pdf, max_workers, timeout=600.0,
                cancel_event=cancel_event,
            )
            if handled:
                rotations = (meta or {}).get("page_rotations") or None
                log_cb("Preprocess hướng trang chạy ở process prep riêng")
                return out_pdf, rotations
            # R2-04: bị hủy (cancel_event hoặc prep báo canceled) → KHÔNG
            # fallback inline — người dùng đã Stop, chạy thêm preprocess chỉ
            # kéo dài việc dừng; caller kiểm tra cancel_event ở điểm kế.
            if cancel_event is not None and cancel_event.is_set():
                log_cb(f"Prep process dừng theo yêu cầu hủy ({err})")
                return pdf_path, None
            if str(err or "").startswith("canceled:"):
                log_cb(f"Prep process bị hủy ({err})")
                return pdf_path, None
            raise _PrepProcessError(f"Prep process chưa hoàn tất ({err}); file giữ để thử lại")
        except _PrepProcessError:
            raise
        except Exception as exc:
            raise _PrepProcessError(f"Prep process lỗi ({exc}); file giữ để thử lại") from exc
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
    detect_cb: Callable[[dict, int], bool] | None = None,
    stop_check: Callable[[], bool] | None = None,
    page_error_policy: Callable[[int], str] | None = None,
    deferred_errors: dict[int, str] | None = None,
) -> dict:
    """Build a canonical document from a PDF, classifying each page.

    ``deferred_errors`` (P1-E, REVIEW v2): khi caller truyền dict, lỗi lắp
    trang (OCR sentinel/missing) KHÔNG raise/skip ngay tại callback — được
    ghi ``{rel_page_idx: reason}`` để caller phân loại SAU khi biết dấu được
    chấp nhận (kết quả nghiệp vụ độc lập thứ tự hoàn tất callback). Bỏ qua
    tham số → hành vi cũ (raise ngay / policy skip_warn).

    Reuses ``classify_pdf_page`` (the same per-page classifier the PDF-to-Word
    feature uses) so each page is handled correctly:
      - ``digital``  → native text via ``page.get_text`` (OCR skipped)
      - ``scan``/``mixed`` → ScreenAI OCR at ``dpi``

    This avoids the false negative the old fast-path had (scan_ocr_low text
    layer accepted blindly) and the wasted OCR of purely-digital pages.

    v4: trang scan/mixed được lắp vào canonical NGAY khi OCR xong (theo thứ tự
    hoàn thành; sort theo ``page_index`` trước khi ghi JSON) và ``detect_cb``
    chạy trên từng trang vừa xong nếu được cung cấp — trả True để dừng nộp
    trang mới (dừng sớm theo dấu). Lỗi/timeout OCR → raise RuntimeError (không
    bao giờ lắp trang rỗng). ``_ScanCancelled`` truyền nguyên trạng qua pool.
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
    # multiple files (run in parallel file-worker threads) share its workers.
    # DPI override keeps _SECRET_SCAN_DPI (200) instead of the engine-wide 240.
    ocr_pages = [p for p in page_indices if classifications.get(p, "scan") != "digital"]
    ocr_results: dict[int, dict | None] = {}

    def _assemble_scan_page(page_idx: int, result) -> dict:
        """Helper DÙNG CHUNG (P1-06): lắp record trang scan/mixed — khai báo
        ngoài nhánh pool để cả đường single-worker lẫn fallback tuần tự đều
        lắp đúng; sentinel/missing → raise (không trang rỗng)."""
        state = direct_ocr_engine.page_result_state(result)
        if state in {"error", "missing"}:
            reason = (result or {}).get("error") if isinstance(result, dict) else None
            raise RuntimeError(
                f"OCR thất bại ở trang {page_idx + 1}: {reason or state}"
            )
        page_w, page_h = page_rects[page_idx]
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
        page_record["source_mode"] = classifications.get(page_idx, "scan")
        return page_record

    completed_scan_pages: set[int] = set()
    skipped_scan_pages: set[int] = set()  # trang bỏ CÓ CHỦ ĐÍCH (dừng sớm)

    if ocr_pages:
        use_pool = direct_ocr_engine.get_parallel_capacity() > 1
        if use_pool:
            log_cb(
                f"OCR {len(ocr_pages)}/{len(page_indices)} trang (scan/mixed) @ {dpi} DPI "
                f"qua pool {direct_ocr_engine.get_parallel_capacity()} worker..."
            )

            def _on_page_done(page_idx: int, result: dict) -> bool:
                completed_scan_pages.add(page_idx)
                try:
                    page_record = _assemble_scan_page(page_idx, result)
                except RuntimeError as exc:
                    # P1-E: caller truyền deferred_errors → lỗi trang là MỘT
                    # outcome được giữ theo index, phân loại SAU khi biết dấu
                    # được chấp nhận (độc lập thứ tự callback). Không raise
                    # vội: trang dấu cùng nhóm có thể chưa kịp lắp.
                    if deferred_errors is not None:
                        deferred_errors[page_idx] = str(exc)
                        log_cb(f"{exc} — tạm giữ, phân loại sau khi biết "
                               "dấu được chấp nhận")
                        return False
                    # P1-08 (đường cũ): lỗi trang SAU dấu đã chấp nhận =
                    # trang thừa → cảnh báo + bỏ qua có ghi nhận; còn lại raise.
                    if (
                        page_error_policy is not None
                        and page_error_policy(page_idx) == "skip_warn"
                    ):
                        skipped_scan_pages.add(page_idx)
                        log_cb(f"{exc} — trang thừa sau dấu đã chấp nhận, "
                               "bỏ qua có ghi nhận")
                        return False
                    raise
                canonical["pages"].append(page_record)
                if detect_cb is not None:
                    if detect_cb(canonical, page_idx):
                        skipped_scan_pages.update(
                            p for p in ocr_pages if p not in completed_scan_pages
                        )
                        return True
                return False

            def _pool_log(msg, lvl="info"):
                if str(lvl).lower() != "debug":
                    log_cb(str(msg))

            try:
                ocr_results = direct_ocr_engine._process_selected_pages_parallel(
                    pdf_path,
                    ocr_pages,
                    total,
                    _pool_log,
                    dpi=dpi,
                    on_page_done=_on_page_done,
                    stop_check=stop_check,
                    cancel_check=cancel_event.is_set,
                )
            except _ScanCancelled:
                raise
            except direct_ocr_engine.PagePostOcrError as exc:
                # Lỗi lắp/dò từng trang — lỗi THẬT, không fallback:
                # trang phải được tính lỗi (resume quét lại), không bỏ qua.
                raise RuntimeError(
                    f"OCR thất bại ở trang {exc.page_idx + 1}: {exc.cause}"
                )
            except Exception as exc:
                log_cb(f"Pool OCR lỗi, chuyển single-worker: {exc}")
                ocr_results = {}
        if not ocr_results:
            log_cb(f"OCR {len(ocr_pages)} trang (scan/mixed) @ {dpi} DPI single-worker...")
            for pidx in ocr_pages:
                if cancel_event.is_set():
                    raise _ScanCancelled()
                single = _ocr_one_page_single_worker(
                    direct_ocr_engine, pdf_path, pidx, dpi=dpi
                )
                # lắp NGAY qua helper dùng chung (P1-06) — cả Tìm nhanh lẫn
                # Tìm kỹ đều có record; lỗi sentinel/None → raise đúng trang.
                try:
                    page_record = _assemble_scan_page(pidx, single)
                except RuntimeError as exc:
                    # P1-E: chế độ deferred — giữ như một outcome, không raise
                    # tại đây (phân loại ở caller sau khi biết dấu chấp nhận).
                    if deferred_errors is not None:
                        deferred_errors[pidx] = str(exc)
                        log_cb(f"{exc} — tạm giữ, phân loại sau khi biết "
                               "dấu được chấp nhận")
                        continue
                    raise
                canonical["pages"].append(page_record)
                completed_scan_pages.add(pidx)
                if detect_cb is not None:
                    if detect_cb(canonical, pidx):
                        skipped_scan_pages.update(
                            p for p in ocr_pages if p not in completed_scan_pages
                        )
                        break
    else:
        log_cb(f"Tất cả {len(page_indices)} trang là digital — bỏ OCR")

    # Digital pages: native text. Trang scan CHƯA lắp:
    #  - có kết quả (kể cả engine fallback tuần tự trả dict) → lắp BÂY GIỜ
    #    qua helper — không bỏ kết quả OCR hợp lệ (P1-06);
    #  - không có kết quả và không nằm trong tập bỏ-có-chủ-đích → lỗi thật
    #    (không được ngầm coi là "bỏ qua có chủ đích").
    scanned_idx = {p.get("page_index") for p in canonical["pages"]}
    for page_idx in page_indices:
        if cancel_event.is_set():
            raise _ScanCancelled()
        if page_idx < 0 or page_idx >= total:
            continue
        if page_idx in scanned_idx:
            continue
        if classifications.get(page_idx, "scan") == "digital":
            with _FITZ_LOCK:
                with fitz.open(pdf_path) as doc:
                    page = doc[page_idx]
                    page_record = _build_native_page_record_from_pdf(page, page_idx)
            page_record["source_mode"] = "digital"
            canonical["pages"].append(page_record)
            continue
        if page_idx in skipped_scan_pages:
            continue  # bỏ CÓ CHỦ ĐÍCH do dừng sớm — không phải lỗi
        if deferred_errors is not None and page_idx in deferred_errors:
            continue  # P1-E: lỗi được giữ cho caller phân loại sau — không raise tại đây
        if page_idx in ocr_results:
            page_record = _assemble_scan_page(page_idx, ocr_results[page_idx])
            canonical["pages"].append(page_record)
            continue
        raise RuntimeError(
            f"OCR thất bại ở trang {page_idx + 1}: thiếu kết quả "
            "(không phải trang bỏ qua có chủ đích)"
        )

    canonical["pages"].sort(key=lambda r: int(r.get("page_index", 0)))
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
    stop_on_first: bool = False,
) -> list[SecretScanMatch]:
    matches: list[SecretScanMatch] = []
    # Chụp mtime một lần cho cả file — hiển thị "Ngày cập nhật" ở bảng/Excel.
    try:
        file_mtime = float(os.stat(source_path).st_mtime)
    except OSError:
        file_mtime = 0.0
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
                    file_mtime=file_mtime,
                )
            )
            if stop_on_first:
                # Mọi chế độ Tìm kỹ: gặp trang đầu tiên có dấu là KẾT LUẬN —
                # các trang sau không xét nữa (dấu nào tới trước tính theo
                # thứ tự trang).
                break
    return matches


def _thorough_page_window(total_pages: int, page_limit: int) -> list[int]:
    """Danh sách chỉ số trang cần xét ở nhánh "Tìm kỹ": toàn bộ trang, hoặc
    CHỈ ``page_limit`` trang đầu khi ``page_limit`` > 0 ("Tìm kỹ 10 trang
    đầu"). File ít trang hơn giới hạn thì xét hết file."""
    total = max(0, int(total_pages))
    if page_limit and int(page_limit) > 0:
        return list(range(min(int(page_limit), total)))
    return list(range(total))


def _pdf_file_kind(pdf_path: str) -> str:
    """File-level classify (digital/scan/…) — quyết định có cần xoay trang.
    Lỗi → "scan" (an toàn: vẫn xoay như trước)."""
    try:
        from scanindex.core.preprocessing import preprocessing as _pre

        return _pre.classify_pdf(pdf_path)
    except Exception:
        return "scan"


def _stream_chunk_size() -> int:
    """Số trang mỗi nhóm chuẩn bị (cắt/tiền xử lý) của Tìm kỹ — TÁCH KHỎI số
    worker pool (mục 12.4): nhóm 8 không có nghĩa pool chỉ có 8 worker; nhiều
    tài liệu cùng cấp việc cho pool chung. Giá trị khởi điểm ``min(16,
    capacity)`` chặn chi phí chuẩn bị; trần "sẵn sàng" của file là cửa sổ
    admission (không còn trần ẩn 16 của collector). Override bằng biến môi
    trường ``SECRET_SCAN_STREAM_CHUNK`` (thử nghiệm có chủ đích)."""
    raw = os.environ.get("SECRET_SCAN_STREAM_CHUNK", "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    try:
        from scanindex.core.ocr import direct_engine

        capacity = int(direct_engine.get_parallel_capacity())
    except Exception:
        capacity = 2
    return max(4, min(16, capacity))


def _process_and_detect_streaming(
    scan_pdf: str,
    source_path: str,
    relative_path: str,
    page_indices: list[int],
    *,
    mode: str,
    artifact_path: str,
    note: str,
    cancel_event: threading.Event,
    log_cb: Callable[[str], None],
    dpi: int = _SECRET_SCAN_DPI,
    json_path: str | None = None,
    stop_on_first: bool = True,
    rotate: bool = False,
    ocr_workers: int | None = None,
    file_work_dir: str = "",
) -> tuple[dict, list[SecretScanMatch], list[int] | None, str]:
    """OCR + dò dấu SONG SONG theo từng trang; dừng sớm theo dấu ĐƯỢC XÁC NHẬN.

    Mỗi trang OCR xong là được dò NGAY (detect per page). Gặp dấu ứng viên:
    LightGBM xác nhận NGAY trên canonical hiện có — chấp nhận thì dừng (các
    trang sau không OCR); bị loại thì TIẾP TỤC xét các trang sau (ứng viên bị
    loại không làm toàn file thành sạch khi còn trang chưa xét); LightGBM lỗi
    thì quét phủ đầy phạm vi và giữ ứng viên với chú thích "chưa xác nhận"
    (không bao giờ âm tính im lặng). Khi cần xoay, từng lượt được cắt riêng
    rồi xoay riêng; rebase chỉ số theo ``page_index`` của từng record (không
    ghép vị trí). Canonical giữ prefix liên tục đến trang dấu được chấp nhận
    (bất biến cho LightGBM/KIE lọc theo ordinal). Trả về
    ``(canonical, matches, rotations, mark_status)`` với ``mark_status`` ∈
    ``none | accepted | dropped_continued | lgbm_error_unconfirmed``.
    """
    from scanindex.core.kie.json_utils import make_document_stub
    from scanindex.core.ocr.text_normalizer import OCR_TEXT_NORMALIZATION

    canonical = make_document_stub(
        input_path=scan_pdf,
        engine="direct_screen_ai",
        ocr_dpi=dpi,
        source_path=source_path,
        text_normalization=OCR_TEXT_NORMALIZATION,
        raw_text_preserved=True,
    )
    chunk_size = _stream_chunk_size()
    matches: list[SecretScanMatch] = []
    rotations: list[int] | None = []
    current_map: list[int] = []  # rel → abs cho lượt đang xử lý ([] = không xoay)
    detected_rel: set[int] = set()  # trang đã dò trong lượt hiện tại
    pending_candidates: list[tuple[SecretScanMatch, int]] = []
    coverage_incomplete = False
    # Trạng thái từ TẬP (P1-08) — không dùng một biến "sự kiện cuối cùng":
    any_accepted = False          # đã có dấu được LightGBM chấp nhận
    accepted_page: int | None = None
    lgbm_error = False            # LightGBM lỗi — ứng viên giữ kèm cảnh báo
    # P1-E (REVIEW v2): lỗi trang trong lượt được GIỮ theo index (rel → reason)
    # và chỉ phân loại bắt-buộc/thừa SAU khi biết dấu được chấp nhận — kết quả
    # nghiệp vụ độc lập thứ tự hoàn tất callback.
    deferred_errors: dict[int, str] = {}

    def _assert_dense_prefix_upto(abs_k: int) -> None:
        """Bất invariant cho LightGBM/KIE (lọc theo ordinal): canonical phải
        chứa LIÊN MỊCH các trang nguồn 0..abs_k. Vi phạm là lỗi lập trình."""
        present = set()
        for page in canonical.get("pages") or []:
            try:
                present.add(int(page.get("page_index", -1)))
            except (TypeError, ValueError):
                continue
        missing = [i for i in range(0, abs_k + 1) if i not in present]
        if missing:
            raise RuntimeError(
                f"canonical thiếu prefix trang nguồn {missing[:5]} — "
                "LightGBM/KIE sẽ đọc sai ordinal"
            )

    def _confirm_candidate(match: SecretScanMatch, abs_page_idx: int) -> bool:
        """LightGBM xác nhận ứng viên trên canonical ĐÃ MERGE (chứa prefix
        nguồn đến trang ứng viên). Trả True nếu chấp nhận (dừng)."""
        nonlocal any_accepted, accepted_page, lgbm_error
        if not json_path:
            matches.append(match)
            any_accepted = True
            accepted_page = abs_page_idx
            return True
        _assert_dense_prefix_upto(abs_page_idx)
        try:
            _finalize_canonical(canonical, json_path)  # LightGBM đọc từ file
            doc_start_pages, lgbm_note = _doc_start_pages(json_path)
        except Exception as exc:
            log_cb(f"LightGBM lỗi, không xác nhận trang đầu văn bản: {exc}")
            lgbm_error = True
            match.note = f"{match.note}; chưa xác nhận LightGBM ({exc})"
            matches.append(match)
            return False
        if abs_page_idx in set(doc_start_pages):
            match.note = f"{match.note}; {lgbm_note}; xác nhận trang đầu"
            matches.append(match)
            any_accepted = True
            accepted_page = abs_page_idx
            return True
        log_cb(
            f"Bỏ qua dấu {match.keyword} ở trang {match.page_number}: "
            "LightGBM không xác định là trang đầu văn bản — tiếp tục xét "
            "các trang sau"
        )
        return False

    def _detect_cb(canonical_partial: dict, rel_page_idx: int) -> bool:
        """Dò từng trang khi OCR xong — chỉ THU ỨNG VIÊN; xác nhận LightGBM
        chạy SAU khi cả lượt được merge vào canonical tổng (P1-05)."""
        if cancel_event.is_set():
            raise _ScanCancelled()
        detected_rel.add(rel_page_idx)
        if not _detect_secret_on_page(canonical_partial, rel_page_idx):
            return False
        page_hits = _collect_secret_matches(
            canonical_partial,
            [rel_page_idx],
            source_path=source_path,
            relative_path=relative_path,
            mode=mode,
            artifact_path=artifact_path,
            note=note,
            cancel_event=cancel_event,
            stop_on_first=True,
        )
        if not page_hits:
            return False
        match = page_hits[0]
        if current_map and 0 <= rel_page_idx < len(current_map):
            try:
                match.page_number = int(current_map[rel_page_idx]) + 1
            except (TypeError, ValueError):
                pass
        pending_candidates.append((match, rel_page_idx))
        return False

    pending_candidates: list[tuple[SecretScanMatch, int]] = []

    for start in range(0, len(page_indices), chunk_size):
        if cancel_event.is_set():
            raise _ScanCancelled()
        abs_chunk = list(page_indices[start:start + chunk_size])
        # Khi xoay: cắt riêng lượt trang thành PDF con (chỉ số 0..n-1) rồi
        # gán lại chỉ số tuyệt đối sau khi OCR; không xoay thì dùng thẳng
        # chỉ số tuyệt đối trên scan_pdf.
        rel_chunk = list(range(len(abs_chunk))) if rotate else abs_chunk
        current_map = abs_chunk if rotate else []
        detected_rel = set()
        log_cb(
            "Dò song song với OCR: xét trang "
            f"{abs_chunk[0] + 1}–{abs_chunk[-1] + 1}..."
        )
        work_pdf = scan_pdf
        if rotate:
            chunk_pdf = os.path.join(file_work_dir, f"chunk_{start:05d}.pdf")
            work_pdf = _extract_pdf_pages(scan_pdf, chunk_pdf, abs_chunk)
            pre_pdf = os.path.join(
                file_work_dir, f"chunk_{start:05d}_pre.pdf"
            )
            work_pdf, chunk_rotations = _preprocess_pdf_for_ocr(
                work_pdf, pre_pdf, log_cb, max_workers=ocr_workers,
                cancel_event=cancel_event,
            )
            if chunk_rotations is None:
                rotations = None
            elif rotations is not None:
                rotations.extend(int(r or 0) for r in chunk_rotations)
        chunk_canonical = _process_pdf_per_page(
            work_pdf,
            source_path,
            rel_chunk,
            cancel_event,
            log_cb,
            dpi=dpi,
            detect_cb=_detect_cb,
            deferred_errors=deferred_errors,
        )
        chunk_pages = chunk_canonical.get("pages") or []
        # Fallback dò hậu-lượt: trang đã OCR nhưng chưa được dò (khi
        # `_process_pdf_per_page` không hỗ trợ detect_cb — vd giả lập test).
        present_rel = {p.get("page_index") for p in chunk_pages}
        for rel in rel_chunk:
            if rel in detected_rel or rel not in present_rel:
                continue
            _detect_cb(chunk_canonical, rel)
        for page_record in chunk_pages:
            try:
                rel_idx = int(page_record.get("page_index", 0))
            except (TypeError, ValueError):
                continue
            if rotate and 0 <= rel_idx < len(abs_chunk):
                page_record["page_index"] = int(abs_chunk[rel_idx])
        canonical["pages"].extend(chunk_pages)

        # Coverage check (P1-08): trang bị bỏ/lỗi TRONG LƯỢT mà chưa có dấu
        # được chấp nhận → coverage chưa đủ; nếu cuối file vẫn không có dấu
        # được chấp nhận thì file KHÔNG được kết luận sạch.
        merged_idx = {p.get("page_index") for p in chunk_pages}
        missing_abs = [a for a in abs_chunk if a not in merged_idx]
        if missing_abs and not any_accepted:
            coverage_incomplete = True

        # Xác nhận ứng viên SAU khi lượt đã merge vào canonical tổng —
        # theo thứ tự trang; prefix nguồn phải hợp lệ trước mỗi lần predict.
        if pending_candidates:
            pending_candidates.sort(key=lambda item: item[1])
            for match, rel_page_idx in pending_candidates:
                if any_accepted:
                    break
                abs_idx = (
                    current_map[rel_page_idx]
                    if current_map and 0 <= rel_page_idx < len(current_map)
                    else rel_page_idx
                )
                if _confirm_candidate(match, abs_idx):
                    break
            pending_candidates = []

        # P1-E: phân loại lỗi trang ĐÃ GIỮ — giờ đã biết dấu được chấp nhận
        # hay chưa. Trang lỗi SAU dấu chấp nhận = trang thừa → cảnh báo; còn
        # lại (kể cả khi ứng viên bị loại/LightGBM lỗi) = lỗi bắt buộc → file
        # lỗi, resume quét lại (P1-F: không thành unconfirmed/cache hoàn tất).
        if deferred_errors:
            for rel_idx in sorted(deferred_errors):
                reason = deferred_errors[rel_idx]
                abs_idx = (
                    current_map[rel_idx]
                    if current_map and 0 <= rel_idx < len(current_map)
                    else rel_idx
                )
                if (
                    any_accepted
                    and accepted_page is not None
                    and abs_idx > accepted_page
                ):
                    log_cb(
                        f"OCR thất bại ở trang {abs_idx + 1} — trang thừa "
                        f"sau dấu đã chấp nhận (trang {accepted_page + 1}), "
                        "bỏ qua có ghi nhận"
                    )
                else:
                    raise RuntimeError(
                        f"OCR thất bại ở trang {abs_idx + 1}: {reason}"
                    )
            deferred_errors.clear()
        if any_accepted:
            log_cb(
                f"Phát hiện dấu mật ở trang {accepted_page + 1} — "
                "dừng; các trang sau không OCR/xét nữa."
            )
            break
        # LightGBM lỗi: KHÔNG dừng sớm vô căn cứ — quét phủ đầy phạm vi,
        # ứng viên giữ với cảnh báo (status tổng ở caller).
    _finalize_canonical(canonical, json_path)
    if any_accepted:
        mark_status = "accepted"
    elif lgbm_error or coverage_incomplete:
        # LGBM lỗi hoặc còn trang chưa xét (bỏ theo ứng viên tiềm năng đã bị
        # loại) → chưa đủ cơ sở kết luận sạch — caller xếp unconfirmed.
        mark_status = "lgbm_error_unconfirmed"
    else:
        mark_status = "none"
    return canonical, matches, rotations, mark_status


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
# Ngoài ra, khi KIE ĐÃ chạy cho trang (pass tên cơ quan dự phòng chạy
# TRƯỚC) thì tận dụng luôn năm trong kết quả đó — một lần infer lấy đủ
# năm + cơ quan ban hành + cơ quan cấp trên.
# KIE KHÔNG dùng để xác định độ mật (model không có nhãn dấu mật).
# Tắt năm-KIE bằng SECRET_SCAN_DISABLE_KIE_YEAR=1.
_DECLASS_KIE_MIN_CANDIDATES = 3
# 2 file-worker không cùng warmup/infer model song song — cả pass năm ban
# hành lẫn pass tên cơ quan dự phòng đều phải chạy qua lock này.
_KIE_INFER_LOCK = threading.RLock()
# Field instances infer theo (file canonical, trang): pass năm và pass tên
# cơ quan thường cùng chạy trên một trang → cache để không infer 2 lần.
# Working set mỗi file chỉ vài trang; vượt ngưỡng thì xóa sạch. R3-03: cache
# CHỈ là tối ưu — kết quả bắt buộc của job deferred được truyền cho applier
# bằng dữ liệu cục bộ, eviction không được đổi metadata đã nhận.
_KIE_FIELDS_CACHE_MAX = 256
# P2-3: run_id của lượt quét ĐANG CHẠY trên thread file-worker — truyền cho
# `_try_deferred_kie` mà không đổi chữ ký public (tương thích monkeypatch).
_scan_run_ctx = threading.local()
_KIE_FIELDS_CACHE: dict[tuple[str, int], list[dict] | None] = {}
# Sentinel "chưa infer cho trang này" — phân biệt với None ("đã infer nhưng
# lỗi/không có field"), để pass năm biết có thể tận dụng kết quả sẵn.
_KIE_CACHE_MISS = object()


def _kie_cache_key(canonical_json_path: str, page_index: int) -> tuple[str, int]:
    return (os.path.normcase(os.path.abspath(str(canonical_json_path))), int(page_index))


def _kie_cache_store(canonical_json_path: str, page_index: int, fields) -> None:
    """R2-06: ghi cache field CHỈ QUA ĐÂY — áp giới hạn `_KIE_FIELDS_CACHE_MAX`
    cho CẢ đường đồng bộ lẫn callback deferred (trước đây deferred ghi thẳng
    dictionary, vượt giới hạn theo số canonical đã xử lý). Evict theo batch
    như semantics hiện có. R3-03: cache CHỈ là tối ưu — đường deferred áp
    fields từ dữ liệu cục bộ của job (`fields_override`), eviction mất cache
    KHÔNG được làm sai/kthiếu metadata của kết quả vừa nhận."""
    key = _kie_cache_key(canonical_json_path, page_index)
    if key not in _KIE_FIELDS_CACHE \
            and len(_KIE_FIELDS_CACHE) >= _KIE_FIELDS_CACHE_MAX:
        _KIE_FIELDS_CACHE.clear()
    _KIE_FIELDS_CACHE[key] = fields


def _kie_cached_fields(canonical_json_path: str, page_index: int):
    """Peek cache KHÔNG infer: trả list field, None (infer lỗi) hoặc
    ``_KIE_CACHE_MISS`` khi trang chưa từng được infer."""
    if not canonical_json_path:
        return _KIE_CACHE_MISS
    return _KIE_FIELDS_CACHE.get(
        _kie_cache_key(canonical_json_path, page_index), _KIE_CACHE_MISS
    )


def _kie_fields_for_page(
    canonical_json_path: str, page_index: int
) -> list[dict] | None:
    """Field instances của LayoutLM trên đúng trang, infer 1 lần/(file, trang).

    Trả None khi model không có sẵn / lỗi / payload lệch dạng — caller
    fallback về kết quả regex (năm ban hành) hoặc bỏ qua (tên cơ quan dự
    phòng). Mọi lỗi model đều bị nuốt ở đây: một file mật không được phép
    fail lượt quét chỉ vì KIE lỗi.
    """
    if not canonical_json_path or not os.path.exists(canonical_json_path):
        return None
    key = _kie_cache_key(canonical_json_path, page_index)
    if key in _KIE_FIELDS_CACHE:
        return _KIE_FIELDS_CACHE[key]
    result: list[dict] | None = None
    try:
        from scanindex.core.kie.engine import _run_layoutlmv3

        with _KIE_INFER_LOCK:
            payload = _run_layoutlmv3(
                canonical_json_path, selected_pages=[int(page_index)]
            )
        if isinstance(payload, dict):
            # Bọc try riêng: payload dạng lạ (field_instances là chuỗi/số...)
            # phải đọc thành rỗng chứ không raise ra khỏi hàm này.
            try:
                raw_fields = payload.get("field_instances") or []
                result = [f for f in raw_fields if isinstance(f, dict)]
            except Exception:
                result = None
    except Exception:
        result = None
    _kie_cache_store(canonical_json_path, page_index, result)
    return result


def _kie_year_from_fields(fields, page_index: int) -> int | None:
    """Năm ban hành từ DANH SÁCH field đã có (không infer) — nhãn PLACE_DATE.
    Trả None khi không gán được."""
    for field in fields or []:
        if not isinstance(field, dict):
            continue
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


def _kie_issue_year(canonical_json_path: str, page_index: int) -> int | None:
    """Năm ban hành theo field PLACE_DATE của LayoutLM trên đúng trang.

    Trả None khi model không có sẵn / lỗi / không gán nhãn được — caller
    fallback về kết quả regex.
    """
    return _kie_year_from_fields(
        _kie_fields_for_page(canonical_json_path, page_index), page_index
    )


def _match_meets_declassification(match: SecretScanMatch) -> bool:
    return bool(match.declass_due) or DECLASS_NOTE_LABEL in (match.note or "")


def _safe_page_index(page_number) -> int:
    """Trang 0-based từ page_number của match; kiểu rác (None/str hỏng…) → 0.

    page_number do code mình sinh luôn là int, nhưng dòng nạp lại từ
    Excel/journal không đáng tin tuyệt đối — một dòng rác không được phép
    làm sập nguyên file-worker.
    """
    try:
        return max(0, int(page_number or 1) - 1)
    except (TypeError, ValueError):
        return 0


def _deferred_cache_final(
    artifact,
    path: str,
    scan_stat: tuple[int, float] | None,
    source_unstable: bool = False,
) -> tuple[bool, str]:
    """P1-07/P2.5/P2-H: kết quả file có được ghi cache-final không.

    - metadata deferred CHƯA áp xong (drain timeout / KIE lỗi) → KHÔNG:
      lượt sau phải quét lại thay vì cache-hit kết quả thiếu metadata.
    - `source_unstable` (nguồn đổi TRONG lúc quét: pre-stat ≠ post-stat) →
      KHÔNG: kết quả trộn phiên bản, không được hợp thức hóa bằng cách chụp
      stat lại.
    - fingerprint chụp trước khi đọc (scan_stat) ≠ hiện tại → KHÔNG (file
      đổi giữa lúc chờ commit).
    Trả ``(cache_final, lý_do)`` — lý do rỗng khi đủ điều kiện."""
    kie_state = getattr(artifact, "kie_state", None)
    if kie_state is not None and not kie_state.get("applied"):
        return False, "metadata-pending"
    if source_unstable:
        return False, "nguồn-dổi-trong-lúc-quét"
    if scan_stat is not None:
        try:
            st = os.stat(path)
        except OSError:
            return False, "source-missing"
        if (st.st_size, st.st_mtime) != tuple(scan_stat):
            return False, "source-changed"
    return True, ""


def _strip_declass_note_details(note: str) -> str:
    """Bỏ mọi đoạn ghi chú "Đáp ứng thời gian giải mật (...)" khỏi note —
    cho phép áp lại idempotent (baseline regex → KIE nâng cấp năm mà KHÔNG
    đúp note). Các đoạn note khác giữ nguyên."""
    if not note:
        return note
    segments = [s.strip() for s in note.split(";")]
    kept = [
        s for s in segments
        if s and DECLASS_NOTE_LABEL not in s
    ]
    return "; ".join(kept)


def _apply_declassification_notes(
    matches: list[SecretScanMatch],
    canonical: dict,
    canonical_json_path: str | None = None,
    allow_model: bool = True,
    fields_override: dict[int, list[dict] | None] | None = None,
) -> None:
    """Gắn ghi chú "Đáp ứng thời gian giải mật" khi đã quá thời hạn mật.

    Năm ban hành lấy từ dòng ngày của CHÍNH trang chứa dấu mật (file ghép
    nhiều văn bản), so với năm hiện tại theo độ mật (Mật 10, Tối mật 20,
    Tuyệt mật 30 năm). Không suy được năm ban hành thì bỏ qua.

    Lấy năm theo cascade: regex chọn dòng ngắn gần đầu trang (miễn phí);
    gọi KIE (LayoutLM, nhãn PLACE_DATE) khi (a) trang có nhiều hơn 2 dòng
    ngày ứng viên (nhiễu), hoặc (b) KIE ĐÃ chạy cho trang này vì pass tên
    cơ quan dự phòng — khi đó đọc lại từ cache, không infer thêm. Chỉ chạy
    trên các file đã xác định là mật.

    ``allow_model=False`` (P1-08): chỉ regex — KHÔNG infer, KHÔNG kể cache.
    Dùng cho baseline-trước-enqueue và callback deferred (không được quay về
    KIE đồng bộ trong callback). Hàm IDEMPOTENT: ghi chú cũ cùng nhãn bị
    thay (không đúp) nên gọi nhiều lần an toàn.

    ``fields_override`` (R3-03): fields của job KIE truyền TRỰC TIẾP theo
    trang — dữ liệu cục bộ còn sống tới khi áp xong, ưu tiên cao nhất. Cache
    toàn cục chỉ là TỐI ƯU: giữa store và apply, writer khác có thể chạm
    ngưỡng và clear cache — applier deferred (allow_model=False) không được
    phép rơi về regex/thiếu fields mà vẫn đánh dấu applied.
    """
    if not matches:
        return
    current_year = time.localtime().tm_year
    # Cờ env tắt năm-KIE chặn CẢ cache-read lẫn infer; allow_model=False
    # (baseline/callback deferred) chỉ chặn INFER — cache vẫn đọc được.
    kie_enabled = (
        os.environ.get("SECRET_SCAN_DISABLE_KIE_YEAR", "").lower()
        not in {"1", "true"}
    )
    year_by_page: dict[int, int] = {}
    for match in matches:
        # Idempotent: bỏ ghi chú giải mật cũ trước khi tính lại (năm KIE có
        # thể khác năm regex baseline — chỉ được tồn tại MỘT ghi chú cuối).
        base_note = _strip_declass_note_details(str(match.note or ""))
        page_idx = _safe_page_index(match.page_number)
        if page_idx not in year_by_page:
            page = (
                _page_for_index(canonical, page_idx)
                if isinstance(canonical, dict)
                else None
            )
            candidates = _issue_date_candidates(page) if page else []
            year = candidates[0][1] if candidates else 0
            if kie_enabled and canonical_json_path:
                override = (
                    fields_override.get(page_idx, _KIE_CACHE_MISS)
                    if fields_override is not None
                    else _KIE_CACHE_MISS
                )
                kie_year = None
                if override is not _KIE_CACHE_MISS:
                    # R3-03: fields của CHÍNH job này (dữ liệu cục bộ) —
                    # không phụ thuộc cache còn sống hay không.
                    kie_year = _kie_year_from_fields(override, page_idx)
                else:
                    already_inferred = (
                        _kie_cached_fields(canonical_json_path, page_idx)
                        is not _KIE_CACHE_MISS
                    )
                    if already_inferred:
                        # Cache CÓ fields (infer đã chạy trước đó / job KIE
                        # vừa về) → đọc cache, KHÔNG infer — đường thường
                        # (allow_model=True) đi đây.
                        cached = _kie_cached_fields(
                            canonical_json_path, page_idx
                        )
                        kie_year = _kie_year_from_fields(cached, page_idx)
                    elif allow_model \
                            and len(candidates) >= _DECLASS_KIE_MIN_CANDIDATES:
                        # (a) trang nhiễu → infer rồi lấy năm theo nhãn
                        # PLACE_DATE. allow_model=False (baseline/callback)
                        # thì KHÔNG BAO GIỜ infer — giữ regex.
                        kie_year = _kie_issue_year(
                            canonical_json_path, page_idx
                        )
                if kie_year is not None:
                    year = kie_year
            year_by_page[page_idx] = year
        year = year_by_page[page_idx]
        match.note = base_note
        # P1-A (review 2): bộ giá trị derived phải được TÍNH LẠI TOÀN BỘ từ
        # nguồn hiện tại rồi thay cùng nhau — nâng/cụt năm (đủ hạn ↔ chưa đủ)
        # hoặc năm không suy được phải XÓA trạng thái cũ của baseline, không
        # để `declass_due=True` cũ sống sót qua lần tính lại.
        match.issue_year = 0
        match.declass_years = 0
        match.declass_due = False
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
            match.note = f"{base_note}; {detail}" if base_note else detail


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


def _settings_file_worker_count() -> int:
    """Đọc [SecretScan] MaxFileWorkers từ settings.ini (0 = không cấu hình).

    Tầng song song KHÁC MaxConcurrentOCR (số trang OCR song song trong pool
    dùng chung toàn app): đây là số TÀI LIỆU quét mật xử lý đồng thời.
    """
    try:
        import configparser

        from scanindex.infra.data_versioning import get_active_settings_path

        path = get_active_settings_path()
        if not path or not os.path.exists(path):
            return 0
        parser = configparser.ConfigParser()
        parser.read(path, encoding="utf-8")
        if not parser.has_option("SecretScan", "MaxFileWorkers"):
            return 0
        return max(1, int(parser.get("SecretScan", "MaxFileWorkers")))
    except Exception:
        return 0


def _file_worker_count(total: int) -> int:
    """Số thread xử lý file song song (clamp 1..total).

    Thứ tự ưu tiên: biến môi trường SECRET_SCAN_MAX_FILE_WORKERS (debug,
    override mọi thứ) → settings.ini [SecretScan] MaxFileWorkers → mặc
    định 2. Ở "Tìm nhanh" mỗi file chỉ cần 1 trang OCR, nên để dùng hết
    pool N worker OCR thì số file-worker cần cùng cỡ N (2 file-worker chỉ
    bận 2/4 worker của pool MaxConcurrentOCR=4). Ở "Tìm kỹ" streaming,
    1 file-worker đã đẩy nguyên MỘT LƯỢT (= số worker pool) trang vào
    pool, nên 2 file-worker là đủ để chồng việc chuẩn bị (mở/xoay/cắt
    lượt) của file này với OCR của file kia — tăng cao hơn không làm OCR
    nhanh hơn (pool mới là nút cổ chai), chỉ tốn thêm RAM.

    Mỗi thread tự mở PDF riêng bằng PyMuPDF trong cùng process (Document
    không chia sẻ chéo thread). Stress-test trên wheel 1.26.7 ổn định,
    nhưng nếu cần loại trừ nghi vấn xung đột PyMuPDF đa luồng (vd máy
    đang crash 0xc0000409), đặt MaxFileWorkers=1 — scan chạy tuần hoàn
    toàn phần, chậm hơn nhưng không còn fitz đa luồng nào.
    """
    want = 0
    raw_env = os.environ.get("SECRET_SCAN_MAX_FILE_WORKERS")
    if raw_env:
        try:
            want = int(raw_env)
        except ValueError:
            want = 0
    if not want:
        want = _settings_file_worker_count()
    if not want:
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
    *,
    page_limit: int = 0,
    run_id: str | None = None,
    kie_spool_cb=None,
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
      - ``first_page_only=False, page_limit=0`` ("Tìm kỹ"): preprocess hướng
        trang toàn file, process ALL pages, detect secrecy page-by-page và
        DỪNG NGAY khi gặp trang đầu tiên có dấu — các trang sau không được
        xét nữa. Trang có dấu được LightGBM xác nhận là trang đầu văn bản;
        bị loại thì file trả về không có dòng.
      - ``first_page_only=False, page_limit=N>0`` ("Tìm kỹ N trang đầu"):
        như "Tìm kỹ" nhưng chỉ cắt tối đa N trang đầu để xoay/OCR; phần còn
        lại của file không bị đụng tới. Dừng sớm như trên vẫn áp dụng.
    """
    os.makedirs(file_work_dir, exist_ok=True)
    _precheck_source_readable(source_path)
    ext = os.path.splitext(source_path)[1].lower()
    word_document = ext in {".doc", ".docx"}
    if first_page_only:
        mode = "Trang đầu"
    elif page_limit and int(page_limit) > 0:
        mode = f"Tìm kỹ {int(page_limit)} trang đầu"
    else:
        mode = "Tìm kỹ"
    # Cửa sổ trang: chỉ cắt tối đa N trang đầu để xoay/OCR (page_limit > 0).
    windowed = (not first_page_only) and bool(page_limit and int(page_limit) > 0)
    # Dừng sớm: MỌI chế độ Tìm kỹ đều kết luận ở trang dấu đầu tiên.
    stop_first = not first_page_only
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
        elif windowed:
            page_indices = _thorough_page_window(
                len(page_indices), int(page_limit or 0)
            ) or [0]
            note = f"{note}; DOC/DOCX tối đa {len(page_indices)} trang đầu"
        matches = _collect_secret_matches(
            native_canonical,
            page_indices,
            source_path=source_path,
            relative_path=relative_path,
            mode=mode,
            artifact_path=canonical_json_path,
            note=note,
            cancel_event=cancel_event,
            stop_on_first=stop_first,
        )
        # Cơ quan dự phòng TRƯỚC, giải mật SAU: pass cơ quan là bên quyết định
        # có infer KIE cho trang hay không — pass năm đọc lại cache, một lần
        # infer lấy đủ năm + cơ quan ban hành + cơ quan cấp trên.
        _apply_kie_org_fallback(matches, canonical_json_path)
        _apply_declassification_notes(
            matches, native_canonical, canonical_json_path
        )
        # GĐ K: nhánh DOCX giữ KIE đồng bộ (deferred chỉ áp PDF quick/thorough).
        return SecretScanArtifact(
            matches=matches,
            source_pdf="",
            canonical=native_canonical,
            rotations=None,
            status="confirmed_positive" if matches else "clean",
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
        # "Tìm kỹ" (page_limit=0): mọi trang. "Tìm kỹ N trang đầu": chỉ cửa
        # sổ N trang đầu — phần còn lại của file không bị đụng tới.
        page_indices = _thorough_page_window(total_pages, int(page_limit or 0))

    # For single-page PDFs, no need to extract a sub-PDF — classify + OCR in place.
    scan_pdf = source_pdf
    if first_page_only and ext == ".pdf" and total_pages > 1:
        first_pdf = os.path.join(file_work_dir, "first_page.pdf")
        scan_pdf = _extract_pdf_pages(source_pdf, first_pdf, [0])
        page_indices = [0]
    elif windowed and total_pages > len(page_indices):
        window_pdf = os.path.join(file_work_dir, "window_pages.pdf")
        scan_pdf = _extract_pdf_pages(source_pdf, window_pdf, page_indices)
        log_cb(
            f"Chế độ giới hạn trang: chỉ cắt {len(page_indices)} trang đầu "
            "để xoay/OCR, phần còn lại của file không được xét."
        )

    note = source_note
    if first_page_only:
        # "Tìm nhanh": 1 trang — xoay + OCR + dò như cũ (chi phí không đáng kể).
        rotations: list[int] | None = None
        if (
            os.environ.get("SECRET_SCAN_DISABLE_ROTATE", "").lower()
            not in {"1", "true"}
        ):
            if _pdf_file_kind(scan_pdf) != "digital":
                log_cb(
                    "Xoay đúng chiều trang trước khi dò (như Số hóa lưu trữ)..."
                )
                pre_pdf = os.path.join(file_work_dir, "preprocessed.pdf")
                pre_out, rotations = _preprocess_pdf_for_ocr(
                    scan_pdf,
                    pre_pdf,
                    log_cb,
                    max_workers=ocr_workers,
                    cancel_event=cancel_event,
                )
                if os.path.abspath(pre_out) != os.path.abspath(scan_pdf):
                    scan_pdf = pre_out
                    note = f"{note}; đã xoay đúng chiều"
        canonical = _process_pdf_per_page(
            scan_pdf,
            source_path,
            page_indices,
            cancel_event,
            log_cb,
            dpi=_SECRET_SCAN_DPI,
            json_path=canonical_json_path,
        )
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
        # Cơ quan dự phòng trước, giải mật sau — xem chú thích nhánh Word.
        # GĐ K: PDF Tìm nhanh có thể deferred (OCRTOOL_KIE_DEFERRED=1).
        deferred_kie = _try_deferred_kie(
            matches, canonical, canonical_json_path, run_id=run_id,
            kie_spool_cb=kie_spool_cb,
            artifact_status="confirmed_positive" if matches else "clean",
        )
        if deferred_kie is None:
            _apply_kie_org_fallback(matches, canonical_json_path)
            _apply_declassification_notes(matches, canonical, canonical_json_path)
        # Tìm nhanh: dò detector trên trang đầu, KHÔNG qua LightGBM (theo
        # thiết kế) — dấu tìm thấy là mức detector, status phản ánh đúng điều đó.
        return SecretScanArtifact(
            matches=matches,
            source_pdf=scan_pdf,
            canonical=canonical,
            rotations=rotations,
            status="confirmed_positive" if matches else "clean",
            metadata_pending=deferred_kie is not None,
            kie_state=deferred_kie,
        )

    # "Tìm kỹ" (cả giới hạn 10 trang lẫn mọi trang): OCR + dò SONG SONG theo
    # từng trang; dừng sớm khi dấu ứng viên được LightGBM XÁC NHẬN. Xoay trang
    # cũng theo từng lượt (tránh xoay cả 300 trang rồi mới biết dấu ở trang 3).
    if windowed:
        note = (
            f"{note}; kiểm tối đa {len(page_indices)} trang đầu, "
            "dừng khi gặp dấu đầu tiên"
        )
    else:
        note = (
            f"{note}; kiểm tất cả {len(page_indices)} trang, "
            "dừng khi gặp dấu đầu tiên"
        )
    rotate = (
        os.environ.get("SECRET_SCAN_DISABLE_ROTATE", "").lower()
        not in {"1", "true"}
        and _pdf_file_kind(scan_pdf) != "digital"
    )
    if rotate:
        note = f"{note}; đã xoay đúng chiều"
    canonical, matches, rotations, mark_status = _process_and_detect_streaming(
        scan_pdf,
        source_path,
        relative_path,
        page_indices,
        mode=mode,
        artifact_path=canonical_json_path,
        note=note,
        cancel_event=cancel_event,
        log_cb=log_cb,
        dpi=_SECRET_SCAN_DPI,
        json_path=canonical_json_path,
        stop_on_first=stop_first,
        rotate=rotate,
        ocr_workers=ocr_workers,
        file_work_dir=file_work_dir,
    )
    # Xác nhận LightGBM đã chạy TỪNG TRANG trong streaming (không còn filter
    # hậu-hội): matches = dấu đã chấp nhận (+ ứng viên chưa xác nhận khi
    # LightGBM lỗi). Ba trạng thái: confirmed_positive / unconfirmed / clean.
    if matches and mark_status == "accepted":
        artifact_status = "confirmed_positive"
    elif mark_status == "lgbm_error_unconfirmed":
        artifact_status = "unconfirmed"
    else:
        artifact_status = "clean"
    # Cơ quan dự phòng trước, giải mật sau — xem chú thích nhánh Word.
    # GĐ K: PDF Tìm kỹ có thể deferred (OCRTOOL_KIE_DEFERRED=1) — metadata
    # điền vào matches trước khi lượt quét commit (drain cuối lượt).
    deferred_kie = _try_deferred_kie(
        matches, canonical, canonical_json_path, run_id=run_id,
        kie_spool_cb=kie_spool_cb, artifact_status=artifact_status,
    )
    if deferred_kie is None:
        _apply_kie_org_fallback(matches, canonical_json_path)
        _apply_declassification_notes(matches, canonical, canonical_json_path)
    return SecretScanArtifact(
        matches=matches,
        source_pdf=scan_pdf,
        canonical=canonical,
        rotations=rotations,
        status=artifact_status,
        metadata_pending=deferred_kie is not None,
        kie_state=deferred_kie,
    )


def scan_one_file_for_secret(
    source_path: str,
    relative_path: str,
    file_work_dir: str,
    first_page_only: bool,
    cancel_event: threading.Event,
    log_cb: Callable[[str], None],
    ocr_workers: int | None = None,
    *,
    page_limit: int = 0,
) -> list[SecretScanMatch]:
    return scan_one_file_for_secret_artifact(
        source_path=source_path,
        relative_path=relative_path,
        file_work_dir=file_work_dir,
        first_page_only=first_page_only,
        cancel_event=cancel_event,
        log_cb=log_cb,
        ocr_workers=ocr_workers,
        page_limit=page_limit,
    ).matches


EXCEL_HEADERS = [
    "STT",
    "Độ mật",
    "Tên tệp",
    "Mã cơ quan",
    "Tên cơ quan",
    "File (trong thư mục quét)",
    "Đường dẫn đầy đủ",
    "Ngày cập nhật",
    "Trang",
    "Chế độ",
    "Đáp ứng giải mật",
    "Ghi chú",
]

_EXCEL_COL_WIDTHS = [6, 12, 30, 14, 34, 42, 62, 17, 8, 12, 26, 42]

# Giá trị cột "Đáp ứng giải mật": CHỈ 2 trạng thái — "Đáp ứng (năm, N năm)"
# khi đã đủ thời hạn giải mật, và TRỐNG khi chưa đạt / chưa xác định được.
_DECLASS_CELL_DUE_RE = re.compile(r"^Đáp ứng \((\d{4}), (\d{1,2}) năm\)$")
_DECLASS_NOTE_FIELDS_RE = re.compile(r"văn bản (\d{4}), \S+ (\d{1,2}) năm")


def _declass_cell_text(match: SecretScanMatch, current_version: str) -> str:
    if match.declass_due and match.issue_year and match.declass_years:
        return f"Đáp ứng ({match.issue_year}, {match.declass_years} năm)"
    return ""


def _match_is_inherited(match: SecretScanMatch, current_version: str) -> bool:
    """Dòng kế thừa chưa cập nhật: không có dữ liệu giải mật VÀ không do
    version hiện tại sinh ra (R8 — không tính 0/False mặc định là kết luận;
    chỉ dùng cho thống kê trạng thái, không ghi ra Excel)."""
    return not match.issue_year and (match.source_version or "") != current_version


# ---------------------------------------------------------------------------
# Mã định danh cơ quan từ tên file "<uuid>_<mã>-<text>.<ext>"
# ---------------------------------------------------------------------------

_UUID_FILENAME_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{12}_(.+)$"
)

_ORG_LOOKUP_CACHE: dict[str, str] | None = None


def _org_name_lookup() -> dict[str, str]:
    """Bảng mã định danh → tên cơ quan, nạp 1 lần từ madinhdanh_lookup.json.

    Tìm qua ``get_resource_path``: ưu tiên file đặt CẠNH EXE (người dùng có
    thể thay danh mục mới hơn mà không cần build lại), fallback về bản đóng
    gói trong thư mục bundle ``_internal`` của PyInstaller. File thiếu/hỏng
    → bảng rỗng (cột Tên cơ quan để trống), không bao giờ làm fail lượt quét.
    """
    global _ORG_LOOKUP_CACHE
    if _ORG_LOOKUP_CACHE is None:
        cache: dict[str, str] = {}
        try:
            path = get_resource_path("madinhdanh_lookup.json")
            with open(path, encoding="utf-8") as fh:
                records = json.load(fh)
            for record in records or []:
                code = str(record.get("ma_dinh_danh") or "").strip()
                name = str(record.get("ten_co_quan") or "").strip()
                if code:
                    cache[code] = name
        except Exception:
            cache = {}
        _ORG_LOOKUP_CACHE = cache
    return _ORG_LOOKUP_CACHE


def _org_code_from_filename(source_path: str) -> str:
    """Mã cơ quan trong tên file dạng "<uuid>_<mã>-<text>.<ext>": đoạn giữa
    uuid và dấu "-" đầu tiên (bỏ phần mở rộng).

    Ví dụ: 39cfea63-...-340ab5e3c6af_A29.183-A29.37.28.001-04-0010-082.pdf
    → "A29.183". Tên không theo dạng uuid → trả "" (cả hai cột để trống)."""
    name = os.path.basename(str(source_path or ""))
    found = _UUID_FILENAME_RE.match(name)
    if not found:
        return ""
    rest = os.path.splitext(found.group(1))[0]
    return rest.split("-", 1)[0].strip()


def _kie_org_name_from_fields(fields, page_index: int) -> str:
    """Tên cơ quan dự phòng từ DANH SÁCH field đã có (không infer)."""
    issuing = ""
    superior = ""
    for field in fields or []:
        if not isinstance(field, dict):
            continue
        label = str(field.get("label") or "")
        if label not in ("ISSUE_ORG_NAME", "ISSUE_ORG_SUPERIOR"):
            continue
        try:
            if int(field.get("page_index", -1)) != int(page_index):
                continue
        except (TypeError, ValueError):
            continue
        # Field multi_line nối các dòng bằng "\n" — gộp về 1 dòng trắng phân cách.
        text = re.sub(r"\s+", " ", str(field.get("text") or "")).strip()
        if not text:
            continue
        if label == "ISSUE_ORG_NAME" and not issuing:
            issuing = text
        elif label == "ISSUE_ORG_SUPERIOR" and not superior:
            superior = text
    parts = [part for part in (issuing, superior) if part]
    return " ".join(parts) + "*" if parts else ""


def _kie_org_fallback_name(canonical_json_path: str, page_index: int) -> str:
    """Tên cơ quan dự phòng "<cơ quan ban hành> <cơ quan cấp trên>*" theo KIE
    (infer nếu cache chưa có — chỉ đường ĐỒNG BỘ được gọi hàm này)."""
    return _kie_org_name_from_fields(
        _kie_fields_for_page(canonical_json_path, page_index), page_index
    )


def _org_display_name(source_path: str, kie_org_name: str) -> str:
    """Tên cơ quan cho bảng/Excel: ưu tiên tên tra theo mã định danh trong
    tên file qua madinhdanh_lookup; không tra được thì dùng tên dự phòng KIE
    bóc từ văn bản (kết thúc "*", cơ quan theo văn bản)."""
    org_code = _org_code_from_filename(source_path)
    name = _org_name_lookup().get(org_code, "") if org_code else ""
    return name or (kie_org_name or "").strip()


def _effective_org_code(match: SecretScanMatch) -> str:
    """Mã cơ quan có hiệu lực của một dòng: gõ đè tay trên bảng (nếu có)
    thì theo tay, không thì suy từ tên file "<uuid>_<mã>-…"."""
    return (
        match.org_code_override or _org_code_from_filename(match.source_path)
    ).strip()


def _effective_org_name(match: SecretScanMatch) -> str:
    """Tên cơ quan có hiệu lực của một dòng: gõ đè tay trên bảng → tên tra
    theo mã hiệu lực qua madinhdanh_lookup → dự phòng KIE (kết thúc "*")."""
    name = (match.org_name_override or "").strip()
    if name:
        return name
    code = _effective_org_code(match)
    looked = _org_name_lookup().get(code, "") if code else ""
    return looked or (match.kie_org_name or "").strip()


def _file_mtime_for_display(match: SecretScanMatch) -> float:
    """Mtime hiện thời của file ("Ngày cập nhật"); file đã bị xóa khỏi đĩa
    thì dùng mtime chụp lúc quét. Không có gì → 0 (hiển thị trống)."""
    try:
        return os.stat(match.source_path).st_mtime
    except OSError:
        return float(match.file_mtime or 0.0)


def _format_file_mtime(ts: float) -> str:
    """Epoch → "dd/MM/yyyy HH:mm" giờ địa phương; 0 → chuỗi rỗng."""
    return time.strftime("%d/%m/%Y %H:%M", time.localtime(ts)) if ts > 0 else ""


def _apply_kie_org_fallback(
    matches: list[SecretScanMatch],
    canonical_json_path: str | None,
    allow_model: bool = True,
    fields_override: dict[int, list[dict] | None] | None = None,
) -> None:
    """Điền tên cơ quan dự phòng (KIE) cho file mật không tra được tên.

    Pass này chạy TRƯỚC pass giải mật ở ``scan_one_file_for_secret_artifact``:
    khi nó phải infer cho một trang (mã định danh không tra được tên), kết
    quả được cache lại để pass năm tận dụng luôn PLACE_DATE — một lần infer
    lấy đủ năm + cơ quan ban hành + cơ quan cấp trên.

    Mã định danh trong tên file KHÔNG có trong madinhdanh_lookup (hoặc tên
    file không theo dạng uuid có mã) → nhờ KIE bóc "<cơ quan ban hành>
    <cơ quan cấp trên>*" từ chính văn bản. Chỉ chạy trên các dòng đã là mật
    (sau khi lọc); mỗi trang chứa dấu chỉ infer 1 lần (cache dùng chung với
    pass năm ban hành). Tắt bằng SECRET_SCAN_DISABLE_KIE_ORG=1.

    ``allow_model=False`` (P1-08): KHÔNG BAO GIỜ infer — chỉ dùng cache sẵn.
    Bắt buộc cho baseline-trước-enqueue và cho callback deferred (callback
    chạy trong thread coordinator; infer trong parent tại đây chính là cửa
    KIE-đồng-bộ-ngược lài làm `wait_all` mất ý nghĩa).

    ``fields_override`` (R3-03): fields của job truyền TRỰC TIẾP — cache
    chỉ là tối ưu, không phải nơi giữ duy nhất của kết quả bắt buộc (giữa
    store và apply, writer khác có thể clear cache).
    """
    if not matches:
        return
    if os.environ.get("SECRET_SCAN_DISABLE_KIE_ORG", "").lower() in {"1", "true"}:
        return
    canonical_json_path = str(canonical_json_path or "")
    if not canonical_json_path or not os.path.exists(canonical_json_path):
        return
    try:
        lookup = _org_name_lookup()
    except Exception:
        return  # đã có try bên trong, giữ cho chắc: không fail lượt quét
    name_by_page: dict[int, str] = {}
    for match in matches:
        try:
            if match.kie_org_name:
                continue
            org_code = _org_code_from_filename(match.source_path)
            if org_code and lookup.get(org_code):
                continue  # tra được tên theo danh mục → không cần dự phòng
            page_idx = _safe_page_index(match.page_number)
            if page_idx not in name_by_page:
                override = (
                    fields_override.get(page_idx, _KIE_CACHE_MISS)
                    if fields_override is not None
                    else _KIE_CACHE_MISS
                )
                if override is not _KIE_CACHE_MISS:
                    # R3-03: fields của CHÍNH job này — ưu tiên cao nhất.
                    name_by_page[page_idx] = _kie_org_name_from_fields(
                        override, page_idx
                    )
                elif allow_model:
                    name_by_page[page_idx] = _kie_org_fallback_name(
                        canonical_json_path, page_idx
                    )
                else:
                    # KHÔNG infer — chỉ đọc cache (applier deferred khi job
                    # không trả fields cho trang này).
                    cached = _kie_cached_fields(canonical_json_path, page_idx)
                    name_by_page[page_idx] = (
                        _kie_org_name_from_fields(cached, page_idx)
                        if cached is not _KIE_CACHE_MISS else ""
                    )
            match.kie_org_name = name_by_page[page_idx]
        except Exception:
            # Một dòng rác không được kéo sập cả file: bỏ qua dự phòng của
            # dòng đó, giữ nguyên các dòng đã điền.
            continue


def _kie_needed_pages(
    matches: list[SecretScanMatch],
    canonical: dict,
    canonical_json_path: str | None,
) -> set[int]:
    """P1-08 (planner): CHỈ các trang THỰC SỰ cần model KIE — giữ nguyên các
    điều kiện skip của hai pass (tắt bằng SECRET_SCAN_DISABLE_KIE_ORG/_YEAR,
    tên cơ quan tra được theo danh mục, ngày rõ < 3 ứng viên). Trang đã có
    cache field (đã infer trước đó) cũng không cần job."""
    org_enabled = (
        os.environ.get("SECRET_SCAN_DISABLE_KIE_ORG", "").lower()
        not in {"1", "true"}
    )
    year_enabled = (
        os.environ.get("SECRET_SCAN_DISABLE_KIE_YEAR", "").lower()
        not in {"1", "true"}
    )
    if not org_enabled and not year_enabled:
        return set()
    try:
        lookup = _org_name_lookup()
    except Exception:
        lookup = {}
    needed: set[int] = set()
    for match in matches:
        page_idx = _safe_page_index(match.page_number)
        if page_idx in needed:
            continue
        cached = _kie_cached_fields(canonical_json_path or "", page_idx)
        already = cached is not _KIE_CACHE_MISS
        if not already and org_enabled and not match.kie_org_name:
            org_code = _org_code_from_filename(match.source_path)
            if not (org_code and lookup.get(org_code)):
                needed.add(page_idx)
                continue
        if not already and year_enabled:
            page = (
                _page_for_index(canonical, page_idx)
                if isinstance(canonical, dict)
                else None
            )
            candidates = _issue_date_candidates(page) if page else []
            if len(candidates) >= _DECLASS_KIE_MIN_CANDIDATES:
                needed.add(page_idx)
    return needed


def _kie_refused_state(reason: str, error_code: str = "deferred_refused") -> dict:
    """D3: trạng thái 'deferred từ chối' — metadata CHƯA đủ (baseline regex
    đã áp), KHÔNG infer trong parent, KHÔNG cache-final; commit đặt nghĩa vụ
    quét lại. Job KHÔNG nằm trong coordinator nên drain không chờ nó."""
    return {
        "applied": False,
        "error_code": error_code,
        "refused_reason": str(reason or ""),
        "registered": False,
        "on_applied": None,
        "lock": threading.Lock(),
    }


def _kie_spool_prepare(canonical_json_path: str, rel: str, mode_key: str,
                       run_id: str) -> dict | None:
    """Reserve retained storage before copying; failures remain resumable."""
    from scanindex.core.secret_scan_spool import prepare_snapshot
    return prepare_snapshot(
        canonical_json_path, rel, mode_key, run_id,
        cancel=getattr(_scan_run_ctx, "cancel_event", None),
        stop=getattr(_scan_run_ctx, "producer_stop", None),
    )


def _try_deferred_kie(
    matches: list[SecretScanMatch],
    canonical: dict,
    canonical_json_path: str | None,
    run_id: str | None = None,
    *,
    needed_override=None,
    kie_spool_cb=None,
    artifact_status: str = "confirmed_positive",
) -> dict | None:
    """GĐ K: bàn giao KIE cho coordinator (pool process session riêng) khi
    bật ``OCRTOOL_KIE_DEFERRED=1``. Trả **state dict** nếu ĐÃ nhận (caller
    BỎ đường đồng bộ và HOÃN commit file tới khi coordinator drain; state
    báo ``applied`` khi metadata đã áp — commit theo ACK từng file, P1-09);
    trả **None** nếu giữ đường đồng bộ (không nhận / không trang nào cần
    model / lỗi submit).

    P1-08: baseline regex/ghi chú được áp TRƯỚC khi enqueue (không model) và
    chỉ enqueue các trang thật sự cần model — model thiếu/worker lỗi GIỮ
    nguyên baseline như đường đồng bộ khi model lỗi, không mất dữ liệu.
    Callback KHÔNG BAO GIỜ infer trong parent (không KIE đồng bộ ngược).

    D3: coordinator TỪ CHỐI (credit/budget đầy, executor chết) → backpressure
    hủy được rồi trạng thái `_kie_refused_state` — KHÔNG BAO GIỜ fallback
    infer trong parent.

    D2: ``kie_spool_cb`` ({"prepare": fn(canonical_path)->info|None,
    "commit": fn(info)}) — chép canonical snapshot vào spool bền TRƯỚC khi
    submit (job đọc spool copy) và ghi nghĩa vụ `kspool` vào journal SAU khi
    nhận — restart chỉ resubmit KIE (OCR-call = 0) khi snapshot còn hợp lệ.

    Kết luận về dấu không phụ thuộc đường này; không bao giờ fail lượt quét."""
    request_stop = getattr(_scan_run_ctx, "request_producer_stop", None)
    producer_stop = getattr(_scan_run_ctx, "producer_stop", None)
    try:
        from scanindex.core import secret_scan_kie as ssk

        if not ssk.deferred_enabled() or not matches or not canonical_json_path:
            return None
        if needed_override is not None:
            needed = {int(o) for o in needed_override}
        else:
            needed = _kie_needed_pages(matches, canonical, canonical_json_path)
        if not needed:
            # Không trang nào cần model: đường đồng bộ (regex/lookup) tự xử
            # — trước đây vẫn tạo job rồi bỏ cả hai pass khi model lỗi.
            return None
        # P2-3: run_id của lượt (thread file-worker set trước khi scan).
        run_id = run_id or str(getattr(_scan_run_ctx, "run_id", "") or "")
        # Baseline KHÔNG model trước khi bàn giao: năm rõ/ghi chú regex có
        # sẵn như đường đồng bộ khi model lỗi (sync-on-error == pure regex).
        _apply_declassification_notes(
            matches, canonical, canonical_json_path, allow_model=False
        )
        # Reserve durable storage before copying/submitting. Failure preserves
        # detection as resumable and stops this producer.
        spool_info = None
        if kie_spool_cb is not None:
            spool_info = kie_spool_cb["prepare"](canonical_json_path)
        coord = ssk.ensure_started()
        state: dict = {
            "applied": False,      # metadata KIE đã áp xong lên match
            "error_code": None,
            "registered": False,   # caller đã đăng ký entry commit
            "on_applied": None,    # do caller gắn: commit từng file sau ACK
            "lock": threading.Lock(),
        }
        payload = {
            "job_id": f"k{time.monotonic_ns()}",
            "generation": 0,
            "canonical_path": str(
                spool_info["canonical"] if spool_info else canonical_json_path
            ),
            "selected_ordinals": sorted(needed),
            "model_fp": "",
            "run_id": run_id or "",
        }

        def _deliver() -> None:
            deliver = None
            with state["lock"]:
                if state["registered"]:
                    deliver = state.get("on_applied")
            if deliver is not None:
                try:
                    deliver()
                except Exception:
                    logging.getLogger(__name__).exception(
                        "KIE deferred commit-theo-ACK lỗi (job %s)",
                        payload["job_id"],
                    )

        def _on_done(result: dict) -> None:
            try:
                if result.get("status") == "ready":
                    # R3-03: fields của job giữ trong DỮ LIỆU CỤC BỘ và truyền
                    # thẳng cho hai pass — cache toàn cục chỉ còn là tối ưu
                    # (giữa store và apply, writer khác có thể chạm ngưỡng
                    # clear cache; applier allow_model=False không được rơi
                    # về regex rồi vẫn đánh dấu applied). Ghi cache vẫn làm
                    # để các lượt/tệp khác tận dụng (R2-06: qua helper giới
                    # hạn; job nhiều trang tự eviction KHÔNG còn ảnh hưởng
                    # kết quả vì áp từ dữ liệu cục bộ).
                    fields_local: dict[int, list[dict] | None] = {}
                    for src, fields in (
                        result.get("fields_by_source") or {}
                    ).items():
                        try:
                            fields_local[int(src)] = fields
                        except (TypeError, ValueError):
                            continue
                        _kie_cache_store(canonical_json_path, int(src), fields)
                    _apply_kie_org_fallback(
                        matches, canonical_json_path, allow_model=False,
                        fields_override=fields_local,
                    )
                    _apply_declassification_notes(
                        matches, canonical, canonical_json_path,
                        allow_model=False,
                        fields_override=fields_local,
                    )
                    # Chốt applied SAU khi đã dùng ĐẦY ĐỦ kết quả job.
                    state["applied"] = True
                else:
                    state["error_code"] = result.get("error_code")
                    if result.get("error_code") == "model_missing" and request_stop is not None:
                        request_stop("Không nạp được model KIE; giữ tiến độ để tiếp tục sau khi sửa model")
                    logging.getLogger(__name__).warning(
                        "KIE deferred lỗi (%s: %s) — giữ baseline regex đã "
                        "áp; file sẽ được quét lại lần sau để có metadata "
                        "đầy đủ",
                        result.get("error_code"), result.get("error"),
                    )
            except Exception:
                logging.getLogger(__name__).exception(
                    "KIE deferred on_done lỗi — giữ baseline hiện có"
                )
            finally:
                # Lease canonical do COORDINATOR sở hữu và tự nhả khi terminal
                # (kể cả cancel/error/dispatch-fail) — callback không nhả hộ.
                _deliver()

        def _submit_bounded() -> tuple[bool, str | None]:
            """D3: backpressure CÓ THỂ HỦY khi credit đầy — chờ có giới hạn
            (`OCRTOOL_KIE_REFUSED_WAIT_S`, mặc định 30 s) cho slot trống rồi
            thử lại ĐÚNG MỘT lần. KHÔNG bao giờ fallback infer trong parent:
            hết thời gian → trạng thái `refused` (metadata chưa đủ → nghĩa vụ
            quét lại), giữ trần process/thread và không có KIE ẩn."""
            wait_s = 30.0
            raw = os.environ.get("OCRTOOL_KIE_REFUSED_WAIT_S", "").strip()
            if raw:
                try:
                    wait_s = max(0.0, float(raw))
                except ValueError:
                    pass
            cancel = getattr(_scan_run_ctx, "cancel_event", None)
            deadline = time.monotonic() + wait_s
            accepted, reason = coord.submit(payload, _on_done)
            while not accepted and time.monotonic() < deadline:
                if ((cancel is not None and cancel.is_set())
                        or (producer_stop is not None and producer_stop.is_set())
                        or str(reason or "").startswith("model_missing:")):
                    break
                time.sleep(0.25)
                # Retry theo nhịp poll tới hết budget chờ — mỗi lần từ chối
                # coordinator tự nhả lease (không tích tài nguyên).
                accepted, reason = coord.submit(payload, _on_done)
            return accepted, reason

        accepted, reason = _submit_bounded()
        if not accepted:
            if str(reason or "").startswith("model_missing:") and request_stop is not None:
                request_stop("Không nạp được model KIE; giữ tiến độ để tiếp tục sau khi sửa model")
            logging.getLogger(__name__).warning(
                "KIE deferred TỪ CHỐI sau backpressure (%s) — KHÔNG infer "
                "trong parent: file giữ baseline regex + nghĩa vụ quét lại "
                "khi còn slot", reason,
            )
            if spool_info and kie_spool_cb is not None:
                try:
                    kie_spool_cb["discard"](spool_info)
                except Exception:
                    pass
            return _kie_refused_state(reason)
        if spool_info and kie_spool_cb is not None:
            try:
                # D2: nghĩa vụ KIE-only ghi bền SAU khi nhận job — restart
                # resubmit KIE-only với ĐÚNG spool snapshot job đã đọc.
                kie_spool_cb["commit"]({
                    **spool_info,
                    "ordinals": sorted(needed),
                    "matches": [asdict(m) for m in matches],
                    "fp": list(spool_info.get("fp") or []),
                    "status": artifact_status,
                })
            except Exception:
                logging.getLogger(__name__).exception(
                    "kie spool commit lỗi — restart sẽ quét lại toàn bộ "
                    "(không mất đúng/sai, chỉ mất tối ưu KIE-only)"
                )
        return state
    except Exception as exc:
        from scanindex.core.secret_scan_spool import SpoolBudgetError
        if isinstance(exc, (SpoolBudgetError, OSError)):
            if request_stop is not None:
                request_stop(str(exc))
            return _kie_refused_state(str(exc), "spool_unavailable")
        logging.getLogger(__name__).exception(
            "KIE deferred submit lỗi — trạng thái refused (không infer "
            "trong parent; file sẽ quét lại lần sau)"
        )
        return _kie_refused_state("submit exception", "deferred_submit_error")


def export_matches_to_excel(
    matches: list[SecretScanMatch],
    output_path: str,
    scan_summary: dict | None = None,
) -> str:
    """Write the scan results to a one-sheet .xlsx workbook.

    One row per detected stamp — the same rows shown in the results table —
    plus the absolute path and bare file name so the list stays usable when
    shared outside the app. "Mã cơ quan"/"Tên cơ quan" theo giá trị hiệu lực:
    gõ đè tay trên bảng (nếu có) > suy từ tên file "<uuid>_<mã>-…" > tra
    madinhdanh_lookup > dự phòng KIE bóc từ văn bản (kết thúc "*"). "Đáp ứng
    giải mật" chỉ 2 trạng thái: giá trị khi đến hạn, trống khi chưa đạt;
    chuỗi trong "Ghi chú" giữ nguyên để file nạp lại không mâu thuẫn.

    ``scan_summary`` (tuỳ chọn, từ lượt quét trong phiên): khi có, ghi thêm
    sheet "Tom tat" ĐẰNG SAU sheet danh sách — bằng chứng rà soát (thư mục,
    thời gian, số liệu, kết luận). Sheet danh sách phải luôn đứng đầu và là
    sheet active: ``load_secret_matches_from_excel`` đọc ``wb.active`` nên
    sheet tóm tắt không được phép chiếm vị trí đó.
    """
    import openpyxl
    from datetime import datetime
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    from scanindex.infra.version import get_version

    current_version = get_version()
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

    green_font = Font(color="1D7A34", bold=True)
    for idx, match in enumerate(matches, start=1):
        row = idx + 1
        org_code = _effective_org_code(match)
        ws.cell(row=row, column=1, value=idx).alignment = center
        ws.cell(row=row, column=2, value=match.keyword)
        ws.cell(row=row, column=3, value=os.path.basename(match.source_path))
        ws.cell(row=row, column=4, value=org_code)
        ws.cell(
            row=row,
            column=5,
            value=_effective_org_name(match),
        )
        ws.cell(row=row, column=6, value=match.relative_path)
        ws.cell(row=row, column=7, value=match.source_path)
        # "Ngày cập nhật": ô datetime thật (sắp xếp/lọc được trong Excel),
        # định dạng dd/MM/yyyy HH:mm; không biết ngày → để trống.
        mtime = _file_mtime_for_display(match)
        if mtime > 0:
            date_cell = ws.cell(
                row=row, column=8, value=datetime.fromtimestamp(mtime)
            )
            date_cell.number_format = "DD/MM/YYYY HH:MM"
            date_cell.alignment = center
        ws.cell(row=row, column=9, value=int(match.page_number)).alignment = center
        ws.cell(row=row, column=10, value=match.mode)
        declass_cell = ws.cell(
            row=row, column=11, value=_declass_cell_text(match, current_version)
        )
        if declass_cell.value:
            declass_cell.font = green_font
        note_cell = ws.cell(row=row, column=12, value=match.note)
        if _match_meets_declassification(match):
            note_cell.font = green_font

    for col, width in enumerate(_EXCEL_COL_WIDTHS, start=1):
        ws.column_dimensions[get_column_letter(col)].width = width
    ws.freeze_panes = "A2"
    last_col = get_column_letter(len(EXCEL_HEADERS))
    ws.auto_filter.ref = f"A1:{last_col}{max(1, len(matches) + 1)}"

    if scan_summary:
        _write_summary_sheet(wb, scan_summary, len(matches))

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    wb.save(output_path)
    return output_path


# Cặp (nhận các key của scan_summary, tiêu đề hiển thị). Thứ tự hiển thị
# trên sheet "Tom tat"; key thiếu trong dict thì bỏ dòng.
_SUMMARY_ROWS = [
    ("folder", "Thư mục rà soát"),
    ("started", "Thời điểm bắt đầu"),
    ("finished", "Thời điểm kết thúc"),
    ("mode", "Chế độ quét"),
    ("status", "Trạng thái lượt quét"),
    ("total", "Tổng số file trong thư mục"),
    ("scanned", "File quét mới"),
    ("skipped", "File kế thừa lượt quét trước"),
    ("cache_skipped", "File bỏ qua nhờ cache (không đổi)"),
    ("junk", "File hỏng bỏ qua"),
    ("failures", "File lỗi"),
    ("updated", "File mật cập nhật kết quả"),
    ("kept_old", "File mật giữ kết quả cũ"),
    ("found", "Số dòng mật phát hiện"),
    ("app_version", "Phiên bản ứng dụng"),
]

# Sheet tóm tắt dùng ASCII như sheet "Van ban mat" (nhất quán, an toàn tên
# sheet qua các bản Excel/openpyxl).
_SUMMARY_SHEET = "Tom tat"


def _scan_mode_label(mode_key: str) -> str:
    """Nhãn chế độ quét cho sheet tóm tắt (khớp item của mode_combo)."""
    return {
        "fast": "Tìm nhanh (trang đầu)",
        "thorough10": "Tìm kỹ 10 trang đầu",
        "thorough": "Tìm kỹ (mọi trang)",
    }.get(mode_key, str(mode_key or ""))


def _summary_conclusion(summary: dict, found: int) -> str:
    """Câu kết luận của minh chứng rà soát — trạng thái dở phải thành thật."""
    status = str(summary.get("status") or "")
    if status and status != "Hoàn tất":
        return (
            f"Lượt quét {status.lower()} — file minh chứng chưa đủ điều kiện "
            "làm kết luận cuối cùng"
        )
    if found:
        return f"Phát hiện {found} dòng văn bản mật (xem sheet danh sách)"
    return "Không phát hiện văn bản mật nào trong thư mục rà soát"


def _write_summary_sheet(wb, summary: dict, found: int) -> None:
    """Ghi sheet "Tom tat" — bằng chứng lượt quét, đứng SAU sheet danh sách.

    Key/value mỗi dòng một cặp để dễ đọc in; chỉ nhận key có trong dict nên
    lượt quét cũ thiếu trường mới vẫn xuất được mà không lỗi.
    """
    from openpyxl.styles import Alignment, Font

    ws = wb.create_sheet(_SUMMARY_SHEET)
    bold = Font(bold=True)
    ws.cell(row=1, column=1, value="Hạng mục").font = bold
    ws.cell(row=1, column=2, value="Giá trị").font = bold

    row = 2
    for key, label in _SUMMARY_ROWS:
        if key not in summary or summary[key] in (None, ""):
            continue
        value = summary[key]
        if key == "mode":
            value = _scan_mode_label(str(value))
        ws.cell(row=row, column=1, value=label)
        ws.cell(row=row, column=2, value=value)
        row += 1
    conclusion = _summary_conclusion(summary, found)
    ws.cell(row=row, column=1, value="Kết luận").font = bold
    concl_cell = ws.cell(row=row, column=2, value=conclusion)
    if not found:
        concl_cell.font = Font(bold=True, color="1D7A34")

    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 72
    ws.cell(row=row, column=2).alignment = Alignment(
        horizontal="left", vertical="center", wrap_text=True
    )


def _norm(path: str) -> str:
    """Chuẩn hóa key so sánh đường dẫn (cột checkbox / xóa theo file).
    Phải giữ ĐỒNG NHẤT công thức với ``secret_scan_progress._norm_path``."""
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


# Cột bảng cho phép sửa TRỰC TIẾP (double-click / F2): Độ mật, Mã CQ,
# Cơ quan, Ghi chú — kết quả sửa được ghi NGAY vào match của dòng nên
# "Xuất Excel" dùng luôn giá trị mới. Các cột còn lại chỉ xem.
_EDITABLE_COLS = frozenset({1, 3, 4, 8})


def _match_scan_root(match: SecretScanMatch) -> str:
    """Thư mục quét gốc của MỘT dòng (xem ``_derive_scan_root``)."""
    return _derive_scan_root([match])


def _dedupe_matches(
    matches: list[SecretScanMatch],
) -> tuple[list[SecretScanMatch], int]:
    """Bỏ dòng trùng khi gộp nhiều danh sách: cùng file (đường dẫn chuẩn
    hóa) + cùng trang + cùng độ mật là cùng một dấu → giữ dòng đầu tiên.
    Trả về (danh sách đã lọc, số dòng bị bỏ)."""
    unique: list[SecretScanMatch] = []
    seen: set[tuple[str, int, str]] = set()
    for m in matches:
        key = (
            _norm(m.source_path),
            int(m.page_number or 1),
            (m.keyword or "").strip(),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(m)
    return unique, len(matches) - len(unique)


def _clear_translatable_item(item) -> None:
    """Gỡ binding dịch tự động khỏi một item bảng (UserRole+197/198/199).

    Gọi khi người dùng SỬA TAY nội dung ô (Ghi chú) để không bị
    ``retranslate_widget_tree`` dịch đè lên nội dung đã sửa."""
    try:
        from PySide6.QtCore import Qt as _Qt

        role = int(_Qt.ItemDataRole.UserRole) + 197
        item.setData(role, None)
        item.setData(role + 1, None)
        item.setData(role + 2, None)
    except Exception:
        pass


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


def build_secret_copy_plan(
    matches: list[SecretScanMatch],
) -> list[tuple[str, str]]:
    """Lập kế hoạch "Sao chép": mỗi FILE mật đúng một lần, giữ thứ tự bảng.

    Một file có thể chiếm nhiều dòng bảng (mỗi dấu/trang một dòng) — gộp
    theo đường dẫn đã chuẩn hóa, giữ dòng đầu tiên. ``relative_path`` (con
    đường tương đối của file dưới thư mục quét gốc, ghi sẵn trong từng
    dòng) quyết định cấu trúc thư mục con ở nơi nhận; rỗng/tuyệt đối/leo
    ra ngoài gốc ("..") thì lùi về tên file để không chép lộn chỗ.
    Trả về danh sách (đường dẫn nguồn, đường dẫn tương đối).
    """
    plan: list[tuple[str, str]] = []
    seen: set[str] = set()
    for m in matches:
        src = m.source_path or ""
        if not src:
            continue
        key = _norm(src)
        if key in seen:
            continue
        seen.add(key)
        rel = os.path.normpath((m.relative_path or "").strip())
        if not rel or rel == "." or os.path.isabs(rel) or rel.startswith(".."):
            rel = os.path.basename(src)
        plan.append((src, rel))
    return plan


def _long_path(p: str) -> str:
    """Tiền tố \\?\\ cho đường dẫn dài (>260 ký tự) trên Windows."""
    p = os.path.abspath(p)
    if os.name == "nt" and not p.startswith("\\\\?\\"):
        return "\\\\?\\" + p
    return p


def _secret_copy_one(src: str, dst: str, *, overwrite: bool) -> str:
    """Chép MỘT file; trả "copied" hoặc "skipped" (đích đã có, không đè).

    Ghi đè = thay hẳn nội dung đích bằng nguồn (copy2 giữ nguyên mtime
    nguồn). Đích trùng chính nguồn → lỗi (thư mục đích trùng thư mục đã
    quét). File đích read-only trên Windows → gỡ thuộc tính rồi chép lại;
    đường dẫn dài → thử lại qua \\?\\ — cùng chính sách với _permanent_delete.
    """
    if _norm(dst) == _norm(src):
        raise OSError(
            "đích trùng chính file nguồn (thư mục đích trùng thư mục đã quét)"
        )
    if not overwrite and os.path.exists(dst):
        return "skipped"
    try:
        shutil.copy2(src, dst)
        return "copied"
    except PermissionError:
        # Windows: file đích đang read-only → gỡ thuộc tính rồi thử lại.
        try:
            os.chmod(dst, stat.S_IWRITE)
            shutil.copy2(src, dst)
            return "copied"
        except OSError:
            pass
    except OSError:
        pass
    # Lần hai: sau khi gỡ read-only, hoặc qua \\?\\ cho đường dẫn dài.
    shutil.copy2(_long_path(src), _long_path(dst))
    return "copied"


def copy_secret_files(
    plan: list[tuple[str, str]],
    dest_root: str,
    *,
    overwrite: bool = True,
    progress: "Callable[[int, int, str], None] | None" = None,
) -> dict:
    """Sao chép các file mật theo kế hoạch vào ``dest_root``.

    Tạo sẵn mọi thư mục con trung gian để nơi nhận giữ nguyên cấu trúc
    như thư mục gốc. Lỗi từng file (mất nguồn, đích read-only, đường dẫn
    dài…) không dừng các file còn lại — gom lại trong ``errors``. Trả về
    dict {"total", "copied", "skipped", "errors"}: ``skipped`` đếm file bị
    bỏ qua vì đích đã có khi ``overwrite=False``; ``errors`` là danh sách
    mô tả "nguồn\\n→ đích\\n lỗi".

    Hàm thuần (không đụng Qt) — chạy được cả ở thread nền lẫn trong test.
    """
    total = len(plan)
    copied = skipped = 0
    errors: list[str] = []
    done = 0
    for src, rel in plan:
        done += 1
        dst = os.path.join(dest_root, rel)
        try:
            parent = os.path.dirname(dst)
            if parent:
                os.makedirs(parent, exist_ok=True)
            if _secret_copy_one(src, dst, overwrite=overwrite) == "copied":
                copied += 1
            else:
                skipped += 1
        except OSError as exc:
            errors.append(f"{src}\n→ {dst}\n{exc}")
        if progress is not None:
            progress(done, total, os.path.basename(dst))
    return {
        "total": total,
        "copied": copied,
        "skipped": skipped,
        "errors": errors,
    }


def load_secret_matches_from_excel(xlsx_path: str) -> list[SecretScanMatch]:
    """Đọc lại danh sách do ``export_matches_to_excel`` ghi ra.

    Chấp nhận thiếu cột "Chế độ"/"Ghi chú" (đặt giá trị mặc định), nhưng
    bắt buộc có "STT" + "Đường dẫn đầy đủ" để tránh nạp nhầm file Excel
    khác. Trả về danh sách match theo đúng thứ tự trong file.
    """
    import openpyxl
    from datetime import datetime

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
        c_dec = col("Đáp ứng giải mật")
        c_note = col("Ghi chú", 7)
        c_orgcode = col("Mã cơ quan")
        c_org = col("Tên cơ quan")
        c_date = col("Ngày cập nhật")

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
            note = str(cell(r, c_note) or "").strip()
            # Cột "Đáp ứng giải mật" (file mới): CHỈ nhận "Đáp ứng (năm, N
            # năm)", trống = chưa đạt; file cũ (không cột) fallback bóc từ
            # chuỗi Ghi chú để round-trip không mâu thuẫn.
            issue_year = declass_years = 0
            declass_due = False
            source_version = "loaded"  # có dữ liệu giải mật → không phải kế thừa
            declass_text = str(cell(r, c_dec) or "").strip()
            found_cell = _DECLASS_CELL_DUE_RE.match(declass_text)
            if found_cell:
                issue_year = int(found_cell.group(1))
                declass_years = int(found_cell.group(2))
                declass_due = True
            elif DECLASS_NOTE_LABEL in note:
                found_note = _DECLASS_NOTE_FIELDS_RE.search(note)
                if found_note:
                    issue_year = int(found_note.group(1))
                    declass_years = int(found_note.group(2))
                    declass_due = True
            if not issue_year:
                source_version = ""
            # Chỉ round-trip tên cơ quan dự phòng KIE (kết thúc "*"): tên
            # tra được từ danh mục thì để tra lại lúc hiển thị, không giữ
            # bản sao cũ lỡ danh mục đã đổi.
            org_cell = str(cell(r, c_org) or "").strip()
            # Mã/tên cơ quan KHÁC với suy động hiện tại (tên file + danh
            # mục) là dấu vết sửa tay lần trước → giữ lại để round-trip
            # không mất; trùng thì bỏ trống để luôn theo giá trị mới nhất.
            derived_code = _org_code_from_filename(source)
            code_cell = str(cell(r, c_orgcode) or "").strip()
            org_code_override = (
                code_cell if code_cell and code_cell != derived_code else ""
            )
            org_name_override = ""
            if org_cell and not org_cell.endswith("*"):
                derived_name = (
                    _org_name_lookup().get(derived_code, "")
                    if derived_code
                    else ""
                )
                if org_cell != derived_name:
                    org_name_override = org_cell
            # "Ngày cập nhật": ưu tiên ô datetime thật của openpyxl; chuỗi
            # tay ("dd/mm/yyyy hh:mm" hoặc "dd/mm/yyyy") parse lại; lỗi → 0.
            file_mtime = 0.0
            raw_date = cell(r, c_date)
            if isinstance(raw_date, datetime):
                try:
                    file_mtime = float(raw_date.timestamp())
                except (OSError, OverflowError, ValueError):
                    file_mtime = 0.0
            elif raw_date:
                for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y"):
                    try:
                        file_mtime = float(
                            datetime.strptime(str(raw_date).strip(), fmt).timestamp()
                        )
                        break
                    except ValueError:
                        continue
            matches.append(
                SecretScanMatch(
                    source_path=source,
                    relative_path=rel,
                    keyword=str(cell(r, c_key) or "").strip(),
                    page_number=max(1, page),
                    mode=str(cell(r, c_mode) or "load").strip() or "load",
                    ocr_pdf_path="",
                    note=note,
                    issue_year=issue_year,
                    declass_years=declass_years,
                    declass_due=declass_due,
                    source_version=source_version,
                    kie_org_name=org_cell if org_cell.endswith("*") else "",
                    org_code_override=org_code_override,
                    org_name_override=org_name_override,
                    file_mtime=file_mtime,
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


class _CheckAllHeader(QHeaderView):
    """Header ngang có ô check "chọn tất cả" vẽ ngay trong ô cột 0 (cột
    checkbox của bảng).

    Bấm ô check: tích / bỏ tích MỌI DÒNG ĐANG HIỆN (dòng bị ẩn bởi bộ lọc
    nhanh không bị động đến). Trạng thái ô header do màn hình cập nhật:
    tích hết → checked, một phần → ba trạng thái, không có → unchecked.
    """

    check_toggled = Signal(bool)  # True = check hết các dòng đang hiện

    def __init__(self, parent=None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.setSectionsClickable(True)
        self._check_state = Qt.CheckState.Unchecked

    def check_state(self) -> Qt.CheckState:
        return self._check_state

    def set_check_state(self, state: Qt.CheckState) -> None:
        if state != self._check_state:
            self._check_state = state
            self.updateSection(0)

    def paintSection(self, painter, rect, logical_index) -> None:
        super().paintSection(painter, rect, logical_index)
        if logical_index != 0:
            return
        style = self.style()
        opt = QStyleOptionButton()
        opt.initFrom(self)
        w = style.pixelMetric(QStyle.PixelMetric.PM_IndicatorWidth, opt, self)
        h = style.pixelMetric(
            QStyle.PixelMetric.PM_IndicatorHeight, opt, self
        )
        opt.rect = QRect(
            rect.x() + max(0, (rect.width() - w) // 2),
            rect.y() + max(0, (rect.height() - h) // 2),
            w,
            h,
        )
        opt.state |= QStyle.StateFlag.State_Enabled
        if self._check_state == Qt.CheckState.Checked:
            opt.state |= QStyle.StateFlag.State_On
        elif self._check_state == Qt.CheckState.PartiallyChecked:
            opt.state |= QStyle.StateFlag.State_NoChange
        else:
            opt.state |= QStyle.StateFlag.State_Off
        style.drawControl(QStyle.ControlElement.CE_CheckBox, opt, painter)

    def mousePressEvent(self, event) -> None:
        if self.logicalIndexAt(event.position().toPoint()) == 0:
            checked = self._check_state != Qt.CheckState.Checked
            self.set_check_state(
                Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
            )
            self.check_toggled.emit(checked)
        else:
            super().mousePressEvent(event)


class _ResultsDelegate(QStyledItemDelegate):
    """Delegate mặc định + báo lại đúng ô vừa được SỬA qua editor.

    Dùng signal ``edit_committed`` thay vì bắt ``itemChanged`` để phân biệt
    rõ "người dùng vừa sửa xong một ô" với các lần chương trình tự set text
    (thêm dòng, đổi ngôn ngữ, tô màu…) — write-back vào match chỉ xảy ra
    khi có commit thật sự từ editor."""

    edit_committed = Signal(object)  # QModelIndex

    def setModelData(self, editor, model, index) -> None:
        super().setModelData(editor, model, index)
        self.edit_committed.emit(index)


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
    # Thay thế TOÀN BỘ hàng của một file (nhánh quét lại file mật cũ):
    # payload = {"path": abs_norm, "matches": [match_dict...]}.
    _results_replace = Signal(object)
    _scan_finished = Signal(object)
    # Emitted from the background warm-up thread once the OCR pool is ready
    # (or failed); handled on the main thread by _on_pool_ready.
    _pool_ready = Signal(bool, str)
    # Kết quả convert preview từ thread nền: dict {gen, key, out_pdf, error, note}
    _preview_ready = Signal(object)
    # Kết quả sao chép file mật từ thread nền:
    # dict {"total", "copied", "skipped", "errors", "dest"}
    _copy_finished = Signal(object)

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
        # Toàn bộ file Excel đã load trong phiên (kể cả khi gộp) — "Xuất
        # Excel" không bao giờ ghi đè lên chúng.
        self._loaded_files: set[str] = set()
        # Thông tin lượt quét gần nhất trong phiên (đặt ở _on_finished):
        # cho phép "Xuất Excel" làm minh chứng ngay cả khi 0 phát hiện —
        # bị xóa trong _reset_results để không bám nhầm lần xuất sau.
        self._last_scan_summary: dict | None = None
        self._checked_paths: set[str] = set()
        # Derived UI counts, maintained alongside row insertion/removal.
        # The table remains the source of matches for actions and export.
        self._result_file_keys: set[str] = set()
        self._result_items_by_file: dict[str, list[QTableWidgetItem]] = {}
        self._checked_row_ids: set[int] = set()
        self._visible_checked_rows = 0
        self._visible_result_rows = 0
        self._pending_status: str | None = None
        self._pending_progress: tuple[int, int] | None = None
        self._feedback_timer = QTimer(self)
        self._feedback_timer.setSingleShot(True)
        self._feedback_timer.setInterval(100)
        self._feedback_timer.timeout.connect(self._flush_worker_feedback)
        self._preview_current: SecretScanMatch | None = None
        self._preview_convert_gen = 0
        self._preview_cache: dict[tuple, str] = {}
        # Lượt "Sao chép file mật" đang chạy (một lượt mỗi lần, nền thread).
        self._copy_running = False
        # PDF tạm cho preview chỉ là cache phiên — xoá sạch của lần chạy trước.
        shutil.rmtree(
            os.path.join(get_base_dir(), "temp", "secret_scan_preview"),
            ignore_errors=True,
        )
        self._build_ui()
        self._status_changed.connect(self._queue_status)
        self._progress_changed.connect(self._queue_progress)
        self._result_found.connect(self._add_result)
        self._results_replace.connect(self._on_results_replace)
        self._scan_finished.connect(self._on_finished)
        self._pool_ready.connect(self._on_pool_ready)
        self._preview_ready.connect(self._on_preview_ready)
        self._copy_finished.connect(self._on_copy_finished)

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
            translations.localize_text(
                "Chọn MỘT hoặc NHIỀU file Excel (.xlsx) danh sách văn bản mật\n"
                "đã xuất để nạp lại: bảng đang trống thì nạp luôn; đang có nội\n"
                "dung thì hỏi \"Gộp thêm\" (nối danh sách nhiều thư mục vào cùng\n"
                "bảng, dòng trùng nhau bỏ qua) hoặc \"Thay thế\". Sau khi xem/\n"
                "sửa/xóa, xuất ra file MỚI (file đã load không bao giờ bị ghi đè)."
            )
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

        self.btn_copy = QPushButton("📄  Sao chép file mật")
        self.btn_copy.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_copy.setStyleSheet(self._secondary_btn_qss())
        self.btn_copy.setEnabled(False)
        self.btn_copy.setToolTip(
            "Sao chép các file mật đang hiển thị ra một thư mục khác,\n"
            "giữ nguyên cấu trúc thư mục con như thư mục đã quét."
        )
        self.btn_copy.clicked.connect(self._copy_clicked)
        row.addWidget(self.btn_copy)

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

        # Chế độ quét: nhanh / kỹ giới hạn 10 trang / kỹ mọi trang.
        # Mặc định "Tìm kỹ (mọi trang)": bản release tải về chạy lần đầu
        # KHÔNG được bỏ sót dấu mật nằm sau trang 1 — muốn nhanh thì người
        # dùng tự chuyển "Tìm nhanh" trong phiên.
        self.mode_combo = QComboBox()
        for label, key in (
            ("Tìm nhanh (trang đầu)", "fast"),
            ("Tìm kỹ 10 trang đầu", "thorough10"),
            ("Tìm kỹ (mọi trang)", "thorough"),
        ):
            self.mode_combo.addItem(label, key)
        self.mode_combo.setCurrentIndex(
            max(0, self.mode_combo.findData("thorough"))
        )
        self.mode_combo.setToolTip(
            translations.localize_text(
                "Tìm nhanh: chỉ xét trang đầu mỗi file, không dùng LightGBM\n"
                "(rất nhanh, phù hợp dấu mật luôn ở trang đầu).\n"
                "Tìm kỹ 10 trang đầu: cắt tối đa 10 trang đầu; OCR + dò chạy\n"
                "SONG SONG theo lượt nhỏ, DỪNG ngay khi gặp trang đầu tiên có\n"
                "dấu — OCR các trang sau cũng ngừng theo.\n"
                "Tìm kỹ: như trên nhưng xét MỌI trang (không cắt bớt file;\n"
                "chậm nhất khi file KHÔNG mật)."
            )
        )
        status_row.addWidget(self.mode_combo)
        picker_layout.addLayout(status_row)
        layout.addWidget(picker)

        self.table = _ResultsTable(0, 9)
        # Mode/note items are translated when created; full retranslation
        # still runs when showing the screen or changing the UI language.
        self.table.setProperty("_scanindex_i18n_explicit_items", True)
        self.table.setHorizontalHeaderLabels(
            [
                "", "Độ mật", "File", "Mã CQ", "Cơ quan", "Ngày cập nhật",
                "Trang", "Chế độ", "Ghi chú",
            ]
        )
        self.table.verticalHeader().setVisible(False)
        # Sửa trực tiếp các cột thông tin (Độ mật, Mã CQ, Cơ quan, Ghi chú)
        # bằng double-click hoặc F2; các cột còn lại không đặt cờ editable.
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self._delegate = _ResultsDelegate(self.table)
        self.table.setItemDelegate(self._delegate)
        self._delegate.edit_committed.connect(self._on_edit_committed)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.itemDoubleClicked.connect(self._open_result_file)
        # Click thường (không vào cột checkbox) → xem trước bên phải;
        # double-click vẫn mở file bằng app ngoài như trước.
        self.table.itemClicked.connect(self._on_row_clicked)
        self.table.itemChanged.connect(self._on_item_changed)
        # Phím mũi tên / PageUp / Home… đổi dòng chọn → preview theo file đó.
        self.table.keyboard_row_activated.connect(self._show_preview)
        # Header tùy biến: ô cột đầu tiên là ô check "chọn tất cả".
        header = _CheckAllHeader(self.table)
        self.table.setHorizontalHeader(header)
        header.check_toggled.connect(self._on_select_all_toggled)
        # Cột Interactive: kéo rộng/hẹp từng cột bằng chuột (để thấy được tên
        # file dài); cột cuối (Ghi chú) tự chiếm phần bề ngang còn lại.
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        header.setMinimumSectionSize(28)
        self.table.setWordWrap(False)
        for col, width in (
            (0, 30), (1, 90), (2, 260), (3, 80), (4, 200), (5, 120),
            (6, 56), (7, 90)
        ):
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

        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText(
            translations.localize_text("🔍 Gõ để lọc danh sách…")
        )
        self.filter_edit.setClearButtonEnabled(True)
        # Co giãn theo hàng thay vì cố định 230px: hàng điều khiển không còn
        # ghim chiều rộng tối thiểu của panel trái — splitter luôn còn quãng
        # để kéo bảng / khung xem trước trên màn hình nhỏ.
        self.filter_edit.setMinimumWidth(120)
        self.filter_edit.setToolTip(
            translations.localize_text(
                "Chỉ ẨN/hiện lại các dòng khớp (tìm trên mọi cột + đường dẫn đầy\n"
                "đủ; nhiều từ cách nhau = phải có đủ). Không xóa dòng: \"Xuất\n"
                "Excel\" vẫn xuất TOÀN BỘ danh sách kể cả dòng đang ẩn; ô check\n"
                "\"chọn tất cả\" trên header chỉ check các dòng đang hiện."
            )
        )
        self.filter_edit.textChanged.connect(self._apply_filter)
        results_header.addWidget(self.filter_edit, 1)

        # "Không phải mật (đã chọn)" đã bỏ để gọn hàng điều khiển (màn hình
        # nhỏ): thao tác này vẫn còn ở thanh công cụ của khung xem trước
        # (btn_preview_not_secret) sau khi bấm chọn một dòng.
        self.btn_batch_delete = QPushButton("🗑  Xóa")
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
        # KHÔNG đặt stretch factor (3:2 cũ): có stretch thì mỗi lần cửa sổ
        # đổi cỡ, QSplitter chia lại phần dư theo 60/40 và "cướp" mất tỷ lệ
        # người dùng vừa kéo tay. Không stretch → phần dư chia theo tỷ lệ
        # hiện có, vị trí tay nắm người dùng kéo được giữ nguyên.
        splitter.setSizes([760, 440])
        # Tay nắm mảnh 2px (1/4 độ dày cũ 8px theo yêu cầu) + đổi màu accent
        # khi rê chuột để dễ định vị khi kéo đổi tỷ lệ bảng / khung xem trước.
        splitter.setHandleWidth(2)
        splitter.setStyleSheet(
            f"QSplitter::handle {{ background: {COLOR_BORDER}; border-radius: 1px; }}"
            f"QSplitter::handle:hover {{ background: {COLOR_ACCENT}; }}"
        )
        layout.addWidget(splitter, 1)

    def _build_preview_panel(self) -> QWidget:
        panel = QFrame()
        panel.setStyleSheet(
            f"QFrame {{ background: {COLOR_PANEL}; border: 1px solid {COLOR_BORDER};"
            f" border-radius: {RADIUS_MD}px; }}"
        )
        # Min tường minh NHỎ hơn minimumSizeHint tự nhiên (~600px do tổng
        # chiều rộng nút trên thanh công cụ PDF): splitter dựa trên giá trị
        # này để chặn tay nắm — không có nó thì khung xem trước không thể
        # thu hẹp/kéo rộng tùy ý trên màn hình nhỏ (nút bị cắt mép khi kéo
        # sát là chấp nhận được).
        panel.setMinimumWidth(300)
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

    @staticmethod
    def _scan_mode_for_key(key: str) -> tuple[str, bool, int]:
        """Map key combo → (mode_key, first_page_only, page_limit).

        Hàm thuần để test:
        - "fast"       → ("fast", True, 0)   : Tìm nhanh, trang đầu.
        - "thorough10" → ("thorough10", False, 10) : Tìm kỹ 10 trang đầu.
        - "thorough"   → ("thorough", False, 0) : Tìm kỹ mọi trang.
        Key lạ/rỗng → rơi về "fast" (chế độ mặc định, nhẹ nhất).
        """
        if key == "thorough10":
            return "thorough10", False, 10
        if key == "thorough":
            return "thorough", False, 0
        return "fast", True, 0

    def _scan_mode(self) -> tuple[str, bool, int]:
        return self._scan_mode_for_key(self.mode_combo.currentData() or "fast")

    def _run_clicked(self) -> None:
        folder = self.folder_edit.text().strip()
        if not folder or not os.path.isdir(folder):
            QMessageBox.information(self, "Chưa có thư mục", "Vui lòng chọn thư mục cần quét.")
            return
        if self._busy:
            return

        mode_key, first_page_only, page_limit = self._scan_mode()
        decision = self._resume_decision(folder, first_page_only, mode_key=mode_key)
        if decision is None:
            # Hủy / Esc / đóng hộp thoại: thao tác thoát, KHÔNG chạy worker,
            # KHÔNG reset bảng, KHÔNG tạo journal (review mục 1).
            return

        if (
            decision.mode == ResumeDecision.RESTART
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
        if decision.prog is not None:
            # Khôi phục các dòng mật đã phát hiện ở lượt trước lên bảng.
            # Rel hiển thị tính lại theo thư mục ĐANG quét (R6): match kế
            # thừa từ cache thư mục con mang rel theo thư mục cũ.
            for match_dict in decision.prog.matches:
                restored = dict(match_dict)
                source = str(restored.get("source_path") or "")
                if source:
                    try:
                        restored["relative_path"] = os.path.relpath(source, folder)
                    except ValueError:
                        pass
                try:
                    self._add_result(SecretScanMatch(**restored))
                except (TypeError, ValueError):
                    continue
        self._set_running_ui(True)
        self._set_status("Đang chuẩn bị...")
        self.progress.setValue(0)
        self.progress.setMaximum(1)

        thread = threading.Thread(
            target=self._run_worker,
            args=(folder, first_page_only, decision, mode_key, page_limit),
            daemon=True,
            name="secret-file-scan",
        )
        thread.start()

    @staticmethod
    def _rescan_targets(prog, folder: str) -> list[str]:
        """Các file mật của journal, định danh bằng source_path tuyệt đối đã
        chuẩn hóa, lọc cho nằm trong thư mục gốc của phiên (R6)."""
        root = _norm(folder)
        ordered: list[str] = []
        seen: set[str] = set()
        for d in prog.matches or []:
            source = str(d.get("source_path") or "")
            if not source:
                continue
            key = ssp._norm_path(source)
            if key != root and not key.startswith(root + os.sep):
                continue
            if key in seen:
                continue
            seen.add(key)
            ordered.append(key)
        return ordered

    @staticmethod
    def _resume_dialog_kind(
        journal_version: str, current_version: str, needs_continue: bool
    ) -> str:
        """Phân loại hộp thoại resume (hàm thuần để test):
        - "continue" : còn migration/quét lại dở — đề nghị tiếp tục hoàn tất.
        - "legacy3"  : journal khác version — 3 lựa chọn nâng cấp.
        - "normal"   : cùng version — hỏi tiếp tục/quét lại như cũ.
        """
        if needs_continue:
            return "continue"
        if journal_version != current_version:
            return "legacy3"
        return "normal"

    @staticmethod
    def _continue_mode(prog, targets: list[str]) -> str:
        """Chế độ cho nút "Tiếp tục hoàn tất" (hàm thuần để test): RESUME_RESCAN
        khi còn tập quét lại chờ xử lý, HOẶC migration choice-3 dở mà tập rs
        chưa kịp ghi (crash ngay sau begin_migration) — lúc đó dựng lại tập
        quét lại từ matches theo choice đã lưu. Ngoài ra RESUME."""
        if prog.rescan_pending:
            return ResumeDecision.RESUME_RESCAN
        if (
            prog.migration
            and int(prog.migration.get("choice") or 0) == 3
            and targets
        ):
            return ResumeDecision.RESUME_RESCAN
        return ResumeDecision.RESUME

    def _resume_decision(
        self,
        folder: str,
        first_page_only: bool,
        mode_key: str | None = None,
    ) -> ResumeDecision | None:
        """Hỏi người dùng cách xử lý phiên quét dở của (folder, mode).

        Trả ``None`` khi người dùng hủy. Không có journal → RESTART ngay.
        """
        try:
            ssp.prune_stale()
        except Exception:
            pass
        if mode_key is None:
            mode_key = "fast" if first_page_only else "thorough"
        try:
            prog = ssp.SecretScanProgress.load(folder, mode_key)
        except Exception:
            prog = None
        if prog is None:
            return ResumeDecision(ResumeDecision.RESTART)
        done_count, error_count, skip_count, found = prog.stats()
        if (done_count == 0 and error_count == 0 and skip_count == 0 and found == 0
                and not prog.needs_continue()):
            prog.close()
            return ResumeDecision(ResumeDecision.RESTART)

        from scanindex.infra.version import get_version

        current_version = get_version()
        kind = self._resume_dialog_kind(
            prog.app_version, current_version, prog.needs_continue()
        )
        stats_text = (
            "Thư mục này có lượt quét chưa hoàn tất:\n"
            f"• Đã quét xong: {done_count} file\n"
            f"• Lỗi lần trước (sẽ thử lại): {error_count} file\n"
            f"• File hỏng (sẽ bỏ qua): {skip_count} file\n"
            f"• Dòng mật đã phát hiện: {found}"
        )
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        if kind == "continue":
            box.setWindowTitle("Tiếp tục phiên nâng cấp dở?")
            pending = len(prog.rescan_pending)
            pending_line = (
                f"\n• File mật còn chờ quét lại: {pending}" if pending else ""
            )
            box.setText(
                stats_text
                + pending_line
                + "\n\nLượt trước dừng giữa chừng khi nâng cấp lịch sử quét.\n"
                "Tiếp tục hoàn tất phần việc còn dở?"
            )
            btn_continue = box.addButton(
                "Tiếp tục hoàn tất", QMessageBox.ButtonRole.AcceptRole
            )
            btn_restart = box.addButton(
                "Quét lại từ đầu", QMessageBox.ButtonRole.DestructiveRole
            )
            box.addButton("Hủy", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            clicked = box.clickedButton()
            if clicked is btn_continue:
                targets = self._rescan_targets(prog, folder)
                if (
                    self._continue_mode(prog, targets)
                    == ResumeDecision.RESUME_RESCAN
                ):
                    return ResumeDecision(
                        ResumeDecision.RESUME_RESCAN,
                        prog=prog,
                        rescan_abs=targets,
                    )
                return ResumeDecision(ResumeDecision.RESUME, prog=prog)
            if clicked is btn_restart:
                return ResumeDecision(ResumeDecision.RESTART)
            return None

        if kind == "legacy3":
            targets = self._rescan_targets(prog, folder)
            box.setWindowTitle("Tiếp tục quét? (phiên bản cũ)")
            box.setText(
                stats_text
                + "\n\nPhiên quét dở này được tạo bởi PHIÊN BẢN CŨ của ứng dụng:\n"
                "• Quét tiếp: giữ nguyên kết luận của nhóm đã quét và đóng dấu\n"
                "  phiên bản mới vào lịch sử (không quét lại nhóm đã quét).\n"
                "• Quét tiếp + quét lại file mật cũ: như trên, kèm quét lại đúng\n"
                f"  {len(targets)} file mật đã phát hiện để có đầy đủ dữ liệu giải mật\n"
                "  và loại các dòng nhận nhầm của bản cũ.\n"
                "• Quét lại từ đầu: bỏ mọi kết luận cũ, quét toàn bộ bằng bản mới."
            )
            btn_restart = box.addButton(
                "Quét lại từ đầu", QMessageBox.ButtonRole.DestructiveRole
            )
            btn_resume = box.addButton(
                "Quét tiếp (giữ kết quả cũ)", QMessageBox.ButtonRole.AcceptRole
            )
            btn_rescan = box.addButton(
                f"Quét tiếp + quét lại {len(targets)} file mật cũ",
                QMessageBox.ButtonRole.AcceptRole,
            )
            box.addButton("Hủy", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            clicked = box.clickedButton()
            if clicked is btn_restart:
                return ResumeDecision(ResumeDecision.RESTART)
            if clicked is btn_resume:
                return ResumeDecision(ResumeDecision.RESUME, prog=prog)
            if clicked is btn_rescan:
                return ResumeDecision(
                    ResumeDecision.RESUME_RESCAN, prog=prog, rescan_abs=targets
                )
            return None

        box.setWindowTitle("Tiếp tục quét?")
        box.setText(
            stats_text
            + "\n\nTiếp tục từ nơi dừng không?\n"
            "Chọn \"Quét lại từ đầu\" để quét lại toàn bộ."
        )
        btn_continue = box.addButton(
            "Tiếp tục", QMessageBox.ButtonRole.AcceptRole
        )
        btn_restart = box.addButton(
            "Quét lại từ đầu", QMessageBox.ButtonRole.DestructiveRole
        )
        box.addButton("Hủy", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is btn_continue:
            return ResumeDecision(ResumeDecision.RESUME, prog=prog)
        if clicked is btn_restart:
            return ResumeDecision(ResumeDecision.RESTART)
        return None

    def _stop_clicked(self) -> None:
        self._cancel_event.set()
        self._set_status("Đang dừng...")

    def _set_running_ui(self, running: bool) -> None:
        self.btn_browse.setEnabled(not running)
        self.folder_edit.setEnabled(not running)
        self.mode_combo.setEnabled(not running)
        self.history_checkbox.setEnabled(not running)
        self.btn_clear_history.setEnabled(not running)
        self.btn_load_file.setEnabled(not running)
        # Bật lại nút xuất khi hết bận VÀ có kết quả, HOẶC vừa có lượt quét
        # trong phiên — lượt quét 0 phát hiện vẫn xuất được làm minh chứng.
        self.btn_export.setEnabled(
            not running
            and (self.table.rowCount() > 0 or bool(self._last_scan_summary))
        )
        self.btn_copy.setEnabled(
            not running
            and self.table.rowCount() > 0
            and not self._copy_running
        )
        self.btn_run.setVisible(not running)
        self.btn_stop.setVisible(running)
        # Xóa file / "Không phải mật" vẫn dùng được trong lúc quét — xem
        # _refresh_action_buttons; xem trước cũng cho phép.
        self._refresh_action_buttons()

    def _run_worker(
        self,
        folder: str,
        first_page_only: bool,
        decision: "ResumeDecision | None" = None,
        mode_key: str | None = None,
        page_limit: int = 0,
    ) -> None:
        from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

        started = time.strftime("%Y%m%d_%H%M%S")
        started_display = time.strftime("%d/%m/%Y %H:%M:%S")
        work_root = os.path.join(get_base_dir(), "temp", f"secret_scan_{started}")
        os.makedirs(work_root, exist_ok=True)
        if mode_key is None:
            mode_key = "fast" if first_page_only else "thorough"
        from scanindex.infra.version import get_version

        app_version = get_version()
        restart = decision is None or decision.mode == ResumeDecision.RESTART
        resume_like = not restart
        prog = (
            decision.prog
            if decision is not None and decision.prog is not None
            else ssp.SecretScanProgress.create(folder, mode_key, app_version)
        )

        files = list(_iter_supported_files(folder))
        total = len(files)
        # Ghép nhiệm vụ quét lại theo đường dẫn tuyệt đối ĐÃ CHUẨN HÓA
        # (normcase): rel suy từ rescan_targets/rescan_pending có thể là chữ
        # thường trong khi tên file trên đĩa có chữ HOA — so khớp chuỗi rel
        # trực tiếp sẽ trượt toàn bộ trên Windows (vd "A06.35.25-CongVan.PDF"
        # → rel "a06.35.25-congvan.pdf" không bao giờ tra cứu trúng).
        enum_by_norm: dict[str, tuple[int, str]] = {}
        for _enum_idx, _enum_path in enumerate(files, start=1):
            enum_by_norm.setdefault(_norm(_enum_path), (_enum_idx, _enum_path))

        def _canonical_rel(abs_norm: str) -> str | None:
            """Rel đúng theo tên file trên đĩa cho abs đã chuẩn hóa."""
            hit = enum_by_norm.get(abs_norm)
            return os.path.relpath(hit[1], folder) if hit else None

        pending, skipped = _split_pending(files, folder, prog.done_files())

        # Registry — tách ĐỌC cache khỏi GHI lịch sử (R4/R5 review):
        # - ĐỌC cache: chỉ khi checkbox "Tận dụng" bật VÀ không phải quét
        #   lại từ đầu (RESTART bỏ qua cache toàn lượt dù vẫn ghi mới).
        # - GHI lịch sử: bật khi checkbox bật, HOẶC khi resume nâng cấp
        #   (lựa chọn 2/3) vì người dùng đã yêu cầu lưu bền vững.
        use_history = self.history_checkbox.isChecked()
        needs_upgrade = resume_like and (
            prog.app_version != app_version or bool(prog.migration)
        )
        registry = (
            ssp.FileRegistry.load() if (use_history or needs_upgrade) else None
        )
        if registry is not None and getattr(registry, "readonly", False):
            # D0: khóa single-writer registry thuộc tiến trình khác → chạy
            # KHÔNG cache (không ghi, không tra) thay vì hai tiến trình cùng
            # ghi/truncate một file.
            self.log_message.emit(
                "Registry đang được mở bởi tiến trình ScanIndex khác — lượt "
                "này chạy KHÔNG cache registry",
                "info",
            )
            registry.close()
            registry = None
        use_cache = use_history and not restart
        if registry is None:
            self.log_message.emit("Lịch sử quét: TẮT — không tái dùng kết quả cũ", "info")
        cache_skipped = [0]

        # ── Migration journal bản cũ → version mới (lựa chọn 2/3) ─────────
        # Trình tự bền vững (review mục 4): lưu ý định → nâng registry (chạy
        # lại được) → xác nhận trong journal. Crash giữa chừng: lần sau thấy
        # migration còn dở → tiếp tục, không phụ thuộc av đã khớp.
        rescan_norms: list[str] = []
        upgraded_ok = True
        inherited_created = 0
        if resume_like:
            if prog.rescan_pending:
                # Rel trong rs có thể là chữ thường (journal do bản bị lỗi
                # chuẩn hóa ghi) → đưa về abs chuẩn hóa để so khớp không
                # phân biệt chữ HOA/thường với tên file thật bên dưới.
                candidates = [
                    ssp._norm_path(os.path.join(folder, rel))
                    for rel in sorted(prog.rescan_pending)
                ]
            elif (
                decision is not None
                and decision.mode == ResumeDecision.RESUME_RESCAN
            ):
                candidates = list(decision.rescan_abs)
            else:
                candidates = []
            _seen_norm: set[str] = set()
            for _norm_key in candidates:
                if _norm_key and _norm_key not in _seen_norm:
                    _seen_norm.add(_norm_key)
                    rescan_norms.append(_norm_key)
            if needs_upgrade:
                self._status_changed.emit(
                    "Nâng cấp lịch sử quét của phiên bản cũ..."
                )
                if not prog.migration:
                    prog.begin_migration(
                        app_version,
                        3
                        if decision is not None
                        and decision.mode == ResumeDecision.RESUME_RESCAN
                        else 2,
                    )
                if (
                    decision is not None
                    and decision.mode == ResumeDecision.RESUME_RESCAN
                    and rescan_norms
                    and not prog.rescan_pending
                ):
                    prog.set_rescan_pending(
                        sorted(
                            {
                                rel
                                for rel in (
                                    _canonical_rel(n) for n in rescan_norms
                                )
                                if rel is not None
                            }
                        )
                    )
                prog.save()
                matches_by_abs: dict[str, list[dict]] = {}
                for match_dict in prog.matches:
                    source = str(match_dict.get("source_path") or "")
                    if source:
                        matches_by_abs.setdefault(
                            ssp._norm_path(source), []
                        ).append(match_dict)
                stat_failed: list[str] = []
                record_failed: list[str] = []
                bump_paths: list[str] = []
                for rel in sorted(prog.ok_files()):
                    path = os.path.join(folder, rel)
                    bump_paths.append(path)
                    if registry is not None and not registry.has_entry(
                        path, mode_key
                    ):
                        # Entry thiếu (lượt cũ chạy với lịch sử TẮT, registry
                        # hỏng/mất): kế thừa với fingerprint HIỆN TẠI — mốc
                        # người dùng chấp nhận kế thừa, không phải bằng chứng
                        # file chưa đổi (R4). File skip không được kế thừa.
                        try:
                            st = os.stat(path)
                        except OSError:
                            stat_failed.append(rel)
                            continue
                        try:
                            registry.record(
                                path,
                                st.st_size,
                                st.st_mtime,
                                mode_key,
                                app_version,
                                matches_by_abs.get(ssp._norm_path(path), []),
                                inherited=True,
                            )
                        except Exception:
                            # R3-01: record (append) lỗi → entry KHÔNG bền
                            # vững; RAM đã rollback trong record. Không được
                            # nuốt im lặng coi là đã kế thừa — đánh dấu nâng
                            # cấp chưa xong để lượt sau chạy lại (ý định
                            # migration đã nằm bền trong journal từ trước).
                            record_failed.append(rel)
                            logging.getLogger(__name__).exception(
                                "nâng cấp lịch sử: record kế thừa lỗi (%s) — "
                                "giữ việc nâng cấp còn dở để lượt sau chạy lại",
                                rel,
                            )
                            continue
                        inherited_created += 1
                if registry is not None:
                    registry.bump_versions_exact(
                        bump_paths, mode_key, app_version
                    )
                    registry.snapshot()
                if stat_failed or record_failed:
                    upgraded_ok = False
                    self.log_message.emit(
                        f"Nâng cấp lịch sử: {len(stat_failed)} file không đọc "
                        f"được trạng thái đĩa, {len(record_failed)} file ghi "
                        "registry lỗi — giữ việc nâng cấp còn dở để lần sau "
                        "chạy lại.",
                        "err",
                    )
                else:
                    prog.accept_version(app_version)
                    prog.finish_migration()
                    prog.save()
                    self.log_message.emit(
                        f"Đã nâng cấp lịch sử quét lên bản mới: kế thừa kết luận "
                        f"của {skipped} file đã quét"
                        + (
                            f", tạo {inherited_created} entry kế thừa"
                            if inherited_created
                            else ""
                        )
                        + ".",
                        "info",
                    )

        # ── Hàng đợi: pha quét lại file mật (nếu có) rồi pha pending ──────
        # Một file chỉ có MỘT task (R6): file nằm cả trong rescan lẫn pending
        # (trạng thái err) thì chỉ giữ nhiệm vụ quét lại.
        rescan_tasks: list[tuple[int, str]] = []
        rescan_missing = 0
        for norm_key in rescan_norms:
            hit = enum_by_norm.get(norm_key)
            if hit is None:
                rescan_missing += 1
            else:
                rescan_tasks.append(hit)
        rescan_found = {n for n in rescan_norms if n in enum_by_norm}
        pending = [
            task for task in pending if _norm(task[1]) not in rescan_found
        ]
        if rescan_norms:
            # Ghi lại tập chờ quét lại dạng CHUẨN (rel đúng tên file thật,
            # bỏ file không còn trên đĩa): tự chữa journal từng bị ghi rel
            # chữ thường, và không để file đã xóa kẹt mãi trong tập này
            # (needs_continue luôn đúng → hộp thoại "còn dở" lặp vô hạn).
            canonical_pending = sorted(
                {rel for rel in (_canonical_rel(n) for n in rescan_found) if rel}
            )
            if set(canonical_pending) != prog.rescan_pending:
                prog.set_rescan_pending(canonical_pending)
        if rescan_missing:
            self.log_message.emit(
                f"{rescan_missing} file mật cũ không còn trong thư mục — giữ "
                "kết quả cũ.",
                "info",
            )
        # Tiến độ theo TOÀN thư mục: resume ở 150/200 phải thấy thanh chạy
        # từ 75%, không phải 0% (trước đây thanh đếm theo phần việc còn lại
        # của lượt nên luôn khởi động 0%, trong khi status lại ghi
        # "Đang quét 151/200" — hai thang đo lệch nhau). Base = số file
        # enumeration đã xong; task pending và task quét lại cho file CHƯA
        # được tính ở base (file err cũ có match cũ) hoàn thành xong +1 —
        # không đếm một file hai lần, không vượt total.
        progress_total = total
        folder_done = [skipped]
        counted_norms = {_norm(task[1]) for task in pending}
        _done_rels = prog.done_files()
        for _task_idx, _task_path in rescan_tasks:
            if os.path.relpath(_task_path, folder) not in _done_rels:
                counted_norms.add(_norm(_task_path))
        self._progress_changed.emit(folder_done[0], max(1, progress_total))
        if resume_like:
            self.log_message.emit(
                f"Tiếp tục quét: kế thừa {skipped} file đã quét, còn "
                f"{len(pending)} file cần quét"
                + (
                    f", quét lại {len(rescan_tasks)} file mật cũ"
                    if rescan_tasks
                    else ""
                )
                + f" (tổng thư mục {total} file)",
                "info",
            )
        else:
            self.log_message.emit(
                f"Quét file mật: tìm thấy {total} file hỗ trợ", "info"
            )
            if restart and use_history:
                self.log_message.emit(
                    "Quét lại từ đầu: bỏ qua cache cho toàn lượt", "info"
                )

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
        # P2-3: nhận diện lượt quét cho luồng hủy KIE có ownership — Stop chỉ
        # hủy job của LƯỢT NÀY (cancel_run), không đụng coordinator toàn app.
        run_id = f"r{time.monotonic_ns()}"
        done = [0]
        scanned = [0]
        failures = [0]
        junk = [0]
        updated = [0]
        kept_old = [0]
        cancelled = [False]
        # GĐ K: các file có metadata KIE đang chạy deferred — commit sau ACK
        # từng file (P1-09), hoặc ở drain cuối lượt nếu ACK chưa kịp.
        pending_commits: list[dict] = []
        # P1-E (review 2): barrier vòng đời lượt — callback commit muộn sau
        # khi lượt đóng (cancel/scan_finished) KHÔNG được chạm journal/UI.
        # `run_gate_lock` vừa bảo vệ cờ vừa là barrier: finally giữ lock này
        # khi đóng lượt ⇒ commit đang bay chắc chắn xong trước khi close.
        run_gate = {"open": True}
        run_gate_lock = threading.Lock()
        producer_stop = threading.Event()
        producer_stop_reason = [""]

        def request_producer_stop(reason: str) -> None:
            with progress_lock:
                first = not producer_stop.is_set()
                if first:
                    producer_stop_reason[0] = reason
                    producer_stop.set()
            if first:
                self.log_message.emit(f"Dừng cấp file mới: {reason}", "err")

        def collect_committed_spool(rel: str) -> None:
            # Only this file: preserve the bucketed commit optimization.
            with state_lock:
                prog.kie_spool_gc(durability_confirmed=True, rel=rel)
                # Metadata/result was fsynced before GC. Its tombstone can
                # share the next ordinary save: replay of the old applied
                # descriptor is harmless even if the snapshot is now gone.

        def _stat_fingerprint(path: str) -> tuple[int, float] | None:
            try:
                st = os.stat(path)
                return (st.st_size, st.st_mtime)
            except OSError:
                return None

        def persist_scan_artifact() -> None:
            """R2-02: persist TÁCH KHỎI publish — flush+fsync idempotent, gọi
            lại được khi fsync lỗi mà không phát lại UI/journal. Caller phân
            biệt: publish (một lần cho entry) ↔ persist (retry đến khi xong)."""
            with state_lock:
                prog.save()
                if registry is not None:
                    registry.save()

        def commit_artifact(artifact, rel: str, path: str, replace: bool,
                            file_log, scan_stat: tuple | None = None,
                            source_unstable: bool = False,
                            do_emit: bool = True,
                            on_emitted=None) -> None:
            """PUBLISH kết quả MỘT file: phát hàng UI + ghi journal/registry.

            R2-02: journal ghi theo ĐỊNH DANH file (`commit_replace_file` cho
            CẢ hai nhánh — thay matches theo abs_norm, một dòng rpf nguyên
            vẹn) nên gọi LẠI sau lỗi không sinh dòng/matches trùng. Persist
            (fsync) tách riêng qua `persist_scan_artifact` — caller retry
            persist mà không publish lại. `do_emit=False` khi UI đã phát cho
            entry này; `on_emitted` được gọi NGAY sau khi emit trả về (emit là
            nguyên tố — retry sau đó không phát lại UI kể cả khi journal/persist
            lỗi).

            P1-07/P2.5/P2-H: `cache_final` quyết định có ghi registry không —
            metadata chưa áp xong / nguồn đổi trong lúc quét / đổi sau quét
            → KHÔNG cache (file vào tập quét lại, lượt sau chạy lại).
            P1-D: nghĩa vụ quét lại giữ ở CẢ hai nhánh. MỌI thay đổi journal
            giữ `state_lock`."""
            matches = artifact.matches
            artifact_status = getattr(artifact, "status", "clean") or "clean"
            for match in matches:
                try:
                    match.source_version = app_version
                except AttributeError:
                    # match không phải dataclass (dữ liệu ngoài/test) —
                    # không được chặn commit vì việc gắn version.
                    pass
            match_dicts = [asdict(m) for m in matches]
            # P1-07/P2.5/P2-H: quyết định cache-final TRƯỚC khi ghi gì cả.
            cache_final, cache_block_reason = _deferred_cache_final(
                artifact, path, scan_stat, source_unstable=source_unstable
            )
            rescan_rel = None
            if not cache_final:
                rescan_rel = rel
                file_log(
                    f"Kết quả lần này KHÔNG dùng làm cache ({cache_block_reason}"
                    ") — file sẽ được quét lại lượt sau"
                )
            abs_norm = ssp._norm_path(path)
            if do_emit:
                if replace:
                    # Thay thế hàng THEO FILE (phát _results_replace; không
                    # phát _result_found song song gây trùng).
                    self._results_replace.emit(
                        {"path": abs_norm, "matches": match_dicts}
                    )
                else:
                    for match in matches:
                        self._result_found.emit(match)
                if on_emitted is not None:
                    on_emitted()
            with state_lock:
                # R2-02: MỘT thao tác journal theo định danh file — replay
                # idempotent, retry không append trùng matches.
                prog.commit_replace_file(rel, abs_norm, match_dicts)
                # R3-01: lỗi registry record KHÔNG được nuốt — stat lỗi và
                # persist lỗi phải tách nhau; record lỗi phải đi LÊN cơ chế
                # commit retry/failure (entry ở lại / file đếm lỗi), không
                # được coi là đã hoàn tất khi kết quả chưa nằm trên đĩa.
                registry_error: BaseException | None = None
                if cache_final and registry is not None:
                    # R3-05: record fingerprint của ĐÚNG phiên bản đã đọc
                    # (scan_stat chụp TRƯỚC khi đọc nguồn) — stat lại ở đây
                    # sẽ gắn kết quả của nguồn cũ cho phiên bản mới (cache
                    # hit cho nội dung chưa hề quét).
                    if scan_stat is not None:
                        try:
                            registry.record(
                                path,
                                scan_stat[0],
                                scan_stat[1],
                                mode_key,
                                app_version,
                                match_dicts,
                                status=artifact_status,
                            )
                        except Exception as exc:
                            registry_error = exc
                    else:
                        # Không có fingerprint lúc đọc — không chứng minh được
                        # danh tính phiên bản: không cache, giữ nghĩa vụ quét
                        # lại (fail-closed, hi hữu: stat hỏng ngay đầu lượt).
                        rescan_rel = rescan_rel or rel
                if rescan_rel is not None or registry_error is not None:
                    # P1-D: commit_replace_file vừa XÓA rel khỏi tập quét
                    # lại — metadata chưa hoàn tất thì nghĩa vụ retry phải
                    # được THÊM LẠI, không phụ thuộc thao tác thay matches.
                    pending_rescan = {rel} if registry_error is not None else set()
                    if rescan_rel is not None:
                        pending_rescan.add(rescan_rel)
                    prog.set_rescan_pending(
                        sorted(set(prog.rescan_pending) | pending_rescan)
                    )
                if registry_error is not None:
                    raise _ArtifactPersistError(
                        f"registry record lỗi ({registry_error}) — kết quả "
                        f"chưa bền vững, file quay lại cơ chế retry"
                    ) from registry_error
                if cache_final and registry_error is None:
                    spool = prog.kie_spool(rel)
                    if spool:
                        prog.set_kie_spool(rel, {**spool, "metadata_applied": True})

        def process_one(idx: int, path: str, replace: bool = False,
                        kie_only: bool = False) -> None:
            if self._cancel_event.is_set() or producer_stop.is_set():
                return
            _scan_run_ctx.producer_stop = producer_stop
            _scan_run_ctx.request_producer_stop = request_producer_stop
            _scan_run_ctx.cancel_event = self._cancel_event
            _scan_run_ctx.run_id = run_id
            rel = os.path.relpath(path, folder)
            self._status_changed.emit(
                f"Đang quét lại {idx}/{total}: {rel}"
                if replace
                else f"Đang quét {idx}/{total}: {rel}"
            )
            file_work = os.path.join(work_root, f"{idx:05d}_{_safe_name(Path(path).stem)}")

            def file_log(message: str, rel_path=rel) -> None:
                self.log_message.emit(f"[{rel_path}] {message}", "info")

            def _prepare_spool_for_file(canon_path: str) -> dict | None:
                # D2: copy canonical vào spool bền TRƯỚC khi submit job
                # (fp nguồn chụp tại đây — closure đọc pre_stat đã tính).
                info = _kie_spool_prepare(canon_path, rel, mode_key, run_id)
                if info is not None:
                    info["fp"] = list(pre_stat or [])
                return info

            def _kie_spool_commit(info: dict) -> None:
                with state_lock:
                    prog.set_kie_spool(rel, info)
                    prog.save()

            def _kie_spool_discard(info: dict) -> None:
                from scanindex.core.ocr.direct_engine import cleanup_path_when_idle
                spool_dir = str(info.get("spool_dir") or "")
                if spool_dir:
                    cleanup_path_when_idle(spool_dir)

            def _resume_kie_only(rel: str, path: str, log):
                """D2 — resume KIE-only: snapshot còn hợp lệ (nguồn không đổi
                + spool nguyên vẹn đúng hash) → resubmit metadata job, OCR
                CALL = 0. Trả (artifact, None) hoặc (None, lý do)."""
                try:
                    info = prog.kie_spool(rel)
                except (TypeError, ValueError) as exc:
                    return None, f"spool record không hợp lệ: {exc}"
                if not info:
                    return None, "không có spool record"
                fp = list(info.get("fp") or [])
                try:
                    st = os.stat(path)
                except OSError as exc:
                    return None, f"nguồn không đọc được: {exc}"
                if fp and (st.st_size, st.st_mtime) != tuple(fp[:2]):
                    return None, "nguồn đổi từ lúc submit (fingerprint lệch)"
                spool_canon = str(info.get("canonical") or "")
                if not spool_canon or not os.path.exists(spool_canon):
                    return None, "snapshot spool không còn trên đĩa"
                try:
                    h = hashlib.sha256()
                    with open(spool_canon, "rb") as fh:
                        for chunk in iter(lambda: fh.read(1 << 20), b""):
                            h.update(chunk)
                    if h.hexdigest() != str(info.get("hash") or ""):
                        return None, "hash snapshot lệch"
                except OSError as exc:
                    return None, f"đọc snapshot lỗi: {exc}"
                try:
                    from scanindex.core.canonical_io import load_canonical

                    canonical = load_canonical(spool_canon)
                    stored_matches = [
                        SecretScanMatch(**d)
                        for d in (info.get("matches") or [])
                    ]
                except Exception as exc:
                    return None, f"snapshot decode lỗi: {exc}"
                if not stored_matches:
                    return None, "spool không có match baseline"
                state = _try_deferred_kie(
                    stored_matches, canonical, spool_canon, run_id=run_id,
                    needed_override=info.get("ordinals"),
                    artifact_status=str(
                        info.get("status") or "confirmed_positive"
                    ),
                )
                if state is None:
                    return None, "deferred không nhận job (không trang cần?)"
                return (
                    SecretScanArtifact(
                        matches=stored_matches, source_pdf=path,
                        canonical=canonical,
                        status=str(
                            info.get("status") or "confirmed_positive"
                        ),
                        metadata_pending=True, kie_state=state,
                    ),
                    None,
                )

            try:
                # File bị xóa giữa chừng (xóa ngay trong lúc quét): ghi
                # "skip" thay vì để OCR báo lỗi rồi mỗi lượt resume lại
                # thử lại một file đã không còn tồn tại.
                if not os.path.exists(path):
                    raise _ScanSkip("file đã bị xóa trong lúc quét")
                # Lookup lịch sử TRƯỚC khi quét: file không đổi → dùng lại
                # kết quả cũ, không tốn OCR. Không áp dụng cho quét lại từ
                # đầu (R5) và nhánh thay thế file mật (entry vừa được đóng
                # dấu version mới sẽ trúng cache — bắt buộc quét thật).
                cached_dicts: list[dict] | None = None
                cached_status: str = "clean"
                if use_cache and not replace and registry is not None:
                    try:
                        st = os.stat(path)
                        hit = registry.lookup_with_status(
                            path,
                            st.st_size,
                            st.st_mtime,
                            mode_key,
                            app_version,
                        )
                        if hit is not None:
                            cached_dicts, cached_status = hit
                    except OSError:
                        cached_dicts = None
                if cached_dicts is not None:
                    # Rel tính lại theo thư mục ĐANG quét (R6): match trong
                    # cache của thư mục con mang rel theo thư mục cũ.
                    # Status được bảo toàn xuyên cache: "unconfirmed" KHÔNG
                    # được hiển thị/đếm như kết quả sạch đã hoàn tất.
                    fixed_dicts = []
                    for match_dict in cached_dicts:
                        fixed = dict(match_dict)
                        fixed["relative_path"] = rel
                        if cached_status == "unconfirmed":
                            note = str(fixed.get("note") or "")
                            if "chưa xác nhận LightGBM" not in note:
                                fixed["note"] = (
                                    f"{note}; chưa xác nhận LightGBM — cần xem lại"
                                    if note
                                    else "chưa xác nhận LightGBM — cần xem lại"
                                )
                        fixed_dicts.append(fixed)
                    for fixed in fixed_dicts:
                        try:
                            self._result_found.emit(SecretScanMatch(**fixed))
                        except (TypeError, ValueError):
                            continue
                    with state_lock:
                        prog.record_file(rel, "ok")
                        prog.record_matches(fixed_dicts)
                        cache_skipped[0] += 1
                    self._status_changed.emit(
                        f"Đã quét trước đây, bỏ qua: {rel}"
                    )
                    return

                # D2 — resume KIE-only: nghĩa vụ metadata còn spool hợp lệ
                # → chỉ resubmit KIE (OCR-call = 0). Hết hợp lệ → xóa spool
                # record và QUÉT LẠI TOÀN BỘ kèm lý do rõ ràng.
                kie_only_artifact = None
                if kie_only:
                    kie_only_artifact, _kie_why = _resume_kie_only(
                        rel, path, file_log
                    )
                    if kie_only_artifact is None:
                        file_log(
                            f"KIE-only resume không khả dụng ({_kie_why}) "
                            "— quét lại toàn bộ"
                        )
                        kie_only = False
                        with state_lock:
                            prog.set_kie_spool(rel, None)

                if kie_only_artifact is not None:
                    file_log(
                        "Resume KIE-only: snapshot hợp lệ — chỉ chạy lại "
                        "metadata, không OCR lại"
                    )
                    info0 = prog.kie_spool(rel) or {}
                    pre_stat = tuple(list(info0.get("fp") or [])[:2]) or None
                    post_stat = pre_stat
                    source_unstable = False
                    artifact = kie_only_artifact
                    matches = artifact.matches
                    artifact_status = (
                        getattr(artifact, "status", "clean") or "clean"
                    )
                else:
                    # page_limit truyền theo kwargs CHỈ khi chế độ cửa sổ đang
                    # bật — giữ lời gọi 6 tham số vị trí cũ cho các nhánh còn
                    # lại (tương thích monkeypatch trong test).
                    scan_kwargs = (
                        {"page_limit": page_limit} if page_limit else {}
                    )
                    # P2-3: run_id truyền qua thread-local (KHÔNG qua tham số —
                    # giữ tương thích lời gọi/make-poison trong test). D3: kèm
                    # cancel_event cho backpressure KIE hủy được theo Stop.
                    _scan_run_ctx.run_id = run_id
                    _scan_run_ctx.cancel_event = self._cancel_event
                    # P2-H: fingerprint chụp TRƯỚC KHI ĐỌC nguồn — so với stat
                    # sau quét để phát hiện nguồn đổi TRONG lúc quét (kết quả
                    # trộn phiên bản → không cache).
                    pre_stat = _stat_fingerprint(path)
                    artifact = scan_one_file_for_secret_artifact(
                        path,
                        rel,
                        file_work,
                        first_page_only,
                        self._cancel_event,
                        file_log,
                        kie_spool_cb={
                            "prepare": _prepare_spool_for_file,
                            "commit": _kie_spool_commit,
                            "discard": _kie_spool_discard,
                        },
                        **scan_kwargs,
                    )
                    post_stat = _stat_fingerprint(path)
                    source_unstable = (
                        pre_stat is not None
                        and post_stat is not None
                        and pre_stat != post_stat
                    )
                    if source_unstable:
                        file_log(
                            "File thay đổi TRONG lúc quét — kết quả lần này "
                            "chỉ mang tham chiếu, không dùng làm cache"
                        )
                    matches = artifact.matches
                    artifact_status = (
                        getattr(artifact, "status", "clean") or "clean"
                    )
                    if artifact_status == "unconfirmed":
                        file_log(
                            "Kết quả CHƯA XÁC NHẬN LightGBM (dương có cảnh báo) "
                            "— cần xem lại"
                        )
                if getattr(artifact, "metadata_pending", False):
                    # GĐ K: metadata (cơ quan/năm) đang chạy trên pool KIE —
                    # P1-09: commit THEO ACK từng file (không giữ toàn bộ
                    # artifact tới cuối lượt); file nào ACK chưa kịp sẽ được
                    # commit ở drain cuối lượt với luật cache-final (P1-07).
                    # R2-01: entry ĐƯỢC ĐĂNG KÝ TRƯỚC khi phép commit nào
                    # chạy (kể cả ACK-sớm) — commit lỗi thì entry chắc chắn
                    # nằm trong danh sách retry của drain.
                    entry = {
                        "artifact": artifact, "rel": rel, "path": path,
                        "replace": replace, "log": file_log,
                        "stat": pre_stat, "unstable": source_unstable,
                        "committed": False, "committing": False,
                        "ui_done": False, "error": None,
                        "lock": threading.Lock(),
                    }

                    def _commit_entry(e=entry) -> None:
                        # P1-E: commit chạy TRONG run_gate_lock — lượt đóng
                        # (finally) phải chờ commit đang bay xong rồi mới
                        # save/close; sau khi gate đóng, callback muộn chỉ
                        # được BỎ (file không có dòng journal thì lượt sau
                        # tự quét lại), không được chạm journal/UI.
                        with e["lock"]:
                            # R2-02: đã committed / đang committing → no-op
                            # (chống lặp kể cả khi gate vẫn mở).
                            if e["committed"] or e["committing"]:
                                return
                            e["committing"] = True
                        try:
                            with run_gate_lock:
                                if not run_gate["open"]:
                                    with e["lock"]:
                                        e["committed"] = True  # bỏ, không retry
                                        e["artifact"] = None
                                        e["log"] = None
                                    with state_lock:
                                        try:
                                            pending_commits.remove(e)
                                        except ValueError:
                                            pass
                                    return
                                # PUBLISH — R2-02: UI phát ĐÚNG MỘT LẦN cho
                                # entry (ui_done đặt NGAY sau emit qua
                                # on_emitted — emit là nguyên tố); journal
                                # idempotent theo định danh file nên publish
                                # lỗi giữa chừng gọi lại không sinh trùng.
                                commit_artifact(
                                    e["artifact"], e["rel"], e["path"],
                                    e["replace"], e["log"],
                                    scan_stat=e["stat"],
                                    source_unstable=e["unstable"],
                                    do_emit=not e["ui_done"],
                                    on_emitted=lambda: e.update(ui_done=True),
                                )
                                # P2-I/R2-05: persist ngay tại mốc — lỗi
                                # fsync để entry ở lại, drain thử lại.
                                persist_scan_artifact()
                                collect_committed_spool(e["rel"])
                        except Exception as exc:
                            with e["lock"]:
                                e["committing"] = False
                                e["error"] = str(exc)
                            logging.getLogger(__name__).exception(
                                "commit deferred lỗi (%s) — entry ở lại, "
                                "drain cuối lượt sẽ thử lại",
                                e["rel"],
                            )
                            return
                        with e["lock"]:
                            e["committing"] = False
                            e["committed"] = True
                            # P2-G: thả tham chiếu artifact/canonical ngay —
                            # entry đã commit không được giữ RAM tới cuối lượt.
                            e["artifact"] = None
                            e["log"] = None
                        with state_lock:
                            try:
                                pending_commits.remove(e)
                            except ValueError:
                                pass
                        # R2-02: file commit thành công phải đếm vào summary
                        # (trước đây commit deferred không tăng counter —
                        # summary báo 0 dù file đã xong).
                        with progress_lock:
                            if e["replace"]:
                                updated[0] += 1
                            else:
                                scanned[0] += 1

                    entry["commit"] = _commit_entry
                    with state_lock:
                        # R3-02: đăng ký quyền sở hữu entry TRƯỚC khi publish
                        # callback — callback có thể chạy NGAY khi
                        # `state["registered"]` đặt True, tức là TRƯỚC dòng
                        # kế tiếp của file-worker. Entry phải đã nằm trong
                        # danh sách retry từ trước (append vô điều kiện: entry
                        # vừa tạo, chưa ai có thể commit nó; "đang committing"
                        # KHÔNG phải lý do bỏ đăng ký — callback lỗi khi chưa
                        # kịp append trước đây làm mất nghĩa vụ retry).
                        pending_commits.append(entry)
                    state = getattr(artifact, "kie_state", None)
                    if state is not None:
                        # Hợp đồng tối thiểu của kie_state chỉ hứa "applied";
                        # các khóa điều phối (lock/registered/on_applied) tự
                        # hoàn thiện nếu caller tạo state trần (test/tooling).
                        if "lock" not in state:
                            state["lock"] = threading.Lock()
                        state.setdefault("registered", False)
                        state.setdefault("on_applied", None)
                        with state["lock"]:
                            state["on_applied"] = _commit_entry
                            state["registered"] = True
                            already_applied = state["applied"]
                    else:
                        already_applied = False
                    if already_applied:
                        # ACK đã về trước cả lúc đăng ký — commit luôn.
                        _commit_entry()
                    return
                commit_artifact(artifact, rel, path, replace, file_log,
                                scan_stat=pre_stat,
                                source_unstable=source_unstable)
                persist_scan_artifact()
                collect_committed_spool(rel)
                with progress_lock:
                    if replace:
                        updated[0] += 1
                    else:
                        scanned[0] += 1
            except _ScanCancelled:
                cancelled[0] = True
            except _ScanSkip as exc:
                with state_lock:
                    prog.record_file(rel, "skip", str(exc))
                    if replace:
                        # File giờ là rác (0 byte/hỏng): giữ hàng cũ, bỏ khỏi
                        # tập quét lại để không retry vô hạn qua các lượt.
                        # Trừ cả dạng normcase — journal cũ có thể đang giữ
                        # rel chữ thường cho file này.
                        prog.set_rescan_pending(
                            sorted(
                                prog.rescan_pending
                                - {rel, os.path.normcase(rel)}
                            )
                        )
                with progress_lock:
                    junk[0] += 1
                    if replace:
                        kept_old[0] += 1
                self.log_message.emit(f"[{rel}] Bỏ qua: {exc}", "info")
            except Exception as exc:
                # Nhánh thay thế gặp lỗi: KHÔNG ghi "err" (file vẫn ok với
                # kết luận cũ) và vẫn nằm trong tập quét lại để lượt sau
                # thử lại — tránh pending trùng phát hàng cũ + hàng mới.
                if isinstance(exc, _PrepProcessError):
                    request_producer_stop(str(exc))
                if replace:
                    with progress_lock:
                        failures[0] += 1
                        kept_old[0] += 1
                    self.log_message.emit(
                        f"[{rel}] Quét lại thất bại, giữ kết quả cũ: {exc}",
                        "err",
                    )
                else:
                    with state_lock:
                        prog.record_file(rel, "err", str(exc))
                    with progress_lock:
                        failures[0] += 1
                    logging.getLogger(__name__).exception(
                        "scan file lỗi: %s", rel
                    )
                    self.log_message.emit(f"[{rel}] Lỗi: {exc}", "err")
            finally:
                if os.path.isdir(file_work):
                    # Qua engine: nếu còn request native sống mở file bên
                    # trong thì dọn muộn (janitor rmtree sau khi token hết).
                    # Registry lỗi KHÔNG phải bằng chứng input hết người dùng
                    # (P1-01) — giữ thư mục và log, không rmtree trực tiếp.
                    try:
                        from scanindex.core.ocr import direct_engine as _de

                        if not _de.cleanup_path_when_idle(file_work):
                            file_log("Dọn thư mục tạm muộn (OCR chưa xong hẳn)")
                    except Exception as exc:
                        file_log(
                            f"Không dọn được thư mục tạm (giữ lại, sẽ dọn sau): {exc}"
                        )
                with state_lock:
                    prog.save()
                    if registry is not None:
                        registry.save()
                with progress_lock:
                    done[0] += 1
                    if _norm(path) in counted_norms:
                        folder_done[0] += 1
                    self._progress_changed.emit(
                        folder_done[0], max(1, progress_total)
                    )
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
            from scanindex.core import secret_scan_kie as _ssk
            if _ssk.deferred_enabled():
                try:
                    from scanindex.core.secret_scan_spool import get_ledger
                    get_ledger()  # Recover storage before any resume job can GC.
                except Exception as exc:
                    request_producer_stop(str(exc))
            with ThreadPoolExecutor(
                max_workers=max_file_workers, thread_name_prefix="secret-scan-file"
            ) as executor:
                # Cửa sổ trượt: chỉ giữ tối đa _SCAN_MAX_INFLIGHT_FILES future
                # thay vì submit toàn bộ hàng đợi (có thể là 300k+ file) —
                # chặn việc process chính phình RAM chỉ vì hàng đợi.
                def run_phase(tasks: list[tuple[int, str]], replace: bool,
                              kie_norms: frozenset = frozenset()) -> None:
                    futures: set = set()
                    next_task = 0

                    def submit_window() -> None:
                        nonlocal next_task
                        while (
                            next_task < len(tasks)
                            and len(futures) < _SCAN_MAX_INFLIGHT_FILES
                            and not self._cancel_event.is_set()
                            and not producer_stop.is_set()
                        ):
                            task_idx, task_path = tasks[next_task]
                            futures.add(
                                executor.submit(
                                    process_one, task_idx, task_path,
                                    replace,
                                    ssp._norm_path(task_path) in kie_norms,
                                )
                            )
                            next_task += 1

                    submit_window()
                    while futures and not self._cancel_event.is_set():
                        done_set, futures = wait(
                            futures, timeout=0.2, return_when=FIRST_COMPLETED
                        )
                        for fut in done_set:
                            # Re-raise any exception from the worker (except
                            # _ScanCancelled, which only flags cancellation).
                            try:
                                fut.result()
                            except _ScanCancelled:
                                cancelled[0] = True
                        submit_window()
                    if self._cancel_event.is_set():
                        cancelled[0] = True
                        for fut in futures:
                            fut.cancel()

                # D2: file còn nghĩa vụ KIE (spool hợp lệ) tách pha riêng —
                # chạy TRƯỚC pending thường (rẻ: không OCR), kết thúc trước.
                kie_only_norms = frozenset(
                    ssp._norm_path(os.path.join(folder, rel))
                    for rel in prog.kie_spool_pending()
                )
                kie_only_tasks = [
                    task for task in pending
                    if _norm(task[1]) in kie_only_norms
                ]
                pending = [
                    task for task in pending
                    if _norm(task[1]) not in kie_only_norms
                ]
                # A fresh deferred failure commits baseline + rescan_pending.
                # Those files are in rescan_tasks, not pending; they must also
                # consume their durable KIE snapshot before considering full OCR.
                kie_rescan_tasks = [
                    task for task in rescan_tasks if _norm(task[1]) in kie_only_norms
                ]
                rescan_tasks = [
                    task for task in rescan_tasks if _norm(task[1]) not in kie_only_norms
                ]

                # HAI PHA CÓ RÀO (R6): nhóm quét lại file mật kết thúc HOÀN
                # TOÀN trước khi nhóm pending bắt đầu — chỉ "đặt trước" trong
                # cửa sổ 32 future không bảo đảm thứ tự đó.
                if kie_rescan_tasks:
                    run_phase(kie_rescan_tasks, True, kie_only_norms)
                if rescan_tasks:
                    run_phase(rescan_tasks, True)
                if not self._cancel_event.is_set() and kie_only_tasks:
                    run_phase(kie_only_tasks, False, kie_only_norms)
                if not self._cancel_event.is_set():
                    run_phase(pending, False)
                if pending_commits:
                    # GĐ K (PLAN v5.1 §4.3): completion = metadata CUỐI —
                    # chờ pool KIE terminal hết rồi commit phần chưa ACK.
                    # P1-09: đa số file đã được commit THEO ACK ở trên — ở
                    # đây chỉ còn những file callback chưa kịp về.
                    # R2-01: chạy cả khi cancel — entry chưa commit phải được
                    # chuyển nghĩa vụ retry bền vững, không bị bỏ.
                    from scanindex.core import secret_scan_kie as _ssk

                    if not self._cancel_event.is_set():
                        try:
                            drain_to = float(
                                os.environ.get("OCRTOOL_KIE_DRAIN_TIMEOUT_S")
                                or 600
                            )
                        except ValueError:
                            drain_to = 600.0
                        drained = _ssk.drain(timeout=drain_to)
                        if not drained:
                            # P1-07: drain False = metadata CHƯA đủ — commit
                            # sẽ giữ file resumable (không cache-final),
                            # không im lặng coi như hoàn tất.
                            self.log_message.emit(
                                "KIE deferred: hết thời gian chờ metadata — "
                                "các file còn pending KHÔNG được cache; sẽ "
                                "quét lại lượt sau",
                                "info",
                            )
                        # R2-02: TỐI ĐA 2 lượt thử trong drain — lỗi persist
                        # nhất thời (fsync lỗi một lần) được thử lại ngay,
                        # publish không phát lại (ui_done + journal idempotent
                        # theo định danh file). Vẫn lỗi → leftover xử lý bên
                        # dưới (failure + rescan + giữ journal).
                        # MỌI entry đều đăng ký `commit` trước khi commit nào
                        # chạy (R2-01) — không còn nhánh không-commit-fn.
                        for _drain_attempt in (1, 2):
                            for entry in list(pending_commits):
                                entry["commit"]()
                            with state_lock:
                                if all(
                                    e["committed"] for e in pending_commits
                                ):
                                    break
                    # R2-01/R2-02: entry CHƯA commit thành công (persist lỗi
                    # lặp / callback muộn sau cancel) → KHÔNG được mất cả kết
                    # quả lẫn nghĩa vụ retry: đếm failure, giữ journal, đưa
                    # file vào tập quét lại. KHÔNG clear vô điều kiện.
                    leftover = [
                        e for e in pending_commits if not e["committed"]
                    ]
                    if leftover:
                        with state_lock:
                            prog.set_rescan_pending(sorted(
                                set(prog.rescan_pending)
                                | {e["rel"] for e in leftover}
                            ))
                            try:
                                prog.save()
                            except Exception:
                                logging.getLogger(__name__).exception(
                                    "save journal khi chốt leftover lỗi — "
                                    "giữ trạng thái dở để lượt sau chạy lại"
                                )
                        with progress_lock:
                            failures[0] += len(leftover)
                        self.log_message.emit(
                            f"KIE deferred: {len(leftover)} file chưa commit "
                            "được (lỗi ghi dữ liệu) — giữ journal + quét lại "
                            "lượt sau",
                            "err",
                        )
                        pending_commits.clear()
        finally:
            if cancelled[0] or self._cancel_event.is_set():
                # P2-3/P1-05: dừng lượt → hủy job KIE của ĐÚNG lượt này. Job
                # đang chạy giữ lease tới khi future thật kết thúc (native
                # không bị tuyên bố dừng oan); input không bị cleanup sớm.
                try:
                    from scanindex.core import secret_scan_kie as _ssk

                    n = _ssk.cancel_run(run_id)
                    if n:
                        self.log_message.emit(
                            f"Đã hủy {n} job KIE deferred của lượt quét",
                            "info",
                        )
                except Exception:
                    # P1-B (review 2): lỗi hủy KHÔNG được nuốt im lặng —
                    # scanner không báo được vòng đời file nếu cancel hỏng.
                    logging.getLogger(__name__).exception(
                        "cancel_run lỗi (run %s) — job KIE có thể còn giữ "
                        "lease tới khi native tự kết thúc", run_id,
                    )
            # P1-E: ĐÓNG quyền commit TRƯỚC khi save/close journal — callback
            # KIE muộn (future kết thúc sau điểm này) chỉ được bỏ, không được
            # mở lại journal đã discard/close. run_gate_lock là barrier:
            # commit đang bay chắc chắn xong trước khi ta chạm journal.
            with run_gate_lock:
                run_gate["open"] = False
            with state_lock:
                # Chưa xong hẳn → giữ journal: dừng tay, còn file lỗi, còn
                # file mật chờ quét lại, hoặc migration nâng cấp chưa xác
                # nhận xong (R1 — không chỉ đếm lỗi/hủy của lượt hiện tại).
                incomplete = (
                    cancelled[0]
                    or producer_stop.is_set()
                    or failures[0] > 0
                    or prog.needs_continue()
                    or not upgraded_ok
                )
                # P2-I: barrier bền — mọi entry đã commit đã save (fsync)
                # trong commit_artifact; trước khi quyết định discard, chắc
                # chắn registry đã bền vững phần dữ liệu vừa ghi muộn.
                if registry is not None:
                    if registry.poisoned and registry.recover_writer():
                        self.log_message.emit(
                            "Lỗi lưu registry đã được phục hồi (rebuild từ "
                            "snapshot) — dữ liệu không mất",
                            "info",
                        )
                    try:
                        registry.save()
                    except Exception:
                        logging.getLogger(__name__).exception(
                            "registry save cuối lượt lỗi — giữ journal để "
                            "lượt sau đối chiếu"
                        )
                        incomplete = True
                # R2-01: giữ journal khi CÓ NGHĨA VỤ chưa xong kể cả khi
                # stats rỗng (mọi file fail trước khi ghi — trước đây điều
                # kiện `(done_n or ...) and incomplete` làm discard mất
                # rescan_pending vừa đặt → mất nghĩa vụ retry).
                if incomplete:
                    # D0: writer POISONED → recovery có kiểm soát MỘT lần
                    # (backup + rebuild snapshot từ RAM) rồi save lại; vẫn
                    # lỗi → log err, GIỮ journal (không discard dữ liệu còn
                    # trên đĩa) — lượt sau tự chữa ở load().
                    if prog.poisoned and prog.recover_writer():
                        self.log_message.emit(
                            "Lỗi lưu journal đã được phục hồi (rebuild từ "
                            "snapshot) — dữ liệu không mất",
                            "info",
                        )
                    try:
                        prog.save()
                    except Exception:
                        logging.getLogger(__name__).exception(
                            "save journal cuối lượt lỗi (writer poisoned?) — "
                            "giữ trạng thái dở để lượt sau chạy lại"
                        )
                        self.log_message.emit(
                            "KHÔNG THỂ lưu an toàn trạng thái lượt quét "
                            "(lỗi đĩa) — kiểm tra ổ đĩa; lượt sau sẽ chạy "
                            "lại phần dở",
                            "err",
                        )
                else:
                    # Hoàn tất sạch — mọi việc phục hồi đã xong và kết quả
                    # cần giữ đã nằm trong registry.
                    try:
                        prog.save()
                        prog.kie_spool_gc(durability_confirmed=True)
                        prog.save()
                        prog.discard()
                    except Exception:
                        incomplete = True
                        logging.getLogger(__name__).exception(
                            "Không thể chốt journal/GC — giữ nghĩa vụ phục hồi"
                        )
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
                    "rescan_pending": len(prog.rescan_pending),
                    "metadata_pending": len(prog.kie_spool_pending()),
                    "durability_confirmed": not incomplete,
                    "cancelled": cancelled[0],
                    "producer_stopped": producer_stop.is_set(),
                    "stop_reason": producer_stop_reason[0],
                    "work_root": work_root,
                    "app_version": app_version,
                    "updated": updated[0],
                    "kept_old": kept_old[0],
                    # Nguồn cho minh chứng rà soát (sheet "Tom tat" của file
                    # xuất) — đủ để tái hiện bối cảnh lượt quét.
                    "folder": folder,
                    "started": started_display,
                    "mode": mode_key,
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

    def _preview_delete_clicked(self) -> None:
        if self._preview_current is None:
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
        if self._preview_current is None:
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
        items = self._result_items_by_file.get(_norm(path), [])
        return min((item.row() for item in items), default=None)

    def _delete_files(self, paths: list[str]) -> None:
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
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            translations.localize_text(
                "Load danh sách văn bản mật từ file Excel (chọn được nhiều file)"
            ),
            start_dir,
            translations.localize_text("Excel (*.xlsx)"),
        )
        if not paths:
            return
        matches: list[SecretScanMatch] = []
        errors: list[str] = []
        for path in paths:
            try:
                matches.extend(load_secret_matches_from_excel(path))
            except Exception as exc:
                errors.append(f"{path}\n→ {exc}")
        if errors:
            self._show_path_errors(
                translations.localize_text(
                    f"Không đọc được {len(errors)}/{len(paths)} file "
                    "(các file còn lại vẫn được nạp nếu có dòng):"
                ),
                errors,
            )
        if not matches:
            if not errors:
                QMessageBox.information(
                    self, "Không có dòng nào",
                    translations.localize_text(
                        "Các file không chứa dòng văn bản mật nào."
                    ),
                )
            return
        # Gộp nhiều lần quét hay bị chen dòng trùng (cùng file quét 2 lần):
        # cùng file + trang + độ mật là một dấu → chỉ giữ dòng đầu.
        unique, dup_count = _dedupe_matches(matches)
        if self.table.rowCount() > 0:
            answer = self._ask_replace_or_merge(len(unique), dup_count)
            if answer == "cancel":
                return
            replace = answer == "replace"
        else:
            replace = True
        shown, missing, skipped = self._load_matches_into_table(
            unique, replace=replace
        )
        first = os.path.abspath(paths[0])
        if replace:
            self._loaded_from = first
            self._loaded_files = {os.path.abspath(p) for p in paths}
        else:
            if not self._loaded_from:
                self._loaded_from = first
            self._loaded_files.update(os.path.abspath(p) for p in paths)
        # Gán lại thư mục quét gốc cho nút "Bắt đầu quét" CHỈ khi toàn bộ
        # dòng cùng một gốc; danh sách gộp đa thư mục thì giữ nguyên ô
        # thư mục để người dùng tự chọn nơi quét tiếp.
        roots: dict[str, str] = {}
        for m in unique:
            root = _match_scan_root(m)
            if root:
                roots.setdefault(_norm(root), root)
        if len(roots) == 1:
            self.folder_edit.setText(next(iter(roots.values())))
        bits = [
            f"Đã load {shown} dòng từ {len(paths)} file Excel"
            + ("" if replace else " (gộp thêm)")
        ]
        if dup_count:
            bits.append(f"{dup_count} dòng trùng bị bỏ qua")
        if missing:
            bits.append(
                f"{missing} dòng có file không còn trên đĩa (hiển thị mờ)"
            )
        if skipped:
            if replace:
                bits.append(
                    f"{skipped} dòng bị bỏ qua do đã xác nhận "
                    '"không phải mật"'
                )
            else:
                bits.append(
                    f"{skipped} dòng bị bỏ qua (đã có trong bảng hoặc đã "
                    'xác nhận "không phải mật")'
                )
        if len(roots) > 1:
            bits.append(
                f"danh sách gồm {len(roots)} thư mục gốc — dùng \"Sắp xếp: "
                "Đường dẫn\" để xem theo từng thư mục"
            )
        self._set_status(" • ".join(bits))
        self.log_message.emit("Quét file mật: " + " - ".join(bits), "info")
        QMessageBox.information(
            self, "Đã load danh sách", "\n\n".join(bits)
        )

    def _ask_replace_or_merge(self, new_rows: int, dup_count: int) -> str:
        """Hỏi xử lý danh sách vừa chọn khi bảng đang có nội dung:
        "merge" (nối thêm) / "replace" (thay toàn bộ) / "cancel"."""
        extra = (
            f"\n({dup_count} dòng trùng trong phần vừa chọn sẽ bị bỏ qua)"
            if dup_count
            else ""
        )
        box = QMessageBox(self)
        box.setWindowTitle(
            translations.localize_text("Bảng đang có danh sách")
        )
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(
            translations.localize_text(
                f"Bảng đang có {self.table.rowCount()} dòng.\n"
                f"Danh sách vừa chọn có {new_rows} dòng.{extra}\n\n"
                "• Gộp thêm: nối vào danh sách đang có — xem cùng lúc các file\n"
                "  mật của nhiều thư mục, mỗi dòng vẫn giữ đúng đường dẫn của nó.\n"
                "• Thay thế: bỏ danh sách đang có (chưa xuất Excel sẽ mất)."
            )
        )
        merge_btn = box.addButton(
            translations.localize_text("Gộp thêm"),
            QMessageBox.ButtonRole.AcceptRole,
        )
        box.addButton(
            translations.localize_text("Thay thế"),
            QMessageBox.ButtonRole.AcceptRole,
        )
        cancel_btn = box.addButton("Hủy", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(merge_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is merge_btn:
            return "merge"
        if clicked is cancel_btn:
            return "cancel"
        return "replace"

    def _load_matches_into_table(
        self, matches: list[SecretScanMatch], *, replace: bool
    ) -> tuple[int, int, int]:
        """Nạp danh sách match vào bảng (replace=True xóa bảng trước).

        Khi gộp (replace=False): bỏ dòng trùng LẪN VỀ PHÍA BẢNG ĐANG CÓ —
        cùng file (đường dẫn chuẩn hóa) + trang + độ mật là một dấu. Trả về
        (số dòng hiển thị, số dòng file đã mất trên đĩa, số dòng bị bỏ qua
        — trùng hoặc đã xác nhận "không phải mật")."""
        if replace:
            self._reset_results()

        def key_of(m: SecretScanMatch) -> tuple[str, int, str]:
            return (
                _norm(m.source_path),
                int(m.page_number or 1),
                (m.keyword or "").strip(),
            )

        seen: set[tuple[str, int, str]] = set()
        if not replace:
            for existing in self._current_matches():
                seen.add(key_of(existing))
        missing = 0
        shown = 0
        for m in matches:
            key = key_of(m)
            if key in seen:
                continue
            seen.add(key)
            gone = not os.path.exists(m.source_path)
            if gone:
                missing += 1
            if self._add_result(m, missing=gone, quiet=True):
                shown += 1
        self._apply_filter()
        self._update_select_all_state()
        self._refresh_totals()
        return shown, missing, len(matches) - shown

    def _set_status(self, text: str) -> None:
        self._pending_status = None
        self.status_label.setText(text)

    def _set_progress(self, current: int, total: int) -> None:
        self._pending_progress = None
        self.progress.setMaximum(max(1, total))
        self.progress.setValue(max(0, min(current, max(1, total))))

    def _queue_status(self, text: str) -> None:
        if not self._busy and not self._copy_running:
            self._set_status(text)
            return
        self._pending_status = text
        if not self._feedback_timer.isActive():
            self._feedback_timer.start()

    def _queue_progress(self, current: int, total: int) -> None:
        if not self._busy and not self._copy_running:
            self._set_progress(current, total)
            return
        self._pending_progress = (current, total)
        if not self._feedback_timer.isActive():
            self._feedback_timer.start()

    def _flush_worker_feedback(self) -> None:
        """Coalesce display-only feedback; results/actions are never queued."""
        self._feedback_timer.stop()
        if self._pending_status is not None:
            self._set_status(self._pending_status)
        if self._pending_progress is not None:
            self._set_progress(*self._pending_progress)

    def _add_result(
        self, match: SecretScanMatch, missing: bool = False, quiet: bool = False
    ) -> bool:
        """Thêm một dòng vào bảng. Trả False nếu dòng bị chặn (file đã xác
        nhận "không phải mật" mà chưa đổi nội dung)."""
        # Cửa chặn duy nhất cho CẢ BA nguồn dòng mật (quét mới, resume dở,
        # cache "Tận dụng kết quả đã quét"): file đã được xác nhận "không
        # phải mật" và chưa đổi nội dung thì không bao giờ hiện lại.
        if self._not_secret.is_marked(match.source_path):
            return False
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
        # Programmatic insertion is not a user checkbox change. In
        # particular it must not recount every existing checkbox.
        with QSignalBlocker(self.table):
            self.table.setItem(row, 0, check_item)
        org_code = _effective_org_code(match)
        org_name = _effective_org_name(match)
        values = [
            match.keyword,
            match.relative_path,
            org_code,
            org_name,
            _format_file_mtime(_file_mtime_for_display(match)),
            str(match.page_number),
            match.mode,
            match.note,
        ]
        for col, value in enumerate(values, start=1):
            item = QTableWidgetItem(value)
            # Chỉ các cột thông tin được sửa tay mới nhận cờ editable; ô
            # không sửa được (File, Ngày, Trang, Chế độ) bỏ cờ để double-
            # click vẫn mở file bên ngoài như cũ.
            if col in _EDITABLE_COLS:
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            else:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            if col == 7:
                translations.set_translatable_item_text(
                    item, value, sync_tooltip=True
                )
            elif col == 8:
                translations.set_translatable_item_text(
                    item, value, context="secret_note", sync_tooltip=True
                )
            item.setData(Qt.ItemDataRole.UserRole, match.source_path)
            item.setData(Qt.ItemDataRole.UserRole + 1, match)
            if col not in (7, 8):
                item.setToolTip(match.source_path if col == 2 else value)
            if col == 1:
                item.setForeground(QColor(COLOR_RED))
            if missing:
                item.setForeground(QColor(COLOR_TEXT_SECONDARY))
            self.table.setItem(row, col, item)
        org_item = self.table.item(row, 4)
        if org_item is not None:
            if match.org_name_override:
                org_item.setToolTip(
                    translations.localize_text(
                        "Tên cơ quan đã sửa tay trên bảng "
                        "(ưu tiên khi xuất Excel)"
                    )
                )
            elif org_item.text().endswith("*"):
                org_item.setToolTip(
                    "Cơ quan theo văn bản (bóc bằng KIE) — không tra được tên "
                    'xác định qua danh mục mã định danh (dấu "*")'
                )
        code_item = self.table.item(row, 3)
        if code_item is not None and match.org_code_override:
            code_item.setToolTip(
                translations.localize_text(
                    "Mã cơ quan đã sửa tay trên bảng (không suy từ tên file)"
                )
            )
        if not missing and _match_meets_declassification(match):
            note_item = self.table.item(row, 8)
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
        if match.source_path:
            key = _norm(match.source_path)
            self._result_file_keys.add(key)
            self._result_items_by_file.setdefault(key, []).append(check_item)
        terms = self.filter_edit.text().strip().lower().split()
        haystack = self._row_filter_text(row) if terms else ""
        visible = all(term in haystack for term in terms)
        if not visible:
            self.table.setRowHidden(row, True)
        else:
            self._visible_result_rows += 1
            header = self.table.horizontalHeader()
            if header.check_state() == Qt.CheckState.Checked:
                header.set_check_state(Qt.CheckState.PartiallyChecked)
        self.btn_export.setEnabled(not self._busy)
        self.btn_copy.setEnabled(not self._busy and not self._copy_running)
        if not quiet:
            self._refresh_totals()
        return True

    def _on_results_replace(self, payload: dict) -> None:
        """Slot cho signal thay thế hàng theo file (nhánh quét lại).

        Bỏ mọi hàng cũ của path (giữ nhất quán preview/checkbox qua
        _remove_file_rows) rồi thêm hàng mới — kết quả rỗng đồng nghĩa file
        không còn được xem là mật (FP bản cũ rớt khỏi danh sách).
        """
        path = str(payload.get("path") or "")
        if not path:
            return
        old_items = self._result_items_by_file.get(_norm(path), [])
        overrides = {}
        for item in old_items:
            old = item.data(Qt.ItemDataRole.UserRole + 1)
            if isinstance(old, SecretScanMatch):
                overrides[old.page_number] = (old.org_code_override, old.org_name_override)
        self._remove_file_rows(path)
        for match_dict in payload.get("matches") or []:
            try:
                match = SecretScanMatch(**match_dict)
                code, name = overrides.get(match.page_number, ("", ""))
                if code:
                    match.org_code_override = code
                if name:
                    match.org_name_override = name
                self._add_result(match, quiet=True)
            except (TypeError, ValueError):
                continue
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
        visible = self._visible_result_rows
        files = len(self._result_file_keys)
        if rows == 0:
            text = translations.localize_text("Tổng: 0 văn bản mật")
        elif rows == files:
            text = translations.localize_text(f"Tổng: {files} văn bản mật")
        else:
            text = translations.localize_text(
                f"Tổng: {files} văn bản mật ({rows} dòng)"
            )
        if visible != rows:
            text += translations.localize_text(
                f" — đang lọc: hiện {visible}/{rows} dòng"
            )
        self.lbl_total.setText(text)
        # 0 dòng vẫn bật xuất nếu vừa có lượt quét — minh chứng "không phát
        # hiện" cần file Excel làm bằng chứng như lượt có phát hiện.
        self.btn_export.setEnabled(
            not self._busy
            and (rows > 0 or bool(self._last_scan_summary))
        )
        self.btn_copy.setEnabled(
            not self._busy
            and rows > 0
            and not self._copy_running
        )
        self._refresh_action_buttons()

    def _refresh_action_buttons(self) -> None:
        # Xóa file / "Không phải mật" dùng được NGAY CẢ KHI ĐANG QUÉT:
        # dòng đã hiện trên bảng nghĩa là đã có thể xử lý luôn, không phải
        # đợi hết lượt. File đang bị engine đọc mà xóa hỏng → báo lỗi từng
        # file, giữ dòng, thử lại sau (worker thấy file mất sẽ tự "skip").
        has_check = bool(self._checked_paths)
        self.btn_batch_delete.setEnabled(has_check)
        has_preview = self._preview_current is not None
        self.btn_preview_delete.setEnabled(has_preview)
        self.btn_preview_not_secret.setEnabled(has_preview)

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != 0:
            return
        path = item.data(Qt.ItemDataRole.UserRole)
        if not path:
            return
        checked = item.checkState() == Qt.CheckState.Checked
        was_checked = id(item) in self._checked_row_ids
        if checked:
            self._checked_row_ids.add(id(item))
            self._checked_paths.add(_norm(path))
        else:
            self._checked_row_ids.discard(id(item))
            key = _norm(path)
            if not any(id(other) in self._checked_row_ids
                       for other in self._result_items_by_file.get(key, [])):
                self._checked_paths.discard(key)
        if not self.table.isRowHidden(item.row()):
            self._visible_checked_rows += int(checked) - int(was_checked)
        self._update_select_all_state()
        self._refresh_action_buttons()

    # ------------------------------------------------------------------
    # Sửa tay các cột thông tin (double-click / F2 → delegate commit)
    # ------------------------------------------------------------------

    def _on_edit_committed(self, index) -> None:
        item = self.table.item(index.row(), index.column())
        if item is not None:
            self._apply_cell_edit(item)

    def _apply_cell_edit(self, item: QTableWidgetItem) -> None:
        """Ghi nội dung vừa sửa NGAY vào match của dòng — bảng là nguồn
        chân lý nên "Xuất Excel"/đếm lại dùng luôn giá trị mới."""
        match = item.data(Qt.ItemDataRole.UserRole + 1)
        if not isinstance(match, SecretScanMatch):
            return
        col = item.column()
        text = item.text().strip()
        if col == 1:
            match.keyword = text
        elif col == 3:
            match.org_code_override = text
            self._refresh_org_cells(item.row())
        elif col == 4:
            match.org_name_override = text
            self._refresh_org_cells(item.row())
        elif col == 8:
            match.note = text
            # Nội dung tay — gỡ binding dịch để không bị dịch đè khi đổi
            # ngôn ngữ giao diện.
            _clear_translatable_item(item)

    def _refresh_org_cells(self, row: int) -> None:
        """Tính lại Mã CQ / Cơ quan của dòng theo thứ tự ưu tiên: gõ đè tay
        > tra madinhdanh_lookup theo mã > dự phòng KIE (kết thúc "*")."""
        item0 = self.table.item(row, 0)
        if item0 is None:
            return
        match = item0.data(Qt.ItemDataRole.UserRole + 1)
        if not isinstance(match, SecretScanMatch):
            return
        code_item = self.table.item(row, 3)
        if code_item is not None:
            code_item.setText(_effective_org_code(match))
            if match.org_code_override:
                code_item.setToolTip(
                    translations.localize_text(
                        "Mã cơ quan đã sửa tay trên bảng "
                        "(không suy từ tên file)"
                    )
                )
        name_item = self.table.item(row, 4)
        if name_item is not None:
            name_item.setText(_effective_org_name(match))
            if match.org_name_override:
                name_item.setToolTip(
                    translations.localize_text(
                        "Tên cơ quan đã sửa tay trên bảng "
                        "(ưu tiên khi xuất Excel)"
                    )
                )
            elif name_item.text().endswith("*"):
                name_item.setToolTip(
                    "Cơ quan theo văn bản (bóc bằng KIE) — không tra được tên "
                    'xác định qua danh mục mã định danh (dấu "*")'
                )

    # ------------------------------------------------------------------
    # Ô check "chọn tất cả" trên header
    # ------------------------------------------------------------------

    def _on_select_all_toggled(self, checked: bool) -> None:
        state = (
            Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        )
        # A batch selection should scan the table once, not trigger a
        # complete header recount for each changed checkbox.
        touched = set()
        with QSignalBlocker(self.table):
            for r in range(self.table.rowCount()):
                if self.table.isRowHidden(r):
                    continue  # dòng đang ẩn bởi bộ lọc không bị động đến
                item = self.table.item(r, 0)
                if item is not None:
                    item.setCheckState(state)
                    if checked:
                        self._checked_row_ids.add(id(item))
                    else:
                        self._checked_row_ids.discard(id(item))
                    path = item.data(Qt.ItemDataRole.UserRole)
                    if path:
                        touched.add(_norm(path))
                        if checked:
                            self._checked_paths.add(_norm(path))
                        else:
                            self._checked_paths.discard(_norm(path))
        for key in touched:
            if any(id(item) in self._checked_row_ids
                   for item in self._result_items_by_file.get(key, [])):
                self._checked_paths.add(key)
            else:
                self._checked_paths.discard(key)
        self._visible_checked_rows = self._visible_result_rows if checked else 0
        self._update_select_all_state()
        self._refresh_action_buttons()

    def _update_select_all_state(self) -> None:
        header = self.table.horizontalHeader()
        if not isinstance(header, _CheckAllHeader):
            return
        total, checked = self._visible_result_rows, self._visible_checked_rows
        if total and checked == total:
            state = Qt.CheckState.Checked
        elif checked:
            state = Qt.CheckState.PartiallyChecked
        else:
            state = Qt.CheckState.Unchecked
        header.set_check_state(state)

    # ------------------------------------------------------------------
    # Lọc nhanh + sắp xếp
    # ------------------------------------------------------------------

    def _apply_filter(self) -> None:
        """Bộ lọc nhanh: chỉ ẨN dòng không khớp — dữ liệu và "Xuất Excel"
        vẫn giữ toàn bộ danh sách (lọc chỉ thay đổi cách xem)."""
        terms = self.filter_edit.text().strip().lower().split()
        self._visible_result_rows = 0
        self._visible_checked_rows = 0
        for r in range(self.table.rowCount()):
            haystack = self._row_filter_text(r) if terms else ""
            visible = all(term in haystack for term in terms)
            self.table.setRowHidden(r, not visible)
            self._visible_result_rows += int(visible)
            if visible and id(self.table.item(r, 0)) in self._checked_row_ids:
                self._visible_checked_rows += 1
        self._update_select_all_state()
        self._refresh_totals()

    def _row_filter_text(self, row: int) -> str:
        parts: list[str] = [self._row_path(row)]
        for col in range(1, self.table.columnCount()):
            item = self.table.item(row, col)
            if item is not None:
                parts.append(item.text())
        return " ".join(parts).lower()

    def _on_row_clicked(self, item: QTableWidgetItem) -> None:
        if item.column() == 0:
            return  # cột checkbox: chỉ check, không đổi preview
        self._show_preview(item.row())

    def _remove_file_rows(self, path: str) -> None:
        key = _norm(path)
        items = self._result_items_by_file.pop(key, [])
        rows = []
        for item in items:
            r = item.row()
            rows.append(r)
            if id(item) in self._checked_row_ids:
                if not self.table.isRowHidden(r):
                    self._visible_checked_rows -= 1
                self._checked_row_ids.discard(id(item))
        for r in sorted(rows, reverse=True):
            if not self.table.isRowHidden(r):
                self._visible_result_rows -= 1
            self.table.removeRow(r)
        self._result_file_keys.discard(key)
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
        self._update_select_all_state()
        self._refresh_totals()

    def _reset_results(self) -> None:
        self._loaded_from = None
        self._loaded_files = set()
        # Bảng đổi nguồn (quét mới / nạp file thay bảng) → minh chứng lượt
        # quét cũ không còn đúng bối cảnh, không được bám vào lần xuất sau.
        self._last_scan_summary = None
        self._feedback_timer.stop()
        self._pending_status = None
        self._pending_progress = None
        self._checked_paths.clear()
        self._result_file_keys.clear()
        self._result_items_by_file.clear()
        self._checked_row_ids.clear()
        self._visible_checked_rows = 0
        self._visible_result_rows = 0
        self._preview_current = None
        self._preview_convert_gen += 1  # vô hiệu hoá kết quả convert cũ
        self.table.setRowCount(0)
        self.filter_edit.clear()  # mở lại mọi dòng bị ẩn bởi bộ lọc cũ
        self._update_select_all_state()
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
        self.btn_copy.setEnabled(False)
        self._refresh_totals()

    def _on_finished(self, payload: dict) -> None:
        self._flush_worker_feedback()
        self._busy = False
        total = int(payload.get("total") or 0)
        scanned = int(payload.get("scanned") or 0)
        skipped = int(payload.get("skipped") or 0)
        cache_skipped = int(payload.get("cache_skipped") or 0)
        junk = int(payload.get("junk") or 0)
        failures = int(payload.get("failures") or 0)
        cancelled = bool(payload.get("cancelled"))
        pending = max(int(payload.get("metadata_pending") or 0),
                      int(payload.get("rescan_pending") or 0))
        complete = not failures and not pending and payload.get("durability_confirmed", True)
        updated = int(payload.get("updated") or 0)
        kept_old = int(payload.get("kept_old") or 0)
        app_version = str(payload.get("app_version") or "")
        found = self.table.rowCount()
        # Thông tin minh chứng lượt quét — đặt TRƯỚC _set_running_ui vì điều
        # kiện bật nút xuất có tính đến nó (quét 0 phát hiện vẫn xuất được).
        self._last_scan_summary = {
            "folder": str(payload.get("folder") or ""),
            "started": str(payload.get("started") or ""),
            "finished": time.strftime("%d/%m/%Y %H:%M:%S"),
            "mode": str(payload.get("mode") or ""),
            "status": "Đã dừng giữa chừng" if cancelled else ("Hoàn tất" if complete else "Chưa hoàn tất"),
            "total": total,
            "scanned": scanned,
            "skipped": skipped,
            "cache_skipped": cache_skipped,
            "junk": junk,
            "failures": failures,
            "updated": updated,
            "kept_old": kept_old,
            "stop_reason": str(payload.get("stop_reason") or ""),
            "found": found,
            "app_version": app_version,
        }
        self._set_running_ui(False)
        prefix = "Đã dừng" if cancelled else ("Hoàn tất" if complete else "Chưa hoàn tất")
        detail = f"quét {scanned + skipped + cache_skipped}/{total} file"
        notes = []
        if payload.get("stop_reason"):
            notes.append(str(payload["stop_reason"]))
        if pending:
            notes.append(f"còn {pending} file chờ xử lý lại")
        if skipped:
            notes.append(f"kế thừa {skipped} file đã quét")
        if cache_skipped:
            notes.append(f"tận dụng {cache_skipped} file không đổi")
        if junk:
            notes.append(f"bỏ qua {junk} file hỏng")
        if notes:
            detail += " (" + "; ".join(notes) + ")"
        # R8: phân biệt rõ "đã cập nhật" / "giữ kết quả cũ" / "dòng kế thừa
        # chưa cập nhật" — không báo cập nhật đầy đủ khi còn file lỗi.
        if updated or kept_old:
            detail += (
                f"; cập nhật {updated} file mật, {kept_old} file giữ kết quả cũ"
            )
        if app_version:
            inherited_rows = sum(
                1
                for m in self._current_matches()
                if (m.source_version or "") != app_version
            )
            if inherited_rows:
                detail += f"; {inherited_rows} dòng kế thừa chưa cập nhật"
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
            # Cleanup hai tầng (REVIEW v3 2.2): thư mục cha chỉ xóa khi không
            # còn request native dùng hậu duệ; ngược lại defer cho janitor.
            # Registry lỗi → giữ thư mục + log (P1-01), không rmtree trực tiếp.
            try:
                from scanindex.core.ocr import direct_engine as _de

                if not _de.cleanup_path_when_idle(work_root):
                    self.log_message.emit(
                        "Dọn thư mục tạm của lượt quét muộn (còn OCR chưa "
                        "kết thúc hẳn) — sẽ tự hoàn tất.",
                        "info",
                    )
            except Exception as exc:
                self.log_message.emit(
                    f"Không dọn được thư mục tạm của lượt quét (giữ lại): {exc}",
                    "info",
                )
        try:
            from scanindex.core.ocr import direct_engine as _de

            summary = _de.admission_metrics_summary()
            if summary:
                self.log_message.emit(summary, "info")
            # Telemetry trạng thái pool (PLAN v5.1 §4.1 — schema GĐ R).
            telemetry = _de.pool_telemetry_line()
            if telemetry:
                self.log_message.emit(telemetry, "info")
        except Exception:
            pass

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
        summary = self._last_scan_summary
        if not matches and not summary:
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
        # 0 phát hiện nhưng đã quét → vẫn xuất được: file cùng định dạng
        # (sheet danh sách + sheet tóm tắt lượt quét), cùng cách đặt tên —
        # đây là bằng chứng đã rà thư mục, không phải loại file riêng.
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
        if os.path.abspath(dest) in self._loaded_files:
            # Không bao giờ ghi đè file đã load (kể cả khi gộp nhiều file)
            # — tự đổi tên kèm mốc giờ.
            dest = (
                os.path.splitext(dest)[0]
                + f"_moi_{time.strftime('%Y%m%d_%H%M%S')}.xlsx"
            )
            QMessageBox.information(
                self,
                "Không ghi đè file đang load",
                translations.localize_text(
                    f"File đã load không được ghi đè.\n"
                    f"Danh sách sẽ xuất ra:\n{dest}"
                ),
            )
        try:
            export_matches_to_excel(matches, dest, scan_summary=summary)
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

    # ------------------------------------------------------------------
    # Sao chép file mật sang thư mục khác (giữ cấu trúc thư mục gốc)
    # ------------------------------------------------------------------

    def _copy_clicked(self) -> None:
        if self._copy_running:
            return
        matches = self._current_matches()
        if not matches:
            QMessageBox.information(
                self,
                "Chưa có kết quả",
                "Chưa có văn bản mật nào được phát hiện để sao chép.",
            )
            return
        plan = build_secret_copy_plan(matches)
        if not plan:
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
        dest = QFileDialog.getExistingDirectory(
            self,
            translations.localize_text("Chọn thư mục đích"),
            start_dir,
        )
        if not dest:
            return
        # Chặn chọn nhầm chính thư mục đã quét làm nơi nhận — mọi file đều
        # "đích trùng nguồn", chẳng chép được gì và dễ nhầm là có lỗi.
        root = _derive_scan_root(matches)
        if root and _norm(dest) == _norm(root):
            QMessageBox.warning(
                self,
                "Thư mục đích không hợp lệ",
                translations.localize_text(
                    "Thư mục đích trùng với thư mục đã quét.\n"
                    "Hãy chọn thư mục khác để nhận bản sao."
                ),
            )
            return
        existing = [
            os.path.join(dest, rel) for _src, rel in plan
            if os.path.exists(os.path.join(dest, rel))
        ]
        overwrite = True
        if existing:
            choice = self._ask_copy_overwrite(len(existing))
            if choice is None:
                return
            overwrite = choice
        self._copy_running = True
        self.btn_copy.setEnabled(False)
        self._set_status(
            f"Chuẩn bị sao chép {len(plan)} file mật sang: {dest}"
        )
        thread = threading.Thread(
            target=self._copy_worker,
            args=(plan, dest, overwrite),
            daemon=True,
            name="secret-copy-files",
        )
        thread.start()

    def _ask_copy_overwrite(self, existing_count: int) -> "bool | None":
        """Hỏi cách xử lý file trùng tên sẵn có ở đích: True=ghi đè,
        False=bỏ qua file đã có, None=hủy."""
        box = QMessageBox(self)
        box.setWindowTitle(translations.localize_text("File đã tồn tại ở đích"))
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(
            translations.localize_text(
                f"Đã có {existing_count} file trùng tên trong thư mục đích.\n"
                "Ghi đè lên chúng hay bỏ qua các file đã có?"
            )
        )
        over = box.addButton(
            translations.localize_text("Ghi đè"),
            QMessageBox.ButtonRole.AcceptRole,
        )
        skip = box.addButton(
            translations.localize_text("Bỏ qua file đã có"),
            QMessageBox.ButtonRole.ActionRole,
        )
        box.addButton("Hủy", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is over:
            return True
        if clicked is skip:
            return False
        return None

    def _copy_worker(
        self,
        plan: list[tuple[str, str]],
        dest: str,
        overwrite: bool,
    ) -> None:
        """Sao chép chạy ở thread nền — KHÔNG đụng vào widget; báo tiến độ
        qua signal, kết quả tổng hợp trả về qua _copy_finished."""

        def progress(done: int, total: int, name: str) -> None:
            self._progress_changed.emit(done, total)
            self._status_changed.emit(f"Đang sao chép {done}/{total}: {name}")

        report = copy_secret_files(
            plan, dest, overwrite=overwrite, progress=progress
        )
        report["dest"] = dest
        self._copy_finished.emit(report)

    def _on_copy_finished(self, payload: dict) -> None:
        self._flush_worker_feedback()
        self._copy_running = False
        self.btn_copy.setEnabled(self.table.rowCount() > 0)
        copied = int(payload.get("copied") or 0)
        skipped = int(payload.get("skipped") or 0)
        total = int(payload.get("total") or 0)
        errors = list(payload.get("errors") or [])
        dest = str(payload.get("dest") or "")
        if errors:
            self._set_status(
                f"Không sao chép được {len(errors)}/{total} file — chi tiết trong hộp thoại lỗi."
            )
            self.log_message.emit(
                f"Quét file mật: sao chép {copied}/{total} file mật vào "
                f"{dest}, lỗi {len(errors)} file.",
                "err" if not copied else "info",
            )
            intro = translations.localize_text(
                f"Không sao chép được {len(errors)}/{total} file:"
            )
            if copied or skipped:
                intro += translations.localize_text(
                    f"\n(Đã chép được {copied} file, bỏ qua {skipped} file đã có.)"
                )
            self._show_path_errors(intro, errors)
            return
        skipped_note = ""
        if skipped:
            skipped_note = translations.localize_text(
                f"\n({skipped} file đã có ở đích được bỏ qua, không ghi đè.)"
            )
        self._set_status(f"Đã sao chép {copied} file mật vào: {dest}")
        self.log_message.emit(
            f"Quét file mật: đã sao chép {copied} file mật vào {dest}"
            + (f" (bỏ qua {skipped} file đã có)" if skipped else "")
            + ".",
            "success",
        )
        QMessageBox.information(
            self,
            "Đã sao chép file mật",
            translations.localize_text(
                f"Đã sao chép {copied} file mật vào:\n{dest}"
            )
            + skipped_note,
        )
