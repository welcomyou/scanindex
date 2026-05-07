"""Đo độ chính xác OCR.

Flow:
  1. User bấm "Tải file mẫu" -> copy ocr_groundtruth/groundtruth.pdf về máy.
  2. User OCR file đó bằng phần mềm khác, được PDF có text layer.
  3. User upload PDF kết quả lên, hệ thống so cả hai phía với GT (docx) và báo.
"""
from __future__ import annotations

import os
import shutil
import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QMouseEvent
from PySide6.QtWidgets import (
    QFileDialog, QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QTextEdit, QVBoxLayout, QWidget,
)

from scanindex.ui.model_manager import GROUP_CORE_OCR
from scanindex.ui.screens.screen_base import ScreenContent
from scanindex.ui.theme import (
    COLOR_ACCENT, COLOR_ACCENT_HOVER, COLOR_BG, COLOR_BORDER, COLOR_INPUT,
    COLOR_PANEL, COLOR_SURFACE, COLOR_TEXT, COLOR_TEXT_SECONDARY,
    FONT_MONO, FONT_UI, RADIUS_MD, SP,
)


class _PdfDropZone(QFrame):
    """Vùng kéo thả + bấm để chọn 1 file PDF."""

    file_selected = Signal(str)

    _PROMPT = "📄  Kéo thả file PDF vào đây, hoặc bấm để chọn..."

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(90)
        self._path: str | None = None
        self._hover = False

        v = QVBoxLayout(self)
        v.setContentsMargins(SP[3], SP[3], SP[3], SP[3])
        v.setSpacing(SP[1])
        self._label = QLabel(self._PROMPT, alignment=Qt.AlignmentFlag.AlignCenter)
        self._label.setWordWrap(True)
        v.addWidget(self._label, 1)

        self._apply_style()

    def path(self) -> str | None:
        return self._path

    def set_path(self, path: str) -> None:
        self._path = path
        self._label.setText(f"✓  {os.path.basename(path)}")
        self._apply_style()
        self.file_selected.emit(path)

    def clear(self) -> None:
        self._path = None
        self._label.setText(self._PROMPT)
        self._apply_style()

    def _apply_style(self) -> None:
        if self._path:
            border = COLOR_ACCENT
            border_style = "solid"
            text_color = COLOR_TEXT
            bg = COLOR_INPUT
        else:
            border = COLOR_ACCENT if self._hover else COLOR_BORDER
            border_style = "dashed"
            text_color = COLOR_TEXT_SECONDARY
            bg = COLOR_PANEL if self._hover else COLOR_INPUT
        self.setStyleSheet(
            f"_PdfDropZone, QFrame {{ background: {bg};"
            f" border: 2px {border_style} {border};"
            f" border-radius: {RADIUS_MD}px; }}"
        )
        self._label.setStyleSheet(
            f"color: {text_color}; background: transparent;"
            f" font: 13px '{FONT_UI}';"
        )

    # ── events ────────────────────────────────────────────────────────

    def dragEnterEvent(self, event: QDragEnterEvent):
        if not event.mimeData().hasUrls():
            event.ignore()
            return
        for url in event.mimeData().urls():
            p = url.toLocalFile()
            if p and p.lower().endswith(".pdf"):
                self._hover = True
                self._apply_style()
                event.acceptProposedAction()
                return
        event.ignore()

    def dragLeaveEvent(self, event):
        self._hover = False
        self._apply_style()
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent):
        self._hover = False
        if not event.mimeData().hasUrls():
            self._apply_style()
            return
        for url in event.mimeData().urls():
            p = url.toLocalFile()
            if p and p.lower().endswith(".pdf"):
                self.set_path(p)
                event.acceptProposedAction()
                return
        self._apply_style()

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            path, _ = QFileDialog.getOpenFileName(
                self, "Chọn PDF đã OCR", "", "PDF (*.pdf)"
            )
            if path:
                self.set_path(path)
        super().mousePressEvent(event)

    def enterEvent(self, event):
        if not self._path:
            self._hover = True
            self._apply_style()
        super().enterEvent(event)

    def leaveEvent(self, event):
        if not self._path:
            self._hover = False
            self._apply_style()
        super().leaveEvent(event)


