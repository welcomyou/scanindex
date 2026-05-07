"""
MetadataDialog — Displays extracted document metadata in a form layout.
"""
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QLineEdit, QTextEdit,
    QPushButton, QApplication, QLabel
)
from PySide6.QtGui import QFont
from PySide6.QtCore import Qt

from scanindex.ui.theme import (
    COLOR_BG, COLOR_SURFACE, COLOR_TEXT, COLOR_TEXT_SECONDARY,
    COLOR_ACCENT, COLOR_BORDER, FONT_UI, FONT_MONO, SP, RADIUS_MD
)
from scanindex.infra import translations


# Field definitions: (key, translation_key, multiline)
_FIELDS = [
    ("doc_type", "lbl_doc_type", False),
    ("co_quan_ban_hanh", "lbl_co_quan", False),
    ("ngay_ban_hanh", "lbl_ngay", False),
    ("so_van_ban", "arc_field_so", False),
    ("ky_hieu", "arc_field_ky_hieu", False),
    ("loai_van_ban", "lbl_loai_vb", False),
    ("trich_yeu", "lbl_trich_yeu", True),
    ("nguoi_ky", "lbl_nguoi_ky", False),
]

_DOC_TYPE_DISPLAY = {
    "dang": "Văn bản Đảng",
    "nhanuoc": "Văn bản Nhà nước",
    "unknown": "Không xác định",
}

_FIELD_STYLE = (
    f"background: {COLOR_SURFACE}; color: {COLOR_TEXT}; "
    f"border: 1px solid {COLOR_BORDER}; border-radius: {RADIUS_MD}px; "
    f"padding: 6px 8px;"
)

_LABEL_STYLE = (
    f"font-family: '{FONT_UI}'; font-size: 12px; color: {COLOR_TEXT_SECONDARY}; "
    f"background: transparent;"
)


class MetadataDialog(QDialog):
    """Read-only dialog showing extracted document metadata."""

    def __init__(self, metadata: dict, filename: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle(
            translations.get_text("tooltip_metadata") + (f" — {filename}" if filename else ""))
        self.resize(520, 420)
        self.setStyleSheet(f"QDialog {{ background: {COLOR_BG}; }}")

        self._metadata = metadata or {}
        self._setup_ui()

    def _setup_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SP[4], SP[4], SP[4], SP[4])
        outer.setSpacing(SP[3])

        form = QFormLayout()
        form.setSpacing(SP[2])
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)

        self._widgets = {}
        for key, trans_key, multiline in _FIELDS:
            label = QLabel(translations.get_text(trans_key) + ":")
            label.setStyleSheet(_LABEL_STYLE)

            value = self._metadata.get(key) or ""
            if key == "doc_type":
                value = _DOC_TYPE_DISPLAY.get(value, value)

            if multiline:
                widget = QTextEdit()
                widget.setReadOnly(True)
                widget.setPlainText(str(value))
                widget.setFont(QFont(FONT_UI, 12))
                widget.setFixedHeight(80)
                widget.setStyleSheet(_FIELD_STYLE)
            else:
                widget = QLineEdit(str(value))
                widget.setReadOnly(True)
                widget.setFont(QFont(FONT_UI, 12))
                widget.setStyleSheet(_FIELD_STYLE)

            form.addRow(label, widget)
            self._widgets[key] = widget

        outer.addLayout(form)
        outer.addStretch()

        # Copy All button
        btn_copy = QPushButton(translations.get_text("btn_copy_metadata"))
        btn_copy.setFixedHeight(26)
        btn_copy.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_copy.setStyleSheet(
            f"QPushButton {{ background: {COLOR_ACCENT}; color: white; "
            f"font-family: '{FONT_UI}'; font-size: 12px; border: none; "
            f"border-radius: {RADIUS_MD}px; padding: 0 12px; }}"
            f"QPushButton:hover {{ background: {COLOR_ACCENT}; opacity: 0.9; }}"
        )
        btn_copy.clicked.connect(self._copy_all)
        outer.addWidget(btn_copy, alignment=Qt.AlignmentFlag.AlignRight)

    def _copy_all(self):
        """Copy all metadata fields to clipboard as formatted text."""
        parts = []
        for key, trans_key, _ in _FIELDS:
            label = translations.get_text(trans_key)
            value = self._metadata.get(key) or ""
            if key == "doc_type":
                value = _DOC_TYPE_DISPLAY.get(value, value)
            if value:
                parts.append(f"{label}: {value}")
        text = "\n".join(parts)
        clipboard = QApplication.clipboard()
        if clipboard:
            clipboard.setText(text)
