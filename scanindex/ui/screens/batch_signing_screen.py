"""Bulk-signing tool screen.

A standalone tool under the "Công cụ" menu: pick an input folder, pick an
output folder, load the PDF list, press Start, and every PDF is digitally
signed into the output folder.

Layout (two distinct config zones):
  * **Top bar — tool's own config**: input/output folder pickers + the
    "Tự động OCR nếu phát hiện tập tin chưa OCR" option. Persisted to its
    own ``config/batch_sign_settings.json``.
  * **Body — shared signature config**: the :class:`SigningConfigPanel`
    (cert / TSA / stamp appearance / position), identical to archive Step 3
    and sharing one config file. The signing engine itself
    (``sign_single_pdf``) is reused unchanged.

When "Tự động OCR" is on, scan PDFs lacking a text layer are OCR'd (same rule
as the digitization pipeline: digital PDFs get their native text extracted,
true scans go through ScreenAI) before signing, so the "Tự tránh vùng có chữ"
option can actually find gaps.
"""
from __future__ import annotations

import json
import os
import tempfile

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QFileDialog, QFrame, QGridLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
    QProgressBar, QPushButton, QScrollArea, QSplitter, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from scanindex.ui.screens.screen_base import ScreenContent
from scanindex.ui.theme import (
    COLOR_ACCENT, COLOR_BG, COLOR_BORDER, COLOR_BORDER_DEFAULT, COLOR_ELEVATED,
    COLOR_GREEN, COLOR_GREEN_HOVER, COLOR_RED, COLOR_RED_HOVER, COLOR_SURFACE,
    COLOR_TEXT, COLOR_TEXT_MUTED, COLOR_TEXT_SECONDARY, FONT_UI,
)
from scanindex.ui.widgets.signing_config_panel import SigningConfigPanel
from scanindex.infra import translations

# Reuse the archive Step 3 signing primitives — no duplication.
from scanindex.ui.digitization.signing_step import (
    _DEPS_OK, _IMPORT_ERR,
    _SignItem, _page_count, _pdf_has_digital_signature,
    _safe_pdf_name, _unique_output_path, _resolve_page,
)
from scanindex.core.pdf.signer import (
    SIG_BOX_DEFAULT, STAMP_TEXT_BELOW, sign_single_pdf,
)

try:
    from scanindex.infra.paths import get_base_dir
except Exception:
    def get_base_dir():
        return os.getcwd()

_H = 26
_FONT_SM = 11
_RAD = 4
_SETTINGS_FILE = os.path.join(get_base_dir(), "config", "batch_sign_settings.json")


# --------------------------------------------------------------------------- #
# Worker: OCR (optional) + sign, per file
# --------------------------------------------------------------------------- #

