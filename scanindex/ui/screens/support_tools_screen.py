"""Container screen for small supporting tools."""
from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from scanindex.ui.screens.accuracy_screen import AccuracyScreen
from scanindex.ui.screens.screen_base import ScreenContent
from scanindex.ui.screens.secret_file_scan_screen import SecretFileScanScreen
from scanindex.ui.theme import (
    COLOR_ACCENT,
    COLOR_ACCENT_HOVER,
    COLOR_BG,
    COLOR_BORDER,
    COLOR_ELEVATED,
    COLOR_SURFACE,
    COLOR_TEXT,
    COLOR_TEXT_SECONDARY,
    FONT_UI,
    RADIUS_LG,
    SP,
)


class _ToolTile(QFrame):
    clicked = Signal()

    def __init__(
        self,
        icon: str,
        title: str,
        subtitle: str,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setObjectName("SupportToolTile")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(118)
        self._hover = False
        self._apply_style()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(SP[4], SP[3], SP[4], SP[3])
        layout.setSpacing(SP[2])

        head = QHBoxLayout()
        head.setSpacing(SP[2])
        icon_lbl = QLabel(icon)
        icon_lbl.setFixedWidth(30)
        icon_lbl.setStyleSheet(
            f"color: {COLOR_ACCENT}; font: 22px '{FONT_UI}';"
            " background: transparent; border: none;"
        )
        icon_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        head.addWidget(icon_lbl)

        title_lbl = QLabel(title)
        title_lbl.setWordWrap(True)
        title_lbl.setStyleSheet(
            f"color: {COLOR_TEXT}; font: 600 16px '{FONT_UI}';"
            " background: transparent; border: none;"
        )
        title_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        head.addWidget(title_lbl, 1)
        layout.addLayout(head)

        sub_lbl = QLabel(subtitle)
        sub_lbl.setWordWrap(True)
        sub_lbl.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; font: 12px '{FONT_UI}';"
            " background: transparent; border: none;"
        )
        sub_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout.addWidget(sub_lbl, 1)

    def _apply_style(self) -> None:
        bg = COLOR_ELEVATED if self._hover else COLOR_SURFACE
        border = COLOR_ACCENT if self._hover else COLOR_BORDER
        self.setStyleSheet(
            f"QFrame#SupportToolTile {{ background: {bg};"
            f" border: 1px solid {border}; border-radius: {RADIUS_LG}px; }}"
            "QFrame#SupportToolTile QLabel { background: transparent; border: none; }"
        )

    def enterEvent(self, event: QEvent) -> None:
        self._hover = True
        self._apply_style()
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        self._hover = False
        self._apply_style()
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class SupportToolsScreen(ScreenContent):
    """Top-level screen that opens supporting sub-tools."""

    log_message = Signal(str, str)
    title_changed = Signal(str)

    MENU_TITLE = "Một số công cụ hỗ trợ"
    TOOL_TITLES = {
        "accuracy": "Đo độ chính xác OCR",
        "secret_scan": "Phát hiện file mật trong thư mục",
    }

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setStyleSheet(f"background: {COLOR_BG};")
        self._accuracy = AccuracyScreen()
        self._secret_scan = SecretFileScanScreen()
        self._tools: dict[str, ScreenContent] = {
            "accuracy": self._accuracy,
            "secret_scan": self._secret_scan,
        }
        self._sub_pages: dict[str, QWidget] = {}
        self._build_ui()
        for child in self._tools.values():
            if hasattr(child, "log_message"):
                child.log_message.connect(
                    lambda msg, lvl: self.log_message.emit(msg, lvl)
                )

    def required_models(self) -> list[str]:
        # Supporting tools should open immediately. Individual actions lazily
        # load OCR only if they actually need it.
        return []

    def is_busy(self) -> bool:
        return any(tool.is_busy() for tool in self._tools.values())

    def request_cancel(self) -> None:
        for tool in self._tools.values():
            if tool.is_busy():
                tool.request_cancel()

    def handle_back_requested(self) -> bool:
        """Let the outer header's single Back button return to the tool menu."""
        if self._stack.currentWidget() == self._menu:
            return False
        if self.is_busy():
            confirm = QMessageBox(self)
            confirm.setWindowTitle("Xác nhận dừng")
            confirm.setIcon(QMessageBox.Icon.Question)
            confirm.setText("Đang có tác vụ chạy. Dừng lại?")
            confirm.setInformativeText("Tác vụ hiện tại sẽ được yêu cầu hủy.")
            confirm.setStandardButtons(
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            confirm.setDefaultButton(QMessageBox.StandardButton.No)
            if confirm.exec() != QMessageBox.StandardButton.Yes:
                return True
            self.request_cancel()
        self._stack.setCurrentWidget(self._menu)
        self.title_changed.emit(self.MENU_TITLE)
        return True

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._stack = QStackedWidget()
        root.addWidget(self._stack, 1)

        self._menu = QWidget()
        menu_layout = QVBoxLayout(self._menu)
        menu_layout.setContentsMargins(SP[5], SP[5], SP[5], SP[5])
        menu_layout.setSpacing(SP[3])

        title = QLabel(self.MENU_TITLE)
        title.setStyleSheet(
            f"color: {COLOR_TEXT}; font: 700 20px '{FONT_UI}'; background: transparent;"
        )
        menu_layout.addWidget(title)

        grid_host = QWidget()
        grid_host.setStyleSheet("background: transparent;")
        grid = QGridLayout(grid_host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(SP[3])
        grid.setVerticalSpacing(SP[3])
        menu_layout.addWidget(grid_host)

        accuracy = _ToolTile(
            "🎯",
            "Đo độ chính xác OCR",
            "So sánh PDF OCR với ground truth bằng CER/WER.",
        )
        accuracy.clicked.connect(lambda: self._open_tool("accuracy"))
        grid.addWidget(accuracy, 0, 0)

        secret_scan = _ToolTile(
            "🔒",
            "Phát hiện file mật trong thư mục",
            "Quét PDF, ảnh và Word để tìm dấu MẬT, TỐI MẬT, TUYỆT MẬT.",
        )
        secret_scan.clicked.connect(lambda: self._open_tool("secret_scan"))
        grid.addWidget(secret_scan, 0, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        menu_layout.addStretch(1)

        self._stack.addWidget(self._menu)
        self._sub_pages["accuracy"] = self._accuracy
        self._sub_pages["secret_scan"] = self._secret_scan
        self._stack.addWidget(self._sub_pages["accuracy"])
        self._stack.addWidget(self._sub_pages["secret_scan"])
        self._stack.setCurrentWidget(self._menu)

    def _open_tool(self, key: str) -> None:
        page = self._sub_pages.get(key)
        if page is None:
            return
        self._stack.setCurrentWidget(page)
        self.title_changed.emit(self.TOOL_TITLES.get(key, self.MENU_TITLE))
