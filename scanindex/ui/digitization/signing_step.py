"""ArchiveStep3Sign - Step 3: bulk digital signing for archive PDFs.

The signature *settings* UI (certificate / TSA / stamp appearance) lives in
the shared :class:`scanindex.ui.widgets.signing_config_panel.SigningConfigPanel`,
which is also embedded by the standalone "Ký số hàng loạt" tool. This screen
keeps the archive workflow logic — routing signed PDFs into the session temp
dir, dossier-identity file naming, and the export/import-kho terminal actions.
"""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QFileDialog, QFrame, QHBoxLayout, QHeaderView, QLabel,
    QMessageBox, QPushButton, QSplitter, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from scanindex.ui.theme import (
    COLOR_ACCENT, COLOR_BG, COLOR_BORDER, COLOR_BORDER_DEFAULT, COLOR_ELEVATED,
    COLOR_GREEN, COLOR_GREEN_HOVER, COLOR_RED, COLOR_RED_HOVER, COLOR_SURFACE,
    COLOR_TEXT, COLOR_TEXT_MUTED, COLOR_TEXT_SECONDARY, FONT_UI,
)

try:
    from scanindex.infra.paths import get_base_dir
except Exception:
    def get_base_dir():
        return os.getcwd()

try:
    from scanindex.core.pdf.win_cert_store import free_cert_contexts, list_certificates
    from scanindex.core.pdf.signer import (
        DEFAULT_STAMP_TEMPLATE, DEFAULT_TSA_URL, SIG_BOX_DEFAULT, STAMP_TEMPLATE_FIELDS,
        STAMP_TEXT_BELOW, STAMP_TEXT_RIGHT,
        compute_stamp_natural_size, render_stamp_template, sign_single_pdf,
    )
    _DEPS_OK = True
    _IMPORT_ERR = ""
except Exception as exc:  # pyHanko/Pillow may be missing on dev machines.
    _DEPS_OK = False
    _IMPORT_ERR = str(exc)
    DEFAULT_STAMP_TEMPLATE = "Xác nhận sao tại kho lưu trữ ... {datetime}"
    DEFAULT_TSA_URL = "http://tsa.ca.gov.vn"
    STAMP_TEXT_BELOW = "below"
    STAMP_TEXT_RIGHT = "right"
    STAMP_TEMPLATE_FIELDS = (
        "cn", "org", "ou", "unit_org", "subject", "issuer", "serial",
        "not_after", "ts", "datetime", "date", "time", "reason", "location",
    )

# Shared signature-configuration panel (cert / TSA / appearance). Step 3 and
# the standalone bulk-signing tool both embed it so their settings stay
# identical and share one config file. The signing engine (sign_single_pdf)
# and the _SignWorker thread remain here; callers reuse them.
from scanindex.ui.widgets.signing_config_panel import SigningConfigPanel


_H = 26
_FONT_SM = 11
_RAD = 4
_LEFT_PANEL_W = 380


@dataclass
class _SignItem:
    source_path: str
    display_name: str
    signature_page: Optional[int] = None
    status: str = "Chờ ký"
    output_path: str = ""
    error: str = ""
    start_page: Optional[int] = None  # trang_so from Step 2 (starting page)


def _pdf_has_digital_signature(pdf_path: str) -> bool:
    """True if the PDF already carries a PKCS#7 digital signature.

    Checks for AcroForm signature widgets (`/FT == /Sig`) first (fast), then
    falls back to scanning xref objects for the `/ByteRange` + `/Contents`
    pair that marks an embedded signature dictionary. Used so a reopened
    ZIP (whose PDFs were already signed at export time) shows "Đã ký" in
    Step 3 instead of "Chờ ký"."""
    try:
        import fitz  # PyMuPDF — lazy import
        doc = fitz.open(pdf_path)
    except Exception:
        return False
    try:
        for i in range(doc.page_count):
            for widget in doc[i].widgets() or []:
                if widget.field_type_string == "Signature":
                    return True
        for xref in range(1, doc.xref_length()):
            try:
                obj = doc.xref_object(xref)
            except Exception:
                continue
            if "/ByteRange" in obj and "/Contents" in obj:
                return True
        return False
    finally:
        doc.close()