class AccuracyScreen(ScreenContent):
    """So sánh OCR của phần mềm khác với phần mềm này (cùng đo bằng GT chung)."""

    log_message = Signal(str, str)  # (message, level)
    _result_ready = Signal(str)     # report text (or error message)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setStyleSheet(f"background: {COLOR_BG};")
        self._user_pdf: str | None = None
        self._busy = False
        self._cancel_event = threading.Event()
        self._build_ui()
        # Qt auto-routes signals across threads via QueuedConnection,
        # so emitting from the worker thread re-enters the GUI thread here.
        self._result_ready.connect(self._on_result_ready)

    def required_models(self) -> list[str]:
        # Pool warm-up tốn ~2 phút (4 workers × spawn Python + load 142MB DLL/models).
        # Khi cache groundtruth.ours.txt đã fresh, không cần OCR — bỏ warm-up luôn.
        try:
            from scanindex.core.ocr.accuracy_baseline import _ours_cache_is_fresh
            if _ours_cache_is_fresh():
                return []
        except Exception:
            pass
        return [GROUP_CORE_OCR]

    def is_busy(self) -> bool:
        return self._busy

    def request_cancel(self) -> None:
        self._cancel_event.set()

    # ---------- UI ----------

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(SP[5], SP[5], SP[5], SP[5])
        layout.setSpacing(SP[3])

        intro = QLabel(
            "Quy trình đo:  ① Tải file PDF mẫu về máy.  "
            "② Dùng phần mềm OCR khác xử lý file đó.  "
            "③ Tải PDF kết quả của họ lên đây để so sánh."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; font: 13px '{FONT_UI}';"
        )
        layout.addWidget(intro)

        layout.addWidget(self._build_step1_card())
        layout.addWidget(self._build_step2_card())

        self.result = QTextEdit()
        self.result.setReadOnly(True)
        self.result.setStyleSheet(
            f"background: {COLOR_SURFACE}; color: {COLOR_TEXT};"
            f" border: 1px solid {COLOR_BORDER}; border-radius: {RADIUS_MD}px;"
            f" padding: {SP[2]}px; font: 13px '{FONT_MONO}';"
        )
        self.result.setPlaceholderText(
            "Kết quả so sánh sẽ hiện ở đây sau khi tải file lên..."
        )
        layout.addWidget(self.result, 1)

    def _card_frame(self) -> QFrame:
        card = QFrame()
        card.setStyleSheet(
            f"QFrame {{ background: {COLOR_PANEL}; border: 1px solid {COLOR_BORDER};"
            f" border-radius: {RADIUS_MD}px; }}"
        )
        return card

    def _step_title(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color: {COLOR_TEXT}; font: 600 14px '{FONT_UI}'; background: transparent;"
        )
        return lbl

    def _build_step1_card(self) -> QFrame:
        card = self._card_frame()
        v = QVBoxLayout(card)
        v.setContentsMargins(SP[3], SP[3], SP[3], SP[3])
        v.setSpacing(SP[2])
        v.addWidget(self._step_title("Bước 1 — Tải file PDF mẫu về máy"))

        hint = QLabel(
            "Đem file này cho phần mềm OCR khác xử lý, rồi quay lại bước 2."
        )
        hint.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; font: 12px '{FONT_UI}';"
            f" background: transparent;"
        )
        v.addWidget(hint)

        row = QHBoxLayout()
        self.btn_download = QPushButton("📥  Tải groundtruth.pdf về")
        self.btn_download.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_download.setStyleSheet(self._primary_btn_qss())
        self.btn_download.clicked.connect(self._download_gt)
        row.addWidget(self.btn_download)
        row.addStretch(1)
        v.addLayout(row)
        return card

    def _build_step2_card(self) -> QFrame:
        card = self._card_frame()
        v = QVBoxLayout(card)
        v.setContentsMargins(SP[3], SP[3], SP[3], SP[3])
        v.setSpacing(SP[2])
        v.addWidget(self._step_title("Bước 2 — Tải PDF đã OCR (của phần mềm khác) lên"))

        self.drop_zone = _PdfDropZone()
        self.drop_zone.file_selected.connect(self._on_file_selected)
        v.addWidget(self.drop_zone)

        self.btn_run = QPushButton("So sánh độ chính xác")
        self.btn_run.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_run.setStyleSheet(self._primary_btn_qss())
        self.btn_run.clicked.connect(self._run_clicked)
        v.addWidget(self.btn_run)
        return card

    def _primary_btn_qss(self) -> str:
        return (
            f"QPushButton {{ background: {COLOR_ACCENT}; color: white;"
            f" border: none; padding: 8px 16px; border-radius: {RADIUS_MD}px;"
            f" font: 600 13px '{FONT_UI}'; }}"
            f"QPushButton:hover {{ background: {COLOR_ACCENT_HOVER}; }}"
            f"QPushButton:disabled {{ background: #555; color: #aaa; }}"
        )

    # ---------- Actions ----------

    def _download_gt(self):
        from scanindex.core.ocr import accuracy_baseline
        src = accuracy_baseline.get_gt_pdf_path()
        if not os.path.exists(src):
            QMessageBox.warning(
                self, "Thiếu file mẫu",
                f"Không tìm thấy file mẫu tại:\n{src}",
            )
            return

        target_dir = self._default_download_dir()
        dest, _ = QFileDialog.getSaveFileName(
            self, "Lưu file mẫu",
            os.path.join(target_dir, accuracy_baseline.GT_PDF_NAME),
            "PDF (*.pdf)",
        )
        if not dest:
            return
        try:
            shutil.copyfile(src, dest)
        except OSError as e:
            QMessageBox.critical(self, "Lỗi", f"Không thể lưu file: {e}")
            return
        self.log_message.emit(f"Đã lưu file mẫu: {dest}", "info")
        QMessageBox.information(
            self, "Đã tải file mẫu",
            f"Đã lưu vào:\n{dest}\n\n"
            "Đem file này cho phần mềm OCR khác xử lý, "
            "rồi quay lại tải kết quả lên ở Bước 2.",
        )

    def _default_download_dir(self) -> str:
        for env in ("USERPROFILE", "HOME"):
            home = os.environ.get(env)
            if home:
                downloads = os.path.join(home, "Downloads")
                if os.path.isdir(downloads):
                    return downloads
                return home
        return os.getcwd()

    def _on_file_selected(self, path: str):
        # Mỗi lần drop/chọn file mới (kể cả cùng path): xoá kết quả cũ và
        # đảm bảo nút "So sánh" đang enable, để người dùng có thể chạy lại.
        self._user_pdf = path
        self.result.clear()
        if not self._busy:
            self.btn_run.setEnabled(True)

    def _run_clicked(self):
        if not self._user_pdf:
            QMessageBox.information(
                self, "Chưa có file",
                "Vui lòng kéo thả hoặc bấm vào Bước 2 để chọn PDF.",
            )
            return
        if self._busy:
            return
        self._busy = True
        self._cancel_event.clear()
        self.btn_run.setEnabled(False)
        self.btn_download.setEnabled(False)
        self.result.setPlainText("Đang xử lý...")

        thread = threading.Thread(target=self._run_worker, daemon=True)
        thread.start()

    # ---------- Worker thread ----------

    def _run_worker(self):
        try:
            from scanindex.core.ocr.accuracy_metrics import compare_against_baseline, format_report
            result = compare_against_baseline(
                self._user_pdf,
                cancel_event=self._cancel_event,
                log_cb=lambda m: self.log_message.emit(m, "info"),
            )
            self._result_ready.emit(format_report(result))
        except Exception as e:
            self.log_message.emit(f"Accuracy: {e}", "error")
            self._result_ready.emit(f"Lỗi: {e}")

    def _on_result_ready(self, text: str):
        # Slot chạy trên GUI thread (signal được auto-queue từ worker thread).
        self.result.setPlainText(text)
        self._busy = False
        self.btn_run.setEnabled(True)
        self.btn_download.setEnabled(True)
