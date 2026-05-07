"""
FileItemWidget — Single row in the file list showing filename, status, and action buttons.
"""
import os
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QLabel, QPushButton, QSizePolicy
)
from PySide6.QtGui import QPixmap, QIcon
from PySide6.QtCore import Qt, Signal

from scanindex.ui.theme import (
    COLOR_TEXT, COLOR_TEXT_MUTED, COLOR_INFO, COLOR_WARNING, COLOR_GREEN,
    COLOR_BORDER, FONT_UI, SP, RADIUS_SM, STATUS_COLOR_MAP, STATUS_KEY_MAP,
    COLOR_ELEVATED, COLOR_HOVER, COLOR_BORDER_DEFAULT
)
from scanindex.ui.widgets.status_pill import StatusPill
from scanindex.infra import translations


class FileItemWidget(QWidget):
    """Widget representing one file row in DnD or Batch list."""

    rerun_clicked = Signal(int)
    view_raw_clicked = Signal(int)
    view_corrected_clicked = Signal(int)
    view_metadata_clicked = Signal(int)
    remove_clicked = Signal(int)

    def __init__(self, index: int, item: dict, list_type: str = "dnd",
                 icons: dict = None, show_relative: str = None, parent=None):
        super().__init__(parent)
        self._index = index
        self._item = item
        self._list_type = list_type
        self._icons = icons or {}
        self._show_relative = show_relative  # base dir for relative path display
        self.setFixedHeight(32)

        self._setup_ui()

    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(SP[2], 0, SP[1], 0)
        layout.setSpacing(SP[1])

        # Filename
        f_path = self._item["path"]
        if self._show_relative and f_path.startswith(self._show_relative):
            disp_name = os.path.relpath(f_path, self._show_relative)
        else:
            disp_name = os.path.basename(f_path)

        self.lbl_name = QLabel(disp_name)
        self.lbl_name.setStyleSheet(
            f"font-family: '{FONT_UI}'; font-size: 12px; color: {COLOR_TEXT}; background: transparent;"
        )
        self.lbl_name.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.lbl_name)

        # Status pill
        self.status_pill = StatusPill(self._item.get("status", "Pending"))
        layout.addWidget(self.status_pill)

        # Buttons container
        self.btn_container = QHBoxLayout()
        self.btn_container.setSpacing(2)
        layout.addLayout(self.btn_container)

        self._render_buttons()

    def _make_icon_btn(self, pixmap: QPixmap = None, text: str = "",
                       color: str = COLOR_TEXT_MUTED, tooltip: str = "") -> QPushButton:
        btn = QPushButton()
        btn.setProperty("cssClass", "icon")
        btn.setFixedSize(24, 24)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        if pixmap and not pixmap.isNull():
            btn.setIcon(QIcon(pixmap))
        else:
            btn.setText(text)
            btn.setStyleSheet(
                f"QPushButton {{ color: {color}; background: transparent; border: none; "
                f"font-size: 12px; border-radius: {RADIUS_SM}px; padding: 0; }}"
                f"QPushButton:hover {{ background: {COLOR_HOVER}; }}"
            )
        if tooltip:
            btn.setToolTip(tooltip)
        return btn

    def _render_buttons(self):
        # Clear existing
        while self.btn_container.count():
            item = self.btn_container.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if self._list_type != "dnd":
            return

        status = self._item.get("status", "Pending")

        # Re-run button (Done/Failed/OCR Done/Corrected)
        if status in ["Done", "OCR Done", "Corrected", "Failed"]:
            pm = self._icons.get("refresh", QPixmap())
            btn_rerun = self._make_icon_btn(
                pixmap=pm, text="\u21bb",
                color=COLOR_INFO,
                tooltip=translations.get_text("tooltip_rerun")
            )
            btn_rerun.clicked.connect(lambda: self.rerun_clicked.emit(self._index))
            self.btn_container.addWidget(btn_rerun)

        # View raw (yellow eye) — needs output_path
        output_path = self._item.get("output_path")
        is_pdf_output = (
            output_path
            and str(output_path).lower().endswith(".pdf")
            and os.path.exists(str(output_path))
        )
        if is_pdf_output:
            pm_raw = self._icons.get("eye_yellow", QPixmap())
            btn_raw = self._make_icon_btn(
                pixmap=pm_raw, text="R",
                color=COLOR_WARNING,
                tooltip=translations.get_text("tooltip_view_raw")
            )
            btn_raw.clicked.connect(lambda: self.view_raw_clicked.emit(self._index))
            self.btn_container.addWidget(btn_raw)

            # View corrected (green eye) — only available while corrected text is cached in memory
            has_corrected = bool(self._item.get("corrected_text"))
            if has_corrected:
                pm_corr = self._icons.get("eye_green", QPixmap())
                btn_corr = self._make_icon_btn(
                    pixmap=pm_corr, text="C",
                    color=COLOR_GREEN,
                    tooltip=translations.get_text("tooltip_view_compare")
                )
                btn_corr.clicked.connect(lambda: self.view_corrected_clicked.emit(self._index))
                self.btn_container.addWidget(btn_corr)

        # Metadata button (M) — show when metadata has been extracted
        if self._item.get("metadata"):
            btn_meta = self._make_icon_btn(
                text="M", color=COLOR_INFO,
                tooltip=translations.get_text("tooltip_metadata")
            )
            btn_meta.clicked.connect(lambda: self.view_metadata_clicked.emit(self._index))
            self.btn_container.addWidget(btn_meta)

        # Remove button (always last for DnD)
        btn_remove = self._make_icon_btn(text="x", color=COLOR_TEXT_MUTED)
        btn_remove.clicked.connect(lambda: self.remove_clicked.emit(self._index))
        self.btn_container.addWidget(btn_remove)

    def update_status(self, status: str):
        """Update status pill and re-render buttons if needed."""
        old_status = self._item.get("status")
        self._item["status"] = status
        self.status_pill.update_status(status)
        # Re-render buttons if status category changed
        if old_status != status:
            self._render_buttons()

    def update_item(self, item: dict):
        """Update the backing item data and refresh buttons."""
        self._item = item
        self.status_pill.update_status(item.get("status", "Pending"))
        self._render_buttons()

    def set_spinner_text(self, text: str):
        """Set animated spinner text on the status pill."""
        self.status_pill.set_animated_text(text)

    @property
    def index(self):
        return self._index

    @index.setter
    def index(self, val):
        self._index = val
