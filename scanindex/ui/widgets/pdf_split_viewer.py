"""PdfSplitViewer — Step 1's PDF viewer with click-to-cut between pages.

Vertical scroll of bitmap-rendered pages (mirrors KieArchiveViewer's render
path but stripped of all KIE/edit logic). Between each pair of consecutive
pages sits a `_CutGutter` widget; hovering over it switches the cursor to a
scissor glyph, clicking toggles a cut at that boundary. Cuts are owned by
the parent `ArchiveSession` so the file list view can re-derive segments.

Single-page PDFs render with no gutters since there is nothing to split.

Undo / redo on the cut history are exposed as public methods so the
parent screen can hook them to Ctrl+Z / Ctrl+Y."""
from __future__ import annotations

from collections import deque
from typing import Optional

import fitz
from PySide6.QtCore import Qt, QPoint, QSize, QTimer, Signal
from PySide6.QtGui import (
    QCursor, QImage, QPainter, QPen, QPixmap, QWheelEvent, QColor, QBrush,
)
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from scanindex.ui.theme import (
    COLOR_BG, COLOR_BORDER, COLOR_RED, COLOR_TEXT_MUTED, COLOR_ACCENT,
    FONT_UI,
)


# Render PDF pages once at this base scale (good readability, modest cost).
# All user zoom is applied as a fast Qt pixmap scale on top of the cached
# base pixmap — we do NOT re-rasterise from PyMuPDF on every zoom tick.
_BASE_RENDER_SCALE = 2.0
_MIN_DISPLAY_SCALE = 0.35
_MAX_DISPLAY_SCALE = 2.5
_DEFAULT_DISPLAY_SCALE = 0.85  # so the page fits typical screens at start
_ZOOM_STEP = 1.15
_GUTTER_HEIGHT = 22

# Fat scroll bars — about 2x default Qt size so they're easy to grab.
_SCROLLBAR_PX = 22


class _ZoomScrollArea(QScrollArea):
    """Scroll area where Ctrl+wheel zooms anchored to cursor."""
    zoom_requested = Signal(int, QPoint)

    def wheelEvent(self, event: QWheelEvent):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            if delta != 0:
                self.zoom_requested.emit(1 if delta > 0 else -1,
                                         event.position().toPoint())
            event.accept()
            return
        super().wheelEvent(event)