def _page_count(path: str) -> int:
    try:
        from pypdf import PdfReader
        return len(PdfReader(path).pages)
    except Exception:
        return 0


def _safe_pdf_name(name: str, fallback: str = "document.pdf") -> str:
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(name or ""))
    text = text.strip(" .")
    if not text:
        text = fallback
    if not text.lower().endswith(".pdf"):
        text += ".pdf"
    return text


def _unique_output_path(source_path: str, output_dir: str,
                        output_name: str = "") -> str:
    name = _safe_pdf_name(output_name or os.path.basename(source_path))
    dst = os.path.join(output_dir, name)
    if os.path.abspath(dst) != os.path.abspath(source_path):
        return dst
    base, ext = os.path.splitext(name)
    return os.path.join(output_dir, f"{base}_signed{ext}")


def _resolve_page(item: _SignItem, custom_page: int) -> int:
    page_total = _page_count(item.source_path)
    last_page = max(0, page_total - 1)
    page = custom_page
    return max(0, min(page, last_page))


class _SignWorker(QThread):
    progress = Signal(int, int, object)
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        items: list[_SignItem],
        output_dir: str,
        cert_info: dict,
        sig_box: tuple[float, float, float, float],
        custom_page: int,
        stamp_template: str,
        stamp_image_path: str = "",
        stamp_text_position: str = STAMP_TEXT_BELOW,
        tsa_url: str = "",
        enable_pdfa: bool = False,
        avoid_text_overlap: bool = True,
        parent=None,
    ):
        super().__init__(parent)
        self._items = list(items)
        self._output_dir = output_dir
        self._cert_info = cert_info
        self._sig_box = sig_box
        self._custom_page = custom_page
        self._stamp_template = stamp_template
        self._stamp_image_path = str(stamp_image_path or "").strip()
        self._stamp_text_position = (
            STAMP_TEXT_RIGHT
            if str(stamp_text_position or "").strip() == STAMP_TEXT_RIGHT
            else STAMP_TEXT_BELOW
        )
        self._tsa_url = str(tsa_url or "").strip()
        self._enable_pdfa = bool(enable_pdfa)
        self._avoid_text_overlap = bool(avoid_text_overlap)
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        import tempfile
        try:
            os.makedirs(self._output_dir, exist_ok=True)
            total = len(self._items)
            results = []
            for idx, item in enumerate(self._items):
                if self._cancelled:
                    break
                pdfa_temp = None  # để cleanup nếu PDF/A convert tạo file tạm
                try:
                    page = _resolve_page(item, self._custom_page)
                    dst = _unique_output_path(
                        item.source_path, self._output_dir, item.display_name
                    )
                    # Nguồn để ký = source gốc, hoặc PDF/A converted nếu user bật.
                    sign_input = item.source_path
                    if self._enable_pdfa:
                        from scanindex.core.pdf.pdfa_converter import convert_to_pdfa
                        fd, pdfa_temp = tempfile.mkstemp(suffix="_pdfa.pdf")
                        os.close(fd)
                        ok_a, err_a = convert_to_pdfa(item.source_path, pdfa_temp)
                        if ok_a:
                            sign_input = pdfa_temp
                        else:
                            # Convert thất bại → fallback ký file gốc, log warning
                            # qua exception (sẽ append message vào result.error)
                            raise RuntimeError(
                                f"PDF/A convert failed: {err_a}. "
                                "Fallback: ký file gốc thay vì PDF/A."
                            )
                    sign_single_pdf(
                        sign_input,
                        dst,
                        self._cert_info,
                        sig_box=self._sig_box,
                        page=page,
                        reason=None,
                        location=None,
                        stamp_template=self._stamp_template,
                        tsa_url=self._tsa_url,
                        stamp_image_path=self._stamp_image_path or None,
                        stamp_text_position=self._stamp_text_position,
                        avoid_text_overlap=self._avoid_text_overlap,
                    )
                    result = {
                        "index": idx,
                        "source_path": item.source_path,
                        "output_path": dst,
                        "ok": True,
                        "error": "",
                        "page": page,
                    }
                except Exception as exc:
                    result = {
                        "index": idx,
                        "source_path": item.source_path,
                        "output_path": "",
                        "ok": False,
                        "error": str(exc),
                        "page": None,
                    }
                finally:
                    if pdfa_temp and os.path.exists(pdfa_temp):
                        try:
                            os.remove(pdfa_temp)
                        except OSError:
                            pass
                results.append(result)
                self.progress.emit(idx + 1, total, result)
            self.finished_ok.emit(results)
        except Exception as exc:
            self.failed.emit(str(exc))


