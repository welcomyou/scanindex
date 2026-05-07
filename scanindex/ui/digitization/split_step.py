"""ArchiveStep1Split — phân tách 1 file PDF dài thành nhiều văn bản.

Layout:
    ┌──────────────────────────────────────────────────────┐
    │ Toolbar: [chọn PDF] [path]      [↺ Reset] [Bước 2 →] │
    ├─────────────┬────────────────────────────────────────┤
    │ Segments    │ PDF viewer with cut gutters            │
    │ list        │   (vertical scroll)                    │
    │             │                                        │
    └─────────────┴────────────────────────────────────────┘

Behaviour:
    - On first entry (or whenever IdentityCodes is empty), prompt the user
      with `ArchiveSessionDialog`.
    - User drops / picks a PDF → loaded into `PdfSplitViewer`.
    - Every time the cut set changes, re-derive segments and refresh the
      left list.
    - On load, kick off a background thread that submits every page to the
      OCR pool. Cached page results land in `session.ocr_cache`.
    - "Chuyển bước 2" → physically split the PDF into temp dir + emit
      `request_step2(segment_paths)`.
"""
from __future__ import annotations

import os
import threading
import time
import traceback
from typing import Optional

import fitz
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import (
    QDragEnterEvent, QDropEvent, QKeySequence, QShortcut,
)
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton,
    QSizePolicy, QProgressBar, QSplitter, QVBoxLayout, QWidget,
)

from scanindex.ui.theme import (
    COLOR_ACCENT, COLOR_ACCENT_HOVER, COLOR_BG, COLOR_BORDER,
    COLOR_BORDER_DEFAULT, COLOR_ELEVATED, COLOR_GREEN, COLOR_GREEN_HOVER,
    COLOR_HOVER, COLOR_INPUT, COLOR_RED, COLOR_RED_HOVER, COLOR_SURFACE,
    COLOR_TEXT, COLOR_TEXT_MUTED, COLOR_TEXT_SECONDARY, FONT_UI,
)
from scanindex.ui.widgets.pdf_split_viewer import PdfSplitViewer
from scanindex.ui.dialogs.archive_session_dialog import ArchiveSessionDialog
from scanindex.core.digitization.session import ArchiveSession, Segment
from scanindex.infra import translations


_H = 26
_FONT = 12
_FONT_SM = 11
_RAD = 4
_FLIST_W = 220


