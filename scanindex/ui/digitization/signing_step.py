"""ArchiveStep3Sign - Step 3: bulk digital signing for archive PDFs."""
from __future__ import annotations

import os
import json
import re
from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import QPoint, Qt, QThread, Signal
from PySide6.QtGui import QColor, QBrush, QPainter, QPolygon
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QInputDialog, QMessageBox, QPushButton,
    QAbstractSpinBox, QDoubleSpinBox, QScrollArea, QSizePolicy, QSpinBox, QSplitter, QTableWidget,
    QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
)

from scanindex.ui.theme import (
    COLOR_ACCENT, COLOR_ACCENT_HOVER, COLOR_BG, COLOR_BORDER,
    COLOR_BORDER_DEFAULT, COLOR_ELEVATED, COLOR_GREEN, COLOR_GREEN_HOVER,
    COLOR_HOVER, COLOR_INPUT, COLOR_RED, COLOR_RED_HOVER, COLOR_SURFACE,
    COLOR_TEXT, COLOR_TEXT_MUTED, COLOR_TEXT_SECONDARY,
    COMBOBOX_DROPDOWN_QSS, FONT_MONO_FALLBACK,
    FONT_UI,
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
        compute_stamp_natural_size, render_stamp_template, sign_single_pdf,
    )
    _DEPS_OK = True
    _IMPORT_ERR = ""
except Exception as exc:  # pyHanko/Pillow may be missing on dev machines.
    _DEPS_OK = False
    _IMPORT_ERR = str(exc)
    DEFAULT_STAMP_TEMPLATE = "Xác nhận sao tại kho lưu trữ\n{unit_org}"
    DEFAULT_TSA_URL = "http://tsa.ca.gov.vn"
    STAMP_TEMPLATE_FIELDS = (
        "cn", "org", "ou", "unit_org", "subject", "issuer", "serial",
        "not_after", "ts", "datetime", "date", "time", "reason", "location",
    )


_H = 26
_FONT = 12
_FONT_SM = 11
_RAD = 4
_LEFT_PANEL_W = 380
_DEFAULT_TEMPLATE_NAME = "Mặc định"
_CONFIG_DIR = os.path.join(get_base_dir(), "config")
_TEMPLATE_FILE = os.path.join(_CONFIG_DIR, "sign_templates.json")
_SETTINGS_FILE = os.path.join(_CONFIG_DIR, "sign_settings.json")
_VISIBLE_TEMPLATE_FIELDS = tuple(
    f for f in STAMP_TEMPLATE_FIELDS if f not in {"reason", "location"}
)


@dataclass
class _SignItem:
    source_path: str
    display_name: str
    signature_page: Optional[int] = None
    status: str = "Chờ ký"
    output_path: str = ""
    error: str = ""


class _ComboBox(QComboBox):
    """QComboBox with an explicit down-triangle indicator."""

    def wheelEvent(self, event):
        event.ignore()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(COLOR_TEXT_SECONDARY)))
        cx = self.width() - 15
        cy = self.height() // 2 + 1
        painter.drawPolygon(QPolygon([
            QPoint(cx - 5, cy - 3),
            QPoint(cx + 5, cy - 3),
            QPoint(cx, cy + 4),
        ]))


class _NoWheelSpinBox(QSpinBox):
    def wheelEvent(self, event):
        event.ignore()


