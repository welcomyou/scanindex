"""Modal overlay shown while model groups are being loaded for a screen.

Sits on top of the QStackedWidget and blocks mouse events to underlying
widgets. Status label updates as each library finishes loading.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame, QLabel, QProgressBar, QVBoxLayout, QWidget,
)

from scanindex.ui.theme import (
    COLOR_ACCENT, COLOR_BORDER_DEFAULT, COLOR_HOVER, COLOR_SURFACE,
    COLOR_TEXT, COLOR_TEXT_MUTED, FONT_UI, RADIUS_LG, SP,
)


class SplashOverlay(QWidget):
    """Semi-transparent overlay with a centered card showing the load status.

    The overlay is a child of `host_widget` and resizes to fill it. Use
    `show_loading(title, initial_status)` to display, `set_status(text)` to
    update, and `hide()` to dismiss.
    """

    cancel_requested = Signal()  # reserved (currently no cancel button)

    def __init__(self, host_widget: QWidget):
        super().__init__(host_widget)
        self._host = host_widget
        # Block events to widgets underneath
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("background: rgba(0, 0, 0, 160);")

        # Center card
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addStretch(1)

        self._card = QFrame()
        self._card.setObjectName("SplashOverlayCard")
        self._card.setFixedSize(420, 150)
        self._card.setStyleSheet(
            f"QFrame#SplashOverlayCard {{"
            f"  background: {COLOR_SURFACE};"
            f"  border: 1px solid {COLOR_BORDER_DEFAULT};"
            f"  border-radius: {RADIUS_LG}px;"
            f"}}"
            f"QFrame#SplashOverlayCard QLabel {{"
            f"  border: none; background: transparent;"
            f"}}"
        )

        card_layout = QVBoxLayout(self._card)
        card_layout.setContentsMargins(SP[6], SP[5], SP[6], SP[5])
        card_layout.setSpacing(SP[2])

        self._title_lbl = QLabel("Đang chuẩn bị...")
        self._title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title_lbl.setFont(QFont(FONT_UI, 14, QFont.Weight.Bold))
        self._title_lbl.setStyleSheet(f"color: {COLOR_TEXT};")
        card_layout.addWidget(self._title_lbl)

        self._status_lbl = QLabel("...")
        self._status_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_lbl.setFont(QFont(FONT_UI, 11))
        self._status_lbl.setStyleSheet(f"color: {COLOR_TEXT_MUTED};")
        self._status_lbl.setWordWrap(True)
        card_layout.addWidget(self._status_lbl)

        self._progress = QProgressBar()
        self._progress.setFixedHeight(3)
        self._progress.setRange(0, 0)  # indeterminate
        self._progress.setTextVisible(False)
        self._progress.setStyleSheet(
            f"QProgressBar {{ background: {COLOR_HOVER}; border: none; border-radius: 2px; }}"
            f"QProgressBar::chunk {{ background: {COLOR_ACCENT}; border-radius: 2px; }}"
        )
        card_layout.addWidget(self._progress)

        # Center the card horizontally
        h_wrap = QVBoxLayout()
        h_wrap.setContentsMargins(0, 0, 0, 0)
        wrap = QWidget()
        wrap_layout = QVBoxLayout(wrap)
        wrap_layout.setContentsMargins(0, 0, 0, 0)
        wrap_layout.addWidget(self._card, 0, Qt.AlignmentFlag.AlignHCenter)
        outer.addWidget(wrap, 0)
        outer.addStretch(1)

        self.hide()

    # ── public API ──────────────────────────────────────────────────────

    def show_loading(self, title: str, initial_status: str = "..."):
        self._title_lbl.setText(title)
        self._status_lbl.setText(initial_status)
        self._resize_to_host()
        self.show()
        self.raise_()

    def set_status(self, text: str):
        self._status_lbl.setText(text)

    # ── geometry ────────────────────────────────────────────────────────

    def _resize_to_host(self):
        if self._host is not None:
            self.setGeometry(0, 0, self._host.width(), self._host.height())

    def showEvent(self, event):
        self._resize_to_host()
        super().showEvent(event)

    def eventFilter(self, watched, event):
        # Resize together with host
        if watched is self._host and event.type() in (
            event.Type.Resize, event.Type.Show
        ):
            self._resize_to_host()
        return super().eventFilter(watched, event)
