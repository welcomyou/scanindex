"""Base building blocks for screens in the QStackedWidget navigation."""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QVBoxLayout, QWidget,
)

from scanindex.ui.theme import (
    COLOR_ACCENT, COLOR_ACCENT_HOVER, COLOR_BG, COLOR_BORDER, COLOR_PANEL,
    COLOR_SURFACE, COLOR_TEXT, COLOR_TEXT_SECONDARY, FONT_UI, RADIUS_MD, SP,
)


class ScreenContent(QWidget):
    """Base class for the inner content widget of a screen.

    Subclasses can override:
      - required_models() -> list of model group keys that must be loaded
        before this screen is usable.
      - is_busy() -> True when a long-running task is in progress; used by
        the parent container to confirm with user before allowing back.
      - request_cancel() -> ask the screen to cancel its current task and
        clean up temp files. Called when user confirms cancel.
    """

    def required_models(self) -> list[str]:
        return []

    def is_busy(self) -> bool:
        return False

    def request_cancel(self) -> None:
        return None


class ScreenContainer(QWidget):
    """Wraps a ScreenContent with header (title + back button). The global log
    panel lives at MainWindow level and is shown alongside the stack."""

    back_requested = Signal()

    def __init__(self, title: str, content: QWidget,
                 busy_check: Callable[[], bool] | None = None,
                 cancel_cb: Callable[[], None] | None = None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.content = content
        self._busy_check = busy_check or (lambda: False)
        self._cancel_cb = cancel_cb or (lambda: None)
        self._build_ui(title)

    def _build_ui(self, title: str):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = self._build_header(title)
        outer.addWidget(header)

        outer.addWidget(self.content, 1)

    def _build_header(self, title: str) -> QWidget:
        bar = QWidget()
        bar.setStyleSheet(
            f"background: {COLOR_SURFACE}; border-bottom: 1px solid {COLOR_BORDER};"
        )
        bar.setFixedHeight(44)
        h = QHBoxLayout(bar)
        h.setContentsMargins(SP[3], SP[1], SP[3], SP[1])
        h.setSpacing(SP[2])

        self.btn_back = QPushButton("←  Quay lại")
        self.btn_back.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_back.setStyleSheet(
            f"QPushButton {{ background: {COLOR_ACCENT}; color: white;"
            f" border: none; padding: 4px 14px; border-radius: {RADIUS_MD}px;"
            f" font: 600 13px '{FONT_UI}'; }}"
            f"QPushButton:hover {{ background: {COLOR_ACCENT_HOVER}; }}"
        )
        self.btn_back.clicked.connect(self._on_back_clicked)
        h.addWidget(self.btn_back)

        lbl = QLabel(title)
        lbl.setStyleSheet(
            f"color: {COLOR_TEXT}; font: 600 16px '{FONT_UI}'; padding-left: {SP[2]}px;"
        )
        h.addWidget(lbl)

        self._title_widget_insert_index = h.count()
        h.addStretch(1)
        # Right-side action area — populated via `add_header_action`.
        self._header_layout = h
        return bar

    def add_header_action(self, text: str, on_click: Callable[[], None],
                          *, danger: bool = False) -> QPushButton:
        """Append a right-aligned button to the header bar. `danger=True`
        applies a red tint for destructive actions (e.g. reset)."""
        btn = QPushButton(text)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        if danger:
            btn.setStyleSheet(
                f"QPushButton {{ background: transparent; color: #f87171;"
                f" border: 1px solid #7f1d1d; padding: 4px 14px;"
                f" border-radius: {RADIUS_MD}px; font: 600 13px '{FONT_UI}'; }}"
                f"QPushButton:hover {{ background: #7f1d1d; color: white; }}"
            )
        else:
            btn.setStyleSheet(
                f"QPushButton {{ background: transparent; color: {COLOR_TEXT_SECONDARY};"
                f" border: 1px solid {COLOR_BORDER}; padding: 4px 14px;"
                f" border-radius: {RADIUS_MD}px; font: 500 13px '{FONT_UI}'; }}"
                f"QPushButton:hover {{ background: {COLOR_PANEL}; color: {COLOR_TEXT}; }}"
            )
        btn.clicked.connect(on_click)
        self._header_layout.addWidget(btn)
        return btn

    def add_header_widget(self, widget: QWidget) -> QWidget:
        """Append a custom widget to the right side of the header bar."""
        self._header_layout.addWidget(widget)
        return widget

    def add_title_widget(self, widget: QWidget) -> QWidget:
        """Place a widget immediately beside the screen title."""
        index = getattr(self, "_title_widget_insert_index", None)
        if index is None:
            return self.add_header_widget(widget)
        self._header_layout.insertWidget(index, widget)
        self._title_widget_insert_index = index + 1
        return widget

    def _on_back_clicked(self):
        if self._busy_check():
            confirm = QMessageBox(self)
            confirm.setWindowTitle("Xác nhận dừng")
            confirm.setIcon(QMessageBox.Icon.Question)
            confirm.setText("Đang có tác vụ chạy. Dừng lại?")
            confirm.setInformativeText(
                "Tác vụ hiện tại sẽ bị hủy và các file tạm sẽ bị xóa."
            )
            confirm.setStandardButtons(
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            confirm.setDefaultButton(QMessageBox.StandardButton.No)
            if confirm.exec() != QMessageBox.StandardButton.Yes:
                return
            self._cancel_cb()
        self.back_requested.emit()
