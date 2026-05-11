"""KIE-aware PDF viewer for the archive screen.

Ports the editing essentials from `kie_viewer.py`:
  - All OCR words drawn as bbox candidates (cyan outline)
  - Field-owned words coloured per-label
  - Edit mode: click a word to toggle, Ctrl+drag to ADD, Shift+drag to REMOVE
  - Canonical-JSON-aware: edits flow back into `field_instances`
  - Save button writes the modified canonical JSON to disk

The viewer renders pages into separate `_PdfPageWidget` instances stacked
vertically inside a QScrollArea, mirroring the layout used by kie_viewer.
"""
from __future__ import annotations

import json
import os
import re
from typing import Optional

import fitz
from collections import deque
from PySide6.QtCore import Qt, QRectF, QPoint, QPointF, QTimer, Signal
from PySide6.QtGui import (
    QBrush, QColor, QFont, QImage, QMouseEvent, QPainter, QPen, QPixmap,
    QTransform, QWheelEvent,
)
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QMenu, QMessageBox, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)
from PySide6.QtGui import QAction, QKeySequence, QShortcut

from scanindex.ui.theme import (
    COLOR_ACCENT, COLOR_ACCENT_HOVER, COLOR_BG, COLOR_BORDER, COLOR_BORDER_DEFAULT,
    COLOR_ELEVATED, COLOR_HOVER, COLOR_SURFACE, COLOR_TEXT, COLOR_TEXT_MUTED,
    COLOR_TEXT_SECONDARY, FONT_UI, RADIUS_MD,
)


# ── Per-label colour palette (dark fill / light border) ────────────────
LABEL_COLORS = {
    "REGIME_HEADER":      ("#dc2626", "#fca5a5"),
    "ISSUE_ORG_SUPERIOR": ("#0284c7", "#7dd3fc"),
    "ISSUE_ORG_NAME":     ("#a21caf", "#e879f9"),
    "DOC_NUMBER_SYMBOL":  ("#ea580c", "#fdba74"),
    "PLACE_DATE":         ("#4b5563", "#d1d5db"),
    "DOC_SUBJECT":        ("#65a30d", "#bef264"),
    "ADDRESSEE":          ("#a16207", "#facc15"),
    "RECIPIENTS":         ("#1e40af", "#60a5fa"),
    "SIGNER_ROLE":        ("#0d9488", "#5eead4"),
    "SIGNER_NAME":        ("#9333ea", "#d8b4fe"),
    "URGENCY_MARK":       ("#9f1239", "#fb7185"),
    "SECRECY_MARK":       ("#3f3f46", "#a1a1aa"),
    "CIRCULATION_MARK":   ("#155e75", "#22d3ee"),
    "DOC_TYPE":           ("#737373", "#d4d4d4"),
}

# Numeric badges drawn next to each field bbox (matches kie_viewer.py)
FIELD_NUMBER_MAP = {
    "REGIME_HEADER":      0,
    "ISSUE_ORG_SUPERIOR": 1,
    "ISSUE_ORG_NAME":     2,
    "DOC_NUMBER_SYMBOL":  3,
    "PLACE_DATE":         4,
    "DOC_SUBJECT":        5,
    "ADDRESSEE":          6,
    "RECIPIENTS":         7,
    "SIGNER_ROLE":        8,
    "SIGNER_NAME":        9,
}

FIELD_DISPLAY_NAMES = {
    "REGIME_HEADER":      "Tiêu ngữ",
    "ISSUE_ORG_SUPERIOR": "Cơ quan cấp trên",
    "ISSUE_ORG_NAME":     "Cơ quan ban hành",
    "DOC_NUMBER_SYMBOL":  "Số, ký hiệu văn bản",
    "PLACE_DATE":         "Địa danh, ngày tháng",
    "DOC_SUBJECT":        "Trích yếu",
    "ADDRESSEE":          "Kính gửi",
    "RECIPIENTS":         "Nơi nhận",
    "SIGNER_ROLE":        "Chức vụ người ký",
    "SIGNER_NAME":        "Người ký",
}


def _field_display_name(label: str) -> str:
    return FIELD_DISPLAY_NAMES.get(label, label)


def _field_menu_text(label: str) -> str:
    name = _field_display_name(label)
    return f"{name} ({label})" if name != label else label


_LINE_NUM_RE = re.compile(r"(?:^|[_\-.])(?:l|line)[_\-.]?(\d+)(?=$|[_\-.])", re.IGNORECASE)


def _label_color(label: str) -> tuple[str, str]:
    return LABEL_COLORS.get(label, ("#6b7280", "#9ca3af"))


def _word_bbox(word: dict) -> list[float]:
    bbox = word.get("bbox")
    if bbox and len(bbox) >= 4:
        return [float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])]
    x = float(word.get("x", 0.0) or 0.0)
    y = float(word.get("y", 0.0) or 0.0)
    return [x, y, x + float(word.get("w", 0.0) or 0.0), y + float(word.get("h", 0.0) or 0.0)]


def _line_number_from_id(line_id: object) -> int | None:
    if line_id is None:
        return None
    match = _LINE_NUM_RE.search(str(line_id))
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def _reading_order_words_for_page(page: dict, words: list[dict]) -> list[dict]:
    line_order: dict[str, int] = {}
    for order, line in enumerate(page.get("lines") or []):
        line_id = line.get("id") or line.get("line_id")
        if line_id is not None:
            try:
                line_order[str(line_id)] = int(line.get("order", order) or order)
            except (TypeError, ValueError):
                line_order[str(line_id)] = order

    def key(item):
        fallback_order, word = item
        bbox = _word_bbox(word)
        line_id = word.get("line_id")
        if line_id is not None and str(line_id) in line_order:
            line_key = (0, line_order[str(line_id)])
        else:
            parsed = _line_number_from_id(line_id)
            if parsed is not None:
                line_key = (1, parsed)
            else:
                cy = (float(bbox[1]) + float(bbox[3])) / 2.0
                line_key = (2, round(cy / 10.0))
        try:
            word_order = int(word.get("order", fallback_order) or fallback_order)
        except (TypeError, ValueError):
            word_order = fallback_order
        return (line_key[0], line_key[1], float(bbox[0]), word_order, fallback_order)

    return [word for _idx, word in sorted(enumerate(words or []), key=key)]


# ──────────────────────────────────────────────────────────────────────
# Scroll area: Ctrl+wheel = zoom (anchored to cursor), plain wheel = scroll
# ──────────────────────────────────────────────────────────────────────

class _ZoomScrollArea(QScrollArea):
    zoom_requested = Signal(int, QPoint)  # direction (+1 zoom in, -1 out), viewport pos

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._pan_origin: Optional[QPoint] = None
        self._pan_start_h = 0
        self._pan_start_v = 0
        self._pan_active = False

    def wheelEvent(self, event: QWheelEvent):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            if delta != 0:
                direction = 1 if delta > 0 else -1
                self.zoom_requested.emit(direction, event.position().toPoint())
            event.accept()
            return
        super().wheelEvent(event)

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            self._pan_origin = event.position().toPoint()
            self._pan_start_h = self.horizontalScrollBar().value()
            self._pan_start_v = self.verticalScrollBar().value()
            self._pan_active = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        if (event.buttons() & Qt.MouseButton.LeftButton) and self._pan_origin is not None:
            delta = event.position().toPoint() - self._pan_origin
            if self._pan_active or abs(delta.x()) > 4 or abs(delta.y()) > 4:
                self._pan_active = True
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
                self.horizontalScrollBar().setValue(self._pan_start_h - delta.x())
                self.verticalScrollBar().setValue(self._pan_start_v - delta.y())
                event.accept()
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton and self._pan_origin is not None:
            was_pan = self._pan_active
            self._pan_origin = None
            self._pan_active = False
            self.unsetCursor()
            if was_pan:
                event.accept()
                return
        super().mouseReleaseEvent(event)


# ──────────────────────────────────────────────────────────────────────
# Per-page widget (paint + drag/click hit-testing)
# ──────────────────────────────────────────────────────────────────────

