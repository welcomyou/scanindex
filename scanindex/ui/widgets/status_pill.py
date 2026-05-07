"""
StatusPill — Colored badge label showing file processing status.
"""
from PySide6.QtWidgets import QLabel
from PySide6.QtCore import Qt

from scanindex.ui.theme import (
    COLOR_ELEVATED, COLOR_TEXT, STATUS_COLOR_MAP, STATUS_KEY_MAP,
    FONT_UI, RADIUS_SM
)
from scanindex.infra import translations


class StatusPill(QLabel):
    """Small colored label showing current status text."""

    def __init__(self, status: str = "Pending", parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFixedHeight(18)
        self.setMinimumWidth(52)
        self.update_status(status)

    def update_status(self, status: str):
        """Update the displayed status text and color."""
        self._status = status
        color = STATUS_COLOR_MAP.get(status, COLOR_TEXT)
        display_text = translations.get_text(STATUS_KEY_MAP.get(status, status))
        self.setText(display_text)
        self.setStyleSheet(
            f"QLabel {{"
            f"  color: {color};"
            f"  background: {COLOR_ELEVATED};"
            f"  border: 1px solid {color}40;"
            f"  border-radius: {RADIUS_SM}px;"
            f"  padding: 1px 6px;"
            f"  font-family: '{FONT_UI}';"
            f"  font-size: 10px;"
            f"  font-weight: 600;"
            f"}}"
        )

    def set_animated_text(self, text: str):
        """Set text directly (for spinner animation)."""
        self.setText(text)

    @property
    def status(self):
        return self._status