class _BatchSignWorker(QThread):
    """Per-file: classify → (OCR if scan & enabled) → sign.

    Separate from ``_SignWorker`` (which archive Step 3 shares) so the OCR
    dependency stays out of the archive module. Progress carries a ``phase``
    ("ocr" / "sign") so the UI can show the right stage.
    """

    progress = Signal(int, int, object, str)   # done, total, result, phase
    # Per-page OCR progress during the OCR phase of one file.
    # (file_index, page_done, page_total, current_page_idx)
    ocr_page_progress = Signal(int, int, int, int)
    finished_ok = Signal(object)               # list[dict]
    failed = Signal(str)

    def __init__(
        self,
        items: list[_SignItem],
        output_dir: str,
        cert_info: dict,
        sig_box: tuple[float, float, float, float],
        custom_page: int,
        stamp_template: str,
        stamp_image_path: str = "",
        stamp_text_position: str = STAMP_TEXT_BELOW,
        tsa_url: str = "",
        enable_pdfa: bool = False,
        avoid_text_overlap: bool = True,
        auto_ocr: bool = True,
        parent=None,
    ):
        super().__init__(parent)
        self._items = list(items)
        self._output_dir = output_dir
        self._cert_info = cert_info
        self._sig_box = sig_box
        self._custom_page = custom_page
        self._stamp_template = stamp_template
        self._stamp_image_path = str(stamp_image_path or "").strip()
        self._stamp_text_position = (
            STAMP_TEXT_BELOW
            if str(stamp_text_position or "").strip() != "right"
            else "right"
        )
        self._tsa_url = str(tsa_url or "").strip()
        self._enable_pdfa = bool(enable_pdfa)
        self._avoid_text_overlap = bool(avoid_text_overlap)
        self._auto_ocr = bool(auto_ocr)
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def _classify(self, path: str) -> str:
        try:
            from scanindex.core.preprocessing.preprocessing import classify_pdf
            return classify_pdf(path)
        except Exception:
            return "digital"  # be conservative: don't OCR if we can't tell

    def _ocr_pdf_with_page_progress(
        self, file_idx: int, input_path: str, output_path: str
    ) -> tuple[bool, str | None]:
        """OCR a scan PDF with real per-page progress.

        Mirrors archive Step 1: submit every page to the OCR pool with a
        bounded in-flight queue, emit ``ocr_page_progress`` as each page
        completes, then assemble the searchable PDF from the cached page
        results. Returns ``(ok, error)``.

        Digital PDFs are handled inline by ``classify_pdf`` inside the OCR
        engine's text extractor (no real OCR), but we still route them here
        so the caller gets a searchable PDF; for those the per-page progress
        simply runs over the native-text-extraction pages.
        """
        import time as _time

        try:
            import fitz
        except Exception as exc:
            return False, str(exc)

        try:
            from scanindex.core.ocr import direct_engine
            direct_engine._get_pool()  # raises FileNotFoundError if ScreenAI absent
        except FileNotFoundError as exc:
            return False, "OCR engine chưa cài đặt: " + str(exc)
        except Exception as exc:
            return False, str(exc)

        try:
            doc = fitz.open(input_path)
            page_count = int(doc.page_count)
            doc.close()
        except Exception as exc:
            return False, f"cannot open PDF for OCR: {exc}"
        if page_count <= 0:
            return False, "PDF không có trang để OCR"

        # Bounded queue — same pattern as archive Step 1 (split_step.py).
        try:
            max_in_flight = int(
                os.environ.get("OCRTOOL_MAX_IN_FLIGHT_PAGES") or "16"
            )
        except (TypeError, ValueError):
            max_in_flight = 16
        max_in_flight = max(1, min(page_count, max_in_flight))

        page_results: dict[int, object] = {}
        pending: list[tuple[int, object, float]] = []
        next_page = 0
        done = 0

        def submit_until_full():
            nonlocal next_page
            while next_page < page_count and len(pending) < max_in_flight:
                if self._cancelled:
                    return
                pi = next_page
                next_page += 1
                try:
                    pending.append((
                        pi,
                        direct_engine.submit_page(input_path, pi),
                        _time.monotonic(),
                    ))
                except Exception:
                    # submit failed — treat as a done page with no result so
                    # the overall progress still advances.
                    done += 1
                    self.ocr_page_progress.emit(file_idx, done, page_count, pi)

        try:
            submit_until_full()
            last_done = 0
            while pending:
                if self._cancelled:
                    return False, "đã hủy"
                picked = None
                now = _time.monotonic()
                for idx, (pi, ar, submitted_at) in enumerate(pending):
                    ready_fn = getattr(ar, "ready", None)
                    ready = bool(ready_fn()) if callable(ready_fn) else idx == 0
                    timed_out = now - submitted_at >= 180.0
                    if ready or timed_out:
                        picked = idx
                        break
                if picked is None:
                    _time.sleep(0.02)  # match the OCR engine's own poll cadence
                    continue
                pi, ar, _submitted_at = pending.pop(picked)
                try:
                    _, page_result = ar.get(timeout=0.1)
                except Exception:
                    page_result = None
                if page_result is not None:
                    page_results[pi] = page_result
                done += 1
                # Throttle: only signal the UI when the done count actually
                # advances, so the main-thread event queue is not flooded with
                # redundant repaints (which freezes the UI on long scans).
                if done != last_done:
                    last_done = done
                    self.ocr_page_progress.emit(file_idx, done, page_count, pi)
                    # Yield the GIL briefly so the Qt main thread can process
                    # the queued signal + repaint the spinner. Without this,
                    # a long OCR run starves the UI event loop ("event loop
                    # stall") on big multi-page scans.
                    _time.sleep(0)
                submit_until_full()

            if self._cancelled:
                return False, "đã hủy"

            # assemble_pdf_from_page_results expects a dict {page_idx: result}
            # (it calls all_page_results.get(page_idx)). submit_page returns
            # (page_idx, result) tuples; we collected them keyed by index.
            ok, err = direct_engine.assemble_pdf_from_page_results(
                input_path,
                output_path,
                page_results,
                source_document_path=input_path,
                include_layout_analysis=False,
            )
            return bool(ok), None if ok else (err or "assemble failed")
        except FileNotFoundError as exc:
            return False, "OCR engine chưa cài đặt: " + str(exc)
        except Exception as exc:
            return False, str(exc)

    def run(self):
        try:
            os.makedirs(self._output_dir, exist_ok=True)
            total = len(self._items)
            results = []

            # Ensure the OCR pool is ready once for the whole batch.
            if self._auto_ocr:
                try:
                    from scanindex.core.ocr import direct_engine
                    direct_engine._get_pool()
                except FileNotFoundError as exc:
                    self.failed.emit(
                        "OCR engine chưa cài đặt. Tắt tùy chọn 'Tự động OCR' "
                        "hoặc cài engine ScreenAI.\n" + str(exc)
                    )
                    return
                except Exception:
                    # Non-fatal: per-file OCR will simply fail and fall back
                    # to signing the original; we keep going.
                    pass

            for idx, item in enumerate(self._items):
                if self._cancelled:
                    break
                ocr_temp = None
                pdfa_temp = None
                ocr_note = ""
                try:
                    sign_input = item.source_path

                    # --- Optional OCR so "avoid text overlap" can find gaps ---
                    if self._auto_ocr:
                        pdf_type = self._classify(item.source_path)
                        if pdf_type in ("scan_no_text", "scan_ocr_low"):
                            # Emit an "ocr phase" marker before the long work
                            # so the UI can show "Đang OCR ...".
                            self.progress.emit(idx, total, {
                                "index": idx,
                                "source_path": item.source_path,
                                "phase": "ocr_start",
                            }, "ocr")
                            fd, ocr_temp = tempfile.mkstemp(suffix="_ocr.pdf")
                            os.close(fd)
                            ok, err = self._ocr_pdf_with_page_progress(
                                idx, item.source_path, ocr_temp
                            )
                            if ok:
                                sign_input = ocr_temp
                                ocr_note = "OCR xong"
                            else:
                                # OCR failed → sign the original; note it.
                                ocr_note = f"OCR thất bại ({err}), ký bản gốc"

                    # --- PDF/A conversion (optional) ---
                    if self._enable_pdfa:
                        from scanindex.core.pdf.pdfa_converter import convert_to_pdfa
                        fd, pdfa_temp = tempfile.mkstemp(suffix="_pdfa.pdf")
                        os.close(fd)
                        ok_a, err_a = convert_to_pdfa(sign_input, pdfa_temp)
                        if ok_a:
                            sign_input = pdfa_temp
                        else:
                            raise RuntimeError(
                                f"PDF/A convert failed: {err_a}. "
                                "Fallback: ký file gốc thay vì PDF/A."
                            )

                    # --- Sign ---
                    page = _resolve_page(item, self._custom_page)
                    dst = _unique_output_path(
                        item.source_path, self._output_dir, item.display_name
                    )
                    sign_single_pdf(
                        sign_input,
                        dst,
                        self._cert_info,
                        sig_box=self._sig_box,
                        page=page,
                        reason=None,
                        location=None,
                        stamp_template=self._stamp_template,
                        tsa_url=self._tsa_url,
                        stamp_image_path=self._stamp_image_path or None,
                        stamp_text_position=self._stamp_text_position,
                        avoid_text_overlap=self._avoid_text_overlap,
                    )
                    result = {
                        "index": idx,
                        "source_path": item.source_path,
                        "output_path": dst,
                        "ok": True,
                        "error": "",
                        "page": page,
                        "ocr_note": ocr_note,
                    }
                except Exception as exc:
                    result = {
                        "index": idx,
                        "source_path": item.source_path,
                        "output_path": "",
                        "ok": False,
                        "error": str(exc),
                        "page": None,
                        "ocr_note": ocr_note,
                    }
                finally:
                    for tmp in (ocr_temp, pdfa_temp):
                        if tmp and os.path.exists(tmp):
                            try:
                                os.remove(tmp)
                            except OSError:
                                pass
                results.append(result)
                self.progress.emit(idx + 1, total, result, "sign")
            self.finished_ok.emit(results)
        except Exception as exc:
            self.failed.emit(str(exc))