class ArchiveStep1Split(QWidget):
    """Bước 1 — Phân tách tài liệu."""

    request_step2 = Signal(list)  # list[Segment] (segments with absolute paths in name field via session.segments)
    log_message = Signal(str)
    busy_changed = Signal(bool)
    _ocr_page_done = Signal(int, int, int)  # run_id, page_idx, done_count
    _ocr_stage = Signal(int, str, str)  # run_id, status, title
    _ocr_finished = Signal(int, str, object, str)  # run_id, ocr_pdf_path, split_result, error
    _ocr_cancelled = Signal(int)  # run_id

    def __init__(self, session: ArchiveSession, parent=None):
        super().__init__(parent)
        self.session = session
        self._ocr_thread: Optional[threading.Thread] = None
        self._ocr_cancel = threading.Event()
        self._ocr_busy = False
        self._ocr_run_id = 0
        self._ocr_started_at = 0.0
        # Spinner state for OCR progress
        self._spinner_chars = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        self._spinner_idx = 0
        # Per-source-page secrecy mark cache, keyed by 0-based page index.
        # Populated once after the canonical OCR JSON for the whole source PDF
        # exists; consulted by `_refresh_segments` to flag classified docs
        # in the segment list. Detection re-uses the rule from kie_viewer
            # (`scanindex.core.kie.inference_pipeline.detect_secrecy_mark`).
        self._page_secrecy: dict[int, str] = {}

        self.setAcceptDrops(True)
        self._setup_ui()
        self._ocr_page_done.connect(self._on_ocr_page_done)
        self._ocr_stage.connect(self._on_ocr_stage)
        self._ocr_finished.connect(self._on_ocr_finished)
        self._ocr_cancelled.connect(self._on_ocr_cancelled)
        self._setup_shortcuts()

    # ── ui ──────────────────────────────────────────────────────────

    def _setup_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._build_toolbar(outer)

        # Resizable horizontal splitter: user can drag the divider between
        # the segment list and the viewer to give the document-name column
        # more width when names get truncated.
        self._body_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._body_splitter.setChildrenCollapsible(False)
        self._body_splitter.setHandleWidth(4)
        self._body_splitter.setStyleSheet(
            f"QSplitter::handle {{ background: {COLOR_BORDER}; }}"
            f"QSplitter::handle:hover {{ background: {COLOR_ACCENT}; }}"
        )

        self._build_segment_panel(self._body_splitter)

        self._viewer = PdfSplitViewer()
        self._viewer.cut_changed.connect(self._on_cut_changed)
        self._viewer.page_count_changed.connect(self._on_page_count)
        self._body_splitter.addWidget(self._viewer)

        # Initial sizes: ~220px left, rest for viewer
        self._body_splitter.setStretchFactor(0, 0)
        self._body_splitter.setStretchFactor(1, 1)
        self._body_splitter.setSizes([_FLIST_W, 9999])

        outer.addWidget(self._body_splitter, 1)
        self._build_ocr_overlay()

        # OCR progress timer (animates spinner in toolbar status)
        self._ocr_timer = QTimer(self)
        self._ocr_timer.setInterval(120)
        self._ocr_timer.timeout.connect(self._tick_status)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "_ocr_overlay"):
            self._ocr_overlay.setGeometry(self.rect())

    def _build_ocr_overlay(self):
        self._ocr_overlay = QFrame(self)
        self._ocr_overlay.setObjectName("archiveStep1OcrOverlay")
        self._ocr_overlay.setGeometry(self.rect())
        self._ocr_overlay.setStyleSheet(f"""
            QFrame#archiveStep1OcrOverlay {{
                background: rgba(20, 20, 20, 205);
                border: none;
            }}
        """)
        layout = QVBoxLayout(self._ocr_overlay)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addStretch(1)

        card = QFrame()
        card.setFixedWidth(520)
        card.setStyleSheet(f"""
            QFrame {{
                background: {COLOR_SURFACE};
                border: 1px solid {COLOR_BORDER_DEFAULT};
                border-radius: 6px;
            }}
        """)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 14, 16, 14)
        card_layout.setSpacing(10)

        self._ocr_overlay_title = QLabel("Đang OCR file dài")
        self._ocr_overlay_title.setStyleSheet(
            f"color: {COLOR_TEXT}; font-size: 14px; font-weight: 700; "
            f"font-family: {FONT_UI}; border: none;"
        )
        card_layout.addWidget(self._ocr_overlay_title)

        self._ocr_progress = QProgressBar()
        self._ocr_progress.setRange(0, 100)
        self._ocr_progress.setValue(0)
        card_layout.addWidget(self._ocr_progress)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(5)
        self._ocr_lbl_pages = self._make_overlay_value("0/0")
        self._ocr_lbl_current = self._make_overlay_value("-")
        self._ocr_lbl_elapsed = self._make_overlay_value("0.0s")
        self._ocr_lbl_splits = self._make_overlay_value("-")
        grid.addWidget(self._make_overlay_key("OCR"), 0, 0)
        grid.addWidget(self._ocr_lbl_pages, 0, 1)
        grid.addWidget(self._make_overlay_key("Trang gần nhất"), 1, 0)
        grid.addWidget(self._ocr_lbl_current, 1, 1)
        grid.addWidget(self._make_overlay_key("Thời gian"), 2, 0)
        grid.addWidget(self._ocr_lbl_elapsed, 2, 1)
        grid.addWidget(self._make_overlay_key("Split tự động"), 3, 0)
        grid.addWidget(self._ocr_lbl_splits, 3, 1)
        card_layout.addLayout(grid)

        self._ocr_overlay_status = QLabel("Đang chuẩn bị OCR...")
        self._ocr_overlay_status.setWordWrap(True)
        self._ocr_overlay_status.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; font-size: {_FONT_SM}px; "
            f"font-family: {FONT_UI}; border: none;"
        )
        card_layout.addWidget(self._ocr_overlay_status)

        buttons = QHBoxLayout()
        buttons.addStretch()
        self._btn_cancel_ocr = QPushButton("Hủy")
        self._btn_cancel_ocr.setFixedHeight(_H)
        self._btn_cancel_ocr.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_cancel_ocr.setStyleSheet(
            f"QPushButton {{ background: {COLOR_RED}; border: none; "
            f"border-radius: {_RAD}px; color: white; font-size: {_FONT}px; "
            f"font-family: {FONT_UI}; font-weight: 600; padding: 0 18px; }} "
            f"QPushButton:hover {{ background: {COLOR_RED_HOVER}; }}"
        )
        self._btn_cancel_ocr.clicked.connect(self._on_cancel_ocr_clicked)
        buttons.addWidget(self._btn_cancel_ocr)
        card_layout.addLayout(buttons)

        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(card)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addStretch(2)

        self._ocr_overlay.hide()

    def _make_overlay_key(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; font-size: {_FONT_SM}px; "
            f"font-family: {FONT_UI}; border: none;"
        )
        return label

    def _make_overlay_value(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet(
            f"color: {COLOR_TEXT}; font-size: {_FONT_SM}px; "
            f"font-family: {FONT_UI}; font-weight: 600; border: none;"
        )
        return label

    def _build_toolbar(self, parent_layout: QVBoxLayout):
        bar = QFrame()
        bar.setFixedHeight(34)
        bar.setStyleSheet(
            f"QFrame {{ background: {COLOR_SURFACE}; "
            f"border-bottom: 1px solid {COLOR_BORDER}; }}"
        )
        h = QHBoxLayout(bar)
        h.setContentsMargins(8, 0, 8, 0)
        h.setSpacing(6)

        # File label
        self._file_label = QLineEdit()
        self._file_label.setReadOnly(True)
        self._file_label.setPlaceholderText(
            translations.get_text("arc_step1_drop_hint"))
        self._file_label.setFixedHeight(_H)
        self._file_label.setStyleSheet(
            f"QLineEdit {{ background: {COLOR_INPUT}; "
            f"border: 1px solid {COLOR_BORDER}; border-radius: {_RAD}px; "
            f"color: {COLOR_TEXT}; font-size: {_FONT_SM}px; "
            f"font-family: {FONT_UI}; padding: 0 6px; }}"
        )
        h.addWidget(self._file_label, 1)

        self._btn_pick = self._make_text_btn(translations.get_text("arc_step1_pick_pdf"))
        self._btn_pick.clicked.connect(self._on_pick_pdf)
        h.addWidget(self._btn_pick)

        # OCR status indicator
        self._status_label = QLabel("")
        self._status_label.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; font-size: {_FONT_SM}px; "
            f"font-family: {FONT_UI};"
        )
        h.addWidget(self._status_label)

        h.addSpacing(8)

        self._btn_reset = self._make_text_btn(translations.get_text("arc_step1_reset"))
        self._btn_reset.clicked.connect(self._on_reset)
        h.addWidget(self._btn_reset)

        self._btn_next = QPushButton(translations.get_text("arc_step1_to_step2"))
        self._btn_next.setFixedHeight(_H)
        self._btn_next.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_next.setStyleSheet(
            f"QPushButton {{ background: {COLOR_GREEN}; border: none; "
            f"border-radius: {_RAD}px; color: white; font-size: {_FONT}px; "
            f"font-family: {FONT_UI}; font-weight: 600; padding: 0 14px; }} "
            f"QPushButton:hover {{ background: {COLOR_GREEN_HOVER}; }} "
            f"QPushButton:disabled {{ background: {COLOR_BORDER_DEFAULT}; "
            f"color: {COLOR_TEXT_MUTED}; }}"
        )
        self._btn_next.setEnabled(False)
        self._btn_next.clicked.connect(self._on_to_step2)
        h.addWidget(self._btn_next)

        parent_layout.addWidget(bar)

    def _build_segment_panel(self, parent: QSplitter):
        panel = QWidget()
        # Sensible bounds — the splitter handles the actual width
        panel.setMinimumWidth(140)
        panel.setStyleSheet(f"background: {COLOR_SURFACE};")
        v = QVBoxLayout(panel)
        v.setContentsMargins(6, 4, 6, 4)
        v.setSpacing(4)

        hdr = QHBoxLayout()
        hdr.setSpacing(4)
        title = QLabel(translations.get_text("arc_step1_segments"))
        title.setStyleSheet(
            f"font-size: {_FONT_SM}px; font-weight: 600; "
            f"color: {COLOR_TEXT_SECONDARY}; font-family: {FONT_UI}; "
            f"text-transform: uppercase; letter-spacing: 0.5px;"
        )
        hdr.addWidget(title)
        hdr.addStretch()
        self._lbl_count = QLabel("0")
        self._lbl_count.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; font-size: {_FONT_SM}px; "
            f"font-family: {FONT_UI};"
        )
        hdr.addWidget(self._lbl_count)
        v.addLayout(hdr)

        self._seg_list = QListWidget()
        self._seg_list.setStyleSheet(f"""
            QListWidget {{
                background: {COLOR_BG};
                border: 1px solid {COLOR_BORDER};
                border-radius: {_RAD}px;
                outline: none;
                font-size: {_FONT_SM}px;
                font-family: {FONT_UI};
                color: {COLOR_TEXT};
            }}
            QListWidget::item {{ padding: 3px 6px; }}
            QListWidget::item:selected {{ background: {COLOR_ACCENT}; color: white; }}
            QListWidget::item:hover:!selected {{ background: {COLOR_ELEVATED}; }}
        """)
        # Click on a document → scroll viewer to that document's start page.
        # `currentItemChanged` also fires on Up/Down keyboard navigation so
        # arrow keys move the selection AND advance the PDF viewer to that
        # segment's first page (mirroring click behaviour).
        self._seg_list.itemClicked.connect(self._on_segment_clicked)
        self._seg_list.currentItemChanged.connect(
            lambda cur, _prev: self._on_segment_clicked(cur)
        )
        v.addWidget(self._seg_list, 1)

        parent.addWidget(panel)

    def _on_segment_clicked(self, item: QListWidgetItem | None):
        if item is None:
            return
        start_page = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(start_page, int):
            self._viewer.scroll_to_page(start_page)

    def _make_text_btn(self, text: str) -> QPushButton:
        b = QPushButton(text)
        b.setFixedHeight(_H)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setStyleSheet(
            f"QPushButton {{ background: {COLOR_ELEVATED}; "
            f"border: 1px solid {COLOR_BORDER_DEFAULT}; border-radius: {_RAD}px; "
            f"color: {COLOR_TEXT_SECONDARY}; font-size: {_FONT_SM}px; "
            f"font-family: {FONT_UI}; padding: 0 10px; }} "
            f"QPushButton:hover {{ background: {COLOR_HOVER}; color: {COLOR_TEXT}; }}"
        )
        return b

    def _setup_shortcuts(self):
        QShortcut(QKeySequence("Ctrl+Z"), self, activated=self._viewer.undo)
        QShortcut(QKeySequence("Ctrl+Y"), self, activated=self._viewer.redo)
        QShortcut(QKeySequence("Ctrl+Shift+Z"), self, activated=self._viewer.redo)
        # Up / Down: jump exactly one page in the viewer (Qt's default scroll
        # increment is one line — too small for page-by-page navigation).
        # Attach to the viewer (not the whole step) so the segment list keeps
        # its own up/down item navigation when it has focus.
        sc_up = QShortcut(QKeySequence(Qt.Key.Key_Up), self._viewer,
                          activated=self._viewer.page_up)
        sc_down = QShortcut(QKeySequence(Qt.Key.Key_Down), self._viewer,
                            activated=self._viewer.page_down)
        sc_up.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        sc_down.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)

    # ── public API ──────────────────────────────────────────────────

    def ensure_identity_or_prompt(self) -> bool:
        """Ask for identity codes if missing. Returns True if identity is set
        (either already or after the user accepts the dialog), False if the
        user cancels."""
        if self.session.identity and self.session.identity.is_complete():
            return True
        dlg = ArchiveSessionDialog(
            initial=self.session.identity,
            seed_for_unstructured=self.session.session_id,
            parent=self,
        )
        if dlg.exec():
            self.session.identity = dlg.result_codes()
            # If a PDF is already loaded, refresh segment names with the new codes
            if self.session.source_pdf:
                self._refresh_segments()
            return True
        return False

    # ── drag & drop ─────────────────────────────────────────────────

    def dragEnterEvent(self, event: QDragEnterEvent):
        if self._ocr_busy:
            event.ignore()
            return
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                p = url.toLocalFile()
                if p and p.lower().endswith(".pdf"):
                    event.acceptProposedAction()
                    return
        super().dragEnterEvent(event)

    def dropEvent(self, event: QDropEvent):
        if self._ocr_busy:
            event.ignore()
            return
        if not event.mimeData().hasUrls():
            return super().dropEvent(event)
        for url in event.mimeData().urls():
            p = url.toLocalFile()
            if p and p.lower().endswith(".pdf"):
                self._load_pdf(p)
                event.acceptProposedAction()
                return

    # ── slots ───────────────────────────────────────────────────────

    def _on_pick_pdf(self):
        if self._ocr_busy:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, translations.get_text("arc_step1_pick_pdf_title"),
            "", "PDF Files (*.pdf)",
        )
        if path:
            self._load_pdf(path)

    def _on_reset(self):
        if self._ocr_busy:
            return
        if not self.session.source_pdf:
            return
        ok = QMessageBox.question(
            self, translations.get_text("arc_confirm_title"),
            translations.get_text("arc_step1_reset_confirm"),
        )
        if ok != QMessageBox.StandardButton.Yes:
            return
        self.force_reset()

    def force_reset(self) -> None:
        """Wipe Step 1 state without prompting. Safe to call regardless of
        whether OCR is in flight or a source PDF is loaded."""
        self._cancel_ocr()
        self.session.reset_for_new_source("", 0)
        # Drop the dossier identity too — otherwise the next "Add file"
        # would silently reuse the previously-entered Mã định danh /
        # phông / mục lục / hồ sơ instead of prompting the operator.
        self.session.identity = None
        self._viewer.clear()
        self._file_label.clear()
        self._status_label.clear()
        self._page_secrecy = {}
        self._refresh_segments()
        self._btn_next.setEnabled(False)
        self._btn_reset.setEnabled(False)

    def _on_cut_changed(self, page_idx: int, on: bool):
        if self._ocr_busy:
            return
        if on:
            self.session.cut_points.add(page_idx)
        else:
            self.session.cut_points.discard(page_idx)
        self._refresh_segments()

    def _on_page_count(self, n: int):
        self._update_status()
        # Enable "next" once the PDF is loaded (segments default to 1)
        self._btn_next.setEnabled(n > 0 and not self._ocr_busy)

    def _on_to_step2(self):
        if self._ocr_busy:
            return
        if not self.session.source_pdf:
            return
        if not self.ensure_identity_or_prompt():
            return
        segs = self.session.compute_segments()
        if not segs:
            return
        msg = translations.get_text("arc_step1_to_step2_confirm").format(n=len(segs))
        ok = QMessageBox.question(
            self, translations.get_text("arc_confirm_title"), msg,
        )
        if ok != QMessageBox.StandardButton.Yes:
            return
        # Materialize physical split files in the session temp dir
        try:
            seg_paths = self._physically_split(segs)
        except Exception as e:
            QMessageBox.critical(
                self, translations.get_text("arc_error_title"),
                f"Split failed: {e}",
            )
            return
        # Stash absolute paths back into segments before handing off
        for s, p in zip(segs, seg_paths):
            s.name = os.path.basename(p)
        self.session.segments = segs
        self.request_step2.emit(segs)

    # ── PDF loading ─────────────────────────────────────────────────

    def _load_pdf(self, path: str):
        if self._ocr_busy:
            return
        if not self.ensure_identity_or_prompt():
            return
        # Cancel any in-flight OCR run on the previous file
        self._cancel_ocr()

        try:
            with fitz.open(path) as d:
                page_count = len(d)
        except Exception as e:
            QMessageBox.critical(
                self, translations.get_text("arc_error_title"),
                f"Cannot open PDF: {e}",
            )
            return

        self.session.reset_for_new_source(path, page_count)
        self._file_label.setText(path)
        # Drop the previous file's secrecy cache so segments don't briefly
        # carry the old document's red flags before OCR rebuilds the cache.
        self._page_secrecy = {}
        self._show_ocr_progress(page_count)
        # Force the overlay to paint before the viewer's synchronous page
        # rendering (one fitz.get_pixmap per page) blocks the GUI thread.
        # Without this, large PDFs (>50 pages) show no feedback for several
        # seconds after Add File.
        QApplication.processEvents()
        n = self._viewer.load_pdf(path)
        if n <= 0:
            self._hide_ocr_progress()
            self.session.reset_for_new_source("", 0)
            self._btn_reset.setEnabled(False)
            self._btn_next.setEnabled(False)
            QMessageBox.critical(
                self,
                translations.get_text("arc_error_title"),
                "Cannot render PDF.",
            )
            return
        self._viewer.set_interaction_enabled(False)
        self._refresh_segments()

        # Kick off background OCR
        self._start_background_ocr(path, n)

    def _refresh_segments(self):
        if not self.session.source_pdf:
            self._seg_list.clear()
            self._lbl_count.setText("0")
            return
        segs = self.session.compute_segments()
        self._seg_list.clear()
        from PySide6.QtGui import QBrush, QColor
        for s in segs:
            page_range = (f"({s.start_page + 1})"
                          if s.page_count() == 1
                          else f"({s.start_page + 1}-{s.end_page + 1})")
            secrecy = self._page_secrecy.get(int(s.start_page))
            if secrecy:
                # Mirror kie_viewer's classified styling: lock glyph + red
                # foreground + tooltip with the matched keyword.
                label = f"🔒 {s.name}  {page_range}  ({secrecy})"
            else:
                label = f"{s.name}  {page_range}"
            item = QListWidgetItem(label, self._seg_list)
            # Stash start_page on the item so click handler can jump there
            item.setData(Qt.ItemDataRole.UserRole, int(s.start_page))
            if secrecy:
                item.setForeground(QBrush(QColor("#dc2626")))
                item.setToolTip(f"Văn bản mật: {secrecy}\n{s.name}  {page_range}")
            else:
                item.setToolTip(f"{s.name}  {page_range}")
        self._lbl_count.setText(str(len(segs)))

    def _build_page_secrecy_cache(self, canonical_json_path: str):
        """Run `detect_secrecy_mark` on every source page and cache hits by
        page index. Each segment's first page is later looked up here in
        `_refresh_segments`. Best-effort — silent on failure so OCR results
        still display even if classification import broke."""
        self._page_secrecy = {}
        if not canonical_json_path or not os.path.exists(canonical_json_path):
            return
        try:
            from scanindex.core.kie.inference_pipeline import detect_secrecy_mark
        except Exception:
            return
        try:
            import json as _json
            with open(canonical_json_path, "r", encoding="utf-8") as f:
                canonical = _json.load(f)
        except Exception:
            return
        pages = canonical.get("pages") or []
        for p in pages:
            try:
                idx = p.get("page_index")
            except Exception:
                continue
            if idx is None:
                continue
            # `detect_secrecy_mark` only inspects the page whose
            # `page_index == 0`. To re-use it per source page we feed it a
            # mini-doc containing just that page, relabelled as page 0 —
            # that's exactly the "this page is the doc's first page" frame
            # each segment sits in.
            clone = dict(p)
            clone["page_index"] = 0
            try:
                kw = detect_secrecy_mark({"pages": [clone]})
            except Exception:
                kw = None
            if kw:
                self._page_secrecy[int(idx)] = kw

    # ── physical split ──────────────────────────────────────────────

    def _physically_split(self, segs: list[Segment]) -> list[str]:
        """Use PyMuPDF to extract each segment's pages into its own PDF in
        the session temp dir. Returns the list of output paths."""
        import fitz as _f
        sub = self.session.step1_split_dir()
        out_paths: list[str] = []
        with _f.open(self.session.source_pdf) as src:
            for s in segs:
                dst_path = os.path.join(sub, s.name)
                dst = _f.open()
                dst.insert_pdf(src, from_page=s.start_page, to_page=s.end_page)
                dst.save(dst_path, deflate=True, garbage=4)
                dst.close()
                out_paths.append(dst_path)
        return out_paths

    # OCR overlay ------------------------------------------------------------

    def _show_ocr_progress(self, page_count: int):
        self._ocr_busy = True
        self.busy_changed.emit(True)
        self._ocr_started_at = time.monotonic()
        self._btn_pick.setEnabled(False)
        self._btn_reset.setEnabled(False)
        self._btn_next.setEnabled(False)
        self._seg_list.setEnabled(False)
        self._viewer.set_interaction_enabled(False)
        self._ocr_progress.setRange(0, max(1, page_count))
        self._ocr_progress.setValue(0)
        self._ocr_lbl_pages.setText(f"0/{page_count}")
        self._ocr_lbl_current.setText("-")
        self._ocr_lbl_elapsed.setText("0.0s")
        self._ocr_lbl_splits.setText("-")
        self._set_overlay_status("Đang OCR từng trang. Có thể hủy nếu chọn nhầm file.")
        self._ocr_overlay_title.setText("Đang OCR file dài")
        self._ocr_overlay.setGeometry(self.rect())
        self._ocr_overlay.raise_()
        self._ocr_overlay.show()

    def _hide_ocr_progress(self):
        self._ocr_busy = False
        self.busy_changed.emit(False)
        self._ocr_overlay.hide()
        self._btn_pick.setEnabled(True)
        self._btn_reset.setEnabled(bool(self.session.source_pdf))
        has_pdf = bool(self.session.source_pdf and self._viewer.page_count() > 0)
        self._btn_next.setEnabled(has_pdf)
        self._seg_list.setEnabled(True)
        self._viewer.set_interaction_enabled(True)

    def _set_overlay_status(self, text: str, *, title: str | None = None):
        if title:
            self._ocr_overlay_title.setText(title)
        self._ocr_overlay_status.setText(text)

    def _on_cancel_ocr_clicked(self):
        self._cancel_ocr(user_cancel=True)

    def _on_ocr_page_done(self, run_id: int, page_idx: int, done_count: int):
        if run_id != self._ocr_run_id or not self._ocr_busy:
            return
        total = max(1, self.session.source_page_count)
        self._ocr_progress.setValue(min(done_count, total))
        self._ocr_lbl_pages.setText(f"{min(done_count, total)}/{total}")
        self._ocr_lbl_current.setText(str(page_idx + 1))
        self._ocr_lbl_elapsed.setText(f"{time.monotonic() - self._ocr_started_at:.1f}s")
        self._viewer.scroll_to_page(page_idx)
        # Status line stays generic — page progress already shown by the
        # progress bar + "OCR  X/Y" + "Trang gần nhất" rows above.

    def _on_ocr_stage(self, run_id: int, status: str, title: str):
        if run_id != self._ocr_run_id or not self._ocr_busy:
            return
        self._set_overlay_status(status, title=title or None)

    def _on_ocr_finished(self, run_id: int, ocr_pdf_path: str, split_result: object, error: str):
        if run_id != self._ocr_run_id:
            return
        self._ocr_timer.stop()
        if error:
            self._hide_ocr_progress()
            self._status_label.setText("")
            QMessageBox.critical(
                self,
                translations.get_text("arc_error_title"),
                f"OCR/split failed: {error}",
            )
            return

        json_path = f"{ocr_pdf_path}.json" if ocr_pdf_path else ""
        self.session.step1_ocr_pdf_path = ocr_pdf_path
        self.session.step1_ocr_json_path = json_path if os.path.exists(json_path) else None

        result = split_result if isinstance(split_result, dict) else {}
        starts = [int(p) for p in result.get("start_pages", [0]) if isinstance(p, int)]
        starts = sorted({p for p in starts if 0 <= p < self.session.source_page_count})
        if 0 not in starts:
            starts.insert(0, 0)

        ui_t0 = time.monotonic()
        if self._viewer.page_count() != self.session.source_page_count:
            if ocr_pdf_path and os.path.exists(ocr_pdf_path):
                self.log_message.emit("[step1-ui] source viewer missing; loading OCR PDF for review")
                self._viewer.load_pdf(ocr_pdf_path)
        else:
            self.log_message.emit("[step1-ui] kept source PDF viewer; skipped full OCR PDF re-render")
        self._viewer.set_interaction_enabled(False)

        self.session.cut_points = set(starts[1:])
        self.session.doc_start_predictions = result.get("pages", [])
        self._viewer.set_cut_points(self.session.cut_points)
        # Build the per-page secrecy cache once on the freshly assembled
        # canonical JSON so every refresh of the segment list (including the
        # ones triggered by user cut-toggling) can look up classification
        # without re-reading the JSON.
        self._build_page_secrecy_cache(json_path)
        self._refresh_segments()
        self._ocr_lbl_splits.setText(str(len(starts)))
        self._set_overlay_status("Đã OCR xong và tự động gợi ý tách văn bản.", title="Hoàn tất")
        self._update_status()
        self._hide_ocr_progress()
        if starts:
            self._viewer.scroll_to_page(starts[-1])
        self.log_message.emit(
            f"[step1-ui] applied {len(starts)} split suggestion(s) in {time.monotonic() - ui_t0:.1f}s"
        )

    def _on_ocr_cancelled(self, run_id: int):
        if run_id != self._ocr_run_id:
            return
        self._ocr_timer.stop()
        self._hide_ocr_progress()
        self._status_label.setText("")

    # ── background OCR ──────────────────────────────────────────────

    def _start_background_ocr(self, path: str, page_count: int):
        if page_count <= 0:
            return
        self._ocr_run_id += 1
        run_id = self._ocr_run_id
        self._ocr_cancel = threading.Event()
        cancel = self._ocr_cancel
        session = self.session
        run_temp_dir = session.temp_dir()

        def worker():
            try:
                from scanindex.core.ocr import direct_engine as direct_ocr_engine
                warm_thread = None

                def warm_splitter_models():
                    t0 = time.monotonic()
                    try:
                        from scanindex.core.digitization import page_splitter as archive_page_splitter
                        archive_page_splitter.load_model()
                        archive_page_splitter.load_signer_model()
                        self.log_message.emit(
                            f"[step1-splitter] warmed LightGBM models in {time.monotonic() - t0:.1f}s"
                        )
                    except Exception as e:
                        self.log_message.emit(f"[step1-splitter] warmup failed: {e}")

                warm_thread = threading.Thread(
                    target=warm_splitter_models,
                    name="archive-step1-splitter-warmup",
                    daemon=True,
                )
                warm_thread.start()

                ar_list = []
                for pi in range(page_count):
                    if cancel.is_set() or run_id != self._ocr_run_id:
                        self._ocr_cancelled.emit(run_id)
                        return
                    try:
                        ar = direct_ocr_engine.submit_page(path, pi)
                        ar_list.append((pi, ar))
                    except Exception as e:
                        self.log_message.emit(f"[step1-ocr] submit failed: {e}")
                        self._ocr_finished.emit(run_id, "", {}, str(e))
                        return

                done = 0
                for pi, ar in ar_list:
                    if cancel.is_set() or run_id != self._ocr_run_id:
                        self._ocr_cancelled.emit(run_id)
                        return
                    try:
                        _, page_result = ar.get(timeout=180)
                    except Exception as e:
                        page_result = None
                        self.log_message.emit(f"[step1-ocr] page {pi}: {e}")
                    if page_result is not None and not cancel.is_set():
                        if session.source_pdf == path and run_id == self._ocr_run_id:
                            session.cache_page(pi, page_result)
                    done += 1
                    self._ocr_page_done.emit(run_id, pi, done)

                if cancel.is_set() or run_id != self._ocr_run_id:
                    self._ocr_cancelled.emit(run_id)
                    return

                self._ocr_stage.emit(
                    run_id,
                    "OCR xong. Đang dựng PDF OCR và phát hiện trang đầu...",
                    "Đang hoàn tất",
                )
                if cancel.is_set() or run_id != self._ocr_run_id:
                    self._ocr_cancelled.emit(run_id)
                    return
                os.makedirs(run_temp_dir, exist_ok=True)
                ocr_pdf_path = os.path.join(run_temp_dir, "_step1_source_ocr.pdf")
                page_results = {
                    pi: cached
                    for pi in range(page_count)
                    for cached in [session.get_cached_page(pi)]
                    if cached is not None
                }
                assemble_t0 = time.monotonic()
                ok, msg = direct_ocr_engine.assemble_pdf_from_page_results(
                    path,
                    ocr_pdf_path,
                    page_results,
                    source_document_path=path,
                    update_callback=lambda m, lvl="info": self.log_message.emit(str(m)),
                    canonical_profile="layoutlmv3_runtime",
                    include_layout_analysis=False,
                )
                if not ok:
                    self._ocr_finished.emit(run_id, "", {}, msg or "assemble failed")
                    return
                self.log_message.emit(
                    f"[step1] assembled cached OCR PDF/JSON in {time.monotonic() - assemble_t0:.1f}s"
                )
                if cancel.is_set() or run_id != self._ocr_run_id:
                    self._ocr_cancelled.emit(run_id)
                    return

                split_result = {}
                try:
                    self._ocr_stage.emit(
                        run_id,
                        "Đang phát hiện trang đầu bằng LightGBM...",
                        "Đang tách văn bản",
                    )
                    if warm_thread is not None and warm_thread.is_alive():
                        self.log_message.emit("[step1-splitter] waiting for LightGBM warmup...")
                        while warm_thread.is_alive():
                            if cancel.is_set() or run_id != self._ocr_run_id:
                                self._ocr_cancelled.emit(run_id)
                                return
                            warm_thread.join(timeout=0.2)
                    from scanindex.core.digitization import page_splitter as archive_page_splitter
                    split_t0 = time.monotonic()
                    split_result = archive_page_splitter.predict_doc_starts(
                        f"{ocr_pdf_path}.json",
                        threshold=0.50,
                    )
                    self.log_message.emit(
                        "[step1-splitter] doc_start predicted "
                        f"{len(split_result.get('start_pages', []))} start(s) on "
                        f"{len(split_result.get('pages', []))} page(s) in "
                        f"{time.monotonic() - split_t0:.2f}s"
                    )
                except Exception as e:
                    self.log_message.emit(f"[step1-splitter] failed: {e}")
                    split_result = {"start_pages": [0], "pages": []}
                self._ocr_finished.emit(run_id, ocr_pdf_path, split_result, "")
            except Exception as e:
                self.log_message.emit(f"[step1-ocr] crashed: {e}\n{traceback.format_exc()}")
                self._ocr_finished.emit(run_id, "", {}, str(e))

        self._ocr_thread = threading.Thread(target=worker, name="archive-step1-ocr",
                                            daemon=True)
        self._ocr_thread.start()
        self._ocr_timer.start()

    def _cancel_ocr(self, user_cancel: bool = False):
        self._ocr_cancel.set()
        self._ocr_timer.stop()
        self._status_label.setText("")
        if user_cancel:
            self._ocr_run_id += 1
            self._hide_ocr_progress()
            self.session.reset_for_new_source("", 0)
            self._viewer.clear()
            self._file_label.clear()
            self._refresh_segments()
            self._btn_reset.setEnabled(False)
            self._btn_next.setEnabled(False)
            self.log_message.emit("[step1] OCR cancelled.")

    def _tick_status(self):
        self._spinner_idx = (self._spinner_idx + 1) % len(self._spinner_chars)
        self._update_status()
        if (self.session.source_pdf
                and self.session.all_pages_cached()):
            self._set_overlay_status(
                "OCR xong. Đang dựng PDF OCR và phát hiện trang đầu...",
                title="Đang hoàn tất",
            )

    def _update_status(self):
        if not self.session.source_pdf:
            self._status_label.setText("")
            return
        total = self.session.source_page_count
        done = self.session.cached_page_count()
        if done < total:
            ch = self._spinner_chars[self._spinner_idx]
            self._status_label.setText(
                f"{ch} OCR ngầm: {done}/{total} trang"
            )
            self._status_label.setStyleSheet(
                f"color: {COLOR_ACCENT}; font-size: {_FONT_SM}px; "
                f"font-family: {FONT_UI};"
            )
        else:
            self._status_label.setText(f"✓ OCR xong {total}/{total} trang")
            self._status_label.setStyleSheet(
                f"color: {COLOR_GREEN}; font-size: {_FONT_SM}px; "
                f"font-family: {FONT_UI};"
            )
