"""ArchiveContainer — vỏ "Số hóa lưu trữ" chứa 3 bước.

The container owns the shared `ArchiveSession`, hosts a top step-bar that
selects between the 3 sub-screens, and forwards the same Qt signal API the
old monolithic `ArchiveTab` exposed (so `main_window.py` integrations keep
working with minimal change).

Bước 1: Phân tách (Step 1 — split a long PDF into named segments)
Bước 2: Trích xuất KIE (Step 2 — OCR + correction + KIE)
Bước 3: Ký số (Step 3 — placeholder)

Cross-step rules:
- If user goes back to Step 1 and resubmits to Step 2, the container cancels
  any in-flight Step 2 run and resets the document list.
- The `archive_tab` attribute aliases to the Step 2 widget so legacy slots
  in main_window can call `.set_documents()`, `.update_doc_status()`, etc.
"""
import os
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QPushButton, QSizePolicy, QStackedWidget,
    QVBoxLayout, QWidget,
)

from scanindex.ui.theme import (
    COLOR_ACCENT, COLOR_ACCENT_HOVER, COLOR_BORDER, COLOR_BORDER_DEFAULT, COLOR_ELEVATED,
    COLOR_HOVER, COLOR_SURFACE, COLOR_TEXT, COLOR_TEXT_MUTED,
    COLOR_TEXT_SECONDARY, FONT_UI,
)
from scanindex.ui.digitization.split_step import ArchiveStep1Split
from scanindex.ui.digitization.extraction_step import ArchiveStep2Kie
from scanindex.ui.digitization.signing_step import ArchiveStep3Sign
from scanindex.core.digitization.session import ArchiveSession
from scanindex.infra import translations


_STEP_BAR_H = 36


class _StepBar(QFrame):
    """Slim segmented control: 'Bước 1 | Bước 2 | Bước 3'."""
    step_clicked = Signal(int)  # 0 / 1 / 2

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(_STEP_BAR_H)
        self.setStyleSheet(
            f"QFrame {{ background: {COLOR_SURFACE}; "
            f"border-bottom: 1px solid {COLOR_BORDER}; }}"
        )
        h = QHBoxLayout(self)
        h.setContentsMargins(8, 4, 8, 4)
        h.setSpacing(4)

        self._buttons = []
        keys = ["arc_step1_title", "arc_step2_title", "arc_step3_title"]
        for i, k in enumerate(keys):
            b = QPushButton(translations.get_text(k))
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
            b.setMinimumWidth(140)
            b.setFixedHeight(_STEP_BAR_H - 8)
            b.clicked.connect(lambda _checked=False, idx=i: self.step_clicked.emit(idx))
            self._buttons.append(b)
            h.addWidget(b)

        h.addStretch()
        self._restyle()

    def set_active(self, idx: int):
        for i, b in enumerate(self._buttons):
            b.setChecked(i == idx)
        self._restyle()

    def update_texts(self):
        keys = ["arc_step1_title", "arc_step2_title", "arc_step3_title"]
        for b, k in zip(self._buttons, keys):
            b.setText(translations.get_text(k))

    def _restyle(self):
        for b in self._buttons:
            if b.isChecked():
                b.setStyleSheet(f"""
                    QPushButton {{
                        background: {COLOR_ACCENT};
                        border: 1px solid {COLOR_ACCENT_HOVER};
                        border-radius: 4px;
                        color: white;
                        font-size: 12px;
                        font-family: {FONT_UI};
                        font-weight: 600;
                        padding: 0 14px;
                    }}
                """)
            else:
                b.setStyleSheet(f"""
                    QPushButton {{
                        background: transparent;
                        border: 1px solid {COLOR_BORDER_DEFAULT};
                        border-radius: 4px;
                        color: {COLOR_TEXT_SECONDARY};
                        font-size: 12px;
                        font-family: {FONT_UI};
                        padding: 0 14px;
                    }}
                    QPushButton:hover {{
                        background: {COLOR_ELEVATED};
                        color: {COLOR_TEXT};
                        border-color: {COLOR_ACCENT};
                    }}
                """)


