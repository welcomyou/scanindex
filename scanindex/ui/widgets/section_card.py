"""
SectionCard — Elevated frame used in Settings tab for grouping options.
"""
from PySide6.QtWidgets import QFrame, QVBoxLayout, QLabel

from scanindex.ui.theme import COLOR_TEXT_SECONDARY, COLOR_BORDER, FONT_UI, SP


class SectionCard(QFrame):
    """Elevated card container with section title header."""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setProperty("cssClass", "section-card")

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(SP[3], SP[2], SP[3], SP[3])
        self._layout.setSpacing(SP[1])

        self._title_label = QLabel(title.upper())
        self._title_label.setStyleSheet(
            f"font-family: '{FONT_UI}'; font-size: 10px; font-weight: 700; "
            f"color: {COLOR_TEXT_SECONDARY}; letter-spacing: 0.5px;"
            f"background: transparent; border: none;"
            f"padding-bottom: 2px; border-bottom: 1px solid {COLOR_BORDER};"
        )
        self._layout.addWidget(self._title_label)

    def content_layout(self) -> QVBoxLayout:
        return self._layout

    def set_title(self, title: str):
        self._title_label.setText(title)
