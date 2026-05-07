"""
ComparisonDialog — Side-by-side diff viewer for raw vs corrected OCR text.
Ported from show_comparison_window() + comp_render() + comp_reprocess() in ocr_app.py.
"""
import os
import re
import difflib

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout,
    QTextEdit, QTableWidget, QTableWidgetItem, QPushButton,
    QLabel, QSplitter, QHeaderView, QAbstractItemView
)
from PySide6.QtGui import (
    QFont, QTextCharFormat, QTextCursor, QColor, QWheelEvent
)
from PySide6.QtCore import Qt, Signal

from scanindex.ui.theme import (
    COLOR_BG, COLOR_SURFACE, COLOR_ELEVATED, COLOR_TEXT, COLOR_TEXT_SECONDARY,
    COLOR_ACCENT, COLOR_ACCENT_HOVER, COLOR_ORANGE, COLOR_GREEN, COLOR_RED,
    COLOR_RED_HOVER, COLOR_BORDER, FONT_UI, FONT_MONO, SP, RADIUS_MD
)
from scanindex.infra import translations


class ComparisonDialog(QDialog):
    """Side-by-side comparison of raw and corrected OCR text with word-level diff."""

    reprocess_requested = Signal(int, str)  # (file_idx, final_text)

    def __init__(self, filename: str, original: str, corrected: str,
                 file_idx: int = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(translations.get_text("win_comparison", filename))
        self.resize(1400, 800)
        self.setStyleSheet(f"QDialog {{ background: {COLOR_BG}; }}")

        self._filename = filename
        self._file_idx = file_idx
        self._font_size = 12
        self._is_exporting = False

        # Calculate diff state
        self._state = self._calculate_state(original, corrected)

        self._setup_ui()
        self._render()

    def _calculate_state(self, original: str, corrected: str) -> dict:
        original = original or ""
        corrected = corrected or ""
        original = original.replace("\r\n", "\n").replace("\r", "\n")
        corrected = corrected.replace("\r\n", "\n").replace("\r", "\n")

        pat = re.compile(r'(\S+|\s+)')
        tok_o = [t for t in pat.split(original) if t]
        tok_c = [t for t in pat.split(corrected) if t]

        matcher = difflib.SequenceMatcher(None, tok_o, tok_c)
        segments = []

        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            seg = {
                "tag": tag,
                "orig_tokens": tok_o[i1:i2],
                "corr_tokens": tok_c[j1:j2],
                "status": "match" if tag == "equal" else "active",
                "orig_idx_start": None,
                "corr_idx_start": None,
            }
            segments.append(seg)

        return {
            "original_text": original,
            "initial_corrected_text": corrected,
            "segments": segments,
            "idx": self._file_idx,
            "filename": self._filename,
        }

    def _setup_ui(self):
        main_layout = QGridLayout(self)
        main_layout.setContentsMargins(SP[2], SP[2], SP[2], SP[2])
        main_layout.setSpacing(SP[1])
        main_layout.setColumnStretch(0, 1)
        main_layout.setColumnStretch(1, 1)
        main_layout.setColumnStretch(2, 0)
        main_layout.setRowStretch(1, 1)

        # --- Headers ---
        lbl_raw = QLabel(translations.get_text("comp_raw_results"))
        lbl_raw.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl_raw.setStyleSheet(
            f"color: {COLOR_ORANGE}; font-weight: bold; font-size: 12px; background: transparent;"
        )
        main_layout.addWidget(lbl_raw, 0, 0)

        lbl_corr = QLabel(translations.get_text("comp_corrected_results"))
        lbl_corr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl_corr.setStyleSheet(
            f"color: {COLOR_GREEN}; font-weight: bold; font-size: 12px; background: transparent;"
        )
        main_layout.addWidget(lbl_corr, 0, 1)

        # Right header with reprocess button
        hdr_right = QHBoxLayout()
        lbl_words = QLabel(translations.get_text("comp_correction_words"))
        lbl_words.setStyleSheet(
            f"color: {COLOR_ACCENT}; font-weight: bold; font-size: 12px; background: transparent;"
        )
        hdr_right.addWidget(lbl_words)
        hdr_right.addStretch()

        self.btn_action = QPushButton(translations.get_text("comp_reprocess"))
        self.btn_action.setProperty("cssClass", "primary")
        self.btn_action.setFixedSize(90, 30)
        self.btn_action.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_action.clicked.connect(self._on_action_click)
        hdr_right.addWidget(self.btn_action)

        main_layout.addLayout(hdr_right, 0, 2)

        # --- Text Widgets ---
        self.txt_orig = QTextEdit()
        self.txt_orig.setReadOnly(True)
        self.txt_orig.setFont(QFont(FONT_MONO, self._font_size))
        self.txt_orig.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.txt_orig.setStyleSheet(
            f"background: {COLOR_SURFACE}; border-radius: {RADIUS_MD}px;"
        )
        main_layout.addWidget(self.txt_orig, 1, 0)

        self.txt_corr = QTextEdit()
        self.txt_corr.setReadOnly(True)
        self.txt_corr.setFont(QFont(FONT_MONO, self._font_size))
        self.txt_corr.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.txt_corr.setStyleSheet(
            f"background: {COLOR_SURFACE}; border-radius: {RADIUS_MD}px;"
        )
        main_layout.addWidget(self.txt_corr, 1, 1)

        # --- Correction Lists Panel ---
        lists_widget = QSplitter(Qt.Orientation.Vertical)
        lists_widget.setFixedWidth(350)

        # Active corrections table
        active_container = QVBoxLayout()
        active_widget = self._make_list_container(active_container)

        lbl_active = QLabel(translations.get_text("comp_active_corrections"))
        lbl_active.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; font-weight: bold; font-size: 11px; background: transparent;"
        )
        active_container.addWidget(lbl_active)

        self.table_active = self._make_table()
        active_container.addWidget(self.table_active)

        lists_widget.addWidget(active_widget)

        # Restore candidates table
        restore_container = QVBoxLayout()
        restore_widget = self._make_list_container(restore_container)

        lbl_restore = QLabel(translations.get_text("comp_restore_candidates"))
        lbl_restore.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; font-weight: bold; font-size: 11px; background: transparent;"
        )
        restore_container.addWidget(lbl_restore)

        self.table_restore = self._make_table()
        restore_container.addWidget(self.table_restore)

        lists_widget.addWidget(restore_widget)

        main_layout.addWidget(lists_widget, 1, 2)

        # --- Scroll sync ---
        self.txt_orig.verticalScrollBar().valueChanged.connect(
            self.txt_corr.verticalScrollBar().setValue
        )
        self.txt_corr.verticalScrollBar().valueChanged.connect(
            self.txt_orig.verticalScrollBar().setValue
        )

        # --- Zoom via event filter ---
        self.txt_orig.viewport().installEventFilter(self)
        self.txt_corr.viewport().installEventFilter(self)

    def _make_list_container(self, layout) -> QVBoxLayout:
        from PySide6.QtWidgets import QWidget
        w = QWidget()
        layout_obj = layout
        w.setLayout(layout_obj)
        layout_obj.setContentsMargins(0, 0, 0, 0)
        layout_obj.setSpacing(2)
        return w

    def _make_table(self) -> QTableWidget:
        table = QTableWidget(0, 3)
        table.setHorizontalHeaderLabels([
            translations.get_text("tree_original"),
            translations.get_text("tree_corrected"),
            translations.get_text("tree_action"),
        ])
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(2, 50)
        table.verticalHeader().setVisible(False)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.cellClicked.connect(self._on_table_click)
        return table

    # --- Rendering ---

    def _render(self, keep_scroll: bool = False):
        scroll_pos = self.txt_orig.verticalScrollBar().value() if keep_scroll else 0

        # Block scroll sync during render
        self.txt_orig.verticalScrollBar().valueChanged.disconnect()
        self.txt_corr.verticalScrollBar().valueChanged.disconnect()

        self.txt_orig.clear()
        self.txt_corr.clear()
        self.table_active.setRowCount(0)
        self.table_restore.setRowCount(0)

        # Text formats for highlights
        fmt_del = QTextCharFormat()
        fmt_del.setBackground(QColor("#b91c1c"))
        fmt_del.setForeground(QColor("white"))

        fmt_add = QTextCharFormat()
        fmt_add.setBackground(QColor("#15803d"))
        fmt_add.setForeground(QColor("white"))

        fmt_del_line = QTextCharFormat()
        fmt_del_line.setBackground(QColor("#3d2020"))

        fmt_add_line = QTextCharFormat()
        fmt_add_line.setBackground(QColor("#1a3d1a"))

        fmt_normal = QTextCharFormat()

        cursor_o = self.txt_orig.textCursor()
        cursor_c = self.txt_corr.textCursor()
        cursor_o.movePosition(QTextCursor.MoveOperation.Start)
        cursor_c.movePosition(QTextCursor.MoveOperation.Start)

        segments = self._state["segments"]
        # Map segment index -> table row for click handling
        self._active_seg_map = {}  # table_row -> seg_idx
        self._restore_seg_map = {}

        for i, seg in enumerate(segments):
            tag = seg["tag"]
            status = seg["status"]
            orig_text = "".join(seg["orig_tokens"])
            corr_text = "".join(seg["corr_tokens"])

            # Record positions
            seg["orig_idx_start"] = cursor_o.position()
            seg["corr_idx_start"] = cursor_c.position()

            # --- Raw view ---
            if tag == "equal":
                cursor_o.insertText(orig_text, fmt_normal)
            elif tag in ("delete", "replace"):
                fmt = fmt_del if status == "active" else fmt_normal
                cursor_o.insertText(orig_text, fmt)
            # insert: nothing in raw

            # --- Corrected view ---
            if tag == "equal":
                cursor_c.insertText(orig_text, fmt_normal)
            elif tag == "delete":
                if status == "reverted":
                    cursor_c.insertText(orig_text, fmt_normal)
            elif tag == "replace":
                if status == "active":
                    cursor_c.insertText(corr_text, fmt_add)
                else:
                    cursor_c.insertText(orig_text, fmt_normal)
            elif tag == "insert":
                if status == "active":
                    cursor_c.insertText(corr_text, fmt_add)

            # --- Tables ---
            o_clean = orig_text.strip()
            c_clean = corr_text.strip()
            if not o_clean and not c_clean:
                continue

            if status == "active" and tag in ("replace", "delete", "insert"):
                row = self.table_active.rowCount()
                self.table_active.insertRow(row)
                self.table_active.setItem(row, 0, QTableWidgetItem(o_clean))
                self.table_active.setItem(row, 1, QTableWidgetItem(c_clean))
                btn_item = QTableWidgetItem("\u274c")
                btn_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table_active.setItem(row, 2, btn_item)
                self._active_seg_map[row] = i
            elif status == "reverted" and tag in ("replace", "delete", "insert"):
                row = self.table_restore.rowCount()
                self.table_restore.insertRow(row)
                self.table_restore.setItem(row, 0, QTableWidgetItem(c_clean))
                self.table_restore.setItem(row, 1, QTableWidgetItem(o_clean))
                btn_item = QTableWidgetItem("\u274c")
                btn_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table_restore.setItem(row, 2, btn_item)
                self._restore_seg_map[row] = i

        # Reconnect scroll sync
        self.txt_orig.verticalScrollBar().valueChanged.connect(
            self.txt_corr.verticalScrollBar().setValue
        )
        self.txt_corr.verticalScrollBar().valueChanged.connect(
            self.txt_orig.verticalScrollBar().setValue
        )

        if keep_scroll:
            self.txt_orig.verticalScrollBar().setValue(scroll_pos)

    def _on_table_click(self, row, col):
        sender = self.sender()
        if col == 2:
            # Toggle status
            if sender is self.table_active:
                seg_idx = self._active_seg_map.get(row)
                if seg_idx is not None:
                    self._state["segments"][seg_idx]["status"] = "reverted"
            elif sender is self.table_restore:
                seg_idx = self._restore_seg_map.get(row)
                if seg_idx is not None:
                    self._state["segments"][seg_idx]["status"] = "active"
            self._render(keep_scroll=True)
        elif col in (0, 1):
            # Scroll to segment
            seg_map = self._active_seg_map if sender is self.table_active else self._restore_seg_map
            seg_idx = seg_map.get(row)
            if seg_idx is not None:
                seg = self._state["segments"][seg_idx]
                pos = seg.get("orig_idx_start", 0)
                cursor = self.txt_orig.textCursor()
                cursor.setPosition(pos)
                self.txt_orig.setTextCursor(cursor)
                self.txt_orig.ensureCursorVisible()

    def _on_action_click(self):
        """Reconstruct final text from segments and emit reprocess signal."""
        segments = self._state["segments"]
        final_parts = []
        for seg in segments:
            tag = seg["tag"]
            status = seg["status"]
            if tag == "equal":
                final_parts.append("".join(seg["orig_tokens"]))
            elif tag == "delete":
                if status == "reverted":
                    final_parts.append("".join(seg["orig_tokens"]))
            elif tag == "replace":
                if status == "active":
                    final_parts.append("".join(seg["corr_tokens"]))
                else:
                    final_parts.append("".join(seg["orig_tokens"]))
            elif tag == "insert":
                if status == "active":
                    final_parts.append("".join(seg["corr_tokens"]))

        final_text = "".join(final_parts)
        self.reprocess_requested.emit(self._file_idx, final_text)

    def refresh_state(self, original: str, corrected: str):
        """Re-calculate diff state and re-render (called after save)."""
        self._state = self._calculate_state(original, corrected)
        self._render()

    # --- Zoom ---

    def eventFilter(self, obj, event):
        if isinstance(event, QWheelEvent):
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                delta = 1 if event.angleDelta().y() > 0 else -1
                self._update_font_size(delta)
                return True
            else:
                # Sync scroll via wheel
                scroll_delta = -event.angleDelta().y() // 120
                bar = self.txt_orig.verticalScrollBar()
                bar.setValue(bar.value() + scroll_delta * 3)
                return True
        return super().eventFilter(obj, event)

    def _update_font_size(self, delta: int):
        new_size = max(8, min(72, self._font_size + delta))
        if new_size != self._font_size:
            self._font_size = new_size
            font = QFont(FONT_MONO, self._font_size)
            self.txt_orig.setFont(font)
            self.txt_corr.setFont(font)
