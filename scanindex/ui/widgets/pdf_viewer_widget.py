"""
PDF Viewer Widget — Continuous-scroll viewer with:
- Ctrl+wheel zoom anchored to cursor position
- Middle-click or left-click drag to pan
- All pages stacked vertically, smooth scroll
"""
import re
import time

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QScrollArea, QLabel,
    QPushButton, QSizePolicy, QFrame, QApplication
)
from PySide6.QtCore import Qt, Signal, QRect, QRectF, QTimer, QSize, QPoint
from PySide6.QtGui import (
    QPixmap, QImage, QPainter, QColor, QPen, QWheelEvent,
    QMouseEvent, QCursor, QKeySequence, QShortcut
)

import threading

from scanindex.ui.theme import (
    COLOR_BG, COLOR_SURFACE, COLOR_ELEVATED, COLOR_HOVER,
    COLOR_BORDER, COLOR_BORDER_DEFAULT,
    COLOR_TEXT, COLOR_TEXT_SECONDARY, COLOR_TEXT_MUTED, COLOR_ACCENT,
    COLOR_RED,
    SP, RADIUS_MD, FONT_UI
)
from scanindex.infra import translations

# ---------- Design tokens ----------
_H = 26
_FONT = 12
_FONT_SM = 11
_PAGE_GAP = 6

_TOOLBAR_BG = COLOR_SURFACE

_ICON_BTN = f"""
    QPushButton {{
        background: transparent;
        border: 1px solid transparent;
        border-radius: 4px;
        color: {COLOR_TEXT_SECONDARY};
        font-size: {_FONT}px;
        font-family: {FONT_UI};
        min-width: {_H}px; max-width: {_H}px;
        min-height: {_H}px; max-height: {_H}px;
        padding: 0;
    }}
    QPushButton:hover {{
        background: {COLOR_ELEVATED};
        border-color: {COLOR_BORDER_DEFAULT};
        color: {COLOR_TEXT};
    }}
    QPushButton:pressed {{ background: {COLOR_HOVER}; }}
    QPushButton:disabled {{ color: {COLOR_BORDER_DEFAULT}; }}
"""

_TEXT_BTN = f"""
    QPushButton {{
        background: transparent;
        border: 1px solid transparent;
        border-radius: 4px;
        color: {COLOR_TEXT_SECONDARY};
        font-size: {_FONT_SM}px;
        font-family: {FONT_UI};
        padding: 0 8px;
        min-height: {_H}px; max-height: {_H}px;
    }}
    QPushButton:hover {{
        background: {COLOR_ELEVATED};
        border-color: {COLOR_BORDER_DEFAULT};
        color: {COLOR_TEXT};
    }}
    QPushButton:pressed {{ background: {COLOR_HOVER}; }}
"""

_TOGGLE_TEXT_BTN = _TEXT_BTN + f"""
    QPushButton:checked {{
        background: {COLOR_ACCENT};
        border-color: {COLOR_ACCENT};
        color: white;
    }}
"""

_LABEL_STYLE = f"color: {COLOR_TEXT_MUTED}; font-size: {_FONT_SM}px; font-family: {FONT_UI};"

# Per-KIE-label color map (matches the conventions used by kie_viewer)
_LABEL_COLORS = {
    "REGIME_HEADER":      "#ff6b6b",
    "ISSUE_ORG_SUPERIOR": "#ffa94d",
    "ISSUE_ORG_NAME":     "#ffd43b",
    "DOC_NUMBER_SYMBOL":  "#94d82d",
    "PLACE_DATE":         "#868e96",
    "DOC_SUBJECT":        "#3bc9db",
    "ADDRESSEE":          "#4dabf7",
    "RECIPIENTS":         "#748ffc",
    "SIGNER_ROLE":        "#9775fa",
    "SIGNER_NAME":        "#da77f2",
    "URGENCY_MARK":       "#ff8787",
    "SECRECY_MARK":       "#fa5252",
    "CIRCULATION_MARK":   "#fab005",
    "DOC_TYPE":           "#15aabf",
}