class ArchiveContainer(QWidget):
    """Top-level widget for the "Số hóa lưu trữ" function."""

    # Signals re-emitted from the Step 2 child so main_window wiring stays
    # the same as the old ArchiveTab.
    browse_input_clicked = Signal()
    process_clicked = Signal()
    stop_clicked = Signal()
    field_label_clicked = Signal(str)

    # Step 3 terminal actions — both independent of each other.
    export_external_clicked = Signal()  # write Excel + final PDFs to user folder
    import_kho_clicked = Signal()       # import dossier into internal Kho

    # New signals
    step1_segments_ready = Signal(list)  # emitted with list[Segment] when user moves to step 2
    log_message = Signal(str)

    def __init__(self, icons=None, parent=None):
        super().__init__(parent)
        self._icons = icons or {}
        self.session = ArchiveSession()
        self._step1_busy = False

        self._setup_ui()
        self._bridge_signals()

    # ── ui ─────────────────────────────────────────────────────────

    def _setup_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._step_bar = _StepBar()
        self._step_bar.step_clicked.connect(self._on_step_clicked)
        outer.addWidget(self._step_bar)

        self._stack = QStackedWidget()
        self._step1 = ArchiveStep1Split(self.session)
        self._step2 = ArchiveStep2Kie(icons=self._icons)
        self._step3 = ArchiveStep3Sign(session=self.session)
        self._stack.addWidget(self._step1)
        self._stack.addWidget(self._step2)
        self._stack.addWidget(self._step3)
        outer.addWidget(self._stack, 1)

        # Default landing screen is Step 1
        self._step_bar.set_active(0)
        self._stack.setCurrentIndex(0)

    def _bridge_signals(self):
        # Re-export Step 2's signals on the container surface
        self._step2.browse_input_clicked.connect(self.browse_input_clicked.emit)
        self._step2.process_clicked.connect(self.process_clicked.emit)
        self._step2.stop_clicked.connect(self.stop_clicked.emit)
        self._step2.field_label_clicked.connect(self.field_label_clicked.emit)
        self._step2.log_message.connect(self.log_message.emit)

        self._step1.request_step2.connect(self._on_step1_to_step2)
        self._step1.log_message.connect(self.log_message.emit)
        self._step1.busy_changed.connect(self._on_step1_busy)
        self._step3.refresh_requested.connect(self._prepare_step3)
        self._step3.log_message.connect(self.log_message.emit)
        self._step3.export_clicked.connect(self.export_external_clicked.emit)
        self._step3.import_kho_clicked.connect(self.import_kho_clicked.emit)

    # ── attribute aliasing for legacy callers ──────────────────────

    @property
    def archive_tab(self):
        """Legacy alias — old code calls `archive_container.archive_tab.x`."""
        return self._step2

    # Forward common Step-2 methods so existing main_window code keeps working
    def set_input_folder(self, p): self._step2.set_input_folder(p)
    def set_output_folder(self, p): self._step2.set_output_folder(p)
    def get_input_folder(self): return self._step2.get_input_folder()
    def get_output_folder(self): return self._step2.get_output_folder()
    def set_processing_state(self, r): self._step2.set_processing_state(r)
    def set_progress(self, c, t): self._step2.set_progress(c, t)
    def set_documents(self, docs, default_status="Pending"):
        self._step2.set_documents(docs, default_status=default_status)
    def update_doc_status(self, i, s): self._step2.update_doc_status(i, s)
    def get_documents(self): return self._step2.get_documents()
    def refresh_current_doc(self): self._step2.refresh_current_doc()
    def update_texts(self):
        self._step_bar.update_texts()
        self._step2.update_texts()
        self._step3.update_texts()

    # ── step navigation ────────────────────────────────────────────

    def _on_step_clicked(self, idx: int):
        if self._step1_busy:
            return
        current_idx = self._stack.currentIndex()
        if idx != 1 and not self.confirm_unsaved_before_leave():
            self._step_bar.set_active(current_idx)
            return
        if idx == 2:
            self._prepare_step3()
        # Always allow free navigation between steps; cancellation is only
        # required when re-running Step 1 → Step 2 with a new segment list.
        self._stack.setCurrentIndex(idx)
        self._step_bar.set_active(idx)

    def _on_step1_busy(self, busy: bool):
        self._step1_busy = busy
        self._step_bar.setEnabled(not busy)

    def _on_step1_to_step2(self, segments: list):
        """User clicked "Chuyển bước 2" in Step 1. Switch tabs, set source
        mode, and let the host (main_window) start the pipeline by listening
        for `step1_segments_ready`."""
        if not self.confirm_unsaved_before_leave():
            return
        # Switch immediately so the user sees the load spinners
        self._stack.setCurrentIndex(1)
        self._step_bar.set_active(1)
        # Configure Step 2 in "from-step1" mode
        self._step2.set_source_mode("step1")
        self._step2.set_input_folder(
            translations.get_text("arc_step2_source_step1_value").format(
                n=len(segments))
        )
        # Build the document list — paths come from the session temp dir.
        # Carry over the per-page secrecy mark Step 1 detected so Step 2
        # can flag classified files (red row) before KIE runs.
        seg_dir = self.session.step1_split_dir()
        secrecy_cache = getattr(self._step1, "_page_secrecy", {}) or {}
        docs = []
        for s in segments:
            secrecy = secrecy_cache.get(int(s.start_page))
            source_pages = list(s.page_indices())
            docs.append({
                "pdf_path": os.path.join(seg_dir, s.name),
                "path": os.path.join(seg_dir, s.name),
                "output_path": None,
                "ocr_path": None,
                "json_path": None,
                "metadata": {},
                "zones": {},
                "status": "OCR...",
                "_step1_segment": s,            # carry the source page range
                "_step1_source_pdf": self.session.source_pdf,
                "_step1_source_pages": source_pages,
                "_secrecy": secrecy,            # None | "MẬT" | "TỐI MẬT" | …
            })
        self._step2.set_documents(docs, default_status="OCR...")
        self.step1_segments_ready.emit(docs)

    def _prepare_step3(self):
        """Refresh Step 3 from the current Step 2 document state."""
        try:
            docs = self._step2.get_documents()
        except Exception:
            docs = []
        try:
            out_dir = self._step2.get_output_folder()
        except Exception:
            out_dir = ""
        self._step3.set_documents(docs, default_output_dir=out_dir)

    def goto_step(self, idx: int):
        """Public helper for external callers."""
        self._on_step_clicked(idx)

    def confirm_unsaved_before_leave(self) -> bool:
        """Prompt when the active archive workflow screen has pending edits."""
        try:
            dirty = bool(self._step2.has_unsaved_changes())
        except Exception as e:
            self.log_message.emit(f"Archive: unsaved-change check failed: {e}")
            return False
        if self._stack.currentIndex() != 1 and not dirty:
            return True
        try:
            return bool(self._step2.confirm_unsaved_before_leave())
        except Exception as e:
            self.log_message.emit(f"Archive: unsaved-change prompt failed: {e}")
            return False

    def reset_workflow(self) -> None:
        """Wipe every step's UI state, drop the temp dir, and return to
        Step 1. Caller (main_window) is responsible for cancelling any
        external Step 2 pipeline runner *before* invoking this — the
        container only owns step UI state and the per-session temp dir."""
        # Step 1 — cancel in-flight OCR + clear viewer/segments.
        try:
            self._step1.force_reset()
        except Exception:
            pass
        # Step 2 — viewer + document list + form + fuzzy + nav.
        try:
            self._step2.reset()
        except Exception:
            pass
        # Step 3 — stop signing worker + drop document list.
        try:
            self._step3.cleanup()
        except Exception:
            pass
        try:
            self._step3.set_documents([], default_output_dir="")
        except Exception:
            pass
        # Per-session temp dir.
        try:
            self.session.cleanup_temp()
        except Exception:
            pass
        # Land back on Step 1 with a clean slate.
        self._stack.setCurrentIndex(0)
        self._step_bar.set_active(0)

    # ── lifecycle ──────────────────────────────────────────────────

    def cleanup(self):
        """Called on app exit — drop the per-session temp dir."""
        try:
            self._step3.cleanup()
        except Exception:
            pass
        try:
            self.session.cleanup_temp()
        except Exception:
            pass