class ArchiveStep3Sign(QWidget):
    """Step 3 screen that signs the PDFs produced by Step 2."""

    log_message = Signal(str)
    export_clicked = Signal()        # Xuất hồ sơ nén ra thư mục ngoài
    import_kho_clicked = Signal()    # Chuyển vào Kho lưu trữ nội bộ

    def __init__(self, session=None, parent=None):
        super().__init__(parent)
        # `session` is the per-run ArchiveSession owned by ArchiveContainer.
        # Step 3 reads it to route signed PDFs into <temp>/_step3_signed/.
        # Allowed to be None for unit tests / standalone preview — in that
        # case _signed_dir() falls back to the OS temp dir.
        self._session = session
        self._items: list[_SignItem] = []
        self._worker: Optional[_SignWorker] = None

        self.setStyleSheet(f"background: {COLOR_BG}; color: {COLOR_TEXT};")
        # SigningConfigPanel is created inside _setup_ui (it builds the left
        # column). It loads templates/settings/certs in its own constructor.
        self._setup_ui()

    # ------------------------------------------------------------------ UI

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._build_toolbar(root)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        split.setStyleSheet(f"QSplitter::handle {{ background: {COLOR_BORDER}; width: 3px; }}")

        # The left column is the shared SigningConfigPanel (cert / TSA /
        # signature appearance). It owns the templates, settings and the
        # certificate list — Step 3 reads them back via get_* accessors.
        self._panel = SigningConfigPanel()
        self._panel.log_message.connect(self.log_message.emit)

        left_scroll = _wrap_in_scroll(self._panel)
        left_scroll.setMinimumWidth(260)

        right = QWidget()
        right_l = QVBoxLayout(right)
        right_l.setContentsMargins(6, 8, 8, 8)
        right_l.setSpacing(6)
        self._build_file_section(right_l)

        split.addWidget(left_scroll)
        split.addWidget(right)
        split.setSizes([_LEFT_PANEL_W, 760])
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        root.addWidget(split, 1)

    def _build_toolbar(self, root: QVBoxLayout):
        bar = QFrame()
        bar.setFixedHeight(34)
        bar.setStyleSheet(
            f"QFrame {{ background: {COLOR_SURFACE}; border-bottom: 1px solid {COLOR_BORDER}; }}"
        )
        h = QHBoxLayout(bar)
        h.setContentsMargins(8, 0, 8, 0)
        h.setSpacing(6)

        # Step 2's output is auto-loaded into Step 3 when the user opens the
        # tab (see container._prepare_step3), so there is no manual "load"
        # button — this label just reports how many files were carried over.
        self._lbl_load_info = QLabel("Chưa nạp file")
        self._lbl_load_info.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; font-size: {_FONT_SM}px;"
            f" padding: 0 6px;"
        )
        h.addWidget(self._lbl_load_info)

        self._btn_open_output = self._button("Mở thư mục đã ký", "success")
        self._btn_open_output.clicked.connect(self._open_output_dir)
        h.addWidget(self._btn_open_output)

        self._btn_edit_dossier = self._button("Sửa thông tin hồ sơ", "success")
        self._btn_edit_dossier.clicked.connect(self._edit_dossier_info)
        h.addWidget(self._btn_edit_dossier)

        self.lbl_status = QLabel("Sẵn sàng")
        self.lbl_status.setStyleSheet(f"color: {COLOR_TEXT_SECONDARY}; font-size: {_FONT_SM}px;")
        h.addWidget(self.lbl_status, 1)

        self.btn_sign = self._button("Ký số hàng loạt", "success")
        self.btn_sign.clicked.connect(self._start_signing)
        h.addWidget(self.btn_sign)

        # The two terminal actions of the workflow — both independent.
        # "Xuất hồ sơ nén" writes Excel + final PDFs to a user-chosen folder.
        # "Chuyển vào Kho" imports the dossier into the internal Kho lưu trữ.
        self._btn_export = self._button("Xuất hồ sơ nén", "success")
        self._btn_export.clicked.connect(self.export_clicked.emit)
        h.addWidget(self._btn_export)

        self._btn_import_kho = self._button("Chuyển vào Kho", "success")
        self._btn_import_kho.clicked.connect(self.import_kho_clicked.emit)
        h.addWidget(self._btn_import_kho)
        root.addWidget(bar)

    def _build_file_section(self, parent: QVBoxLayout):
        hdr = QHBoxLayout()
        hdr.setSpacing(4)
        title = QLabel("Danh sách PDF cần ký")
        title.setStyleSheet(
            f"font-size: {_FONT_SM}px; font-weight: 600; color: {COLOR_TEXT_SECONDARY}; "
            f"font-family: {FONT_UI}; text-transform: uppercase;"
        )
        hdr.addWidget(title)
        self.lbl_count = QLabel("0 file")
        self.lbl_count.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: {_FONT_SM}px;")
        hdr.addWidget(self.lbl_count)
        hdr.addStretch()

        self._btn_add_files = self._button("Thêm file", "ghost")
        self._btn_add_files.clicked.connect(self._add_files)
        hdr.addWidget(self._btn_add_files)
        self._btn_add_folder = self._button("Thêm thư mục", "ghost")
        self._btn_add_folder.clicked.connect(self._add_folder)
        hdr.addWidget(self._btn_add_folder)
        self._btn_remove = self._button("Xóa chọn", "ghost")
        self._btn_remove.clicked.connect(self._remove_selected)
        hdr.addWidget(self._btn_remove)
        self._btn_clear = self._button("Xóa tất cả", "ghost")
        self._btn_clear.clicked.connect(self._clear_items)
        hdr.addWidget(self._btn_clear)
        parent.addLayout(hdr)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Tên tập tin", "Trang số", "Trạng thái", "File đã ký"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        # "Tên tập tin" fits the longest name; "Trang số"/"Trạng thái" fit
        # their content; "File đã ký" stretches to absorb the remaining space.
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setStyleSheet(f"""
            QTableWidget {{
                background: {COLOR_BG};
                border: 1px solid {COLOR_BORDER};
                border-radius: {_RAD}px;
                gridline-color: {COLOR_BORDER};
                color: {COLOR_TEXT};
                font-size: {_FONT_SM}px;
            }}
            QHeaderView::section {{
                background: {COLOR_SURFACE};
                color: {COLOR_TEXT_SECONDARY};
                border: none;
                border-right: 1px solid {COLOR_BORDER};
                border-bottom: 1px solid {COLOR_BORDER};
                padding: 4px 6px;
                font-size: {_FONT_SM}px;
            }}
            QTableWidget::item {{ padding: 3px 6px; }}
            QTableWidget::item:selected {{ background: {COLOR_ACCENT}; color: white; }}
        """)
        parent.addWidget(self.table, 1)

    def _button(self, text: str, role: str = "ghost") -> QPushButton:
        b = QPushButton(text)
        b.setFixedHeight(_H)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        if role == "success":
            bg, hover, border, color, weight = COLOR_GREEN, COLOR_GREEN_HOVER, "none", "#fff", "600"
        elif role == "danger":
            bg, hover, border, color, weight = COLOR_RED, COLOR_RED_HOVER, "none", "#fff", "600"
        else:
            bg, hover, border, color, weight = "transparent", COLOR_ELEVATED, f"1px solid {COLOR_BORDER_DEFAULT}", COLOR_TEXT_SECONDARY, "400"
        b.setStyleSheet(f"""
            QPushButton {{
                background: {bg}; border: {border}; border-radius: {_RAD}px;
                color: {color}; font-size: {_FONT_SM}px; font-family: {FONT_UI};
                font-weight: {weight}; padding: 0 10px;
            }}
            QPushButton:hover {{
                background: {hover}; color: {COLOR_TEXT};
                border-color: {COLOR_ACCENT if role == "ghost" else "transparent"};
            }}
            QPushButton:disabled {{
                color: {COLOR_TEXT_MUTED}; background: {COLOR_ELEVATED};
                border-color: {COLOR_BORDER};
            }}
        """)
        return b

    # ------------------------------------------------------------- documents

    def _display_name_for_source(self, row_index: int, source_path: str,
                                 doc: dict | None = None) -> str:
        identity = getattr(self._session, "identity", None)
        # Prefer the doc's so_thu_tu so the Bước 3 display name matches the
        # ZIP export name; fall back to the 1-based row position.
        stt = row_index + 1
        if doc is not None:
            raw = str((doc.get("metadata") or {}).get("so_thu_tu", "")).strip()
            try:
                n = int(raw)
                if n > 0:
                    stt = n
            except (ValueError, TypeError):
                pass
        if identity is not None:
            try:
                if identity.is_complete():
                    return _safe_pdf_name(identity.make_segment_name(stt))
            except Exception:
                pass
        return _safe_pdf_name(os.path.basename(source_path))

    def _signed_output_path(self, item: _SignItem) -> str:
        return _unique_output_path(item.source_path, self._signed_dir(), item.display_name)

    def _refresh_identity_file_names(self, rename_signed: bool = False) -> int:
        moved = 0
        out_dir = self._signed_dir()
        for row, item in enumerate(self._items):
            old_output = item.output_path
            old_display = item.display_name
            item.display_name = self._display_name_for_source(row, item.source_path)
            if not rename_signed:
                continue
            new_output = self._signed_output_path(item)
            candidates = []
            # ONLY consider candidates that live INSIDE the signed output
            # directory. A reopened ZIP marks its already-signed source PDFs
            # by pointing item.output_path back at the original file under
            # `_zip_input/` — that is Step 2's source-of-truth, NOT a signed
            # copy we may move. Moving it out would delete the file Step 2
            # still reads, making those documents unviewable after the next
            # navigation. So never rename anything outside _step3_signed/.
            if old_output and os.path.abspath(old_output).startswith(
                    os.path.abspath(out_dir) + os.sep):
                candidates.append(old_output)
            # Legacy signed name before this change: signed_dir/<step2 basename>.
            candidates.append(os.path.join(out_dir, os.path.basename(item.source_path)))
            # Previous canonical name if the user edited identity more than once.
            if old_display:
                candidates.append(os.path.join(out_dir, old_display))
            source = next(
                (
                    p for p in candidates
                    if p and os.path.exists(p)
                    and os.path.abspath(p) != os.path.abspath(new_output)
                ),
                "",
            )
            if source:
                try:
                    os.makedirs(os.path.dirname(new_output), exist_ok=True)
                    os.replace(source, new_output)
                    moved += 1
                except Exception as exc:
                    self.log_message.emit(
                        f"Archive Step 3: không đổi tên file đã ký {os.path.basename(source)}: {exc}"
                    )
            if os.path.exists(new_output):
                item.output_path = new_output
                if item.status == "Chờ ký":
                    item.status = "Đã ký"
        if self._items:
            self._refresh_table()
        return moved

    def set_documents(self, documents: list[dict], default_output_dir: str = ""):
        if self._worker and self._worker.isRunning():
            return
        items: list[_SignItem] = []
        for row, doc in enumerate(documents or []):
            source = doc.get("output_path") or ""
            if not source or not os.path.exists(source):
                continue
            sig_page = doc.get("signature_page")
            if sig_page is not None:
                try:
                    sig_page = int(sig_page)
                except Exception:
                    sig_page = None
            # Carry the doc's trang_so (starting page within the dossier)
            # from Step 2 so the "Trang số" column reflects where this
            # document begins — independent of the signing page spinbox.
            start_page = None
            raw_trang = str((doc.get("metadata") or {}).get("trang_so", "")).strip()
            try:
                start_page = int(raw_trang) if raw_trang else None
            except (ValueError, TypeError):
                start_page = None
            items.append(_SignItem(
                source_path=os.path.abspath(source),
                display_name=self._display_name_for_source(row, source, doc),
                signature_page=sig_page,
                start_page=start_page,
            ))
            # A reopened ZIP's PDFs are often already digitally signed at
            # export time — flag them so Step 3 shows "Đã ký" instead of
            # "Chờ ký". The output must point at a COPY inside _step3_signed/,
            # NOT at the source file itself: the source lives in _zip_input/
            # and is Step 2's source-of-truth. Pointing output_path at it
            # would let _refresh_identity_file_names rename/move it out of
            # _zip_input/ on the next identity edit, breaking Step 2.
            if _pdf_has_digital_signature(source):
                items[-1].status = "Đã ký (có sẵn)"
                signed_copy = self._signed_output_path(items[-1])
                try:
                    os.makedirs(os.path.dirname(signed_copy), exist_ok=True)
                    if not os.path.exists(signed_copy):
                        shutil.copyfile(source, signed_copy)
                    items[-1].output_path = signed_copy
                except Exception:
                    # If the copy fails, leave output_path empty so the
                    # operator can re-sign rather than corrupt Step 2's file.
                    items[-1].output_path = ""
        self._items = items
        # `default_output_dir` is no longer threaded through — signed PDFs
        # always land in `<session_temp>/_step3_signed/` per the new
        # workflow contract.
        self._refresh_table()
        if items:
            self._lbl_load_info.setText(f"Đã nạp {len(items)} file từ Bước 2")
        else:
            self._lbl_load_info.setText("Chưa có file từ Bước 2")

    def _add_item_path(self, path: str):
        path = os.path.abspath(os.path.normpath(path))
        if not path.lower().endswith(".pdf") or not os.path.exists(path):
            return
        if any(os.path.abspath(i.source_path) == path for i in self._items):
            return
        self._items.append(_SignItem(source_path=path, display_name=os.path.basename(path)))

    def _add_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Chọn file PDF", "", "PDF (*.pdf)")
        for path in paths:
            self._add_item_path(path)
        self._refresh_table()

    def _add_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Chọn thư mục chứa PDF")
        if not folder:
            return
        for root, _, files in os.walk(folder):
            for name in sorted(files):
                if name.lower().endswith(".pdf"):
                    self._add_item_path(os.path.join(root, name))
        self._refresh_table()

    def _remove_selected(self):
        rows = sorted({idx.row() for idx in self.table.selectedIndexes()}, reverse=True)
        for row in rows:
            if 0 <= row < len(self._items):
                self._items.pop(row)
        self._refresh_table()

    def _clear_items(self):
        if self._worker and self._worker.isRunning():
            return
        self._items.clear()
        self._refresh_table()

    def _refresh_table(self):
        self.table.setRowCount(len(self._items))
        for row, item in enumerate(self._items):
            self._set_table_item(row, 0, item.display_name, tooltip=item.source_path)
            self._set_table_item(row, 1, self._page_label(item))
            self._set_table_item(row, 2, item.status, status=item.status)
            self._set_table_item(row, 3, os.path.basename(item.output_path), tooltip=item.output_path)
        self.lbl_count.setText(f"{len(self._items)} file")

    def _set_table_item(self, row: int, col: int, text: str, tooltip: str = "", status: str = ""):
        cell = QTableWidgetItem(text or "")
        if tooltip:
            cell.setToolTip(tooltip)
        if status:
            color = COLOR_GREEN if status.startswith("Đã ký") else COLOR_RED if status.startswith("Lỗi") else COLOR_TEXT_SECONDARY
            cell.setForeground(QBrush(QColor(color)))
        self.table.setItem(row, col, cell)

    def _page_label(self, item: _SignItem) -> str:
        # "Trang số" shows the document's starting page (trang_so) carried
        # over from Step 2, not the signature-placement page from the spinbox.
        if item.start_page is not None and item.start_page > 0:
            return str(item.start_page)
        return ""

    # ------------------------------------------------------------- signing

    def _signed_dir(self) -> str:
        """Resolve the per-session signed-output dir. Falls back to the OS
        temp dir if no session is attached (standalone preview)."""
        if self._session is not None:
            try:
                return self._session.step3_signed_dir()
            except Exception:
                pass
        import tempfile as _tf
        return os.path.join(_tf.gettempdir(), "_step3_signed")

    def _open_output_dir(self):
        folder = self._signed_dir()
        os.makedirs(folder, exist_ok=True)
        try:
            os.startfile(folder)
        except Exception as exc:
            QMessageBox.information(self, "Thông báo",
                                     f"Không mở được thư mục:\n{folder}\n{exc}")

    def _edit_dossier_info(self):
        if self._session is None:
            QMessageBox.information(
                self,
                "Thông tin hồ sơ",
                "Không có phiên số hóa đang mở để sửa thông tin hồ sơ.",
            )
            return
        from scanindex.ui.dialogs.archive_session_dialog import DossierInfoDialog

        dlg = DossierInfoDialog(
            initial=getattr(self._session, "identity", None),
            seed_for_unstructured=getattr(self._session, "session_id", "step3"),
            parent=self,
            actual_page_count=self._total_scanned_pages(),
        )
        if not dlg.exec():
            return
        codes = dlg.result_codes()
        if codes is None:
            return
        self._session.identity = codes
        moved = self._refresh_identity_file_names(rename_signed=True)
        msg = "Archive Step 3: dossier info updated"
        if moved:
            msg += f"; renamed {moved} signed PDF file(s)"
        self.log_message.emit(msg)

    def _total_scanned_pages(self):
        """Total pages across all Step-3 sign items, for the dossier
        dialog's "Số lượng trang" mismatch warning. None if unknown."""
        if not self._items:
            return None
        total = 0
        any_found = False
        for it in self._items:
            try:
                n = _page_count(it.source_path)
            except Exception:
                n = 0
            if n:
                total += n
                any_found = True
        return total if any_found else None

    def _start_signing(self):
        if not _DEPS_OK:
            QMessageBox.critical(self, "Thiếu thư viện", _IMPORT_ERR)
            return
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self.btn_sign.setEnabled(False)
            self.lbl_status.setText("Đang dừng sau file hiện tại...")
            return
        cert = self._panel.get_cert_info()
        if cert is None:
            QMessageBox.warning(self, "Lỗi", "Hãy chọn chứng thư số.")
            return
        items = [i for i in self._items if os.path.exists(i.source_path)]
        if not items:
            QMessageBox.warning(self, "Lỗi", "Chưa có file PDF hợp lệ để ký.")
            return
        out_dir = self._signed_dir()
        os.makedirs(out_dir, exist_ok=True)
        stamp_template = self._panel.get_stamp_template()
        stamp_image_path = self._panel.get_stamp_image_path()
        stamp_text_position = self._panel.get_stamp_text_position()
        use_tsa = self._panel.is_tsa_enabled()
        tsa_url = self._panel.get_tsa_url() if use_tsa else ""
        time_decision = None
        if not stamp_template:
            QMessageBox.warning(self, "Mẫu chữ ký", "Mẫu hiển thị không được để trống.")
            return
        if stamp_image_path and not os.path.exists(stamp_image_path):
            QMessageBox.warning(self, "Hình dấu", f"Không tìm thấy hình dấu:\n{stamp_image_path}")
            return
        if use_tsa and not tsa_url:
            QMessageBox.warning(self, "Máy chủ TSA", "Nhập địa chỉ máy chủ TSA hoặc tắt chức năng cấp dấu thời gian.")
            return
        if use_tsa and not tsa_url.lower().startswith(("http://", "https://")):
            QMessageBox.warning(self, "Máy chủ TSA", "Địa chỉ TSA phải bắt đầu bằng http:// hoặc https://.")
            return
        try:
            self._panel.validate_stamp_template(cert, stamp_template)
        except KeyError as exc:
            QMessageBox.warning(self, "Mẫu chữ ký", f"Trường không hỗ trợ: {{{exc.args[0]}}}")
            return

        min_h = self._panel.minimum_visible_stamp_height(cert, stamp_template)
        if self._panel.spin_h.value() < min_h:
            self._panel.spin_h.setValue(min_h)

        if use_tsa:
            time_decision = self._panel.resolve_batch_time_decision(tsa_url)
            if time_decision is None:
                return
            tsa_url = time_decision.tsa_url
        else:
            tsa_url = ""

        for item in self._items:
            item.status = "Chờ ký"
            item.output_path = ""
            item.error = ""
        self._refresh_table()

        sig_box = self._panel.get_sig_box()
        self._panel.save_settings()
        if time_decision is not None and time_decision.mode == "tsa":
            self.log_message.emit(f"Archive Step 3: signing with TSA {tsa_url}")
        elif time_decision is not None and time_decision.mode == "local_fallback":
            self.log_message.emit(
                "Archive Step 3: batch time source fixed to local computer time "
                "after TSA connection failure"
            )
        else:
            self.log_message.emit(
                "Archive Step 3: signing without TSA; using local computer time"
            )
        self.btn_sign.setText("Dừng")
        self.btn_sign.setEnabled(True)
        self._set_inputs_enabled(False)
        self.lbl_status.setText(f"Đang ký 0/{len(items)} file...")

        self._worker = _SignWorker(
            items=items,
            output_dir=out_dir,
            cert_info=cert,
            sig_box=sig_box,
            custom_page=self._panel.get_page_index(),
            stamp_template=stamp_template,
            stamp_image_path=stamp_image_path,
            stamp_text_position=stamp_text_position,
            tsa_url=tsa_url,
            enable_pdfa=self._panel.is_pdfa(),
            avoid_text_overlap=self._panel.is_avoid_overlap(),
            parent=self,
        )
        self._worker.progress.connect(self._on_sign_progress)
        self._worker.finished_ok.connect(self._on_sign_done)
        self._worker.failed.connect(self._on_sign_failed)
        self._worker.start()

    def _on_sign_progress(self, done: int, total: int, result: dict):
        source_path = os.path.abspath(result.get("source_path") or "")
        idx = next(
            (i for i, item in enumerate(self._items)
             if os.path.abspath(item.source_path) == source_path),
            result.get("index", -1),
        )
        if 0 <= idx < len(self._items):
            item = self._items[idx]
            if result.get("ok"):
                item.status = "Đã ký"
                item.output_path = result.get("output_path", "")
                item.error = ""
            else:
                item.status = "Lỗi"
                item.output_path = ""
                item.error = result.get("error", "")
            self._set_table_item(idx, 2, item.status, tooltip=item.error, status=item.status)
            self._set_table_item(idx, 3, os.path.basename(item.output_path), tooltip=item.output_path)
        name = (
            self._items[idx].display_name
            if 0 <= idx < len(self._items)
            else os.path.basename(result.get("source_path") or "")
        )
        self.lbl_status.setText(f"Đang ký {done}/{total}: {name}")

    def _on_sign_done(self, results: list[dict]):
        ok_count = sum(1 for r in results if r.get("ok"))
        err_count = len(results) - ok_count
        self.btn_sign.setText("Ký số hàng loạt")
        self.btn_sign.setEnabled(True)
        self._set_inputs_enabled(True)
        self.lbl_status.setText(f"Xong: {ok_count} thành công, {err_count} lỗi.")
        self.log_message.emit(f"Archive Step 3: signed {ok_count}/{len(results)} PDF files")
        if err_count:
            failed = [os.path.basename(r.get("source_path", "")) for r in results if not r.get("ok")]
            QMessageBox.warning(self, "Có lỗi ký số", "\n".join(failed[:15]))

    def _on_sign_failed(self, error: str):
        self.btn_sign.setText("Ký số hàng loạt")
        self.btn_sign.setEnabled(True)
        self._set_inputs_enabled(True)
        self.lbl_status.setText(f"Ký số thất bại: {error}")
        QMessageBox.critical(self, "Lỗi ký số", error)

    def _set_inputs_enabled(self, enabled: bool):
        for widget in [
            self._btn_export, self._btn_import_kho,
            self._btn_open_output, self._btn_edit_dossier,
            self._btn_add_files, self._btn_add_folder, self._btn_remove,
            self._btn_clear,
        ]:
            widget.setEnabled(enabled)
        self._panel.set_inputs_enabled(enabled)

    # ------------------------------------------------------------- lifecycle

    def update_texts(self):
        # Step 3 currently keeps explicit Vietnamese labels because certificate
        # stores and signing errors come from the Windows/token driver.
        pass

    def cleanup(self):
        try:
            self._panel.save_settings()
        except Exception:
            pass
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(1500)
            if self._worker.isRunning():
                return
        self._panel.cleanup()


# Small helper: wrap any widget in a styled non-bordered scroll area, matching
# the original Step-3 left-column look. Kept here (not in widgets/) because it
# is only the styling wrapper the splitter uses.
def _wrap_in_scroll(widget: QWidget):
    from PySide6.QtWidgets import QScrollArea
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    scroll.setWidget(widget)
    scroll.setStyleSheet(f"""
        QScrollArea {{ background: {COLOR_BG}; border: none; }}
        QScrollBar:vertical {{ background: transparent; width: 8px; }}
        QScrollBar::handle:vertical {{
            background: {COLOR_BORDER_DEFAULT}; border-radius: 4px; min-height: 24px;
        }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    """)
    return scroll
