"""
TextPreviewDialog — Simple read-only text viewer with Ctrl+Wheel zoom.
Replaces open_text_window() from ocr_app.py.
"""
from PySide6.QtWidgets import QDialog, QVBoxLayout, QTextEdit
from PySide6.QtGui import QFont, QWheelEvent
from PySide6.QtCore import Qt

from scanindex.ui.theme import COLOR_BG, COLOR_SURFACE, COLOR_TEXT, FONT_MONO, SP, RADIUS_MD


class TextPreviewDialog(QDialog):
    """Read-only text preview with zoom support."""

    def __init__(self, title: str, content: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(650, 550)
        self.setStyleSheet(f"QDialog {{ background: {COLOR_BG}; }}")

        self._font_size = 12

        layout = QVBoxLayout(self)
        layout.setContentsMargins(SP[3], SP[3], SP[3], SP[3])

        self.text_edit = QTextEdit()
        self.text_edit.setReadOnly(True)
        self.text_edit.setFont(QFont(FONT_MONO, self._font_size))
        self.text_edit.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.text_edit.setStyleSheet(
            f"background: {COLOR_SURFACE}; color: {COLOR_TEXT}; "
            f"border: none; border-radius: {RADIUS_MD}px;"
        )
        self.text_edit.setPlainText(content)
        layout.addWidget(self.text_edit)

        # Install wheel event filter for zoom
        self.text_edit.viewport().installEventFilter(self)

    def eventFilter(self, obj, event):
        if obj is self.text_edit.viewport() and isinstance(event, QWheelEvent):
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                delta = 1 if event.angleDelta().y() > 0 else -1
                self._update_font_size(delta)
                return True
        return super().eventFilter(obj, event)

    def _update_font_size(self, delta: int):
        new_size = max(8, min(72, self._font_size + delta))
        if new_size != self._font_size:
            self._font_size = new_size
            self.text_edit.setFont(QFont(FONT_MONO, self._font_size))