# --------------------------------------------------------------------------- #
# Screen
# --------------------------------------------------------------------------- #

# Braille-character spinner sequence, same as archive Step 1 (split_step.py)
# and the main window file list — keeps the loading look consistent.
_SPINNER_CHARS = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


class BatchSigningScreen(ScreenContent):
    """Standalone bulk-signing tool: folder in → folder out."""

    log_message = Signal(str, str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._input_dir = ""
        self._output_dir = ""
        self._items: list[_SignItem] = []
        self._worker = None
        # Spinner animation state for the per-file OCR phase.
        self._spinner_idx = 0
        # Current OCR page progress for the row being OCRed, if any.
        # (page_done, page_total, file_display_name)
        self._ocr_view: tuple[int, int, str] | None = None
        # Which file row is currently being OCRed (for the spinner cell), or None.
        self._ocr_file_idx: int | None = None
        self.setStyleSheet(f"background: {COLOR_BG}; color: {COLOR_TEXT};")
        self._build_ui()
        # Hook the shared panel's status callback so TSA-probing messages
        # surface on our status label instead of being dropped.
        self._panel.status = self._status
        self._load_settings()
        # Spinner timer — animates the braille char in the status line and the
        # table cell while a file is being OCRed. Started/stopped with the run.
        self._spinner_timer = QTimer(self)
        self._spinner_timer.setInterval(120)
        self._spinner_timer.timeout.connect(self._tick_spinner)

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # --- Top bar: tool's own config (input/output dirs + OCR option) -
        self._build_io_bar(root)

        # --- Body: left = shared signature config, right = file list -------
        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        split.setStyleSheet(f"QSplitter::handle {{ background: {COLOR_BORDER}; width: 3px; }}")

        self._panel = SigningConfigPanel()
        self._panel.log_message.connect(lambda msg: self.log_message.emit(msg, "info"))
        left_scroll = _wrap_in_scroll(self._panel)
        left_scroll.setMinimumWidth(300)

        right = QWidget()
        right_l = QVBoxLayout(right)
        right_l.setContentsMargins(6, 8, 8, 8)
        right_l.setSpacing(6)
        self._build_file_section(right_l)

        split.addWidget(left_scroll)
        split.addWidget(right)
        split.setSizes([380, 760])
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        root.addWidget(split, 1)

    def _status(self, *args, **_kwargs):
        # The SigningConfigPanel calls this (set as its `status` callback)
        # during TSA connection probing — surface the message on our label.
        if args:
            self.lbl_status.setText(str(args[0]))

    def _tick_spinner(self):
        """Advance the braille-char spinner and refresh the OCR status line.

        This is the SINGLE place that repaints the spinner — driven by the
        120ms ``_spinner_timer``. Per-page signals only update ``_ocr_view``
        (the data); the timer handles the actual repaint. This keeps the main
        thread's repaint rate bounded (~8/s) regardless of how fast pages
        complete, so the UI stays responsive on long scans.
        """
        self._spinner_idx = (self._spinner_idx + 1) % len(_SPINNER_CHARS)
        ch = _SPINNER_CHARS[self._spinner_idx]
        if self._ocr_view is not None:
            done, total_pages, name = self._ocr_view
            self.lbl_status.setText(
                f"{ch} Đang OCR {done}/{total_pages} trang: {name}"
            )
            self.lbl_status.setStyleSheet(
                f"color: {COLOR_ACCENT}; font-size: {_FONT_SM}px;"
            )
            # Refresh the in-progress row cell too (cheap setText).
            file_idx = self._ocr_file_idx
            if file_idx is not None and 0 <= file_idx < len(self._items):
                cell = self.table.item(file_idx, 2)
                if cell is not None:
                    cell.setText(f"{ch} OCR {done}/{total_pages}")

    def _on_ocr_page_progress(self, file_idx: int, page_done: int,
                              page_total: int, current_page: int):
        """Worker reports each OCRed page.

        Deliberately light: only record the latest counts + which row is in
        progress. The actual spinner repaint happens in ``_tick_spinner``
        (120ms timer), so the main thread repaint rate stays bounded no matter
        how fast pages arrive. Repainting on every page signal is what made the
        UI feel frozen on long scans.
        """
        name = (
            self._items[file_idx].display_name
            if 0 <= file_idx < len(self._items) else ""
        )
        self._ocr_file_idx = file_idx
        self._ocr_view = (page_done, max(1, page_total), name)

    def _build_io_bar(self, root: QVBoxLayout):
        bar = QFrame()
        bar.setStyleSheet(
            f"QFrame {{ background: {COLOR_SURFACE}; border-bottom: 1px solid {COLOR_BORDER}; }}"
        )
        v = QVBoxLayout(bar)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(6)

        # Input folder row
        in_row = QHBoxLayout()
        in_row.setSpacing(6)
        in_lbl = QLabel("Thư mục đầu vào:")
        in_lbl.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; font-size: {_FONT_SM}px; font-weight: 600;"
        )
        in_row.addWidget(in_lbl)
        self._edit_input = QLineEdit()
        self._edit_input.setReadOnly(True)
        self._edit_input.setPlaceholderText("Chọn thư mục chứa các file PDF cần ký…")
        self._edit_input.setToolTip("Chỉ các file .pdf trong thư mục (và thư mục con) được nạp.")
        self._edit_input.setFixedHeight(_H)
        self._style_line_edit(self._edit_input)
        in_row.addWidget(self._edit_input, 1)
        self._btn_pick_input = self._button("Chọn…", "ghost")
        self._btn_pick_input.clicked.connect(self._pick_input_dir)
        in_row.addWidget(self._btn_pick_input)
        v.addLayout(in_row)

        # Output folder row
        out_row = QHBoxLayout()
        out_row.setSpacing(6)
        out_lbl = QLabel("Thư mục đầu ra:")
        out_lbl.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; font-size: {_FONT_SM}px; font-weight: 600;"
        )
        out_row.addWidget(out_lbl)
        self._edit_output = QLineEdit()
        self._edit_output.setReadOnly(True)
        self._edit_output.setPlaceholderText("Chọn thư mục sẽ chứa các file PDF đã ký…")
        self._edit_output.setFixedHeight(_H)
        self._style_line_edit(self._edit_output)
        out_row.addWidget(self._edit_output, 1)
        self._btn_pick_output = self._button("Chọn…", "ghost")
        self._btn_pick_output.clicked.connect(self._pick_output_dir)
        out_row.addWidget(self._btn_pick_output)
        v.addLayout(out_row)

        # Options row
        self.chk_ocr = QCheckBox(
            "Tự động OCR nếu phát hiện tập tin chưa OCR"
        )
        self.chk_ocr.setChecked(True)
        self.chk_ocr.setStyleSheet(
            f"QCheckBox {{ color: {COLOR_TEXT}; font-size: {_FONT_SM}px; }}"
        )
        self.chk_ocr.setToolTip(
            "Các file là ảnh scan (chưa có text layer) sẽ được OCR trước khi ký "
            "để chữ ký tránh đè lên nội dung văn bản. File PDF điện tử (đã có text) "
            "được trích text trực tiếp, không OCR. Nếu tắt, file scan sẽ ký ở đúng "
            "vị trí X/Y đã chọn (không tránh được chữ vì không có text layer)."
        )
        self.chk_ocr.stateChanged.connect(lambda *_: self._save_settings())
        v.addWidget(self.chk_ocr)

        # Action row
        act_row = QHBoxLayout()
        act_row.setSpacing(6)
        self._btn_load = self._button("Nạp danh sách PDF", "ghost")
        self._btn_load.clicked.connect(self._load_files)
        act_row.addWidget(self._btn_load)
        self._btn_open_output = self._button("Mở thư mục đã ký", "ghost")
        self._btn_open_output.clicked.connect(self._open_output_dir)
        act_row.addWidget(self._btn_open_output)
        act_row.addStretch()
        self.lbl_status = QLabel("Sẵn sàng")
        self.lbl_status.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; font-size: {_FONT_SM}px;"
        )
        act_row.addWidget(self.lbl_status)
        self.btn_sign = self._button("Bắt đầu ký số", "success")
        self.btn_sign.clicked.connect(self._start_signing)
        act_row.addWidget(self.btn_sign)
        v.addLayout(act_row)

        root.addWidget(bar)

    def _build_file_section(self, parent: QVBoxLayout):
        hdr = QHBoxLayout()
        hdr.setSpacing(4)
        title = QLabel("Danh sách PDF cần ký")
        title.setStyleSheet(
            f"font-size: {_FONT_SM}px; font-weight: 600; color: {COLOR_TEXT_SECONDARY}; "
            f"font-family: {FONT_UI}; text-transform: uppercase;"
        )
        hdr.addWidget(title)
        self.lbl_count = QLabel("0 file")
        self.lbl_count.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: {_FONT_SM}px;")
        hdr.addWidget(self.lbl_count)
        hdr.addStretch()
        parent.addLayout(hdr)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Tên tập tin", "Trang", "Trạng thái", "File đã ký"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setStyleSheet(f"""
            QTableWidget {{
                background: {COLOR_BG};
                border: 1px solid {COLOR_BORDER};
                border-radius: {_RAD}px;
                gridline-color: {COLOR_BORDER};
                color: {COLOR_TEXT};
                font-size: {_FONT_SM}px;
            }}
            QHeaderView::section {{
                background: {COLOR_SURFACE};
                color: {COLOR_TEXT_SECONDARY};
                border: none;
                border-right: 1px solid {COLOR_BORDER};
                border-bottom: 1px solid {COLOR_BORDER};
                padding: 4px 6px;
                font-size: {_FONT_SM}px;
            }}
            QTableWidget::item {{ padding: 3px 6px; }}
            QTableWidget::item:selected {{ background: {COLOR_ACCENT}; color: white; }}
        """)
        parent.addWidget(self.table, 1)

    # ---------------------------------------------------------- styling

    def _button(self, text: str, role: str = "ghost") -> QPushButton:
        b = QPushButton(text)
        b.setFixedHeight(_H)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        if role == "success":
            bg, hover, border, color, weight = COLOR_GREEN, COLOR_GREEN_HOVER, "none", "#fff", "600"
        elif role == "danger":
            bg, hover, border, color, weight = COLOR_RED, COLOR_RED_HOVER, "none", "#fff", "600"
        else:
            bg, hover, border, color, weight = "transparent", COLOR_ELEVATED, f"1px solid {COLOR_BORDER_DEFAULT}", COLOR_TEXT_SECONDARY, "400"
        b.setStyleSheet(f"""
            QPushButton {{
                background: {bg}; border: {border}; border-radius: {_RAD}px;
                color: {color}; font-size: {_FONT_SM}px; font-family: {FONT_UI};
                font-weight: {weight}; padding: 0 10px;
            }}
            QPushButton:hover {{
                background: {hover}; color: {COLOR_TEXT};
                border-color: {COLOR_ACCENT if role == "ghost" else "transparent"};
            }}
            QPushButton:disabled {{
                color: {COLOR_TEXT_MUTED}; background: {COLOR_ELEVATED};
                border-color: {COLOR_BORDER};
            }}
        """)
        return b

    def _style_line_edit(self, edit: QLineEdit):
        edit.setStyleSheet(f"""
            QLineEdit {{
                background: {COLOR_BG};
                color: {COLOR_TEXT};
                border: 1px solid {COLOR_BORDER_DEFAULT};
                border-radius: {_RAD}px;
                padding: 0 8px;
                font-size: {_FONT_SM}px;
            }}
            QLineEdit:read-only {{
                color: {COLOR_TEXT_SECONDARY};
            }}
        """)

    # ---------------------------------------------------------- settings

    def _settings_path(self) -> str:
        return os.path.abspath(_SETTINGS_FILE)

    def _load_settings(self):
        path = self._settings_path()
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return
            if "auto_ocr" in data and hasattr(self, "chk_ocr"):
                self.chk_ocr.blockSignals(True)
                self.chk_ocr.setChecked(bool(data.get("auto_ocr", True)))
                self.chk_ocr.blockSignals(False)
            in_dir = str(data.get("input_dir") or "").strip()
            if in_dir and os.path.isdir(in_dir):
                self._input_dir = in_dir
                self._edit_input.setText(in_dir)
            out_dir = str(data.get("output_dir") or "").strip()
            if out_dir:
                self._output_dir = out_dir
                self._edit_output.setText(out_dir)
        except Exception as exc:
            self.log_message.emit(
                f"Bulk signing: cannot load settings: {exc}", "warning"
            )

    def _save_settings(self):
        path = self._settings_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            data = {
                "auto_ocr": bool(self.chk_ocr.isChecked()) if hasattr(self, "chk_ocr") else True,
                "input_dir": self._input_dir,
                "output_dir": self._output_dir,
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            self.log_message.emit(
                f"Bulk signing: cannot save settings: {exc}", "warning"
            )

    # ---------------------------------------------------------- directories

    def _pick_input_dir(self):
        if self._worker and self._worker.isRunning():
            return
        folder = QFileDialog.getExistingDirectory(
            self,
            translations.localize_text("Chọn thư mục đầu vào (chứa PDF)"),
        )
        if not folder:
            return
        self._input_dir = os.path.abspath(folder)
        self._edit_input.setText(self._input_dir)
        # Default the output dir to <input>_signed beside the input, if not set.
        if not self._output_dir:
            default_out = self._input_dir.rstrip("\\/") + "_signed"
            self._output_dir = default_out
            self._edit_output.setText(self._output_dir)
        self._save_settings()
        self._load_files()

    def _pick_output_dir(self):
        folder = QFileDialog.getExistingDirectory(
            self,
            translations.localize_text("Chọn thư mục đầu ra (PDF đã ký)"),
        )
        if not folder:
            return
        self._output_dir = os.path.abspath(folder)
        self._edit_output.setText(self._output_dir)
        self._save_settings()

    def _open_output_dir(self):
        folder = self._output_dir or self._edit_output.text().strip()
        if not folder:
            QMessageBox.information(self, "Mở thư mục đã ký", "Chưa chọn thư mục đầu ra.")
            return
        try:
            os.makedirs(folder, exist_ok=True)
            os.startfile(folder)
        except Exception as exc:
            QMessageBox.information(self, "Thông báo",
                                     f"Không mở được thư mục:\n{folder}\n{exc}")

    # ---------------------------------------------------------- file loading

    def _load_files(self):
        if self._worker and self._worker.isRunning():
            return
        if not self._input_dir or not os.path.isdir(self._input_dir):
            QMessageBox.information(self, "Nạp danh sách", "Hãy chọn thư mục đầu vào trước.")
            return
        items: list[_SignItem] = []
        seen: set[str] = set()
        for root, dirs, files in os.walk(self._input_dir):
            dirs[:] = [d for d in dirs if d not in {".git", "__pycache__"}]
            for name in sorted(files):
                if not name.lower().endswith(".pdf"):
                    continue
                path = os.path.abspath(os.path.join(root, name))
                if path in seen:
                    continue
                seen.add(path)
                item = _SignItem(source_path=path, display_name=name)
                # Mark already-signed PDFs so the operator can skip them.
                try:
                    if _pdf_has_digital_signature(path):
                        item.status = "Đã ký (có sẵn)"
                except Exception:
                    pass
                items.append(item)
        self._items = items
        self._refresh_table()
        self.lbl_status.setText(
            f"Đã nạp {len(items)} file PDF từ thư mục đầu vào."
        )
        self.log_message.emit(
            f"Bulk signing: loaded {len(items)} PDF(s) from {self._input_dir}",
            "info",
        )

    def _refresh_table(self):
        self.table.setRowCount(len(self._items))
        for row, item in enumerate(self._items):
            self._set_table_item(row, 0, item.display_name, tooltip=item.source_path)
            self._set_table_item(row, 1, self._page_label(item))
            self._set_table_item(row, 2, item.status, status=item.status)
            self._set_table_item(row, 3, os.path.basename(item.output_path), tooltip=item.output_path)
        self.lbl_count.setText(f"{len(self._items)} file")

    def _set_table_item(self, row: int, col: int, text: str, tooltip: str = "", status: str = ""):
        cell = QTableWidgetItem(text or "")
        if status:
            translations.set_translatable_item_text(cell, text)
        if tooltip:
            cell.setToolTip(
                translations.localize_text(tooltip) if status else tooltip
            )
        if status:
            color = (
                COLOR_GREEN if status.startswith("Đã ký")
                else COLOR_RED if status.startswith("Lỗi")
                else COLOR_TEXT_SECONDARY
            )
            cell.setForeground(QBrush(QColor(color)))
        self.table.setItem(row, col, cell)

    def _page_label(self, item: _SignItem) -> str:
        try:
            n = _page_count(item.source_path)
        except Exception:
            n = 0
        return str(n) if n else ""

    # ---------------------------------------------------------- signing

    def _start_signing(self):
        if not _DEPS_OK:
            QMessageBox.critical(self, "Thiếu thư viện", _IMPORT_ERR)
            return
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self.btn_sign.setEnabled(False)
            self.lbl_status.setText("Đang dừng sau file hiện tại…")
            return
        if not self._input_dir or not os.path.isdir(self._input_dir):
            QMessageBox.warning(self, "Lỗi", "Hãy chọn thư mục đầu vào.")
            return
        if not self._output_dir:
            QMessageBox.warning(self, "Lỗi", "Hãy chọn thư mục đầu ra.")
            return
        if os.path.abspath(self._output_dir) == os.path.abspath(self._input_dir):
            confirm = QMessageBox.question(
                self, "Thư mục trùng nhau",
                "Thư mục đầu ra trùng thư mục đầu vào. Các file gốc sẽ bị ghi đè "
                "bằng file đã ký. Tiếp tục?",
            )
            if confirm != QMessageBox.StandardButton.Yes:
                return

        auto_ocr = self.chk_ocr.isChecked()
        # If OCR is requested, verify the engine is available before starting.
        if auto_ocr:
            try:
                from scanindex.core.ocr import direct_engine
                status = direct_engine.check_ocr_status()
                if not getattr(status, "ready", False):
                    QMessageBox.warning(
                        self, "OCR chưa sẵn sàng",
                        "Engine OCR (ScreenAI) chưa được cài đặt. Hãy tắt tùy chọn "
                        "'Tự động OCR' hoặc cài engine OCR trong phần Cài đặt.",
                    )
                    return
            except Exception:
                # If we can't check, let the worker try; it will report a clear
                # error via the failed signal if the pool cannot start.
                pass

        cert = self._panel.get_cert_info()
        if cert is None:
            QMessageBox.warning(self, "Lỗi", "Hãy chọn chứng thư số.")
            return
        items = [i for i in self._items if os.path.exists(i.source_path)]
        if not items:
            QMessageBox.warning(self, "Lỗi", "Chưa có file PDF hợp lệ để ký. Hãy nạp danh sách.")
            return
        out_dir = self._output_dir
        os.makedirs(out_dir, exist_ok=True)

        stamp_template = self._panel.get_stamp_template()
        stamp_image_path = self._panel.get_stamp_image_path()
        stamp_text_position = self._panel.get_stamp_text_position()
        use_tsa = self._panel.is_tsa_enabled()
        tsa_url = self._panel.get_tsa_url() if use_tsa else ""
        if not stamp_template:
            QMessageBox.warning(self, "Mẫu chữ ký", "Mẫu hiển thị không được để trống.")
            return
        if stamp_image_path and not os.path.exists(stamp_image_path):
            QMessageBox.warning(self, "Hình dấu", f"Không tìm thấy hình dấu:\n{stamp_image_path}")
            return
        if use_tsa and not tsa_url:
            QMessageBox.warning(
                self, "Máy chủ TSA",
                "Nhập địa chỉ máy chủ TSA hoặc tắt chức năng cấp dấu thời gian.",
            )
            return
        if use_tsa and not tsa_url.lower().startswith(("http://", "https://")):
            QMessageBox.warning(
                self, "Máy chủ TSA",
                "Địa chỉ TSA phải bắt đầu bằng http:// hoặc https://.",
            )
            return
        try:
            self._panel.validate_stamp_template(cert, stamp_template)
        except KeyError as exc:
            QMessageBox.warning(self, "Mẫu chữ ký", f"Trường không hỗ trợ: {{{exc.args[0]}}}")
            return

        min_h = self._panel.minimum_visible_stamp_height(cert, stamp_template)
        if self._panel.spin_h.value() < min_h:
            self._panel.spin_h.setValue(min_h)

        time_decision = None
        if use_tsa:
            time_decision = self._panel.resolve_batch_time_decision(tsa_url)
            if time_decision is None:
                return
            tsa_url = time_decision.tsa_url
        else:
            tsa_url = ""

        for item in self._items:
            item.status = "Chờ ký"
            item.output_path = ""
            item.error = ""
        self._refresh_table()

        sig_box = self._panel.get_sig_box()
        self._panel.save_settings()
        self._save_settings()
        self.log_message.emit(
            f"Bulk signing: starting {len(items)} file(s) → {out_dir}"
            + (" (with auto-OCR)" if auto_ocr else " (no OCR)"),
            "info",
        )
        self.btn_sign.setText("Dừng")
        self.btn_sign.setEnabled(True)
        self._set_inputs_enabled(False)
        self.lbl_status.setText(f"Đang xử lý 0/{len(items)} file…")

        self._worker = _BatchSignWorker(
            items=items,
            output_dir=out_dir,
            cert_info=cert,
            sig_box=sig_box,
            custom_page=self._panel.get_page_index(),
            stamp_template=stamp_template,
            stamp_image_path=stamp_image_path,
            stamp_text_position=stamp_text_position,
            tsa_url=tsa_url,
            enable_pdfa=self._panel.is_pdfa(),
            avoid_text_overlap=self._panel.is_avoid_overlap(),
            auto_ocr=auto_ocr,
            parent=self,
        )
        self._worker.progress.connect(self._on_sign_progress)
        self._worker.ocr_page_progress.connect(self._on_ocr_page_progress)
        self._worker.finished_ok.connect(self._on_sign_done)
        self._worker.failed.connect(self._on_sign_failed)
        # Start the spinner animation for the OCR phase.
        self._ocr_view = None
        self._ocr_file_idx = None
        self._spinner_idx = 0
        self._spinner_timer.start()
        self._worker.start()

    def _on_sign_progress(self, done: int, total: int, result: dict, phase: str):
        source_path = os.path.abspath(result.get("source_path") or "")
        idx = next(
            (i for i, item in enumerate(self._items)
             if os.path.abspath(item.source_path) == source_path),
            result.get("index", -1),
        )
        name = (
            self._items[idx].display_name
            if 0 <= idx < len(self._items)
            else os.path.basename(result.get("source_path") or "")
        )

        # OCR phase: a scan file is about to be OCRed. Record the row + an
        # initial view; the timer-driven _tick_spinner handles the repaint.
        if phase == "ocr":
            self._ocr_file_idx = idx if 0 <= idx < len(self._items) else None
            self._ocr_view = (0, 1, name)
            return

        # Sign phase: this file is done (signed or error). Clear the OCR view
        # so the spinner stops showing page counts for a finished file.
        self._ocr_view = None
        self._ocr_file_idx = None
        if 0 <= idx < len(self._items):
            item = self._items[idx]
            ocr_note = result.get("ocr_note") or ""
            if result.get("ok"):
                item.status = "Đã ký"
                item.output_path = result.get("output_path", "")
                item.error = ""
            else:
                item.status = "Lỗi"
                item.output_path = ""
                item.error = result.get("error", "")
            # Fold any OCR note into the status cell tooltip so the operator
            # can see whether OCR ran / failed / was skipped.
            tip = item.error or ""
            if ocr_note:
                tip = (tip + "\n" if tip else "") + ocr_note
            self._set_table_item(idx, 2, item.status, tooltip=tip, status=item.status)
            self._set_table_item(idx, 3, os.path.basename(item.output_path), tooltip=item.output_path)
        self.lbl_status.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; font-size: {_FONT_SM}px;"
        )
        self.lbl_status.setText(f"Đang ký {done}/{total}: {name}")

    def _on_sign_done(self, results: list[dict]):
        self._spinner_timer.stop()
        self._ocr_view = None
        self._ocr_file_idx = None
        ok_count = sum(1 for r in results if r.get("ok"))
        err_count = len(results) - ok_count
        ocr_count = sum(1 for r in results if r.get("ocr_note"))
        self.btn_sign.setText("Bắt đầu ký số")
        self.btn_sign.setEnabled(True)
        self._set_inputs_enabled(True)
        self.lbl_status.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; font-size: {_FONT_SM}px;"
        )
        self.lbl_status.setText(f"Xong: {ok_count} thành công, {err_count} lỗi.")
        summary = f"Bulk signing: signed {ok_count}/{len(results)} PDF file(s)"
        if ocr_count:
            summary += f"; OCR applied to {ocr_count} file(s)"
        self.log_message.emit(summary, "info")
        if err_count:
            failed = [os.path.basename(r.get("source_path", "")) for r in results if not r.get("ok")]
            QMessageBox.warning(self, "Có lỗi ký số", "\n".join(failed[:15]))

    def _on_sign_failed(self, error: str):
        self._spinner_timer.stop()
        self._ocr_view = None
        self._ocr_file_idx = None
        self.btn_sign.setText("Bắt đầu ký số")
        self.btn_sign.setEnabled(True)
        self._set_inputs_enabled(True)
        self.lbl_status.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; font-size: {_FONT_SM}px;"
        )
        self.lbl_status.setText(f"Ký số thất bại: {error}")
        QMessageBox.critical(self, "Lỗi ký số", error)

    def _set_inputs_enabled(self, enabled: bool):
        for widget in [
            self._btn_pick_input, self._btn_pick_output, self._btn_load,
            self._btn_open_output, self.chk_ocr,
        ]:
            widget.setEnabled(enabled)
        self._panel.set_inputs_enabled(enabled)

    # ------------------------------------------------ ScreenContent contract

    def required_models(self) -> list[str]:
        # Signing needs no AI models; OCR (when enabled) lazily starts the
        # ScreenAI pool via _get_pool inside the worker.
        return []

    def is_busy(self) -> bool:
        return self._worker is not None and self._worker.isRunning()

    def request_cancel(self) -> None:
        if self.is_busy():
            self._worker.cancel()

    def cleanup(self) -> None:
        try:
            self._spinner_timer.stop()
        except Exception:
            pass
        try:
            if self._worker and self._worker.isRunning():
                self._worker.cancel()
                self._worker.wait(2000)
        except Exception:
            pass
        try:
            self._save_settings()
        except Exception:
            pass
        try:
            self._panel.cleanup()
        except Exception:
            pass


# Styling-only scroll wrapper (kept local; mirrors the archive Step 3 look).
def _wrap_in_scroll(widget: QWidget) -> QScrollArea:
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    scroll.setWidget(widget)
    scroll.setStyleSheet(f"""
        QScrollArea {{ background: {COLOR_BG}; border: none; }}
        QScrollBar:vertical {{ background: transparent; width: 8px; }}
        QScrollBar::handle:vertical {{
            background: {COLOR_BORDER_DEFAULT}; border-radius: 4px; min-height: 24px;
        }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    """)
    return scroll
