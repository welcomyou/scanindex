"""
LogPanel — Activity log display with colored text per level.
"""
import time
from PySide6.QtWidgets import QTextEdit, QWidget, QVBoxLayout, QHBoxLayout, QLabel
from PySide6.QtGui import QTextCharFormat, QColor, QFont, QTextCursor
from PySide6.QtCore import Qt, Slot

from scanindex.ui.theme import (
    COLOR_PANEL, COLOR_TEXT, COLOR_TEXT_SECONDARY, COLOR_GREEN, COLOR_RED,
    COLOR_INFO, COLOR_TEXT_MUTED, COLOR_BORDER, FONT_UI, FONT_MONO, SP,
    LOG_INFO, LOG_ERROR, LOG_DEBUG, LOG_SUCCESS
)
from scanindex.infra import translations


class LogPanel(QWidget):
    """Right-side activity log panel with colored messages."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._verbose = True
        self._setup_ui()
        self._setup_formats()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header
        header = QHBoxLayout()
        header.setContentsMargins(SP[2], SP[1], SP[2], SP[1])

        lbl_title = QLabel(translations.get_text("lbl_activity_log"))
        lbl_title.setStyleSheet(
            f"font-family: '{FONT_UI}'; font-size: 11px; font-weight: 600; "
            f"color: {COLOR_TEXT_SECONDARY}; background: transparent;"
            f"text-transform: uppercase; letter-spacing: 0.5px;"
        )
        header.addWidget(lbl_title)
        header.addStretch()

        self.status_dot = QLabel("\u25cf")  # filled circle
        self.status_dot.setStyleSheet(
            f"font-size: 8px; color: {COLOR_GREEN}; background: transparent;"
        )
        header.addWidget(self.status_dot)

        layout.addLayout(header)

        # Log text area
        self.text_edit = QTextEdit()
        self.text_edit.setObjectName("logPanel")
        self.text_edit.setReadOnly(True)
        self.text_edit.setFont(QFont(FONT_MONO, 10))
        layout.addWidget(self.text_edit)

    def _setup_formats(self):
        """Create QTextCharFormat for each log level."""
        self._formats = {}

        fmt_info = QTextCharFormat()
        fmt_info.setForeground(QColor(COLOR_INFO))
        self._formats[LOG_INFO] = fmt_info

        fmt_error = QTextCharFormat()
        fmt_error.setForeground(QColor(COLOR_RED))
        self._formats[LOG_ERROR] = fmt_error

        fmt_debug = QTextCharFormat()
        fmt_debug.setForeground(QColor(COLOR_TEXT_MUTED))
        self._formats[LOG_DEBUG] = fmt_debug

        fmt_success = QTextCharFormat()
        fmt_success.setForeground(QColor(COLOR_GREEN))
        self._formats[LOG_SUCCESS] = fmt_success

    def set_verbose(self, enabled: bool):
        self._verbose = enabled

    @Slot(str, str)
    def append_log(self, msg: str, level: str = LOG_INFO):
        """Append a timestamped log message. Thread-safe when called via signal."""
        if level == LOG_DEBUG and not self._verbose:
            return

        ts = time.strftime("[%H:%M:%S] ")
        fmt = self._formats.get(level, self._formats[LOG_INFO])

        cursor = self.text_edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(ts + msg + "\n", fmt)
        self.text_edit.setTextCursor(cursor)
        self.text_edit.ensureCursorVisible()
