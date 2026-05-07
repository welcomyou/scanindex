"""
About Tab — Read-only information display.
"""
from PySide6.QtWidgets import QWidget, QVBoxLayout, QTextEdit
from PySide6.QtGui import QFont
from PySide6.QtCore import Qt

from scanindex.ui.theme import COLOR_TEXT_SECONDARY, FONT_UI, SP
from scanindex.infra import translations


class AboutTab(QWidget):
    """About tab showing application info."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(SP[3], SP[3], SP[3], SP[3])

        self.text_edit = QTextEdit()
        self.text_edit.setReadOnly(True)
        self.text_edit.setFont(QFont(FONT_UI, 12))
        self.text_edit.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; background: transparent; border: none;"
        )
        self.text_edit.setText(translations.get_text("txt_about_content"))
        layout.addWidget(self.text_edit)

    def update_texts(self):
        self.text_edit.setText(translations.get_text("txt_about_content"))