class _ContinuousPageWidget(QWidget):
    """Stacks all rendered page pixmaps vertically with zone highlight overlay.

    Two layers of highlights:
      - `_overlays`: list of (page_idx, x, y, w, h, color, label, is_selected)
        — persistent, rendered on every paint. Used for KIE field bboxes.
      - `_zone`: a single transient highlight; takes precedence visually."""

    fuzzy_clicked = Signal(int, str, object)  # page_idx, text, bbox_pdf_or_pixel
    text_selection_dragged = Signal(object, object)
    text_selection_finished = Signal(object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._page_pixmaps = {}
        self._page_sizes = []
        self._page_y_offsets = []
        self._zone = None  # (page_idx, x, y, w, h)
        self._zone_color = QColor(COLOR_ACCENT)
        self._overlays: list[tuple] = []
        self._search_highlight = None  # [(page_idx, [(x, y, w, h), ...], style)]
        # Fuzzy match overlays — drawn in a distinct color, clickable
        self._fuzzy_overlays: list[dict] = []
        self._text_selection_enabled = False
        self._text_selecting = False
        self._text_selection_start = None
        self._text_selection_rects: list[tuple] = []
        self._text_drag_rect = None
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def set_pages(self, pixmaps):
        self._page_sizes = [pm.size() for pm in pixmaps]
        self._page_pixmaps = {i: pm for i, pm in enumerate(pixmaps)}
        self._zone = None
        self._overlays = []
        self._text_selection_rects = []
        self._recalc_offsets()
        self.update()

    def set_page_sizes(self, sizes):
        self._page_sizes = [QSize(s) for s in (sizes or [])]
        self._page_pixmaps = {}
        self._zone = None
        self._overlays = []
        self._text_selection_rects = []
        self._recalc_offsets()
        self.update()

    def set_page_pixmap(self, page_idx: int, pixmap: QPixmap):
        if 0 <= page_idx < len(self._page_sizes):
            self._page_pixmaps[int(page_idx)] = pixmap
            self.update(self._page_rect(page_idx))

    def release_page_pixmap(self, page_idx: int):
        if int(page_idx) in self._page_pixmaps:
            self._page_pixmaps.pop(int(page_idx), None)
            self.update(self._page_rect(page_idx))

    def rendered_page_indices(self):
        return set(self._page_pixmaps.keys())

    def clear_pages(self):
        self._page_pixmaps = {}
        self._page_sizes = []
        self._page_y_offsets = []
        self._zone = None
        self._overlays = []
        self._search_highlight = None
        self._text_selection_rects = []
        self._text_drag_rect = None
        self.setFixedSize(0, 0)
        self.update()

    def set_zone(self, page_idx, rect):
        self._zone = (page_idx, *rect)
        self.update()

    def clear_zone(self):
        self._zone = None
        self.update()

    def set_search_highlight(self, page_idx, rects, style="box"):
        self.set_search_highlights([(page_idx, list(rects or []), style or "box")])
        self.update()

    def set_search_highlights(self, highlights):
        cleaned = []
        for item in highlights or []:
            try:
                page_idx, rects, style = item
            except (TypeError, ValueError):
                continue
            page_rects = [r for r in (rects or []) if r and len(r) >= 4]
            if page_rects:
                cleaned.append((int(page_idx), page_rects, style or "box"))
        self._search_highlight = cleaned or None
        self.update()

    def clear_search_highlight(self):
        self._search_highlight = None
        self.update()

    def set_overlays(self, overlays):
        """Set the persistent overlay list. Each overlay is a tuple:
        (page_idx, x, y, w, h, color_hex, label, is_selected)."""
        self._overlays = list(overlays or [])
        self.update()

    def clear_overlays(self):
        self._overlays = []
        self.update()

    def set_fuzzy_overlays(self, overlays):
        """Each fuzzy overlay is a dict with keys:
        page_idx, x, y, w, h, text, score, rank."""
        self._fuzzy_overlays = list(overlays or [])
        self.update()

    def clear_fuzzy_overlays(self):
        self._fuzzy_overlays = []
        self.update()

    def set_text_selection_enabled(self, enabled: bool):
        self._text_selection_enabled = bool(enabled)
        if not enabled:
            self._text_selecting = False
            self._text_selection_start = None
            self._text_drag_rect = None
        self.setCursor(
            Qt.CursorShape.IBeamCursor if enabled else Qt.CursorShape.ArrowCursor
        )
        self.update()

    def set_text_selection_rects(self, rects):
        """Each rect is (page_idx, x, y, w, h) in rendered page pixels."""
        self._text_selection_rects = list(rects or [])
        self.update()

    def clear_text_selection_rects(self):
        self._text_selection_rects = []
        self.update()

    def mousePressEvent(self, event):
        if (
            self._text_selection_enabled
            and event.button() == Qt.MouseButton.LeftButton
        ):
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self._text_selecting = True
            self._text_selection_start = event.position().toPoint()
            self._text_drag_rect = QRectF(
                self._text_selection_start,
                self._text_selection_start,
            ).normalized()
            self.text_selection_dragged.emit(
                self._text_selection_start, self._text_selection_start
            )
            self.update()
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self._fuzzy_overlays:
            click_pos = event.position().toPoint()
            for ov in self._fuzzy_overlays:
                if not (0 <= ov["page_idx"] < len(self._page_y_offsets)):
                    continue
                y_off = self._page_y_offsets[ov["page_idx"]]
                size = self.page_size(ov["page_idx"])
                x_off = (self.width() - size.width()) // 2
                rx = int(x_off + ov["x"])
                ry = int(y_off + ov["y"])
                rw = int(ov["w"])
                rh = int(ov["h"])
                rect = QRect(rx, ry, rw, rh)
                if rect.contains(click_pos):
                    self.fuzzy_clicked.emit(ov["page_idx"], ov["text"], ov.get("bbox_pdf"))
                    event.accept()
                    return
        # Otherwise let QScrollArea handle pan
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._text_selection_enabled and self._text_selecting:
            if event.buttons() & Qt.MouseButton.LeftButton:
                start = self._text_selection_start or event.position().toPoint()
                current = event.position().toPoint()
                self._text_drag_rect = QRectF(start, current).normalized()
                self.text_selection_dragged.emit(start, current)
                self.update()
                event.accept()
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if (
            self._text_selection_enabled
            and self._text_selecting
            and event.button() == Qt.MouseButton.LeftButton
        ):
            self._text_selecting = False
            start = self._text_selection_start or event.position().toPoint()
            current = event.position().toPoint()
            self._text_selection_start = None
            self._text_drag_rect = None
            self.text_selection_finished.emit(start, current)
            self.update()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def page_y_offset(self, page_idx):
        if 0 <= page_idx < len(self._page_y_offsets):
            return self._page_y_offsets[page_idx]
        return 0

    def page_count(self):
        return len(self._page_sizes)

    def page_size(self, page_idx):
        if 0 <= int(page_idx) < len(self._page_sizes):
            return self._page_sizes[int(page_idx)]
        return QSize(0, 0)

    def _page_rect(self, page_idx):
        if not (0 <= int(page_idx) < len(self._page_y_offsets)):
            return QRect()
        size = self.page_size(page_idx)
        x_off = (self.width() - size.width()) // 2
        return QRect(x_off, self._page_y_offsets[int(page_idx)], size.width(), size.height())

    def _recalc_offsets(self):
        self._page_y_offsets = []
        y = 0
        max_w = 0
        for size in self._page_sizes:
            self._page_y_offsets.append(y)
            y += size.height() + _PAGE_GAP
            max_w = max(max_w, size.width())
        total_h = y - _PAGE_GAP if self._page_sizes else 0
        self.setFixedSize(max(max_w, 1), max(total_h, 1))

    def paintEvent(self, event):
        if not self._page_sizes:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        visible = event.rect()
        widget_w = self.width()

        for i, size in enumerate(self._page_sizes):
            y_off = self._page_y_offsets[i]
            if y_off + size.height() < visible.top() or y_off > visible.bottom():
                continue
            x_off = (widget_w - size.width()) // 2
            pm = self._page_pixmaps.get(i)
            if pm is not None:
                painter.drawPixmap(x_off, y_off, pm)
            else:
                painter.fillRect(QRect(x_off, y_off, size.width(), size.height()), QColor("#ffffff"))
                painter.setPen(QPen(QColor(COLOR_BORDER_DEFAULT), 1))
                painter.drawRect(QRect(x_off, y_off, size.width(), size.height()))

        # Persistent overlays (KIE field bboxes)
        for overlay in self._overlays:
            try:
                pi, zx, zy, zw, zh, color_hex, label, is_selected = overlay
            except (ValueError, TypeError):
                continue
            if not (0 <= pi < len(self._page_y_offsets)):
                continue
            y_off = self._page_y_offsets[pi]
            size = self.page_size(pi)
            x_off = (widget_w - size.width()) // 2
            rx, ry = int(x_off + zx), int(y_off + zy)
            rw, rh = int(zw), int(zh)
            color = QColor(color_hex) if color_hex else QColor(COLOR_ACCENT)
            fill = QColor(color)
            fill.setAlpha(80 if is_selected else 35)
            painter.fillRect(QRect(rx, ry, rw, rh), fill)
            pen = QPen(color, 2 if is_selected else 1)
            painter.setPen(pen)
            painter.drawRect(QRect(rx, ry, rw, rh))
            if label:
                # Draw a small label badge at the top-left of the bbox
                badge_h = 14
                badge_w = max(40, len(label) * 7)
                badge_rect = QRect(rx, max(0, ry - badge_h), badge_w, badge_h)
                badge_fill = QColor(color)
                badge_fill.setAlpha(220)
                painter.fillRect(badge_rect, badge_fill)
                painter.setPen(QPen(QColor("#ffffff")))
                painter.drawText(badge_rect.adjusted(4, 0, -2, 0),
                                 Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                                 label)

        # Fuzzy match overlays — distinct cyan color, drawn on top of field
        # bboxes so users can see them clearly
        FUZZY_COLOR = QColor("#00ffff")
        for ov in self._fuzzy_overlays:
            pi = ov.get("page_idx", 0)
            if not (0 <= pi < len(self._page_y_offsets)):
                continue
            y_off = self._page_y_offsets[pi]
            size = self.page_size(pi)
            x_off = (widget_w - size.width()) // 2
            rx = int(x_off + ov["x"])
            ry = int(y_off + ov["y"])
            rw = int(ov["w"])
            rh = int(ov["h"])
            fill = QColor(FUZZY_COLOR)
            fill.setAlpha(60)
            painter.fillRect(QRect(rx, ry, rw, rh), fill)
            painter.setPen(QPen(FUZZY_COLOR, 2))
            painter.drawRect(QRect(rx, ry, rw, rh))
            score = ov.get("score", 0)
            rank = ov.get("rank", 0)
            badge = f"#{rank+1}  {score:.0f}%"
            badge_h = 14
            badge_w = max(60, len(badge) * 7)
            badge_rect = QRect(rx, max(0, ry - badge_h), badge_w, badge_h)
            painter.fillRect(badge_rect, FUZZY_COLOR)
            painter.setPen(QPen(QColor("#000000")))
            painter.drawText(badge_rect.adjusted(4, 0, -2, 0),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                             badge)

        # Search result highlight. Exact/fuzzy lexical matches use a
        # highlighter-style fill; semantic/body chunk matches keep the
        # broader box.
        if self._search_highlight:
            for pi, rects, style in self._search_highlight:
                if not (0 <= pi < len(self._page_y_offsets)):
                    continue
                y_off = self._page_y_offsets[pi]
                size = self.page_size(pi)
                x_off = (widget_w - size.width()) // 2
                color = QColor("#f59e0b" if style == "highlight" else COLOR_RED)
                for zx, zy, zw, zh in rects:
                    rx, ry = int(x_off + zx), int(y_off + zy)
                    rw, rh = int(zw), int(zh)
                    if style == "underline":
                        pen = QPen(color, 3)
                        painter.setPen(pen)
                        y = int(ry + rh * 0.88)
                        painter.drawLine(rx, y, rx + rw, y)
                    elif style == "highlight":
                        fill = QColor("#facc15")
                        fill.setAlpha(90)
                        painter.fillRect(QRect(rx, ry, rw, rh), fill)
                        painter.setPen(QPen(color, 2))
                        painter.drawRect(QRect(rx, ry, rw, rh))
                    else:
                        fill = QColor(color)
                        fill.setAlpha(35)
                        painter.fillRect(QRect(rx, ry, rw, rh), fill)
                        painter.setPen(QPen(color, 2))
                        painter.drawRect(QRect(rx, ry, rw, rh))

        # Single transient zone (takes precedence — drawn on top)
        if self._zone and len(self._zone) == 5:
            pi, zx, zy, zw, zh = self._zone
            if 0 <= pi < len(self._page_y_offsets):
                y_off = self._page_y_offsets[pi]
                size = self.page_size(pi)
                x_off = (widget_w - size.width()) // 2
                rx, ry = int(x_off + zx), int(y_off + zy)
                rw, rh = int(zw), int(zh)
                fill = QColor(self._zone_color)
                fill.setAlpha(35)
                painter.fillRect(QRect(rx, ry, rw, rh), fill)
                pen = QPen(self._zone_color, 2)
                painter.setPen(pen)
                painter.drawRect(QRect(rx, ry, rw, rh))

        if self._text_selection_rects:
            fill = QColor("#2563eb")
            fill.setAlpha(70)
            pen = QColor("#60a5fa")
            pen.setAlpha(220)
            for item in self._text_selection_rects:
                try:
                    pi, zx, zy, zw, zh = item
                except (ValueError, TypeError):
                    continue
                if not (0 <= pi < len(self._page_y_offsets)):
                    continue
                y_off = self._page_y_offsets[pi]
                size = self.page_size(pi)
                x_off = (widget_w - size.width()) // 2
                rx, ry = int(x_off + zx), int(y_off + zy)
                rw, rh = max(1, int(zw)), max(1, int(zh))
                rect = QRect(rx, ry, rw, rh)
                painter.fillRect(rect, fill)
                painter.setPen(QPen(pen, 1.5))
                painter.drawRect(rect)

        if self._text_drag_rect is not None:
            drag_fill = QColor(0, 200, 255, 30)
            drag_pen = QColor(0, 200, 255, 180)
            painter.setBrush(drag_fill)
            painter.setPen(QPen(drag_pen, 1.5, Qt.PenStyle.DashLine))
            painter.drawRect(self._text_drag_rect)

        painter.end()


class _PanZoomScrollArea(QScrollArea):
    """
    QScrollArea with:
    - Ctrl+Wheel → zoom anchored to cursor
    - Left-click drag → pan (hand tool)
    """

    zoom_at_pos = Signal(int, QPoint)  # direction (+1/-1), viewport position

    def __init__(self, parent=None):
        super().__init__(parent)
        self._panning = False
        self._pan_start = QPoint()
        self._pan_hbar_start = 0
        self._pan_vbar_start = 0
        self._text_selection_enabled = False

    def set_text_selection_enabled(self, enabled: bool):
        self._text_selection_enabled = bool(enabled)
        if enabled and self._panning:
            self._panning = False
        self.setCursor(
            Qt.CursorShape.IBeamCursor
            if self._text_selection_enabled
            else Qt.CursorShape.OpenHandCursor
        )

    def wheelEvent(self, event: QWheelEvent):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            if delta != 0:
                direction = 1 if delta > 0 else -1
                self.zoom_at_pos.emit(direction, event.position().toPoint())
            event.accept()
        else:
            super().wheelEvent(event)

    def mousePressEvent(self, event: QMouseEvent):
        can_left_pan = (
            event.button() == Qt.MouseButton.LeftButton
            and not self._text_selection_enabled
        )
        can_middle_pan = event.button() == Qt.MouseButton.MiddleButton
        if can_left_pan or can_middle_pan:
            self._panning = True
            self._pan_start = event.globalPosition().toPoint()
            self._pan_hbar_start = self.horizontalScrollBar().value()
            self._pan_vbar_start = self.verticalScrollBar().value()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._panning:
            delta = event.globalPosition().toPoint() - self._pan_start
            self.horizontalScrollBar().setValue(self._pan_hbar_start - delta.x())
            self.verticalScrollBar().setValue(self._pan_vbar_start - delta.y())
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        if self._panning:
            self._panning = False
            self.setCursor(
                Qt.CursorShape.IBeamCursor
                if self._text_selection_enabled
                else Qt.CursorShape.OpenHandCursor
            )
            event.accept()
        else:
            super().mouseReleaseEvent(event)


class PdfViewerWidget(QWidget):
    """
    Continuous-scroll PDF viewer with pan and zoom-to-cursor.
    """

    prev_file_requested = Signal()
    next_file_requested = Signal()
    page_changed = Signal(int)
    _pages_rendered = Signal(object)

    RENDER_DPI = 150
    ZOOM_STEPS = [0.25, 0.33, 0.5, 0.67, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0]
    RENDER_CACHE_MAX_PAGES = 18

    def __init__(self, parent=None, *, fit_on_load=True,
                 text_selection_available=False):
        super().__init__(parent)
        self._doc = None
        self._page_count = 0
        self._pdf_path = None
        self._raw_pixmaps = {}
        self._page_point_sizes = []
        self._render_queue = []
        self._render_queued = set()
        self._render_active = False
        self._render_active_gen = None
        self._zoom = 1.0
        self._fit_on_load = bool(fit_on_load)
        self._fit_mode = True
        self._file_label_text = ""
        self._render_gen = 0
        self._render_threads_lock = threading.Lock()
        self._active_render_threads = set()
        self._current_search_highlight = None
        self._pending_view_scroll = None
        self._text_selection_available = bool(text_selection_available)
        self._text_selection_enabled = False
        self._selection_words_by_page = None
        self._selected_words = []
        self._selected_text = ""
        self._pages_rendered.connect(self._on_pages_rendered)
        self._hires_timer = QTimer(self)
        self._hires_timer.setSingleShot(True)
        self._hires_timer.setInterval(300)
        self._hires_timer.timeout.connect(self._start_hires_render)
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setSingleShot(True)
        self._scroll_timer.setInterval(60)
        self._scroll_timer.timeout.connect(self._scroll_debounce_fire)
        self._setup_ui()
        self._copy_shortcut = QShortcut(QKeySequence.StandardKey.Copy, self)
        self._copy_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._copy_shortcut.activated.connect(self.copy_selected_text)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # --- Toolbar ---
        tb_frame = QFrame()
        tb_frame.setFixedHeight(32)
        tb_frame.setStyleSheet(f"""
            QFrame {{
                background: {_TOOLBAR_BG};
                border-bottom: 1px solid {COLOR_BORDER};
            }}
        """)
        toolbar = QHBoxLayout(tb_frame)
        toolbar.setContentsMargins(6, 0, 6, 0)
        toolbar.setSpacing(2)

        # File navigation
        self._btn_prev_file = QPushButton("\u25C0")
        self._btn_prev_file.setStyleSheet(_ICON_BTN)
        self._btn_prev_file.setToolTip("Previous file")
        self._btn_prev_file.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_prev_file.clicked.connect(self.prev_file_requested.emit)
        toolbar.addWidget(self._btn_prev_file)

        self._lbl_file = QLabel()
        self._lbl_file.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lbl_file.setMinimumWidth(60)
        self._lbl_file.setStyleSheet(_LABEL_STYLE)
        toolbar.addWidget(self._lbl_file)

        self._btn_next_file = QPushButton("\u25B6")
        self._btn_next_file.setStyleSheet(_ICON_BTN)
        self._btn_next_file.setToolTip("Next file")
        self._btn_next_file.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_next_file.clicked.connect(self.next_file_requested.emit)
        toolbar.addWidget(self._btn_next_file)

        # Separator
        sep = QFrame()
        self._file_nav_sep = sep
        sep.setFixedSize(1, 16)
        sep.setStyleSheet(f"background: {COLOR_BORDER_DEFAULT};")
        toolbar.addSpacing(4)
        toolbar.addWidget(sep)
        toolbar.addSpacing(4)

        # Zoom controls
        self._btn_zoom_out = QPushButton("\u2212")
        self._btn_zoom_out.setStyleSheet(_ICON_BTN)
        self._btn_zoom_out.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_zoom_out.clicked.connect(self._zoom_out)
        toolbar.addWidget(self._btn_zoom_out)

        self._lbl_zoom = QLabel("100%")
        self._lbl_zoom.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lbl_zoom.setFixedWidth(44)
        self._lbl_zoom.setStyleSheet(_LABEL_STYLE)
        toolbar.addWidget(self._lbl_zoom)

        self._btn_zoom_in = QPushButton("+")
        self._btn_zoom_in.setStyleSheet(_ICON_BTN)
        self._btn_zoom_in.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_zoom_in.clicked.connect(self._zoom_in)
        toolbar.addWidget(self._btn_zoom_in)

        self._btn_fit = QPushButton("Fit")
        self._btn_fit.setStyleSheet(_TEXT_BTN)
        self._btn_fit.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_fit.clicked.connect(self._zoom_fit)
        toolbar.addWidget(self._btn_fit)

        self._btn_select_text = QPushButton("Chọn chữ")
        self._btn_select_text.setCheckable(True)
        self._btn_select_text.setStyleSheet(_TOGGLE_TEXT_BTN)
        self._btn_select_text.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_select_text.setToolTip("Bật chế độ kéo chọn chữ; Ctrl+C để copy")
        self._btn_select_text.toggled.connect(self.set_text_selection_enabled)
        self._btn_select_text.setVisible(self._text_selection_available)
        toolbar.addWidget(self._btn_select_text)

        # Page indicator
        toolbar.addSpacing(4)
        self._lbl_page = QLabel()
        self._lbl_page.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lbl_page.setMinimumWidth(60)
        self._lbl_page.setStyleSheet(_LABEL_STYLE)
        toolbar.addWidget(self._lbl_page)

        toolbar.addStretch()
        layout.addWidget(tb_frame)

        # --- Scroll area with pan + zoom ---
        self._scroll = _PanZoomScrollArea()
        self._scroll.setWidgetResizable(False)
        self._scroll.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self._scroll.setCursor(Qt.CursorShape.OpenHandCursor)
        self._scroll.setStyleSheet(f"""
            QScrollArea {{ background: {COLOR_BG}; border: none; }}
            QScrollBar:vertical {{
                background: transparent; width: 8px; margin: 0;
            }}
            QScrollBar::handle:vertical {{
                background: {COLOR_BORDER_DEFAULT}; border-radius: 4px; min-height: 30px;
            }}
            QScrollBar::handle:vertical:hover {{ background: {COLOR_TEXT_MUTED}; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
            QScrollBar:horizontal {{
                background: transparent; height: 8px; margin: 0;
            }}
            QScrollBar::handle:horizontal {{
                background: {COLOR_BORDER_DEFAULT}; border-radius: 4px; min-width: 30px;
            }}
            QScrollBar::handle:horizontal:hover {{ background: {COLOR_TEXT_MUTED}; }}
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
        """)
        self._scroll.zoom_at_pos.connect(self._on_zoom_at_pos)
        self._scroll.verticalScrollBar().valueChanged.connect(self._on_scroll)

        self._pages_widget = _ContinuousPageWidget()
        self._pages_widget.text_selection_dragged.connect(
            self._on_text_selection_dragged
        )
        self._pages_widget.text_selection_finished.connect(
            self._on_text_selection_finished
        )
        self._scroll.setWidget(self._pages_widget)
        layout.addWidget(self._scroll, 1)

        # --- Hint (empty state) ---
        self._hint_label = QLabel(translations.get_text("arc_no_preview"))
        self._hint_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._hint_label.setWordWrap(True)
        self._hint_label.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 12px; font-family: {FONT_UI};")
        self._hint_label.setParent(self._scroll)
        self._hint_label.setGeometry(0, 0, 300, 80)

        self._update_nav_state()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._hint_label.isVisible():
            sw, sh = self._scroll.width(), self._scroll.height()
            self._hint_label.setGeometry(0, sh // 3, sw, 60)
        if self._fit_mode and self._page_point_sizes:
            self._rebuild_scaled_pages()

    # ------ Public API ------

    def load_pdf(self, pdf_path):
        self._close_doc()
        self._hires_timer.stop()
        self._scroll_timer.stop()
        self._raw_pixmaps = {}
        self._page_point_sizes = []
        self._render_queue = []
        self._render_queued = set()
        self._render_active = False
        self._render_active_gen = None
        self._pages_widget.clear_pages()
        self._current_search_highlight = None
        self._pending_view_scroll = None
        self._selection_words_by_page = None
        self._clear_text_selection()
        self._pages_widget.clear_search_highlight()
        try:
            import fitz
            self._doc = fitz.open(pdf_path)
            self._pdf_path = pdf_path
            self._page_count = len(self._doc)
            self._page_point_sizes = [
                (
                    max(1.0, float(self._doc[i].rect.width)),
                    max(1.0, float(self._doc[i].rect.height)),
                )
                for i in range(self._page_count)
            ]
            self._fit_mode = self._fit_on_load
            self._hint_label.setVisible(False)
            self._render_gen += 1
            self._rebuild_scaled_pages(reset_generation=False)
            QTimer.singleShot(0, self._enqueue_viewport_pages)
            self._update_nav_state()
        except Exception as e:
            self._hint_label.setText(str(e))
            self._hint_label.setVisible(True)

    def set_file_label(self, current_idx, total):
        self._file_label_text = f"{current_idx + 1} / {total}" if total > 0 else ""
        self._lbl_file.setText(self._file_label_text)

    def set_file_nav_enabled(self, can_prev, can_next):
        self._btn_prev_file.setEnabled(can_prev)
        self._btn_next_file.setEnabled(can_next)

    def scroll_to_page(self, page_idx):
        y = self._pages_widget.page_y_offset(page_idx)
        self._scroll.verticalScrollBar().setValue(max(0, y - 4))

    def scroll_to_bbox(self, page_idx, bbox_pdf=None):
        if not bbox_pdf or len(bbox_pdf) < 4:
            self.scroll_to_page(page_idx)
            return
        page_y = self._pages_widget.page_y_offset(page_idx)
        _, y_scale, _, rect_y0 = self._page_view_transform(page_idx)
        target_y = page_y + int((float(bbox_pdf[1]) - rect_y0) * y_scale) - 96
        self._scroll.verticalScrollBar().setValue(max(0, target_y))

    def show_pdf(self, pdf_path, page=1, bbox=None, bboxes=None,
                 highlight_style="box", page_bboxes=None):
        """Repository-compatible API: load a PDF, jump to page, and draw
        exact lexical highlights or broader semantic/chunk boxes."""
        path = str(pdf_path)
        if path != self._pdf_path:
            self.load_pdf(path)
        boxes = [bb for bb in (bboxes or []) if bb and len(bb) >= 4]
        if not boxes and bbox and len(bbox) >= 4:
            boxes = [bbox]
        page_idx = max(0, int(page or 1) - 1)
        page_boxes = self._normalize_page_bboxes(page_bboxes)
        if page_boxes:
            self.highlight_page_regions(page_boxes, highlight_style)
        elif boxes:
            self.highlight_regions(page_idx, boxes, highlight_style)
        else:
            self.clear_highlight()
        focus_bbox = boxes[0] if boxes else next(
            (bb for pi, bb in page_boxes if pi == page_idx),
            page_boxes[0][1] if page_boxes else None,
        )
        if self._pages_widget.page_count() == 0:
            self._pending_view_scroll = (page_idx, focus_bbox)
            return
        self._pending_view_scroll = None
        if focus_bbox:
            self.scroll_to_bbox(page_idx, focus_bbox)
        else:
            self.scroll_to_page(page_idx)

    def highlight_zone(self, page_idx, bbox_pdf):
        if not self._doc or not bbox_pdf:
            return
        if page_idx < 0 or page_idx >= self._page_count:
            return
        x0, y0, w, h = self._pdf_bbox_to_view_rect(page_idx, bbox_pdf)
        self._pages_widget.set_zone(page_idx, (x0, y0, w, h))
        page_y = self._pages_widget.page_y_offset(page_idx)
        target_y = page_y + int(y0) - 40
        self._scroll.verticalScrollBar().setValue(max(0, target_y))

    def clear_highlight(self):
        self._pages_widget.clear_zone()
        self._current_search_highlight = None
        self._pages_widget.clear_search_highlight()

    def highlight_regions(self, page_idx, bboxes_pdf, style="box"):
        boxes = [list(bb[:4]) for bb in (bboxes_pdf or []) if bb and len(bb) >= 4]
        self._current_search_highlight = (int(page_idx), boxes, style or "box")
        self._reapply_search_highlight()

    def highlight_page_regions(self, page_bboxes, style="box"):
        grouped: dict[int, list[list[float]]] = {}
        for page_idx, bbox in self._normalize_page_bboxes(page_bboxes):
            grouped.setdefault(int(page_idx), []).append(list(bbox[:4]))
        self._current_search_highlight = [
            (page_idx, boxes, style or "box")
            for page_idx, boxes in sorted(grouped.items())
            if boxes
        ]
        self._reapply_search_highlight()

    @staticmethod
    def _normalize_page_bboxes(page_bboxes):
        out = []
        for item in page_bboxes or []:
            page = None
            bbox = None
            if isinstance(item, dict):
                page = item.get("page_idx", item.get("page"))
                bbox = item.get("bbox")
            else:
                try:
                    page, bbox = item
                except (TypeError, ValueError):
                    continue
            if bbox and len(bbox) >= 4:
                try:
                    page_idx = int(page)
                except (TypeError, ValueError):
                    continue
                out.append((page_idx, [float(v) for v in bbox[:4]]))
        return out

    def _apply_pending_view_scroll(self):
        pending = self._pending_view_scroll
        if not pending or self._pages_widget.page_count() == 0:
            return
        self._pending_view_scroll = None
        page_idx, focus_bbox = pending

        def _scroll():
            if focus_bbox:
                self.scroll_to_bbox(page_idx, focus_bbox)
            else:
                self.scroll_to_page(page_idx)

        QTimer.singleShot(0, _scroll)

    def _reapply_search_highlight(self):
        if not self._current_search_highlight or self._pages_widget.page_count() == 0:
            self._pages_widget.clear_search_highlight()
            return
        current = self._current_search_highlight
        if isinstance(current, tuple) and len(current) == 3:
            entries = [current]
        else:
            entries = list(current or [])
        highlights = []
        for page_idx, boxes, style in entries:
            rects = []
            for box in boxes:
                rect = self._pdf_bbox_to_view_rect(page_idx, box)
                rects.append(self._pad_search_highlight_rect(page_idx, rect, style))
            if rects:
                highlights.append((page_idx, rects, style))
        self._pages_widget.set_search_highlights(highlights)

    # ── KIE field overlays (multiple bboxes, one per field) ─────────

    def _pad_search_highlight_rect(self, page_idx: int, rect, style: str):
        if (style or "") != "highlight" or not rect:
            return rect
        x, y, w, h = (float(v) for v in rect[:4])
        pad_y = max(2.5, min(8.0, h * 0.35))
        new_y = max(0.0, y - pad_y)
        new_h = h + (y - new_y) + pad_y
        if 0 <= page_idx < self._pages_widget.page_count():
            page_h = float(self._pages_widget.page_size(page_idx).height())
            new_h = min(new_h, max(1.0, page_h - new_y))
        return x, new_y, w, max(1.0, new_h)

    def set_field_overlays(self, fields):
        """Display KIE field bboxes on the rendered pages.

        `fields` is a list of dicts with keys:
          page_index, bbox (PDF points: x0,y0,x1,y1), label,
          is_selected (optional bool), color (optional hex string).

        Coordinates are converted from PDF points to pixel space using the
        current render scale. Stored so they survive zoom re-renders."""
        self._current_field_overlays = list(fields or [])
        if not self._doc:
            self._pages_widget.set_overlays([])
            return
        overlays = []
        for f in fields or []:
            bbox = f.get("bbox")
            page_idx = int(f.get("page_index", 0))
            if not bbox or len(bbox) < 4:
                continue
            label = f.get("label") or ""
            color = f.get("color") or _LABEL_COLORS.get(label, COLOR_ACCENT)
            is_selected = bool(f.get("is_selected"))
            zx, zy, zw, zh = self._pdf_bbox_to_view_rect(page_idx, bbox)
            overlays.append((page_idx, zx, zy, zw, zh, color, label, is_selected))
        self._pages_widget.set_overlays(overlays)

    def clear_field_overlays(self):
        self._current_field_overlays = []
        self._pages_widget.clear_overlays()

    # ── Fuzzy match overlays (transient, clickable) ─────────────────

    fuzzy_match_picked = Signal(str, list)  # text, bbox_pdf

    def set_fuzzy_matches(self, matches: list):
        """Display ranked fuzzy-match candidates as cyan clickable overlays.

        `matches` is the list returned by `archive_fuzzy.fuzzy_rank` —
        each item has keys: text, bbox (PDF points), page_index, score."""
        self._current_fuzzy_matches = list(matches or [])
        self._reapply_fuzzy_matches()

    def clear_fuzzy_matches(self):
        self._current_fuzzy_matches = []
        self._pages_widget.clear_fuzzy_overlays()

    def _reapply_fuzzy_matches(self):
        if not getattr(self, "_current_fuzzy_matches", None) or not self._doc:
            self._pages_widget.clear_fuzzy_overlays()
            return
        overlays = []
        for rank, m in enumerate(self._current_fuzzy_matches):
            bbox = m.get("bbox")
            if not bbox or len(bbox) < 4:
                continue
            page_idx = int(m.get("page_index", 0))
            x, y, w, h = self._pdf_bbox_to_view_rect(page_idx, bbox)
            overlays.append({
                "page_idx": page_idx,
                "x": x, "y": y,
                "w": w, "h": h,
                "text": m.get("text", ""),
                "score": float(m.get("score", 0)),
                "rank": rank,
                "bbox_pdf": list(bbox),
            })
        self._pages_widget.set_fuzzy_overlays(overlays)
        # Forward click events from the inner widget
        try:
            self._pages_widget.fuzzy_clicked.disconnect()
        except Exception:
            pass
        self._pages_widget.fuzzy_clicked.connect(self._on_fuzzy_clicked)

    def _on_fuzzy_clicked(self, page_idx, text, bbox_pdf):
        self.fuzzy_match_picked.emit(text, bbox_pdf or [])

    def clear(self):
        self._render_gen += 1
        self._hires_timer.stop()
        self._scroll_timer.stop()
        self._close_doc()
        self._raw_pixmaps = {}
        self._page_point_sizes = []
        self._render_queue = []
        self._render_queued = set()
        self._render_active = False
        self._render_active_gen = None
        self._current_field_overlays = []
        self._selection_words_by_page = None
        self._clear_text_selection()
        self._pages_widget.clear_pages()
        self._hint_label.setText(translations.get_text("arc_no_preview"))
        self._hint_label.setVisible(True)
        self._update_nav_state()
        self._update_zoom_label()
        self._lbl_page.setText("")

    def release_file_handles(self, timeout: float = 5.0) -> bool:
        """Close the active PDF and wait briefly for background renders.

        Windows prevents repository rename operations while either the main
        viewer or an asynchronous render worker still has the PDF open.
        """
        self.clear()
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            with self._render_threads_lock:
                threads = list(self._active_render_threads)
            if not threads:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            threads[0].join(min(0.05, remaining))

    def update_texts(self):
        if self._doc is None:
            self._hint_label.setText(translations.get_text("arc_no_preview"))
        self._update_nav_state()

    def set_text_selection_enabled(self, enabled: bool):
        enabled = bool(enabled and self._text_selection_available)
        self._text_selection_enabled = enabled
        if hasattr(self, "_btn_select_text") and self._btn_select_text.isChecked() != enabled:
            self._btn_select_text.blockSignals(True)
            self._btn_select_text.setChecked(enabled)
            self._btn_select_text.blockSignals(False)
        self._pages_widget.set_text_selection_enabled(enabled)
        self._scroll.set_text_selection_enabled(enabled)
        if enabled:
            self._pages_widget.setFocus(Qt.FocusReason.MouseFocusReason)
        else:
            self._clear_text_selection()

    def copy_selected_text(self):
        text = self._selected_text.strip()
        if not text:
            return False
        clipboard = QApplication.clipboard()
        if clipboard is None:
            return False
        clipboard.setText(text)
        return True

    def _clear_text_selection(self):
        self._selected_words = []
        self._selected_text = ""
        if hasattr(self, "_pages_widget"):
            self._pages_widget.clear_text_selection_rects()

    def _current_pdf_to_view_scale(self) -> float:
        return float(self.RENDER_DPI) / 72.0 * float(self._zoom or 1.0)

    def _page_view_transform(self, page_idx: int):
        fallback = self._current_pdf_to_view_scale()
        try:
            page = self._doc[int(page_idx)] if self._doc is not None else None
            size = self._pages_widget.page_size(int(page_idx))
            rect = page.rect if page is not None else None
            if size.isEmpty() or rect is None:
                return fallback, fallback, 0.0, 0.0
            rect_w = max(1.0, float(rect.width))
            rect_h = max(1.0, float(rect.height))
            return (
                float(size.width()) / rect_w,
                float(size.height()) / rect_h,
                float(rect.x0),
                float(rect.y0),
            )
        except Exception:
            return fallback, fallback, 0.0, 0.0

    def _pdf_bbox_to_view_rect(self, page_idx: int, bbox_pdf) -> tuple[float, float, float, float]:
        if not bbox_pdf or len(bbox_pdf) < 4:
            return 0.0, 0.0, 0.0, 0.0
        x_scale, y_scale, rect_x0, rect_y0 = self._page_view_transform(page_idx)
        x0, y0, x1, y1 = (float(v) for v in bbox_pdf[:4])
        return (
            (x0 - rect_x0) * x_scale,
            (y0 - rect_y0) * y_scale,
            max(1.0, (x1 - x0) * x_scale),
            max(1.0, (y1 - y0) * y_scale),
        )

    @staticmethod
    def _bbox_from_word_record(word: dict):
        box = word.get("bbox")
        if isinstance(box, (list, tuple)) and len(box) >= 4:
            try:
                x0, y0, x1, y1 = (float(v) for v in box[:4])
            except (TypeError, ValueError):
                return None
            if x1 > x0 and y1 > y0:
                return [x0, y0, x1, y1]
        try:
            x = float(word.get("x", 0.0) or 0.0)
            y = float(word.get("y", 0.0) or 0.0)
            w = float(word.get("w", 0.0) or 0.0)
            h = float(word.get("h", 0.0) or 0.0)
        except (TypeError, ValueError):
            return None
        if w <= 0 or h <= 0:
            return None
        return [x, y, x + w, y + h]

    def _selection_words_from_companion(self) -> dict[int, list[dict]]:
        if not self._pdf_path:
            return {}
        try:
            from scanindex.core.canonical_io import load_canonical, resolve_companion
            companion = resolve_companion(self._pdf_path)
            if companion is None:
                return {}
            data = load_canonical(companion)
        except Exception:
            return {}

        words_by_page: dict[int, list[dict]] = {}
        pages = data.get("pages") or []
        if not isinstance(pages, list):
            return {}
        for fallback_page_idx, page in enumerate(pages):
            if not isinstance(page, dict):
                continue
            try:
                page_idx = int(page.get("page_index", fallback_page_idx))
            except Exception:
                page_idx = fallback_page_idx
            line_by_id = {}
            line_by_order = {}
            for line_order, line in enumerate(page.get("lines") or []):
                if not isinstance(line, dict):
                    continue
                raw_line_order = line.get("order", line.get("line_index", line_order))
                try:
                    line_idx = int(raw_line_order or 0)
                except (TypeError, ValueError):
                    line_idx = line_order
                try:
                    block_id = int(line.get("block_id", 0) or 0)
                except (TypeError, ValueError):
                    block_id = 0
                paragraph_id = line.get("paragraph_id")
                if paragraph_id is None:
                    paragraph_id = block_id
                meta = {
                    "block": block_id,
                    "paragraph": str(paragraph_id),
                    "line": line_idx,
                    "line_id": str(line.get("id") or line.get("line_id") or ""),
                }
                if meta["line_id"]:
                    line_by_id[meta["line_id"]] = meta
                line_by_order[line_idx] = meta
            page_words = []
            for order, word in enumerate(page.get("words") or []):
                if not isinstance(word, dict):
                    continue
                text = str(word.get("text") or word.get("ocr_text") or "")
                if not text.strip():
                    continue
                bbox = self._bbox_from_word_record(word)
                if not bbox:
                    continue
                line_id = str(word.get("line_id") or "")
                line_meta = line_by_id.get(line_id)
                if line_meta is None:
                    try:
                        line_idx = int(word.get("line_index", 0) or 0)
                    except (TypeError, ValueError):
                        line_idx = 0
                    line_meta = line_by_order.get(line_idx, {})
                try:
                    block_id = int(word.get("block_id", line_meta.get("block", 0)) or 0)
                except (TypeError, ValueError):
                    block_id = int(line_meta.get("block", 0) or 0)
                paragraph_id = word.get("paragraph_id", line_meta.get("paragraph"))
                if paragraph_id is None:
                    paragraph_id = block_id
                try:
                    line_idx = int(word.get("line_index", line_meta.get("line", 0)) or 0)
                except (TypeError, ValueError):
                    line_idx = int(line_meta.get("line", 0) or 0)
                page_words.append({
                    "page_idx": page_idx,
                    "bbox": bbox,
                    "text": text,
                    "block": block_id,
                    "paragraph": str(paragraph_id),
                    "line": line_idx,
                    "line_id": line_id or str(line_meta.get("line_id") or ""),
                    "word": int(
                        word.get("word_index", word.get("order", order)) or order
                    ),
                    "order": order,
                    "has_space_after": bool(word.get("has_space_after", True)),
                })
            if page_words:
                words_by_page[page_idx] = page_words
        return words_by_page

    def _ensure_selection_words_loaded(self) -> dict[int, list[dict]]:
        if self._selection_words_by_page is not None:
            return self._selection_words_by_page
        companion_words = self._selection_words_from_companion()
        if companion_words:
            self._selection_words_by_page = companion_words
            return companion_words
        words_by_page: dict[int, list[dict]] = {}
        if self._doc is None:
            self._selection_words_by_page = words_by_page
            return words_by_page
        for page_idx in range(self._page_count):
            page_words = []
            try:
                raw_words = self._doc[page_idx].get_text("words") or []
            except Exception:
                raw_words = []
            for order, item in enumerate(raw_words):
                if len(item) < 5:
                    continue
                text = str(item[4] or "")
                if not text.strip():
                    continue
                try:
                    x0, y0, x1, y1 = (float(item[i]) for i in range(4))
                except Exception:
                    continue
                if x1 <= x0 or y1 <= y0:
                    continue
                page_words.append({
                    "page_idx": page_idx,
                    "bbox": [x0, y0, x1, y1],
                    "text": text,
                    "block": int(item[5]) if len(item) > 5 else 0,
                    "paragraph": str(int(item[5]) if len(item) > 5 else 0),
                    "line": int(item[6]) if len(item) > 6 else 0,
                    "line_id": "",
                    "word": int(item[7]) if len(item) > 7 else order,
                    "order": order,
                    "has_space_after": True,
                })
            words_by_page[page_idx] = page_words
        self._selection_words_by_page = words_by_page
        return words_by_page

    @staticmethod
    def _pdf_rect_intersects(a: list[float], b: tuple[float, float, float, float]) -> bool:
        ax0, ay0, ax1, ay1 = (float(v) for v in a)
        bx0, by0, bx1, by1 = b
        return min(ax1, bx1) > max(ax0, bx0) and min(ay1, by1) > max(ay0, by0)

    def _words_in_content_rect(self, start: QPoint, end: QPoint) -> list[dict]:
        if self._doc is None or not self._pages_widget.page_count():
            return []
        selection_rect = QRectF(start, end).normalized()
        if selection_rect.width() < 3 and selection_rect.height() < 3:
            return []

        widget_w = float(self._pages_widget.width())
        words_by_page = self._ensure_selection_words_loaded()
        selected: list[dict] = []
        for page_idx in range(self._pages_widget.page_count()):
            size = self._pages_widget.page_size(page_idx)
            page_x0 = (widget_w - float(size.width())) / 2.0
            page_y0 = float(self._pages_widget.page_y_offset(page_idx))
            page_rect = QRectF(page_x0, page_y0, float(size.width()), float(size.height()))
            if not selection_rect.intersects(page_rect):
                continue
            page_selection = selection_rect.intersected(page_rect).translated(
                -page_x0,
                -page_y0,
            )
            for word in words_by_page.get(page_idx, []):
                wx, wy, ww, wh = self._pdf_bbox_to_view_rect(page_idx, word["bbox"])
                if page_selection.intersects(QRectF(wx, wy, ww, wh)):
                    selected.append(word)
        return self._visual_order_words(selected)

    @staticmethod
    def _clipboard_word_text(text: str) -> str:
        text = str(text or "").replace("\xa0", " ").strip()
        return "-" if text == "\xad" else text.replace("\xad", "-")

    @staticmethod
    def _word_bbox(word: dict) -> list[float]:
        bbox = word.get("bbox") or [0.0, 0.0, 0.0, 0.0]
        try:
            return [float(v) for v in bbox[:4]]
        except (TypeError, ValueError):
            return [0.0, 0.0, 0.0, 0.0]

    @staticmethod
    def _word_center_y(word: dict) -> float:
        bbox = PdfViewerWidget._word_bbox(word)
        return (bbox[1] + bbox[3]) / 2.0

    @staticmethod
    def _word_height(word: dict) -> float:
        bbox = PdfViewerWidget._word_bbox(word)
        return max(1.0, bbox[3] - bbox[1])

    @staticmethod
    def _word_left(word: dict) -> float:
        return PdfViewerWidget._word_bbox(word)[0]

    @staticmethod
    def _visual_line_groups(words: list[dict]) -> list[list[dict]]:
        if not words:
            return []
        ordered = sorted(
            words,
            key=lambda w: (
                int(w.get("page_idx", 0)),
                PdfViewerWidget._word_center_y(w),
                PdfViewerWidget._word_left(w),
                int(w.get("block", 0)),
                int(w.get("line", 0)),
                int(w.get("word", 0)),
                int(w.get("order", 0)),
            ),
        )
        groups: list[dict] = []
        for word in ordered:
            page_idx = int(word.get("page_idx", 0))
            center_y = PdfViewerWidget._word_center_y(word)
            height = PdfViewerWidget._word_height(word)
            if groups and groups[-1]["page_idx"] == page_idx:
                prev = groups[-1]
                tol = max(3.0, max(prev["height"], height) * 0.70)
                if abs(center_y - prev["center_y"]) <= tol:
                    count = len(prev["words"])
                    prev["center_y"] = (prev["center_y"] * count + center_y) / (count + 1)
                    prev["height"] = max(prev["height"], height)
                    prev["words"].append(word)
                    continue
            groups.append({
                "page_idx": page_idx,
                "center_y": center_y,
                "height": height,
                "words": [word],
            })
        return [
            sorted(
                group["words"],
                key=lambda w: (
                    PdfViewerWidget._word_left(w),
                    int(w.get("word", 0)),
                    int(w.get("order", 0)),
                ),
            )
            for group in groups
        ]

    @staticmethod
    def _visual_order_words(words: list[dict]) -> list[dict]:
        return [
            word
            for group in PdfViewerWidget._visual_line_groups(words)
            for word in group
        ]

    @staticmethod
    def _clipboard_text_from_line_words(words: list[dict]) -> str:
        parts: list[str] = []
        for word in words:
            text = PdfViewerWidget._clipboard_word_text(word.get("text") or "")
            if not text:
                continue
            parts.append(text)
            if word.get("has_space_after", True):
                parts.append(" ")
        text = "".join(parts).strip()
        text = re.sub(r"\s+", " ", text)
        text = re.sub(r"\s+([,.;:!?%)])", r"\1", text)
        text = re.sub(r"([(])\s+", r"\1", text)
        return text.strip()

    @staticmethod
    def _selection_lines_from_words(words: list[dict]) -> list[dict]:
        if not words:
            return []
        lines: list[dict] = []

        for line_words in PdfViewerWidget._visual_line_groups(words):
            if not line_words:
                continue
            text = PdfViewerWidget._clipboard_text_from_line_words(line_words)
            boxes = [PdfViewerWidget._word_bbox(w) for w in line_words]
            x0 = min(float(b[0]) for b in boxes)
            y0 = min(float(b[1]) for b in boxes)
            x1 = max(float(b[2]) for b in boxes)
            y1 = max(float(b[3]) for b in boxes)
            first = line_words[0]
            lines.append({
                "page_idx": int(first.get("page_idx", 0)),
                "block": int(first.get("block", 0)),
                "paragraph": str(first.get("paragraph", first.get("block", 0))),
                "line": int(first.get("line", 0)),
                "line_id": str(first.get("line_id", "")),
                "text": text,
                "bbox": [x0, y0, x1, y1],
                "height": max(1.0, y1 - y0),
            })
        return [line for line in lines if line.get("text")]

    @staticmethod
    def _selection_line_separator(prev: dict, cur: dict) -> str:
        if int(prev.get("page_idx", 0)) != int(cur.get("page_idx", 0)):
            return "\n\n"
        if str(prev.get("paragraph")) != str(cur.get("paragraph")):
            return "\n"
        if int(prev.get("block", 0)) != int(cur.get("block", 0)):
            return "\n"
        return " "

    def _selection_text_from_words(self, words: list[dict]) -> str:
        if not words:
            return ""
        lines = self._selection_lines_from_words(words)
        if not lines:
            return ""
        out = [str(lines[0].get("text") or "")]
        for prev, cur in zip(lines, lines[1:]):
            out.append(self._selection_line_separator(prev, cur))
            out.append(str(cur.get("text") or ""))
        return "".join(out)

    def _reapply_text_selection(self):
        if not self._selected_words:
            self._pages_widget.clear_text_selection_rects()
            return
        rects = []
        for word in self._selected_words:
            page_idx = int(word.get("page_idx", 0))
            rects.append((page_idx, *self._pdf_bbox_to_view_rect(page_idx, word["bbox"])))
        self._pages_widget.set_text_selection_rects(rects)

    def _select_text_between_points(self, start: QPoint, end: QPoint):
        self._selected_words = self._words_in_content_rect(start, end)
        self._selected_text = self._selection_text_from_words(self._selected_words)
        self._reapply_text_selection()

    def _on_text_selection_dragged(self, start: QPoint, end: QPoint):
        if self._text_selection_enabled:
            self._select_text_between_points(start, end)

    def _on_text_selection_finished(self, start: QPoint, end: QPoint):
        if self._text_selection_enabled:
            self._select_text_between_points(start, end)

    # ------ Zoom (anchor to cursor) ------

    def _on_zoom_at_pos(self, direction, viewport_pos):
        """Zoom in/out anchored at the cursor position in the viewport."""
        if not self._page_point_sizes:
            return

        old_zoom = self._zoom
        # Find new zoom level
        if direction > 0:
            new_zoom = None
            for z in self.ZOOM_STEPS:
                if z > old_zoom + 0.01:
                    new_zoom = z
                    break
            if new_zoom is None:
                return
        else:
            new_zoom = None
            for z in reversed(self.ZOOM_STEPS):
                if z < old_zoom - 0.01:
                    new_zoom = z
                    break
            if new_zoom is None:
                return

        # Get scroll position + cursor pos relative to content
        hbar = self._scroll.horizontalScrollBar()
        vbar = self._scroll.verticalScrollBar()
        # Content coordinate under cursor before zoom
        content_x = hbar.value() + viewport_pos.x()
        content_y = vbar.value() + viewport_pos.y()

        # Scale factor
        ratio = new_zoom / old_zoom

        # Apply zoom
        self._fit_mode = False
        self._zoom = new_zoom
        self._rebuild_scaled_pages()

        # Adjust scroll so the same content point stays under cursor
        new_hval = int(content_x * ratio) - viewport_pos.x()
        new_vval = int(content_y * ratio) - viewport_pos.y()
        hbar.setValue(max(0, new_hval))
        vbar.setValue(max(0, new_vval))

    def _zoom_in(self):
        self._fit_mode = False
        for z in self.ZOOM_STEPS:
            if z > self._zoom + 0.01:
                self._set_zoom(z)
                return

    def _zoom_out(self):
        self._fit_mode = False
        for z in reversed(self.ZOOM_STEPS):
            if z < self._zoom - 0.01:
                self._set_zoom(z)
                return

    def _zoom_fit(self):
        self._fit_mode = True
        self._rebuild_scaled_pages()

    def _set_zoom(self, z):
        self._zoom = z
        self._rebuild_scaled_pages()

    def _display_page_sizes(self):
        scale = self._current_pdf_to_view_scale()
        return [
            QSize(
                max(1, int(round(width * scale))),
                max(1, int(round(height * scale))),
            )
            for width, height in self._page_point_sizes
        ]

    def _rebuild_scaled_pages(self, *, reset_generation=True):
        if not self._page_point_sizes:
            return
        if self._fit_mode:
            avail_w = self._scroll.viewport().width() - 12
            if avail_w <= 0:
                avail_w = 600
            max_raw_w = max(
                width * (self.RENDER_DPI / 72.0)
                for width, _height in self._page_point_sizes
            )
            self._zoom = avail_w / max_raw_w if max_raw_w > 0 else 1.0

        if reset_generation:
            self._render_gen += 1
        self._raw_pixmaps = {}
        self._render_queue = []
        self._render_queued = set()
        self._pages_widget.set_page_sizes(self._display_page_sizes())
        if getattr(self, "_current_field_overlays", None):
            self.set_field_overlays(self._current_field_overlays)
        self._reapply_search_highlight()
        if getattr(self, "_current_fuzzy_matches", None):
            self._reapply_fuzzy_matches()
        self._reapply_text_selection()
        self._update_zoom_label()
        self._hires_timer.stop()
        QTimer.singleShot(0, self._enqueue_viewport_pages)

    def _update_zoom_label(self):
        self._lbl_zoom.setText(f"{int(self._zoom * 100)}%")

    # ------ Scroll tracking ------

    def _on_scroll(self, value):
        if self._pages_widget.page_count() == 0:
            return
        for i in range(self._pages_widget.page_count() - 1, -1, -1):
            if value >= self._pages_widget.page_y_offset(i) - 20:
                self._lbl_page.setText(f"{i + 1} / {self._page_count}")
                break
        self._scroll_timer.start()

    def _scroll_debounce_fire(self):
        self._enqueue_viewport_pages()
        self._evict_far_pixmaps()

    # ------ Internal ------

    def _close_doc(self):
        if self._doc:
            self._doc.close()
        self._doc = None
        self._page_count = 0
        self._pdf_path = None
        self._page_point_sizes = []

    def _visible_page_indices(self, margin_pages=1):
        count = self._pages_widget.page_count()
        if count <= 0:
            return []
        top = self._scroll.verticalScrollBar().value()
        bottom = top + max(1, self._scroll.viewport().height())
        visible = []
        for i in range(count):
            y = self._pages_widget.page_y_offset(i)
            h = self._pages_widget.page_size(i).height()
            if y + h < top:
                continue
            if y > bottom:
                if visible:
                    break
                continue
            visible.append(i)
        if not visible:
            current = self._current_page_index()
            visible = [current] if current is not None else [0]
        start = max(0, min(visible) - margin_pages)
        end = min(count, max(visible) + margin_pages + 1)
        return list(range(start, end))

    def _current_page_index(self):
        count = self._pages_widget.page_count()
        if count <= 0:
            return None
        value = self._scroll.verticalScrollBar().value()
        current = 0
        for i in range(count):
            if value >= self._pages_widget.page_y_offset(i) - 20:
                current = i
            else:
                break
        return current

    def _enqueue_viewport_pages(self):
        if not self._pdf_path or self._pages_widget.page_count() == 0:
            return
        rendered = self._pages_widget.rendered_page_indices()
        for page_idx in self._visible_page_indices(margin_pages=2):
            if page_idx in rendered or page_idx in self._render_queued:
                continue
            self._render_queue.append(page_idx)
            self._render_queued.add(page_idx)
        self._drain_render_queue()

    def _drain_render_queue(self):
        if self._render_active or not self._render_queue or not self._pdf_path:
            return
        page_idx = self._render_queue.pop(0)
        self._render_queued.discard(page_idx)
        self._render_active = True
        path = self._pdf_path
        gen = self._render_gen
        self._render_active_gen = gen
        dpi = max(24.0, float(self.RENDER_DPI) * float(self._zoom or 1.0))
        signal = self._pages_rendered

        def _worker():
            try:
                import fitz
                with fitz.open(path) as doc:
                    if not (0 <= page_idx < len(doc)):
                        signal.emit(("error", page_idx, "page out of range", gen))
                        return
                    mat = fitz.Matrix(dpi / 72.0, dpi / 72.0)
                    pix = doc[page_idx].get_pixmap(matrix=mat, alpha=False, annots=True)
                    image = QImage(
                        pix.samples, pix.width, pix.height,
                        pix.stride, QImage.Format.Format_RGB888,
                    ).copy()
                signal.emit(("page", page_idx, image, gen))
            except Exception as e:
                signal.emit(("error", page_idx, str(e), gen))
            finally:
                with self._render_threads_lock:
                    self._active_render_threads.discard(threading.current_thread())

        worker = threading.Thread(target=_worker, daemon=True)
        with self._render_threads_lock:
            self._active_render_threads.add(worker)
        worker.start()

    def _on_pages_rendered(self, data):
        if not isinstance(data, tuple) or len(data) < 4:
            return
        kind, page_idx, payload, gen = data
        if gen == self._render_active_gen:
            self._render_active = False
            self._render_active_gen = None
        if gen != self._render_gen:
            self._drain_render_queue()
            return
        if kind == "page":
            pixmap = QPixmap.fromImage(payload)
            target_size = self._pages_widget.page_size(int(page_idx))
            if not target_size.isEmpty() and pixmap.size() != target_size:
                pixmap = pixmap.scaled(
                    target_size,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            self._raw_pixmaps[int(page_idx)] = pixmap
            self._pages_widget.set_page_pixmap(int(page_idx), pixmap)
            self._apply_pending_view_scroll()
            self._evict_far_pixmaps()
        self._drain_render_queue()

    def _evict_far_pixmaps(self):
        rendered = self._pages_widget.rendered_page_indices()
        if len(rendered) <= self.RENDER_CACHE_MAX_PAGES:
            return
        current = self._current_page_index()
        if current is None:
            current = 0
        keep = set(self._visible_page_indices(margin_pages=2))
        ordered = sorted(rendered, key=lambda idx: (idx in keep, -abs(idx - current)))
        while len(rendered) > self.RENDER_CACHE_MAX_PAGES and ordered:
            idx = ordered.pop(0)
            if idx in keep and len(rendered) <= len(keep):
                break
            rendered.discard(idx)
            self._raw_pixmaps.pop(idx, None)
            self._pages_widget.release_page_pixmap(idx)

    def _start_hires_render(self):
        self._enqueue_viewport_pages()

    def _update_nav_state(self):
        has = self._doc is not None
        self._lbl_page.setText(f"1 / {self._page_count}" if has else "")