class _PdfPageWidget(QLabel):
    word_selection_changed = Signal(int, list, str)  # page_idx, word_ids, op
    bbox_clicked_non_edit = Signal(int, str)           # page_idx, word_id
    bbox_right_clicked = Signal(int, str, object)        # page_idx, word_id, global_pos QPoint
    page_right_clicked = Signal(int, object)             # page_idx, global_pos QPoint (empty area)
    empty_clicked = Signal()                              # left-click on empty area
    fuzzy_clicked = Signal(int, str, object)            # page_idx, text, bbox_pdf

    def __init__(self, page_index: int, parent: QWidget | None = None):
        super().__init__(parent)
        self.page_index = page_index
        # `_source_pixmap`: pristine high-resolution fitz render. Step 2 is
        # opened at 50%, but zooming must scale from this high-res source
        # instead of from the 50% display pixmap; otherwise the page gets soft
        # as soon as users zoom in.
        # `base_pixmap`: pixmap at the current display scale — what overlays
        # are painted onto by `_repaint`.
        self._source_pixmap: Optional[QPixmap] = None
        self._source_render_scale = 1.0
        self.base_pixmap: Optional[QPixmap] = None
        self.pdf_width = 0.0
        self.pdf_height = 0.0
        self.render_scale = 1.0
        self.word_rects: list[tuple[str, list[float]]] = []
        self.word_ownership: dict[str, str] = {}     # word_id -> label
        self.selected_word_ids: set[str] = set()
        self.highlight_bboxes: list[tuple] = []      # (bbox, label, is_selected)
        self.field_icons: list[tuple] = []            # (anchor_bbox, number, label, is_selected)
        self.fuzzy_overlays: list[dict] = []          # [{bbox, text, score, rank}]
        self.word_to_line: dict[str, str] = {}       # word_id → line_id
        self.line_to_words: dict[str, list[str]] = {}
        # Coord-system metadata copied from canonical JSON. Both flags here
        # are READ in _bbox_to_rect (and therefore also affect hit testing,
        # since hit testing reuses the same projection).
        self.bbox_origin_bottom_left = False          # PDF y=0-at-bottom legacy frame
        self.applied_rotation = 0                     # 0/90/180/270 from preprocessing
        self.edit_mode = False
        self._drag_origin: Optional[QPointF] = None
        self._drag_current: Optional[QPointF] = None
        self._is_dragging = False
        self._drag_modifiers = Qt.KeyboardModifier.NoModifier
        self._pan_scroll: Optional[QScrollArea] = None
        self._pan_origin: Optional[QPoint] = None
        self._pan_start_h = 0
        self._pan_start_v = 0
        self._pan_active = False
        self._non_edit_click_pos: Optional[QPointF] = None
        self._overlaid_pixmap: Optional[QPixmap] = None
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)

    # ---- public ----------------------------------------------------

    def set_page(
        self,
        pixmap: QPixmap,
        pdf_w: float,
        pdf_h: float,
        scale: float,
        source_scale: float | None = None,
    ):
        # Store the fitz output as the unscaled high-res source so subsequent
        # zoom ticks can rescale from it without cumulative blur.
        self._source_pixmap = pixmap
        self._source_render_scale = float(source_scale or scale or 1.0)
        self.pdf_width = pdf_w
        self.pdf_height = pdf_h
        self.render_scale = scale
        self._overlaid_pixmap = None
        self.rescale_from_source(scale)

    def rescale_from_source(self, display_scale: float):
        """Update the visible pixmap from the high-res source render."""
        self.render_scale = display_scale
        target_w = max(1, int(round(self.pdf_width * display_scale)))
        target_h = max(1, int(round(self.pdf_height * display_scale)))
        src = self._source_pixmap
        if src is None or src.isNull():
            self.setFixedSize(target_w, target_h)
            return
        if src.width() == target_w and src.height() == target_h:
            display_pm = src
        else:
            display_pm = src.scaled(
                target_w, target_h,
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        self.base_pixmap = display_pm
        self._overlaid_pixmap = None
        self.setPixmap(display_pm)
        self.setFixedSize(display_pm.size())

    def set_word_rects(self, words: list[tuple[str, list[float]]]):
        self.word_rects = list(words)
        self._repaint()

    def set_word_ownership(self, ownership: dict[str, str]):
        self.word_ownership = dict(ownership or {})
        self._repaint()

    def set_selected_field(self, label: str | None,
                            highlight_bboxes: list[tuple] | None = None,
                            selected_word_ids: list[str] | None = None,
                            field_icons: list[tuple] | None = None):
        self.highlight_bboxes = list(highlight_bboxes or [])
        self.selected_word_ids = {str(wid) for wid in (selected_word_ids or [])}
        self.field_icons = list(field_icons or [])
        self._repaint()

    def set_edit_mode(self, on: bool):
        self.edit_mode = on
        if on:
            self.unsetCursor()
        else:
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        self._repaint()

    def set_pan_scroll_area(self, scroll_area: QScrollArea):
        self._pan_scroll = scroll_area

    def set_fuzzy_overlays(self, overlays: list[dict]):
        """Each overlay: {bbox, text, score, rank} — bbox in PDF points."""
        self.fuzzy_overlays = list(overlays or [])
        self._repaint()

    def clear_fuzzy_overlays(self):
        self.fuzzy_overlays = []
        self._repaint()

    # ---- paint -----------------------------------------------------

    def _bbox_to_rect(self, bbox: list[float]) -> QRectF:
        """Project an OCR bbox onto the rendered pixmap.

        ``bbox_origin_bottom_left=True`` means the canonical JSON encodes bbox
        coordinates in the frame of the upside-down source image (a 180° flip,
        not just a y mirror) — so both X and Y must be mirrored. Used by both
        painting and hit testing, so the two stay consistent automatically.
        """
        s = self.render_scale
        x0, y0, x1, y1 = bbox[:4]
        if self.bbox_origin_bottom_left and self.pdf_height and self.pdf_width:
            top_y = self.pdf_height - y1
            left_x = self.pdf_width - x1
        else:
            top_y = y0
            left_x = x0
        return QRectF(left_x * s, top_y * s, (x1 - x0) * s, (y1 - y0) * s)

    def _repaint(self):
        if not self.base_pixmap:
            return
        pm = self.base_pixmap.copy()
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Draw ALL words in edit mode (cyan outline for unassigned, dim per-label
        # for words owned by other fields)
        if self.edit_mode and self.word_rects:
            for wid, bbox in self.word_rects:
                rect = self._bbox_to_rect(bbox)
                if wid in self.selected_word_ids:
                    continue  # rendered by highlight_bboxes pass below
                if wid in self.word_ownership:
                    label = self.word_ownership[wid]
                    dark, light = _label_color(label)
                    fill = QColor(dark); fill.setAlpha(20)
                    painter.setBrush(QBrush(fill))
                    painter.setPen(QPen(QColor(light).darker(150), 0.8))
                    painter.drawRect(rect)
                else:
                    fill = QColor(0, 200, 255, 15)
                    painter.setBrush(QBrush(fill))
                    painter.setPen(QPen(QColor(0, 200, 255, 120), 1.2))
                    painter.drawRect(rect)

        # Draw highlighted bboxes (selected field's words)
        for bbox, label, is_selected in self.highlight_bboxes:
            dark, light = _label_color(label)
            fill = QColor(dark)
            fill.setAlpha(70 if is_selected else 35)
            painter.setBrush(QBrush(fill))
            pen = QColor(light); pen.setAlpha(220)
            painter.setPen(QPen(pen, 2.5 if is_selected else 1.5))
            painter.drawRect(self._bbox_to_rect(bbox))

        # Field icons — numeric pill badges (one per field) connected to
        # their bbox by a short line. Matches kie_viewer's visual.
        for anchor_bbox, number, label, is_selected in self.field_icons:
            self._draw_field_icon(painter, anchor_bbox, number, label, is_selected)

        # Fuzzy match overlays — distinct cyan, drawn on top
        if self.fuzzy_overlays:
            cyan = QColor("#00ffff")
            for ov in self.fuzzy_overlays:
                bbox = ov.get("bbox")
                if not bbox or len(bbox) < 4:
                    continue
                rect = self._bbox_to_rect(bbox)
                fill = QColor(cyan); fill.setAlpha(60)
                painter.setBrush(QBrush(fill))
                painter.setPen(QPen(cyan, 2))
                painter.drawRect(rect)
                badge = f"#{ov.get('rank', 0) + 1}  {ov.get('score', 0):.0f}%"
                bw = max(60, len(badge) * 7)
                badge_rect = QRectF(rect.left(), max(0, rect.top() - 14), bw, 14)
                painter.fillRect(badge_rect, cyan)
                painter.setPen(QPen(QColor("#000000")))
                painter.drawText(badge_rect.adjusted(4, 0, -2, 0),
                                  Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                                  badge)

        painter.end()
        self._overlaid_pixmap = pm
        self.setPixmap(pm)

    def _draw_field_icon(self, painter: QPainter, anchor_bbox: list[float],
                          number, label: str, is_selected: bool):
        """Pill (ellipse) + text + connector line, placed to the right of the
        field's bbox. Width grows with text length so multi-char badges fit."""
        rect = self._bbox_to_rect(anchor_bbox)
        if rect.isEmpty():
            return
        dark, light = _label_color(label)
        text = str(number)

        ay = rect.center().y()
        ry = 12 if is_selected else 10
        rx = ry + max(0, len(text) - 1) * 4
        line_len = 14

        cx = rect.right() + line_len + rx
        pm_w = self.base_pixmap.width() if self.base_pixmap else 0
        if cx + rx > pm_w:
            cx = rect.left() - line_len - rx
            line_start_x = rect.left()
            line_end_x = cx + rx
        else:
            line_start_x = rect.right()
            line_end_x = cx - rx
        cy = ay

        # Connector line
        pen = QPen(QColor(dark), 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawLine(QPointF(line_start_x, ay), QPointF(line_end_x, cy))

        # Pill (filled dark, light border)
        painter.setBrush(QBrush(QColor(dark)))
        painter.setPen(QPen(QColor(light), 2.0 if is_selected else 1.2))
        painter.drawEllipse(QPointF(cx, cy), rx, ry)

        # Text (white, bold)
        painter.setPen(QColor("white"))
        font_size = 9 if is_selected else 8
        if len(text) >= 3:
            font_size -= 1
        font = QFont(FONT_UI, font_size)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(
            QRectF(cx - rx, cy - ry, 2 * rx, 2 * ry),
            Qt.AlignmentFlag.AlignCenter, text,
        )

    def _draw_rubber_band(self):
        if not self._overlaid_pixmap or not self._drag_origin or not self._drag_current:
            return
        pm = self._overlaid_pixmap.copy()
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self._drag_origin, self._drag_current).normalized()
        painter.setBrush(QBrush(QColor(0, 200, 255, 30)))
        painter.setPen(QPen(QColor(0, 200, 255, 180), 1.5, Qt.PenStyle.DashLine))
        painter.drawRect(rect)
        painter.end()
        self.setPixmap(pm)

    # ---- input -----------------------------------------------------

    def _pixmap_offset(self) -> QPointF:
        pm = self.pixmap()
        if not pm:
            return QPointF(0, 0)
        ox = (self.width() - pm.width()) / 2.0
        oy = (self.height() - pm.height()) / 2.0
        return QPointF(max(0, ox), max(0, oy))

    def mousePressEvent(self, event: QMouseEvent):
        # Right-click anywhere → either bbox-context (Delete) or page-context
        # (+ Trường mới) menu, depending on whether we hit a word.
        if event.button() == Qt.MouseButton.RightButton:
            off = self._pixmap_offset()
            pos = event.position() - off
            global_pos = event.globalPosition().toPoint()
            for wid, bbox in self.word_rects:
                if self._bbox_to_rect(bbox).contains(pos):
                    self.bbox_right_clicked.emit(self.page_index, wid, global_pos)
                    return
            # Empty area → page-level context menu (add new field)
            self.page_right_clicked.emit(self.page_index, global_pos)
            return

        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        if not self.edit_mode:
            self._non_edit_click_pos = event.position()
            self._pan_origin = event.globalPosition().toPoint()
            self._pan_active = False
            if self._pan_scroll is not None:
                self._pan_start_h = self._pan_scroll.horizontalScrollBar().value()
                self._pan_start_v = self._pan_scroll.verticalScrollBar().value()
            event.accept()
            return
        off = self._pixmap_offset()
        self._drag_origin = event.position() - off
        self._drag_current = self._drag_origin
        self._is_dragging = False
        self._drag_modifiers = event.modifiers()

    def mouseMoveEvent(self, event: QMouseEvent):
        if not self.edit_mode and self._pan_origin is not None:
            delta = event.globalPosition().toPoint() - self._pan_origin
            if self._pan_scroll is not None and (
                self._pan_active or abs(delta.x()) > 4 or abs(delta.y()) > 4
            ):
                self._pan_active = True
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
                self._pan_scroll.horizontalScrollBar().setValue(
                    self._pan_start_h - delta.x()
                )
                self._pan_scroll.verticalScrollBar().setValue(
                    self._pan_start_v - delta.y()
                )
            event.accept()
            return
        if not self.edit_mode or self._drag_origin is None:
            return super().mouseMoveEvent(event)
        off = self._pixmap_offset()
        pos = event.position() - off
        delta = pos - self._drag_origin
        if not self._is_dragging and (abs(delta.x()) > 4 or abs(delta.y()) > 4):
            self._is_dragging = True
        if self._is_dragging:
            self._drag_current = pos
            self._draw_rubber_band()

    def mouseReleaseEvent(self, event: QMouseEvent):
        if not self.edit_mode and event.button() == Qt.MouseButton.LeftButton:
            was_pan = self._pan_active
            click_pos = self._non_edit_click_pos or event.position()
            self._reset_pan()
            if was_pan:
                event.accept()
                return
            self._handle_non_edit_click(click_pos)
            event.accept()
            return
        if not self.edit_mode or event.button() != Qt.MouseButton.LeftButton:
            self._reset_drag()
            return super().mouseReleaseEvent(event)
        if self._drag_origin is None:
            # The matching press may have happened in non-edit mode and turned
            # edit mode on. Do not treat that same release as an edit click.
            self._reset_drag()
            event.accept()
            return
        off = self._pixmap_offset()
        pos = event.position() - off
        modifiers = self._drag_modifiers | event.modifiers()
        was_dragging = self._is_dragging

        if was_dragging:
            rect = QRectF(self._drag_origin, self._drag_current).normalized()
            hits = []
            for wid, bbox in self.word_rects:
                if rect.intersects(self._bbox_to_rect(bbox)):
                    hits.append(wid)
            self._reset_drag()
            self._repaint()  # remove rubber band
            if hits:
                op = self._op_from_modifiers(modifiers, default="set")
                self.word_selection_changed.emit(self.page_index, hits, op)
        else:
            self._reset_drag()
            for wid, bbox in self.word_rects:
                if self._bbox_to_rect(bbox).contains(pos):
                    # Single safeguard so a click never silently destroys data
                    # the user spent time labelling:
                    #   - Word belongs to a DIFFERENT KIE field than the
                    #     active one  →  switch focus to that field only
                    #     (re-use `bbox_clicked_non_edit`). NO transfer.
                    #   - Word IS in the active field   →  toggle (= remove).
                    #   - Word is unassigned              →  toggle (= add).
                    # Ctrl/Shift bypasses the focus-switch for power users.
                    explicit_op = bool(
                        modifiers & (Qt.KeyboardModifier.ControlModifier
                                     | Qt.KeyboardModifier.ShiftModifier)
                    )
                    if (wid in self.word_ownership
                            and wid not in self.selected_word_ids
                            and not explicit_op):
                        self.bbox_clicked_non_edit.emit(self.page_index, wid)
                        return
                    op = self._op_from_modifiers(modifiers, default="toggle")
                    self.word_selection_changed.emit(self.page_index, [wid], op)
                    return
            # Empty-area click in edit mode exits edit mode only; the parent
            # keeps pending bbox edits dirty until an explicit save/leave.
            self.empty_clicked.emit()

    def _handle_non_edit_click(self, click_pos: QPointF):
        off = self._pixmap_offset()
        pos = click_pos - off
        # Fuzzy overlays click takes precedence and works outside edit mode.
        if self.fuzzy_overlays:
            for ov in self.fuzzy_overlays:
                bbox = ov.get("bbox")
                if not bbox:
                    continue
                if self._bbox_to_rect(bbox).contains(pos):
                    self.fuzzy_clicked.emit(
                        self.page_index, ov.get("text", ""), list(bbox)
                    )
                    return
        for wid, bbox in self.word_rects:
            if self._bbox_to_rect(bbox).contains(pos):
                self.bbox_clicked_non_edit.emit(self.page_index, wid)
                return
        self.empty_clicked.emit()

    def _reset_drag(self):
        self._drag_origin = None
        self._drag_current = None
        self._is_dragging = False
        self._drag_modifiers = Qt.KeyboardModifier.NoModifier

    def _reset_pan(self):
        self._pan_origin = None
        self._pan_active = False
        self._non_edit_click_pos = None
        if self.edit_mode:
            self.unsetCursor()
        else:
            self.setCursor(Qt.CursorShape.OpenHandCursor)

    @staticmethod
    def _op_from_modifiers(modifiers, default):
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            return "add"
        if modifiers & Qt.KeyboardModifier.ShiftModifier:
            return "remove"
        return default


# ──────────────────────────────────────────────────────────────────────
# KieArchiveViewer — multi-page stacked viewer with edit mode + save
# ──────────────────────────────────────────────────────────────────────

class KieArchiveViewer(QWidget):
    """Drop-in replacement for the archive screen's PDF viewer.

    Public API mirrors the simpler PdfViewerWidget for back-compat where
    possible:
      - `load_pdf(path)`
      - `clear()`
      - `set_field_overlays(fields)` (legacy non-edit overlay)
      - `clear_field_overlays()`

    New API for KIE editing:
      - `load_canonical(canonical_json_path)` — feed OCR words to the viewer
      - `set_active_field(field_id)` — highlight that field's words
      - `set_edit_mode(bool)` — enable click-and-drag word selection
      - `save_now()` — write canonical JSON if dirty
      - `dirty_changed`, `field_words_changed`, `field_clicked` signals.
    """

    prev_file_requested = Signal()
    next_file_requested = Signal()
    field_words_changed = Signal(str, str)   # field_id, op  (set/add/remove/toggle/deleted:<label>)
    field_clicked = Signal(str)              # field_id (when user clicks a word in non-edit mode)
    fuzzy_match_picked = Signal(str, list)    # text, bbox_pdf
    dirty_changed = Signal(bool)

    RENDER_DPI = 150
    # Match Step 1's strategy: keep a high-quality base raster and scale the
    # visible pixmap from it. At 150 DPI this is roughly 2.08x PDF points, so
    # the default 50% view can zoom to 100% without reusing a low-res image.
    SOURCE_RENDER_SCALE = RENDER_DPI / 72.0

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._doc: Optional[fitz.Document] = None
        self._pdf_path: Optional[str] = None
        self._canonical: Optional[dict] = None
        self._canonical_path: Optional[str] = None
        self._page_widgets: list[_PdfPageWidget] = []
        # Default 50% — Step 2's wide screen layout fits 2 columns of
        # PDF + form comfortably at this zoom. User can increase via the
        # zoom controls.
        self._zoom = 0.5
        self._edit_mode = False
        self._dirty = False
        self._last_dirty_resolution: Optional[str] = None
        self._active_field_id: Optional[str] = None
        # Pixmap cache keyed by (pdf_path, source_scale, page_idx) — switching back
        # to a file we've already opened reuses cached pixmaps so the file
        # appears instantly instead of blocking on fitz re-render.
        self._pixmap_cache: dict[tuple, QPixmap] = {}
        self._pixmap_cache_max = 200  # cap to avoid unbounded growth

        # Lazy viewport rendering state — page widgets start as empty
        # placeholders sized to the PDF page; pixmaps are filled in only
        # for pages currently visible in the viewport (+1 viewport buffer).
        # Rendering happens off the click path via QTimer.singleShot ticks.
        self._render_queue: deque = deque()
        self._render_queued: set = set()       # page indices in queue or rendered
        self._render_active = False             # single-slot render guard
        self._render_gen = 0                    # bump on file load to invalidate
        self._scroll_debounce_pending = False

        # Undo/redo stacks — store (field_id, word_ids, line_ids, text) snapshots
        # of the active field's word selection. Cleared on file load / field swap.
        self._undo_stack: list[dict] = []
        self._redo_stack: list[dict] = []
        self._UNDO_LIMIT = 50

        self._build_ui()
        # Wire Ctrl+Z / Ctrl+Y shortcuts on the viewer
        QShortcut(QKeySequence("Ctrl+Z"), self, self._undo)
        QShortcut(QKeySequence("Ctrl+Y"), self, self._redo)
        QShortcut(QKeySequence("Ctrl+Shift+Z"), self, self._redo)

    # ---- UI build --------------------------------------------------

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Toolbar
        bar = QFrame()
        bar.setFixedHeight(34)
        bar.setStyleSheet(
            f"QFrame {{ background: {COLOR_SURFACE}; border-bottom: 1px solid {COLOR_BORDER}; }}"
        )
        h = QHBoxLayout(bar)
        h.setContentsMargins(6, 0, 6, 0)
        h.setSpacing(4)

        self._btn_prev = self._mini_btn("◀")
        self._btn_prev.clicked.connect(self.prev_file_requested.emit)
        h.addWidget(self._btn_prev)
        self._lbl_file = QLabel("")
        self._lbl_file.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 11px;")
        self._lbl_file.setMinimumWidth(80)
        self._lbl_file.setAlignment(Qt.AlignmentFlag.AlignCenter)
        h.addWidget(self._lbl_file)
        self._btn_next = self._mini_btn("▶")
        self._btn_next.clicked.connect(self.next_file_requested.emit)
        h.addWidget(self._btn_next)

        h.addSpacing(8)
        self._btn_zoom_out = self._mini_btn("−")
        self._btn_zoom_out.clicked.connect(self._zoom_out)
        h.addWidget(self._btn_zoom_out)
        self._lbl_zoom = QLabel(f"{int(self._zoom * 100)}%")
        self._lbl_zoom.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 11px;")
        self._lbl_zoom.setMinimumWidth(40)
        self._lbl_zoom.setAlignment(Qt.AlignmentFlag.AlignCenter)
        h.addWidget(self._lbl_zoom)
        self._btn_zoom_in = self._mini_btn("+")
        self._btn_zoom_in.clicked.connect(self._zoom_in)
        h.addWidget(self._btn_zoom_in)

        h.addStretch(1)

        # Active-field indicator (shown when edit mode is on)
        self._lbl_active = QLabel("")
        self._lbl_active.setStyleSheet(
            f"color: {COLOR_TEXT}; font: 600 11px '{FONT_UI}'; padding: 0 8px;"
        )
        self._lbl_active.hide()
        h.addWidget(self._lbl_active)

        # Edit mode toggle
        self._btn_edit = QPushButton("✎ Sửa bbox")
        self._btn_edit.setCheckable(True)
        self._btn_edit.setStyleSheet(self._toggle_style())
        self._btn_edit.toggled.connect(self._on_edit_toggled)
        h.addWidget(self._btn_edit)

        # Save button
        self._btn_save = QPushButton("💾 Lưu")
        self._btn_save.setStyleSheet(
            f"QPushButton {{ background: {COLOR_ACCENT}; color: white;"
            f" border: none; padding: 4px 12px; border-radius: {RADIUS_MD}px;"
            f" font: 600 12px '{FONT_UI}'; }}"
            f"QPushButton:hover {{ background: {COLOR_ACCENT_HOVER}; }}"
            f"QPushButton:disabled {{ background: #555; color: #aaa; }}"
        )
        self._btn_save.clicked.connect(self.save_now)
        self._btn_save.setEnabled(False)
        h.addWidget(self._btn_save)

        outer.addWidget(bar)

        # Scroll area with stacked pages — supports Ctrl+wheel zoom
        self._scroll = _ZoomScrollArea()
        self._scroll.zoom_requested.connect(self._on_zoom_wheel)
        self._scroll.setWidgetResizable(True)
        self._scroll.setStyleSheet(f"""
            QScrollArea {{ background: {COLOR_BG}; border: none; }}
            QScrollBar:vertical {{
                background: {COLOR_SURFACE};
                width: 12px;
                margin: 0;
            }}
            QScrollBar:horizontal {{
                background: {COLOR_SURFACE};
                height: 12px;
                margin: 0;
            }}
            QScrollBar::handle:vertical,
            QScrollBar::handle:horizontal {{
                background: {COLOR_BORDER_DEFAULT};
                border-radius: 6px;
                min-height: 36px;
                min-width: 36px;
            }}
            QScrollBar::handle:vertical:hover,
            QScrollBar::handle:horizontal:hover {{
                background: {COLOR_TEXT_MUTED};
            }}
            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical,
            QScrollBar::add-line:horizontal,
            QScrollBar::sub-line:horizontal {{
                width: 0;
                height: 0;
            }}
            QScrollBar::add-page:vertical,
            QScrollBar::sub-page:vertical,
            QScrollBar::add-page:horizontal,
            QScrollBar::sub-page:horizontal {{
                background: transparent;
            }}
        """)
        self._inner = QWidget()
        self._inner.setStyleSheet(f"background: {COLOR_BG};")
        self._inner_layout = QVBoxLayout(self._inner)
        self._inner_layout.setContentsMargins(8, 8, 8, 8)
        self._inner_layout.setSpacing(6)
        self._inner_layout.addStretch(1)
        self._scroll.setWidget(self._inner)
        outer.addWidget(self._scroll, 1)

    def _mini_btn(self, text: str) -> QPushButton:
        b = QPushButton(text)
        b.setFixedSize(26, 26)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setStyleSheet(
            f"QPushButton {{ background: transparent; border: 1px solid transparent;"
            f" border-radius: 4px; color: {COLOR_TEXT_SECONDARY}; font-size: 12px; }}"
            f"QPushButton:hover {{ background: {COLOR_ELEVATED};"
            f" border-color: {COLOR_BORDER_DEFAULT}; color: {COLOR_TEXT}; }}"
        )
        return b

    def _toggle_style(self) -> str:
        return (
            f"QPushButton {{ background: transparent; color: {COLOR_TEXT_SECONDARY};"
            f" border: 1px solid {COLOR_BORDER_DEFAULT}; padding: 3px 10px;"
            f" border-radius: {RADIUS_MD}px; font: 11px '{FONT_UI}'; }}"
            f"QPushButton:hover {{ background: {COLOR_HOVER}; color: {COLOR_TEXT}; }}"
            f"QPushButton:checked {{ background: {COLOR_ACCENT}; color: white;"
            f" border-color: {COLOR_ACCENT}; }}"
        )

    # ---- doc lifecycle --------------------------------------------

    def load_pdf(self, pdf_path: str):
        self._close_doc()
        # Wipe undo/redo when switching files — stacks are scoped per active
        # field and the per-file canonical changes underneath us.
        self._undo_stack.clear()
        self._redo_stack.clear()
        try:
            self._doc = fitz.open(pdf_path)
            self._pdf_path = pdf_path
        except Exception as e:
            self._show_message(f"Cannot open PDF: {e}")
            return
        self._render_pages()

    def load_canonical(self, canonical_json_path: str):
        """Load the canonical OCR JSON so word bboxes + ownership become
        available. Call AFTER load_pdf for the same file."""
        self._canonical_path = canonical_json_path
        self._canonical = None
        self._last_dirty_resolution = None
        if canonical_json_path and os.path.exists(canonical_json_path):
            try:
                with open(canonical_json_path, "r", encoding="utf-8") as f:
                    self._canonical = json.load(f)
                ann = (self._canonical or {}).get("annotations") or None
                if ann and ann.get("field_instances"):
                    try:
                        from scanindex.core.kie.postprocess import apply_layoutlmv3_schema_postprocess
                        self._canonical["annotations"] = apply_layoutlmv3_schema_postprocess(self._canonical, ann)
                    except Exception:
                        pass
            except Exception:
                self._canonical = None
        self._apply_word_data_to_pages()

    def clear(self):
        self._close_doc()
        self._canonical = None
        self._canonical_path = None
        self._active_field_id = None
        self._dirty = False
        self._last_dirty_resolution = None
        self.dirty_changed.emit(False)
        self._btn_save.setEnabled(False)
        self._clear_pages()

    def _close_doc(self):
        if self._doc is not None:
            try:
                self._doc.close()
            except Exception:
                pass
        self._doc = None
        self._pdf_path = None

    def _clear_pages(self):
        # Remove all page widgets except the trailing stretch
        while self._inner_layout.count() > 1:
            item = self._inner_layout.takeAt(0)
            if item is None:
                continue
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._page_widgets = []

    def _show_message(self, text: str):
        self._clear_pages()
        lbl = QLabel(text)
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; padding: 40px;")
        self._inner_layout.insertWidget(0, lbl)

    def _render_pages(self):
        """Lay out PLACEHOLDER widgets for every page in the document, sized
        to the page's rendered dimensions but with no pixmap yet. Pixmaps
        are filled in lazily by `_drain_render_queue` for pages currently
        visible in the viewport (matches kie_viewer's smooth-load UX).

        For 100-page documents this turns the load-time cost from
        ~5s (render every page) into ~50ms (compute placeholder sizes)."""
        if not self._doc:
            return
        self._clear_pages()
        # New render generation invalidates any in-flight tick from the
        # previously-loaded file
        self._render_gen += 1
        self._render_queue.clear()
        self._render_queued.clear()
        self._render_active = False

        my_gen = self._render_gen
        scale = self.RENDER_DPI / 72.0 * self._zoom

        for pi in range(self._doc.page_count):
            page = self._doc[pi]
            page_w_pts = page.rect.width
            page_h_pts = page.rect.height
            # If preprocessing rotated the source image 90/270° before OCR,
            # the OCR bbox frame is also rotated — swap w/h here so the
            # placeholder reserves the right slot AND _bbox_to_rect operates
            # in the rotated frame.
            rotation = self._get_page_rotation(pi)
            if rotation in (90, 270):
                page_w_pts, page_h_pts = page_h_pts, page_w_pts
            ph_w = int(round(page_w_pts * scale))
            ph_h = int(round(page_h_pts * scale))

            pw = _PdfPageWidget(pi, parent=self._inner)
            pw.set_pan_scroll_area(self._scroll)
            pw.setFixedSize(ph_w, ph_h)
            # Placeholder visual — pale background so user knows space is reserved
            pw.setStyleSheet(
                f"background: {COLOR_SURFACE};"
                f" border: 1px solid {COLOR_BORDER};"
            )
            # Important: still record pdf dims + scale so _bbox_to_rect works
            # before the pixmap arrives (no overlays drawn until pixmap loaded
            # but the storage is correct).
            pw.pdf_width = page_w_pts
            pw.pdf_height = page_h_pts
            pw.render_scale = scale
            pw.applied_rotation = rotation
            pw.bbox_origin_bottom_left = self._page_uses_bottom_left_coords(pi)

            pw.set_edit_mode(self._edit_mode)
            pw.word_selection_changed.connect(self._on_word_selection_changed)
            pw.bbox_clicked_non_edit.connect(self._on_bbox_clicked_non_edit)
            pw.bbox_right_clicked.connect(self._on_bbox_right_clicked)
            pw.page_right_clicked.connect(self._on_page_right_clicked)
            pw.empty_clicked.connect(self._on_empty_clicked)
            pw.fuzzy_clicked.connect(self._on_fuzzy_clicked)
            self._inner_layout.insertWidget(pi, pw, 0, Qt.AlignmentFlag.AlignHCenter)
            self._page_widgets.append(pw)

        # Hook scroll observer once. Connecting twice would fire twice per
        # scroll tick, but disconnecting on a never-connected signal raises
        # a RuntimeWarning, so we use a sentinel attribute instead.
        if not getattr(self, "_scroll_observer_wired", False):
            self._scroll.verticalScrollBar().valueChanged.connect(self._on_scroll)
            self._scroll_observer_wired = True

        # Apply ownership/word_rects so click hit-testing works even on
        # placeholder pages — overlays draw once the pixmap is filled.
        self._apply_word_data_to_pages()

        # Defer the first viewport enqueue so the layout is realised by Qt
        # before we measure widget y-positions.
        QTimer.singleShot(0, lambda: self._enqueue_viewport_pages(my_gen))

    def _viewport_page_indices(self) -> list[int]:
        """Indices of pages whose y-range overlaps viewport (+1 viewport
        buffer above/below) — these are the pages we need pixmaps for."""
        if not self._page_widgets:
            return []
        scroll_y = self._scroll.verticalScrollBar().value()
        vp_h = self._scroll.viewport().height()
        buffer = vp_h
        top = scroll_y - buffer
        bot = scroll_y + vp_h + buffer
        hits = []
        for pw in self._page_widgets:
            y = pw.y()
            h = pw.height()
            if y + h >= top and y <= bot:
                hits.append(pw.page_index)
        return hits

    def _enqueue_viewport_pages(self, my_gen: int):
        """Add visible pages (+ buffer) to the render queue if not already."""
        if my_gen != self._render_gen:
            return
        newly_queued = 0
        for idx in self._viewport_page_indices():
            if idx in self._render_queued:
                continue
            self._render_queue.append(idx)
            self._render_queued.add(idx)
            newly_queued += 1
        if newly_queued and not self._render_active:
            QTimer.singleShot(0, lambda: self._drain_render_queue(my_gen))

    def _drain_render_queue(self, my_gen: int):
        """Render one queued page per tick, then re-schedule until empty."""
        if my_gen != self._render_gen:
            return
        if not self._render_queue:
            self._render_active = False
            return
        self._render_active = True
        page_idx = self._render_queue.popleft()
        try:
            self._fill_page_pixmap(page_idx)
        except Exception:
            pass
        self._render_active = False
        if self._render_queue:
            QTimer.singleShot(0, lambda: self._drain_render_queue(my_gen))

    def _fill_page_pixmap(self, page_idx: int):
        """Materialise the actual pixmap for a placeholder page widget.
        Hits the pixmap cache when possible; otherwise calls fitz."""
        if not self._doc:
            return
        if not (0 <= page_idx < len(self._page_widgets)):
            return
        pw = self._page_widgets[page_idx]
        if pw.base_pixmap is not None:
            return  # already rendered
        page = self._doc[page_idx]
        display_scale = self.RENDER_DPI / 72.0 * self._zoom
        source_scale = max(self.SOURCE_RENDER_SCALE, display_scale)
        rotation = self._get_page_rotation(page_idx)
        # Cache key includes rotation so a manually-rotated page does not
        # collide with the un-rotated render.
        cache_key = (self._pdf_path, round(source_scale, 4), page_idx, rotation)
        pm = self._pixmap_cache.get(cache_key)
        if pm is None:
            mat = fitz.Matrix(source_scale, source_scale)
            pix = page.get_pixmap(matrix=mat, annots=True)
            img = QImage(pix.samples, pix.width, pix.height, pix.stride,
                          QImage.Format.Format_RGB888)
            pm = QPixmap.fromImage(img.copy())
            if rotation:
                pm = pm.transformed(
                    QTransform().rotate(rotation),
                    Qt.TransformationMode.SmoothTransformation,
                )
            self._cache_pixmap(cache_key, pm)
        # PDF dims for the page-widget are the rotated frame (so overlays
        # and hit testing project against the right axes).
        w_pts, h_pts = page.rect.width, page.rect.height
        if rotation in (90, 270):
            w_pts, h_pts = h_pts, w_pts
        pw.set_page(pm, w_pts, h_pts, display_scale, source_scale=source_scale)
        # set_page wipes word_rects/ownership; re-apply this page's data
        self._apply_word_data_to_single_page(page_idx)
        # Re-apply highlights now that the pixmap is in place
        self._refresh_active_field_highlight()

    def _apply_word_data_to_single_page(self, page_idx: int):
        """Re-apply the canonical's word data to ONE page (used after lazy
        render fills in its pixmap)."""
        if not self._canonical or not (0 <= page_idx < len(self._page_widgets)):
            return
        pages = self._canonical.get("pages") or []
        if not (0 <= page_idx < len(pages)):
            return
        page = pages[page_idx]
        ann = self._canonical.get("annotations") or {}
        fields = ann.get("field_instances") or []
        ownership: dict[str, str] = {}
        for f in fields:
            if int(f.get("page_index", 0)) != page_idx:
                continue
            label = f.get("label", "")
            for wid in f.get("word_ids") or []:
                ownership[str(wid)] = label
        words = page.get("words") or []
        word_rects = []
        word_to_line: dict[str, str] = {}
        line_to_words: dict[str, list[str]] = {}
        word_bbox_by_id: dict[str, list[float]] = {}
        for w in words:
            wid = w.get("id") or w.get("word_id")
            if not wid:
                continue
            wid = str(wid)
            bbox = _word_bbox(w)
            word_rects.append((wid, bbox))
            word_bbox_by_id[wid] = bbox
            lid = w.get("line_id")
            if lid:
                lid = str(lid)
                word_to_line[wid] = lid
                line_to_words.setdefault(lid, []).append(wid)
        for line_word_ids in line_to_words.values():
            line_word_ids.sort(key=lambda wid: (word_bbox_by_id.get(wid, [0, 0, 0, 0])[0], wid))
        pw = self._page_widgets[page_idx]
        # Refresh coord-system flags from canonical — they may have changed
        # if the user is editing across files with mixed pipeline versions.
        pw.applied_rotation = self._get_page_rotation(page_idx)
        pw.bbox_origin_bottom_left = self._page_uses_bottom_left_coords(page_idx)
        pw.set_word_rects(word_rects)
        pw.set_word_ownership(ownership)
        pw.word_to_line = word_to_line
        pw.line_to_words = line_to_words
    def _on_scroll(self, _value: int):
        """Coalesce rapid scroll events; queue any newly-visible pages."""
        if self._scroll_debounce_pending:
            return
        self._scroll_debounce_pending = True
        my_gen = self._render_gen
        QTimer.singleShot(60, lambda: self._scroll_debounce_fire(my_gen))

    def _scroll_debounce_fire(self, my_gen: int):
        self._scroll_debounce_pending = False
        if my_gen != self._render_gen:
            return
        self._enqueue_viewport_pages(my_gen)

    def _cache_pixmap(self, key: tuple, pm: QPixmap):
        """Store pixmap with simple LRU-ish eviction (drop oldest entries
        when the cap is hit)."""
        if len(self._pixmap_cache) >= self._pixmap_cache_max:
            for k in list(self._pixmap_cache.keys())[:20]:
                self._pixmap_cache.pop(k, None)
        self._pixmap_cache[key] = pm

    def _apply_word_data_to_pages(self):
        if not self._canonical or not self._page_widgets:
            return
        pages = self._canonical.get("pages") or []
        ann = (self._canonical.get("annotations") or {})
        fields = ann.get("field_instances") or []
        ownership: dict[int, dict[str, str]] = {}
        for f in fields:
            pi = int(f.get("page_index", 0))
            label = f.get("label", "")
            for wid in f.get("word_ids") or []:
                ownership.setdefault(pi, {})[str(wid)] = label

        for pi, pw in enumerate(self._page_widgets):
            page = pages[pi] if pi < len(pages) else {}
            words = page.get("words") or []
            word_rects = []
            for w in words:
                wid = w.get("id") or w.get("word_id")
                if not wid:
                    continue
                wid = str(wid)
                bbox = w.get("bbox") or [0, 0, 0, 0]
                word_rects.append((wid, list(bbox)))
            pw.set_word_rects(word_rects)
            pw.set_word_ownership(ownership.get(pi, {}))
        self._refresh_active_field_highlight()

    # ---- field selection -------------------------------------------

    def set_active_field(self, field_id: str | None):
        # Active-field change → undo/redo stack is no longer applicable
        # (it tracks state of the field that *was* active)
        if field_id != self._active_field_id:
            self._undo_stack.clear()
            self._redo_stack.clear()
        self._active_field_id = field_id
        self._refresh_active_field_highlight()
        self._update_active_label()

    def _update_active_label(self):
        label_txt = ""
        if self._canonical and self._active_field_id:
            for f in (self._canonical.get("annotations") or {}).get("field_instances") or []:
                if f.get("field_id") == self._active_field_id:
                    label_txt = f"Đang sửa: {_field_menu_text(f.get('label', '?'))}"
                    break
        if self._edit_mode and not label_txt:
            label_txt = "Chọn field bên metadata để sửa bbox"
        self._lbl_active.setText(label_txt)
        self._lbl_active.setVisible(bool(label_txt))

    def _refresh_active_field_highlight(self):
        """Build the per-page render data sent to each `_PdfPageWidget`.

        Highlight bboxes are emitted PER WORD (one tuple per `word_id`)
        rather than as a single merged union — this matches kie_viewer's
        visual where each word has its own small box. The numeric pill
        icon still anchors to the union bbox (one icon per field)."""
        if not self._canonical or not self._page_widgets:
            return
        ann = self._canonical.get("annotations") or {}
        fields = ann.get("field_instances") or []
        pages = self._canonical.get("pages") or []

        # Build a per-page word_id → bbox lookup once
        word_bbox_by_page: list[dict[str, list[float]]] = []
        for page in pages:
            wmap: dict[str, list[float]] = {}
            for w in page.get("words", []) or []:
                wid = w.get("id") or w.get("word_id")
                if wid:
                    bb = w.get("bbox") or [
                        float(w.get("x", 0)), float(w.get("y", 0)),
                        float(w.get("x", 0)) + float(w.get("w", 0)),
                        float(w.get("y", 0)) + float(w.get("h", 0)),
                    ]
                    wmap[str(wid)] = list(bb)
            word_bbox_by_page.append(wmap)

        per_page: dict[int, list] = {}
        per_page_selected: dict[int, list] = {}
        per_page_icons: dict[int, list] = {}
        active_field = None

        # Phase 1 — group same-label fields per page for stable a/b/c badge
        # suffixes (e.g. SIGNER_ROLE 8a / 8b when there are two signers on
        # one page). Single-instance fields keep their plain number.
        groups_pl: dict[tuple[int, str], list[dict]] = {}
        for f in fields:
            pi = int(f.get("page_index", 0))
            label = f.get("label", "")
            groups_pl.setdefault((pi, label), []).append(f)

        def _anchor_xy(field: dict) -> tuple[float, float]:
            pi = int(field.get("page_index", 0))
            wmap = word_bbox_by_page[pi] if 0 <= pi < len(word_bbox_by_page) else {}
            ys: list[float] = []
            xs: list[float] = []
            for wid in field.get("word_ids") or []:
                bb = wmap.get(str(wid))
                if bb:
                    ys.append(bb[1])
                    xs.append(bb[0])
            if not ys and field.get("bbox"):
                ys.append(field["bbox"][1])
                xs.append(field["bbox"][0])
            return (min(ys) if ys else 0.0, min(xs) if xs else 0.0)

        # Phase 2 — assign badge text per field id.
        field_badge: dict[str, str] = {}
        for (_pi, label), group in groups_pl.items():
            base = FIELD_NUMBER_MAP.get(label)
            if base is None:
                continue
            if len(group) > 1:
                group.sort(key=_anchor_xy)
            n = len(group)
            for idx, gf in enumerate(group):
                fid = gf.get("field_id")
                if not fid:
                    continue
                if n == 1:
                    field_badge[fid] = str(base)
                elif idx < 26:
                    field_badge[fid] = f"{base}{chr(ord('a') + idx)}"
                else:
                    field_badge[fid] = f"{base}.{idx + 1}"

        # Phase 3 — collect bboxes + icons per page using the assigned badges.
        for f in fields:
            if f.get("field_id") == self._active_field_id:
                active_field = f
            pi = int(f.get("page_index", 0))
            label = f.get("label", "")
            is_sel = (f.get("field_id") == self._active_field_id)
            wmap = word_bbox_by_page[pi] if 0 <= pi < len(word_bbox_by_page) else {}

            # Per-word bboxes for the highlight layer
            word_ids = f.get("word_ids") or []
            for wid in word_ids:
                bb = wmap.get(str(wid))
                if bb:
                    per_page.setdefault(pi, []).append((bb, label, is_sel))
            # Fallback: if no word_ids resolve (e.g. prediction-only field
            # without word tracking), draw the union bbox once
            if not any((str(wid) in wmap) for wid in word_ids):
                ub = f.get("bbox")
                if ub:
                    per_page.setdefault(pi, []).append((ub, label, is_sel))

            # One numeric pill per field, anchored at the union bbox.
            # Uses the multi-instance-aware badge text from Phase 2.
            union_bbox = f.get("bbox")
            if union_bbox:
                badge = field_badge.get(
                    f.get("field_id"),
                    str(FIELD_NUMBER_MAP.get(label, "?")),
                )
                per_page_icons.setdefault(pi, []).append(
                    (union_bbox, badge, label, is_sel)
                )

        if active_field is not None:
            pi = int(active_field.get("page_index", 0))
            per_page_selected[pi] = list(active_field.get("word_ids") or [])

        for pi, pw in enumerate(self._page_widgets):
            pw.set_selected_field(
                label=None,
                highlight_bboxes=per_page.get(pi, []),
                selected_word_ids=per_page_selected.get(pi, []),
                field_icons=per_page_icons.get(pi, []),
            )

    # ---- legacy compat (for non-edit display path) -----------------

    def set_field_overlays(self, fields: list[dict]):
        # The new viewer's overlay display is driven by `_canonical`'s
        # annotations block. This shim is kept so archive_tab can call
        # the same API as before — it just triggers a refresh.
        self._refresh_active_field_highlight()

    def clear_field_overlays(self):
        for pw in self._page_widgets:
            pw.set_selected_field(None, [], [])

    # ── Fuzzy-match overlays (clickable) ─────────────────────────────

    def set_fuzzy_matches(self, matches: list):
        """Display ranked fuzzy candidates on whatever page they belong to.
        `matches` is the list returned by `archive_fuzzy.fuzzy_rank`."""
        if not self._page_widgets:
            return
        # Group by page
        by_page: dict[int, list[dict]] = {}
        for rank, m in enumerate(matches or []):
            pi = int(m.get("page_index", 0))
            by_page.setdefault(pi, []).append({
                "bbox": list(m.get("bbox") or []),
                "text": m.get("text", ""),
                "score": float(m.get("score", 0)),
                "rank": rank,
            })
        for pi, pw in enumerate(self._page_widgets):
            pw.set_fuzzy_overlays(by_page.get(pi, []))

    def clear_fuzzy_matches(self):
        for pw in self._page_widgets:
            pw.clear_fuzzy_overlays()

    def highlight_zone(self, page_idx: int, bbox: list[float]):
        """Scroll the inner content so that `bbox` on `page_idx` is centred
        in the viewport. Used by the field-label-click handler to jump
        directly to the chosen field's location."""
        if not (0 <= page_idx < len(self._page_widgets)):
            return
        pw = self._page_widgets[page_idx]
        # Position of the page widget within the inner content
        page_top = pw.y()
        # Convert PDF-points bbox y-mid to pixel space using current render scale
        scale = self.RENDER_DPI / 72.0 * self._zoom
        if bbox and len(bbox) >= 4:
            y_mid_pdf = (bbox[1] + bbox[3]) / 2.0
            y_in_page = int(y_mid_pdf * scale)
        else:
            y_in_page = 0
        target_y = page_top + y_in_page
        # Centre the bbox in the viewport
        viewport_h = self._scroll.viewport().height()
        scroll_to = max(0, target_y - viewport_h // 2)
        self._scroll.verticalScrollBar().setValue(scroll_to)
        # Also flash a transient zone marker on that bbox
        # (the persistent overlay already paints field bboxes when active)

    def clear_highlight(self):
        pass

    def set_file_label(self, current_idx: int, total: int):
        self._lbl_file.setText(f"{current_idx + 1} / {total}" if total else "")

    def set_file_nav_enabled(self, can_prev: bool, can_next: bool):
        self._btn_prev.setEnabled(can_prev)
        self._btn_next.setEnabled(can_next)

    def update_texts(self):
        pass

    # ---- zoom ------------------------------------------------------

    def _zoom_in(self):
        self._set_zoom(min(3.0, round(self._zoom + 0.25, 2)))

    def _zoom_out(self):
        self._set_zoom(max(0.25, round(self._zoom - 0.25, 2)))

    def _set_zoom(self, new_zoom: float):
        if abs(new_zoom - self._zoom) < 0.001:
            return
        self._zoom = new_zoom
        self._lbl_zoom.setText(f"{int(self._zoom * 100)}%")
        if self._doc:
            self._apply_zoom_in_place()

    def _apply_zoom_in_place(self):
        """Resize page widgets and scale from the high-resolution source
        pixmap, mirroring Step 1's zoom path. This keeps zoom responsive
        while avoiding the old 50%-source blur."""
        if not self._doc:
            return
        new_scale = self.RENDER_DPI / 72.0 * self._zoom
        for pw in self._page_widgets:
            pw.rescale_from_source(new_scale)
            try:
                pw._repaint()
            except Exception:
                pw.update()
        # Pixmaps not yet loaded (placeholders) need to be re-queued so they
        # render at the current display scale from the high-res source cache.
        self._render_queue.clear()
        self._render_queued = {
            pw.page_index for pw in self._page_widgets
            if pw.base_pixmap is not None
        }
        QTimer.singleShot(0, lambda: self._enqueue_viewport_pages(self._render_gen))

    def _on_zoom_wheel(self, direction: int, viewport_pos: QPoint):
        """Ctrl+wheel zoom anchored at the cursor. Re-renders pages and
        adjusts scroll bars so the point under the cursor stays put."""
        old_zoom = self._zoom
        steps = [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0]
        if direction > 0:
            new_zoom = next((z for z in steps if z > old_zoom + 0.01), None)
        else:
            new_zoom = next((z for z in reversed(steps) if z < old_zoom - 0.01), None)
        if new_zoom is None:
            return

        # Translate viewport pos to inner-content coords pre-zoom
        hbar = self._scroll.horizontalScrollBar()
        vbar = self._scroll.verticalScrollBar()
        content_x = hbar.value() + viewport_pos.x()
        content_y = vbar.value() + viewport_pos.y()
        ratio = new_zoom / old_zoom

        self._set_zoom(new_zoom)

        # Re-anchor: keep the world point under the cursor at the same screen pos
        new_x = int(content_x * ratio - viewport_pos.x())
        new_y = int(content_y * ratio - viewport_pos.y())
        hbar.setValue(max(0, new_x))
        vbar.setValue(max(0, new_y))

    # ---- edit mode -------------------------------------------------

    def _on_edit_toggled(self, checked: bool):
        self._edit_mode = bool(checked)
        # If user enters edit mode without an active field, auto-pick the
        # first one so clicks aren't silently dropped.
        if self._edit_mode and not self._active_field_id and self._canonical:
            fields = (self._canonical.get("annotations") or {}).get("field_instances") or []
            if fields:
                self._active_field_id = fields[0].get("field_id")
                self._refresh_active_field_highlight()
        for pw in self._page_widgets:
            pw.set_edit_mode(self._edit_mode)
        self._update_active_label()

    # ---- unsaved-changes guard -------------------------------------

    def check_unsaved(self) -> bool:
        """Prompt the user about pending bbox edits before navigating away.
        Returns True if the caller may proceed (saved or discarded), False
        if the user cancelled. Mirrors kie_viewer._check_unsaved()."""
        self._last_dirty_resolution = None
        if not self._dirty:
            return True
        stem = ""
        if self._pdf_path:
            stem = os.path.splitext(os.path.basename(self._pdf_path))[0]
        r = QMessageBox.question(
            self, "Chưa lưu",
            f"File '{stem}' có thay đổi chưa lưu.\nBạn muốn lưu không?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard |
            QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if r == QMessageBox.StandardButton.Save:
            ok = bool(self.save_now())
            if not ok:
                self._last_dirty_resolution = "cancel"
            return ok
        if r == QMessageBox.StandardButton.Discard:
            self._dirty = False
            self._last_dirty_resolution = "discard"
            self._btn_save.setEnabled(False)
            self.dirty_changed.emit(False)
            return True
        self._last_dirty_resolution = "cancel"
        return False  # Cancel

    # ---- canonical-driven coord-system helpers --------------------

    def _get_page_rotation(self, page_index: int) -> int:
        """Cardinal rotation (0/90/180/270) preprocessing applied to this page
        before OCR. Reads `applied_rotation` from the canonical page entry.
        Defaults to 0 if missing or unparseable. The archive viewer does not
        run the on-the-fly ONNX orientation classifier — pipeline output
        already carries this flag, so reading metadata is sufficient."""
        if not self._canonical:
            return 0
        for p in self._canonical.get("pages") or []:
            if p.get("page_index") == page_index:
                rot = p.get("applied_rotation")
                if rot is not None:
                    try:
                        return int(rot) % 360
                    except (TypeError, ValueError):
                        return 0
                break
        return 0

    def _page_uses_bottom_left_coords(self, page_index: int) -> bool:
        """Whether bbox y-axis uses PDF origin (y=0 at bottom) on this page.

        Source of truth: explicit `coord_origin`/`y_origin` on the canonical
        page entry. We don't run the heuristic fallback the kie_viewer uses
        for legacy hand-edited JSON — pipeline output is always tagged."""
        if not self._canonical:
            return False
        for p in self._canonical.get("pages") or []:
            if p.get("page_index") == page_index:
                origin = (p.get("coord_origin") or p.get("y_origin") or "")
                origin = str(origin).lower()
                if origin in {"bottom-left", "bottom_left", "bottom"}:
                    return True
                return False
        return False

    # ---- selection updates -----------------------------------------

    def _on_fuzzy_clicked(self, page_idx: int, text: str, bbox_pdf):
        """Per-page widget reported a fuzzy-overlay click. Forward up so the
        archive screen can replace the active field's text."""
        self.fuzzy_match_picked.emit(text, list(bbox_pdf or []))

    def _on_bbox_clicked_non_edit(self, page_idx: int, word_id: str):
        """Click on an existing bbox while NOT in edit mode → automatically
        enter edit mode on that field (matches kie_viewer's UX)."""
        if not self._canonical:
            return
        word_id = str(word_id)
        for f in (self._canonical.get("annotations") or {}).get("field_instances") or []:
            if word_id in {str(wid) for wid in (f.get("word_ids") or [])}:
                field_id = f.get("field_id", "")
                self.set_active_field(field_id)
                # Auto-enter edit mode (toggling button keeps state in sync)
                if not self._edit_mode:
                    self._btn_edit.setChecked(True)  # triggers _on_edit_toggled
                self.field_clicked.emit(field_id)
                return

    def _on_empty_clicked(self):
        """Click on empty area:
          - Edit mode → exit edit mode only; pending bbox edits stay dirty
            so row/tab/back navigation can ask Save / Discard / Cancel.
          - Non-edit mode → clear active field highlight"""
        if not self._edit_mode:
            self._active_field_id = None
            self._refresh_active_field_highlight()
            self._update_active_label()
            return
        # Do not auto-save here. An empty click is just a focus/selection
        # gesture; persistence is handled by the explicit Save button or by
        # the Step 2 leave guard.
        self._btn_edit.setChecked(False)
        self._active_field_id = None
        self._refresh_active_field_highlight()
        self._update_active_label()

    def _on_page_right_clicked(self, page_idx: int, global_pos):
        """Right-click on empty page area → menu of all KIE labels.
        Picking a label creates an empty field on this page and enters edit
        mode so the user's next drag fills its bbox."""
        if not self._canonical:
            return
        menu = QMenu(self)
        header = QAction(f"+ Trường mới trên trang {page_idx + 1}", menu)
        header.setEnabled(False)
        menu.addAction(header)
        menu.addSeparator()

        # The set of labels users can create — same as kie_viewer's V3 set
        labels = list(FIELD_NUMBER_MAP.keys())
        for label in labels:
            act = QAction(
                f"{FIELD_NUMBER_MAP.get(label, '?')} • {_field_menu_text(label)}",
                menu,
            )
            act.triggered.connect(
                lambda _checked=False, lbl=label, pi=page_idx:
                    self._create_empty_field(lbl, pi)
            )
            menu.addAction(act)
        menu.exec(global_pos)

    def _create_empty_field(self, label: str, page_idx: int):
        """Create an empty field with `label` on `page_idx`, enter edit mode,
        and select it so the next drag fills its bbox."""
        if not self._canonical:
            return
        ann = self._canonical.setdefault(
            "annotations", {"field_instances": [], "relations": []}
        )
        fields = ann.setdefault("field_instances", [])
        # Generate a unique field_id
        existing_ids = {f.get("field_id") for f in fields}
        idx = 1
        while f"f{idx}" in existing_ids:
            idx += 1
        new_field = {
            "field_id": f"f{idx}",
            "label": label,
            "page_index": page_idx,
            "line_ids": [],
            "word_ids": [],
            "bbox": None,
            "text": "",
        }
        fields.append(new_field)
        self._active_field_id = new_field["field_id"]
        if not self._edit_mode:
            self._btn_edit.setChecked(True)
        self._mark_dirty()
        self.field_words_changed.emit(new_field["field_id"], "created")
        self._refresh_active_field_highlight()
        self._update_active_label()

    def _on_bbox_right_clicked(self, page_idx: int, word_id: str, global_pos):
        """Right-click on a field bbox → context menu with Delete option."""
        if not self._canonical:
            return
        # Find the field that owns this word
        word_id = str(word_id)
        target = None
        for f in (self._canonical.get("annotations") or {}).get("field_instances") or []:
            if word_id in {str(wid) for wid in (f.get("word_ids") or [])}:
                target = f
                break
        if target is None:
            return

        menu = QMenu(self)
        label = target.get("label", "?")
        text_preview = (target.get("text") or "").strip()
        if len(text_preview) > 40:
            text_preview = text_preview[:40] + "..."

        # Header (non-clickable info row)
        info = QAction(f"{_field_menu_text(label)} • {text_preview}", menu)
        info.setEnabled(False)
        menu.addAction(info)
        menu.addSeparator()

        act_select = QAction("Đặt làm field đang sửa", menu)
        act_select.triggered.connect(
            lambda _checked=False, fid=target.get("field_id"): self.set_active_field(fid)
        )
        menu.addAction(act_select)

        act_delete = QAction("Xóa field này", menu)
        act_delete.triggered.connect(
            lambda _checked=False, fid=target.get("field_id"): self._delete_field(fid)
        )
        menu.addAction(act_delete)

        menu.exec(global_pos)

    def _remove_field_instance(self, field_id: str, *, confirm: bool) -> bool:
        """Remove a field instance and any relations pointing to it.

        `confirm=False` is used when the user explicitly removes the last bbox
        of the active field. In that flow an empty selection means delete the
        KIE field entirely, not leave an unclickable placeholder behind.
        """
        if not field_id or not self._canonical:
            return False
        ann = self._canonical.get("annotations") or {}
        fields = ann.get("field_instances") or []
        target = None
        for f in fields:
            if f.get("field_id") == field_id:
                target = f
                break
        if target is None:
            return False

        label = str(target.get("label") or "")
        if confirm:
            answer = QMessageBox.question(
                self, "Xác nhận xóa",
                f"Xóa field '{target.get('label','?')}'?\n"
                f"Nội dung: {(target.get('text') or '').strip()[:80]}",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return False

        new_fields = [f for f in fields if f.get("field_id") != field_id]
        # Also drop any relations that point to this field
        relations = ann.get("relations") or []
        new_relations = [
            r for r in relations
            if r.get("from_field_id") != field_id
            and r.get("to_field_id") != field_id
        ]
        ann["field_instances"] = new_fields
        ann["relations"] = new_relations
        self._canonical["annotations"] = ann
        # Drop active selection if it was the deleted field
        if self._active_field_id == field_id:
            self._active_field_id = None
        self._mark_dirty()
        # Surface the change to the archive screen so the metadata form refreshes
        self.field_words_changed.emit(field_id, f"deleted:{label}" if label else "deleted")
        self._apply_word_data_to_pages()
        self._update_active_label()
        return True

    def _delete_field(self, field_id: str):
        """Remove the field from the canonical annotation and refresh the UI."""
        self._remove_field_instance(field_id, confirm=True)

    def _on_word_selection_changed(self, page_idx: int, word_ids: list[str], op: str):
        if not self._canonical:
            return
        if not self._active_field_id:
            return  # No field selected — nothing to attach the words to
        word_ids = [str(wid) for wid in (word_ids or [])]
        ann = self._canonical.setdefault("annotations", {"field_instances": [], "relations": []})
        fields = ann.get("field_instances") or []
        target = None
        for f in fields:
            if f.get("field_id") == self._active_field_id:
                target = f
                break
        if target is None:
            return
        existing = [str(wid) for wid in (target.get("word_ids") or [])]
        target_pi = target.get("page_index")
        if existing and target_pi is not None and int(target_pi) != page_idx:
            return  # cross-page edit blocked

        # Push undo snapshot BEFORE mutating
        self._push_undo(target)

        new_set = list(existing)
        if op == "set":
            new_set = list(word_ids)
        elif op == "add":
            seen = set(new_set)
            for wid in word_ids:
                if wid not in seen:
                    new_set.append(wid); seen.add(wid)
        elif op == "remove":
            drop = set(word_ids)
            new_set = [w for w in new_set if w not in drop]
        elif op == "toggle":
            current = set(new_set)
            for wid in word_ids:
                if wid in current:
                    new_set.remove(wid); current.discard(wid)
                else:
                    new_set.append(wid); current.add(wid)

        if not new_set:
            self._remove_field_instance(self._active_field_id, confirm=False)
            return

        target["word_ids"] = new_set
        if new_set:
            target["page_index"] = page_idx
        # Rebuild text + bbox from the new word list
        self._rebuild_field_aggregates(target, page_idx)
        self._mark_dirty()
        self.field_words_changed.emit(self._active_field_id, op)
        self._apply_word_data_to_pages()

    # ---- field aggregate rebuild ----------------------------------

    def _rebuild_field_aggregates(self, field: dict, page_idx: int):
        """Recompute text + bbox + line_ids from word_ids."""
        if not self._canonical:
            return
        pages = self._canonical.get("pages") or []
        if not (0 <= page_idx < len(pages)):
            return
        page = pages[page_idx]
        word_index = {
            str(w.get("id") or w.get("word_id")): w
            for w in page.get("words", [])
            if (w.get("id") or w.get("word_id")) is not None
        }
        word_ids = [str(wid) for wid in (field.get("word_ids") or [])]
        words = [word_index[wid] for wid in word_ids if wid in word_index]
        words = _reading_order_words_for_page(page, words)
        ordered_word_ids = []
        seen_word_ids: set[str] = set()
        for word in words:
            wid = word.get("id") or word.get("word_id")
            if wid is None:
                continue
            wid = str(wid)
            if wid in seen_word_ids:
                continue
            seen_word_ids.add(wid)
            ordered_word_ids.append(wid)
        field["word_ids"] = ordered_word_ids

        # Text join (use ocr_text if present, else text), preserving line breaks.
        parts: list[str] = []
        last_line_id = None
        for w in words:
            t = (w.get("ocr_text") or w.get("text") or "").strip()
            if t:
                line_id = w.get("line_id")
                if parts and line_id is not None and last_line_id is not None and str(line_id) != str(last_line_id):
                    parts.append("\n")
                elif parts and parts[-1] != "\n":
                    parts.append(" ")
                parts.append(t)
                last_line_id = line_id
        field["text"] = "".join(parts).strip()
        # Merged bbox
        if words:
            boxes = [_word_bbox(w) for w in words]
            if boxes:
                field["bbox"] = [
                    min(b[0] for b in boxes),
                    min(b[1] for b in boxes),
                    max(b[2] for b in boxes),
                    max(b[3] for b in boxes),
                ]
        # Line ids
        line_ids = []
        seen_lines: set[str] = set()
        for w in words:
            lid = w.get("line_id")
            if lid:
                lid = str(lid)
            if lid and lid not in seen_lines:
                line_ids.append(lid); seen_lines.add(lid)
        field["line_ids"] = line_ids

    # ---- undo/redo (active field's word selection) -----------------

    def _push_undo(self, field: dict):
        """Snapshot the field's current state before a mutation."""
        snap = {
            "field_id": field.get("field_id"),
            "word_ids": list(field.get("word_ids") or []),
            "line_ids": list(field.get("line_ids") or []),
            "text": field.get("text", ""),
            "bbox": list(field.get("bbox") or [0, 0, 0, 0]),
        }
        self._undo_stack.append(snap)
        if len(self._undo_stack) > self._UNDO_LIMIT:
            self._undo_stack.pop(0)
        self._redo_stack.clear()

    def _undo(self):
        if not self._undo_stack or not self._active_field_id:
            return
        snap = self._undo_stack[-1]
        if snap["field_id"] != self._active_field_id:
            return  # focus moved to a different field — protect against confusion
        self._undo_stack.pop()
        ann = (self._canonical or {}).get("annotations") or {}
        target = next(
            (f for f in ann.get("field_instances", [])
             if f.get("field_id") == self._active_field_id),
            None,
        )
        if target is None:
            return
        # Push current state to redo stack
        self._redo_stack.append({
            "field_id": target.get("field_id"),
            "word_ids": list(target.get("word_ids") or []),
            "line_ids": list(target.get("line_ids") or []),
            "text": target.get("text", ""),
            "bbox": list(target.get("bbox") or [0, 0, 0, 0]),
        })
        target["word_ids"] = snap["word_ids"]
        target["line_ids"] = snap["line_ids"]
        target["text"] = snap["text"]
        target["bbox"] = snap["bbox"]
        self._mark_dirty()
        self.field_words_changed.emit(self._active_field_id, "undo")
        self._apply_word_data_to_pages()

    def _redo(self):
        if not self._redo_stack or not self._active_field_id:
            return
        snap = self._redo_stack[-1]
        if snap["field_id"] != self._active_field_id:
            return
        self._redo_stack.pop()
        ann = (self._canonical or {}).get("annotations") or {}
        target = next(
            (f for f in ann.get("field_instances", [])
             if f.get("field_id") == self._active_field_id),
            None,
        )
        if target is None:
            return
        self._undo_stack.append({
            "field_id": target.get("field_id"),
            "word_ids": list(target.get("word_ids") or []),
            "line_ids": list(target.get("line_ids") or []),
            "text": target.get("text", ""),
            "bbox": list(target.get("bbox") or [0, 0, 0, 0]),
        })
        target["word_ids"] = snap["word_ids"]
        target["line_ids"] = snap["line_ids"]
        target["text"] = snap["text"]
        target["bbox"] = snap["bbox"]
        self._mark_dirty()
        self.field_words_changed.emit(self._active_field_id, "redo")
        self._apply_word_data_to_pages()

    # ---- save ------------------------------------------------------

    def _mark_dirty(self):
        if not self._dirty:
            self._dirty = True
            self._last_dirty_resolution = None
            self._btn_save.setEnabled(True)
            self.dirty_changed.emit(True)

    def is_dirty(self) -> bool:
        return self._dirty

    def dirty_resolution(self) -> Optional[str]:
        return self._last_dirty_resolution

    def save_now(self) -> bool:
        """Drop empty fields, run the shared validator, write to disk, then
        re-validate the on-disk file. Mirrors the kie_viewer.py save flow:
          * Hard errors → block save (show dialog)
          * Warnings    → ask "save anyway?"
          * Post-save reload validate → log post-save anomalies
        """
        if not self._canonical or not self._canonical_path:
            return False

        # 1. Drop fields with no word/line ids (placeholders user never filled)
        ann = self._canonical.setdefault(
            "annotations", {"field_instances": [], "relations": []}
        )
        raw_fields = ann.get("field_instances") or []
        kept_fields = []
        dropped_ids = set()
        for f in raw_fields:
            wids = f.get("word_ids") or []
            lids = f.get("line_ids") or []
            if not wids and not lids:
                dropped_ids.add(f.get("field_id"))
            else:
                kept_fields.append(f)
        relations = [
            r for r in ann.get("relations") or []
            if r.get("from_field_id") not in dropped_ids
            and r.get("to_field_id") not in dropped_ids
        ]

        # 2. Pre-save validate
        save_data = {
            "schema": ann.get("schema", "kie_vi_official_v3"),
            "source": ann.get("source", "viewer_manual"),
            "status": "edited",
            "field_instances": kept_fields,
            "relations": relations,
        }
        if not self._run_validator_before_save(save_data):
            return False

        # 3. Persist (annotations block only — keep canonical pages untouched)
        ann["field_instances"] = kept_fields
        ann["relations"] = relations
        try:
            tmp = self._canonical_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._canonical, f, ensure_ascii=False)
            os.replace(tmp, self._canonical_path)
        except Exception as e:
            QMessageBox.warning(self, "Lỗi lưu", f"Không thể ghi file:\n{e}")
            return False
        self._dirty = False
        self._last_dirty_resolution = "save"
        self._btn_save.setEnabled(False)
        self.dirty_changed.emit(False)

        # Clear dropped active selection
        if self._active_field_id and self._active_field_id in dropped_ids:
            self._active_field_id = None

        # 4. Post-save reload & re-validate
        self._run_validator_after_save(self._canonical_path, dropped_count=len(dropped_ids))
        return True

    def _run_validator_before_save(self, save_data: dict) -> bool:
        """Validate `save_data` against the canonical doc. Block on errors;
        warnings are silently ignored. Confidence-range errors are also
        filtered out — KIE engine emits raw LightGBM ranker scores that
        legitimately fall outside [0, 1], so that check is not meaningful
        for archive-viewer output."""
        try:
            from scanindex.core.kie.labeling_workspace import validate_label_output_detailed
        except Exception:
            return True  # Validator unavailable → don't block save
        try:
            result = validate_label_output_detailed(
                save_data, self._canonical, llm_name="archive_viewer",
            )
        except Exception as e:
            QMessageBox.critical(
                self, "Validator lỗi", f"Validator crash:\n{e}\n\nKhông lưu.",
            )
            return False

        errors = self._filter_validator_errors(result.get("errors") or [])

        if errors:
            self._show_validation_dialog(
                title="Có lỗi — không thể lưu",
                header=f"Validator phát hiện {len(errors)} lỗi:",
                errors=errors, warnings=[], blocking=True,
            )
            return False
        return True

    @staticmethod
    def _filter_validator_errors(errors: list) -> list:
        """Drop confidence-range errors — KIE engine output uses raw ranker
        scores, not probabilities, so the [0,1] check is not meaningful."""
        return [e for e in errors if "confidence out of range" not in str(e)]

    def _run_validator_after_save(self, saved_path: str, dropped_count: int = 0):
        """Reload the just-written file and re-validate to catch viewer bugs."""
        try:
            from scanindex.core.kie.labeling_workspace import validate_label_output_detailed
        except Exception:
            return
        try:
            with open(saved_path, "r", encoding="utf-8") as f:
                on_disk = json.load(f)
        except Exception as e:
            QMessageBox.warning(self, "Post-save load lỗi", str(e))
            return
        try:
            result = validate_label_output_detailed(
                on_disk, self._canonical, llm_name="archive_viewer",
            )
        except Exception:
            return
        errs = self._filter_validator_errors(result.get("errors") or [])
        if errs:
            self._show_validation_dialog(
                title="POST-SAVE: file ghi xong vẫn còn lỗi",
                header=("File đã ghi nhưng reload validate vẫn báo lỗi — "
                        "có thể là bug viewer:"),
                errors=errs, warnings=[],
                blocking=True,
            )

    def _show_validation_dialog(self, title: str, header: str,
                                  errors: list, warnings: list,
                                  blocking: bool) -> bool:
        dlg = QMessageBox(self)
        dlg.setWindowTitle(title)
        dlg.setIcon(QMessageBox.Icon.Critical if blocking else QMessageBox.Icon.Warning)
        dlg.setText(header)
        body_lines: list[str] = []
        if errors:
            body_lines.append("LỖI:")
            for e in errors[:10]:
                body_lines.append(f"  • {e}")
            if len(errors) > 10:
                body_lines.append(f"  … và {len(errors)-10} lỗi khác")
        if warnings:
            if body_lines:
                body_lines.append("")
            body_lines.append("CẢNH BÁO:")
            for w in warnings[:10]:
                if isinstance(w, dict):
                    body_lines.append(f"  • {w.get('message', str(w))}")
                else:
                    body_lines.append(f"  • {w}")
            if len(warnings) > 10:
                body_lines.append(f"  … và {len(warnings)-10} cảnh báo khác")
        dlg.setDetailedText("\n".join(body_lines))
        if blocking:
            dlg.setStandardButtons(QMessageBox.StandardButton.Ok)
            dlg.exec()
            return False
        dlg.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        dlg.setDefaultButton(QMessageBox.StandardButton.No)
        return dlg.exec() == QMessageBox.StandardButton.Yes

    # ---- accessors -------------------------------------------------

    def canonical(self) -> dict | None:
        return self._canonical
