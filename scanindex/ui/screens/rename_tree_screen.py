"""Đổi tên theo cây thư mục — công cụ cho cấu trúc CSDL_SOHOA.

Đây là VỎ mỏng quanh :class:`scanindex.ui.widgets.rename_tree_editor.
RenameTreeEditor` — widget chỉnh cây DÙNG CHUNG (kế hoạch nhập thư mục nhiều
hồ sơ): toàn bộ hành vi cây (lazy populate, kéo-thả, đổi tên cascade, đánh
số, Ctrl+Z, xem PDF) sống ở editor; màn hình này chỉ còn toolbar chọn thư
mục gốc + nạp/lưu settings (``config/rename_tree_settings.json``).

Popup chọn hồ sơ ở Bước 2 Số hóa lưu trữ nhúng CÙNG editor đó — sửa lỗi
hành vi cây ở editor có hiệu lực ở cả hai nơi. Các thuộc tính/method cây
(``tree``, ``_set_root``, ``_item_for_rel``, …) được forward về editor để
giữ tương thích với test và mã gọi cũ.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QVBoxLayout, QWidget,
)

from scanindex.core import rename_tree as rt  # noqa: F401 (re-export)
from scanindex.infra import translations
from scanindex.ui.screens.screen_base import ScreenContent
from scanindex.ui.theme import (
    COLOR_ACCENT, COLOR_ACCENT_HOVER, COLOR_BG, COLOR_BORDER, COLOR_PANEL,
    COLOR_SURFACE, COLOR_TEXT, COLOR_TEXT_SECONDARY, FONT_MONO, FONT_UI,
    RADIUS_MD, SP,
)
from scanindex.ui.widgets.rename_tree_editor import (
    RenameTreeEditor,
    _DUMMY_TEXT,
    _FOLDER_FALLBACK_COLOR,
    _ICON_CACHE,
    _LEVEL_COLOR,
    _MOVABLE_LEVELS,
    _ROLE_BADGE,
    _ROLE_ISDIR,
    _ROLE_LEVEL,
    _ROLE_LOADED,
    _ROLE_REORDERABLE,
    _ROLE_REL,
    _ROLE_TIP,
    _RenameDialog,
    _RenameWorker,
    _ReorderTree,
    _UndoEntry,
    _dossier_icon,
    _file_icon,
    _folder_icon,
)

try:
    from scanindex.infra.paths import get_base_dir
except Exception:
    def get_base_dir():
        return os.getcwd()

_SETTINGS_FILE = os.path.join(get_base_dir(), "config", "rename_tree_settings.json")


class RenameTreeScreen(ScreenContent):
    """Công cụ đổi tên theo cây thư mục CSDL_SOHOA (vỏ của RenameTreeEditor)."""

    log_message = Signal(str, str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setStyleSheet(f"background: {COLOR_BG};")
        self._editor = RenameTreeEditor(self)
        self._build_ui()
        # Forward log của editor ra màn hình (support tools + main window đều
        # kết nối signal của screen).
        self._editor.log_message.connect(self.log_message)
        self._editor.root_changed.connect(self._on_editor_root_changed)
        self._editor.orders_changed.connect(self._save_settings)
        self._editor.status_text.connect(self.lbl_stats.setText)
        self._editor.busy_changed.connect(self._on_editor_busy_changed)
        self._load_settings()

    # ------------------------------------------------------------- layout

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SP[4], SP[4], SP[4], SP[4])
        outer.setSpacing(SP[2])

        bar = QFrame()
        bar.setStyleSheet("QFrame { background: transparent; }")
        h = QHBoxLayout(bar)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(SP[2])

        self.btn_pick = QPushButton("📂  Chọn thư mục CSDL_SOHOA…")
        self.btn_pick.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_pick.setStyleSheet(
            f"QPushButton {{ background: {COLOR_ACCENT}; color: white;"
            f" border: none; padding: 8px 16px; border-radius: {RADIUS_MD}px;"
            f" font: 600 13px '{FONT_UI}'; }}"
            f"QPushButton:hover {{ background: {COLOR_ACCENT_HOVER}; }}"
        )
        self.btn_pick.clicked.connect(self._pick_root)
        h.addWidget(self.btn_pick)

        self.edit_root = QLineEdit()
        self.edit_root.setReadOnly(True)
        self.edit_root.setPlaceholderText("Chưa chọn thư mục gốc")
        self.edit_root.setStyleSheet(
            f"QLineEdit {{ background: {COLOR_SURFACE}; color: {COLOR_TEXT};"
            f" border: 1px solid {COLOR_BORDER}; border-radius: {RADIUS_MD}px;"
            f" padding: 6px 10px; font: 12px '{FONT_MONO}'; }}"
        )
        h.addWidget(self.edit_root, 1)

        self.btn_refresh = QPushButton("🔄  Làm mới")
        self.btn_refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_refresh.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {COLOR_TEXT_SECONDARY};"
            f" border: 1px solid {COLOR_BORDER}; padding: 6px 14px;"
            f" border-radius: {RADIUS_MD}px; font: 500 13px '{FONT_UI}'; }}"
            f"QPushButton:hover {{ background: {COLOR_PANEL}; color: {COLOR_TEXT}; }}"
        )
        self.btn_refresh.clicked.connect(self._editor.refresh)
        h.addWidget(self.btn_refresh)

        self.lbl_stats = QLabel("")
        self.lbl_stats.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; font: 12px '{FONT_UI}';"
        )
        h.addWidget(self.lbl_stats)

        outer.addWidget(bar)
        outer.addWidget(self._editor, 1)

    # --------------------------------------------------------- chọn thư mục

    def _pick_root(self):
        if self._editor.is_busy():
            return
        root = self._editor.root()
        folder = QFileDialog.getExistingDirectory(
            self, translations.localize_text("Chọn thư mục gốc CSDL_SOHOA"),
            str(root) if root else "",
        )
        if not folder:
            return
        self._editor.set_root(Path(folder))

    def _on_editor_root_changed(self, root):
        self.edit_root.setText(str(root or ""))

    def _on_editor_busy_changed(self, busy: bool):
        self.btn_pick.setEnabled(not busy)
        self.btn_refresh.setEnabled(not busy)

    def showEvent(self, event):
        super().showEvent(event)
        # Editor tự lo việc bung full khi thành visible (showEvent của nó).

    # ----------------------------------------------------- ScreenContent

    def is_busy(self) -> bool:
        return self._editor.is_busy()

    def request_cancel(self) -> None:
        self._editor.request_cancel()

    def update_texts(self) -> None:
        self._editor.update_texts()

    # ------------------------------------------------- tương thích ngược

    def __getattr__(self, name):
        """Forward mọi thứ cây về editor dùng chung (test và mã gọi cũ vẫn
        dùng ``screen.tree``, ``screen._set_root``, ``screen._item_for_rel``,
        ``screen.preview_stack``, ``screen._current_pdf_rel``, …)."""
        if name.startswith("__"):
            raise AttributeError(name)
        try:
            editor = self.__dict__["_editor"]
        except KeyError:
            raise AttributeError(name) from None
        return getattr(editor, name)

    # ------------------------------------------------------------- settings

    def _load_settings(self):
        try:
            if not os.path.exists(_SETTINGS_FILE):
                return
            with open(_SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            root = str(data.get("root") or "").strip()
            if root and os.path.isdir(root):
                self._editor.set_root(Path(root))
                order = data.get("order")
                if isinstance(order, dict):
                    # Chỉ nhận thứ tự thủ công của cây vừa mở lại.
                    self._editor.restore_manual_order(order)
                    # set_root đã xả file settings với thứ tự rỗng (nó phải
                    # clear trước khi đọc được root) — ghi lại ngay, kẻo app
                    # đóng trước một lần _save_settings kế tiếp thì mất.
                    self._save_settings()
        except Exception:
            pass

    def _save_settings(self):
        try:
            root = self._editor.root()
            os.makedirs(os.path.dirname(_SETTINGS_FILE), exist_ok=True)
            with open(_SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(
                    {"root": str(root or ""),
                     "order": self._editor.manual_order()},
                    f, ensure_ascii=False, indent=2)
        except Exception:
            pass

