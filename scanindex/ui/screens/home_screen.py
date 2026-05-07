"""Home screen — 5 tile buttons that navigate to functional screens.

Tiles use QFrame (not QPushButton) so QLabel word-wrap inside the tile works
correctly and the layout is responsive when the parent splitter resizes.
The grid reflows from 3 columns → 2 → 1 depending on available width.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QFont, QMouseEvent
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QScrollArea, QSizePolicy,
    QVBoxLayout, QWidget,
)

from scanindex.ui.theme import (
    COLOR_ACCENT, COLOR_BG, COLOR_BORDER, COLOR_ELEVATED, COLOR_SURFACE,
    COLOR_TEXT, COLOR_TEXT_SECONDARY, FONT_UI, RADIUS_LG, SP,
)

# Function identifiers (used for navigation + model declarations)
FUNCTION_HOME = "home"
FUNCTION_PDF_TO_WORD = "pdf_to_word"
FUNCTION_DIGITIZATION = "digitization"
FUNCTION_REPOSITORY = "repository"
FUNCTION_ARCHIVE = FUNCTION_DIGITIZATION
FUNCTION_KHO_LUU_TRU = FUNCTION_REPOSITORY
FUNCTION_SETTINGS = "settings"
FUNCTION_ABOUT = "about"
FUNCTION_ACCURACY = "accuracy"


# Width breakpoints for grid reflow
_BP_3_COL = 880
_BP_2_COL = 560


class _Tile(QFrame):
    """A clickable tile with icon + title + subtitle. Uses QFrame instead of
    QPushButton so QLabel word-wrap heights compute correctly. Object name
    `HomeTile` keeps the border from leaking onto child QLabels."""

    clicked = Signal()

    def __init__(self, icon: str, title: str, subtitle: str,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("HomeTile")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(140)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._apply_style(hover=False)
        self.setMouseTracking(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(SP[4], SP[3], SP[4], SP[3])
        layout.setSpacing(SP[2])

        # Icon + title row
        header = QHBoxLayout()
        header.setSpacing(SP[2])
        header.setContentsMargins(0, 0, 0, 0)

        self._icon_lbl = QLabel(icon)
        self._icon_lbl.setStyleSheet(
            f"color: {COLOR_ACCENT}; font: 22px '{FONT_UI}';"
            f" background: transparent; border: none; padding: 0;"
        )
        self._icon_lbl.setFixedWidth(28)
        self._icon_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._icon_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        header.addWidget(self._icon_lbl, 0)

        self._title_lbl = QLabel(title)
        self._title_lbl.setStyleSheet(
            f"color: {COLOR_TEXT}; font: 600 16px '{FONT_UI}';"
            f" background: transparent; border: none; padding: 0;"
        )
        self._title_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._title_lbl.setWordWrap(True)
        self._title_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        header.addWidget(self._title_lbl, 1)

        layout.addLayout(header)

        self._sub_lbl = QLabel(subtitle)
        self._sub_lbl.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; font: 12px '{FONT_UI}';"
            f" background: transparent; border: none; padding: 0;"
        )
        self._sub_lbl.setWordWrap(True)
        self._sub_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._sub_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._sub_lbl.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self._sub_lbl, 1)

        self._status_lbl = QLabel("")
        self._status_lbl.setVisible(False)
        self._status_lbl.setWordWrap(True)
        self._status_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._status_lbl.setStyleSheet(
            f"color: {COLOR_ACCENT}; font: 600 11px '{FONT_UI}';"
            " background: transparent; border: none; padding-top: 4px;"
        )
        layout.addWidget(self._status_lbl)

    def _apply_style(self, hover: bool):
        border_color = COLOR_ACCENT if hover else COLOR_BORDER
        bg = COLOR_ELEVATED if hover else COLOR_SURFACE
        # Scoped to #HomeTile so child QLabels do NOT inherit the border
        self.setStyleSheet(
            f"QFrame#HomeTile {{"
            f"  background: {bg}; border: 1px solid {border_color};"
            f"  border-radius: {RADIUS_LG}px;"
            f"}}"
            f"QFrame#HomeTile QLabel {{ border: none; background: transparent; }}"
        )

    def enterEvent(self, event: QEvent) -> None:
        self._apply_style(hover=True)
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        self._apply_style(hover=False)
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    def set_status(self, text: str) -> None:
        text = text or ""
        self._status_lbl.setText(text)
        self._status_lbl.setVisible(bool(text))


class HomeScreen(QWidget):
    """Dashboard with 5 function tiles that reflow based on available width."""

    function_selected = Signal(str)  # emits FUNCTION_*

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setStyleSheet(f"background: {COLOR_BG};")
        self._tiles: list[tuple[str, _Tile]] = []
        self._tile_by_key: dict[str, _Tile] = {}
        self._current_cols = 0
        self._build_ui()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Scroll area so very-narrow windows still reach all tiles
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setStyleSheet(f"background: {COLOR_BG};")
        outer.addWidget(scroll)

        inner = QWidget()
        inner.setStyleSheet(f"background: {COLOR_BG};")
        scroll.setWidget(inner)

        v = QVBoxLayout(inner)
        v.setContentsMargins(SP[6], SP[6], SP[6], SP[6])
        v.setSpacing(SP[3])

        title = QLabel("ScanIndex")
        title.setStyleSheet(
            f"color: {COLOR_TEXT}; font: 700 26px '{FONT_UI}'; background: transparent;"
        )
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(title)

        subtitle = QLabel("Chọn chức năng để bắt đầu")
        subtitle.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; font: 13px '{FONT_UI}'; background: transparent;"
        )
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(subtitle)

        v.addSpacing(SP[3])

        # Container for the responsive grid
        self._grid_host = QWidget()
        self._grid_host.setStyleSheet("background: transparent;")
        self._grid = QGridLayout(self._grid_host)
        self._grid.setHorizontalSpacing(SP[3])
        self._grid.setVerticalSpacing(SP[3])
        v.addWidget(self._grid_host)
        v.addStretch(1)

        specs = [
            (FUNCTION_PDF_TO_WORD, "📄", "Chuyển scan PDF → Word",
             "OCR file PDF scan, sửa lỗi chính tả tiếng Việt, "
             "xuất ra DOCX có bảng biểu và hình ảnh."),
            (FUNCTION_ARCHIVE, "🗂", "Số hóa lưu trữ",
             "OCR cả thư mục, trích xuất metadata "
             "văn bản hành chính cho lưu trữ."),
            (FUNCTION_KHO_LUU_TRU, "🔎", "Kho lưu trữ",
             "Tra cứu toàn văn + ngữ nghĩa kho PDF đã số hóa, "
             "lọc theo metadata HSLTCQ."),
            (FUNCTION_ACCURACY, "🎯", "Đo độ chính xác OCR",
             "Tải lên PDF có ground truth, "
             "so sánh kết quả OCR (CER/WER)."),
            (FUNCTION_SETTINGS, "⚙", "Cấu hình",
             "Tùy chỉnh tốc độ xử lý, model, "
             "ngôn ngữ và tùy chọn nâng cao."),
            (FUNCTION_ABOUT, "ℹ", "Giới thiệu",
             "Thông tin phiên bản, phạm vi sử dụng, "
             "chức năng và công nghệ."),
        ]
        for key, icon, t, sub in specs:
            tile = _Tile(icon, t, sub)
            tile.clicked.connect(lambda k=key: self.function_selected.emit(k))
            self._tiles.append((key, tile))
            self._tile_by_key[key] = tile

        self._relayout(cols=3)

    def _relayout(self, cols: int):
        if cols == self._current_cols:
            return
        self._current_cols = cols
        # Remove all from grid (without deleting)
        while self._grid.count():
            item = self._grid.takeAt(0)
            if item is None:
                continue
            w = item.widget()
            if w is not None:
                w.setParent(None)
        # Re-add in new column count
        for idx, (_, tile) in enumerate(self._tiles):
            row, col = divmod(idx, cols)
            self._grid.addWidget(tile, row, col)
            tile.setParent(self._grid_host)
            tile.show()
        # Equal stretch on each column
        for c in range(self._grid.columnCount()):
            self._grid.setColumnStretch(c, 0)
        for c in range(cols):
            self._grid.setColumnStretch(c, 1)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        w = self.width()
        if w >= _BP_3_COL:
            cols = 3
        elif w >= _BP_2_COL:
            cols = 2
        else:
            cols = 1
        self._relayout(cols)

    def set_function_status(self, function_id: str, text: str) -> None:
        tile = self._tile_by_key.get(function_id)
        if tile is not None:
            tile.set_status(text)
