"""Shared signature-configuration panel.

Hosts the controls for certificate selection, timestamp (TSA) settings and
signature appearance (template, stamp image, position). It is the single
source of truth for these settings, used by both the archive Step 3 screen
(``ArchiveStep3Sign``) and the standalone "Ký số hàng loạt" tool so the two
stay visually and behaviourally identical.

Configuration is persisted to the shared files under ``config/``:
``sign_templates-*.json``, ``sign_settings-*.json`` and the
``sign_stamp_images/`` folder.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import ssl
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

from PySide6.QtCore import QPoint, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPen, QPixmap, QPolygon
from PySide6.QtWidgets import (
    QAbstractSpinBox, QApplication, QButtonGroup, QCheckBox, QComboBox,
    QDoubleSpinBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QInputDialog,
    QLabel, QLineEdit, QMessageBox, QPushButton, QRadioButton, QSpinBox,
    QSizePolicy, QTextEdit, QVBoxLayout, QWidget,
)

from scanindex.ui.theme import (
    COLOR_ACCENT, COLOR_BORDER, COLOR_BORDER_DEFAULT, COLOR_ELEVATED,
    COLOR_INPUT, COLOR_SURFACE, COLOR_TEXT, COLOR_TEXT_MUTED,
    COLOR_TEXT_SECONDARY, COMBOBOX_DROPDOWN_QSS, FONT_MONO_FALLBACK, FONT_UI,
)
from scanindex.infra import translations

try:
    from scanindex.infra.paths import get_base_dir
except Exception:
    def get_base_dir():
        return os.getcwd()

try:
    from scanindex.core.pdf.win_cert_store import free_cert_contexts, list_certificates
    from scanindex.core.pdf.signer import (
        DEFAULT_STAMP_TEMPLATE, DEFAULT_TSA_URL, SIG_BOX_DEFAULT,
        STAMP_TEMPLATE_FIELDS, STAMP_TEXT_BELOW, STAMP_TEXT_RIGHT,
        compute_stamp_natural_size, render_stamp_template,
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

# Versioned config files (see scanindex.infra.data_versioning). Resolved at
# import time, after run_startup_migration has run — so these point at the
# migrated versioned name if any legacy file existed.
from scanindex.infra.data_versioning import get_active_config_path
_TEMPLATE_FILE = get_active_config_path("config/sign_templates", ".json")
_SETTINGS_FILE = get_active_config_path("config/sign_settings", ".json")
_CONFIG_DIR = os.path.join(get_base_dir(), "config")
_STAMP_IMAGE_DIR = os.path.join(_CONFIG_DIR, "sign_stamp_images")
_TSA_CONNECT_TIMEOUT_SECONDS = 5.0
_VISIBLE_TEMPLATE_FIELDS = tuple(
    f for f in STAMP_TEMPLATE_FIELDS if f not in {"reason", "location"}
)

_H = 26
_FONT_SM = 11
_RAD = 4
_DEFAULT_TEMPLATE_NAME = "Mặc định"


# --------------------------------------------------------------------------- #
# Small reusable UI primitives. Defined here (rather than in signing_step) so
# that this module has no dependency back on signing_step, avoiding a circular
# import: signing_step imports SigningConfigPanel, not the other way around.
# --------------------------------------------------------------------------- #

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


class _ClearIconButton(QPushButton):
    """Small centred X icon button, drawn instead of relying on font metrics."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(24, 24)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Xóa hình dấu")
        self.setStyleSheet("QPushButton { background: transparent; border: none; }")

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        enabled = self.isEnabled()
        hover = enabled and self.underMouse()

        rect = QRectF(self.rect()).adjusted(2.0, 2.0, -2.0, -2.0)
        if hover:
            painter.setBrush(QBrush(QColor("#dc2626")))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(rect)
            icon = QColor("#ffffff")
        else:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            border = QColor(COLOR_BORDER_DEFAULT if enabled else COLOR_BORDER)
            painter.setPen(QPen(border, 1.0))
            painter.drawEllipse(rect)
            icon = QColor("#dc2626" if enabled else COLOR_TEXT_MUTED)

        painter.setPen(QPen(icon, 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        cx = rect.center().x()
        cy = rect.center().y()
        r = 4.6
        painter.drawLine(QPoint(int(round(cx - r)), int(round(cy - r))),
                         QPoint(int(round(cx + r)), int(round(cy + r))))
        painter.drawLine(QPoint(int(round(cx + r)), int(round(cy - r))),
                         QPoint(int(round(cx - r)), int(round(cy + r))))


@dataclass(frozen=True)
class BatchTimeDecision:
    tsa_url: str = ""
    mode: str = "local_config"


# --------------------------------------------------------------------------- #
# The panel itself
# --------------------------------------------------------------------------- #

class SigningConfigPanel(QWidget):
    """Reusable panel of signature settings (cert / TSA / appearance).

    Callers (the archive Step 3 screen and the bulk-signing tool) read the
    current configuration through the ``get_*`` accessors and run their own
    ``_SignWorker``. The panel owns the certificate list, the templates and
    the persisted settings — it does NOT run the signing itself.
    """

    log_message = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._certs: list[dict] = []
        self._templates: dict[str, dict] = {}
        self._current_template_name = _DEFAULT_TEMPLATE_NAME
        self._loading_template = False
        self._stamp_image_path = ""
        # Dirty tracking + loading guard. The panel is shared between the
        # archive Step 3 screen and the standalone bulk-signing tool, and both
        # read/write the SAME config files. To honour "load cấu hình dùng sau
        # cùng" (load the most-recent config) across the two screens within one
        # app session:
        #   - ``_dirty`` is set True only by genuine user edits; ``save_settings``
        #     then writes to disk and clears it. A panel that was merely loaded
        #     (never edited) does NOT write back, so it can't clobber a newer
        #     config saved by the other screen.
        #   - on ``showEvent`` the panel reloads from disk when it is not dirty,
        #     so switching screens reflects the other screen's changes.
        self._dirty = False
        self._loading = True  # True until the first load finishes

        self._build_ui()
        self._loading = True
        self._load_templates()
        self._load_settings()
        self._loading = False
        self._dirty = False
        self._set_deps_state()
        if _DEPS_OK:
            self._reload_certs()
        # Reflect the initial selection (disable Xóa when Mặc định is active).
        self._update_delete_button_state()

    # --------------------------------------------------------------- UI build

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        self._build_cert_section(root)
        self._build_tsa_section(root)
        self._build_metadata_section(root)
        root.addStretch()

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
        self.edit_tsa_url.editingFinished.connect(self._user_edit)
        self._style_line_edit(self.edit_tsa_url)
        layout.addWidget(self.edit_tsa_url)
        self._sync_tsa_enabled()
        parent.addWidget(frame)

    def _build_metadata_section(self, parent: QVBoxLayout):
        frame, layout = self._section("Nội dung chữ ký")

        # Row 1: "Mẫu:" label + [+ Thêm] + [✎ Đổi tên] + [🗑 Xóa].
        tpl_head = QHBoxLayout()
        tpl_head.setSpacing(6)
        tpl_head.addWidget(self._label("Mẫu:"))
        tpl_head.addStretch()
        self._btn_template_new = self._button("+ Thêm", "ghost")
        self._btn_template_new.clicked.connect(self._new_template)
        tpl_head.addWidget(self._btn_template_new)
        self._btn_template_rename = self._button("✎ Đổi tên", "ghost")
        self._btn_template_rename.clicked.connect(self._rename_template)
        tpl_head.addWidget(self._btn_template_rename)
        self._btn_template_delete = self._button("🗑 Xóa", "ghost")
        self._btn_template_delete.clicked.connect(self._delete_template)
        tpl_head.addWidget(self._btn_template_delete)
        layout.addLayout(tpl_head)

        # Row 2: combobox (stretch) + "Mặc định".
        tpl_row = QHBoxLayout()
        tpl_row.setSpacing(4)
        self.combo_template = _ComboBox()
        self.combo_template.setFixedHeight(_H)
        self._style_combo(self.combo_template)
        self.combo_template.currentIndexChanged.connect(self._on_template_changed)
        tpl_row.addWidget(self.combo_template, 1)
        self._btn_template_default = self._button("Mặc định", "ghost")
        self._btn_template_default.clicked.connect(self._select_default_template)
        tpl_row.addWidget(self._btn_template_default)
        layout.addLayout(tpl_row)

        stamp_header = QHBoxLayout()
        stamp_header.setSpacing(6)
        stamp_header.addWidget(self._label("Hình dấu:"))
        self._btn_stamp_image_choose = self._button("Chọn ảnh", "ghost")
        self._btn_stamp_image_choose.clicked.connect(self._choose_stamp_image)
        self._style_stamp_choose_button(self._btn_stamp_image_choose)
        stamp_header.addWidget(self._btn_stamp_image_choose)
        stamp_header.addStretch()
        layout.addLayout(stamp_header)

        stamp_row = QHBoxLayout()
        stamp_row.setSpacing(6)
        self.lbl_stamp_image_preview = QLabel("Không có")
        self.lbl_stamp_image_preview.setFixedSize(QSize(96, 52))
        self.lbl_stamp_image_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_stamp_image_preview.setStyleSheet(
            f"background: {COLOR_INPUT}; color: {COLOR_TEXT_MUTED}; "
            f"border: 1px solid {COLOR_BORDER_DEFAULT}; border-radius: {_RAD}px; "
            f"font-size: 10px;"
        )
        stamp_row.addWidget(self.lbl_stamp_image_preview)
        self._btn_stamp_image_clear = _ClearIconButton()
        self._btn_stamp_image_clear.clicked.connect(self._clear_stamp_image)
        stamp_row.addWidget(self._btn_stamp_image_clear, alignment=Qt.AlignmentFlag.AlignTop)
        stamp_row.addStretch()
        layout.addLayout(stamp_row)

        content_row = QHBoxLayout()
        content_row.setSpacing(8)
        content_row.addWidget(self._label("Nội dung hiển thị:"))
        content_row.addStretch()
        self._stamp_text_group = QButtonGroup(self)
        self._stamp_text_group.setExclusive(True)
        self.radio_text_below = QRadioButton("Dưới dấu")
        self.radio_text_right = QRadioButton("Bên phải dấu")
        for radio in (self.radio_text_below, self.radio_text_right):
            self._style_stamp_radio(radio)
            self._stamp_text_group.addButton(radio)
            content_row.addWidget(radio)
        self.radio_text_below.setChecked(True)
        self.radio_text_below.toggled.connect(lambda *_: self._user_edit())
        self.radio_text_right.toggled.connect(lambda *_: self._user_edit())
        layout.addLayout(content_row)

        self.text_template = QTextEdit()
        self.text_template.setFixedHeight(76)
        self.text_template.setAcceptRichText(False)
        self.text_template.setPlainText(DEFAULT_STAMP_TEMPLATE)
        # Auto-save the current template when the text changes, debounced so
        # we don't write to disk on every keystroke.
        self._text_save_timer = QTimer(self)
        self._text_save_timer.setSingleShot(True)
        self._text_save_timer.setInterval(500)
        self._text_save_timer.timeout.connect(self._auto_save_current_template)
        self.text_template.textChanged.connect(self._text_save_timer.start)
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

        defaults = SIG_BOX_DEFAULT if _DEPS_OK else (0.0, 0.0, 220.0, 60.0)
        self.spin_x = self._coord_spin(defaults[0])
        self.spin_y = self._coord_spin(defaults[1])
        self.spin_w = self._coord_spin(defaults[2])
        self.spin_h = self._coord_spin(defaults[3])
        # Auto-save the current template when a position field is finished
        # editing (editingFinished fires on focus loss / Enter, not on every
        # arrow press, so it won't thrash the disk).
        for sp in (self.spin_page, self.spin_x, self.spin_y,
                   self.spin_w, self.spin_h):
            sp.editingFinished.connect(self._auto_save_current_template)

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

        self._btn_fit = self._button("Tự khớp rộng/cao", "ghost")
        self._btn_fit.setToolTip(
            "Giữ nguyên Trang, X và Y. Chỉ tính lại Rộng/Cao đủ chứa "
            "nội dung mẫu theo chứng thư đang chọn, cỡ chữ chuẩn 8pt."
        )
        self._btn_fit.clicked.connect(self.auto_fit_box)
        layout.addWidget(self._btn_fit, alignment=Qt.AlignmentFlag.AlignLeft)

        hint = QLabel("X/Y là góc trên trái. Tự khớp chỉ đổi Rộng/Cao.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: {_FONT_SM}px;")
        layout.addWidget(hint)

        self.chk_avoid_text_overlap = QCheckBox("Tự tránh vùng có chữ khi đặt chữ ký")
        self.chk_avoid_text_overlap.setChecked(True)
        self.chk_avoid_text_overlap.setStyleSheet(
            f"QCheckBox {{ color: {COLOR_TEXT}; font-size: {_FONT_SM}px; }}"
        )
        self.chk_avoid_text_overlap.setToolTip(
            "Trước khi ký, phần mềm đọc text/OCR trên trang rồi thu nhỏ khung "
            "chữ ký theo khoảng trống từ vị trí X/Y, có chừa khoảng hở "
            "với text PDF. Nếu trang không có text layer hoặc không tìm được vùng "
            "trống phù hợp thì dùng vị trí hiện tại."
        )
        self.chk_avoid_text_overlap.stateChanged.connect(lambda *_: self._user_edit())
        layout.addWidget(self.chk_avoid_text_overlap)

        # PDF/A-2b conversion option (chuẩn lưu trữ dài hạn).
        # Convert TRƯỚC khi ký số: signature trong PDF/A-2 vẫn valid.
        from scanindex.core.pdf.pdfa_converter import is_available as _pdfa_available
        self.chk_pdfa = QCheckBox("Convert PDF/A-2b trước khi ký")
        self.chk_pdfa.setStyleSheet(
            f"QCheckBox {{ color: {COLOR_TEXT}; font-size: {_FONT_SM}px; }}"
        )
        if not _pdfa_available():
            self.chk_pdfa.setEnabled(False)
            self.chk_pdfa.setToolTip("Cài lại pikepdf để dùng tính năng này.")
        else:
            self.chk_pdfa.setToolTip(
                "Convert PDF sang PDF/A-2b (chuẩn ISO 19005-2) trước khi ký số. "
                "Giữ nguyên text layer + font gốc, tránh lỗi mojibake."
            )
        self.chk_pdfa.stateChanged.connect(lambda *_: self._user_edit())
        layout.addWidget(self.chk_pdfa)

    # ---------------------------------------------------------- styling helpers

    def _section(self, title: str) -> tuple[QFrame, QVBoxLayout]:
        frame = QFrame()
        frame.setStyleSheet(
            f"QFrame {{ background: {COLOR_SURFACE};"
            f" border: 1px solid {COLOR_BORDER}; border-radius: {_RAD}px; }}"
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
            bg, hover, border, color, weight = "#16a34a", "#15803d", "none", "#fff", "600"
        elif role == "danger":
            bg, hover, border, color, weight = "#dc2626", "#b91c1c", "none", "#fff", "600"
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

    def _style_stamp_choose_button(self, button: QPushButton):
        button.setStyleSheet(f"""
            QPushButton {{
                background: transparent; border: 1px solid {COLOR_BORDER_DEFAULT};
                border-radius: {_RAD}px; color: {COLOR_TEXT}; font-size: {_FONT_SM}px;
                font-family: {FONT_UI}; padding: 0 10px;
            }}
            QPushButton:hover {{
                background: {COLOR_ELEVATED}; border-color: {COLOR_ACCENT};
            }}
            QPushButton:disabled {{
                color: {COLOR_TEXT_MUTED}; background: transparent;
                border-color: {COLOR_BORDER};
            }}
        """)

    def _style_stamp_radio(self, radio: QRadioButton):
        radio.setStyleSheet(f"""
            QRadioButton {{
                background: transparent; color: {COLOR_TEXT}; font-size: {_FONT_SM}px;
                font-family: {FONT_UI}; padding: 4px 8px; border-radius: {_RAD}px;
                spacing: 6px;
            }}
            QRadioButton:checked {{
                background: {COLOR_ELEVATED}; color: {COLOR_TEXT};
            }}
            QRadioButton::indicator {{
                width: 14px; height: 14px; border-radius: 7px;
                border: 1px solid {COLOR_TEXT_SECONDARY}; background: transparent;
            }}
            QRadioButton::indicator:checked {{
                border: 1px solid {COLOR_ACCENT};
                background: qradialgradient(
                    cx: 0.5, cy: 0.5, radius: 0.58,
                    fx: 0.5, fy: 0.5,
                    stop: 0 {COLOR_ACCENT},
                    stop: 0.42 {COLOR_ACCENT},
                    stop: 0.45 transparent,
                    stop: 1 transparent
                );
            }}
            QRadioButton::indicator:unchecked {{
                background: transparent;
            }}
            QRadioButton:disabled {{
                color: {COLOR_TEXT_MUTED};
            }}
            QRadioButton::indicator:disabled {{
                border-color: {COLOR_TEXT_MUTED}; background: transparent;
            }}
        """)

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
                background: transparent;
                color: {COLOR_TEXT_MUTED};
                border-color: {COLOR_BORDER};
            }}
        """)

    def _style_combo(self, combo: QComboBox):
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

    # ------------------------------------------------------------- stamp image

    def _stamp_image_dir(self) -> str:
        return os.path.abspath(_STAMP_IMAGE_DIR)

    def _resolve_stamp_image_path(self, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        path = text if os.path.isabs(text) else os.path.join(get_base_dir(), text)
        return os.path.abspath(os.path.normpath(path))

    def _stored_stamp_image_value(self, path: str) -> str:
        if not path:
            return ""
        abs_path = os.path.abspath(os.path.normpath(path))
        base = os.path.abspath(get_base_dir())
        try:
            if os.path.commonpath([base, abs_path]) == base:
                return os.path.relpath(abs_path, base).replace("\\", "/")
        except Exception:
            pass
        return abs_path

    def _copy_stamp_image_to_store(self, source_path: str) -> str:
        source_path = os.path.abspath(os.path.normpath(source_path))
        pix = QPixmap(source_path)
        if pix.isNull():
            raise RuntimeError("File ảnh không hợp lệ.")
        store_dir = self._stamp_image_dir()
        os.makedirs(store_dir, exist_ok=True)
        try:
            if os.path.commonpath([store_dir, source_path]) == store_dir:
                return source_path
        except ValueError:
            pass
        ext = os.path.splitext(source_path)[1].lower()
        if ext not in {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}:
            ext = ".png"
        dst = os.path.join(store_dir, f"{uuid.uuid4().hex}{ext}")
        if ext == os.path.splitext(source_path)[1].lower():
            shutil.copy2(source_path, dst)
        elif not pix.save(dst, "PNG"):
            raise RuntimeError("Không lưu được hình dấu.")
        return os.path.abspath(dst)

    def get_stamp_image_path(self) -> str:
        return str(getattr(self, "_stamp_image_path", "") or "").strip()

    def get_stamp_text_position(self) -> str:
        if hasattr(self, "radio_text_right") and self.radio_text_right.isChecked():
            return STAMP_TEXT_RIGHT
        return STAMP_TEXT_BELOW

    def _set_stamp_text_position(self, value: str) -> None:
        position = STAMP_TEXT_RIGHT if str(value or "").strip() == STAMP_TEXT_RIGHT else STAMP_TEXT_BELOW
        if hasattr(self, "radio_text_right") and hasattr(self, "radio_text_below"):
            self.radio_text_right.blockSignals(True)
            self.radio_text_below.blockSignals(True)
            self.radio_text_right.setChecked(position == STAMP_TEXT_RIGHT)
            self.radio_text_below.setChecked(position != STAMP_TEXT_RIGHT)
            self.radio_text_right.blockSignals(False)
            self.radio_text_below.blockSignals(False)

    def _set_stamp_image_path(self, path: str) -> None:
        self._stamp_image_path = self._resolve_stamp_image_path(path) if path else ""
        self._update_stamp_image_preview()

    def _update_stamp_image_preview(self) -> None:
        if not hasattr(self, "lbl_stamp_image_preview"):
            return
        path = self.get_stamp_image_path()
        pix = QPixmap(path) if path and os.path.exists(path) else QPixmap()
        if pix.isNull():
            self.lbl_stamp_image_preview.setPixmap(QPixmap())
            self.lbl_stamp_image_preview.setText("Mất ảnh" if path else "Không có")
            self.lbl_stamp_image_preview.setToolTip(path)
            if hasattr(self, "_btn_stamp_image_clear"):
                self._btn_stamp_image_clear.setEnabled(bool(path))
            return
        self.lbl_stamp_image_preview.setText("")
        self.lbl_stamp_image_preview.setToolTip(path)
        preview_size = QSize(
            max(1, self.lbl_stamp_image_preview.width() - 8),
            max(1, self.lbl_stamp_image_preview.height() - 8),
        )
        self.lbl_stamp_image_preview.setPixmap(
            pix.scaled(
                preview_size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        if hasattr(self, "_btn_stamp_image_clear"):
            self._btn_stamp_image_clear.setEnabled(True)

    def _choose_stamp_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            translations.localize_text("Chọn hình dấu"),
            "",
            translations.localize_text(
                "Ảnh (*.png *.jpg *.jpeg *.bmp *.webp *.tif *.tiff)"
            ),
        )
        if not path:
            return
        try:
            stored = self._copy_stamp_image_to_store(path)
        except Exception as exc:
            QMessageBox.warning(self, "Hình dấu", str(exc))
            return
        self._set_stamp_image_path(stored)
        self._user_edit()

    def _clear_stamp_image(self):
        self._set_stamp_image_path("")
        self._user_edit()

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
            image_path = self._resolve_stamp_image_path(
                value.get("stamp_image_path") or value.get("image_path") or ""
            )
            stamp_text_position = (
                STAMP_TEXT_RIGHT
                if str(value.get("stamp_text_position") or "").strip() == STAMP_TEXT_RIGHT
                else STAMP_TEXT_BELOW
            )
        else:
            text = str(value or "").strip()
            position = self._default_position()
            image_path = ""
            stamp_text_position = STAMP_TEXT_BELOW
        return {
            "text": text,
            "position": position,
            "stamp_image_path": image_path,
            "stamp_text_position": stamp_text_position,
        }

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
            "stamp_image_path": self.get_stamp_image_path(),
            "stamp_text_position": self.get_stamp_text_position(),
        }

    def _auto_save_current_template(self) -> None:
        """Auto-save the currently selected template (text/position/stamp)
        whenever the user edits one of its fields. No-op during loads."""
        if self._loading or self._loading_template:
            return
        name = self._current_template_name
        if not name or name not in self._templates:
            return
        text = self.text_template.toPlainText().strip()
        if not text:
            return  # don't persist an empty template
        self._store_template_from_ui(name)
        self._save_templates()
        self._mark_dirty()

    def _apply_template_profile(self, name: str) -> None:
        profile = self._templates.get(name)
        if not profile:
            return
        self.text_template.setPlainText(str(profile.get("text") or ""))
        self._apply_position(profile.get("position") or self._default_position())
        self._set_stamp_image_path(str(profile.get("stamp_image_path") or ""))
        self._set_stamp_text_position(str(profile.get("stamp_text_position") or STAMP_TEXT_BELOW))
        self._current_template_name = name

    def _load_templates(self):
        templates = {
            _DEFAULT_TEMPLATE_NAME: {
                "text": DEFAULT_STAMP_TEMPLATE,
                "position": self._default_position(),
                "stamp_image_path": "",
                "stamp_text_position": STAMP_TEXT_BELOW,
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
                self.log_message.emit(f"Signing: cannot load signature templates: {exc}")

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
                "stamp_text_position": (
                    STAMP_TEXT_RIGHT
                    if str((profile or {}).get("stamp_text_position") or "").strip() == STAMP_TEXT_RIGHT
                    else STAMP_TEXT_BELOW
                ),
            }
            image_path = self._resolve_stamp_image_path(
                str((profile or {}).get("stamp_image_path") or "")
            )
            if image_path:
                data[name]["stamp_image_path"] = self._stored_stamp_image_value(image_path)
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
        self._update_delete_button_state()
        # Persist the selected template name + position into the settings file
        # too (user explicitly switched templates).
        self._user_edit()

    def _update_delete_button_state(self) -> None:
        """Disable 'Xóa'/'Đổi tên' when the default template is selected."""
        name = self.combo_template.currentText().strip() if hasattr(self, "combo_template") else ""
        editable = name != _DEFAULT_TEMPLATE_NAME and bool(name)
        if hasattr(self, "_btn_template_delete"):
            self._btn_template_delete.setEnabled(editable)
        if hasattr(self, "_btn_template_rename"):
            self._btn_template_rename.setEnabled(editable)

    def _select_default_template(self):
        self.combo_template.setCurrentText(_DEFAULT_TEMPLATE_NAME)
        if self.combo_template.currentText().strip() == _DEFAULT_TEMPLATE_NAME:
            self._apply_template_profile(_DEFAULT_TEMPLATE_NAME)

    def _new_template(self):
        name, ok = QInputDialog.getText(self, "Mẫu chữ ký mới", "Tên mẫu:")
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
        # A NEW template starts from defaults — NOT from whatever the current
        # template happens to show — so the operator gets a clean slate to
        # build on. We reset the UI controls first, then capture them.
        self._loading_template = True
        try:
            self.text_template.setPlainText(DEFAULT_STAMP_TEMPLATE)
            self._apply_position(self._default_position())
            self._set_stamp_image_path("")
            self._set_stamp_text_position(STAMP_TEXT_BELOW)
        finally:
            self._loading_template = False
        self._templates[name] = {
            "text": DEFAULT_STAMP_TEMPLATE,
            "position": self._default_position(),
            "stamp_image_path": "",
            "stamp_text_position": STAMP_TEXT_BELOW,
        }
        self._current_template_name = name
        self._save_templates()
        self._reload_template_combo(name)
        # Persist selection + ensure this panel is marked as the latest writer
        # so the config survives restart / sync to the other screen.
        self._user_edit()

    def _rename_template(self):
        """Rename the currently selected (non-default) template."""
        old = self.combo_template.currentText().strip()
        if not old or old == _DEFAULT_TEMPLATE_NAME:
            return
        new_name, ok = QInputDialog.getText(
            self, "Đổi tên mẫu", "Tên mới:", text=old
        )
        if not ok:
            return
        new_name = new_name.strip()
        if not new_name or new_name == old:
            return
        if new_name == _DEFAULT_TEMPLATE_NAME:
            QMessageBox.warning(self, "Mẫu chữ ký", "Tên này đang dùng cho mẫu mặc định.")
            return
        if new_name in self._templates:
            confirm = QMessageBox.question(
                self, "Mẫu chữ ký", f"Mẫu '{new_name}' đã tồn tại. Ghi đè?"
            )
            if confirm != QMessageBox.StandardButton.Yes:
                return
        # Preserve the profile, just change the key.
        self._templates[new_name] = self._templates.pop(old)
        self._current_template_name = new_name
        self._save_templates()
        self._reload_template_combo(new_name)
        self._user_edit()

    def _delete_template(self):
        name = self.combo_template.currentText().strip()
        if not name or name == _DEFAULT_TEMPLATE_NAME:
            # Never delete the default; just select it.
            self._select_default_template()
            return
        # Confirm with the template name emphasised (bold) so the operator is
        # certain which template they are removing.
        confirm = QMessageBox(self)
        confirm.setIcon(QMessageBox.Icon.Question)
        confirm.setWindowTitle("Xóa mẫu chữ ký")
        confirm.setTextFormat(Qt.TextFormat.RichText)
        confirm.setText(
            f"Bạn có chắc muốn xóa mẫu <b>“{name}”</b>?<br>"
            "Hành động này không thể hoàn tác."
        )
        confirm.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        confirm.setDefaultButton(QMessageBox.StandardButton.No)
        if confirm.exec() != QMessageBox.StandardButton.Yes:
            return
        self._templates.pop(name, None)
        self._save_templates()
        self._reload_template_combo(_DEFAULT_TEMPLATE_NAME)
        self._user_edit()

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
        self._update_delete_button_state()

    # ------------------------------------------------------------- settings

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
            if hasattr(self, "chk_pdfa") and self.chk_pdfa.isEnabled():
                self.chk_pdfa.setChecked(bool(data.get("convert_pdfa", False)))
            if hasattr(self, "chk_avoid_text_overlap"):
                self.chk_avoid_text_overlap.blockSignals(True)
                self.chk_avoid_text_overlap.setChecked(
                    bool(data.get("avoid_text_overlap", True))
                )
                self.chk_avoid_text_overlap.blockSignals(False)
            if hasattr(self, "chk_tsa"):
                self.chk_tsa.blockSignals(True)
                self.chk_tsa.setChecked(bool(data.get("tsa_enabled", True)))
                self.chk_tsa.blockSignals(False)
            if hasattr(self, "edit_tsa_url"):
                tsa_url = data.get("tsa_url", DEFAULT_TSA_URL)
                self.edit_tsa_url.setText(str(tsa_url or "").strip())
            self._sync_tsa_enabled()
        except Exception as exc:
            self.log_message.emit(f"Signing: cannot load signature settings: {exc}")

    def reload_from_disk(self) -> None:
        """Re-read templates + settings from the shared config files.

        Called on ``showEvent`` so a panel reflects changes the other screen
        (Bước 3 ↔ tool Ký số) just saved. Skipped when this panel has unsaved
        user edits (``_dirty``), so an in-progress edit is never clobbered.
        """
        if self._dirty:
            return
        was_loading = self._loading
        self._loading = True
        self._loading_template = True
        try:
            self._load_templates()
            self._load_settings()
        except Exception as exc:
            self.log_message.emit(f"Signing: cannot reload config: {exc}")
        finally:
            self._loading_template = False
            self._loading = was_loading
            self._dirty = False

    def showEvent(self, event):
        # When this panel becomes visible again (e.g. the user switches from
        # Bước 3 to the bulk-signing tool or back), reload the shared config so
        # both screens always show the most recently saved settings.
        try:
            self.reload_from_disk()
        except Exception:
            pass
        super().showEvent(event)

    def _mark_dirty(self) -> None:
        """Flag that the user genuinely changed something in THIS panel."""
        if not self._loading and not self._loading_template:
            self._dirty = True

    def _user_edit(self) -> None:
        """User changed a control: mark dirty then persist immediately."""
        self._mark_dirty()
        self._save_settings()

    def _save_settings(self):
        # Only write back when the user actually edited this panel. The panel
        # is shared between two screens over one config file; a panel that was
        # merely loaded must NOT overwrite a newer config saved by the other
        # screen (otherwise the "load cấu hình dùng sau cùng" rule is broken).
        if not self._dirty and not self._loading:
            return
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
            "avoid_text_overlap": (
                bool(self.chk_avoid_text_overlap.isChecked())
                if hasattr(self, "chk_avoid_text_overlap") else True
            ),
            "tsa_enabled": bool(self.chk_tsa.isChecked()) if hasattr(self, "chk_tsa") else True,
            "tsa_url": self.edit_tsa_url.text().strip() if hasattr(self, "edit_tsa_url") else DEFAULT_TSA_URL,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        self._dirty = False

    # ------------------------------------------------------------- TSA

    def _on_tsa_toggle(self, *_):
        self._sync_tsa_enabled()
        self._user_edit()

    def _sync_tsa_enabled(self, inputs_enabled: Optional[bool] = None):
        if not (hasattr(self, "chk_tsa") and hasattr(self, "edit_tsa_url")):
            return
        if inputs_enabled is None:
            inputs_enabled = self.chk_tsa.isEnabled()
        self.edit_tsa_url.setEnabled(bool(inputs_enabled and self.chk_tsa.isChecked()))

    def _check_tsa_connection(self, tsa_url: str) -> tuple[bool, str]:
        parsed = urlparse(str(tsa_url or "").strip())
        scheme = parsed.scheme.lower()
        host = parsed.hostname
        if scheme not in {"http", "https"} or not host:
            return False, "Địa chỉ TSA không hợp lệ."

        port = parsed.port or (443 if scheme == "https" else 80)
        sock = None
        try:
            sock = socket.create_connection(
                (host, port),
                timeout=_TSA_CONNECT_TIMEOUT_SECONDS,
            )
            if scheme == "https":
                context = ssl.create_default_context()
                with context.wrap_socket(sock, server_hostname=host):
                    pass
                sock = None
            return True, ""
        except Exception as exc:
            return False, f"{host}:{port} - {exc}"
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    def _ask_local_time_fallback(self, detail: str = "") -> str:
        current_time = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Không kết nối được TSA")
        box.setText(
            "Không kết nối được máy chủ cấp dấu thời gian (TSA).\n\n"
            "Bạn có thể tiếp tục ký sử dụng thời gian của máy tính nội bộ. "
            "Thời gian này không phải là dấu thời gian tin cậy từ TSA và phụ thuộc "
            "vào thiết lập ngày giờ trên máy tính.\n\n"
            f"Thời gian máy hiện tại: {current_time}\n\n"
            "Vui lòng điều chỉnh thời gian chính xác, sau đó chọn Đồng ý để ký "
            "sử dụng thời gian nội bộ."
        )
        if detail:
            box.setDetailedText(str(detail))
        retry_btn = box.addButton("Thử lại TSA", QMessageBox.ButtonRole.ActionRole)
        agree_btn = box.addButton("Đồng ý", QMessageBox.ButtonRole.AcceptRole)
        cancel_btn = box.addButton("Hủy", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel_btn)
        box.setEscapeButton(cancel_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked == retry_btn:
            return "retry"
        if clicked == agree_btn:
            return "local"
        return "cancel"

    def resolve_batch_time_decision(self, tsa_url: str) -> Optional[BatchTimeDecision]:
        """Resolve the time source once for the current signing batch.

        Probes the TSA; if unreachable, prompts the user (retry / use local
        time / cancel). Returns None when the user cancels the whole batch.
        If the caller set ``self.status`` (a Qt ``Signal(str)`` or any callable),
        it receives short status strings during the probe.
        """
        status_cb = getattr(self, "status", None)

        def _emit_status(msg: str) -> None:
            if status_cb is None:
                return
            try:
                if hasattr(status_cb, "emit"):
                    status_cb.emit(msg)
                elif callable(status_cb):
                    status_cb(msg)
            except Exception:
                pass

        while True:
            _emit_status("Đang kiểm tra kết nối TSA...")
            QApplication.processEvents()
            ok, detail = self._check_tsa_connection(tsa_url)
            if ok:
                return BatchTimeDecision(tsa_url=tsa_url, mode="tsa")

            action = self._ask_local_time_fallback(detail)
            if action == "retry":
                continue
            if action == "local":
                self.log_message.emit(
                    "Signing: TSA unavailable; user confirmed signing "
                    "with local computer time"
                )
                return BatchTimeDecision(tsa_url="", mode="local_fallback")
            _emit_status("Đã hủy ký số.")
            return None

    # ------------------------------------------------------------- certs

    def _set_deps_state(self):
        if _DEPS_OK:
            return
        translations.add_localized_combo_items(
            self.combo_cert, ["Thiếu thư viện ký số"]
        )
        self.lbl_cert_detail.setText(_IMPORT_ERR)
        if hasattr(self, "_btn_fit"):
            self._btn_fit.setEnabled(False)

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
                translations.add_localized_combo_items(
                    self.combo_cert,
                    ["Không tìm thấy chứng thư có khóa bí mật"],
                )
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
                f"OU:     {c.get('ou', '')}\n"
                f"ORG:    {c.get('org', '')}"
            )
        else:
            self.lbl_cert_detail.setText("")

    # ---------------------------------------------------------- public getters

    def get_cert_info(self) -> dict | None:
        """Selected certificate dict, or None if no usable cert is selected."""
        if not _DEPS_OK:
            return None
        idx = self.combo_cert.currentIndex()
        if 0 <= idx < len(self._certs):
            return self._certs[idx]
        return None

    def get_sig_box(self) -> tuple[float, float, float, float]:
        return (
            float(self.spin_x.value()),
            float(self.spin_y.value()),
            float(self.spin_w.value()),
            float(self.spin_h.value()),
        )

    def get_page_index(self) -> int:
        """0-based signature page index."""
        return max(0, self.spin_page.value() - 1)

    def get_stamp_template(self) -> str:
        return self.text_template.toPlainText().strip()

    def is_avoid_overlap(self) -> bool:
        return (
            bool(self.chk_avoid_text_overlap.isChecked())
            if hasattr(self, "chk_avoid_text_overlap") else True
        )

    def is_pdfa(self) -> bool:
        return bool(self.chk_pdfa.isChecked()) if hasattr(self, "chk_pdfa") else False

    def is_tsa_enabled(self) -> bool:
        return bool(self.chk_tsa.isChecked()) if hasattr(self, "chk_tsa") else True

    def get_tsa_url(self) -> str:
        return self.edit_tsa_url.text().strip() if hasattr(self, "edit_tsa_url") else ""

    def minimum_visible_stamp_height(self, cert: dict, stamp_template: str) -> float:
        """Keep multi-line bitmap appearances from silently dropping lines."""
        if not _DEPS_OK:
            return 20.0
        try:
            _, height = compute_stamp_natural_size(
                cert,
                font_size=8,
                stamp_template=stamp_template,
                reason=None,
                location=None,
                stamp_image_path=self.get_stamp_image_path() or None,
                stamp_text_position=self.get_stamp_text_position(),
            )
            return float(height)
        except Exception:
            return 20.0

    def auto_fit_box(self):
        cert = self.get_cert_info()
        if cert is None:
            QMessageBox.information(self, "Tự khớp rộng/cao", "Hãy chọn chứng thư trước.")
            return
        try:
            w, h = compute_stamp_natural_size(
                cert,
                font_size=8,
                stamp_template=self.get_stamp_template(),
                reason=None,
                location=None,
                stamp_image_path=self.get_stamp_image_path() or None,
                stamp_text_position=self.get_stamp_text_position(),
            )
            self.spin_w.setValue(float(w))
            min_h = self.minimum_visible_stamp_height(cert, self.get_stamp_template())
            self.spin_h.setValue(float(max(h, min_h)))
            self._user_edit()
        except KeyError as exc:
            QMessageBox.warning(self, "Mẫu chữ ký", f"Trường không hỗ trợ: {{{exc.args[0]}}}")
        except Exception as exc:
            QMessageBox.warning(self, "Tự khớp rộng/cao", str(exc))

    def validate_stamp_template(self, cert: dict, stamp_template: str) -> None:
        """Raise KeyError if the template references an unknown field."""
        if not _DEPS_OK:
            return
        render_stamp_template(
            cert,
            stamp_template,
            reason=None,
            location=None,
            ts="0000-00-00T00:00:00+0700",
            date="0000-00-00",
            time="00:00:00",
        )

    def set_inputs_enabled(self, enabled: bool):
        for widget in [
            self._btn_reload_certs,
            self.combo_cert, self.combo_template,
            self._btn_template_new,
            self._btn_template_rename,
            self._btn_template_delete, self._btn_template_default,
            self._btn_stamp_image_choose, self._btn_stamp_image_clear,
            self.radio_text_below, self.radio_text_right,
            self.spin_page, self.spin_x, self.spin_y, self.spin_w, self.spin_h,
            self.text_template, self.chk_avoid_text_overlap, self.chk_tsa, self.edit_tsa_url,
            self._btn_fit,
        ]:
            widget.setEnabled(enabled)
        if enabled:
            self._update_delete_button_state()
            self._update_stamp_image_preview()
        self._sync_tsa_enabled(enabled)

    # ------------------------------------------------------------- lifecycle

    def save_settings(self):
        """Force-write current state (called when the user starts signing).

        This bypasses the dirty guard so an explicit programmatic save always
        lands on disk, even right after a load.
        """
        self._dirty = True
        self._save_settings()

    def cleanup(self):
        # Persist only if the user actually edited this panel — otherwise leave
        # the shared config untouched so the other screen's newer config wins.
        try:
            self._save_settings()
        except Exception:
            pass
        if _DEPS_OK:
            free_cert_contexts(self._certs)
        self._certs = []
