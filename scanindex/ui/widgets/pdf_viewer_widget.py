"""
PDF Viewer Widget — Continuous-scroll viewer with:
- Ctrl+wheel zoom anchored to cursor position
- Middle-click or left-click drag to pan
- All pages stacked vertically, smooth scroll
"""
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QScrollArea, QLabel,
    QPushButton, QSizePolicy, QFrame, QApplication
)
from PySide6.QtCore import Qt, Signal, QRect, QTimer, QSize, QPoint
from PySide6.QtGui import (
    QPixmap, QImage, QPainter, QColor, QPen, QWheelEvent,
    QMouseEvent, QCursor
)

import threading

from scanindex.ui.theme import (
    COLOR_BG, COLOR_SURFACE, COLOR_ELEVATED, COLOR_HOVER,
    COLOR_BORDER, COLOR_BORDER_DEFAULT,
    COLOR_TEXT, COLOR_TEXT_SECONDARY, COLOR_TEXT_MUTED, COLOR_ACCENT,
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

    def __init__(self, parent=None):
        super().__init__(parent)
        self._page_pixmaps = []
        self._page_y_offsets = []
        self._zone = None  # (page_idx, x, y, w, h)
        self._zone_color = QColor(COLOR_ACCENT)
        self._overlays: list[tuple] = []
        self._search_highlight = None  # (page_idx, [(x, y, w, h), ...], style)
        # Fuzzy match overlays — drawn in a distinct color, clickable
        self._fuzzy_overlays: list[dict] = []

    def set_pages(self, pixmaps):
        self._page_pixmaps = pixmaps
        self._zone = None
        self._overlays = []
        self._recalc_offsets()
        self.update()

    def clear_pages(self):
        self._page_pixmaps = []
        self._page_y_offsets = []
        self._zone = None
        self._overlays = []
        self._search_highlight = None
        self.setFixedSize(0, 0)
        self.update()

    def set_zone(self, page_idx, rect):
        self._zone = (page_idx, *rect)
        self.update()

    def clear_zone(self):
        self._zone = None
        self.update()

    def set_search_highlight(self, page_idx, rects, style="box"):
        self._search_highlight = (page_idx, list(rects or []), style or "box")
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

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._fuzzy_overlays:
            click_pos = event.position().toPoint()
            for ov in self._fuzzy_overlays:
                if not (0 <= ov["page_idx"] < len(self._page_y_offsets)):
                    continue
                y_off = self._page_y_offsets[ov["page_idx"]]
                pm = self._page_pixmaps[ov["page_idx"]]
                x_off = (self.width() - pm.width()) // 2
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

    def page_y_offset(self, page_idx):
        if 0 <= page_idx < len(self._page_y_offsets):
            return self._page_y_offsets[page_idx]
        return 0

    def page_count(self):
        return len(self._page_pixmaps)

    def _recalc_offsets(self):
        self._page_y_offsets = []
        y = 0
        max_w = 0
        for pm in self._page_pixmaps:
            self._page_y_offsets.append(y)
            y += pm.height() + _PAGE_GAP
            max_w = max(max_w, pm.width())
        total_h = y - _PAGE_GAP if self._page_pixmaps else 0
        self.setFixedSize(max(max_w, 1), max(total_h, 1))

    def paintEvent(self, event):
        if not self._page_pixmaps:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        visible = event.rect()
        widget_w = self.width()

        for i, pm in enumerate(self._page_pixmaps):
            y_off = self._page_y_offsets[i]
            if y_off + pm.height() < visible.top() or y_off > visible.bottom():
                continue
            x_off = (widget_w - pm.width()) // 2
            painter.drawPixmap(x_off, y_off, pm)

        # Persistent overlays (KIE field bboxes)
        for overlay in self._overlays:
            try:
                pi, zx, zy, zw, zh, color_hex, label, is_selected = overlay
            except (ValueError, TypeError):
                continue
            if not (0 <= pi < len(self._page_y_offsets)):
                continue
            y_off = self._page_y_offsets[pi]
            pm = self._page_pixmaps[pi]
            x_off = (widget_w - pm.width()) // 2
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
            pm = self._page_pixmaps[pi]
            x_off = (widget_w - pm.width()) // 2
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

        # Search result highlight. Exact/fuzzy lexical matches use a thin
        # underline; semantic/body chunk matches keep the broader box.
        if self._search_highlight:
            pi, rects, style = self._search_highlight
            if 0 <= pi < len(self._page_y_offsets):
                y_off = self._page_y_offsets[pi]
                pm = self._page_pixmaps[pi]
                x_off = (widget_w - pm.width()) // 2
                color = QColor(COLOR_ACCENT)
                for zx, zy, zw, zh in rects:
                    rx, ry = int(x_off + zx), int(y_off + zy)
                    rw, rh = int(zw), int(zh)
                    if style == "underline":
                        pen = QPen(color, 2)
                        painter.setPen(pen)
                        y = int(ry + rh * 0.88)
                        painter.drawLine(rx, y, rx + rw, y)
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
                pm = self._page_pixmaps[pi]
                x_off = (widget_w - pm.width()) // 2
                rx, ry = int(x_off + zx), int(y_off + zy)
                rw, rh = int(zw), int(zh)
                fill = QColor(self._zone_color)
                fill.setAlpha(35)
                painter.fillRect(QRect(rx, ry, rw, rh), fill)
                pen = QPen(self._zone_color, 2)
                painter.setPen(pen)
                painter.drawRect(QRect(rx, ry, rw, rh))

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
        if event.button() in (Qt.MouseButton.LeftButton, Qt.MouseButton.MiddleButton):
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
            self.setCursor(Qt.CursorShape.OpenHandCursor)
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

    def __init__(self, parent=None, *, fit_on_load=True):
        super().__init__(parent)
        self._doc = None
        self._page_count = 0
        self._pdf_path = None
        self._raw_pixmaps = []
        self._zoom = 1.0
        self._fit_on_load = bool(fit_on_load)
        self._fit_mode = True
        self._file_label_text = ""
        self._render_gen = 0
        self._current_search_highlight = None
        self._pages_rendered.connect(self._on_pages_rendered)
        self._hires_timer = QTimer(self)
        self._hires_timer.setSingleShot(True)
        self._hires_timer.setInterval(300)
        self._hires_timer.timeout.connect(self._start_hires_render)
        self._setup_ui()

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
        if self._fit_mode and self._raw_pixmaps:
            self._rebuild_scaled_pages()

    # ------ Public API ------

    def load_pdf(self, pdf_path):
        self._close_doc()
        self._hires_timer.stop()
        self._raw_pixmaps = []
        self._pages_widget.clear_pages()
        self._current_search_highlight = None
        self._pages_widget.clear_search_highlight()
        try:
            import fitz
            self._doc = fitz.open(pdf_path)
            self._pdf_path = pdf_path
            self._page_count = len(self._doc)
            self._fit_mode = self._fit_on_load
            self._hint_label.setVisible(False)
            self._render_gen += 1
            self._bg_render(self.RENDER_DPI, self._render_gen, is_base=True)
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
        scale = self.RENDER_DPI / 72.0 * self._zoom
        page_y = self._pages_widget.page_y_offset(page_idx)
        target_y = page_y + int(float(bbox_pdf[1]) * scale) - 96
        self._scroll.verticalScrollBar().setValue(max(0, target_y))

    def show_pdf(self, pdf_path, page=1, bbox=None, bboxes=None, highlight_style="box"):
        """Repository-compatible API: load a PDF, jump to page, and draw
        exact lexical underlines or broader semantic/chunk boxes."""
        path = str(pdf_path)
        if path != self._pdf_path:
            self.load_pdf(path)
        boxes = [bb for bb in (bboxes or []) if bb and len(bb) >= 4]
        if not boxes and bbox and len(bbox) >= 4:
            boxes = [bbox]
        page_idx = max(0, int(page or 1) - 1)
        if boxes:
            self.highlight_regions(page_idx, boxes, highlight_style)
            self.scroll_to_bbox(page_idx, boxes[0])
        else:
            self.clear_highlight()
            self.scroll_to_page(page_idx)

    def highlight_zone(self, page_idx, bbox_pdf):
        if not self._doc or not bbox_pdf:
            return
        if page_idx < 0 or page_idx >= self._page_count:
            return
        scale = self.RENDER_DPI / 72.0 * self._zoom
        x0, y0 = bbox_pdf[0] * scale, bbox_pdf[1] * scale
        x1, y1 = bbox_pdf[2] * scale, bbox_pdf[3] * scale
        self._pages_widget.set_zone(page_idx, (x0, y0, x1 - x0, y1 - y0))
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

    def _reapply_search_highlight(self):
        if not self._current_search_highlight or not self._raw_pixmaps:
            self._pages_widget.clear_search_highlight()
            return
        page_idx, boxes, style = self._current_search_highlight
        scale = self.RENDER_DPI / 72.0 * self._zoom
        rects = []
        for x0, y0, x1, y1 in boxes:
            rects.append((
                float(x0) * scale,
                float(y0) * scale,
                (float(x1) - float(x0)) * scale,
                (float(y1) - float(y0)) * scale,
            ))
        self._pages_widget.set_search_highlight(page_idx, rects, style)

    # ── KIE field overlays (multiple bboxes, one per field) ─────────

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
        scale = self.RENDER_DPI / 72.0 * self._zoom
        overlays = []
        for f in fields or []:
            bbox = f.get("bbox")
            page_idx = int(f.get("page_index", 0))
            if not bbox or len(bbox) < 4:
                continue
            label = f.get("label") or ""
            color = f.get("color") or _LABEL_COLORS.get(label, COLOR_ACCENT)
            is_selected = bool(f.get("is_selected"))
            x0, y0, x1, y1 = bbox[0], bbox[1], bbox[2], bbox[3]
            zx, zy = x0 * scale, y0 * scale
            zw, zh = (x1 - x0) * scale, (y1 - y0) * scale
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
        scale = self.RENDER_DPI / 72.0 * self._zoom
        overlays = []
        for rank, m in enumerate(self._current_fuzzy_matches):
            bbox = m.get("bbox")
            if not bbox or len(bbox) < 4:
                continue
            x0, y0, x1, y1 = bbox[0], bbox[1], bbox[2], bbox[3]
            overlays.append({
                "page_idx": int(m.get("page_index", 0)),
                "x": x0 * scale, "y": y0 * scale,
                "w": (x1 - x0) * scale, "h": (y1 - y0) * scale,
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
        self._close_doc()
        self._raw_pixmaps = []
        self._current_field_overlays = []
        self._pages_widget.clear_pages()
        self._hint_label.setText(translations.get_text("arc_no_preview"))
        self._hint_label.setVisible(True)
        self._update_nav_state()
        self._update_zoom_label()
        self._lbl_page.setText("")

    def update_texts(self):
        if self._doc is None:
            self._hint_label.setText(translations.get_text("arc_no_preview"))
        self._update_nav_state()

    # ------ Zoom (anchor to cursor) ------

    def _on_zoom_at_pos(self, direction, viewport_pos):
        """Zoom in/out anchored at the cursor position in the viewport."""
        if not self._raw_pixmaps:
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

    def _rebuild_scaled_pages(self):
        if not self._raw_pixmaps:
            return
        if self._fit_mode:
            avail_w = self._scroll.viewport().width() - 12
            if avail_w <= 0:
                avail_w = 600
            max_raw_w = max(pm.width() for pm in self._raw_pixmaps)
            self._zoom = avail_w / max_raw_w if max_raw_w > 0 else 1.0

        scaled = []
        for pm in self._raw_pixmaps:
            w = int(pm.width() * self._zoom)
            h = int(pm.height() * self._zoom)
            scaled.append(pm.scaled(
                w, h, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
        self._pages_widget.set_pages(scaled)
        # Re-apply overlays after the page swap (set_pages clears them)
        if getattr(self, "_current_field_overlays", None):
            self.set_field_overlays(self._current_field_overlays)
        self._reapply_search_highlight()
        if getattr(self, "_current_fuzzy_matches", None):
            self._reapply_fuzzy_matches()
        self._update_zoom_label()

        # Schedule hi-res re-render if zoomed beyond base resolution
        if self._zoom > 1.05 and self._pdf_path:
            self._hires_timer.start()
        else:
            self._hires_timer.stop()

    def _update_zoom_label(self):
        self._lbl_zoom.setText(f"{int(self._zoom * 100)}%")

    # ------ Scroll tracking ------

    def _on_scroll(self, value):
        if not self._raw_pixmaps:
            return
        for i in range(self._pages_widget.page_count() - 1, -1, -1):
            if value >= self._pages_widget.page_y_offset(i) - 20:
                self._lbl_page.setText(f"{i + 1} / {self._page_count}")
                return

    # ------ Internal ------

    def _close_doc(self):
        if self._doc:
            self._doc.close()
            self._doc = None
            self._page_count = 0
            self._pdf_path = None

    def _bg_render(self, dpi, gen, is_base=False):
        """Render all pages at *dpi* in a background thread."""
        path = self._pdf_path
        signal = self._pages_rendered

        def _worker():
            try:
                import fitz
                doc = fitz.open(path)
                mat = fitz.Matrix(dpi / 72.0, dpi / 72.0)
                images = []
                for i in range(len(doc)):
                    pix = doc[i].get_pixmap(matrix=mat, alpha=False, annots=True)
                    images.append(QImage(pix.samples, pix.width, pix.height,
                                         pix.stride, QImage.Format.Format_RGB888).copy())
                doc.close()
                signal.emit((images, gen, is_base))
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True).start()

    def _on_pages_rendered(self, data):
        images, gen, is_base = data
        if gen != self._render_gen:
            return
        pixmaps = [QPixmap.fromImage(img) for img in images]
        if is_base:
            self._raw_pixmaps = pixmaps
            self._rebuild_scaled_pages()
        else:
            # Hi-res: pixmaps already at correct display size
            self._pages_widget.set_pages(pixmaps)
            if getattr(self, "_current_field_overlays", None):
                self.set_field_overlays(self._current_field_overlays)
            self._reapply_search_highlight()
            if getattr(self, "_current_fuzzy_matches", None):
                self._reapply_fuzzy_matches()

    def _start_hires_render(self):
        if not self._pdf_path or self._zoom <= 1.05:
            return
        effective_dpi = self.RENDER_DPI * self._zoom
        self._render_gen += 1
        self._bg_render(effective_dpi, self._render_gen, is_base=False)

    def _update_nav_state(self):
        has = self._doc is not None
        self._lbl_page.setText(f"1 / {self._page_count}" if has else "")