class _CutGutter(QFrame):
    """Click-toggleable strip between two page widgets."""
    cut_toggled = Signal(int)  # page_idx where cut would fall (= "before this page")

    def __init__(self, before_page_idx: int, parent=None):
        super().__init__(parent)
        self.before_page_idx = before_page_idx
        self.is_cut = False
        self._hover = False
        self.setFixedHeight(_GUTTER_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setStyleSheet(f"background: transparent;")

    def set_cut(self, on: bool):
        if self.is_cut != on:
            self.is_cut = on
            self.update()

    def enterEvent(self, ev):
        self._hover = True
        self.update()
        super().enterEvent(ev)

    def leaveEvent(self, ev):
        self._hover = False
        self.update()
        super().leaveEvent(ev)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self.cut_toggled.emit(self.before_page_idx)
            ev.accept()
            return
        super().mousePressEvent(ev)

    def paintEvent(self, ev):
        super().paintEvent(ev)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect()
        cy = rect.center().y()

        if self.is_cut:
            # Solid red line + scissor glyph centered
            pen = QPen(QColor(COLOR_RED), 2.2)
            p.setPen(pen)
            p.drawLine(rect.left() + 4, cy, rect.right() - 4, cy)
            self._draw_scissor(p, rect.center().x(), cy, QColor(COLOR_RED))
        elif self._hover:
            # Dashed accent preview when hovering
            pen = QPen(QColor(COLOR_ACCENT), 1.4, Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.drawLine(rect.left() + 4, cy, rect.right() - 4, cy)
            self._draw_scissor(p, rect.center().x(), cy, QColor(COLOR_ACCENT))
        # Idle state: nothing drawn — invisible gutter
        p.end()

    @staticmethod
    def _draw_scissor(p: QPainter, cx: int, cy: int, color: QColor):
        # Tiny stylized scissor: two circles + crossed lines
        p.setBrush(QBrush(QColor(0, 0, 0, 0)))
        p.setPen(QPen(color, 1.4))
        r = 4
        p.drawEllipse(cx - 7, cy - 5, r, r)
        p.drawEllipse(cx + 3, cy - 5, r, r)
        p.drawLine(cx - 5, cy - 1, cx + 6, cy + 6)
        p.drawLine(cx + 5, cy - 1, cx - 6, cy + 6)


class _PageLabel(QLabel):
    """Just renders one page's pixmap. No interaction."""
    def __init__(self, page_idx: int, parent=None):
        super().__init__(parent)
        self.page_idx = page_idx
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet(f"background: white; border: 1px solid {COLOR_BORDER};")


class PdfSplitViewer(QWidget):
    """Continuous-scroll PDF viewer with click-to-cut gutters."""

    cut_changed = Signal(int, bool)  # page_idx, is_now_cut
    page_count_changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._doc: Optional[fitz.Document] = None
        self._page_labels: list[_PageLabel] = []
        # Pixmaps rendered ONCE from PyMuPDF at _BASE_RENDER_SCALE; zoom
        # operates on these via Qt's smooth pixmap scaler (no re-render).
        self._base_pixmaps: list[QPixmap] = []
        self._display_scale = _DEFAULT_DISPLAY_SCALE
        self._gutters: list[_CutGutter] = []
        # History of cut-toggle actions for undo/redo: each entry is page_idx
        # of the toggled cut + the resulting state (True=on after toggle).
        self._undo: deque[tuple[int, bool]] = deque(maxlen=200)
        self._redo: deque[tuple[int, bool]] = deque(maxlen=200)

        self._setup_ui()

    def _setup_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._scroll = _ZoomScrollArea()
        self._scroll.setWidgetResizable(True)
        # Fat scroll bars (2x default) so the user can grab them easily on
        # high-DPI screens. Themed to match the dark surface.
        self._scroll.setStyleSheet(
            f"QScrollArea {{ background: {COLOR_BG}; border: none; }}"
            f"QScrollBar:vertical {{ width: {_SCROLLBAR_PX}px; "
            f"background: {COLOR_BG}; margin: 0; }}"
            f"QScrollBar:horizontal {{ height: {_SCROLLBAR_PX}px; "
            f"background: {COLOR_BG}; margin: 0; }}"
            f"QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{"
            f"  background: {COLOR_BORDER}; border-radius: 6px; "
            f"  min-height: 32px; min-width: 32px; }}"
            f"QScrollBar::handle:vertical:hover, "
            f"QScrollBar::handle:horizontal:hover {{ background: {COLOR_ACCENT}; }}"
            f"QScrollBar::add-line, QScrollBar::sub-line {{ "
            f"  width: 0; height: 0; background: none; border: none; }}"
            f"QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}"
        )
        self._scroll.zoom_requested.connect(self._on_zoom)

        self._inner = QWidget()
        self._inner.setStyleSheet(f"background: {COLOR_BG};")
        self._stack = QVBoxLayout(self._inner)
        self._stack.setContentsMargins(12, 12, 12, 12)
        self._stack.setSpacing(0)
        self._stack.addStretch()

        self._scroll.setWidget(self._inner)
        outer.addWidget(self._scroll, 1)

        # Empty-state placeholder shown when no PDF is loaded
        self._empty_label = QLabel(
            "Kéo thả file PDF vào đây hoặc dùng nút \"Chọn PDF\" ở thanh trên",
            alignment=Qt.AlignmentFlag.AlignCenter,
        )
        self._empty_label.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; font-size: 14px; font-family: {FONT_UI}; "
            f"padding: 60px;"
        )
        self._stack.insertWidget(0, self._empty_label)

    # ── public API ──────────────────────────────────────────────────

    def load_pdf(self, path: str) -> int:
        """Open the PDF, render every page once, build cut gutters between
        them. Returns the page count (0 on failure)."""
        self.clear()
        try:
            self._doc = fitz.open(path)
        except Exception:
            self._doc = None
            return 0

        n = len(self._doc)
        # Hide empty-state placeholder
        self._empty_label.setVisible(False)

        for i in range(n):
            page_w = _PageLabel(i)
            base_pm = self._render_base_pixmap(i)
            self._base_pixmaps.append(base_pm)
            self._apply_display_scale(page_w, base_pm)
            self._page_labels.append(page_w)
            # Insert before the trailing stretch
            self._stack.insertWidget(self._stack.count() - 1, page_w,
                                     0, Qt.AlignmentFlag.AlignHCenter)
            if i < n - 1:
                gutter = _CutGutter(before_page_idx=i + 1)
                gutter.cut_toggled.connect(self._on_gutter_clicked)
                self._gutters.append(gutter)
                self._stack.insertWidget(self._stack.count() - 1, gutter)

        self.page_count_changed.emit(n)
        return n

    def clear(self):
        # Drop everything except the trailing stretch
        for w in self._page_labels:
            w.setParent(None); w.deleteLater()
        for g in self._gutters:
            g.setParent(None); g.deleteLater()
        self._page_labels.clear()
        self._gutters.clear()
        self._base_pixmaps.clear()
        self._undo.clear()
        self._redo.clear()
        if self._doc is not None:
            try:
                self._doc.close()
            except Exception:
                pass
            self._doc = None
        self._empty_label.setVisible(True)

    def page_count(self) -> int:
        return len(self._page_labels)

    def get_cut_points(self) -> set[int]:
        """Return the set of page indices that are currently cut on."""
        return {g.before_page_idx for g in self._gutters if g.is_cut}

    def set_cut_points(self, cuts: set[int]):
        """Replace cut state from an external source (e.g. session restore)."""
        for g in self._gutters:
            g.set_cut(g.before_page_idx in cuts)

    def set_interaction_enabled(self, enabled: bool):
        """Enable/disable only user editing affordances.

        Programmatic scrolling remains available while Step 1 OCR is running,
        but users cannot toggle split gutters until the OCR/classifier phase
        finishes.
        """
        for g in self._gutters:
            g.setEnabled(enabled)
            g.setCursor(
                QCursor(Qt.CursorShape.PointingHandCursor)
                if enabled else QCursor(Qt.CursorShape.ArrowCursor)
            )

    def scroll_to_page(self, page_idx: int):
        if not (0 <= page_idx < len(self._page_labels)):
            return
        target = self._page_labels[page_idx]
        self._scroll.ensureWidgetVisible(target, 0, 12)

    # ── undo / redo ─────────────────────────────────────────────────

    def undo(self):
        if not self._undo:
            return
        page_idx, was_on_after = self._undo.pop()
        # Reverse: flip back
        target_state = not was_on_after
        self._apply_cut(page_idx, target_state, push_undo=False)
        self._redo.append((page_idx, was_on_after))

    def redo(self):
        if not self._redo:
            return
        page_idx, want_state = self._redo.pop()
        self._apply_cut(page_idx, want_state, push_undo=False)
        self._undo.append((page_idx, want_state))

    # ── zoom ────────────────────────────────────────────────────────

    def _on_zoom(self, direction: int, viewport_pos: QPoint):
        old = self._display_scale
        new = old * (_ZOOM_STEP if direction > 0 else 1 / _ZOOM_STEP)
        new = max(_MIN_DISPLAY_SCALE, min(_MAX_DISPLAY_SCALE, new))
        if abs(new - old) < 1e-3:
            return
        self._display_scale = new
        # On long PDFs (134+ pages) calling QPixmap.scaled on every page per
        # tick was 150-200ms and caused the visible stutter. Strategy:
        #   1. Scale pages currently in (or near) the viewport synchronously
        #      so the user sees instant feedback at the cursor.
        #   2. Defer the rest to the next event-loop tick, processed in small
        #      batches so the GUI thread stays responsive.
        visible = self._visible_page_indices()
        for i in visible:
            if 0 <= i < len(self._page_labels):
                self._apply_display_scale(
                    self._page_labels[i], self._base_pixmaps[i]
                )
        if len(visible) < len(self._page_labels):
            self._zoom_pending = [
                i for i in range(len(self._page_labels)) if i not in set(visible)
            ]
            QTimer.singleShot(0, self._drain_zoom_pending)
        else:
            self._zoom_pending = []

    def _drain_zoom_pending(self):
        """Rescale up to N off-screen pages per event-loop tick. Reschedules
        until done so zoom + scroll remain responsive on long PDFs."""
        if not getattr(self, "_zoom_pending", None):
            return
        batch = self._zoom_pending[:8]
        self._zoom_pending = self._zoom_pending[8:]
        for i in batch:
            if 0 <= i < len(self._page_labels):
                self._apply_display_scale(
                    self._page_labels[i], self._base_pixmaps[i]
                )
        if self._zoom_pending:
            QTimer.singleShot(0, self._drain_zoom_pending)

    def _visible_page_indices(self) -> list[int]:
        """Indices of pages whose vertical band overlaps the viewport, plus a
        one-page buffer above and below for smooth scroll-during-zoom."""
        if not self._page_labels:
            return []
        sb = self._scroll.verticalScrollBar()
        top = sb.value()
        vp_h = self._scroll.viewport().height()
        # Buffer = one viewport above/below so freshly-scrolled pages are
        # already at the right size by the time the user gets to them.
        margin = vp_h
        bot = top + vp_h
        hits = []
        for i, label in enumerate(self._page_labels):
            y = label.y()
            h = label.height()
            if y + h >= top - margin and y <= bot + margin:
                hits.append(i)
        return hits

    def page_up(self):
        """Scroll viewer up by one page; called from arrow keys."""
        idx = self._current_page_index()
        if idx is None:
            return
        self.scroll_to_page(max(0, idx - 1))

    def page_down(self):
        """Scroll viewer down by one page; called from arrow keys."""
        idx = self._current_page_index()
        if idx is None:
            return
        self.scroll_to_page(min(len(self._page_labels) - 1, idx + 1))

    def _current_page_index(self) -> Optional[int]:
        """Best guess at which page is currently centered in the viewport.
        Returns None if no PDF loaded."""
        if not self._page_labels:
            return None
        viewport_top = self._scroll.verticalScrollBar().value()
        viewport_mid = viewport_top + self._scroll.viewport().height() // 2
        # Walk pages and pick the one whose vertical band straddles the mid.
        # Pages are stacked top-to-bottom inside _inner; their y is in inner
        # coordinates which equals scroll_value-relative.
        for i, label in enumerate(self._page_labels):
            top = label.y()
            bottom = top + label.height()
            if top <= viewport_mid <= bottom:
                return i
        # Fallback: closest to mid
        best = min(range(len(self._page_labels)),
                   key=lambda i: abs(self._page_labels[i].y()
                                     + self._page_labels[i].height() // 2
                                     - viewport_mid))
        return best

    # ── internals ───────────────────────────────────────────────────

    def _render_base_pixmap(self, page_idx: int) -> QPixmap:
        """Rasterise one PDF page at the high-quality base scale, used as
        the source for all subsequent display scaling."""
        if self._doc is None or not (0 <= page_idx < len(self._doc)):
            return QPixmap()
        page = self._doc[page_idx]
        mat = fitz.Matrix(_BASE_RENDER_SCALE, _BASE_RENDER_SCALE)
        pix = page.get_pixmap(matrix=mat, alpha=False, annots=True)
        img = QImage(pix.samples, pix.width, pix.height,
                     pix.stride, QImage.Format.Format_RGB888)
        return QPixmap.fromImage(img.copy())

    def _apply_display_scale(self, label: _PageLabel, base_pm: QPixmap):
        """Display the base pixmap at the current zoom level via Qt's smooth
        transform — no PyMuPDF call. Cheap enough for live zoom on long PDFs."""
        if base_pm.isNull():
            return
        # Scale relative to base render scale so display_scale=1.0 means
        # 'same physical size as the base render', i.e. roughly 2x natural
        # since _BASE_RENDER_SCALE is 2.0.
        ratio = self._display_scale / _BASE_RENDER_SCALE
        target_w = max(1, int(base_pm.width() * ratio))
        target_h = max(1, int(base_pm.height() * ratio))
        scaled = base_pm.scaled(
            target_w, target_h,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        label.setPixmap(scaled)
        label.setFixedSize(scaled.size())

    def _on_gutter_clicked(self, page_idx: int):
        # Find which gutter and flip
        for g in self._gutters:
            if g.before_page_idx == page_idx:
                new_state = not g.is_cut
                self._apply_cut(page_idx, new_state, push_undo=True)
                # New action invalidates any pending redo branch
                self._redo.clear()
                return

    def _apply_cut(self, page_idx: int, on: bool, push_undo: bool):
        for g in self._gutters:
            if g.before_page_idx == page_idx:
                if g.is_cut == on:
                    return
                g.set_cut(on)
                if push_undo:
                    self._undo.append((page_idx, on))
                self.cut_changed.emit(page_idx, on)
                return