class _NoWheelDoubleSpinBox(QDoubleSpinBox):
    def wheelEvent(self, event):
        event.ignore()


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
        tsa_url: str = "",
        enable_pdfa: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self._items = list(items)
        self._output_dir = output_dir
        self._cert_info = cert_info
        self._sig_box = sig_box
        self._custom_page = custom_page
        self._stamp_template = stamp_template
        self._tsa_url = str(tsa_url or "").strip()
        self._enable_pdfa = bool(enable_pdfa)
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
    refresh_requested = Signal()
    export_clicked = Signal()        # Xuất hồ sơ nén ra thư mục ngoài
    import_kho_clicked = Signal()    # Chuyển vào Kho lưu trữ nội bộ

    def __init__(self, session=None, parent=None):
        super().__init__(parent)
        # `session` is the per-run ArchiveSession owned by ArchiveContainer.
        # Step 3 reads it to route signed PDFs into <temp>/_step3_signed/.
        # Allowed to be None for unit tests / standalone preview — in that
        # case _signed_dir() falls back to the OS temp dir.
        self._session = session
        self._certs: list[dict] = []
        self._items: list[_SignItem] = []
        self._templates: dict[str, dict] = {}
        self._current_template_name = _DEFAULT_TEMPLATE_NAME
        self._worker: Optional[_SignWorker] = None
        self._loading_template = False

        self.setStyleSheet(f"background: {COLOR_BG}; color: {COLOR_TEXT};")
        self._setup_ui()
        self._load_templates()
        self._load_settings()
        self._set_deps_state()
        if _DEPS_OK:
            self._reload_certs()

    # ------------------------------------------------------------------ UI

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._build_toolbar(root)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        split.setStyleSheet(f"QSplitter::handle {{ background: {COLOR_BORDER}; width: 3px; }}")

        left = QWidget()
        left.setMinimumWidth(0)
        left_l = QVBoxLayout(left)
        left_l.setContentsMargins(8, 8, 6, 8)
        left_l.setSpacing(8)
        self._build_cert_section(left_l)
        self._build_tsa_section(left_l)
        self._build_metadata_section(left_l)
        left_l.addStretch()

        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setFrameShape(QFrame.Shape.NoFrame)
        left_scroll.setMinimumWidth(260)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        left_scroll.setWidget(left)
        left_scroll.setStyleSheet(f"""
            QScrollArea {{ background: {COLOR_BG}; border: none; }}
            QScrollBar:vertical {{ background: transparent; width: 8px; }}
            QScrollBar::handle:vertical {{
                background: {COLOR_BORDER_DEFAULT}; border-radius: 4px; min-height: 24px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
        """)

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

        self._btn_refresh = self._button("Nạp PDF từ Bước 2", "ghost")
        self._btn_refresh.clicked.connect(self.refresh_requested.emit)
        h.addWidget(self._btn_refresh)

        self._btn_open_output = self._button("Mở thư mục đã ký", "ghost")
        self._btn_open_output.clicked.connect(self._open_output_dir)
        h.addWidget(self._btn_open_output)

        self._btn_edit_dossier = self._button("Sửa thông tin hồ sơ", "ghost")
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

    def _build_cert_section(self, parent: QVBoxLayout):
        frame, layout = self._section("Chứng thư số")
        self.combo_cert = _ComboBox()
        self.combo_cert.setFixedHeight(_H)
        self._style_combo(self.combo_cert)
        self.combo_cert.setMinimumContentsLength(18)
        self.combo_cert.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.combo_cert.currentIndexChanged.connect(self._on_cert_change)
        layout.addWidget(self.combo_cert)

        row = QHBoxLayout()
        row.setSpacing(4)
        self._btn_reload_certs = self._button("Tải lại chứng thư", "ghost")
        self._btn_reload_certs.clicked.connect(self._reload_certs)
        row.addWidget(self._btn_reload_certs)
        row.addStretch()
        layout.addLayout(row)

        self.lbl_cert_detail = QLabel("")
        self.lbl_cert_detail.setWordWrap(True)
        self.lbl_cert_detail.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; font-size: 10px; font-family: {FONT_MONO_FALLBACK};"
        )
        layout.addWidget(self.lbl_cert_detail)
        parent.addWidget(frame)

    def _add_position_controls(self, layout: QVBoxLayout):
        lbl = QLabel("Vị trí chữ ký")
        lbl.setStyleSheet(
            f"font-size: {_FONT_SM}px; font-weight: 600; color: {COLOR_TEXT_SECONDARY}; "
            f"font-family: {FONT_UI}; text-transform: uppercase;"
        )
        layout.addWidget(lbl)

        self.spin_page = _NoWheelSpinBox()
        self.spin_page.setRange(1, 9999)
        self.spin_page.setValue(1)
        self.spin_page.setFixedHeight(_H)
        self.spin_page.setMinimumWidth(82)
        self.spin_page.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.spin_page.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.spin_page.valueChanged.connect(self._refresh_page_column)

        defaults = SIG_BOX_DEFAULT if _DEPS_OK else (0.0, 0.0, 220.0, 60.0)
        self.spin_x = self._coord_spin(defaults[0])
        self.spin_y = self._coord_spin(defaults[1])
        self.spin_w = self._coord_spin(defaults[2])
        self.spin_h = self._coord_spin(defaults[3])

        pos_grid = QGridLayout()
        pos_grid.setHorizontalSpacing(8)
        pos_grid.setVerticalSpacing(6)
        for row, (label, widget) in enumerate((
            ("Trang:", self.spin_page),
            ("X:", self.spin_x),
            ("Y:", self.spin_y),
            ("Rộng:", self.spin_w),
            ("Cao:", self.spin_h),
        )):
            pos_grid.addWidget(self._form_label(label), row, 0)
            pos_grid.addWidget(widget, row, 1)
        pos_grid.setColumnStretch(1, 1)
        layout.addLayout(pos_grid)

        btn_fit = self._button("Tự khớp rộng/cao", "ghost")
        btn_fit.setToolTip(
            "Giữ nguyên Trang, X và Y. Chỉ tính lại Rộng/Cao đủ chứa "
            "nội dung mẫu theo chứng thư đang chọn, cỡ chữ chuẩn 8pt."
        )
        btn_fit.clicked.connect(self._auto_fit_box)
        layout.addWidget(btn_fit, alignment=Qt.AlignmentFlag.AlignLeft)

        hint = QLabel("X/Y là góc trên trái. Tự khớp chỉ đổi Rộng/Cao.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: {_FONT_SM}px;")
        layout.addWidget(hint)

        # PDF/A-2b conversion option (chuẩn lưu trữ dài hạn).
        # Convert TRƯỚC khi ký số: signature trong PDF/A-2 vẫn valid.
        # Cần Ghostscript đã cài (auto-detect).
        from scanindex.core.pdf.pdfa_converter import is_available as _pdfa_available
        self.chk_pdfa = QCheckBox("Convert PDF/A-2b trước khi ký")
        self.chk_pdfa.setStyleSheet(
            f"QCheckBox {{ color: {COLOR_TEXT}; font-size: {_FONT_SM}px; }}"
        )
        if not _pdfa_available():
            self.chk_pdfa.setEnabled(False)
            self.chk_pdfa.setToolTip(
                "Cần cài Ghostscript (gswin64c) để dùng tính năng này."
            )
        else:
            self.chk_pdfa.setToolTip(
                "Convert PDF sang PDF/A-2b (chuẩn ISO 19005-2) trước khi ký số. "
                "Giữ nguyên ảnh JPEG gốc (PassThroughJPEGImages)."
            )
        self.chk_pdfa.stateChanged.connect(lambda *_: self._save_settings())
        layout.addWidget(self.chk_pdfa)

    def _build_tsa_section(self, parent: QVBoxLayout):
        frame, layout = self._section("Dịch vụ cấp dấu thời gian")
        self.chk_tsa = QCheckBox("Sử dụng TSA cấp dấu thời gian")
        self.chk_tsa.setChecked(True)
        self.chk_tsa.setStyleSheet(
            f"QCheckBox {{ color: {COLOR_TEXT}; font-size: {_FONT_SM}px; }}"
        )
        self.chk_tsa.stateChanged.connect(self._on_tsa_toggle)
        layout.addWidget(self.chk_tsa)

        layout.addWidget(self._label("Máy chủ TSA:"))
        self.edit_tsa_url = QLineEdit()
        self.edit_tsa_url.setFixedHeight(_H)
        self.edit_tsa_url.setText(DEFAULT_TSA_URL)
        self.edit_tsa_url.setPlaceholderText(DEFAULT_TSA_URL)
        self.edit_tsa_url.setToolTip("Địa chỉ máy chủ cấp dấu thời gian RFC 3161.")
        self.edit_tsa_url.editingFinished.connect(self._save_settings)
        self._style_line_edit(self.edit_tsa_url)
        layout.addWidget(self.edit_tsa_url)
        self._sync_tsa_enabled()
        parent.addWidget(frame)

    def _on_tsa_toggle(self, *_):
        self._sync_tsa_enabled()
        self._save_settings()

    def _sync_tsa_enabled(self, inputs_enabled: Optional[bool] = None):
        if not (hasattr(self, "chk_tsa") and hasattr(self, "edit_tsa_url")):
            return
        if inputs_enabled is None:
            inputs_enabled = self.chk_tsa.isEnabled()
        self.edit_tsa_url.setEnabled(bool(inputs_enabled and self.chk_tsa.isChecked()))

    def _build_metadata_section(self, parent: QVBoxLayout):
        frame, layout = self._section("Nội dung chữ ký")
        layout.addWidget(self._label("Mẫu:"))
        tpl_row = QHBoxLayout()
        tpl_row.setSpacing(4)
        self.combo_template = _ComboBox()
        self.combo_template.setFixedHeight(_H)
        self._style_combo(self.combo_template)
        self.combo_template.currentIndexChanged.connect(self._on_template_changed)
        tpl_row.addWidget(self.combo_template, 1)
        self._btn_template_save = self._button("Lưu", "ghost")
        self._btn_template_save.clicked.connect(self._save_current_template)
        tpl_row.addWidget(self._btn_template_save)
        layout.addLayout(tpl_row)

        tpl_btns = QGridLayout()
        tpl_btns.setHorizontalSpacing(4)
        tpl_btns.setVerticalSpacing(4)
        self._btn_template_new = self._button("Mẫu mới", "ghost")
        self._btn_template_new.clicked.connect(self._new_template)
        tpl_btns.addWidget(self._btn_template_new, 0, 0)
        self._btn_template_delete = self._button("Xóa mẫu", "ghost")
        self._btn_template_delete.clicked.connect(self._delete_template)
        tpl_btns.addWidget(self._btn_template_delete, 0, 1)
        self._btn_template_default = self._button("Mặc định", "ghost")
        self._btn_template_default.clicked.connect(self._select_default_template)
        tpl_btns.addWidget(self._btn_template_default, 1, 0, 1, 2)
        tpl_btns.setColumnStretch(0, 1)
        tpl_btns.setColumnStretch(1, 1)
        layout.addLayout(tpl_btns)

        layout.addWidget(self._label("Nội dung hiển thị:"))
        self.text_template = QTextEdit()
        self.text_template.setFixedHeight(76)
        self.text_template.setAcceptRichText(False)
        self.text_template.setPlainText(DEFAULT_STAMP_TEMPLATE)
        layout.addWidget(self.text_template)

        fields = ", ".join("{" + f + "}" for f in _VISIBLE_TEMPLATE_FIELDS)
        lbl_fields = QLabel("Trường: " + fields)
        lbl_fields.setWordWrap(True)
        lbl_fields.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; font-size: 10px; font-family: {FONT_MONO_FALLBACK};"
        )
        layout.addWidget(lbl_fields)

        self._add_position_controls(layout)
        parent.addWidget(frame)

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
        self.table.setHorizontalHeaderLabels(["File", "Trang", "Trạng thái", "File đã ký"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
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

    def _section(self, title: str) -> tuple[QFrame, QVBoxLayout]:
        frame = QFrame()
        frame.setStyleSheet(
            f"QFrame {{ background: {COLOR_SURFACE}; border: 1px solid {COLOR_BORDER}; "
            f"border-radius: {_RAD}px; }}"
            f"QLabel {{ border: none; }}"
        )
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(8, 6, 8, 8)
        layout.setSpacing(5)
        lbl = QLabel(title)
        lbl.setStyleSheet(
            f"font-size: {_FONT_SM}px; font-weight: 600; color: {COLOR_TEXT_SECONDARY}; "
            f"font-family: {FONT_UI}; text-transform: uppercase;"
        )
        layout.addWidget(lbl)
        return frame, layout

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

    def _label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(f"color: {COLOR_TEXT_SECONDARY}; font-size: {_FONT_SM}px;")
        return lbl

    def _form_label(self, text: str) -> QLabel:
        lbl = self._label(text)
        lbl.setFixedWidth(54)
        return lbl

    def _style_line_edit(self, edit: QLineEdit):
        edit.setStyleSheet(f"""
            QLineEdit {{
                background: {COLOR_INPUT};
                color: {COLOR_TEXT};
                border: 1px solid {COLOR_BORDER_DEFAULT};
                border-radius: {_RAD}px;
                padding: 0 8px;
                font-size: {_FONT_SM}px;
            }}
            QLineEdit:focus {{
                border-color: {COLOR_ACCENT};
            }}
            QLineEdit:disabled {{
                background: {COLOR_BG};
                color: {COLOR_TEXT_MUTED};
                border-color: {COLOR_BORDER};
            }}
        """)

    def _style_combo(self, combo: QComboBox):
        # Local QSS isolates the widget from the global theme's
        # ::drop-down/::down-arrow rules, so re-apply them via the shared
        # COMBOBOX_DROPDOWN_QSS so the chevron stays visible.
        combo.setStyleSheet(f"""
            QComboBox {{
                background: {COLOR_INPUT};
                color: {COLOR_TEXT};
                border: 1px solid {COLOR_BORDER_DEFAULT};
                border-radius: {_RAD}px;
                padding: 0 30px 0 8px;
                font-size: {_FONT_SM}px;
            }}
            QComboBox:focus {{
                border-color: {COLOR_ACCENT};
            }}
            QComboBox QAbstractItemView {{
                background: {COLOR_ELEVATED};
                color: {COLOR_TEXT};
                selection-background-color: {COLOR_ACCENT};
                selection-color: white;
                border: 1px solid {COLOR_BORDER_DEFAULT};
                outline: none;
            }}
        """ + COMBOBOX_DROPDOWN_QSS)

    def _coord_spin(self, value: float) -> QDoubleSpinBox:
        w = _NoWheelDoubleSpinBox()
        w.setRange(0, 5000)
        w.setDecimals(1)
        w.setSingleStep(5)
        w.setValue(float(value))
        w.setFixedHeight(_H)
        w.setMinimumWidth(82)
        w.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        w.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        return w

    # ------------------------------------------------------------- templates

    def _template_path(self) -> str:
        return os.path.abspath(_TEMPLATE_FILE)

    def _default_position(self) -> dict:
        defaults = SIG_BOX_DEFAULT if _DEPS_OK else (0.0, 0.0, 220.0, 60.0)
        return {
            "page": 1,
            "x": float(defaults[0]),
            "y": float(defaults[1]),
            "width": float(defaults[2]),
            "height": float(defaults[3]),
        }

    def _current_position(self) -> dict:
        return {
            "page": int(self.spin_page.value()),
            "x": float(self.spin_x.value()),
            "y": float(self.spin_y.value()),
            "width": float(self.spin_w.value()),
            "height": float(self.spin_h.value()),
        }

    def _coerce_position(self, value) -> dict:
        pos = self._default_position()
        if isinstance(value, dict):
            for key in ("page", "x", "y", "width", "height"):
                if key in value:
                    try:
                        pos[key] = int(value[key]) if key == "page" else float(value[key])
                    except Exception:
                        pass
        return pos

    def _normalise_template_profile(self, value) -> dict:
        if isinstance(value, dict):
            text = str(value.get("text") or value.get("template") or "").strip()
            position = self._coerce_position(value.get("position") or value)
        else:
            text = str(value or "").strip()
            position = self._default_position()
        return {"text": text, "position": position}

    def _template_text(self, name: str) -> str:
        profile = self._templates.get(name) or {}
        return str(profile.get("text") or "")

    def _apply_position(self, position: dict):
        pos = self._coerce_position(position)
        self.spin_page.setValue(max(1, int(pos["page"])))
        self.spin_x.setValue(float(pos["x"]))
        self.spin_y.setValue(float(pos["y"]))
        self.spin_w.setValue(float(pos["width"]))
        self.spin_h.setValue(float(pos["height"]))

    def _store_template_from_ui(self, name: Optional[str] = None) -> None:
        name = (name or self._current_template_name or _DEFAULT_TEMPLATE_NAME).strip()
        text = self.text_template.toPlainText().strip()
        if not name or not text:
            return
        self._templates[name] = {
            "text": text,
            "position": self._current_position(),
        }

    def _apply_template_profile(self, name: str) -> None:
        profile = self._templates.get(name)
        if not profile:
            return
        self.text_template.setPlainText(str(profile.get("text") or ""))
        self._apply_position(profile.get("position") or self._default_position())
        self._current_template_name = name

    def _load_templates(self):
        templates = {
            _DEFAULT_TEMPLATE_NAME: {
                "text": DEFAULT_STAMP_TEMPLATE,
                "position": self._default_position(),
            }
        }
        path = self._template_path()
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    for name, profile_data in data.items():
                        name = str(name).strip()
                        profile = self._normalise_template_profile(profile_data)
                        if name and profile["text"]:
                            templates[name] = profile
            except Exception as exc:
                self.log_message.emit(f"Archive Step 3: cannot load signature templates: {exc}")

        self._templates = templates
        self._loading_template = True
        self.combo_template.clear()
        names = [_DEFAULT_TEMPLATE_NAME] + sorted(
            n for n in templates.keys() if n != _DEFAULT_TEMPLATE_NAME
        )
        self.combo_template.addItems(names)
        self.combo_template.setCurrentText(_DEFAULT_TEMPLATE_NAME)
        self._apply_template_profile(_DEFAULT_TEMPLATE_NAME)
        self._loading_template = False

    def _save_templates(self):
        path = self._template_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = {}
        for name, profile in self._templates.items():
            text = str((profile or {}).get("text") or "").strip()
            if not text:
                continue
            data[name] = {
                "text": text,
                "position": self._coerce_position((profile or {}).get("position")),
            }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _settings_path(self) -> str:
        return os.path.abspath(_SETTINGS_FILE)

    def _load_settings(self):
        path = self._settings_path()
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return
            template_name = str(data.get("template_name") or "").strip()
            selected = template_name if template_name in self._templates else _DEFAULT_TEMPLATE_NAME
            if all(key in data for key in ("page", "x", "y", "width", "height")):
                profile = self._templates.get(selected)
                if profile:
                    profile["position"] = self._coerce_position(data)
            self.combo_template.setCurrentText(selected)
            self._apply_template_profile(selected)
            # PDF/A toggle (chỉ load khi UI checkbox enabled — Ghostscript có sẵn)
            if hasattr(self, "chk_pdfa") and self.chk_pdfa.isEnabled():
                self.chk_pdfa.setChecked(bool(data.get("convert_pdfa", False)))
            if hasattr(self, "chk_tsa"):
                self.chk_tsa.blockSignals(True)
                self.chk_tsa.setChecked(bool(data.get("tsa_enabled", True)))
                self.chk_tsa.blockSignals(False)
            if hasattr(self, "edit_tsa_url"):
                tsa_url = data.get("tsa_url", DEFAULT_TSA_URL)
                self.edit_tsa_url.setText(str(tsa_url or "").strip())
            self._sync_tsa_enabled()
        except Exception as exc:
            self.log_message.emit(f"Archive Step 3: cannot load signature settings: {exc}")

    def _save_settings(self):
        if hasattr(self, "combo_template") and not self._loading_template:
            self._store_template_from_ui()
            self._save_templates()
        path = self._settings_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = {
            "page": int(self.spin_page.value()),
            "x": float(self.spin_x.value()),
            "y": float(self.spin_y.value()),
            "width": float(self.spin_w.value()),
            "height": float(self.spin_h.value()),
            "template_name": self.combo_template.currentText().strip(),
            "convert_pdfa": bool(self.chk_pdfa.isChecked()) if hasattr(self, "chk_pdfa") else False,
            "tsa_enabled": bool(self.chk_tsa.isChecked()) if hasattr(self, "chk_tsa") else True,
            "tsa_url": self.edit_tsa_url.text().strip() if hasattr(self, "edit_tsa_url") else DEFAULT_TSA_URL,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _on_template_changed(self):
        if self._loading_template:
            return
        previous = self._current_template_name
        if previous in self._templates:
            self._store_template_from_ui(previous)
        name = self.combo_template.currentText().strip()
        if name in self._templates:
            self._apply_template_profile(name)
        self._save_templates()

    def _select_default_template(self):
        self.combo_template.setCurrentText(_DEFAULT_TEMPLATE_NAME)
        if self.combo_template.currentText().strip() == _DEFAULT_TEMPLATE_NAME:
            self._apply_template_profile(_DEFAULT_TEMPLATE_NAME)

    def _new_template(self):
        text = self.text_template.toPlainText().strip()
        if not text:
            QMessageBox.warning(self, "Mẫu chữ ký", "Nội dung mẫu không được để trống.")
            return
        name, ok = QInputDialog.getText(self, "Mẫu chữ ký", "Tên mẫu:")
        if not ok:
            return
        name = name.strip()
        if not name:
            return
        if name == _DEFAULT_TEMPLATE_NAME:
            QMessageBox.warning(self, "Mẫu chữ ký", "Tên này đang dùng cho mẫu mặc định.")
            return
        if name in self._templates:
            confirm = QMessageBox.question(
                self, "Mẫu chữ ký", f"Mẫu '{name}' đã tồn tại. Ghi đè?"
            )
            if confirm != QMessageBox.StandardButton.Yes:
                return
        self._templates[name] = {"text": text, "position": self._current_position()}
        self._save_templates()
        self._reload_template_combo(name)

    def _save_current_template(self):
        name = self.combo_template.currentText().strip()
        text = self.text_template.toPlainText().strip()
        if not text:
            QMessageBox.warning(self, "Mẫu chữ ký", "Nội dung mẫu không được để trống.")
            return
        self._templates[name] = {"text": text, "position": self._current_position()}
        self._current_template_name = name
        self._save_templates()
        self.lbl_status.setText(f"Đã lưu mẫu '{name}'.")

    def _delete_template(self):
        name = self.combo_template.currentText().strip()
        if name == _DEFAULT_TEMPLATE_NAME:
            self._select_default_template()
            return
        confirm = QMessageBox.question(
            self, "Mẫu chữ ký", f"Xóa mẫu '{name}'?"
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self._templates.pop(name, None)
        self._save_templates()
        self._reload_template_combo(_DEFAULT_TEMPLATE_NAME)

    def _reload_template_combo(self, selected: str):
        self._loading_template = True
        self.combo_template.clear()
        names = [_DEFAULT_TEMPLATE_NAME] + sorted(
            n for n in self._templates.keys() if n != _DEFAULT_TEMPLATE_NAME
        )
        self.combo_template.addItems(names)
        selected = selected if selected in self._templates else _DEFAULT_TEMPLATE_NAME
        self.combo_template.setCurrentText(selected)
        self._apply_template_profile(selected)
        self._loading_template = False

    # ------------------------------------------------------------- documents

    def _display_name_for_source(self, row_index: int, source_path: str) -> str:
        identity = getattr(self._session, "identity", None)
        if identity is not None:
            try:
                if identity.is_complete():
                    return _safe_pdf_name(identity.make_segment_name(row_index + 1))
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
            if old_output:
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
            items.append(_SignItem(
                source_path=os.path.abspath(source),
                display_name=self._display_name_for_source(row, source),
                signature_page=sig_page,
            ))
        self._items = items
        # `default_output_dir` is no longer threaded through — signed PDFs
        # always land in `<session_temp>/_step3_signed/` per the new
        # workflow contract.
        self._refresh_table()
        if items:
            self.lbl_status.setText(f"Đã nạp {len(items)} file từ Bước 2.")
        else:
            self.lbl_status.setText("Chưa có file đầu ra từ Bước 2 để ký.")

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
        return str(self.spin_page.value())

    def _refresh_page_column(self):
        for row, item in enumerate(self._items):
            self._set_table_item(row, 1, self._page_label(item))

    # ------------------------------------------------------------- certs

    def _set_deps_state(self):
        if _DEPS_OK:
            return
        self.combo_cert.addItem("Thiếu thư viện ký số")
        self.lbl_cert_detail.setText(_IMPORT_ERR)
        self.btn_sign.setEnabled(False)
        self.lbl_status.setText(
            "Không tải được module ký số. Cài dependencies trong requirements_qt.txt rồi khởi động lại."
        )

    def _reload_certs(self):
        if not _DEPS_OK:
            return
        try:
            free_cert_contexts(self._certs)
            self._certs = list_certificates("MY")
            self.combo_cert.blockSignals(True)
            self.combo_cert.clear()
            if self._certs:
                for c in self._certs:
                    text = self._cert_display(c)
                    self.combo_cert.addItem(text)
                    row = self.combo_cert.count() - 1
                    self.combo_cert.setItemData(
                        row, c.get("display", text), Qt.ItemDataRole.ToolTipRole
                    )
                self.combo_cert.setCurrentIndex(0)
            else:
                self.combo_cert.addItem("Không tìm thấy chứng thư có khóa bí mật")
            self.combo_cert.blockSignals(False)
            self._on_cert_change()
        except Exception as exc:
            QMessageBox.critical(self, "Lỗi chứng thư", f"Không đọc được Windows cert store:\n{exc}")

    def _cert_display(self, cert: dict) -> str:
        name = str(cert.get("cn") or cert.get("org") or cert.get("display") or "Certificate").strip()
        if len(name) > 28:
            name = name[:12] + "..." + name[-10:]
        not_after = cert.get("not_after") or ""
        if hasattr(not_after, "strftime"):
            return f"{name} ({not_after.strftime('%Y-%m-%d')})"
        return name

    def _on_cert_change(self):
        idx = self.combo_cert.currentIndex()
        if 0 <= idx < len(self._certs):
            c = self._certs[idx]
            self.lbl_cert_detail.setText(
                f"CN:     {c.get('cn', '')}\n"
                f"Org:    {c.get('org', '')}\n"
                f"OU:     {c.get('ou', '')}\n"
                f"Issuer: {str(c.get('issuer', ''))[:70]}"
            )
        else:
            self.lbl_cert_detail.setText("")

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

    def _auto_fit_box(self):
        idx = self.combo_cert.currentIndex()
        if not (_DEPS_OK and 0 <= idx < len(self._certs)):
            QMessageBox.information(self, "Tự khớp rộng/cao", "Hãy chọn chứng thư trước.")
            return
        try:
            w, h = compute_stamp_natural_size(
                self._certs[idx],
                font_size=8,
                stamp_template=self.text_template.toPlainText().strip(),
                reason=None,
                location=None,
            )
            self.spin_w.setValue(float(w))
            min_h = self._minimum_visible_stamp_height(
                self._certs[idx],
                self.text_template.toPlainText().strip(),
            )
            self.spin_h.setValue(float(max(h, min_h)))
            self._save_settings()
        except KeyError as exc:
            QMessageBox.warning(self, "Mẫu chữ ký", f"Trường không hỗ trợ: {{{exc.args[0]}}}")
        except Exception as exc:
            QMessageBox.warning(self, "Tự khớp rộng/cao", str(exc))

    def _minimum_visible_stamp_height(self, cert: dict, stamp_template: str) -> float:
        """Keep multi-line bitmap appearances from silently dropping lines."""
        try:
            _, height = compute_stamp_natural_size(
                cert,
                font_size=8,
                stamp_template=stamp_template,
                reason=None,
                location=None,
            )
            return float(height)
        except Exception:
            return 20.0

    def _start_signing(self):
        if not _DEPS_OK:
            QMessageBox.critical(self, "Thiếu thư viện", _IMPORT_ERR)
            return
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self.btn_sign.setEnabled(False)
            self.lbl_status.setText("Đang dừng sau file hiện tại...")
            return
        idx = self.combo_cert.currentIndex()
        if not (0 <= idx < len(self._certs)):
            QMessageBox.warning(self, "Lỗi", "Hãy chọn chứng thư số.")
            return
        items = [i for i in self._items if os.path.exists(i.source_path)]
        if not items:
            QMessageBox.warning(self, "Lỗi", "Chưa có file PDF hợp lệ để ký.")
            return
        out_dir = self._signed_dir()
        os.makedirs(out_dir, exist_ok=True)
        stamp_template = self.text_template.toPlainText().strip()
        use_tsa = bool(self.chk_tsa.isChecked()) if hasattr(self, "chk_tsa") else True
        tsa_url = self.edit_tsa_url.text().strip() if use_tsa and hasattr(self, "edit_tsa_url") else ""
        if not stamp_template:
            QMessageBox.warning(self, "Mẫu chữ ký", "Mẫu hiển thị không được để trống.")
            return
        if use_tsa and not tsa_url:
            QMessageBox.warning(self, "Máy chủ TSA", "Nhập địa chỉ máy chủ TSA hoặc tắt chức năng cấp dấu thời gian.")
            return
        if use_tsa and not tsa_url.lower().startswith(("http://", "https://")):
            QMessageBox.warning(self, "Máy chủ TSA", "Địa chỉ TSA phải bắt đầu bằng http:// hoặc https://.")
            return
        cert = self._certs[idx]
        try:
            render_stamp_template(
                cert,
                stamp_template,
                reason=None,
                location=None,
                ts="0000-00-00T00:00:00+0700",
                date="0000-00-00",
                time="00:00:00",
            )
        except KeyError as exc:
            QMessageBox.warning(self, "Mẫu chữ ký", f"Trường không hỗ trợ: {{{exc.args[0]}}}")
            return

        min_h = self._minimum_visible_stamp_height(cert, stamp_template)
        if float(self.spin_h.value()) < min_h:
            self.spin_h.setValue(min_h)

        for item in self._items:
            item.status = "Chờ ký"
            item.output_path = ""
            item.error = ""
        self._refresh_table()

        sig_box = (
            float(self.spin_x.value()),
            float(self.spin_y.value()),
            float(self.spin_w.value()),
            float(self.spin_h.value()),
        )
        self._save_settings()
        self.btn_sign.setText("Dừng")
        self.btn_sign.setEnabled(True)
        self._set_inputs_enabled(False)
        self.lbl_status.setText(f"Đang ký 0/{len(items)} file...")

        self._worker = _SignWorker(
            items=items,
            output_dir=out_dir,
            cert_info=cert,
            sig_box=sig_box,
            custom_page=max(0, self.spin_page.value() - 1),
            stamp_template=stamp_template,
            tsa_url=tsa_url,
            enable_pdfa=bool(self.chk_pdfa.isChecked()) if hasattr(self, "chk_pdfa") else False,
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
            self._btn_refresh, self._btn_export, self._btn_import_kho,
            self._btn_open_output, self._btn_edit_dossier, self._btn_reload_certs,
            self._btn_add_files, self._btn_add_folder, self._btn_remove,
            self._btn_clear, self.combo_cert, self.combo_template,
            self._btn_template_save, self._btn_template_new,
            self._btn_template_delete, self._btn_template_default,
            self.spin_page, self.spin_x, self.spin_y, self.spin_w, self.spin_h,
            self.text_template, self.chk_tsa, self.edit_tsa_url,
        ]:
            widget.setEnabled(enabled)
        self._sync_tsa_enabled(enabled)

    # ------------------------------------------------------------- lifecycle

    def update_texts(self):
        # Step 3 currently keeps explicit Vietnamese labels because certificate
        # stores and signing errors come from the Windows/token driver.
        pass

    def cleanup(self):
        try:
            self._save_settings()
        except Exception:
            pass
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(1500)
            if self._worker.isRunning():
                return
        if _DEPS_OK:
            free_cert_contexts(self._certs)
        self._certs = []
