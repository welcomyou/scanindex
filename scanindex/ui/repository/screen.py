"""Kho lưu trữ screen — searchable archive of OCR'd PDFs.

Layout (3 columns, horizontal splitter):

  +------------------------------------------------------------------+
  | Toolbar: search · mode · filters · import · settings             |
  +------------------------------------------------------------------+
  | Status: path · stats · progress                                  |
  +------------------------------------------------------------------+
  | Filter panel (collapsible)                                       |
  +-----------+----------------------------+-------------------------+
  |   List    |     PDF preview (only)     |   Right panel:          |
  |           |                            |   · file info card      |
  | Modes:    |                            |   · search snippets     |
  | A) browse |                            |     (only when query)   |
  |    dossiers                            |                         |
  | B) browse |                            |                         |
  |    files in 1 dossier                  |                         |
  | C) search hits (1 card per file,       |                         |
  |    dedup score = sum top-3 chunks)     |                         |
  +-----------+----------------------------+-------------------------+

Workers:
- ImportWorker: runs Importer.import_folder() off-thread, emits progress.
- SearchWorker: runs SearchEngine.search() off-thread.
"""
from __future__ import annotations

import configparser
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import (
    QEvent, QMimeData, QModelIndex, QRect, QRectF, QSize, Qt, Signal,
    QAbstractListModel, QThread, QTimer,
)
from PySide6.QtGui import (
    QCursor, QDrag, QImage, QPainter, QPainterPath, QPen, QColor, QPixmap,
)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QComboBox, QFrame, QScrollArea, QSplitter, QStackedWidget,
    QFileDialog, QMessageBox, QProgressBar, QGridLayout, QListView,
    QStyle, QStyledItemDelegate,
    QToolButton, QApplication, QCheckBox, QSizePolicy, QDialog, QDialogButtonBox,
)

from scanindex.ui.screens.screen_base import ScreenContent
from scanindex.ui.theme import (
    COLOR_ACCENT, COLOR_ACCENT_HOVER, COLOR_BG, COLOR_BORDER,
    COLOR_BORDER_DEFAULT, COLOR_ELEVATED, COLOR_GREEN, COLOR_GREEN_HOVER,
    COLOR_INPUT, COLOR_PANEL, COLOR_RED, COLOR_RED_HOVER, COLOR_SURFACE,
    COLOR_TEXT, COLOR_TEXT_MUTED, COLOR_TEXT_SECONDARY,
    COMBOBOX_DROPDOWN_QSS,
    BUTTON_PRIMARY_QSS,
    FONT_UI, FONT_MONO, RADIUS_MD, RADIUS_SM, SP,
)
from scanindex.ui.widgets.fuzzy_combobox import FuzzyComboBox
from scanindex.ui.widgets.pdf_viewer_widget import PdfViewerWidget
from scanindex.infra.paths import get_base_dir
from scanindex.infra import translations

from scanindex.core.repository import constants as C
from scanindex.core.repository.store import ArchiveStore
from scanindex.core.repository.indexer import HybridIndex
from scanindex.core.digitization.session import IdentityCodes
from scanindex.core.repository.importer import (
    Importer, ImportProgress, KIE_COLUMNS, KIE_LABELS,
    extract_blocks_from_canonical,
)
from scanindex.core.repository.search_engine import (
    SearchEngine,
    SearchResult,
    _fuzzy_span_token_ranges,
)
from scanindex.core.repository.repair import run_startup_repair
from scanindex.core.repository.tokenizer import search_norm, to_no_diacritic


# Độ mật levels for the Tra cứu tài liệu criteria combo. Same canonical
# order/labels as the digitization screen's "Độ mật" dropdown; an absent
# secrecy mark means "Thường".
_CONFIDENTIALITY_OPTIONS = ("Thường", "Mật", "Tối mật", "Tuyệt mật")


# ---------------------------------------------------------------- Workers


class ImportWorker(QThread):
    progress = Signal(object)
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(self, importer: Importer, source: Path):
        super().__init__()
        self._importer = importer
        self._source = source
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            res = self._importer.import_folder(
                self._source,
                progress_cb=lambda p: self.progress.emit(p),
                cancel_check=lambda: self._cancel,
            )
            self.finished_ok.emit(res)
        except Exception as e:
            self.failed.emit(str(e))


class SearchWorker(QThread):
    finished_ok = Signal(list, bool)   # results, fuzzy_budget_exhausted
    failed = Signal(str)

    def __init__(self, engine: SearchEngine, query: str, filters: dict, mode: str):
        super().__init__()
        self._engine = engine
        self._query = query
        self._filters = filters
        self._mode = mode
        self._cancel = False

    def cancel(self):
        """Cooperative cancel: engine polls between stages and inside the
        linear fallback loops, then returns the partial results."""
        self._cancel = True

    @property
    def cancelled(self) -> bool:
        return self._cancel

    def run(self):
        try:
            results = self._engine.search(
                self._query, self._filters, self._mode,
                cancel_check=lambda: self._cancel,
            )
            self.finished_ok.emit(
                results,
                bool(getattr(self._engine, "fuzzy_budget_exhausted", False)),
            )
        except Exception as e:
            self.failed.emit(str(e))


class _HydrateWorker(QThread):
    """Off-thread hydration of word-level match bboxes.

    Reads the per-PDF canonical OCR companion (zstd) or opens the PDF with
    fitz — pure I/O that must never block the GUI thread. Results mutate the
    passed SearchResult objects in place; the screen caches them so
    re-selecting a document never re-reads disk.
    """
    finished_ok = Signal(str, list)   # (doc_id, hydrated chunks)
    failed = Signal(str)

    def __init__(self, engine: SearchEngine, doc_id: str,
                 chunks: List[SearchResult]):
        super().__init__()
        self._engine = engine
        self._doc_id = doc_id
        self._chunks = chunks

    def run(self):
        try:
            self._engine.hydrate_match_bboxes(
                self._chunks, limit=max(48, len(self._chunks))
            )
            self.finished_ok.emit(self._doc_id, self._chunks)
        except Exception as e:
            self.failed.emit(str(e))


class _IndexRebuildWorker(QThread):
    """Off-thread rebuild of the derived Tantivy index from SQLite.

    Used at open time when the on-disk index predates this indexer
    generation (schema upgrade, missing folder, stale watermark). SQLite is
    never touched; a cancel leaves the previous generation in place.
    """
    progress = Signal(int, int)      # done, total chunks
    finished_ok = Signal(object)     # rebuild summary dict
    failed = Signal(str)

    def __init__(self, store, archive_path):
        super().__init__()
        self._store = store
        self._archive_path = archive_path
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            from scanindex.core.repository.reindex import rebuild_search_index
            res = rebuild_search_index(
                self._store, self._archive_path,
                progress_cb=lambda done, total: self.progress.emit(done, total),
                cancel_check=lambda: self._cancel,
            )
            self.finished_ok.emit(res)
        except Exception as e:
            import traceback
            self.failed.emit(f"{e}\n{traceback.format_exc()}")


class _AddFileWorker(QThread):
    """Off-thread index write for the "Thêm văn bản" flow."""
    finished_ok = Signal(str)     # doc_id
    failed = Signal(str)

    def __init__(self, archive_path, dossier_id, pdf_path,
                 kie_fields, body_chunks, kie_annotation_json: str = ""):
        super().__init__()
        self._archive_path = archive_path
        self._dossier_id = dossier_id
        self._pdf_path = pdf_path
        self._kie_fields = kie_fields
        self._body_chunks = body_chunks
        self._kie_annotation_json = kie_annotation_json or "{}"

    def run(self):
        try:
            from scanindex.core.repository.store import ArchiveStore
            from scanindex.core.repository.indexer import HybridIndex
            from scanindex.core.repository import admin
            from scanindex.infra.data_versioning import get_active_db_filename
            doc_id = ""
            store = ArchiveStore(
                self._archive_path, db_filename=get_active_db_filename()
            )
            with store:
                idx = HybridIndex(self._archive_path)
                idx.open()
                try:
                    doc_id = admin.add_document(
                        store, idx,
                        dossier_id=self._dossier_id,
                        pdf_path=self._pdf_path,
                        kie_fields=self._kie_fields,
                        body_chunks=self._body_chunks,
                        kie_annotation_json=self._kie_annotation_json,
                    )
                finally:
                    idx.close()
            if doc_id:
                self.finished_ok.emit(doc_id)
        except Exception as e:
            import traceback
            self.failed.emit(f"{e}\n{traceback.format_exc()}")


class _AddFilesWorker(QThread):
    """Persist multiple OCR/KIE-prepared PDFs into one dossier."""

    progress = Signal(object)
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(self, archive_path, dossier_id, items: list[dict]):
        super().__init__()
        self._archive_path = archive_path
        self._dossier_id = dossier_id
        self._items = list(items or [])

    def _emit_progress(self, done: int, total: int, file_name: str) -> None:
        self.progress.emit({
            "message": f"Đang thêm vào Kho {min(done + 1, total)}/{total}",
            "done": int(done),
            "total": int(total),
            "file": file_name,
        })

    def run(self):
        try:
            from scanindex.core.repository.store import ArchiveStore
            from scanindex.core.repository.indexer import HybridIndex
            from scanindex.core.repository import admin
            from scanindex.infra.data_versioning import get_active_db_filename

            imported = []
            total = len(self._items)
            store = ArchiveStore(
                self._archive_path, db_filename=get_active_db_filename()
            )
            with store:
                idx = HybridIndex(self._archive_path)
                idx.open()
                try:
                    for i, item in enumerate(self._items):
                        pdf_path = Path(item["pdf_path"])
                        self._emit_progress(i, total, pdf_path.name)
                        doc_id = admin.add_document(
                            store, idx,
                            dossier_id=self._dossier_id,
                            pdf_path=pdf_path,
                            kie_fields=item.get("kie_fields") or {},
                            body_chunks=item.get("body_chunks") or [],
                            kie_annotation_json=item.get("kie_annotation_json") or "{}",
                        )
                        imported.append(doc_id)
                finally:
                    idx.close()
            self.finished_ok.emit(imported)
        except Exception as e:
            import traceback
            self.failed.emit(f"{e}\n{traceback.format_exc()}")


class _ZipKhoParseWorker(QThread):
    """Phase 1 of the multi-ZIP Kho import: unpack and parse every ZIP on a
    worker thread so the GUI stays responsive. `parse_export_zip_for_kho`
    extracts the whole archive to disk, which used to run on the UI thread
    before any progress dialog existed — with many/large ZIPs the screen
    froze and the import dialog only appeared long after the click. Emits
    (done, total, zip_name) per archive, then the parsed (jobs, problems);
    cancelled or failed runs clean up every temp dir they created."""

    progress = Signal(int, int, str)       # done, total, current zip name
    finished_ok = Signal(object, object)   # jobs, problems
    failed = Signal(str)

    def __init__(self, zip_paths: list[str], stamp: str):
        """`stamp` — `zip_kho_<stamp>_<n:02d>` temp dir prefix shared with
        the import phase so both phases agree on extraction folders."""
        super().__init__()
        self._zip_paths = list(zip_paths)
        self._stamp = stamp
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        from scanindex.core.digitization.zip_roundtrip import (
            parse_export_zip_for_kho, ZipRoundtripError,
        )
        jobs: list[dict] = []
        problems: list[str] = []
        created_roots: list[str] = []
        try:
            total = len(self._zip_paths)
            for i, path in enumerate(self._zip_paths, start=1):
                if self._cancel:
                    break
                name = os.path.basename(path)
                temp_root = os.path.join(
                    os.getcwd(), "temp", f"zip_kho_{self._stamp}_{i:02d}",
                )
                created_roots.append(temp_root)
                self.progress.emit(i - 1, total, name)
                try:
                    codes, docs, no_companion, _out_dir = (
                        parse_export_zip_for_kho(path, temp_root)
                    )
                except ZipRoundtripError as e:
                    problems.append(f"{name}: ZIP hồ sơ không hợp lệ — {e}")
                    shutil.rmtree(temp_root, ignore_errors=True)
                    continue
                except Exception as e:
                    problems.append(f"{name}: không đọc được — {e}")
                    shutil.rmtree(temp_root, ignore_errors=True)
                    continue
                jobs.append({
                    "codes": codes,
                    "docs": docs,
                    "temp_root": temp_root,
                    "zip_name": name,
                    "no_companion": no_companion,
                })
                self.progress.emit(i, total, name)
            if self._cancel:
                # Jobs parsed before the cancel are discarded — drop their
                # extraction dirs too (already-deleted roots are no-ops).
                for root in created_roots:
                    shutil.rmtree(root, ignore_errors=True)
                return
            self.finished_ok.emit(jobs, problems)
        except Exception as e:
            import traceback
            for root in created_roots:
                shutil.rmtree(root, ignore_errors=True)
            self.failed.emit(f"{e}\n{traceback.format_exc()}")


class _ZipKhoImportWorker(QThread):
    """Import dossiers parsed from exported ZIPs into Kho — no OCR/KIE
    re-run. Docs with canonical `.json.zst` sidecars use them as-is; docs
    without (legacy ZIPs) get a companion synthesized from the PDF text
    layer inside `Importer.import_dossier`. Handles one or many ZIP jobs
    with a single store/index session; emits an aggregate ImportProgress
    across all jobs. Best-effort deletes every job's ZIP extraction temp
    dir when done (each imported doc is copied into the repo first)."""

    progress = Signal(object)        # aggregate ImportProgress
    finished_ok = Signal(object)     # aggregate ImportProgress
    failed = Signal(str)

    def __init__(self, archive_path, jobs: list[dict]):
        """`jobs` — one entry per ZIP: {"codes", "docs", "temp_root"}."""
        super().__init__()
        self._archive_path = archive_path
        self._jobs = list(jobs or [])
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        temp_roots = [
            str(j.get("temp_root") or "") for j in self._jobs
        ]
        try:
            from scanindex.core.repository.store import ArchiveStore
            from scanindex.core.repository.indexer import HybridIndex
            from scanindex.core.repository.importer import (
                ImportProgress, Importer,
            )
            from scanindex.infra.data_versioning import get_active_db_filename

            # Honour the "Không lưu văn bản trùng thừa" setting (default
            # ON): duplicates are skipped; OFF keeps them (still reported).
            skip_duplicates = _read_skip_duplicate_docs_setting()

            total = sum(len(j.get("docs") or []) for j in self._jobs)
            agg = ImportProgress(total=total)
            store = ArchiveStore(
                self._archive_path, db_filename=get_active_db_filename()
            )
            with store:
                idx = HybridIndex(self._archive_path)
                idx.open()
                try:
                    importer = Importer(store, idx)
                    for job in self._jobs:
                        # Per-job progress is re-based onto the aggregate so
                        # the dialog shows one continuous counter.
                        base = (agg.imported, agg.skipped, agg.failed,
                                agg.text_layer_imports)

                        def _cb(p, base=base):
                            self.progress.emit(ImportProgress(
                                total=total,
                                imported=base[0] + p.imported,
                                skipped=base[1] + p.skipped,
                                failed=base[2] + p.failed,
                                text_layer_imports=base[3] + p.text_layer_imports,
                                current_file=p.current_file,
                                message=p.message,
                            ))

                        res = importer.import_dossier(
                            job["codes"], job["docs"],
                            progress_cb=_cb,
                            cancel_check=lambda: self._cancel,
                            skip_duplicates=skip_duplicates,
                        )
                        if res is not None:
                            agg.imported += res.imported
                            agg.skipped += res.skipped
                            agg.failed += res.failed
                            agg.duplicates += res.duplicates
                            agg.text_layer_imports += res.text_layer_imports
                        if self._cancel:
                            break
                finally:
                    idx.close()
            self.finished_ok.emit(agg)
        except Exception as e:
            import traceback
            self.failed.emit(f"{e}\n{traceback.format_exc()}")
        finally:
            for root in temp_roots:
                if root:
                    shutil.rmtree(root, ignore_errors=True)


class _LegacyZipCodesDialog(QDialog):
    """Prompt for the 4 identity codes when a ZIP carries a generic name.

    ZIPs exported as `HSLTCQ.zip` (one of the 4 codes was blank at export
    time) have no identity in the file name and the workbook only carries
    "Đơn vị bảo quản số" (= số hồ sơ). Without the codes the Kho import
    gate (`ma_dinh_danh` + `fonds`) would reject the whole ZIP, so the
    operator types the missing values once here; they are stored on the
    parsed `DossierCodes` for this import only.
    """

    def __init__(self, zip_name: str, codes, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Nhập mã hồ sơ cho ZIP")
        self.setMinimumWidth(420)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        intro = QLabel(
            f"File ZIP \"{zip_name}\" không chứa mã định danh / mã phông trong "
            "tên file. Hãy nhập 4 mã hồ sơ để nhập ZIP này vào Kho lưu trữ:"
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        self._edits: dict[str, QLineEdit] = {}
        fields = [
            ("ma_dinh_danh", "Mã định danh"),
            ("fonds", "Mã phông"),
            ("catalog", "Mục lục"),
            ("dossier_code", "Số hồ sơ"),
        ]
        for row, (key, label) in enumerate(fields):
            grid.addWidget(QLabel(label), row, 0)
            edit = QLineEdit(getattr(codes, key, "") or "")
            edit.setPlaceholderText(label)
            grid.addWidget(edit, row, 1)
            self._edits[key] = edit
        layout.addLayout(grid)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Nhập ZIP")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Bỏ qua")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def apply_to(self, codes) -> None:
        """Copy the entered values onto a `DossierCodes` instance."""
        for key, edit in self._edits.items():
            setattr(codes, key, edit.text().strip())

    def is_complete(self) -> bool:
        return all(
            e.text().strip() for e in (
                self._edits["ma_dinh_danh"], self._edits["fonds"],
            )
        )

    def accept(self):
        if not self.is_complete():
            QMessageBox.warning(
                self, "Nhập mã hồ sơ cho ZIP",
                "Cần nhập tối thiểu Mã định danh và Mã phông.",
            )
            return
        super().accept()


class _PrepareAddFileWorker(QThread):
    """Run the same OCR -> page selection -> LayoutLMv3 KIE preparation used
    by Digitization Step 2 before showing the shared KIE viewer."""

    progress = Signal(object)
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(self, pdf_paths, *, kie_mode: str = "layoutlmv3",
                 enable_correction: bool = True):
        super().__init__()
        if isinstance(pdf_paths, (str, Path)):
            pdf_paths = [pdf_paths]
        self._pdf_paths = [Path(p) for p in pdf_paths]
        self._kie_mode = kie_mode or "layoutlmv3"
        self._enable_correction = bool(enable_correction)
        self._runner = None
        self._cancel = False
        self._work_dir: Optional[Path] = None
        self._completed = set()

    def cancel(self) -> None:
        self._cancel = True
        runner = self._runner
        if runner is not None:
            try:
                runner.cancel()
            except Exception:
                pass

    def _emit_progress(self, *, stage: str, file_name: str = "",
                       done: int | None = None) -> None:
        total = len(self._pdf_paths)
        if done is None:
            done = len(self._completed)
        if file_name:
            current = done if stage == "Hoàn tất" else min(done + 1, total)
            message = f"{stage} {current}/{total}"
        else:
            message = f"{stage} {done}/{total}"
        self.progress.emit({
            "message": message,
            "stage": stage,
            "file": file_name,
            "done": int(done),
            "total": int(total),
        })

    def _on_runner_event(self, evt, payload: dict) -> None:
        file_id = str((payload or {}).get("file_id") or "")
        if evt == "file_queued":
            self._emit_progress(stage="Đang OCR", file_name=file_id)
        elif evt == "file_ocr_done":
            self._emit_progress(stage="Đang sửa/chọn trang", file_name=file_id)
        elif evt == "kie_start":
            self._emit_progress(stage="Đang KIE", file_name=file_id)
        elif evt == "file_complete":
            self._completed.add(file_id)
            self._emit_progress(
                stage="Hoàn tất",
                file_name=file_id,
                done=len(self._completed),
            )
        elif evt == "file_failed":
            self._emit_progress(stage="Lỗi xử lý", file_name=file_id)

    def run(self):
        try:
            from scanindex.core.digitization.runner import ArchiveRunner, FileSpec
            from scanindex.core.repository.importer import _extract_raw_kie_fields

            temp_root = Path(get_base_dir()) / "temp"
            temp_root.mkdir(parents=True, exist_ok=True)
            self._work_dir = Path(tempfile.mkdtemp(
                prefix="repository_add_", dir=str(temp_root)
            ))
            out_dir = self._work_dir / "_step2_kie"
            out_dir.mkdir(parents=True, exist_ok=True)

            total = len(self._pdf_paths)
            if total <= 0:
                raise RuntimeError("Chưa chọn file PDF")
            self._emit_progress(stage="Chuẩn bị", done=0)
            specs = [
                FileSpec(
                    input_path=str(pdf_path),
                    file_id=pdf_path.name,
                    source_document_path=str(pdf_path),
                )
                for pdf_path in self._pdf_paths
            ]
            runner = ArchiveRunner(
                output_dir=str(out_dir),
                file_specs=specs,
                kie_mode=self._kie_mode,
                on_event=self._on_runner_event,
                log_cb=lambda _m: None,
                write_excel_on_done=False,
                use_signer_page_selector=True,
                enable_correction=self._enable_correction,
            )
            self._runner = runner
            runner._run_inner()
            if self._cancel:
                raise RuntimeError("Đã hủy OCR/KIE")

            tasks = list(getattr(runner, "_tasks_completed", []) or [])
            if not tasks:
                raise RuntimeError("OCR/KIE không trả về kết quả")
            results = []
            source_by_name = {p.name: p for p in self._pdf_paths}
            for task in tasks:
                if getattr(task, "error", None):
                    raise RuntimeError(f"{task.file_id}: {task.error}")
                from scanindex.core.canonical_io import load_canonical, resolve_companion
                output_pdf = Path(getattr(task, "output_pdf_path", "") or "")
                output_json = resolve_companion(getattr(task, "output_json_path", "") or "")
                if not output_pdf.exists() or output_json is None:
                    raise RuntimeError(
                        f"{task.file_id}: Thiếu PDF OCR hoặc JSON KIE sau khi xử lý"
                    )

                canonical = load_canonical(output_json)
                kie_fields = _extract_raw_kie_fields(canonical)
                ann_block = canonical.get("annotations") or {}
                results.append({
                    "source_pdf": str(source_by_name.get(
                        task.file_id, Path(getattr(task, "source_document_path", "") or "")
                    )),
                    "output_pdf": str(output_pdf),
                    "output_json": str(output_json),
                    "work_dir": str(self._work_dir),
                    "kie_fields": kie_fields,
                    "kie_annotation_json": json.dumps(ann_block, ensure_ascii=False),
                    "selected_pages": list(getattr(task, "selected_pages", None) or []),
                })
            self.finished_ok.emit(results)
        except Exception as e:
            if self._work_dir is not None:
                shutil.rmtree(self._work_dir, ignore_errors=True)
            import traceback
            self.failed.emit(f"{e}\n{traceback.format_exc()}")


# ---------------------------------------------------------------- Helpers


def _read_repository_path_setting() -> Path:
    """Read [Repository] path from the active settings file; default to
    <base>/repository."""
    from scanindex.infra.data_versioning import get_active_settings_path
    cfg_path = Path(get_active_settings_path())
    base = Path(get_base_dir())
    if cfg_path.exists():
        cfg = configparser.ConfigParser()
        try:
            cfg.read(cfg_path, encoding="utf-8")
            if cfg.has_section("Repository") and cfg.has_option("Repository", "path"):
                p = cfg.get("Repository", "path").strip()
                if p:
                    return Path(p) if Path(p).is_absolute() else (base / p)
            if cfg.has_section("Archive") and cfg.has_option("Archive", "path"):
                p = cfg.get("Archive", "path").strip()
                if p:
                    return Path(p) if Path(p).is_absolute() else (base / p)
        except Exception:
            pass
    return base / C.DEFAULT_ARCHIVE_DIRNAME


def _read_archive_path_setting() -> Path:
    return _read_repository_path_setting()


def _read_zip_include_canonical_setting() -> bool:
    """Whether dossier-ZIP exports should bundle each PDF's canonical
    `.json.zst` sidecar (OCR + KIE) so the ZIP can be re-imported into Kho
    without re-running OCR/KIE. Mirrors the Settings-tab checkbox; default
    ON. Kept in sync with MainWindow's `[Export] IncludeCanonicalZip`."""
    try:
        from scanindex.infra.data_versioning import get_active_settings_path
        cfg_path = Path(get_active_settings_path())
        if cfg_path.exists():
            cfg = configparser.ConfigParser()
            cfg.read(cfg_path, encoding="utf-8")
            if cfg.has_section("Export"):
                return cfg.getboolean(
                    "Export", "IncludeCanonicalZip", fallback=True
                )
    except Exception:
        pass
    return True


def _read_skip_duplicate_docs_setting() -> bool:
    """Whether Kho imports should skip byte-identical PDFs within one
    dossier (sha256 dedup). Mirrors the Settings-tab checkbox; default ON
    (historic behaviour). Kept in sync with MainWindow's
    `[Repository] SkipDuplicateDocs`."""
    try:
        from scanindex.infra.data_versioning import get_active_settings_path
        cfg_path = Path(get_active_settings_path())
        if cfg_path.exists():
            cfg = configparser.ConfigParser()
            cfg.read(cfg_path, encoding="utf-8")
            if cfg.has_section("Repository"):
                return cfg.getboolean(
                    "Repository", "SkipDuplicateDocs", fallback=True
                )
    except Exception:
        pass
    return True


def _write_repository_path_setting(path: Path) -> None:
    from scanindex.infra.data_versioning import get_active_settings_path
    cfg_path = Path(get_active_settings_path())
    cfg = configparser.ConfigParser()
    if cfg_path.exists():
        try:
            cfg.read(cfg_path, encoding="utf-8")
        except Exception:
            pass
    if not cfg.has_section("Repository"):
        cfg.add_section("Repository")
    cfg.set("Repository", "path", str(path))
    with open(cfg_path, "w", encoding="utf-8") as f:
        cfg.write(f)


def _write_archive_path_setting(path: Path) -> None:
    _write_repository_path_setting(path)


# ---------------------------------------------------------------- Domain
# Lightweight in-screen view types — kept separate from search_engine's
# SearchResult so the UI can compose its own dossier / file / hit groupings
# without leaking SQL columns into the engine layer.


@dataclass
class DossierRow:
    dossier_id: int
    title: str
    fonds: str
    catalog: str
    dossier_code: str
    doc_count: int
    page_count: int
    start_date: str
    end_date: str
    ma_dinh_danh: str = ""
    is_unstructured: bool = False
    retention: str = ""
    term: str = ""
    storage_unit: str = ""
    physical_state: str = ""
    topic: str = ""
    note: str = ""
    fonds_name: str = ""
    catalog_name: str = ""
    stored_at: Optional[int] = None  # epoch of when the dossier entered the repo (dossiers.created_at)


def _norm_tokens(text: str) -> list[str]:
    """Word tokens, diacritic-stripped + lowercased, for dossier matching."""
    return re.findall(r"\w+", to_no_diacritic(str(text or "")).lower())


def _dossier_matches_title(title: str, query: str) -> bool:
    """Dossier-title keyword match: every query token (diacritic-stripped,
    lowercased) must appear as a SUBSTRING of the normalized title, so a
    partial word still matches (gõ "z" ra "zXcXC", "zst" ra "…Kèm ZST").
    Empty query matches everything, so the unfiltered browse stays the
    empty state."""
    tokens = _norm_tokens(query)
    if not tokens:
        return True
    hay = " ".join(_norm_tokens(title))
    return all(t in hay for t in tokens)


def _dossier_matches_text(value: str, filter_text: str) -> bool:
    """Diacritic-insensitive contains-match for one free-text dossier field
    (chuyên đề / nhiệm kỳ / thời hạn). Empty filter matches everything."""
    needle = to_no_diacritic(str(filter_text or "").strip()).lower()
    if not needle:
        return True
    return needle in to_no_diacritic(str(value or "")).lower()


_DOSSIER_SQL_CODE_FILTERS: tuple[tuple[str, str], ...] = (
    # (filter key, dossiers column) — ASCII code columns matched with a
    # case-insensitive SQL LIKE so gõ "777" ra cả "7777".
    ("ma_dinh_danh", "ma_dinh_danh"),
    ("dossier_code", "dossier_code"),
)


def _dossier_sql_filters(filters: dict) -> tuple[list[str], list[str]]:
    """WHERE fragments + params for the dossier browse/search SQL: exact
    equality on phông/mục lục, contains-LIKE on the code columns."""
    where_parts: list[str] = []
    params: list[str] = []
    for key, column in (("fonds", "fonds"), ("catalog", "catalog")):
        value = str((filters or {}).get(key) or "").strip()
        if value:
            where_parts.append(f"COALESCE(d.{column}, '') = ?")
            params.append(value)
    for key, column in _DOSSIER_SQL_CODE_FILTERS:
        value = str((filters or {}).get(key) or "").strip()
        if value:
            where_parts.append(f"COALESCE(d.{column}, '') LIKE ?")
            params.append(f"%{value}%")
    return where_parts, params


@dataclass
class FileRow:
    doc_id: str
    dossier_id: Optional[int]
    file_name: str
    file_path: str
    subject: str
    doc_number: str
    issue_org: str
    issue_org_superior: str
    signer_name: str
    issue_date: str
    doc_type: str
    secrecy_mark: str
    page_count: int
    dossier_title: str = ""
    trang_so: Optional[int] = None
    so_thu_tu: Optional[int] = None
    fonds: str = ""
    catalog: str = ""
    dossier_code: str = ""


@dataclass
class FileHit:
    """One file with all its matching chunks, sorted by chunk score desc.
    For lexical search, `score_total` still favours repeated matches. For
    search results are grouped per file so the UI can show one card per PDF."""
    file_row: FileRow
    chunks: List[SearchResult]
    score_total: float = 0.0
    match_total: int = 0
    match_kind: str = ""
    # Tiered relevance: match class ×1000 + field weight ×40 + saturated
    # term frequency ×10 + BM25 percentile tie-break ×5. See
    # _filehit_relevance — replaces the pure-frequency ordering, which
    # biased long documents and boilerplate forms.
    relevance: float = 0.0


def _bbox_tuple(chunk: SearchResult) -> Optional[tuple[float, float, float, float]]:
    bbox = getattr(chunk, "bbox", None) or []
    if len(bbox) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in bbox)
    except Exception:
        return None
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1, y1


def _bbox_area(bbox: Optional[tuple[float, float, float, float]]) -> float:
    if bbox is None:
        return 0.0
    x0, y0, x1, y1 = bbox
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def _bbox_iou(a: Optional[tuple[float, float, float, float]],
              b: Optional[tuple[float, float, float, float]]) -> float:
    if a is None or b is None:
        return 0.0
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    inter = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
    if inter <= 0:
        return 0.0
    union = _bbox_area(a) + _bbox_area(b) - inter
    return inter / union if union > 0 else 0.0


def _is_same_match_bbox(a: list[float], b: list[float]) -> bool:
    if len(a) != 4 or len(b) != 4:
        return False
    try:
        at = tuple(float(v) for v in a)
        bt = tuple(float(v) for v in b)
    except Exception:
        return False
    if _bbox_iou(at, bt) >= 0.72:
        return True
    ax0, ay0, ax1, ay1 = at
    bx0, by0, bx1, by1 = bt
    aw, ah = max(1.0, ax1 - ax0), max(1.0, ay1 - ay0)
    bw, bh = max(1.0, bx1 - bx0), max(1.0, by1 - by0)
    acx, acy = (ax0 + ax1) / 2.0, (ay0 + ay1) / 2.0
    bcx, bcy = (bx0 + bx1) / 2.0, (by0 + by1) / 2.0
    return (
        abs(acx - bcx) <= max(aw, bw) * 0.35
        and abs(acy - bcy) <= max(ah, bh) * 0.60
    )


def _token_overlap_ratio(a: str, b: str) -> float:
    at = set(re.findall(r"\w+", to_no_diacritic(a or "").lower()))
    bt = set(re.findall(r"\w+", to_no_diacritic(b or "").lower()))
    if not at or not bt:
        return 0.0
    return len(at & bt) / max(1, min(len(at), len(bt)))


def _chunk_quality_rank(chunk: SearchResult) -> tuple[int, float, float, int]:
    bbox = _bbox_tuple(chunk)
    return (
        int(chunk.match_count or 0),
        float(chunk.score or 0.0),
        -_bbox_area(bbox),
        -len(chunk.text or ""),
    )


def _is_near_duplicate_chunk(a: SearchResult, b: SearchResult) -> bool:
    if a.doc_id != b.doc_id or int(a.page or 0) != int(b.page or 0):
        return False
    if _bbox_iou(_bbox_tuple(a), _bbox_tuple(b)) < 0.70:
        return False
    return _token_overlap_ratio(a.text or "", b.text or "") >= 0.55


def _query_tokens_for_highlight(query: str) -> list[str]:
    return [
        t for t in re.findall(r"\w+", to_no_diacritic(query or "").lower())
        if len(t) >= 2
    ]


def _fuzzy_query_tokens_for_highlight(query: str) -> list[str]:
    return [
        t for t in _query_tokens_for_highlight(query)
        if len(t) >= 2 or any(ch.isdigit() for ch in t)
    ]


def _display_fuzzy_token_match(qt: str,
                               token: str,
                               *,
                               allow_short_fuzzy: bool = True) -> bool:
    if not qt or not token or qt == token:
        return bool(qt and token)
    if any(ch.isdigit() for ch in qt + token):
        if not (qt.isdigit() and token.isdigit()):
            return False
        if qt[:1] != token[:1]:
            return False
        max_dist = (
            0 if len(qt) <= C.FUZZY_EXACT_MAX_LEN
            else (1 if len(qt) <= C.FUZZY_ONE_EDIT_MAX_LEN else 2)
        )
    else:
        if len(qt) <= 2:
            if not allow_short_fuzzy:
                return False
            return qt == token
        max_dist = 1 if len(qt) <= C.FUZZY_ONE_EDIT_MAX_LEN else 2
        if abs(len(qt) - len(token)) > max_dist:
            return False
    try:
        from rapidfuzz.distance import DamerauLevenshtein
        dist = DamerauLevenshtein.distance(qt, token)
    except Exception:
        try:
            from rapidfuzz.distance import Levenshtein
            dist = Levenshtein.distance(qt, token)
        except Exception:
            return False
    return dist <= max_dist


def _query_match_spans(text: str, query: str, *, fuzzy: bool = False) -> list[tuple[int, int]]:
    source = str(text or "")
    qtokens = (
        _fuzzy_query_tokens_for_highlight(query)
        if fuzzy
        else _query_tokens_for_highlight(query)
    )
    if not source or not qtokens:
        return []
    allow_short_fuzzy = len(qtokens) > 1

    word_spans = [
        (m.start(), m.end(), to_no_diacritic(m.group(0)).lower())
        for m in re.finditer(r"\w+", source, flags=re.UNICODE)
    ]
    spans: list[tuple[int, int]] = []
    n = len(qtokens)
    for i in range(0, len(word_spans) - n + 1):
        if [tok for _, _, tok in word_spans[i:i + n]] == qtokens:
            spans.append((word_spans[i][0], word_spans[i + n - 1][1]))

    # Multi-word queries must highlight the contiguous phrase only. If we also
    # mark each token separately, searching "Pham Van Hien" paints unrelated
    # occurrences such as "Van phong", which is misleading.
    if n == 1:
        qset = set(qtokens)
        for start, end, token in word_spans:
            if token in qset:
                spans.append((start, end))
    if not spans and fuzzy:
        if n == 1:
            for start, end, token in word_spans:
                if _display_fuzzy_token_match(
                    qtokens[0],
                    token,
                    allow_short_fuzzy=allow_short_fuzzy,
                ):
                    spans.append((start, end))
        elif n < 8:
            tokens = [token for _, _, token in word_spans]
            for start_idx, end_idx in _fuzzy_span_token_ranges(tokens, qtokens):
                spans.append((word_spans[start_idx][0], word_spans[end_idx][1]))
    if not spans:
        return []

    spans.sort()
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def _snippet_context_text(text: str,
                          query: str,
                          max_chars: int = 360,
                          *,
                          fuzzy: bool = False) -> str:
    """Return a short snippet centered around the first visible query match."""
    source = " ".join(str(text or "").strip().split())
    if not source or len(source) <= max_chars:
        return source

    spans = _query_match_spans(source, query, fuzzy=fuzzy)
    if not spans:
        return source[:max_chars].rstrip() + "..."

    start, end = spans[0]
    span_len = max(1, end - start)
    left_context = max(0, (max_chars - span_len) // 2)
    left = max(0, start - left_context)
    right = min(len(source), left + max_chars)
    if right < end:
        right = min(len(source), end)
        left = max(0, right - max_chars)

    if left > 0:
        next_space = source.find(" ", left, min(start, left + 48))
        if next_space != -1 and next_space < start:
            left = next_space + 1
    if right < len(source):
        prev_space = source.rfind(" ", max(end, right - 48), right)
        if prev_space != -1 and prev_space > end:
            right = prev_space

    snippet = source[left:right].strip()
    if left > 0:
        snippet = "..." + snippet
    if right < len(source):
        snippet += "..."
    return snippet


def _highlight_query_html(text: str, query: str, *, fuzzy: bool = False) -> str:
    """Return escaped snippet HTML with query terms highlighted."""
    source = str(text or "")
    if not source:
        return html.escape(source)

    spans = _query_match_spans(source, query, fuzzy=fuzzy)
    if not spans:
        return html.escape(source)

    out: list[str] = []
    pos = 0
    for start, end in spans:
        out.append(html.escape(source[pos:start]))
        out.append(
            "<span style='background-color:#facc15;color:#111827;"
            "padding:0 1px;border-radius:2px;'>"
            f"{html.escape(source[start:end])}</span>"
        )
        pos = end
    out.append(html.escape(source[pos:]))
    return "".join(out)


def _secrecy_mark_color(mark: str) -> str:
    """Only classified marks need warning red; normal access stays neutral."""
    normalized = to_no_diacritic(str(mark or "").strip()).lower()
    return "#dc2626" if "mat" in normalized else COLOR_TEXT


def _single_line(value: str | None) -> str:
    return " ".join(str(value or "").replace("\xa0", " ").split())


def _format_issue_date(value: str | None) -> str:
    text = _single_line(value)
    if not text:
        return ""
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).strftime("%d/%m/%Y")
        except ValueError:
            pass
    try:
        from scanindex.core.digitization.metadata_export import _parse_date_from_place_date
        parsed = _parse_date_from_place_date(text)
        if parsed:
            return parsed
    except Exception:
        pass
    m = re.search(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})", text)
    if not m:
        # The repository card only shows the canonical issue-date value. Do
        # not leak the place portion (or arbitrary unparsed OCR prose) into the
        # compact summary line.
        return ""
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 100:
        y = 2000 + y if y < 50 else 1900 + y
    return f"{d:02d}/{mo:02d}/{y:04d}"


def _issue_date_sort_key(value: str | None) -> str:
    """Return YYYYMMDD so display dates can be sorted chronologically."""
    formatted = _format_issue_date(value)
    if not formatted:
        return ""
    try:
        return datetime.strptime(formatted, "%d/%m/%Y").strftime("%Y%m%d")
    except ValueError:
        return ""


def _format_stored_at(epoch: Optional[int]) -> str:
    """Format a unix epoch (dossier.created_at — the moment the dossier first
    entered the repository) as "HH:MM dd/mm/yyyy" for display."""
    if not epoch:
        return ""
    try:
        return datetime.fromtimestamp(int(epoch)).strftime("%H:%M %d/%m/%Y")
    except (ValueError, OSError, OverflowError):
        return ""


def _format_doc_number(value: str | None) -> str:
    text = _single_line(value)
    if not text:
        return ""
    try:
        from scanindex.core.kie.ontology import (
            parse_doc_number_symbol,
            strip_doc_number_symbol_prefix,
        )
        stripped = _single_line(strip_doc_number_symbol_prefix(text) or text)
        parsed = parse_doc_number_symbol(text)
        number = _single_line(parsed.get("number") or "")
        symbol = _single_line(parsed.get("symbol") or "")
        year = _single_line(parsed.get("year") or "")
        if number and symbol:
            return f"{number}-{year + '/' if year else ''}{symbol}"
        return stripped
    except Exception:
        return re.sub(r"^\s*S[ốo0]\s*[:.]?\s*", "", text, flags=re.IGNORECASE).strip()


def _format_issue_org(issue_org: str | None,
                      issue_org_superior: str | None = "") -> str:
    name = _single_line(issue_org)
    superior = _single_line(issue_org_superior)
    if name and superior and superior.lower() not in name.lower():
        return f"{name} {superior}"
    return name or superior


def _file_summary_parts(file: "FileRow", *, localized: bool = False) -> list[str]:
    parts: list[str] = []
    doc_number = _format_doc_number(file.doc_number)
    date = _format_issue_date(file.issue_date)
    org = _format_issue_org(file.issue_org, file.issue_org_superior)
    signer = _single_line(file.signer_name)
    if doc_number:
        parts.append(doc_number)
    if date:
        parts.append(date)
    if org:
        parts.append(org)
    if signer:
        parts.append(signer)
    return parts


def _file_summary_text(file: "FileRow", *, localized: bool = False) -> str:
    """Return the stable Vietnamese summary contract unless rendering UI."""
    return " · ".join(_file_summary_parts(file, localized=localized))


def _file_card_title(file: "FileRow", ordinal: int = 0) -> str:
    title = file.subject or file.file_name or translations.localize_text("(không tiêu đề)")
    return f"{int(ordinal):02d}. {title}" if int(ordinal or 0) > 0 else title


def _is_unstructured_dossier(dossier: "DossierRow") -> bool:
    if bool(getattr(dossier, "is_unstructured", False)):
        return True
    markers = (
        getattr(dossier, "ma_dinh_danh", ""),
        getattr(dossier, "fonds", ""),
        getattr(dossier, "catalog", ""),
        getattr(dossier, "dossier_code", ""),
    )
    return any(str(part or "").strip().upper().startswith("UNSTRUCT") for part in markers)


def _dossier_code_line(dossier: "DossierRow") -> str:
    if _is_unstructured_dossier(dossier):
        return ""
    parts = [
        dossier.ma_dinh_danh or "—",
        dossier.fonds or "—",
        dossier.catalog or "—",
        dossier.dossier_code or "—",
    ]
    return "-".join(parts)


def _dossier_display_title(dossier: "DossierRow") -> str:
    title = _single_line(getattr(dossier, "title", "") or "")
    if title:
        return title
    if _is_unstructured_dossier(dossier):
        return translations.localize_text("Hồ sơ chưa phân loại")
    return _dossier_code_line(dossier) or translations.localize_text("Hồ sơ")


def _dossier_stats_text(dossier: "DossierRow",
                        *,
                        doc_count: Optional[int] = None,
                        page_count: Optional[int] = None,
                        localized: bool = False) -> str:
    docs = int(doc_count if doc_count is not None else (dossier.doc_count or 0))
    pages = int(page_count if page_count is not None else (dossier.page_count or 0))
    bits = []
    value = f"{docs} tài liệu"
    bits.append(translations.localize_text(value) if localized else value)
    value = f"{pages} trang"
    bits.append(translations.localize_text(value) if localized else value)
    stored = _format_stored_at(getattr(dossier, "stored_at", None))
    if stored:
        bits.append(stored)
    return " · ".join(bits)


def _dossier_status_html(dossier: "DossierRow", *, doc_count: Optional[int] = None,
                         localized: bool = False) -> str:
    title = html.escape(_dossier_display_title(dossier))
    stats = html.escape(_dossier_stats_text(
        dossier, doc_count=doc_count, localized=localized,
    ))
    if not stats:
        return f"<span style='color:{COLOR_TEXT};font-weight:600'>{title}</span>"
    return (
        f"<span style='color:{COLOR_TEXT};font-weight:600'>{title}</span>"
        f"<span style='color:{COLOR_TEXT_MUTED}'> · {stats}</span>"
    )


def _format_repo_stats(dossier_count: int,
                       doc_count: int,
                       page_count: int,
                       chunk_count: int,
                       *, localized: bool = False) -> str:
    value = (
        f"{int(dossier_count or 0)} hồ sơ · "
        f"{int(doc_count or 0)} tài liệu · "
        f"{int(page_count or 0)} trang · "
        f"{int(chunk_count or 0)} đoạn"
    )
    return translations.localize_text(value) if localized else value


def _reordered_doc_ids(doc_ids: List[str], dragged_doc_id: str,
                       target_doc_id: str, insert_after: bool) -> List[str]:
    """Return a reordered copy for one card drop, preserving every doc id."""
    out = list(doc_ids)
    if dragged_doc_id == target_doc_id:
        return out
    if dragged_doc_id not in out or target_doc_id not in out:
        raise ValueError("Không tìm thấy văn bản cần sắp xếp")
    out.remove(dragged_doc_id)
    target_index = out.index(target_doc_id)
    if insert_after:
        target_index += 1
    out.insert(target_index, dragged_doc_id)
    return out


def _files_so_thu_tu_is_contiguous(files: List["FileRow"]) -> bool:
    """True when the files' stored so_thu_tu values form a complete 1..N
    sequence. All-blank (None) also counts as contiguous — legacy docs that
    predate the column have no explicit numbering, so dragging just assigns
    one. A gap (e.g. 1,2,4,5,6,7) means a slot was reserved for a doc that
    wasn't scanned, and a drag would clobber it, so we warn first."""
    vals = [f.so_thu_tu for f in files if f.so_thu_tu is not None]
    if not vals:
        return True
    if any(v <= 0 for v in vals) or len(vals) != len(files):
        return False
    return sorted(vals) == list(range(1, len(files) + 1))


class _DateFilterInput(QWidget):
    """Line edit plus calendar button for dd/mm/yyyy metadata filters."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self._line = QLineEdit()
        self._line.setPlaceholderText("dd/mm/yyyy")
        layout.addWidget(self._line, 1)

        self._btn = QToolButton()
        self._btn.setText("▾")
        self._btn.setFixedWidth(28)
        self._btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn.clicked.connect(self._open_calendar)
        layout.addWidget(self._btn)

    def text(self) -> str:
        return self._line.text().strip()

    def clear(self) -> None:
        self._line.clear()

    def setText(self, value: str) -> None:
        self._line.setText(value)

    def _open_calendar(self) -> None:
        from PySide6.QtCore import QDate
        from PySide6.QtWidgets import QCalendarWidget, QDialog, QVBoxLayout

        dlg = QDialog(self)
        dlg.setWindowTitle("Chọn ngày")
        dlg.setModal(True)
        dlg.setStyleSheet(f"QDialog {{ background: {COLOR_BG}; }}")
        layout = QVBoxLayout(dlg)
        calendar = QCalendarWidget()
        calendar.setGridVisible(True)
        current = QDate.fromString(self.text(), "dd/MM/yyyy")
        if current.isValid():
            calendar.setSelectedDate(current)
        layout.addWidget(calendar)

        def choose(date):
            self._line.setText(date.toString("dd/MM/yyyy"))
            dlg.accept()

        calendar.clicked.connect(choose)
        dlg.exec()


# Field-probe order for the ranking weight W: the first KIE field whose
# normalized value contains the query phrase determines the weight.
_RANK_FIELD_PROBES: tuple[tuple[str, float], ...] = (
    ("doc_number", 6.0), ("signer_name", 4.0),
    ("issue_org", 3.0), ("subject", 3.0), ("recipients", 2.0),
)


def _filehit_relevance(fh: "FileHit", query_norm: str,
                       bm25_scale: float) -> float:
    """Tiered relevance for one FileHit (expert-review formula, adapted).

        G: 4 query == full doc_number · 3 exact in metadata · 2 exact in
           body · 1.5 substring · 1 fuzzy
        W: weight of the strongest matched field (metadata prose → 2.5)
        TF: saturated, length-normalized frequency — long documents and
            boilerplate forms can no longer win by repetition alone
        BM25 percentile: light tie-break, never discarded entirely

    score = 1000·G + 40·W + 10·TF + 5·BM25p
    """
    import math

    chunks = fh.chunks or []
    has_meta_exact = any(
        (c.match_kind or "") == "exact"
        and (c.chunk_type or "body") == "metadata" for c in chunks
    )
    has_body_exact = any(
        (c.match_kind or "") == "exact"
        and (c.chunk_type or "body") != "metadata" for c in chunks
    )
    if (query_norm and fh.file_row.doc_number
            and query_norm == search_norm(fh.file_row.doc_number)):
        g = 4.0
    elif has_meta_exact:
        g = 3.0
    elif has_body_exact:
        g = 2.0
    elif (fh.match_kind or "") == "substring":
        g = 1.5
    else:
        g = 1.0

    w = 1.0
    if has_meta_exact:
        for attr, weight in _RANK_FIELD_PROBES:
            value = getattr(fh.file_row, attr, "") or ""
            if query_norm and query_norm in search_norm(value):
                w = max(w, weight)
        if w <= 1.0:
            w = 2.5  # matched the synthesized metadata prose

    tf_meta = sum(
        int(c.match_count or 0) for c in chunks
        if (c.chunk_type or "body") == "metadata" and (c.match_kind or "") == "exact"
    )
    tf_body = sum(
        int(c.match_count or 0) for c in chunks
        if (c.chunk_type or "body") != "metadata" and (c.match_kind or "") == "exact"
    )
    # Length normalization uses the DOCUMENT's real word count when the
    # engine projected it (SUM(chunks.word_count)); retrieved-chunk words
    # are only a fallback for projections that lack it.
    doc_words = int(getattr(fh.chunks[0], "doc_word_count", 0) or 0) if chunks else 0
    if doc_words <= 0:
        doc_words = sum(
            len((c.text or "").split()) for c in chunks
            if (c.chunk_type or "body") != "metadata"
        )
    tf = (
        w * math.log2(1 + min(tf_meta, 3))
        + math.log2(1 + min(tf_body, 8))
        / math.sqrt(max(doc_words / 500.0, 1.0))
    )

    best_bm25 = max((getattr(c, "bm25", 0.0) or 0.0) for c in chunks) \
        if chunks else 0.0
    # Normalized to 0..1 — a LIGHT tie-break worth at most 5 points, not
    # the earlier ×100 scale that let BM25 outweigh field weights.
    bm25n = (best_bm25 / bm25_scale) if bm25_scale > 0 else 0.0

    return 1000.0 * g + 40.0 * w + 10.0 * tf + 5.0 * bm25n


def _group_results_by_file(results: List[SearchResult]) -> List[FileHit]:
    """Dedupe per-chunk SearchResults into one FileHit per doc_id."""
    by_doc: dict[str, List[SearchResult]] = defaultdict(list)
    for r in results:
        by_doc[r.doc_id].append(r)
    out: List[FileHit] = []
    for doc_id, chunks in by_doc.items():
        deduped: dict[int, SearchResult] = {}
        for chunk in chunks:
            key = int(chunk.chunk_id or 0)
            prev = deduped.get(key)
            if prev is None:
                deduped[key] = chunk
                continue
            prev_rank = (int(prev.match_count or 0), float(prev.score or 0.0))
            chunk_rank = (int(chunk.match_count or 0), float(chunk.score or 0.0))
            if chunk_rank > prev_rank:
                deduped[key] = chunk
        values = list(deduped.values())
        has_body_exact_boxes = any(
            (getattr(c, "match_kind", "") or "") == "exact"
            and (getattr(c, "chunk_type", "body") or "body") != "metadata"
            and bool(getattr(c, "match_bboxes", None))
            for c in values
        )
        chunks = []
        seen_match_boxes_by_page: dict[int, list[list[float]]] = {}
        for chunk in sorted(
            values, key=_chunk_quality_rank, reverse=True
        ):
            if (
                has_body_exact_boxes
                and (getattr(chunk, "match_kind", "") or "") == "exact"
                and (getattr(chunk, "chunk_type", "body") or "body") == "metadata"
            ):
                # Metadata duplicates visible PDF text but has no word bboxes,
                # so do not count/show it when body matches already exist.
                continue
            if (getattr(chunk, "match_kind", "") or "") == "exact":
                boxes = list(getattr(chunk, "match_bboxes", None) or [])
                if boxes:
                    page = int(chunk.page or 0)
                    seen = seen_match_boxes_by_page.setdefault(page, [])
                    filtered = []
                    for bb in boxes:
                        if any(_is_same_match_bbox(bb, old) for old in seen):
                            continue
                        filtered.append(bb)
                    if not filtered:
                        continue
                    chunk.match_bboxes = filtered
                    chunk.match_count = len(filtered)
                    chunk.score = float(len(filtered))
                    seen.extend(filtered)
                elif has_body_exact_boxes:
                    continue
            if any(_is_near_duplicate_chunk(chunk, kept) for kept in chunks):
                continue
            chunks.append(chunk)
        chunks.sort(
            key=lambda c: (int(c.match_count or 0), float(c.score or 0.0)),
            reverse=True,
        )
        score_total = sum((c.score or 0.0) for c in chunks[:3])
        match_total = sum(int(c.match_count or 0) for c in chunks)
        head = chunks[0]
        # Synthesize a FileRow from the headline chunk (search_engine
        # already projects raw kie_* under the legacy attribute names).
        fr = FileRow(
            doc_id=doc_id,
            dossier_id=getattr(head, "dossier_id", None),
            file_name=head.file_name or "",
            file_path=head.file_path or "",
            subject=head.subject or "",
            doc_number=head.doc_number or "",
            issue_org=head.issue_org or "",
            issue_org_superior=getattr(head, "issue_org_superior", "") or "",
            signer_name=head.signer_name or "",
            issue_date=head.issue_date or "",
            doc_type=getattr(head, "doc_type", "") or "",
            secrecy_mark="",
            page_count=0,
            dossier_title=head.dossier_title or "",
            fonds=getattr(head, "fonds", "") or "",
            catalog=getattr(head, "catalog", "") or "",
            dossier_code=getattr(head, "dossier_code", "") or "",
        )
        out.append(FileHit(
            file_row=fr,
            chunks=chunks,
            score_total=score_total,
            match_total=match_total,
            match_kind=getattr(head, "match_kind", "") or "",
        ))
    # Tiered relevance (match class first, field + saturated frequency +
    # BM25 tie-break after) — the pure (match_total, score_total) ordering
    # biased long documents and repeated boilerplate.
    bm25_scale = max(
        (getattr(r, "bm25", 0.0) or 0.0) for r in results
    ) if results else 0.0
    query_norm = search_norm(results[0].query or "") if results else ""
    for fh in out:
        fh.relevance = _filehit_relevance(fh, query_norm, bm25_scale)
    out.sort(
        key=lambda fh: (fh.relevance, fh.match_total, fh.score_total),
        reverse=True,
    )
    return out


# ---------------------------------------------------------------- Cards


_CARD_QSS = (
    f"QFrame#Card {{ background: {COLOR_SURFACE}; "
    f"  border: 1px solid {COLOR_BORDER}; border-radius: {RADIUS_MD}px; }}"
    f"QFrame#Card:hover {{ border-color: {COLOR_ACCENT}; }}"
    f"QFrame#Card[active=\"true\"] {{ background: {COLOR_ELEVATED}; "
    f"  border-color: {COLOR_ACCENT}; }}"
    f"QFrame#Card[dropTarget=\"true\"] {{ background: {COLOR_ELEVATED}; "
    f"  border: 2px solid {COLOR_GREEN}; }}"
    f"QFrame#Card[dragReady=\"true\"] {{ border-color: {COLOR_GREEN}; }}"
    f"QFrame#Card QLabel {{ background: transparent; border: none; }}"
)


def _set_card_active(card: QWidget, active: bool) -> None:
    card.setProperty("active", "true" if active else "false")
    card.style().unpolish(card)
    card.style().polish(card)
    card.update()


class _GroupHeader(QLabel):
    def __init__(self, text: str, count: int, parent=None):
        self._source_text = text
        self._count = count
        super().__init__(parent)
        self.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; font: 700 11px '{FONT_UI}';"
            f" padding: 6px 4px 2px 4px; text-transform: uppercase;"
            " background: transparent; border: none;"
        )
        self.update_texts()

    def update_texts(self) -> None:
        label = translations.localize_text(self._source_text)
        self.setText(f"{label} ({self._count})")


class _DossierCard(QFrame):
    """Browse-mode card for one dossier. Body click → emit `clicked` (show
    the dossier's info in the right panel only); the ☰ list button /
    double-click → emit `open_clicked` (jump to the Tài liệu tab with this
    dossier's file list); the small ✏ button on the right → emit
    `edit_clicked` so the host can pop a DossierInfoDialog without losing
    the body click."""
    clicked = Signal(int)
    open_clicked = Signal(int)
    edit_clicked = Signal(int)
    selection_changed = Signal(int, bool)

    def __init__(self, dossier: DossierRow, parent=None):
        super().__init__(parent)
        self.dossier = dossier
        self.setObjectName("Card")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(_CARD_QSS)
        grid = QGridLayout(self)
        grid.setContentsMargins(SP[3], SP[2], SP[3], SP[2])
        grid.setHorizontalSpacing(SP[2])
        grid.setVerticalSpacing(SP[1])

        self._cb = QCheckBox()
        self._cb.setStyleSheet(
            "QCheckBox::indicator { width: 16px; height: 16px; }"
        )
        self._cb.toggled.connect(
            lambda checked: self.selection_changed.emit(
                self.dossier.dossier_id, checked
            )
        )
        grid.addWidget(self._cb, 0, 0, Qt.AlignmentFlag.AlignTop)

        self._title_label = QLabel()
        self._title_label.setWordWrap(True)
        self._title_label.setStyleSheet(f"color: {COLOR_TEXT}; font: 600 13px '{FONT_UI}';")
        grid.addWidget(self._title_label, 0, 1)

        stats_text = _dossier_stats_text(dossier, localized=True)
        self._stats_label = None
        if stats_text:
            self._stats_label = QLabel(stats_text)
            self._stats_label.setWordWrap(True)
            self._stats_label.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font: 11px '{FONT_UI}';")
            # Span the full card width so the area below the selection rail is
            # useful without cramming text into the checkbox column itself.
            grid.addWidget(self._stats_label, 2, 0, 1, 3)

        codes_line = _dossier_code_line(dossier)
        if codes_line:
            codes_lbl = QLabel(codes_line)
            codes_lbl.setStyleSheet(
                f"color: {COLOR_TEXT_SECONDARY}; font: 11px '{FONT_UI}';"
            )
            grid.addWidget(codes_lbl, 1, 1)

        btn_edit = QPushButton("📝")
        btn_edit.setFixedSize(30, 24)
        btn_edit.setToolTip("Sửa thông tin hồ sơ")
        btn_edit.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_edit.setStyleSheet(
            f"QPushButton {{ background: transparent;"
            f" border: 1px solid {COLOR_BORDER};"
            f" border-radius: 4px;"
            f" font: 13px 'Segoe UI Emoji'; padding: 0; }}"
            f"QPushButton:hover {{ background: {COLOR_ELEVATED};"
            f" border-color: {COLOR_ACCENT}; }}"
        )
        btn_edit.clicked.connect(lambda _checked=False: self.edit_clicked.emit(
            self.dossier.dossier_id
        ))
        grid.addWidget(btn_edit, 0, 2, Qt.AlignmentFlag.AlignTop)

        btn_open = QPushButton("☰")
        btn_open.setFixedSize(30, 24)
        btn_open.setToolTip(
            "Xem danh sách tài liệu trong hồ sơ này (hoặc nhấp đúp vào thẻ)"
        )
        btn_open.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_open.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {COLOR_ACCENT};"
            f" border: 1px solid {COLOR_BORDER}; border-radius: 4px;"
            f" font: 600 14px 'Segoe UI Symbol'; padding: 0; }}"
            f"QPushButton:hover {{ background: {COLOR_ELEVATED};"
            f" border-color: {COLOR_ACCENT}; }}"
        )
        btn_open.clicked.connect(lambda _checked=False: self.open_clicked.emit(
            self.dossier.dossier_id
        ))
        grid.addWidget(btn_open, 0, 3, Qt.AlignmentFlag.AlignTop)
        grid.setColumnStretch(1, 1)
        self.update_texts()

    def update_texts(self) -> None:
        self._title_label.setText("📁 " + _dossier_display_title(self.dossier))
        if self._stats_label is not None:
            self._stats_label.setText(
                _dossier_stats_text(self.dossier, localized=True)
            )

    def set_checked(self, checked: bool) -> None:
        self._cb.blockSignals(True)
        self._cb.setChecked(checked)
        self._cb.blockSignals(False)

    def set_active(self, active: bool) -> None:
        _set_card_active(self, active)

    def mousePressEvent(self, e):
        # Only emit `clicked` when the press lands on the body, not the
        # checkbox/select gutter/edit/open buttons.
        if e.button() == Qt.MouseButton.LeftButton:
            pos = e.position().toPoint()
            child = self.childAt(pos)
            if isinstance(child, (QPushButton, QCheckBox)):
                super().mousePressEvent(e)
                return
            # The visual select column is wider than the checkbox itself.
            # Treat clicks in this gutter as selection, so bulk-selecting many
            # dossiers does not accidentally open a dossier.
            cb_geo = self._cb.geometry()
            gutter_right = max(cb_geo.right() + SP[2] + SP[3], 56)
            if pos.x() <= gutter_right:
                self._cb.setChecked(not self._cb.isChecked())
                e.accept()
                return
            self.clicked.emit(self.dossier.dossier_id)
        super().mousePressEvent(e)

    def mouseDoubleClickEvent(self, e):
        # Double-click on the body is the keyboard-free shortcut for the
        # "Xem tài liệu" button: enter the dossier's document list.
        if e.button() == Qt.MouseButton.LeftButton:
            pos = e.position().toPoint()
            child = self.childAt(pos)
            if isinstance(child, (QPushButton, QCheckBox)):
                super().mouseDoubleClickEvent(e)
                return
            cb_geo = self._cb.geometry()
            gutter_right = max(cb_geo.right() + SP[2] + SP[3], 56)
            if pos.x() <= gutter_right:
                super().mouseDoubleClickEvent(e)
                return
            self.open_clicked.emit(self.dossier.dossier_id)
            return
        super().mouseDoubleClickEvent(e)


_FILE_REORDER_MIME = "application/x-scanindex-repository-doc-id"


class _RepositoryListScrollArea(QScrollArea):
    """List scroll area that remains usable during a long-press file drag."""

    _EDGE_SCROLL_MARGIN = 56
    _EDGE_SCROLL_STEP = 28
    _EDGE_SCROLL_INTERVAL_MS = 45
    _WHEEL_SCROLL_STEP = 72

    def __init__(self, parent=None):
        super().__init__(parent)
        self._reorder_drag_active = False
        self._edge_scroll_timer = QTimer(self)
        self._edge_scroll_timer.setInterval(self._EDGE_SCROLL_INTERVAL_MS)
        self._edge_scroll_timer.timeout.connect(self._scroll_for_drag_edge)

    def begin_reorder_drag(self) -> None:
        if self._reorder_drag_active:
            return
        self._reorder_drag_active = True
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        self._edge_scroll_timer.start()

    def end_reorder_drag(self) -> None:
        if not self._reorder_drag_active:
            return
        self._reorder_drag_active = False
        self._edge_scroll_timer.stop()
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)

    @classmethod
    def _edge_scroll_direction(cls, y: int, height: int) -> int:
        if y < 0 or y >= height:
            return 0
        margin = min(cls._EDGE_SCROLL_MARGIN, max(1, height // 2))
        if y < margin:
            return -1
        if y >= height - margin:
            return 1
        return 0

    def _cursor_in_viewport(self) -> Optional[object]:
        point = self.viewport().mapFromGlobal(QCursor.pos())
        return point if self.viewport().rect().contains(point) else None

    def _apply_drag_wheel_delta(self, delta_y: int) -> bool:
        if not delta_y:
            return False
        direction = -1 if delta_y > 0 else 1
        bar = self.verticalScrollBar()
        bar.setValue(bar.value() + direction * self._WHEEL_SCROLL_STEP)
        return True

    def _scroll_for_drag_edge(self) -> None:
        if not self._reorder_drag_active:
            return
        point = self._cursor_in_viewport()
        if point is None:
            return
        direction = self._edge_scroll_direction(
            point.y(), self.viewport().height()
        )
        if not direction:
            return
        bar = self.verticalScrollBar()
        bar.setValue(bar.value() + direction * self._EDGE_SCROLL_STEP)

    def eventFilter(self, watched, event):
        if (self._reorder_drag_active
                and event.type() == QEvent.Type.Wheel
                and self._cursor_in_viewport() is not None
                and self._apply_drag_wheel_delta(event.angleDelta().y())):
            event.accept()
            return True
        return super().eventFilter(watched, event)


class _FileCard(QFrame):
    """Browse-mode card for one file inside a dossier. Has a checkbox at
    the top-left for multi-select bulk delete, and a body click area that
    emits `clicked(doc_id)` to open the file. Holding the body briefly arms
    native drag/drop reorder within the current dossier."""
    clicked = Signal(str)
    selection_changed = Signal(str, bool)   # (doc_id, checked)
    reorder_requested = Signal(str, str, bool)  # dragged, target, insert_after

    _REORDER_MIME = _FILE_REORDER_MIME
    _LONG_PRESS_MS = 450

    def __init__(self, file: FileRow, ordinal: int = 0,
                 allow_reorder: bool = True, parent=None):
        super().__init__(parent)
        self.file = file
        self.ordinal = ordinal
        self._allow_reorder = bool(allow_reorder)
        self.setObjectName("Card")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(_CARD_QSS)
        self.setAcceptDrops(self._allow_reorder)
        self.setToolTip(
            "Giữ chuột rồi kéo để sắp xếp thứ tự văn bản"
            if self._allow_reorder else
            "Chọn sắp xếp theo số thứ tự để kéo thả văn bản"
        )
        self._press_pos = None
        self._drag_ready = False
        self._drag_started = False
        self._active_reorder_scroll = None
        self._long_press_timer = QTimer(self)
        self._long_press_timer.setSingleShot(True)
        self._long_press_timer.timeout.connect(self._arm_drag)
        grid = QGridLayout(self)
        grid.setContentsMargins(SP[3], SP[2], SP[3], SP[2])
        grid.setHorizontalSpacing(SP[2])
        grid.setVerticalSpacing(SP[1])

        # Selection stays in its own action cell; the stored document ordinal
        # is part of the title so it reads naturally as "03. Trích yếu...".
        from PySide6.QtWidgets import QCheckBox
        self._cb = QCheckBox()
        self._cb.setStyleSheet(
            f"QCheckBox::indicator {{ width: 16px; height: 16px; }}"
        )
        self._cb.toggled.connect(
            lambda checked: self.selection_changed.emit(self.file.doc_id, checked)
        )
        grid.addWidget(self._cb, 0, 0, Qt.AlignmentFlag.AlignTop)

        title_text = _file_card_title(file, ordinal)
        self._title_label = QLabel(title_text)
        self._title_label.setProperty("_scanindex_i18n_skip", True)
        self._title_label.setWordWrap(True)
        self._title_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._title_label.setStyleSheet(f"color: {COLOR_TEXT}; font: 600 13px '{FONT_UI}';")
        grid.addWidget(self._title_label, 0, 1)

        meta_text = _file_summary_text(file, localized=True)
        self._meta_label = None
        if meta_text:
            self._meta_label = QLabel(meta_text)
            self._meta_label.setWordWrap(True)
            self._meta_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            self._meta_label.setStyleSheet(f"color: {COLOR_TEXT_SECONDARY}; font: 11px '{FONT_UI}';")
            grid.addWidget(self._meta_label, 1, 0, 1, 2)

        if file.file_name:
            fn = QLabel(f"📄 {file.file_name}")
            fn.setProperty("_scanindex_i18n_skip", True)
            fn.setWordWrap(True)
            fn.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            fn.setStyleSheet(
                f"color: {COLOR_GREEN}; font: 600 11px '{FONT_UI}';"
            )
            grid.addWidget(fn, 2, 0, 1, 2)
        grid.setColumnStretch(1, 1)

    def update_texts(self) -> None:
        self._title_label.setText(_file_card_title(self.file, self.ordinal))
        if self._meta_label is not None:
            self._meta_label.setText(
                _file_summary_text(self.file, localized=True)
            )

    def set_checked(self, checked: bool) -> None:
        self._cb.blockSignals(True)
        self._cb.setChecked(checked)
        self._cb.blockSignals(False)

    def set_ordinal(self, ordinal: int) -> None:
        """Update the title prefix after a reorder without rebuilding."""
        self.ordinal = ordinal
        self._title_label.setText(_file_card_title(self.file, ordinal))

    def set_active(self, active: bool) -> None:
        _set_card_active(self, active)

    def _set_drag_property(self, name: str, value: bool) -> None:
        self.setProperty(name, "true" if value else "false")
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def _reset_press_state(self) -> None:
        self._long_press_timer.stop()
        if self._active_reorder_scroll is not None:
            self._active_reorder_scroll.end_reorder_drag()
            self._active_reorder_scroll = None
        self._press_pos = None
        self._drag_ready = False
        self._set_drag_property("dragReady", False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def _arm_drag(self) -> None:
        if self._press_pos is None or not self._allow_reorder:
            return
        self._drag_ready = True
        self._active_reorder_scroll = self._find_reorder_scroll_area()
        if self._active_reorder_scroll is not None:
            self._active_reorder_scroll.begin_reorder_drag()
        self._set_drag_property("dragReady", True)
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    def _find_reorder_scroll_area(self) -> Optional[_RepositoryListScrollArea]:
        widget = self.parentWidget()
        while widget is not None:
            if isinstance(widget, _RepositoryListScrollArea):
                return widget
            widget = widget.parentWidget()
        return None

    def _start_drag(self) -> None:
        self._drag_started = True
        mime = QMimeData()
        mime.setData(self._REORDER_MIME, self.file.doc_id.encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        self.setCursor(Qt.CursorShape.ClosedHandCursor)
        drag.exec(Qt.DropAction.MoveAction)
        self._reset_press_state()

    def _dragged_doc_id(self, e) -> str:
        if not e.mimeData().hasFormat(self._REORDER_MIME):
            return ""
        return bytes(e.mimeData().data(self._REORDER_MIME)).decode(
            "utf-8", errors="ignore"
        )

    def _set_drop_target(self, active: bool) -> None:
        self._set_drag_property("dropTarget", active)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            child = self.childAt(e.position().toPoint())
            from PySide6.QtWidgets import QCheckBox
            if isinstance(child, QCheckBox):
                super().mousePressEvent(e)
                return
            self._press_pos = e.position().toPoint()
            self._drag_started = False
            if self._allow_reorder:
                self._long_press_timer.start(self._LONG_PRESS_MS)
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if (self._press_pos is not None
                and e.buttons() & Qt.MouseButton.LeftButton):
            distance = (
                e.position().toPoint() - self._press_pos
            ).manhattanLength()
            if self._drag_ready and distance >= QApplication.startDragDistance():
                self._start_drag()
                e.accept()
                return
            if not self._drag_ready and distance >= QApplication.startDragDistance():
                self._reset_press_state()
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self._press_pos is not None:
            should_click = not self._drag_started
            self._reset_press_state()
            if should_click:
                self.clicked.emit(self.file.doc_id)
            e.accept()
            return
        super().mouseReleaseEvent(e)

    def dragEnterEvent(self, e):
        dragged_doc_id = self._dragged_doc_id(e)
        if dragged_doc_id and dragged_doc_id != self.file.doc_id:
            self._set_drop_target(True)
            e.acceptProposedAction()
            return
        e.ignore()

    def dragMoveEvent(self, e):
        dragged_doc_id = self._dragged_doc_id(e)
        if dragged_doc_id and dragged_doc_id != self.file.doc_id:
            e.acceptProposedAction()
            return
        e.ignore()

    def dragLeaveEvent(self, e):
        self._set_drop_target(False)
        super().dragLeaveEvent(e)

    def dropEvent(self, e):
        self._set_drop_target(False)
        dragged_doc_id = self._dragged_doc_id(e)
        if not dragged_doc_id or dragged_doc_id == self.file.doc_id:
            e.ignore()
            return
        insert_after = e.position().y() >= self.height() / 2
        self.reorder_requested.emit(
            dragged_doc_id, self.file.doc_id, insert_after
        )
        e.acceptProposedAction()


# ---------------------------------------------------------------------------
# Virtualized search results (QListView + model + delegate)
# ---------------------------------------------------------------------------
# The widget-card list materializes one QFrame per hit; at thousands of
# results that is tens of thousands of live widgets (RAM + scroll jank).
# The model/delegate pair paints the same card look with ONLY the visible
# rows realized — O(viewport) regardless of result count.

_GROUP_ROW_H = 38
_HIT_ROW_H = 96


class _SearchResultsModel(QAbstractListModel):
    """Rows: ("group", label, count) or ("hit", rank, rank_width, FileHit)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._entries: list[tuple] = []
        self._active_doc_id: str = ""

    # ---- Qt model ----
    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._entries)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or role != Qt.ItemDataRole.UserRole:
            return None
        return self._entries[index.row()]

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    # ---- population / state ----
    def set_entries(self, entries: list[tuple]) -> None:
        self.beginResetModel()
        self._entries = list(entries or [])
        self.endResetModel()

    def entry_at(self, row: int):
        if 0 <= row < len(self._entries):
            return self._entries[row]
        return None

    def set_active(self, doc_id: str) -> None:
        if doc_id == self._active_doc_id:
            return
        old, new = self._active_doc_id, doc_id or ""
        self._active_doc_id = new

        def _touch(d):
            for row, e in enumerate(self._entries):
                if e[0] == "hit" and e[3].file_row.doc_id == d:
                    self.dataChanged.emit(
                        self.index(row, 0), self.index(row, 0))
                    return

        if old:
            _touch(old)
        if new:
            _touch(new)

    @property
    def active_doc_id(self) -> str:
        return self._active_doc_id


class _SearchHitDelegate(QStyledItemDelegate):
    """Paints group headers + hit cards; emits clicks (incl. the inline
    "Mở hồ sơ" pill, hit-tested against the same rect it was painted at)."""

    hit_clicked = Signal(str)      # doc_id
    open_dossier = Signal(int)     # dossier_id

    _BTN_W, _BTN_H = 84, 24

    def sizeHint(self, option, index):
        e = index.data(Qt.ItemDataRole.UserRole)
        if e and e[0] == "group":
            return QSize(option.rect.width(), _GROUP_ROW_H)
        return QSize(option.rect.width(), _HIT_ROW_H)

    # ---- geometry helpers (shared by paint + hit-test) ----
    def _button_rect(self, option) -> QRect:
        m = SP[3]
        return QRect(
            option.rect.right() - m - self._BTN_W,
            option.rect.bottom() - m - 4 - self._BTN_H,
            self._BTN_W, self._BTN_H,
        )

    def _elided(self, painter, text: str, width: int) -> str:
        fm = painter.fontMetrics()
        return fm.elidedText(text, Qt.TextElideMode.ElideRight, max(10, width))

    def paint(self, painter, option, index):
        e = index.data(Qt.ItemDataRole.UserRole)
        if e is None:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = option.rect.adjusted(SP[2], 3, -SP[2], -3)

        if e[0] == "group":
            _, label, count = e
            painter.setPen(QColor(COLOR_TEXT_SECONDARY))
            f = painter.font()
            f.setPointSizeF(8.5)
            f.setBold(True)
            painter.setFont(f)
            painter.drawText(
                r.adjusted(SP[2], 0, 0, 0),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                f"{label.upper()} · {count}",
            )
            painter.restore()
            return

        _, rank, rank_width, fh = e
        f_row = fh.file_row
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        try:
            active = index.model().active_doc_id == f_row.doc_id
        except Exception:
            active = False
        bg = COLOR_ELEVATED if active else COLOR_SURFACE
        border = COLOR_ACCENT if (active or hovered) else COLOR_BORDER
        path = QPainterPath()
        path.addRoundedRect(
            QRectF(r.x(), r.y(), r.width(), r.height()), RADIUS_MD, RADIUS_MD)
        painter.fillPath(path, QColor(bg))
        painter.setPen(QPen(QColor(border), 1))
        painter.drawPath(path)

        pad = SP[3]
        text_r = r.adjusted(pad, 6, -pad, -6)
        # Rank badge (right column, monospace).
        rank_text = str(rank or 0).rjust(rank_width or 2)
        painter.setPen(QColor(COLOR_TEXT_MUTED))
        rf = painter.font()
        rf.setFamily(FONT_MONO)
        rf.setPointSizeF(8.5)
        painter.setFont(rf)
        painter.drawText(
            QRect(text_r.right() - 44, text_r.y(), 44, 18),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            rank_text,
        )
        body_w = text_r.width() - 52

        # Title (bold).
        painter.setPen(QColor(COLOR_TEXT))
        tf = painter.font()
        tf.setFamily(FONT_UI)
        tf.setPointSizeF(10.5)
        tf.setBold(True)
        painter.setFont(tf)
        title = f_row.subject or f_row.file_name or f_row.doc_id[:14]
        painter.drawText(
            QRect(text_r.x(), text_r.y(), body_w, 20),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            self._elided(painter, title, body_w),
        )
        # Meta line.
        painter.setPen(QColor(COLOR_TEXT_SECONDARY))
        mf = painter.font()
        mf.setBold(False)
        mf.setPointSizeF(8.5)
        painter.setFont(mf)
        meta = _file_summary_text(f_row, localized=True)
        if meta:
            painter.drawText(
                QRect(text_r.x(), text_r.y() + 20, body_w, 16),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                self._elided(painter, meta, body_w),
            )
        # File name (green).
        if f_row.file_name:
            painter.setPen(QColor(COLOR_GREEN))
            ff = painter.font()
            ff.setBold(True)
            painter.setFont(ff)
            painter.drawText(
                QRect(text_r.x(), text_r.y() + 36, body_w, 16),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                self._elided(painter, f"📄 {f_row.file_name}", body_w),
            )
        # Archive row + button pill.
        bits = []
        if f_row.fonds:
            bits.append(f"Phông {f_row.fonds}")
        if f_row.catalog:
            bits.append(f"Mục lục {f_row.catalog}")
        if f_row.dossier_code:
            bits.append(f"Hồ sơ {f_row.dossier_code}")
        painter.setPen(QColor(COLOR_TEXT_MUTED))
        af = painter.font()
        af.setBold(False)
        af.setPointSizeF(8)
        painter.setFont(af)
        if bits:
            btn = self._button_rect(option)
            painter.drawText(
                QRect(text_r.x(), btn.y(), text_r.width() - self._BTN_W - SP[3], self._BTN_H),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                self._elided(painter, " · ".join(bits),
                             text_r.width() - self._BTN_W - SP[3] * 2),
            )
        if f_row.dossier_id is not None:
            btn = self._button_rect(option)
            bp = QPainterPath()
            bp.addRoundedRect(QRectF(btn), 4, 4)
            painter.fillPath(bp, QColor(COLOR_SURFACE))
            painter.setPen(QPen(QColor(COLOR_BORDER), 1))
            painter.drawPath(bp)
            painter.setPen(QColor(COLOR_ACCENT))
            bf = painter.font()
            bf.setBold(True)
            bf.setPointSizeF(8)
            painter.setFont(bf)
            painter.drawText(btn, Qt.AlignmentFlag.AlignCenter, "Mở hồ sơ")
        painter.restore()

    def editorEvent(self, event, model, option, index) -> bool:
        if event.type() != QEvent.Type.MouseButtonRelease:
            return False
        e = index.data(Qt.ItemDataRole.UserRole)
        if e is None or e[0] != "hit":
            return False
        fh = e[3]
        if fh.file_row.dossier_id is not None and \
                self._button_rect(option).contains(event.position().toPoint()):
            self.open_dossier.emit(int(fh.file_row.dossier_id))
            return True
        self.hit_clicked.emit(fh.file_row.doc_id)
        return True


class _SearchHitCard(QFrame):
    """Search-mode card: file with N matching chunks, dedup score badge."""
    clicked = Signal(str)  # doc_id
    open_dossier = Signal(int)

    def __init__(self, hit: FileHit, parent=None, *,
                 rank: int = 0, rank_width: int = 2):
        super().__init__(parent)
        self.hit = hit
        self.rank = int(rank or 0)
        self.rank_width = max(2, int(rank_width or 2))
        self.setObjectName("Card")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(_CARD_QSS)
        v = QVBoxLayout(self)
        v.setContentsMargins(SP[3], SP[2], SP[3], SP[2])
        v.setSpacing(SP[1])

        f = hit.file_row
        title_text = self._ranked_title()
        self._title_label = QLabel(title_text)
        self._title_label.setProperty("_scanindex_i18n_skip", True)
        self._title_label.setWordWrap(True)
        self._title_label.setStyleSheet(f"color: {COLOR_TEXT}; font: 600 13px '{FONT_UI}';")
        v.addWidget(self._title_label)

        meta_text = _file_summary_text(f, localized=True)
        self._meta_label = None
        if meta_text:
            self._meta_label = QLabel(meta_text)
            self._meta_label.setWordWrap(True)
            self._meta_label.setStyleSheet(f"color: {COLOR_TEXT_SECONDARY}; font: 11px '{FONT_UI}';")
            v.addWidget(self._meta_label)

        # File name — segments split from the same long source PDF often
        # share KIE metadata; only the file name distinguishes them.
        if f.file_name:
            fn = QLabel(f"📄 {f.file_name}")
            fn.setProperty("_scanindex_i18n_skip", True)
            fn.setWordWrap(True)
            fn.setStyleSheet(
                f"color: {COLOR_GREEN}; font: 600 11px '{FONT_UI}';"
            )
            v.addWidget(fn)

        archive_bits = []
        if f.fonds:
            archive_bits.append(f"Phông {f.fonds}")
        if f.catalog:
            archive_bits.append(f"Mục lục {f.catalog}")
        if f.dossier_code:
            archive_bits.append(f"Hồ sơ {f.dossier_code}")
        if f.dossier_id is not None:
            archive_row = QHBoxLayout()
            archive_row.setSpacing(SP[2])
            if archive_bits:
                archive_label = QLabel(" · ".join(archive_bits))
                archive_label.setProperty("_scanindex_i18n_skip", True)
                archive_label.setWordWrap(True)
                archive_label.setStyleSheet(
                    f"color: {COLOR_TEXT_MUTED}; font: 10px '{FONT_UI}';"
                )
                archive_row.addWidget(archive_label, 1)
            else:
                archive_row.addStretch(1)
            self._open_dossier_btn = QPushButton("Mở hồ sơ")
            self._open_dossier_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self._open_dossier_btn.setFixedHeight(24)
            self._open_dossier_btn.setStyleSheet(
                f"QPushButton {{ background: transparent; color: {COLOR_ACCENT};"
                f" border: 1px solid {COLOR_BORDER}; border-radius: 4px;"
                f" padding: 0 8px; font: 600 10px '{FONT_UI}'; }}"
                f"QPushButton:hover {{ background: {COLOR_ELEVATED}; }}"
            )
            self._open_dossier_btn.clicked.connect(
                lambda _checked=False, did=int(f.dossier_id):
                    self.open_dossier.emit(did)
            )
            archive_row.addWidget(self._open_dossier_btn)
            v.addLayout(archive_row)

        # Footer: keep user-facing labels meaningful; raw scores are internal
        # ranking numbers, not percentages.
        self._footer = QLabel()
        self._footer.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font: 10px '{FONT_UI}';")
        v.addWidget(self._footer)
        self.update_texts()

    def update_texts(self) -> None:
        f = self.hit.file_row
        self._title_label.setText(self._ranked_title())
        if self._meta_label is not None:
            self._meta_label.setText(
                _file_summary_text(self.hit.file_row, localized=True)
            )
        n = len(self.hit.chunks)
        is_metadata = all(
            (getattr(c, "chunk_type", "body") or "body") == "metadata"
            for c in self.hit.chunks
        )
        if self.hit.match_kind == "fuzzy":
            source = f"{n} thông tin gần giống" if is_metadata else f"{n} đoạn gần giống"
            suffix_source = "{} lần gần giống"
        else:
            source = f"{n} thông tin khớp" if is_metadata else f"{n} đoạn khớp"
            suffix_source = "{} lần xuất hiện"
        count = int(self.hit.match_total or 0)
        suffix = (
            " · " + translations.localize_text(suffix_source.format(count))
            if count > 0 else ""
        )
        self._footer.setText(
            f"<span style='color:{COLOR_ACCENT}'>"
            f"{translations.localize_text(source)}</span>{suffix}"
        )

    def _ranked_title(self) -> str:
        f = self.hit.file_row
        title = (
            f.subject
            or f.file_name
            or translations.localize_text("(không tiêu đề)")
        )
        if self.rank <= 0:
            return title
        return f"{self.rank:0{self.rank_width}d}. {title}"

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            child = self.childAt(e.position().toPoint())
            if isinstance(child, QPushButton):
                super().mousePressEvent(e)
                return
            self.clicked.emit(self.hit.file_row.doc_id)
        super().mousePressEvent(e)

    def set_active(self, active: bool) -> None:
        _set_card_active(self, active)


class _SnippetCard(QFrame):
    """Right-panel card for one matching chunk inside the selected file.
    Click → host scrolls PDF to (page, bbox) and highlights the rect."""
    clicked = Signal(int)  # chunk_id

    def __init__(self, result: SearchResult, parent=None):
        super().__init__(parent)
        self.result = result
        self.setObjectName("Card")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(_CARD_QSS)
        v = QVBoxLayout(self)
        v.setContentsMargins(SP[2], SP[2], SP[2], SP[2])
        v.setSpacing(SP[1])

        is_meta = (getattr(result, "chunk_type", "body") == "metadata")
        self._head = QLabel()
        self._head.setStyleSheet(f"color: {COLOR_TEXT_SECONDARY}; font: 11px '{FONT_UI}';")
        v.addWidget(self._head)

        is_fuzzy = (getattr(result, "match_kind", "") or "") == "fuzzy"
        text = _snippet_context_text(
            result.text or "",
            getattr(result, "query", "") or "",
            fuzzy=is_fuzzy,
        )
        body_html = _highlight_query_html(
            text,
            getattr(result, "query", "") or "",
            fuzzy=is_fuzzy,
        )
        self._has_body_content = bool(body_html)
        body = QLabel(
            body_html or translations.localize_text("(không có nội dung)")
        )
        self._body = body
        body.setProperty("_scanindex_i18n_skip", True)
        body.setTextFormat(Qt.TextFormat.RichText)
        body.setWordWrap(True)
        body.setStyleSheet(f"color: {COLOR_TEXT}; font: 12px '{FONT_UI}';")
        v.addWidget(body)
        self.update_texts()

    def update_texts(self) -> None:
        is_meta = (getattr(self.result, "chunk_type", "body") == "metadata")
        badge = (
            "📋 " + translations.localize_text("Tóm tắt văn bản")
            if is_meta
            else translations.localize_text(f"Trang {self.result.page or '?'}")
        )
        kind = getattr(self.result, "match_kind", "") or ""
        if kind == "fuzzy":
            suffix = " · " + translations.localize_text("gần giống")
        else:
            count = int(getattr(self.result, "match_count", 0) or 0)
            suffix = (
                " · " + translations.localize_text(f"{count} lần")
                if count > 0 else ""
            )
        self._head.setText(f"<b style='color:{COLOR_ACCENT}'>{badge}</b>{suffix}")
        if not self._has_body_content:
            self._body.setText(translations.localize_text("(không có nội dung)"))

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.result.chunk_id or 0)
        super().mousePressEvent(e)

    def set_active(self, active: bool) -> None:
        _set_card_active(self, active)


# ---------------------------------------------------------------- Right panel


class _RightPanel(QWidget):
    """File metadata + (optionally) matching-snippet list.
    Snippet click bubbles up so the host can scroll PDF + highlight bbox."""
    snippet_clicked = Signal(object)  # SearchResult
    show_in_folder = Signal(str)
    edit_metadata = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(f"background: {COLOR_BG};")
        self._current_pdf: Optional[Path] = None
        self._display_dossier: Optional[DossierRow] = None
        self._display_file: Optional[FileRow] = None
        self._display_chunks: list[SearchResult] = []
        self._display_archive_path = Path()
        self._display_message_source = ""
        self._snippet_cards_by_id: dict[int, _SnippetCard] = {}
        self._active_chunk_id = 0
        self._build_ui()

    def _build_ui(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(SP[2], SP[2], SP[2], SP[2])
        v.setSpacing(SP[2])

        # File info card (always visible)
        self._info_box = QLabel(
            translations.localize_text("Chọn 1 hồ sơ hoặc văn bản để xem chi tiết")
        )
        self._info_box.setProperty("_scanindex_i18n_skip", True)
        self._info_box.setWordWrap(True)
        self._info_box.setTextFormat(Qt.TextFormat.RichText)
        self._info_box.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; font: 12px '{FONT_UI}';"
            f" background: {COLOR_SURFACE}; border: 1px solid {COLOR_BORDER};"
            f" border-radius: {RADIUS_MD}px; padding: {SP[2]}px;"
        )
        self._info_box.setAlignment(Qt.AlignmentFlag.AlignTop)
        v.addWidget(self._info_box)

        # Action buttons
        action_row = QHBoxLayout()
        action_row.setSpacing(SP[2])

        def _style_action_button(btn: QPushButton) -> None:
            btn.setFixedHeight(34)
            btn.setMinimumWidth(128)
            btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(
                f"QPushButton {{ background: transparent;"
                f" color: {COLOR_TEXT}; border: 1px solid {COLOR_BORDER};"
                f" border-radius: 4px; padding: 0 10px;"
                f" font: 600 12px '{FONT_UI}'; }}"
                f"QPushButton:hover {{ background: {COLOR_ELEVATED};"
                f" border-color: {COLOR_ACCENT}; }}"
                f"QPushButton:disabled {{ color: {COLOR_TEXT_MUTED};"
                f" border-color: {COLOR_BORDER}; background: {COLOR_SURFACE}; }}"
            )

        self.btn_show_in_folder = QPushButton("Thư mục chứa")
        self.btn_show_in_folder.setEnabled(False)
        self.btn_show_in_folder.clicked.connect(self._on_show_in_folder)
        _style_action_button(self.btn_show_in_folder)
        action_row.addWidget(self.btn_show_in_folder, 1)

        self.btn_edit_metadata = QPushButton("Sửa metadata")
        self.btn_edit_metadata.setEnabled(False)
        self.btn_edit_metadata.clicked.connect(self.edit_metadata.emit)
        _style_action_button(self.btn_edit_metadata)
        action_row.addWidget(self.btn_edit_metadata, 1)
        v.addLayout(action_row)

        # Section header for snippets — only visible during search
        self._snippets_header = QLabel("Đoạn liên quan")
        self._snippets_header.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; font: 600 11px '{FONT_UI}';"
            f" text-transform: uppercase; padding: 4px 2px;"
        )
        self._snippets_header.setVisible(False)
        v.addWidget(self._snippets_header)

        # Scrollable snippet list
        self._snip_scroll = QScrollArea()
        self._snip_scroll.setWidgetResizable(True)
        self._snip_scroll.setStyleSheet(
            f"QScrollArea {{ background: {COLOR_BG}; border: none; }}"
        )
        self._snip_inner = QWidget()
        self._snip_inner.setStyleSheet(f"background: {COLOR_BG};")
        self._snip_layout = QVBoxLayout(self._snip_inner)
        self._snip_layout.setContentsMargins(0, 0, 0, 0)
        self._snip_layout.setSpacing(SP[2])
        self._snip_layout.addStretch(1)
        self._snip_scroll.setWidget(self._snip_inner)
        self._snip_scroll.setVisible(False)
        v.addWidget(self._snip_scroll, 1)

    # ------ public API ------

    def show_dossier(self, d: DossierRow):
        self._display_message_source = ""
        self._display_dossier = d
        self._display_file = None
        self._display_chunks = []

        # Keep codes and names on separate rows. Combining them made the
        # right panel compact but difficult to scan and impossible to copy as
        # distinct archival fields. Rows with no value are hidden entirely.
        def row(label: str, value) -> Optional[str]:
            text = _single_line(value)
            if not text:
                return None
            return translations.localize_text(
                f"<b>{label}:</b> {html.escape(text)}"
            )

        rows = [
            row("Mã phông", d.fonds),
            row("Tên phông", d.fonds_name),
            row("Số mục lục", d.catalog),
            row("Tên mục lục", d.catalog_name),
            row("Số hồ sơ", d.dossier_code),
            row("Tên hồ sơ", d.title),
        ]
        if d.doc_count:
            rows.append(translations.localize_text(f"<b>Số văn bản:</b> {d.doc_count}"))
        if d.page_count:
            rows.append(translations.localize_text(f"<b>Tổng số trang:</b> {d.page_count}"))
        if d.start_date or d.end_date:
            span = " – ".join(filter(None, (d.start_date, d.end_date)))
            if span:
                rows.append(translations.localize_text(f"<b>Thời gian:</b> {span}"))
        stored = _format_stored_at(getattr(d, "stored_at", None))
        if stored:
            rows.append(translations.localize_text(f"<b>Thời điểm lưu:</b> {stored}"))
        rows = [r for r in rows if r]
        if rows:
            self._info_box.setText("<br>".join(rows))
        else:
            self._info_box.setText(
                translations.localize_text("(không có thông tin hồ sơ)")
            )
        self._active_chunk_id = 0
        self._snippet_cards_by_id.clear()
        self._set_snippets_visible(False)
        self._set_actions_enabled(False)

    def show_file(self, f: FileRow, archive_path: Path,
                  chunks: Optional[List[SearchResult]] = None):
        self._display_message_source = ""
        self._display_file = f
        self._display_dossier = None
        self._display_chunks = list(chunks or [])
        self._display_archive_path = Path(archive_path)
        rows = []
        if f.dossier_title:
            rows.append(translations.localize_text(f"<b>Hồ sơ:</b> {f.dossier_title}"))
        if f.subject:
            rows.append(translations.localize_text(f"<b>Trích yếu:</b> {f.subject}"))
        meta_text = _file_summary_text(f, localized=True)
        if meta_text:
            rows.append(translations.localize_text(f"<b>Thông tin:</b> {meta_text}"))
        # Same pair the digitization screen shows: Trang số = trang đầu tiên
        # của văn bản trong hồ sơ (ToSoTrangSo), Số trang = số trang PDF.
        if f.trang_so:
            rows.append(translations.localize_text(
                f"<b>Trang số:</b> {int(f.trang_so)}"
            ))
        if f.page_count:
            rows.append(translations.localize_text(
                f"<b>Số trang:</b> {int(f.page_count)}"
            ))
        if f.doc_type:
            doc_type = translations.localize_document_type(f.doc_type)
            rows.append(translations.localize_text(f"<b>Loại văn bản:</b> {doc_type}"))
        if f.secrecy_mark:
            color = _secrecy_mark_color(f.secrecy_mark)
            rows.append(translations.localize_text(
                f"<b>Độ mật:</b> <span style='color:{color}'>{html.escape(f.secrecy_mark)}</span>"
            ))
        if f.file_name:
            rows.append(translations.localize_text(
                f"<b>Tệp:</b> <span style='color:{COLOR_GREEN};"
                f" font-weight:600'>{f.file_name}</span>"
            ))
        self._info_box.setText(
            "<br>".join(rows) or translations.localize_text("(không có metadata)")
        )

        if f.file_path:
            pdf_abs = (archive_path / f.file_path).resolve()
            self._current_pdf = pdf_abs
            exists = pdf_abs.exists()
            self._set_actions_enabled(exists)
        else:
            self._current_pdf = None
            self._set_actions_enabled(False)
        self.btn_edit_metadata.setEnabled(bool(f.doc_id))

        self._set_snippets(chunks or [])

    def show_message(self, source_text: str) -> None:
        self._display_dossier = None
        self._display_file = None
        self._display_chunks = []
        self._display_message_source = str(source_text or "")
        self._info_box.setText(
            translations.localize_text(self._display_message_source)
        )

    def update_texts(self) -> None:
        if self._display_message_source:
            self.show_message(self._display_message_source)
        elif self._display_file is not None:
            self.show_file(
                self._display_file,
                self._display_archive_path,
                self._display_chunks,
            )
        elif self._display_dossier is not None:
            self.show_dossier(self._display_dossier)
        else:
            self._info_box.setText(
                translations.localize_text(
                    "Chọn 1 hồ sơ hoặc văn bản để xem chi tiết"
                )
            )

    # ------ internals ------

    def _set_snippets(self, chunks: List[SearchResult]):
        # Clear existing
        self._snippet_cards_by_id.clear()
        self._active_chunk_id = 0
        while self._snip_layout.count() > 1:
            item = self._snip_layout.takeAt(0)
            if item is None:
                continue
            w = item.widget()
            if w is not None:
                w.deleteLater()
        if not chunks:
            self._set_snippets_visible(False)
            return
        self._snippets_header.setText(f"Đoạn liên quan ({len(chunks)})")
        for c in chunks:
            card = _SnippetCard(c)
            card.clicked.connect(lambda _cid, cc=c: self.snippet_clicked.emit(cc))
            cid = int(c.chunk_id or 0)
            if cid:
                self._snippet_cards_by_id[cid] = card
            self._snip_layout.insertWidget(self._snip_layout.count() - 1, card)
        self._set_snippets_visible(True)
        first = int(chunks[0].chunk_id or 0) if chunks else 0
        if first:
            self.set_active_chunk(first)

    def set_active_chunk(self, chunk_id: int) -> None:
        self._active_chunk_id = int(chunk_id or 0)
        for cid, card in self._snippet_cards_by_id.items():
            card.set_active(cid == self._active_chunk_id)

    def _set_snippets_visible(self, on: bool):
        self._snippets_header.setVisible(on)
        self._snip_scroll.setVisible(on)

    def _set_actions_enabled(self, on: bool):
        self.btn_show_in_folder.setEnabled(on)
        if not on:
            self.btn_edit_metadata.setEnabled(False)

    def _on_show_in_folder(self):
        if not self._current_pdf or not self._current_pdf.exists():
            return
        self.show_in_folder.emit(str(self._current_pdf))


# ---------------------------------------------------------------- PDF pane


class _LegacyPdfPane(QWidget):
    """Center column: full PDF rendered as a vertical stack of page
    images. The user can scroll freely through every page; when a search
    snippet is clicked the pane scrolls to the target page and overlays
    the chunk's bbox in accent colour.

    Zoom: top-bar buttons + Ctrl+wheel; range 25%-300%, default 50% to
    match Bước 2's wide-screen layout. Re-render on zoom is cached per
    (path, zoom) so the second visit at the same zoom is instant."""

    _ZOOM_MIN = 0.25
    _ZOOM_MAX = 3.0
    _ZOOM_STEP = 0.25

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(f"background: {COLOR_BG};")
        self._current_pdf: Optional[Path] = None
        self._zoom = 0.5
        self._page_labels: List[QLabel] = []
        self._page_pixmaps: List[QPixmap] = []   # pristine, no bbox overlay
        # Pixmap cache keyed by (pdf_path_str, zoom): re-zooming back to
        # a level we've rendered before is instant.
        self._render_cache: dict[tuple[str, float], List[QPixmap]] = {}
        self._build_ui()

    def _build_ui(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(SP[2], SP[2], SP[2], SP[2])
        v.setSpacing(SP[1])

        # Zoom toolbar
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(SP[1])
        self._btn_zoom_out = QPushButton("−")
        self._btn_zoom_out.setFixedSize(28, 28)
        self._btn_zoom_out.clicked.connect(self._zoom_out)
        self._btn_zoom_in = QPushButton("+")
        self._btn_zoom_in.setFixedSize(28, 28)
        self._btn_zoom_in.clicked.connect(self._zoom_in)
        self._lbl_zoom = QLabel(f"{int(self._zoom * 100)}%")
        self._lbl_zoom.setFixedWidth(48)
        self._lbl_zoom.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lbl_zoom.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; font: 600 12px '{FONT_UI}';"
        )
        for w in (self._btn_zoom_out, self._btn_zoom_in):
            w.setStyleSheet(
                f"QPushButton {{ background: {COLOR_ELEVATED};"
                f" color: {COLOR_TEXT}; border: 1px solid {COLOR_BORDER};"
                f" border-radius: 4px; font: 600 14px '{FONT_UI}'; }}"
                f"QPushButton:hover {{ background: {COLOR_ACCENT};"
                f" color: white; border-color: {COLOR_ACCENT}; }}"
            )
        bar.addStretch(1)
        bar.addWidget(self._btn_zoom_out)
        bar.addWidget(self._lbl_zoom)
        bar.addWidget(self._btn_zoom_in)
        v.addLayout(bar)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setStyleSheet(
            f"QScrollArea {{ background: {COLOR_PANEL}; border: 1px solid {COLOR_BORDER};"
            f" border-radius: {RADIUS_MD}px; }}"
        )
        self._inner = QWidget()
        self._inner.setStyleSheet(f"background: {COLOR_PANEL};")
        self._inner_layout = QVBoxLayout(self._inner)
        self._inner_layout.setContentsMargins(SP[2], SP[2], SP[2], SP[2])
        self._inner_layout.setSpacing(SP[3])
        self._inner_layout.setAlignment(Qt.AlignmentFlag.AlignTop
                                         | Qt.AlignmentFlag.AlignHCenter)

        self._placeholder = QLabel("(chưa có trang để hiện)")
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; padding: {SP[5]}px; background: transparent;"
        )
        self._inner_layout.addWidget(self._placeholder)
        self._inner_layout.addStretch(1)

        self._scroll.setWidget(self._inner)
        v.addWidget(self._scroll, 1)

    # ── zoom ────────────────────────────────────────────────────────

    def _zoom_in(self):
        self._set_zoom(min(self._ZOOM_MAX, round(self._zoom + self._ZOOM_STEP, 2)))

    def _zoom_out(self):
        self._set_zoom(max(self._ZOOM_MIN, round(self._zoom - self._ZOOM_STEP, 2)))

    def _set_zoom(self, new_zoom: float):
        if abs(new_zoom - self._zoom) < 0.001:
            return
        self._zoom = new_zoom
        self._lbl_zoom.setText(f"{int(self._zoom * 100)}%")
        # Re-render at new zoom — cache hit makes this instant on
        # round-trips (e.g. user zooms in then back out).
        if self._current_pdf is not None:
            self._render_all_pages()

    def wheelEvent(self, e):
        if e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            if e.angleDelta().y() > 0:
                self._zoom_in()
            else:
                self._zoom_out()
            e.accept()
            return
        super().wheelEvent(e)

    def show_pdf(self, pdf_path: Path, page: int = 1,
                 bbox: Optional[List[float]] = None,
                 bboxes: Optional[List[List[float]]] = None,
                 highlight_style: str = "box"):
        """Render the whole PDF if `pdf_path` is new, then jump to `page`
        and draw either exact-match bboxes or the broader chunk bbox."""
        if pdf_path != self._current_pdf:
            self._current_pdf = pdf_path
            self._render_all_pages()
        self._highlight_page(page, bbox=bbox, bboxes=bboxes,
                             style=highlight_style)
        focus_bbox = (bboxes or [bbox or []])[0]
        self._scroll_to_page(page, focus_bbox=focus_bbox)

    def clear(self):
        self._current_pdf = None
        self._page_pixmaps = []
        self._clear_inner()
        self._placeholder = QLabel("(chưa có trang để hiện)")
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; padding: {SP[5]}px; background: transparent;"
        )
        self._inner_layout.addWidget(self._placeholder)
        self._inner_layout.addStretch(1)

    # ---------- internals ----------

    def _clear_inner(self):
        while self._inner_layout.count() > 0:
            item = self._inner_layout.takeAt(0)
            if item is None:
                continue
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._page_labels = []

    def _render_all_pages(self):
        """Render every page at `self._zoom` and mount QLabels in the
        scroll area. Pixmaps are cached per (path, zoom) so re-zooming
        back to a previous level is instant."""
        self._clear_inner()
        if not self._current_pdf or not self._current_pdf.exists():
            err = QLabel("(file PDF không tồn tại)")
            err.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; padding: {SP[5]}px;")
            self._inner_layout.addWidget(err)
            self._inner_layout.addStretch(1)
            return

        cache_key = (str(self._current_pdf), self._zoom)
        cached = self._render_cache.get(cache_key)
        if cached is not None:
            self._page_pixmaps = list(cached)
            for qpix in self._page_pixmaps:
                self._inner_layout.addWidget(self._make_page_label(qpix))
            self._inner_layout.addStretch(1)
            return

        try:
            import fitz
            self._page_pixmaps = []
            with fitz.open(str(self._current_pdf)) as doc:
                mat = fitz.Matrix(self._zoom, self._zoom)
                for idx in range(doc.page_count):
                    pix = doc[idx].get_pixmap(matrix=mat, alpha=False)
                    img = QImage(
                        pix.samples, pix.width, pix.height,
                        pix.stride, QImage.Format.Format_RGB888,
                    )
                    qpix = QPixmap.fromImage(img.copy())
                    self._page_pixmaps.append(qpix)
                    self._inner_layout.addWidget(self._make_page_label(qpix))
            self._inner_layout.addStretch(1)
            # Cap cache to last 6 (path, zoom) combos to avoid RAM blow-up.
            if len(self._render_cache) >= 6:
                # Drop a stale entry (insertion order preserved by dict).
                self._render_cache.pop(next(iter(self._render_cache)))
            self._render_cache[cache_key] = list(self._page_pixmaps)
        except Exception as e:
            err = QLabel(f"(không render được PDF: {e})")
            err.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; padding: {SP[5]}px;")
            self._inner_layout.addWidget(err)
            self._inner_layout.addStretch(1)

    def _make_page_label(self, qpix: QPixmap) -> QLabel:
        lbl = QLabel()
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setPixmap(qpix)
        lbl.setMinimumSize(qpix.size())
        lbl.setStyleSheet(
            f"background: white; border: 1px solid {COLOR_BORDER};"
        )
        self._page_labels.append(lbl)
        return lbl

    def _highlight_page(self, page: int,
                        bbox: Optional[List[float]] = None,
                        bboxes: Optional[List[List[float]]] = None,
                        style: str = "box"):
        """Reset every page label to its pristine pixmap, then redraw the
        requested bbox(es) on the target page. Exact/fuzzy lexical search
        passes word/phrase boxes when available."""
        for i, lbl in enumerate(self._page_labels):
            if i < len(self._page_pixmaps):
                lbl.setPixmap(self._page_pixmaps[i])
        idx = max(0, min(page - 1, len(self._page_labels) - 1))
        if not (0 <= idx < len(self._page_labels)):
            return
        boxes = [bb for bb in (bboxes or []) if bb and len(bb) == 4]
        if not boxes and bbox and len(bbox) == 4:
            boxes = [bbox]
        if not boxes:
            return
        pristine = self._page_pixmaps[idx]
        overlay = QPixmap(pristine)
        painter = QPainter(overlay)
        try:
            pen = QPen(QColor(COLOR_ACCENT))
            pen.setWidth(max(1, int(round(1.4 * self._zoom))) if style == "underline" else 3)
            painter.setPen(pen)
            for bb in boxes:
                x0, y0, x1, y1 = bb
                if style == "underline":
                    # Draw inside the text bbox near the baseline. Drawing
                    # below y1 looks visually detached on OCR text layers.
                    y = int((float(y0) + (float(y1) - float(y0)) * 0.88) * self._zoom)
                    painter.drawLine(
                        int(x0 * self._zoom),
                        y,
                        int(x1 * self._zoom),
                        y,
                    )
                else:
                    painter.drawRect(int(x0 * self._zoom), int(y0 * self._zoom),
                                     int((x1 - x0) * self._zoom),
                                     int((y1 - y0) * self._zoom))
        finally:
            painter.end()
        self._page_labels[idx].setPixmap(overlay)

    def _scroll_to_page(self, page: int,
                        focus_bbox: Optional[List[float]] = None):
        idx = max(0, min(page - 1, len(self._page_labels) - 1))
        if not (0 <= idx < len(self._page_labels)):
            return
        # Scroll so the target page label sits near the top of the viewport.
        target = self._page_labels[idx]
        if focus_bbox and len(focus_bbox) == 4:
            y = target.y() + int(float(focus_bbox[1]) * self._zoom) - 96
            self._scroll.verticalScrollBar().setValue(max(0, y))
        else:
            # Use ensureWidgetVisible with a small top margin so the target
            # page is comfortably visible.
            self._scroll.ensureWidgetVisible(target, 0, 24)


# Active Kho PDF pane. This intentionally reuses the shared continuous PDF
# viewer used elsewhere: cursor-anchored Ctrl+wheel zoom, hand-pan, smooth
# pixmap scaling, and async page rendering.
class _PdfPane(PdfViewerWidget):
    def __init__(self, parent=None):
        super().__init__(
            parent,
            fit_on_load=False,
            text_selection_available=True,
        )
        self._zoom = 0.5
        self._fit_mode = False
        self._update_zoom_label()
        self._btn_prev_file.setVisible(False)
        self._btn_next_file.setVisible(False)
        self._lbl_file.setVisible(False)
        self._file_nav_sep.setVisible(False)
        self._btn_fit.setVisible(False)
        self.setStyleSheet(f"background: {COLOR_BG};")


# ---------------------------------------------------------------- Add-file dialog


class _AddFileMetadataDialog(QWidget):
    """Modal dialog asking the user to fill 14 KIE fields for a PDF
    they're adding to an existing dossier. Subclasses QDialog via the
    shared imports below."""

    def __init__(self, *, pdf_path, body_chunk_count: int,
                 initial_doc_type: str = "Khác", parent=None):
        from PySide6.QtWidgets import QDialog
        # We override QWidget here as a marker; the actual instantiation
        # uses QDialog because Qt requires a true QDialog for exec().
        raise NotImplementedError(
            "Use _AddFileMetadataDialog._build(...) factory instead."
        )

    DialogCode = None  # filled in by factory below

    @classmethod
    def _build(cls, *, pdf_path, body_chunk_count, initial_doc_type, parent,
               initial_fields: Optional[dict] = None,
               window_title: str = "Thêm văn bản — Nhập thông tin",
               info_text: str = ""):
        """Construct as a real QDialog. Done lazily so the import of
        archive_doctype / KIE labels happens once at first use."""
        from PySide6.QtWidgets import (
            QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLineEdit,
            QTextEdit, QPushButton, QLabel, QComboBox, QFrame, QScrollArea,
            QWidget as _QW,
        )
        from scanindex.core.digitization.doctype import all_display_names

        dlg = QDialog(parent)
        dlg.setWindowTitle(window_title)
        dlg.setModal(True)
        dlg.setMinimumSize(640, 600)
        dlg.setStyleSheet(f"QDialog {{ background: {COLOR_BG}; }}")

        outer = QVBoxLayout(dlg)
        outer.setContentsMargins(SP[4], SP[4], SP[4], SP[3])
        outer.setSpacing(SP[3])

        title = QLabel(f"📄 {pdf_path.name}")
        title.setStyleSheet(f"color: {COLOR_TEXT}; font: 600 14px '{FONT_UI}';")
        outer.addWidget(title)

        info = QLabel(info_text or (
            f"Đã trích xuất <b>{body_chunk_count}</b> đoạn từ PDF. "
            "Điền thông tin metadata bên dưới rồi bấm Lưu."
        ))
        info.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font: 11px '{FONT_UI}';")
        info.setWordWrap(True)
        outer.addWidget(info)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {COLOR_BORDER};")
        outer.addWidget(sep)

        # Scrollable form area — 14 fields fit only if scrollable.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(f"QScrollArea {{ background: {COLOR_BG}; border: none; }}")
        body = _QW()
        form = QFormLayout(body)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setSpacing(SP[2])

        widgets: dict[str, object] = {}

        def _styled_line():
            w = QLineEdit()
            w.setStyleSheet(
                f"QLineEdit {{ background: {COLOR_INPUT};"
                f" border: 1px solid {COLOR_BORDER};"
                f" border-radius: 4px; color: {COLOR_TEXT};"
                f" padding: 4px 8px; font: 12px '{FONT_UI}'; }}"
                f"QLineEdit:focus {{ border-color: {COLOR_ACCENT}; }}"
            )
            return w

        def _styled_area(rows: int = 3):
            w = QTextEdit()
            w.setFixedHeight(28 * rows)
            w.setStyleSheet(
                f"QTextEdit {{ background: {COLOR_INPUT};"
                f" border: 1px solid {COLOR_BORDER};"
                f" border-radius: 4px; color: {COLOR_TEXT};"
                f" padding: 4px 8px; font: 12px '{FONT_UI}'; }}"
                f"QTextEdit:focus {{ border-color: {COLOR_ACCENT}; }}"
            )
            return w

        # 14 KIE field rows. Subject is required; everything else optional.
        # Spec: 10 trained + 3 marks + DOC_TYPE = 14 fields.
        # Order matches the synthesised metadata-chunk order so the form
        # reads top-to-bottom like the real document.
        rows = [
            ("kie_doc_type",            "Loại văn bản",        "combo"),
            ("kie_doc_number_symbol",   "Số ký hiệu",          "line"),
            ("kie_issue_org_superior",  "Cơ quan cấp trên",    "area2"),
            ("kie_issue_org_name",      "Cơ quan ban hành *",  "area2"),
            ("kie_place_date",          "Ngày tháng",          "line"),
            ("kie_doc_subject",         "Trích yếu *",         "area3"),
            ("kie_addressee",           "Kính gửi",            "area2"),
            ("kie_recipients",          "Nơi nhận",            "area3"),
            ("kie_signer_role",         "Chức vụ người ký",    "line"),
            ("kie_signer_name",         "Người ký",            "line"),
            ("kie_urgency_mark",        "Độ khẩn",             "line"),
            ("kie_secrecy_mark",        "Độ mật",              "secrecy"),
            ("kie_circulation_mark",    "Hình thức lưu hành",  "line"),
            ("kie_regime_header",       "Header chế độ",       "area2"),
        ]
        initial_fields = initial_fields or {}
        for col, label_vi, kind in rows:
            lbl = QLabel(label_vi)
            lbl.setStyleSheet(
                f"color: {COLOR_TEXT_SECONDARY}; font: 12px '{FONT_UI}';"
            )
            if kind == "combo":
                w = QComboBox()
                w.setEditable(True)
                translations.add_localized_combo_items(
                    w, all_display_names(), context="document_type"
                )
                current_text = initial_fields.get(col) or initial_doc_type
                if current_text:
                    translations.set_combo_value(w, current_text)
                w.setStyleSheet(
                    f"QComboBox {{ background: {COLOR_INPUT};"
                    f" border: 1px solid {COLOR_BORDER};"
                    f" border-radius: 4px; color: {COLOR_TEXT};"
                    f" padding: 4px 28px 4px 8px; font: 12px '{FONT_UI}'; }}"
                    + COMBOBOX_DROPDOWN_QSS
                )
            elif kind == "secrecy":
                # Độ mật is a closed 4-level set (same options as the
                # advance-search filter and Digitization Step 2), so a
                # combo beats free text. sort=False keeps the severity
                # order Thường → Tuyệt mật instead of A-Z.
                w = FuzzyComboBox(sort=False)
                translations.add_localized_combo_items(w, _CONFIDENTIALITY_OPTIONS)
                current = str(initial_fields.get(col) or "").strip()
                lower = current.lower()
                if not lower or lower == "thường":
                    w.setCurrentIndex(0)  # blank ≡ Thường (HSLTCQ convention)
                else:
                    match = next(
                        (o for o in _CONFIDENTIALITY_OPTIONS if o.lower() == lower),
                        None,
                    )
                    if match is not None:
                        current = match  # case-insensitive hit (e.g. MẬT → Mật)
                    else:
                        # Legacy/odd value (e.g. KIE-detected free text):
                        # append it so it stays selectable instead of
                        # silently falling back to item 0 on read-back.
                        w.addItem(current, current)
                    translations.set_combo_value(w, current)
                w.setStyleSheet(
                    f"QComboBox {{ background: {COLOR_INPUT};"
                    f" border: 1px solid {COLOR_BORDER};"
                    f" border-radius: 4px; color: {COLOR_TEXT};"
                    f" padding: 4px 28px 4px 8px; font: 12px '{FONT_UI}'; }}"
                    + COMBOBOX_DROPDOWN_QSS
                )
            elif kind == "line":
                w = _styled_line()
            elif kind == "area2":
                w = _styled_area(2)
            else:
                w = _styled_area(3)
            if col in initial_fields and kind not in ("combo", "secrecy"):
                value = str(initial_fields.get(col) or "")
                if isinstance(w, QTextEdit):
                    w.setPlainText(value)
                elif isinstance(w, QLineEdit):
                    w.setText(value)
            widgets[col] = w
            form.addRow(lbl, w)

        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_cancel = QPushButton("Hủy")
        btn_cancel.setStyleSheet(
            f"QPushButton {{ background: transparent;"
            f" border: 1px solid {COLOR_BORDER};"
            f" border-radius: 4px; color: {COLOR_TEXT_SECONDARY};"
            f" padding: 6px 14px; font: 12px '{FONT_UI}'; }}"
            f"QPushButton:hover {{ background: {COLOR_SURFACE}; color: {COLOR_TEXT}; }}"
        )
        btn_cancel.clicked.connect(dlg.reject)
        btn_row.addWidget(btn_cancel)

        btn_ok = QPushButton("💾 Lưu")
        btn_ok.setStyleSheet(
            f"QPushButton {{ background: {COLOR_ACCENT};"
            f" color: white; border: none; border-radius: 4px;"
            f" padding: 6px 18px; font: 600 12px '{FONT_UI}'; }}"
            f"QPushButton:hover {{ background: {COLOR_ACCENT_HOVER}; }}"
        )
        btn_ok.setDefault(True)
        btn_ok.clicked.connect(dlg.accept)
        btn_row.addWidget(btn_ok)
        outer.addLayout(btn_row)

        # Helper exposed on the dialog instance.
        def get_fields() -> dict:
            from PySide6.QtWidgets import QComboBox as _QC
            out = {}
            for col, w in widgets.items():
                if isinstance(w, _QC):
                    out[col] = translations.combo_value(w).strip()
                elif isinstance(w, QTextEdit):
                    out[col] = w.toPlainText().strip()
                else:
                    out[col] = w.text().strip()
            # "Thường" is the UI default for non-secret docs — the HSLTCQ
            # convention is an empty cell, so collapse it back to blank
            # (same rule as the digitization runner).
            if out.get("kie_secrecy_mark", "").lower() == "thường":
                out["kie_secrecy_mark"] = ""
            return out
        dlg.get_fields = get_fields
        return dlg


# Tiny indirection: the host code calls _AddFileMetadataDialog(...).exec()
# but we want a real QDialog. Override __new__ to return one built by
# the factory so existing call sites stay readable.
def _add_file_dialog_factory(*, pdf_path, body_chunk_count,
                              initial_doc_type, parent,
                              initial_fields=None,
                              window_title="Thêm văn bản — Nhập thông tin",
                              info_text=""):
    return _AddFileMetadataDialogBuilder._build(
        pdf_path=pdf_path,
        body_chunk_count=body_chunk_count,
        initial_doc_type=initial_doc_type,
        initial_fields=initial_fields,
        window_title=window_title,
        info_text=info_text,
        parent=parent,
    )


# Replace the placeholder class with the factory function (callsite
# treats it as if it were a class — `dlg.exec()` works since it's a
# real QDialog).
_AddFileMetadataDialogBuilder = _AddFileMetadataDialog
_AddFileMetadataDialog = _add_file_dialog_factory  # type: ignore


# ---------------------------------------------------------------- Main screen


class RepositoryScreen(ScreenContent):
    """Searchable archive of OCR'd PDFs with dossier-browse default state."""

    log_message = Signal(str, str)

    # Left-column states
    _MODE_DOSSIERS = "dossiers"     # browsing dossier list
    _MODE_FILES    = "files"        # browsing files inside one dossier
    _MODE_SEARCH   = "search"       # search hits across all dossiers
    _ACTION_BAR_BREAKPOINT = 1280

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setStyleSheet(f"background: {COLOR_BG};")

        self._archive_path: Path = _read_repository_path_setting()
        self._store: Optional[ArchiveStore] = None
        self._index: Optional[HybridIndex] = None
        self._engine: Optional[SearchEngine] = None
        self._importer: Optional[Importer] = None

        self._import_worker = None  # legacy slot kept so request_cancel() doesn't AttributeError
        self._prepare_add_worker: Optional[_PrepareAddFileWorker] = None
        self._add_worker: Optional[QThread] = None
        self._search_worker: Optional[SearchWorker] = None
        self._zip_kho_worker: Optional[QThread] = None
        self._busy = False

        # Accept dropping exported HSLTCQ ZIPs anywhere on this screen →
        # direct Kho import (see dragEnterEvent / _import_zip_paths).
        self.setAcceptDrops(True)

        self._mode = self._MODE_DOSSIERS
        self._current_dossier: Optional[DossierRow] = None
        self._current_file: Optional[FileRow] = None
        self._metadata_edit_dialog = None
        self._search_hits: List[FileHit] = []
        self._hits_by_doc: dict[str, FileHit] = {}
        self._search_query = ""
        self._search_filters: dict = {}
        self._search_mode = "content"
        # "Tra cứu hồ sơ" scope state — committed keyword + filters that drive
        # the dossier list (re-applied on sort change / re-render).
        self._dossier_query = ""
        self._dossier_filters: dict = {}
        # Per-scope toolbar memory: switching tabs must not lose the keyword
        # or the ▾ panel state of the tab the user is leaving.
        self._scope_states: dict[str, dict] = {
            "dossiers": {"query": "", "panel_open": False},
            "search":   {"query": "", "panel_open": False},
        }
        self._search_scroll_value = 0
        self._search_selected_doc_id = ""
        self._search_rank_by_doc: dict[str, int] = {}
        # Search result cards are considerably more expensive to construct
        # than dossier/file cards because they contain match summaries and
        # per-result actions.  Keep them detached-but-alive while the user
        # briefly opens a dossier, then put the same widgets back when the
        # Kết quả tìm kiếm tab is selected again.
        self._search_list_cache: list[QWidget] = []
        self._search_list_cache_sort = ""
        # Word-level match bbox cache (LRU) + off-thread hydration state.
        self._bbox_cache: "OrderedDict[int, list]" = OrderedDict()
        self._hydrate_worker: Optional[_HydrateWorker] = None
        self._hydrate_pending: Optional[tuple] = None
        # Set when the user clicks Hủy; labels the (partial) results.
        self._search_cancel_requested = False
        self._dossier_scroll_value = 0
        self._return_to_search = False
        self._sort_by_mode = {
            self._MODE_DOSSIERS: "archive",
            self._MODE_FILES: "ordinal",
            self._MODE_SEARCH: "relevance",
        }
        self._configuring_sort = False
        self._active_doc_id = ""
        self._doc_cards_by_id: dict[str, QFrame] = {}
        # Multi-select state for bulk delete in dossier/file-list modes.
        self._selected_doc_ids: set[str] = set()
        # Selected dossier/file cards for bulk actions in browse modes.
        self._file_cards_by_id: dict[str, "_FileCard"] = {}
        self._selected_dossier_ids: set[int] = set()
        self._dossier_cards_by_id: dict[int, "_DossierCard"] = {}
        # Dossier whose info is shown in the right panel (body-click target);
        # entering its file list is a separate explicit action.
        self._active_dossier_view_id = 0

        self._build_ui()
        self._open_store()
        # Land on dossier list after store opens.
        self._show_dossier_list()

    # ------ ScreenContent overrides ------

    def required_models(self) -> list[str]:
        return []

    def is_busy(self) -> bool:
        return self._busy

    def request_cancel(self) -> None:
        if self._import_worker and self._import_worker.isRunning():
            self._import_worker.cancel()
        if self._prepare_add_worker and self._prepare_add_worker.isRunning():
            self._prepare_add_worker.cancel()

    def header_info_widget(self) -> QWidget:
        """Compact repository status block for the outer screen header."""
        return self._header_info_widget

    # ------ UI ------

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SP[3], SP[3], SP[3], SP[3])
        outer.setSpacing(SP[2])

        self._header_info_widget = self._build_status_bar()
        # Tab row (Hồ sơ / Tài liệu) sits ABOVE the search box because the
        # search box's meaning (tên hồ sơ vs nội dung OCR) follows the
        # active tab. Criteria panels sit under the search bar, next to the
        # "Tìm" button; the list/export actions live in the same top row as
        # the tabs.
        outer.addWidget(self._build_action_bar())
        outer.addWidget(self._build_toolbar())
        self._filter_panel = self._build_filter_panel()
        self._filter_panel.setVisible(False)
        outer.addWidget(self._filter_panel)
        self._dossier_filter_panel = self._build_dossier_filter_panel()
        self._dossier_filter_panel.setVisible(False)
        outer.addWidget(self._dossier_filter_panel)
        self._on_search_mode_changed()

        # 3-column splitter
        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.setChildrenCollapsible(False)
        self._splitter.setStyleSheet(
            f"QSplitter::handle {{ background: {COLOR_BORDER}; width: 3px; }}"
        )
        # Column 1: list
        self._list_column = self._build_list_column()
        self._splitter.addWidget(self._list_column)
        # Column 2: PDF
        self._pdf_pane = _PdfPane()
        self._splitter.addWidget(self._pdf_pane)
        # Column 3: right panel (info + snippets)
        self._right_panel = _RightPanel()
        self._right_panel.snippet_clicked.connect(self._on_snippet_clicked)
        self._right_panel.show_in_folder.connect(self._on_show_in_folder)
        self._right_panel.edit_metadata.connect(self._on_edit_current_file_metadata)
        self._splitter.addWidget(self._right_panel)
        self._right_panel.setMinimumWidth(240)
        self._right_panel.setMaximumWidth(420)
        self._pdf_pane.setMinimumWidth(240)

        self._splitter.setCollapsible(0, False)
        self._splitter.setCollapsible(1, False)
        self._splitter.setCollapsible(2, False)
        self._splitter.setStretchFactor(0, 0)  # list: bounded
        self._splitter.setStretchFactor(1, 1)  # PDF: flex
        self._splitter.setStretchFactor(2, 0)  # right panel: bounded
        self._splitter.setSizes([320, 620, 320])
        outer.addWidget(self._splitter, 1)

    def _build_action_bar(self) -> QWidget:
        bar = QFrame()
        self._action_bar = bar
        bar.setObjectName("repositoryActionBar")
        # Ignore the one-row size hint horizontally so a direct restore/resize
        # to 1024px can reach the breakpoint before the controls are reflowed.
        bar.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        bar.setStyleSheet(
            f"QFrame#repositoryActionBar {{ background: {COLOR_PANEL};"
            f" border: 1px solid {COLOR_BORDER}; border-radius: {RADIUS_MD}px; }}"
        )
        action_layout = QVBoxLayout(bar)
        action_layout.setContentsMargins(SP[2], SP[1], SP[2], SP[1])
        action_layout.setSpacing(SP[1])

        h = QHBoxLayout()
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(SP[2])
        action_layout.addLayout(h)

        self._action_filter_row = QWidget()
        self._action_filter_row.setStyleSheet("background: transparent; border: none;")
        filter_row = QHBoxLayout(self._action_filter_row)
        filter_row.setContentsMargins(0, 0, 0, 0)
        filter_row.setSpacing(SP[2])
        action_layout.addWidget(self._action_filter_row)

        self._action_primary_layout = h
        self._action_filter_layout = filter_row
        action_h = 34

        tab_qss = (
            f"QPushButton {{ background: transparent; color: {COLOR_TEXT_SECONDARY};"
            f" border: 1px solid {COLOR_BORDER}; border-radius: 4px;"
            f" padding: 0 10px; font: 600 12px '{FONT_UI}'; }}"
            f"QPushButton:hover {{ background: {COLOR_ELEVATED}; color: {COLOR_TEXT};"
            f" border-color: {COLOR_ACCENT}; }}"
            f"QPushButton:checked {{ background: {COLOR_ELEVATED}; color: {COLOR_ACCENT};"
            f" border-color: {COLOR_ACCENT}; }}"
            f"QPushButton:checked:disabled {{ background: {COLOR_ELEVATED};"
            f" color: {COLOR_ACCENT}; border-color: {COLOR_ACCENT}; }}"
        )

        self._btn_back_to_dossiers = QPushButton("Hồ sơ")
        self._btn_back_to_dossiers.setCheckable(True)
        self._btn_back_to_dossiers.setFixedHeight(action_h)
        self._btn_back_to_dossiers.setMinimumWidth(64)
        self._btn_back_to_dossiers.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        self._btn_back_to_dossiers.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_back_to_dossiers.setStyleSheet(
            tab_qss
            + f"QPushButton {{ font: 700 13px '{FONT_UI}'; }}"
        )
        self._btn_back_to_dossiers.clicked.connect(self._show_dossier_list)
        h.addWidget(self._btn_back_to_dossiers)

        self._btn_back_to_search = QPushButton("Tài liệu")
        self._btn_back_to_search.setCheckable(True)
        self._btn_back_to_search.setVisible(True)
        self._btn_back_to_search.setFixedHeight(action_h)
        self._btn_back_to_search.setMinimumWidth(92)
        self._btn_back_to_search.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_back_to_search.setStyleSheet(tab_qss)
        self._btn_back_to_search.clicked.connect(self._show_search_tab)
        h.addWidget(self._btn_back_to_search)

        # Contextual back button: while the Tài liệu tab shows a dossier's
        # file list, this returns to the preserved search results.
        self._btn_back_to_results = QPushButton("← Kết quả tra cứu")
        self._btn_back_to_results.setVisible(False)
        self._btn_back_to_results.setFixedHeight(action_h)
        self._btn_back_to_results.setMinimumWidth(140)
        self._btn_back_to_results.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_back_to_results.setStyleSheet(tab_qss)
        self._btn_back_to_results.clicked.connect(self._restore_search_results)
        h.addWidget(self._btn_back_to_results)

        self._list_count_label = QLabel("Đang tải hồ sơ…")
        self._list_count_label.setFixedHeight(action_h)
        self._list_count_label.setMinimumWidth(72)
        self._list_count_label.setTextFormat(Qt.TextFormat.RichText)
        self._list_count_label.setWordWrap(False)
        self._list_count_label.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        self._list_count_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._list_count_label.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; background: {COLOR_SURFACE};"
            f" border: 1px solid {COLOR_BORDER}; border-radius: 4px;"
            f" padding: 0 10px; font: 12px '{FONT_UI}';"
        )
        h.addWidget(self._list_count_label, 1)

        self._sort_label = QLabel("Sắp xếp:")
        self._sort_label.setFixedHeight(action_h)
        self._sort_label.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        self._sort_label.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        self._sort_label.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; background: transparent; border: none;"
            f" font: 11px '{FONT_UI}';"
        )
        h.addWidget(self._sort_label)

        self._sort_combo = QComboBox()
        self._sort_combo.setToolTip("Sắp xếp danh sách hiện tại")
        self._sort_combo.setFixedHeight(action_h)
        self._sort_combo.setMinimumWidth(120)
        # Size to the current option set instead of a fixed wide minimum, so
        # the freed space goes to the dossier-title row. The cap keeps the
        # "Phông → Mục lục → Hồ sơ"-style longest option from hogging the row.
        self._sort_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContents
        )
        self._sort_combo.setMaximumWidth(250)
        self._style_dossier_filter_combo(self._sort_combo)
        self._sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        h.addWidget(self._sort_combo)

        self._btn_add_file = QPushButton("+ Thêm VB")
        self._btn_add_file.setToolTip("Thêm văn bản vào hồ sơ này")
        self._btn_add_file.setVisible(False)
        self._btn_add_file.setFixedHeight(action_h)
        self._btn_add_file.setMinimumWidth(84)
        self._btn_add_file.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        self._btn_add_file.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_add_file.setStyleSheet(
            f"QPushButton {{ background: {COLOR_ACCENT}; color: white;"
            f" border: none; border-radius: 4px;"
            f" padding: 0 12px; font: 600 12px '{FONT_UI}'; }}"
            f"QPushButton:hover {{ background: {COLOR_ACCENT_HOVER}; }}"
        )
        self._btn_add_file.clicked.connect(self._on_add_file_clicked)
        h.addWidget(self._btn_add_file)

        self._btn_export_dossier_zip = QPushButton("Xuất ZIP")
        self._btn_export_dossier_zip.setToolTip(
            "Xuất hồ sơ nén (ZIP kèm .json.zst)"
        )
        self._btn_export_dossier_zip.setVisible(False)
        self._btn_export_dossier_zip.setFixedHeight(action_h)
        self._btn_export_dossier_zip.setMinimumWidth(80)
        self._btn_export_dossier_zip.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        self._btn_export_dossier_zip.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_export_dossier_zip.setStyleSheet(
            f"QPushButton {{ background: {COLOR_GREEN}; color: white;"
            f" border: none; border-radius: 4px;"
            f" padding: 0 12px; font: 600 12px '{FONT_UI}'; }}"
            f"QPushButton:hover {{ background: {COLOR_GREEN_HOVER}; color: {COLOR_TEXT}; }}"
            f"QPushButton:disabled {{ background: {COLOR_ELEVATED};"
            f" color: {COLOR_TEXT_MUTED}; border: 1px solid {COLOR_BORDER}; }}"
        )
        self._btn_export_dossier_zip.clicked.connect(
            self._on_export_dossier_zip_clicked
        )
        h.addWidget(self._btn_export_dossier_zip)

        self._btn_clear_selection = QPushButton("Bỏ chọn")
        self._btn_clear_selection.setVisible(False)
        self._btn_clear_selection.setFixedHeight(action_h)
        self._btn_clear_selection.setMinimumWidth(92)
        self._btn_clear_selection.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        self._btn_clear_selection.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_clear_selection.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {COLOR_TEXT_SECONDARY};"
            f" border: 1px solid {COLOR_BORDER}; border-radius: 4px;"
            f" padding: 0 12px; font: 12px '{FONT_UI}'; }}"
            f"QPushButton:hover {{ background: {COLOR_ELEVATED}; color: {COLOR_TEXT}; }}"
        )
        self._btn_clear_selection.clicked.connect(self._clear_selection)
        h.addWidget(self._btn_clear_selection)

        self._btn_bulk_delete = QPushButton("🗑︎")
        self._btn_bulk_delete.setVisible(False)
        self._btn_bulk_delete.setFixedSize(42, action_h)
        self._btn_bulk_delete.setToolTip("Xóa mục đã chọn")
        self._btn_bulk_delete.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_bulk_delete.setStyleSheet(
            f"QPushButton {{ background: transparent; color: #f87171;"
            f" border: 1px solid #7f1d1d; border-radius: 4px;"
            f" padding: 0; font: 600 13px 'Segoe UI Symbol'; }}"
            f"QPushButton:hover {{ background: #7f1d1d; color: white; }}"
        )
        self._btn_bulk_delete.clicked.connect(self._on_bulk_delete_selected)
        h.addWidget(self._btn_bulk_delete)

        self._action_all_widgets = [
            self._btn_back_to_dossiers,
            self._btn_back_to_search,
            self._btn_back_to_results,
            self._list_count_label,
            self._sort_label,
            self._sort_combo,
            self._btn_add_file,
            self._btn_export_dossier_zip,
            self._btn_clear_selection,
            self._btn_bulk_delete,
        ]
        self._action_main_widgets = [
            self._btn_back_to_dossiers,
            self._btn_back_to_search,
            self._btn_back_to_results,
            self._list_count_label,
            self._btn_add_file,
            self._btn_export_dossier_zip,
            self._btn_clear_selection,
            self._btn_bulk_delete,
        ]
        self._action_secondary_widgets = [
            self._sort_label,
            self._sort_combo,
        ]
        self._action_bar_narrow: Optional[bool] = None
        # Start with the compact arrangement so the layout itself does not
        # impose a >1024px minimum before the first resize event arrives.
        self._set_action_bar_narrow(True)
        return bar

    def _set_action_bar_narrow(self, narrow: bool) -> None:
        if not hasattr(self, "_action_primary_layout"):
            return
        narrow = bool(narrow)
        if self._action_bar_narrow is narrow:
            return
        self._action_bar_narrow = narrow

        primary = self._action_primary_layout
        secondary = self._action_filter_layout
        for widget in self._action_all_widgets:
            primary.removeWidget(widget)
            secondary.removeWidget(widget)

        if narrow:
            for widget in self._action_main_widgets:
                primary.addWidget(
                    widget, 1 if widget is self._list_count_label else 0
                )
            for widget in self._action_secondary_widgets:
                secondary.addWidget(widget)
            self._action_filter_row.setVisible(True)
        else:
            for widget in self._action_all_widgets:
                primary.addWidget(
                    widget, 1 if widget is self._list_count_label else 0
                )
            self._action_filter_row.setVisible(False)

        self._action_filter_row.updateGeometry()
        if hasattr(self, "_action_bar"):
            self._action_bar.updateGeometry()
        if hasattr(self, "_splitter"):
            self._set_splitter_for_mode()

    # Content-driven reflow: keep everything on ONE row for as long as the
    # controls genuinely fit, instead of switching to the two-row arrangement
    # at a fixed window width (which wasted the free space to the right of
    # the tabs on medium-wide windows).
    def _action_bar_single_row_width(self) -> int:
        """Width the tab row would need to show every visible control on a
        single line: sum of per-widget hints + spacing + bar margins."""
        layout = getattr(self, "_action_primary_layout", None)
        spacing = layout.spacing() if layout is not None else SP[2]
        total = 0
        count = 0
        for widget in self._action_all_widgets:
            if widget.isHidden():
                continue
            if widget is self._list_count_label:
                # The only stretchy item. Its rich-text minimumSizeHint
                # (dossier title + stats) is far wider than the space it
                # actually needs — it has always squeezed/clipped when the
                # row is tight, so count a sane floor..cap instead.
                total += min(
                    max(72, widget.minimumSizeHint().width()), 280
                )
            else:
                # Layouts clamp hints to maximumWidth (e.g. the sort combo
                # cap), so the estimate must clamp the same way.
                hint = max(
                    widget.sizeHint().width(), widget.minimumWidth() or 0
                )
                max_w = widget.maximumWidth()
                if 0 < max_w < 16777215:  # QWIDGETSIZE_MAX
                    hint = min(hint, max_w)
                total += hint
            count += 1
        if count:
            total += spacing * (count - 1)
        # Action-bar inner margins + the screen's outer layout margins +
        # a little slack so borderline widths stay on one row.
        total += 2 * (SP[2] + SP[3]) + 8
        return total

    def _reflow_action_bar(self, available_width: Optional[int] = None) -> None:
        if not hasattr(self, "_action_primary_layout"):
            return
        if available_width is None:
            available_width = self.width()
        self._set_action_bar_narrow(
            self._action_bar_single_row_width() > int(available_width)
        )

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reflow_action_bar(event.size().width())

    def _style_dossier_filter_combo(self, combo: QComboBox) -> None:
        combo.setStyleSheet(
            f"QComboBox {{ background: {COLOR_SURFACE}; color: {COLOR_TEXT};"
            f" border: 1px solid {COLOR_BORDER}; border-radius: 4px;"
            f" padding: 0 28px 0 10px; font: 12px '{FONT_UI}'; }}"
            f"QComboBox:hover {{ border-color: {COLOR_ACCENT}; }}"
            f"QComboBox:disabled {{ color: {COLOR_TEXT_MUTED};"
            f" background: {COLOR_ELEVATED}; }}"
            f"QComboBox QAbstractItemView {{ background: {COLOR_SURFACE};"
            f" color: {COLOR_TEXT}; border: 1px solid {COLOR_BORDER};"
            f" selection-background-color: {COLOR_ACCENT}; }}"
            + COMBOBOX_DROPDOWN_QSS
        )

    def _configure_sort_options(self, mode: str) -> None:
        options = {
            self._MODE_DOSSIERS: [
                ("Phông → Mục lục → Hồ sơ", "archive"),
                ("Mới lưu gần đây", "stored_desc"),
                ("Tên hồ sơ A–Z", "title_asc"),
            ],
            self._MODE_FILES: [
                ("Số thứ tự văn bản", "ordinal"),
                ("Ngày ban hành mới nhất", "date_desc"),
                ("Tên văn bản A–Z", "title_asc"),
            ],
            self._MODE_SEARCH: [
                ("Phù hợp nhất", "relevance"),
                ("Ngày ban hành mới nhất", "date_desc"),
                ("Phông → Mục lục → Hồ sơ", "archive"),
                ("Tên văn bản A–Z", "title_asc"),
            ],
        }.get(mode, [])
        current = self._sort_by_mode.get(mode, options[0][1] if options else "")
        self._configuring_sort = True
        self._sort_combo.blockSignals(True)
        try:
            self._sort_combo.clear()
            for label, value in options:
                self._sort_combo.addItem(label, value)
            idx = self._sort_combo.findData(current)
            self._sort_combo.setCurrentIndex(idx if idx >= 0 else 0)
        finally:
            self._sort_combo.blockSignals(False)
            self._configuring_sort = False
        # Option labels differ per mode → the combo's width changed.
        self._reflow_action_bar()

    def _current_sort(self, mode: Optional[str] = None) -> str:
        target = mode or self._mode
        if target == self._mode and self._sort_combo.currentIndex() >= 0:
            value = self._sort_combo.currentData()
            if value:
                return str(value)
        return self._sort_by_mode.get(target, "")

    def _on_sort_changed(self) -> None:
        if self._configuring_sort or self._sort_combo.currentIndex() < 0:
            return
        self._sort_by_mode[self._mode] = str(self._sort_combo.currentData() or "")
        if self._mode == self._MODE_DOSSIERS:
            self._show_dossier_list()
        elif self._mode == self._MODE_FILES and self._current_dossier is not None:
            self._show_files_in_dossier(
                self._current_dossier, from_search=self._return_to_search,
            )
        elif self._mode == self._MODE_SEARCH:
            self._render_search_results(restore_scroll=False)

    def _update_view_navigation(self) -> None:
        # The Hồ sơ tab is dossier search + info only. The Tài liệu tab hosts
        # both the document-search results and a dossier's file list (the ☰
        # button / "Mở hồ sơ" jump here); the dedicated "← Kết quả tra cứu"
        # button returns from a file list to the preserved results.
        on_search_tab = self._mode in (self._MODE_SEARCH, self._MODE_FILES)
        for button, checked in (
            (self._btn_back_to_dossiers, not on_search_tab),
            (self._btn_back_to_search, on_search_tab),
        ):
            button.blockSignals(True)
            button.setChecked(checked)
            button.blockSignals(False)
        self._btn_back_to_dossiers.setVisible(True)
        self._btn_back_to_dossiers.setEnabled(self._mode != self._MODE_DOSSIERS)
        self._btn_back_to_dossiers.setText("Hồ sơ")
        self._btn_back_to_search.setVisible(True)
        self._btn_back_to_search.setEnabled(not on_search_tab)
        n_hits = len(self._search_hits)
        self._btn_back_to_search.setText(
            f"Tài liệu ({n_hits})" if n_hits else "Tài liệu"
        )
        has_search_state = bool(
            self._search_query or self._search_filters or self._search_hits
        )
        self._btn_back_to_results.setVisible(
            self._mode == self._MODE_FILES and has_search_state
        )
        self._btn_back_to_results.setText(
            f"← Kết quả tra cứu ({n_hits})" if n_hits else "← Kết quả tra cứu"
        )
        # Tab/result labels change width (e.g. the hit count suffix) —
        # re-decide whether the row still fits on one line.
        self._reflow_action_bar()

    def _set_splitter_for_mode(self) -> None:
        if not hasattr(self, "_splitter"):
            return
        narrow = self.width() < self._ACTION_BAR_BREAKPOINT
        # The dossier-list view has no PDF to show, so the middle column is
        # hidden and the long dossier titles get its full width. The PDF pane
        # returns for the dossier's file list and for document search.
        show_pdf = self._mode != self._MODE_DOSSIERS
        self._pdf_pane.setVisible(show_pdf)
        list_column = getattr(self, "_list_column", None)
        if list_column is not None:
            list_column.setMaximumWidth(
                560 if show_pdf else 16777215  # QWIDGETSIZE_MAX
            )
        if not show_pdf:
            self._splitter.setSizes([1400, 0, 340])
        elif narrow and self._mode == self._MODE_SEARCH:
            self._splitter.setSizes([360, 680, 260])
        elif narrow:
            self._splitter.setSizes([300, 760, 260])
        elif self._mode == self._MODE_SEARCH:
            self._splitter.setSizes([520, 680, 340])
        else:
            self._splitter.setSizes([360, 760, 340])

    def _build_list_column(self) -> QWidget:
        box = QFrame()
        box.setMinimumWidth(240)
        box.setMaximumWidth(560)
        box.setStyleSheet(
            f"background: {COLOR_BG}; border: none;"
            f" border-radius: {RADIUS_MD}px;"
        )
        v = QVBoxLayout(box)
        v.setContentsMargins(SP[1], SP[1], SP[1], SP[1])
        v.setSpacing(SP[1])

        # Scrollable card area
        self._list_scroll = _RepositoryListScrollArea()
        self._list_scroll.setWidgetResizable(True)
        self._list_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._list_scroll.setStyleSheet(
            f"QScrollArea {{ background: {COLOR_BG}; border: none; }}"
        )
        self._list_inner = QWidget()
        self._list_inner.setStyleSheet(f"background: {COLOR_BG};")
        self._list_layout = QVBoxLayout(self._list_inner)
        self._list_layout.setContentsMargins(SP[1], SP[1], SP[1], SP[1])
        self._list_layout.setSpacing(SP[2])
        self._list_layout.addStretch(1)
        self._list_scroll.setWidget(self._list_inner)

        # Search results use a VIRTUALIZED view (model + delegate): only
        # visible rows are painted, so 3.000+ hits scroll as smoothly as
        # 30. Browse modes keep the widget-based scroll area (page 0).
        self._list_stack = QStackedWidget()
        self._list_stack.addWidget(self._list_scroll)          # page 0
        self._search_model = _SearchResultsModel(self)
        self._search_delegate = _SearchHitDelegate(self._list_stack)
        self._search_delegate.hit_clicked.connect(self._on_virtual_hit_clicked)
        self._search_delegate.open_dossier.connect(self._open_search_hit_dossier)
        self._search_view = QListView()
        self._search_view.setModel(self._search_model)
        self._search_view.setItemDelegate(self._search_delegate)
        self._search_view.setMouseTracking(True)
        self._search_view.setSelectionMode(QListView.SelectionMode.NoSelection)
        self._search_view.setSpacing(SP[1])
        self._search_view.setVerticalScrollMode(
            QListView.ScrollMode.ScrollPerPixel
        )
        self._search_view.setStyleSheet(
            f"QListView {{ background: {COLOR_BG}; border: none; }}"
        )
        self._list_stack.addWidget(self._search_view)          # page 1
        v.addWidget(self._list_stack, 1)
        return box

    def _on_virtual_hit_clicked(self, doc_id: str) -> None:
        hit = self._hits_by_doc.get(doc_id)
        if hit is not None:
            self._show_search_hit(hit)

    def _build_toolbar(self) -> QWidget:
        bar = QFrame()
        bar.setStyleSheet(
            f"QFrame {{ background: {COLOR_SURFACE}; border: none;"
            f" border-radius: {RADIUS_MD}px; }}"
        )
        h = QHBoxLayout(bar)
        h.setContentsMargins(SP[2], SP[1], SP[2], SP[1])
        h.setSpacing(SP[2])

        self.search_input = QLineEdit()
        # Placeholder/tooltip are re-applied per active tra cứu tab in
        # _apply_scope_ui(); these are just the initial (hồ sơ) values.
        self.search_input.setPlaceholderText(
            "Tìm theo tên hồ sơ; mở ▾ để lọc thêm"
        )
        self.search_input.returnPressed.connect(self._on_search_clicked)
        h.addWidget(self.search_input, 1)

        self.btn_filter = QToolButton()
        self.btn_filter.setText("▾")
        self.btn_filter.setToolTip("Lọc hồ sơ (kết hợp AND)")
        self.btn_filter.setCheckable(True)
        self.btn_filter.setFixedSize(28, 26)
        self.btn_filter.toggled.connect(self._on_filter_toggle)
        self.btn_filter.setStyleSheet(
            f"QToolButton {{ background: transparent; color: {COLOR_TEXT};"
            f" border: 1px solid {COLOR_BORDER_DEFAULT}; border-radius: {RADIUS_SM}px;"
            f" padding: 0; font: 14px '{FONT_UI}'; }}"
            f"QToolButton:hover {{ background: {COLOR_ELEVATED};"
            f" border-color: {COLOR_ACCENT}; }}"
            f"QToolButton:checked {{ background: {COLOR_ELEVATED};"
            f" border-color: {COLOR_ACCENT}; color: {COLOR_ACCENT}; }}"
        )
        h.addWidget(self.btn_filter)

        self.btn_search = QPushButton("Tìm")
        self.btn_search.setProperty("cssClass", "primary")
        self.btn_search.setStyleSheet(BUTTON_PRIMARY_QSS)
        self.btn_search.clicked.connect(self._on_search_clicked)
        h.addWidget(self.btn_search)

        self.btn_clear_search = QPushButton("Xóa tìm kiếm")
        self.btn_clear_search.setVisible(False)
        self.btn_clear_search.setFixedHeight(26)
        self.btn_clear_search.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_clear_search.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {COLOR_TEXT_SECONDARY};"
            f" border: 1px solid {COLOR_BORDER}; border-radius: {RADIUS_SM}px;"
            f" padding: 0 12px; font: 600 12px '{FONT_UI}'; }}"
            f"QPushButton:hover {{ background: {COLOR_ELEVATED};"
            f" color: {COLOR_TEXT}; border-color: {COLOR_ACCENT}; }}"
        )
        self.btn_clear_search.clicked.connect(self._clear_search)
        h.addWidget(self.btn_clear_search)

        # Kept as a non-visual compatibility handle for code/tests that read
        # the former scope combo. The repository UI now has one unambiguous
        # keyword scope: OCR content.
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Nội dung OCR", "content")
        self.mode_combo.setVisible(False)

        return bar

    def _build_status_bar(self) -> QWidget:
        # Only path + statistics belong to the information area.
        # Header actions are siblings of that frame so their outlines are not
        # visually grouped with the read-only repository status.
        host = QWidget()
        host.setFixedHeight(30)
        host.setMinimumWidth(420)
        host.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        outer = QHBoxLayout(host)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(SP[2])

        info_bar = QFrame()
        info_bar.setObjectName("repositoryHeaderInfo")
        info_bar.setFixedHeight(30)
        info_bar.setMinimumWidth(220)
        info_bar.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        info_bar.setStyleSheet(
            f"QFrame#repositoryHeaderInfo {{ background: transparent; border: none;"
            f" border-radius: {RADIUS_MD}px; }}"
        )
        h = QHBoxLayout(info_bar)
        h.setContentsMargins(SP[2], 2, SP[2], 2)
        h.setSpacing(SP[2])

        self._path_label = QLabel("")
        self._path_label.setMinimumWidth(0)
        self._path_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self._path_label.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; background: transparent;"
            f" border: none; font: 11px '{FONT_UI}';"
        )
        h.addWidget(self._path_label, 1)

        self._stats_label = QLabel("")
        self._stats_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self._stats_label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._stats_label.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; background: transparent;"
            f" border: none; font: 11px '{FONT_UI}';"
        )
        h.addWidget(self._stats_label)
        outer.addWidget(info_bar, 1)

        # Repository-level actions use the otherwise empty header space while
        # remaining separate from the read-only path/statistics area.
        self.btn_import_zip = QPushButton("Nhập từ ZIP")
        self.btn_import_zip.setToolTip(
            "Nhập một hoặc nhiều file ZIP hồ sơ đã xuất vào Kho lưu trữ. "
            "ZIP kèm .json.zst: dùng dữ liệu OCR/KIE sẵn có (không chạy lại). "
            "ZIP cũ không kèm .json.zst: lấy nội dung từ lớp text PDF và "
            "metadata từ MetaDuLieu.xlsx. Có thể kéo-thả trực tiếp ZIP vào "
            "màn hình này."
        )
        self.btn_import_zip.setFixedHeight(24)
        self.btn_import_zip.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_import_zip.setStyleSheet(BUTTON_PRIMARY_QSS)
        self.btn_import_zip.clicked.connect(self._on_import_zip_clicked)
        outer.addWidget(self.btn_import_zip)

        self.btn_settings = QPushButton("⚙ Vị trí kho")
        self.btn_settings.setFixedHeight(24)
        self.btn_settings.clicked.connect(self._pick_archive_path)
        outer.addWidget(self.btn_settings)

        return host

    def _build_filter_panel(self) -> QWidget:
        """Filter criteria for the Tra cứu tài liệu tab. Phông / Mục lục are
        deliberately absent: they are hồ sơ-level conditions and belong to
        the dossier search panel instead."""
        panel = QFrame()
        panel.setStyleSheet(
            f"QFrame {{ background: {COLOR_SURFACE}; border: 1px solid {COLOR_BORDER};"
            f" border-radius: {RADIUS_MD}px; }}"
            f"QLabel {{ color: {COLOR_TEXT_SECONDARY}; font: 11px '{FONT_UI}'; }}"
        )
        v = QVBoxLayout(panel)
        v.setContentsMargins(SP[3], SP[1], SP[3], SP[1])
        v.setSpacing(SP[1])

        filter_hint = QLabel(
            "Các điều kiện dưới đây được kết hợp AND với nội dung cần tìm."
        )
        filter_hint.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; font: 11px '{FONT_UI}'; border: none;"
        )
        hint_row = QHBoxLayout()
        hint_row.setContentsMargins(0, 0, 0, 0)
        hint_row.addWidget(filter_hint, 1)
        btn_reset = QPushButton("Đặt lại")
        btn_reset.setFixedHeight(24)
        btn_reset.clicked.connect(self._reset_filters)
        hint_row.addWidget(btn_reset)
        v.addLayout(hint_row)

        grid = QGridLayout()
        grid.setHorizontalSpacing(SP[3])
        grid.setVerticalSpacing(SP[2])

        self._filter_inputs: dict[str, QWidget] = {}

        def _prepare_input(widget: QWidget) -> None:
            widget.setMinimumWidth(0)
            policy = widget.sizePolicy()
            policy.setHorizontalPolicy(QSizePolicy.Policy.Ignored)
            widget.setSizePolicy(policy)

        def _add(row: int, col: int, label: str, key: str, width: int = 1):
            grid.addWidget(QLabel(label), row, col * 2)
            le = QLineEdit()
            _prepare_input(le)
            grid.addWidget(le, row, col * 2 + 1, 1, width)
            self._filter_inputs[key] = le

        def _add_date(row: int, col: int, label: str, key: str):
            grid.addWidget(QLabel(label), row, col * 2)
            w = _DateFilterInput()
            _prepare_input(w)
            grid.addWidget(w, row, col * 2 + 1)
            self._filter_inputs[key] = w

        def _add_doc_type(row: int, col: int):
            grid.addWidget(QLabel("Loại VB"), row, col * 2)
            combo = FuzzyComboBox()
            try:
                from scanindex.core.digitization.doctype import all_display_names
                translations.add_localized_combo_items(
                    combo, all_display_names(), context="document_type"
                )
            except Exception:
                translations.add_localized_combo_items(
                    combo,
                    ["Nghị quyết", "Quyết định", "Báo cáo", "Công văn", "Khác"],
                    context="document_type",
                )
            combo.setCurrentIndex(-1)
            _prepare_input(combo)
            grid.addWidget(combo, row, col * 2 + 1)
            self._filter_inputs["doc_type"] = combo
            self._filter_doc_type_combo = combo

        def _add_confidentiality(row: int, col: int):
            grid.addWidget(QLabel("Độ mật"), row, col * 2)
            combo = FuzzyComboBox(sort=False)
            for option in _CONFIDENTIALITY_OPTIONS:
                combo.addItem(option, option)
            combo.setCurrentIndex(-1)
            combo.setToolTip("Chọn mức độ mật: Thường / Mật / Tối mật / Tuyệt mật")
            _prepare_input(combo)
            grid.addWidget(combo, row, col * 2 + 1)
            self._filter_inputs["confidentiality"] = combo

        _add(0, 0, "Số ký hiệu", "doc_number")
        _add(0, 1, "Cơ quan", "issue_org")
        _add(0, 2, "Người ký", "signer_name")
        _add_doc_type(0, 3)
        _add_date(1, 0, "Ngày từ", "issue_date_from")
        _add_date(1, 1, "Đến", "issue_date_to")
        _add(1, 2, "Trích yếu", "subject")
        _add(1, 3, "Nhiệm kỳ", "term")
        _add(2, 0, "Thời hạn", "retention")
        _add_confidentiality(2, 1)

        for col in range(4):
            grid.setColumnStretch(col * 2 + 1, 1)

        v.addLayout(grid)
        return panel

    def _build_dossier_filter_panel(self) -> QWidget:
        """Filter criteria for the Tra cứu hồ sơ tab, combined AND with the
        toolbar keyword (tên hồ sơ)."""
        panel = QFrame()
        panel.setStyleSheet(
            f"QFrame {{ background: {COLOR_SURFACE}; border: 1px solid {COLOR_BORDER};"
            f" border-radius: {RADIUS_MD}px; }}"
            f"QLabel {{ color: {COLOR_TEXT_SECONDARY}; font: 11px '{FONT_UI}'; }}"
        )
        v = QVBoxLayout(panel)
        v.setContentsMargins(SP[3], SP[1], SP[3], SP[1])
        v.setSpacing(SP[1])

        filter_hint = QLabel(
            "Các điều kiện dưới đây được kết hợp AND với từ khóa tên hồ sơ."
        )
        filter_hint.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; font: 11px '{FONT_UI}'; border: none;"
        )
        hint_row = QHBoxLayout()
        hint_row.setContentsMargins(0, 0, 0, 0)
        hint_row.addWidget(filter_hint, 1)
        btn_reset = QPushButton("Đặt lại")
        btn_reset.setFixedHeight(24)
        btn_reset.clicked.connect(self._reset_dossier_filters)
        hint_row.addWidget(btn_reset)
        v.addLayout(hint_row)

        grid = QGridLayout()
        grid.setHorizontalSpacing(SP[3])
        grid.setVerticalSpacing(SP[2])

        self._dossier_filter_inputs: dict[str, QWidget] = {}

        def _prepare_input(widget: QWidget) -> None:
            widget.setMinimumWidth(0)
            policy = widget.sizePolicy()
            policy.setHorizontalPolicy(QSizePolicy.Policy.Ignored)
            widget.setSizePolicy(policy)

        def _add(row: int, col: int, label: str, key: str):
            grid.addWidget(QLabel(label), row, col * 2)
            le = QLineEdit()
            _prepare_input(le)
            grid.addWidget(le, row, col * 2 + 1)
            self._dossier_filter_inputs[key] = le

        def _add_archive_combo(row: int, col: int, label: str, key: str):
            grid.addWidget(QLabel(label), row, col * 2)
            combo = FuzzyComboBox(sort=False)
            combo.addItem(f"Tất cả {label.lower()}", "")
            combo.setCurrentIndex(0)
            _prepare_input(combo)
            grid.addWidget(combo, row, col * 2 + 1)
            self._dossier_filter_inputs[key] = combo
            if key == "fonds":
                self._dossier_fonds_combo = combo
                combo.currentIndexChanged.connect(self._on_dossier_fonds_changed)
            else:
                self._dossier_catalog_combo = combo

        _add_archive_combo(0, 0, "Phông", "fonds")
        _add_archive_combo(0, 1, "Mục lục", "catalog")
        _add(0, 2, "Mã định danh", "ma_dinh_danh")
        _add(0, 3, "Số hồ sơ", "dossier_code")
        _add(1, 0, "Chuyên đề", "topic")
        _add(1, 1, "Nhiệm kỳ", "term")
        _add(1, 2, "Thời hạn", "retention")

        for col in range(4):
            grid.setColumnStretch(col * 2 + 1, 1)

        v.addLayout(grid)
        return panel

    # ------ Store/Index lifecycle ------

    def _open_store(self):
        try:
            # Migrate any legacy repository DB into this version's filename
            # BEFORE opening the store, so ArchiveStore opens the right file.
            from scanindex.infra.data_versioning import (
                migrate_db_if_needed, get_active_db_filename,
            )
            try:
                migrate_db_if_needed(self._archive_path)
            except Exception as e:
                self.log_message.emit(
                    f"DB migration skipped (non-fatal): {e}", "info"
                )
            self._store = ArchiveStore(
                self._archive_path, db_filename=get_active_db_filename()
            )
            self._store.connect()
            self._store.ensure_schema()
            mismatches = self._store.version_mismatches()
            if mismatches:
                details = ", ".join(
                    f"{k}: {old} -> {new}"
                    for k, (old, new) in sorted(mismatches.items())
                )
                self.log_message.emit(
                    f"Kho cần migration/rebuild chỉ mục nhưng dữ liệu không bị xóa: {details}",
                    "info",
                )
            # Derived-index generation check: when the on-disk Tantivy
            # predates this indexer version (schema upgrade / missing /
            # stale), rebuild it from SQLite in a sibling folder. SQLite is
            # the source of truth, so user data is never at risk.
            from scanindex.core.repository.reindex import index_needs_rebuild
            reason = index_needs_rebuild(self._store, self._archive_path)
            if reason:
                self._rebuild_index_interactive(reason)
                return
            self._finish_index_init()
        except Exception as e:
            QMessageBox.critical(self, "Kho lưu trữ",
                                 f"Không mở được kho:\n{e}")

    def _finish_index_init(self):
        """Open the derived index and finish Kho initialization. Called both
        directly and after an interactive index rebuild."""
        try:
            self._index = HybridIndex(self._archive_path)
            self._index.open()
            run_startup_repair(
                self._store, self._index,
                log_cb=lambda m: self.log_message.emit(m, "info"),
            )
            # Outbox replay: converge Tantivy onto SQLite for documents
            # whose write session crashed between the two commits.
            from scanindex.core.repository.reindex import (
                replay_pending_index_jobs,
            )
            replayed = replay_pending_index_jobs(self._store, self._index)
            if replayed.get("replayed") or replayed.get("deleted"):
                self.log_message.emit(
                    "Kho: đã đồng bộ lại chỉ mục cho "
                    f"{replayed.get('replayed', 0)} văn bản "
                    f"({replayed.get('deleted', 0)} đã xóa).", "info",
                )
            self._importer = Importer(self._store, self._index)
            self._engine = SearchEngine(self._store, self._index)
            self._refresh_status()
            self._populate_dossier_filter_combos()
        except Exception as e:
            QMessageBox.critical(self, "Kho lưu trữ",
                                 f"Không mở được kho:\n{e}")

    def _rebuild_index_interactive(self, reason: str):
        """Rebuild the Tantivy index with a cancellable progress dialog.

        The rebuild stages a complete index in a sibling folder and only
        swaps it in when finished, so a crash or cancel mid-way leaves the
        previous generation usable (an older-generation folder simply gets
        rebuilt again on the next open).
        """
        from PySide6.QtWidgets import QProgressDialog
        labels = {
            "indexer_version": "Nâng cấp chỉ mục tìm kiếm lên phiên bản mới…",
            "missing_dir": "Thiếu chỉ mục tìm kiếm — đang dựng lại từ dữ liệu…",
            "stale": "Chỉ mục tìm kiếm lệch với dữ liệu — đang dựng lại…",
        }
        progress = QProgressDialog(
            labels.get(reason, labels["stale"]), "Hủy", 0, 1, self
        )
        progress.setWindowTitle("Kho lưu trữ")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)

        worker = _IndexRebuildWorker(self._store, self._archive_path)
        # Keep references alive for the duration of the rebuild.
        self._index_rebuild_worker = worker
        self._index_rebuild_dialog = progress

        def _on_progress(done: int, total: int):
            progress.setMaximum(max(1, int(total)))
            progress.setValue(int(done))
            progress.setLabelText(f"Đang dựng chỉ mục: {done}/{total} đoạn văn")

        def _on_done(res):
            progress.close()
            self._index_rebuild_dialog = None
            res = res or {}
            if res.get("cancelled"):
                self.log_message.emit(
                    "Kho: đã hủy dựng chỉ mục — tìm kiếm có thể thiếu kết quả "
                    "đến lần dựng lại kế tiếp.", "warning",
                )
            else:
                self.log_message.emit(
                    "Kho: đã dựng lại chỉ mục tìm kiếm "
                    f"({res.get('chunks', 0)} đoạn, {res.get('elapsed', 0):.1f}s).",
                    "success",
                )
            self._finish_index_init()

        def _on_fail(msg):
            progress.close()
            self._index_rebuild_dialog = None
            self.log_message.emit(
                f"Kho: dựng lại chỉ mục thất bại — {msg}", "error",
            )
            # Still finish init: the screen stays usable with an empty/
            # previous index; the next open retries the rebuild.
            self._finish_index_init()

        worker.progress.connect(_on_progress)
        worker.finished_ok.connect(_on_done)
        worker.failed.connect(_on_fail)
        progress.canceled.connect(worker.cancel)
        worker.start()

    def _refresh_status(self):
        if self._store is None:
            self._path_label.setText("Chưa có kho")
            self._path_label.setToolTip("Chưa có kho")
            self._stats_label.setText("")
            self._stats_label.setToolTip("")
            return
        try:
            row = self._store.connect().execute(
                "SELECT "
                "(SELECT COUNT(*) FROM dossiers) AS n_dossiers,"
                "(SELECT COUNT(*) FROM documents WHERE indexed_status != 'deleted') AS n_docs,"
                "(SELECT COALESCE(SUM(page_count), 0) "
                "   FROM documents WHERE indexed_status != 'deleted') AS n_pages,"
                "(SELECT COUNT(*) FROM chunks WHERE indexed_status != 'deleted') AS n_chunks"
            ).fetchone()
            n_dossiers = int(row["n_dossiers"] or 0)
            n_docs = int(row["n_docs"] or 0)
            n_pages = int(row["n_pages"] or 0)
            n_chunks = int(row["n_chunks"] or 0)
        except Exception:
            n_dossiers = 0
            n_docs = int(self._store.get_meta("total_documents") or "0")
            n_pages = 0
            n_chunks = int(self._store.get_meta("total_chunks") or "0")
        self._path_label.setText(f"📂 {self._archive_path}")
        self._path_label.setToolTip(str(self._archive_path))
        stats = _format_repo_stats(
            n_dossiers, n_docs, n_pages, n_chunks, localized=True,
        )
        self._stats_label.setText(stats)
        self._stats_label.setToolTip(stats)

    def update_texts(self) -> None:
        """Refresh dynamic repository labels after an EN/VI switch."""
        self._refresh_status()
        for card_type in (
            _DossierCard, _FileCard, _SearchHitCard, _SnippetCard, _GroupHeader,
        ):
            for card in self.findChildren(card_type):
                card.update_texts()
        self._right_panel.update_texts()
        self._update_selection_toolbar()

    def _pick_archive_path(self):
        new_path = QFileDialog.getExistingDirectory(
            self, translations.localize_text("Chọn vị trí kho lưu trữ"),
            str(self._archive_path),
        )
        if not new_path:
            return
        self._archive_path = Path(new_path)
        try:
            _write_repository_path_setting(self._archive_path)
        except Exception as e:
            self.log_message.emit(f"Không lưu được settings.ini: {e}", "err")
        if self._store is not None:
            if self._index is not None:
                self._index.close()
            self._store.close()
        self._open_store()
        self._show_dossier_list()

    # ------ List rendering helpers ------

    def _clear_list(self):
        # Browse modes render widget cards into the scroll layout — make
        # sure the stack shows that page (search mode flips to the
        # virtualized view after its own _clear_list call).
        stack = getattr(self, "_list_stack", None)
        if stack is not None and stack.currentIndex() != 0:
            stack.setCurrentIndex(0)
        while self._list_layout.count() > 1:
            item = self._list_layout.takeAt(0)
            if item is None:
                continue
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _discard_search_list_cache(self) -> None:
        cached, self._search_list_cache = self._search_list_cache, []
        self._search_list_cache_sort = ""
        for widget in cached:
            widget.deleteLater()

    def _stash_search_list(self) -> None:
        """Detach the current search cards without destroying them."""
        self._discard_search_list_cache()
        cached: list[QWidget] = []
        while self._list_layout.count() > 1:
            item = self._list_layout.takeAt(0)
            if item is None:
                continue
            widget = item.widget()
            if widget is None:
                continue
            widget.hide()
            # Keep ownership under the screen while the widget is outside the
            # scroll layout; this also lets language refreshes still find it.
            widget.setParent(self)
            cached.append(widget)
        self._search_list_cache = cached
        self._search_list_cache_sort = self._current_sort(self._MODE_SEARCH)

    def _add_card(self, card: QWidget):
        self._list_layout.insertWidget(self._list_layout.count() - 1, card)

    def _set_active_doc_card(self, doc_id: str | None) -> None:
        self._active_doc_id = doc_id or ""
        for cid, card in self._doc_cards_by_id.items():
            if hasattr(card, "set_active"):
                card.set_active(cid == self._active_doc_id)
        # Virtualized search list repaints the touched rows.
        model = getattr(self, "_search_model", None)
        if model is not None:
            model.set_active(self._active_doc_id)

    # ------ Mode A: dossier list ------

    @staticmethod
    def _combo_label(code: str, name: str) -> str:
        code = (code or "").strip()
        name = " ".join((name or "").split())
        return f"{code} - {name}" if code and name else (code or name or "—")

    def _fetch_fonds_filter_options(self) -> list[tuple[str, str]]:
        if self._store is None:
            return []
        rows = self._store.connect().execute(
            "SELECT COALESCE(fonds, '') AS fonds, "
            "       COALESCE(MAX(NULLIF(fonds_name, '')), '') AS fonds_name "
            "FROM dossiers "
            "WHERE COALESCE(fonds, '') != '' "
            "  AND COALESCE(is_unstructured, 0) = 0 "
            "GROUP BY COALESCE(fonds, '') "
            "ORDER BY COALESCE(fonds, '') COLLATE NOCASE"
        ).fetchall()
        return [(r["fonds"] or "", r["fonds_name"] or "") for r in rows]

    def _fetch_catalog_filter_options(self, fonds: str = "") -> list[tuple[str, str]]:
        if self._store is None:
            return []
        where = "WHERE COALESCE(catalog, '') != '' "
        where += "  AND COALESCE(is_unstructured, 0) = 0 "
        params: list[str] = []
        if fonds:
            where += "AND COALESCE(fonds, '') = ? "
            params.append(fonds)
        rows = self._store.connect().execute(
            "SELECT COALESCE(catalog, '') AS catalog, "
            "       COALESCE(MAX(NULLIF(catalog_name, '')), '') AS catalog_name "
            "FROM dossiers "
            f"{where}"
            "GROUP BY COALESCE(catalog, '') "
            "ORDER BY COALESCE(catalog, '') COLLATE NOCASE",
            params,
        ).fetchall()
        return [(r["catalog"] or "", r["catalog_name"] or "") for r in rows]

    def _selected_dossier_fonds(self) -> str:
        combo = getattr(self, "_dossier_fonds_combo", None)
        if combo is None:
            return ""
        return str(combo.currentData() or "").strip()

    def _populate_dossier_catalog_combo(self, fonds: str = "",
                                        keep_catalog: str = "") -> None:
        combo = getattr(self, "_dossier_catalog_combo", None)
        if combo is None:
            return
        combo.blockSignals(True)
        try:
            combo.clear()
            combo.addItem("Tất cả mục lục", "")
            for catalog, catalog_name in self._fetch_catalog_filter_options(fonds):
                combo.addItem(self._combo_label(catalog, catalog_name), catalog)
            idx = combo.findData(keep_catalog)
            combo.setCurrentIndex(idx if idx >= 0 else 0)
            combo.setEnabled(combo.count() > 1)
        finally:
            combo.blockSignals(False)

    def _populate_dossier_filter_combos(self) -> None:
        """Refresh phông/mục lục choices of the dossier filter panel, keeping
        the current selections whenever they still exist."""
        fonds_combo = getattr(self, "_dossier_fonds_combo", None)
        if fonds_combo is None:
            return
        keep_fonds = self._selected_dossier_fonds()
        catalog_combo = getattr(self, "_dossier_catalog_combo", None)
        keep_catalog = (
            str(catalog_combo.currentData() or "").strip()
            if catalog_combo is not None else ""
        )
        fonds_combo.blockSignals(True)
        try:
            fonds_combo.clear()
            fonds_combo.addItem("Tất cả phông", "")
            for fonds, fonds_name in self._fetch_fonds_filter_options():
                fonds_combo.addItem(self._combo_label(fonds, fonds_name), fonds)
            idx = fonds_combo.findData(keep_fonds)
            fonds_combo.setCurrentIndex(idx if idx >= 0 else 0)
            fonds_combo.setEnabled(fonds_combo.count() > 1)
        finally:
            fonds_combo.blockSignals(False)
        self._populate_dossier_catalog_combo(keep_fonds, keep_catalog)

    def _on_dossier_fonds_changed(self) -> None:
        # Cascade the mục lục choices to the selected phông. Results refresh
        # only when the user presses Tìm — no auto re-query.
        self._populate_dossier_catalog_combo(self._selected_dossier_fonds(), "")

    def _collect_dossier_filters(self) -> dict:
        f: dict = {}
        for key, widget in self._dossier_filter_inputs.items():
            if isinstance(widget, QComboBox):
                v = str(widget.currentData() or "").strip()
            elif isinstance(widget, QLineEdit):
                v = widget.text().strip()
            else:
                continue
            if v:
                f[key] = v
        return f

    def _reset_dossier_filters(self) -> None:
        for key, widget in self._dossier_filter_inputs.items():
            if isinstance(widget, QComboBox):
                widget.setCurrentIndex(0)
            elif isinstance(widget, QLineEdit):
                widget.clear()
            elif hasattr(widget, "clear"):
                widget.clear()
        self._populate_dossier_catalog_combo("", "")

    def _show_dossier_list(self):
        previous_mode = self._mode
        self._sync_scope_state()
        if previous_mode == self._MODE_SEARCH:
            self._search_scroll_value = self._list_scroll.verticalScrollBar().value()
            self._stash_search_list()
        elif previous_mode == self._MODE_DOSSIERS:
            self._dossier_scroll_value = self._list_scroll.verticalScrollBar().value()
        self._mode = self._MODE_DOSSIERS
        self._current_dossier = None
        self._current_file = None
        self._return_to_search = False
        self._active_doc_id = ""
        self._doc_cards_by_id.clear()
        self._selected_doc_ids.clear()
        self._file_cards_by_id.clear()
        self._selected_dossier_ids.clear()
        self._dossier_cards_by_id.clear()
        self._btn_add_file.setVisible(False)
        self._btn_export_dossier_zip.setVisible(False)
        self._configure_sort_options(self._MODE_DOSSIERS)
        self._update_view_navigation()
        self._set_splitter_for_mode()
        self._update_selection_toolbar()
        self._apply_scope_ui()
        self._pdf_pane.clear()
        self._right_panel.show_dossier(DossierRow(
            dossier_id=0, title="(chưa chọn)",
            fonds="", catalog="", dossier_code="",
            doc_count=0, page_count=0, start_date="", end_date="",
        ))
        self._right_panel.show_message(
            "Chọn một hồ sơ ở cột trái để xem thông tin; nhấn ☰ (hoặc nhấp đúp) "
            "để mở danh sách tài liệu bên trong ở tab Tài liệu."
        )
        self._clear_list()
        if self._store is None:
            self._list_count_label.setText("Chưa mở được kho")
            return
        self._populate_dossier_filter_combos()
        filtered = bool(self._dossier_query or self._dossier_filters)
        dossiers = self._fetch_dossiers(
            query=self._dossier_query, filters=self._dossier_filters,
        )
        if not dossiers:
            if filtered:
                self._list_count_label.setText(
                    "Không có hồ sơ khớp điều kiện tra cứu."
                )
            else:
                self._list_count_label.setText(
                    "Kho rỗng. Dùng Bước 3 trong 'Số hóa lưu trữ' để chuyển hồ sơ vào."
                )
            return
        self._list_count_label.setText(
            f"{len(dossiers)} hồ sơ khớp điều kiện tra cứu" if filtered
            else f"{len(dossiers)} hồ sơ"
        )
        self._btn_export_dossier_zip.setVisible(True)
        for d in dossiers:
            card = _DossierCard(d)
            card.clicked.connect(lambda _did, dd=d: self._select_dossier_view(dd))
            card.open_clicked.connect(
                lambda _did, dd=d: self._show_files_in_dossier(dd)
            )
            card.edit_clicked.connect(lambda _did, dd=d: self._on_edit_dossier(dd))
            card.selection_changed.connect(self._on_dossier_selection_changed)
            self._dossier_cards_by_id[d.dossier_id] = card
            self._add_card(card)
        self._set_active_dossier_card(self._active_dossier_view_id)
        self._update_selection_toolbar()
        QTimer.singleShot(
            0,
            lambda value=self._dossier_scroll_value:
                self._list_scroll.verticalScrollBar().setValue(value),
        )

    def _select_dossier_view(self, dossier: DossierRow) -> None:
        """Dossier body click: only show its info in the right panel.
        Entering the document list is the separate explicit 'Xem tài liệu'
        action (card button or double-click)."""
        self._active_dossier_view_id = dossier.dossier_id
        self._set_active_dossier_card(dossier.dossier_id)
        self._right_panel.show_dossier(dossier)

    def _set_active_dossier_card(self, dossier_id: int) -> None:
        for did, card in self._dossier_cards_by_id.items():
            if hasattr(card, "set_active"):
                card.set_active(did == int(dossier_id))

    def _fetch_dossiers(self, query: str = "",
                        filters: Optional[dict] = None) -> List[DossierRow]:
        """Dossier rows for the Tra cứu hồ sơ tab. Phông/mục lục and the code
        columns are matched in SQL; tên hồ sơ / chuyên đề / nhiệm kỳ / thời hạn
        are matched in Python (diacritic-insensitive) via the module-level
        pure helpers so they stay unit-testable."""
        if self._store is None:
            return []
        where_parts, params = _dossier_sql_filters(filters or {})
        where_sql = (
            "WHERE " + " AND ".join(where_parts) + " "
            if where_parts else ""
        )
        sort_sql = {
            "stored_desc": "d.created_at DESC, COALESCE(d.title, '') COLLATE NOCASE",
            "title_asc": (
                "COALESCE(d.title, '') COLLATE NOCASE, "
                "COALESCE(d.dossier_code, '') COLLATE NOCASE"
            ),
        }.get(
            self._current_sort(self._MODE_DOSSIERS),
            "COALESCE(d.ma_dinh_danh, ''), COALESCE(d.fonds, ''), "
            "COALESCE(d.catalog, ''), COALESCE(d.dossier_code, ''), "
            "d.created_at DESC",
        )
        rows = self._store.connect().execute(
            "SELECT d.dossier_id, d.title, d.ma_dinh_danh, d.fonds, d.fonds_name,"
            "       d.catalog, d.catalog_name,"
            "       d.dossier_code, d.is_unstructured, d.retention, d.term,"
            "       d.storage_unit, d.physical_state, d.topic, d.note,"
            "       d.start_date, d.end_date, d.created_at,"
            "       COUNT(doc.doc_id) AS doc_count,"
            "       COALESCE(SUM(doc.page_count), 0) AS page_count "
            "FROM dossiers d "
            "LEFT JOIN documents doc ON doc.dossier_id = d.dossier_id "
            "    AND doc.indexed_status != 'deleted' "
            f"{where_sql}"
            "GROUP BY d.dossier_id "
            f"ORDER BY {sort_sql}",
            params,
        ).fetchall()
        rows_out = [
            DossierRow(
                dossier_id=int(r["dossier_id"]),
                title=r["title"] or "",
                ma_dinh_danh=r["ma_dinh_danh"] or "",
                fonds=r["fonds"] or "",
                fonds_name=r["fonds_name"] or "",
                catalog=r["catalog"] or "",
                catalog_name=r["catalog_name"] or "",
                dossier_code=r["dossier_code"] or "",
                doc_count=int(r["doc_count"] or 0),
                page_count=int(r["page_count"] or 0),
                start_date=r["start_date"] or "",
                end_date=r["end_date"] or "",
                is_unstructured=bool(r["is_unstructured"] or 0),
                retention=r["retention"] or "",
                term=r["term"] or "",
                storage_unit=r["storage_unit"] or "",
                physical_state=r["physical_state"] or "",
                topic=r["topic"] or "",
                note=r["note"] or "",
                stored_at=int(r["created_at"] or 0),
            )
            for r in rows
        ]
        filters = filters or {}
        topic = str(filters.get("topic") or "").strip()
        term = str(filters.get("term") or "").strip()
        retention = str(filters.get("retention") or "").strip()
        if (str(query or "").strip() or topic or term or retention):
            rows_out = [
                row for row in rows_out
                if _dossier_matches_title(row.title, query)
                and _dossier_matches_text(row.topic, topic)
                and _dossier_matches_text(row.term, term)
                and _dossier_matches_text(row.retention, retention)
            ]
        return rows_out

    # ------ Mode B: files inside a dossier ------

    def _show_files_in_dossier(self, dossier: DossierRow, *, from_search: bool = False):
        came_from_search = from_search or self._mode == self._MODE_SEARCH
        self._sync_scope_state()
        if self._mode == self._MODE_SEARCH:
            self._search_scroll_value = self._list_scroll.verticalScrollBar().value()
            self._stash_search_list()
        elif self._mode == self._MODE_DOSSIERS:
            self._dossier_scroll_value = self._list_scroll.verticalScrollBar().value()
        self._mode = self._MODE_FILES
        self._current_dossier = dossier
        self._current_file = None
        self._return_to_search = bool(came_from_search and self._search_hits)
        self._active_doc_id = ""
        self._doc_cards_by_id.clear()
        self._selected_doc_ids.clear()
        self._file_cards_by_id.clear()
        self._selected_dossier_ids.clear()
        self._dossier_cards_by_id.clear()
        self._btn_add_file.setVisible(True)        # only in file-list mode
        self._btn_export_dossier_zip.setVisible(True)
        self._configure_sort_options(self._MODE_FILES)
        self._update_view_navigation()
        self._set_splitter_for_mode()
        self._update_selection_toolbar()
        self._apply_scope_ui()
        self._pdf_pane.clear()
        self._right_panel.show_dossier(dossier)
        self._clear_list()
        files = self._fetch_files_for_dossier(dossier.dossier_id)
        self._list_count_label.setText(_dossier_status_html(
            dossier, doc_count=len(files), localized=True,
        ))
        for idx, f in enumerate(files, start=1):
            ordinal = int(f.so_thu_tu or idx)
            card = _FileCard(
                f,
                ordinal=ordinal,
                allow_reorder=self._current_sort(self._MODE_FILES) == "ordinal",
            )
            card.clicked.connect(lambda _did, ff=f: self._show_file(ff))
            card.selection_changed.connect(self._on_file_selection_changed)
            card.reorder_requested.connect(self._on_file_reorder_requested)
            self._file_cards_by_id[f.doc_id] = card
            self._doc_cards_by_id[f.doc_id] = card
            self._add_card(card)

    def _fetch_files_for_dossier(self, dossier_id: int) -> List[FileRow]:
        if self._store is None:
            return []
        rows = self._store.connect().execute(
            "SELECT d.doc_id, d.dossier_id, d.file_name, d.file_path,"
            "       d.kie_doc_subject, d.kie_doc_number_symbol,"
            "       d.kie_issue_org_name, d.kie_issue_org_superior,"
            "       d.kie_signer_name, d.kie_place_date,"
            "       d.kie_doc_type, d.kie_secrecy_mark, d.page_count,"
            "       d.trang_so, d.so_thu_tu,"
            "       ds.title AS dossier_title, ds.fonds, ds.catalog,"
            "       ds.dossier_code "
            "FROM documents d "
            "LEFT JOIN dossiers ds ON ds.dossier_id = d.dossier_id "
            "WHERE d.dossier_id = ? AND d.indexed_status != 'deleted' "
            "ORDER BY CASE WHEN d.so_thu_tu IS NULL OR d.so_thu_tu <= 0 "
            "              THEN 1 ELSE 0 END, d.so_thu_tu, d.file_name",
            (dossier_id,),
        ).fetchall()
        files = [self._row_to_file(r) for r in rows]
        sort_mode = self._current_sort(self._MODE_FILES)
        if sort_mode == "date_desc":
            files.sort(
                key=lambda f: (
                    _issue_date_sort_key(f.issue_date),
                    _single_line(f.subject or f.file_name).casefold(),
                ),
                reverse=True,
            )
        elif sort_mode == "title_asc":
            files.sort(
                key=lambda f: (
                    _single_line(f.subject or f.file_name).casefold(),
                    int(f.so_thu_tu or 0),
                )
            )
        return files

    @staticmethod
    def _row_to_file(r) -> FileRow:
        return FileRow(
            doc_id=r["doc_id"],
            dossier_id=r["dossier_id"],
            file_name=r["file_name"] or "",
            file_path=r["file_path"] or "",
            subject=r["kie_doc_subject"] or "",
            doc_number=r["kie_doc_number_symbol"] or "",
            issue_org=r["kie_issue_org_name"] or "",
            issue_org_superior=r["kie_issue_org_superior"] or "",
            signer_name=r["kie_signer_name"] or "",
            issue_date=r["kie_place_date"] or "",
            doc_type=r["kie_doc_type"] or "",
            secrecy_mark=r["kie_secrecy_mark"] or "",
            page_count=int(r["page_count"] or 0),
            dossier_title=r["dossier_title"] or "",
            trang_so=r["trang_so"] if r["trang_so"] is not None else None,
            so_thu_tu=r["so_thu_tu"] if r["so_thu_tu"] is not None else None,
            fonds=r["fonds"] or "",
            catalog=r["catalog"] or "",
            dossier_code=r["dossier_code"] or "",
        )

    def _fetch_dossier_by_id(self, dossier_id: int) -> Optional[DossierRow]:
        if self._store is None:
            return None
        r = self._store.connect().execute(
            "SELECT d.dossier_id, d.title, d.ma_dinh_danh, d.fonds, d.fonds_name,"
            "       d.catalog, d.catalog_name, d.dossier_code, d.is_unstructured,"
            "       d.retention, d.term, d.storage_unit, d.physical_state,"
            "       d.topic, d.note, d.start_date, d.end_date, d.created_at,"
            "       COUNT(doc.doc_id) AS doc_count,"
            "       COALESCE(SUM(doc.page_count), 0) AS page_count "
            "FROM dossiers d "
            "LEFT JOIN documents doc ON doc.dossier_id = d.dossier_id "
            "    AND doc.indexed_status != 'deleted' "
            "WHERE d.dossier_id = ? "
            "GROUP BY d.dossier_id",
            (dossier_id,),
        ).fetchone()
        if r is None:
            return None
        return DossierRow(
            dossier_id=int(r["dossier_id"]),
            title=r["title"] or "",
            ma_dinh_danh=r["ma_dinh_danh"] or "",
            fonds=r["fonds"] or "",
            fonds_name=r["fonds_name"] or "",
            catalog=r["catalog"] or "",
            catalog_name=r["catalog_name"] or "",
            dossier_code=r["dossier_code"] or "",
            doc_count=int(r["doc_count"] or 0),
            page_count=int(r["page_count"] or 0),
            start_date=r["start_date"] or "",
            end_date=r["end_date"] or "",
            is_unstructured=bool(r["is_unstructured"] or 0),
            retention=r["retention"] or "",
            term=r["term"] or "",
            storage_unit=r["storage_unit"] or "",
            physical_state=r["physical_state"] or "",
            topic=r["topic"] or "",
            note=r["note"] or "",
            stored_at=int(r["created_at"] or 0),
        )

    @staticmethod
    def _identity_from_dossier(dossier: DossierRow) -> IdentityCodes:
        return IdentityCodes(
            ma_dinh_danh=dossier.ma_dinh_danh,
            ma_phong=dossier.fonds,
            muc_luc=dossier.catalog,
            ho_so=dossier.dossier_code,
            ten_phong=dossier.fonds_name,
            ten_muc_luc=dossier.catalog_name,
            title=dossier.title,
            is_unstructured=dossier.is_unstructured,
            thoi_han_bao_quan=dossier.retention,
            tinh_trang_vat_ly=dossier.physical_state,
            nhiem_ky=dossier.term,
            chuyen_de=dossier.topic,
            chu_thich=dossier.note,
        )

    @staticmethod
    def _safe_archive_name(name: str, fallback: str) -> str:
        text = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(name or ""))
        text = text.strip(" .")
        return text or fallback

    @classmethod
    def _export_pdf_name_for_dossier(cls, identity: IdentityCodes, stt: int,
                                     fallback_file_name: str = "") -> str:
        name = ""
        try:
            if identity and identity.is_complete():
                name = identity.make_segment_name(stt)
        except Exception:
            name = ""
        if not name:
            stem = os.path.splitext(os.path.basename(fallback_file_name or ""))[0]
            stem = re.sub(r"_ocr$", "", stem, flags=re.IGNORECASE)
            name = f"{stem or f'van-ban-{stt:03d}'}.pdf"
        if not name.lower().endswith(".pdf"):
            name += ".pdf"
        return cls._safe_archive_name(name, f"van-ban-{stt:03d}.pdf")

    @classmethod
    def _export_zip_name_for_dossier(cls, identity: IdentityCodes) -> str:
        parts = [
            identity.ma_dinh_danh,
            identity.ma_phong,
            identity.muc_luc,
            identity.ho_so,
        ]
        parts = [str(p or "").strip() for p in parts]
        if not all(parts):
            return "HSLTCQ.zip"
        return cls._safe_archive_name("-".join(parts), "HSLTCQ") + ".zip"

    @staticmethod
    def _unique_output_path(folder: str, file_name: str) -> str:
        path = os.path.join(folder, file_name)
        if not os.path.exists(path):
            return path
        stem, ext = os.path.splitext(file_name)
        for i in range(2, 10000):
            candidate = os.path.join(folder, f"{stem}_{i}{ext}")
            if not os.path.exists(candidate):
                return candidate
        raise RuntimeError(f"Không tạo được tên file xuất không trùng: {file_name}")

    @staticmethod
    def _annotation_from_repository_doc(row) -> dict:
        fields = []
        for idx, label in enumerate(KIE_LABELS):
            col = f"kie_{label.lower()}"
            text = str(row[col] or "").strip() if col in row.keys() else ""
            if not text:
                continue
            fields.append({
                "id": f"repo-{idx}",
                "label": label,
                "text": text,
                "page": 0,
                "bbox": [],
                "score": 1.0,
            })
        return {
            "schema": "kie_vi_official_v3",
            "source": "repository_sql",
            "status": "stored",
            "field_instances": fields,
            "relations": [],
        }

    def _build_dossier_zip_docs(self, dossier: DossierRow,
                                identity: IdentityCodes) -> tuple[list[dict], int]:
        if self._store is None:
            return [], 0
        kie_cols = ", ".join(f"d.kie_{label.lower()}" for label in KIE_LABELS)
        rows = self._store.connect().execute(
            "SELECT d.doc_id, d.file_name, d.file_path, d.page_count, "
            "       d.trang_so, d.so_thu_tu, "
            f"{kie_cols} "
            "FROM documents d "
            "WHERE d.dossier_id = ? AND d.indexed_status != 'deleted' "
            "ORDER BY d.file_name, d.created_at, d.doc_id",
            (dossier.dossier_id,),
        ).fetchall()
        docs: list[dict] = []
        skipped = 0
        # `file_name` is the persisted dossier order key. Reorder renames the
        # stored PDFs first, so both ZIP members and Excel rows below follow
        # the same new sequence.
        for r in rows:
            pdf_path = (self._archive_path / (r["file_path"] or "")).resolve()
            if not pdf_path.is_file():
                skipped += 1
                continue
            # Name the ZIP member by the doc's so_thu_tu so it matches the
            # workbook's "Số thứ tự" cell and the on-screen order. Fall back
            # to the 1-based position when the column is blank (legacy docs).
            stored_stt = r["so_thu_tu"]
            try:
                stt = int(stored_stt) if stored_stt else 0
            except (ValueError, TypeError):
                stt = 0
            if stt <= 0:
                stt = len(docs) + 1
            export_name = self._export_pdf_name_for_dossier(
                identity, stt, r["file_name"] or ""
            )
            docs.append({
                "pdf_path": str(pdf_path),
                "export_source_path": str(pdf_path),
                "export_file_name": export_name,
                "annotation": self._annotation_from_repository_doc(r),
                # Carry the stored HSLTCQ sequencing fields through to the
                # Excel writer. Empty string (not None) so
                # write_aggregated_excel's form-override path treats them as
                # "operator left blank" and applies its positional fallback.
                "metadata": {
                    "trang_so": str(r["trang_so"]) if r["trang_so"] is not None else "",
                    "so_thu_tu": str(r["so_thu_tu"]) if r["so_thu_tu"] is not None else "",
                },
            })
        # Backfill trang_so when every doc is missing it (legacy DB pre-v9
        # or a folder import that never carried the column). Cumulative
        # start-page numbering derived from each doc's page_count, matching
        # Step 2's `_ensure_trang_so_initialised` / zip_roundtrip backfill.
        if docs and not any(d["metadata"].get("trang_so") for d in docs):
            try:
                from scanindex.core.digitization.metadata_export import (
                    compute_trang_so,
                )
                page_counts = []
                for d in docs:
                    p = d.get("export_source_path") or d.get("pdf_path") or ""
                    n = 0
                    if p:
                        try:
                            import fitz
                            with fitz.open(str(p)) as f:
                                n = int(f.page_count)
                        except Exception:
                            n = 0
                    page_counts.append(max(1, n))
                trang = compute_trang_so(page_counts, first_default=1)
                for d, t in zip(docs, trang):
                    d["metadata"]["trang_so"] = str(t)
            except Exception:
                pass
        # Order ZIP members + Excel rows by so_thu_tu so the file sequence
        # in the archive matches the workbook's "Số thứ tự" column (and the
        # on-screen order in Kho lưu trữ). Docs without a value keep their
        # relative file-name order via a stable sort.
        def _order_key(d):
            try:
                n = int(d["metadata"].get("so_thu_tu") or 0)
                return (0, n) if n > 0 else (1, 0)
            except (ValueError, TypeError):
                return (1, 0)
        docs.sort(key=_order_key)
        return docs, skipped

    def _export_one_dossier_zip(self, dossier: DossierRow,
                                out_dir: str) -> tuple[str, int, int]:
        import tempfile
        import zipfile
        from scanindex.core.canonical_io import companion_for_pdf
        from scanindex.core.digitization.runner import write_aggregated_excel

        identity = self._identity_from_dossier(dossier)
        export_docs, skipped = self._build_dossier_zip_docs(dossier, identity)
        if not export_docs:
            raise ValueError(
                f"Hồ sơ {self._dossier_code_text(dossier)} không có PDF hợp lệ để xuất"
            )

        # When the "kèm .json.zst" setting is on (default), each PDF travels
        # with its canonical sidecar (OCR + KIE) under `<pdf>.json.zst` so
        # the ZIP can be re-imported into Kho lưu trữ without re-running
        # OCR/KIE. Companions live next to the stored PDFs in the repo (the
        # importer copies them there); PMKhoSohoa ignores the extra files.
        include_canonical = _read_zip_include_canonical_setting()

        tmp_xlsx = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
        tmp_xlsx.close()
        excel_tmp_path = tmp_xlsx.name
        try:
            write_aggregated_excel(export_docs, excel_tmp_path, identity=identity)
            zip_name = self._export_zip_name_for_dossier(identity)
            zip_path = self._unique_output_path(out_dir, zip_name)
            copied = 0
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.write(excel_tmp_path, "HSLTCQ/METADATA/MetaDuLieu.xlsx")
                for doc in export_docs:
                    zf.write(
                        doc["export_source_path"],
                        f"HSLTCQ/METADATA/{doc['export_file_name']}",
                    )
                    copied += 1
                    if include_canonical:
                        companion = companion_for_pdf(doc["export_source_path"])
                        if companion.is_file():
                            zf.write(
                                str(companion),
                                f"HSLTCQ/METADATA/{doc['export_file_name']}.json.zst",
                            )
            return zip_path, copied, skipped
        finally:
            try:
                os.unlink(excel_tmp_path)
            except Exception:
                pass

    @staticmethod
    def _dossier_code_text(dossier: DossierRow) -> str:
        return (
            f"{dossier.ma_dinh_danh or '—'}-{dossier.fonds or '—'}-"
            f"{dossier.catalog or '—'}-{dossier.dossier_code or '—'}"
        )

    def _fetch_file_by_doc_id(self, doc_id: str) -> Optional[FileRow]:
        if self._store is None:
            return None
        r = self._store.connect().execute(
            "SELECT d.doc_id, d.dossier_id, d.file_name, d.file_path,"
            "       d.kie_doc_subject, d.kie_doc_number_symbol,"
            "       d.kie_issue_org_name, d.kie_issue_org_superior,"
            "       d.kie_signer_name, d.kie_place_date,"
            "       d.kie_doc_type, d.kie_secrecy_mark, d.page_count,"
            "       d.trang_so, d.so_thu_tu,"
            "       ds.title AS dossier_title, ds.fonds, ds.catalog,"
            "       ds.dossier_code "
            "FROM documents d "
            "LEFT JOIN dossiers ds ON ds.dossier_id = d.dossier_id "
            "WHERE d.doc_id = ?",
            (doc_id,),
        ).fetchone()
        return self._row_to_file(r) if r else None

    # ------ File preview (used by both file-browse and search-hit click) ------

    def _show_file(self, file: FileRow,
                   chunk_hits: Optional[List[SearchResult]] = None):
        """Pivot the right panel + PDF to this file. `chunk_hits` populates
        the snippet list (search mode); empty list = browse mode."""
        self._current_file = file
        self._set_active_doc_card(file.doc_id)
        self._right_panel.show_file(file, self._archive_path, chunk_hits)
        if file.file_path:
            pdf_abs = (self._archive_path / file.file_path).resolve()
            if chunk_hits:
                head = self._first_jumpable_result(chunk_hits)
                is_meta = (getattr(head, "chunk_type", "body") == "metadata")
                match_boxes = getattr(head, "match_bboxes", None) or None
                all_match_boxes = self._match_page_boxes(chunk_hits)
                is_text_match = self._is_text_search_result(head)
                bbox = (
                    None
                    if (is_meta or match_boxes or is_text_match)
                    else (head.bbox or None)
                )
                if bbox and len(bbox) == 4 and all(v == 0 for v in bbox):
                    bbox = None
                self._pdf_pane.show_pdf(pdf_abs, page=head.page or 1,
                                         bbox=bbox,
                                         bboxes=match_boxes,
                                         highlight_style="highlight"
                                         if (all_match_boxes or match_boxes)
                                         else "box",
                                         page_bboxes=all_match_boxes)
                self._right_panel.set_active_chunk(head.chunk_id or 0)
            else:
                self._pdf_pane.show_pdf(pdf_abs, page=1, bbox=None)
        else:
            self._pdf_pane.clear()

    @staticmethod
    def _is_text_search_result(result: SearchResult) -> bool:
        if (getattr(result, "chunk_type", "body") or "body") == "metadata":
            return False
        return (
            (getattr(result, "match_kind", "") or "") in {"exact", "fuzzy"}
            and bool(str(getattr(result, "query", "") or "").strip())
        )

    @staticmethod
    def _match_page_boxes(chunks: List[SearchResult]) -> list[tuple[int, list[float]]]:
        out: list[tuple[int, list[float]]] = []
        seen_by_page: dict[int, list[list[float]]] = {}
        for chunk in chunks or []:
            if (getattr(chunk, "chunk_type", "body") or "body") == "metadata":
                continue
            page = int(getattr(chunk, "page", 0) or 0)
            if page <= 0:
                continue
            seen = seen_by_page.setdefault(page, [])
            for bb in getattr(chunk, "match_bboxes", None) or []:
                if not bb or len(bb) != 4:
                    continue
                clean = [float(v) for v in bb]
                if any(_is_same_match_bbox(clean, old) for old in seen):
                    continue
                seen.append(clean)
                out.append((page - 1, clean))
        return out

    @staticmethod
    def _first_jumpable_result(chunks: List[SearchResult]) -> SearchResult:
        """Pick the first search chunk that can move the PDF to a real hit.

        Metadata hits can rank first because they are strong signals, but
        they do not always carry page-level boxes. When the user clicks a file
        result, prefer the first body hit with a page and bbox so the PDF jumps
        directly to the visible match instead of making the user click a
        snippet on the right.
        """
        if not chunks:
            raise ValueError("chunks is empty")
        for chunk in chunks:
            if (getattr(chunk, "chunk_type", "body") or "body") == "metadata":
                continue
            has_boxes = bool(getattr(chunk, "match_bboxes", None))
            has_bbox = bool(getattr(chunk, "bbox", None))
            if int(getattr(chunk, "page", 0) or 0) > 0 and (has_boxes or has_bbox):
                return chunk
        for chunk in chunks:
            if int(getattr(chunk, "page", 0) or 0) > 0:
                return chunk
        return chunks[0]

    # ------ Search flow ------

    def _collect_filters(self) -> dict:
        f: dict = {}
        for key, widget in self._filter_inputs.items():
            if isinstance(widget, QComboBox):
                v = translations.combo_value(widget).strip()
            elif isinstance(widget, QLineEdit):
                v = widget.text().strip()
            elif hasattr(widget, "text"):
                v = str(widget.text()).strip()
            else:
                continue
            if v:
                f[key] = v
        return f

    def _reset_filters(self):
        for key, widget in self._filter_inputs.items():
            if isinstance(widget, QComboBox):
                widget.setCurrentIndex(-1)
                if widget.isEditable() and widget.lineEdit() is not None:
                    widget.lineEdit().clear()
            elif isinstance(widget, QLineEdit):
                widget.clear()
            elif hasattr(widget, "clear"):
                widget.clear()

    def _refresh_doc_type_filter_choices(self):
        combo = getattr(self, "_filter_doc_type_combo", None)
        if not isinstance(combo, QComboBox):
            return
        current = translations.combo_value(combo).strip()
        try:
            from scanindex.core.digitization.doctype import all_display_names
            values = all_display_names()
        except Exception:
            return
        combo.blockSignals(True)
        try:
            combo.clear()
            translations.add_localized_combo_items(
                combo, values, context="document_type"
            )
            if current:
                translations.set_combo_value(combo, current)
            else:
                combo.setCurrentIndex(-1)
        finally:
            combo.blockSignals(False)

    def _on_filter_toggle(self, checked: bool):
        if not hasattr(self, "_filter_panel"):
            return
        scope = self._active_scope()
        # Exactly one criteria panel is visible at a time: the one belonging
        # to the active tra cứu tab.
        self._filter_panel.setVisible(checked and scope == "search")
        self._dossier_filter_panel.setVisible(checked and scope == "dossiers")
        if hasattr(self, "btn_filter"):
            self.btn_filter.setText("▴" if checked else "▾")
        self._scope_states[scope]["panel_open"] = bool(checked)

    # ------ Per-tab toolbar scope (tra cứu hồ sơ vs tra cứu tài liệu) ------

    def _active_scope(self) -> str:
        """'dossiers' while on the Hồ sơ tab (search + info), 'search' while
        on the Tài liệu tab — which hosts BOTH the document-search results
        and a dossier's file list (mode FILES lives under this scope)."""
        return (
            "dossiers"
            if self._mode == self._MODE_DOSSIERS
            else "search"
        )

    def _sync_scope_state(self) -> None:
        """Persist the shared toolbar widgets into the outgoing scope's
        memory before the active tab changes."""
        if not hasattr(self, "search_input"):
            return
        state = self._scope_states[self._active_scope()]
        state["query"] = self.search_input.text()
        state["panel_open"] = bool(
            getattr(self, "btn_filter", None) and self.btn_filter.isChecked()
        )

    def _apply_scope_ui(self, *, restore_query: bool = True) -> None:
        """Restore the incoming scope's keyword/panel state onto the shared
        toolbar after the active tab changed. `restore_query=False` keeps the
        current input text — used when the tab did NOT change (e.g. search
        finished while the user was already on the tab and may have edited
        the keyword since)."""
        scope = self._active_scope()
        state = self._scope_states[scope]
        panel_open = bool(state.get("panel_open"))
        if restore_query:
            self.search_input.setText(str(state.get("query") or ""))
        self.btn_filter.blockSignals(True)
        self.btn_filter.setChecked(panel_open)
        self.btn_filter.blockSignals(False)
        self.btn_filter.setText("▴" if panel_open else "▾")
        self._filter_panel.setVisible(panel_open and scope == "search")
        self._dossier_filter_panel.setVisible(panel_open and scope == "dossiers")
        self.search_input.setEnabled(True)
        if scope == "dossiers":
            self.search_input.setPlaceholderText(
                "Tìm theo tên hồ sơ; mở ▾ để lọc thêm"
            )
            self.btn_filter.setToolTip("Lọc hồ sơ (kết hợp AND)")
        else:
            self.search_input.setPlaceholderText(
                "Tìm trong nội dung OCR; mở ▾ để lọc thêm thông tin văn bản"
            )
            self.btn_filter.setToolTip("Lọc thông tin văn bản (kết hợp AND)")
        self._update_clear_button()

    def _update_clear_button(self) -> None:
        """The shared 'Xóa tìm kiếm' button reflects the ACTIVE tab's state."""
        if not hasattr(self, "btn_clear_search"):
            return
        if self._active_scope() == "dossiers":
            visible = bool(
                self._dossier_query
                or self._dossier_filters
                or self.search_input.text().strip()
                or self._collect_dossier_filters()
            )
        else:
            visible = bool(
                self._search_query
                or self._search_filters
                or self.search_input.text().strip()
                or self._collect_filters()
            )
        self.btn_clear_search.setVisible(visible)

    def _on_search_mode_changed(self):
        if not hasattr(self, "_filter_panel"):
            return
        self._refresh_doc_type_filter_choices()
        self._apply_scope_ui()

    def _clear_search(self):
        """Clear the ACTIVE tab's keyword + filters + results. Each tra cứu
        tab owns its own search state."""
        if self._active_scope() == "dossiers":
            self._clear_dossier_search()
        else:
            self._clear_document_search()

    def _clear_dossier_search(self) -> None:
        self._dossier_query = ""
        self._dossier_filters = {}
        self._scope_states["dossiers"]["query"] = ""
        self._reset_dossier_filters()
        self._dossier_scroll_value = 0
        if self._active_scope() == "dossiers":
            self.search_input.clear()
        self._show_dossier_list()

    def _clear_document_search(self) -> None:
        self._scope_states["search"]["query"] = ""
        self.search_input.clear()
        self._reset_filters()
        self._reset_document_search_state()

    def _reset_document_search_state(self) -> None:
        self._search_query = ""
        self._search_filters = {}
        self._search_mode = "content"
        self._search_hits = []
        self._hits_by_doc = {}
        self._search_scroll_value = 0
        self._search_selected_doc_id = ""
        self._search_rank_by_doc = {}
        self._discard_search_list_cache()
        self._render_search_empty_state()

    def _run_dossier_search(self) -> None:
        """Tìm của tab Tra cứu hồ sơ: commit keyword + filters, then re-render
        the dossier list (empty both = plain browse-all, as before)."""
        query = self.search_input.text().strip()
        filters = self._collect_dossier_filters()
        self._dossier_query = query
        self._dossier_filters = dict(filters)
        if query or filters:
            self._dossier_scroll_value = 0
        self._show_dossier_list()

    def _on_search_clicked(self):
        if self._busy:
            # Second click while a search runs = cancel. The engine returns
            # the partial results collected by the fast index passes.
            worker = getattr(self, "_search_worker", None)
            if (worker is not None and worker.isRunning()
                    and not self._search_cancel_requested):
                self._search_cancel_requested = True
                worker.cancel()
                self._list_count_label.setText("Đang hủy tìm kiếm…")
            return
        if self._active_scope() == "dossiers":
            self._run_dossier_search()
            return
        if self._engine is None:
            return
        mode = "content"
        query = self.search_input.text().strip()
        filters = self._collect_filters()
        if not query and not filters:
            # No criteria → reset to the tab's waiting state instead of
            # jumping back to the dossier browse.
            self._reset_document_search_state()
            return
        self._busy = True
        self._search_cancel_requested = False
        # Button stays enabled so the same click cancels a long search
        # (fallback scans over a huge archive).
        self.btn_search.setText("Hủy")
        self.btn_clear_search.setEnabled(False)
        self._list_count_label.setText("Đang tìm…")

        if query and filters:
            self._list_count_label.setText(
                "Đang tìm nội dung và lọc thông tin văn bản..."
            )
        elif query:
            self._list_count_label.setText("Đang tìm trong nội dung OCR...")
        else:
            self._list_count_label.setText("Đang lọc thông tin văn bản...")

        self._pending_search_query = query
        self._pending_search_filters = dict(filters)
        self._pending_search_mode = mode
        self._search_worker = SearchWorker(self._engine, query, filters, mode)
        self._search_worker.finished_ok.connect(self._on_search_done)
        self._search_worker.failed.connect(self._on_search_failed)
        self._search_worker.start()

    def _on_search_done(self, results: List[SearchResult],
                        fuzzy_truncated: bool = False):
        cancelled = self._search_cancel_requested
        self._busy = False
        self._search_cancel_requested = False
        self.btn_search.setText("Tìm")
        self.btn_search.setEnabled(True)
        self.btn_clear_search.setEnabled(True)
        self._search_query = getattr(
            self, "_pending_search_query", self.search_input.text().strip()
        )
        self._search_filters = dict(getattr(
            self, "_pending_search_filters", self._collect_filters()
        ))
        self._search_mode = str(getattr(
            self, "_pending_search_mode", "content"
        ))
        self._discard_search_list_cache()
        self._search_hits = _group_results_by_file(results)
        self._hits_by_doc = {h.file_row.doc_id: h for h in self._search_hits}
        self._search_selected_doc_id = ""
        self._search_scroll_value = 0
        self._render_search_results(restore_scroll=False)
        if cancelled:
            self._list_count_label.setText(
                "Đã hủy — " + self._list_count_label.text().lower()
            )
        elif fuzzy_truncated:
            self._list_count_label.setText(
                self._list_count_label.text()
                + " · kết quả gần đúng có thể thiếu (hết thời gian quét sâu)"
            )

    def _sorted_search_hits(self) -> List[FileHit]:
        hits = list(self._search_hits)
        sort_mode = self._current_sort(self._MODE_SEARCH)
        if sort_mode == "relevance":
            return hits
        if sort_mode == "date_desc":
            hits.sort(
                key=lambda h: (
                    _issue_date_sort_key(h.file_row.issue_date),
                    h.match_total,
                    h.score_total,
                ),
                reverse=True,
            )
        elif sort_mode == "archive":
            hits.sort(key=lambda h: (
                _single_line(h.file_row.fonds).casefold(),
                _single_line(h.file_row.catalog).casefold(),
                _single_line(h.file_row.dossier_code).casefold(),
                _single_line(h.file_row.subject or h.file_row.file_name).casefold(),
            ))
        elif sort_mode == "title_asc":
            hits.sort(key=lambda h: (
                _single_line(h.file_row.subject or h.file_row.file_name).casefold(),
                _single_line(h.file_row.file_name).casefold(),
            ))
        return hits

    def _render_search_results(self, *, restore_scroll: bool = True) -> None:
        was_search = self._mode == self._MODE_SEARCH
        if not was_search:
            self._sync_scope_state()
        if was_search:
            self._search_scroll_value = self._search_scrollbar().value()
        self._mode = self._MODE_SEARCH
        self._current_dossier = None
        self._current_file = None
        self._return_to_search = False
        self._btn_add_file.setVisible(False)
        self._btn_export_dossier_zip.setVisible(False)
        self._configure_sort_options(self._MODE_SEARCH)
        self._update_view_navigation()
        self._set_splitter_for_mode()
        # Same-tab re-render (search finished on the tab): do not clobber a
        # keyword the user may have edited meanwhile.
        self._apply_scope_ui(restore_query=not was_search)
        self._selected_doc_ids.clear()
        self._active_doc_id = ""
        self._doc_cards_by_id.clear()
        self._file_cards_by_id.clear()
        self._selected_dossier_ids.clear()
        self._dossier_cards_by_id.clear()
        self._update_selection_toolbar()
        self.btn_clear_search.setEnabled(True)
        self._discard_search_list_cache()
        self._clear_list()
        self._pdf_pane.clear()
        self._search_rank_by_doc = {}
        # Virtualized list page (model + delegate): O(viewport) regardless
        # of how many hits there are.
        self._list_stack.setCurrentWidget(self._search_view)
        self._search_model.set_active("")
        if not self._search_hits:
            self._list_count_label.setText("Không có kết quả.")
            self._right_panel.show_dossier(DossierRow(
                dossier_id=0, title="", fonds="", catalog="", dossier_code="",
                doc_count=0, page_count=0, start_date="", end_date="",
            ))
            self._right_panel.show_message(
                "Không có văn bản phù hợp. Hãy rút gọn từ khóa hoặc bớt điều kiện lọc."
            )
            return
        self._list_count_label.setText(
            f"{len(self._search_hits)} văn bản khớp"
        )
        labels = [
            ("filter", "Khớp bộ lọc"),
            ("exact", "Khớp từ khóa (kể cả không dấu)"),
            ("substring", "Chứa chuỗi con"),
            ("fuzzy", "Kết quả gần đúng"),
        ]
        sorted_hits = self._sorted_search_hits()
        rank = 0
        rank_width = max(2, len(str(len(sorted_hits))))
        entries: list[tuple] = []
        for kind, label in labels:
            group = [h for h in sorted_hits if h.match_kind == kind]
            if not group:
                continue
            entries.append(("group", label, len(group)))
            for hit in group:
                rank += 1
                self._search_rank_by_doc[hit.file_row.doc_id] = rank
                entries.append(("hit", rank, rank_width, hit))
        self._search_model.set_entries(entries)
        selected = self._hits_by_doc.get(self._search_selected_doc_id)
        if selected is None and sorted_hits:
            selected = sorted_hits[0]
        if selected is not None:
            self._show_search_hit(selected)
        if restore_scroll:
            bar = self._search_scrollbar()
            QTimer.singleShot(
                0,
                lambda value=self._search_scroll_value: bar.setValue(value),
            )

    def _search_scrollbar(self):
        """Active scrollbar for the search list (virtualized view when the
        search page is up, else the widget scroll area)."""
        if (getattr(self, "_list_stack", None) is not None
                and self._list_stack.currentIndex() == 1):
            return self._search_view.verticalScrollBar()
        return self._list_scroll.verticalScrollBar()

    def _show_search_tab(self) -> None:
        """Tài liệu tab click: restore the preserved results, or show the
        tab's waiting state when no document search has been run yet.
        Clicking while already on this tab (results or a dossier's file
        list) is a no-op so nothing gets replaced accidentally."""
        if self._mode in (self._MODE_SEARCH, self._MODE_FILES):
            return
        if self._search_query or self._search_filters or self._search_hits:
            self._render_search_results(restore_scroll=True)
        else:
            self._render_search_empty_state()

    def _restore_search_results(self) -> None:
        """'← Kết quả tra cứu' click: leave a dossier's file list and return
        to the preserved document-search results."""
        if self._mode != self._MODE_FILES:
            return
        if self._search_query or self._search_filters or self._search_hits:
            self._render_search_results(restore_scroll=True)
        else:
            self._render_search_empty_state()

    def _render_search_empty_state(self) -> None:
        """Waiting state of the Tra cứu tài liệu tab (no keyword, no filters,
        no results yet)."""
        if self._mode == self._MODE_DOSSIERS:
            self._dossier_scroll_value = self._list_scroll.verticalScrollBar().value()
        self._sync_scope_state()
        self._mode = self._MODE_SEARCH
        self._current_dossier = None
        self._current_file = None
        self._return_to_search = False
        self._active_doc_id = ""
        self._doc_cards_by_id.clear()
        self._selected_doc_ids.clear()
        self._file_cards_by_id.clear()
        self._selected_dossier_ids.clear()
        self._dossier_cards_by_id.clear()
        self._btn_add_file.setVisible(False)
        self._btn_export_dossier_zip.setVisible(False)
        self._configure_sort_options(self._MODE_SEARCH)
        self._update_view_navigation()
        self._set_splitter_for_mode()
        self._apply_scope_ui()
        self._update_selection_toolbar()
        self._clear_list()
        self._pdf_pane.clear()
        self._list_count_label.setText(
            "Nhập từ khóa hoặc mở ▾ để tra cứu tài liệu"
        )
        self._right_panel.show_dossier(DossierRow(
            dossier_id=0, title="(chưa tìm)",
            fonds="", catalog="", dossier_code="",
            doc_count=0, page_count=0, start_date="", end_date="",
        ))
        self._right_panel.show_message(
            "Tra cứu tài liệu: nhập từ khóa trong nội dung OCR, hoặc mở ▾ "
            "để lọc theo thông tin văn bản."
        )

    def _open_search_hit_dossier(self, dossier_id: int) -> None:
        self._search_scroll_value = self._list_scroll.verticalScrollBar().value()
        dossier = self._fetch_dossier_by_id(int(dossier_id))
        if dossier is None:
            self._list_count_label.setText("Không tìm thấy hồ sơ của văn bản.")
            return
        self._show_files_in_dossier(dossier, from_search=True)

    def _on_search_failed(self, err: str):
        self._busy = False
        self._search_cancel_requested = False
        self.btn_search.setText("Tìm")
        self.btn_search.setEnabled(True)
        self.btn_clear_search.setVisible(True)
        self.btn_clear_search.setEnabled(True)
        self._list_count_label.setText(f"Lỗi: {err}")
        self.log_message.emit(f"Search lỗi: {err}", "err")

    def _show_search_hit(self, hit: FileHit):
        """Headline chunk drives initial PDF page; full chunk list goes to
        the right panel as snippet cards (clickable to jump to bbox)."""
        self._search_selected_doc_id = hit.file_row.doc_id
        if self._mode == self._MODE_SEARCH:
            rank = self._search_rank_by_doc.get(hit.file_row.doc_id, 0)
            if rank:
                self._list_count_label.setText(
                    f"{len(self._search_hits)} kết quả · đang xem "
                    f"{rank}/{len(self._search_hits)}"
                )
        full = self._fetch_file_by_doc_id(hit.file_row.doc_id) or hit.file_row
        # Carry over dossier_title from search projection if SQL didn't have one.
        if not full.dossier_title and hit.file_row.dossier_title:
            full.dossier_title = hit.file_row.dossier_title
        chunk_hits = [] if hit.match_kind == "filter" else hit.chunks
        if chunk_hits:
            missing = self._fill_bboxes_from_cache(chunk_hits)
            if missing:
                self._schedule_hydrate(hit.file_row.doc_id, missing)
        self._show_file(full, chunk_hits=chunk_hits or None)

    # ------ Word-bbox hydration: LRU cache + off-thread worker ------

    _BBOX_CACHE_CAP = 4096

    def _fill_bboxes_from_cache(self, chunks: List[SearchResult]) -> List[SearchResult]:
        """Apply cached match bboxes in place. Returns the chunks still
        lacking bboxes (candidates for off-thread hydration)."""
        missing: List[SearchResult] = []
        for chunk in chunks or []:
            if not getattr(chunk, "match_bboxes", None):
                cid = chunk.chunk_id or 0
                if cid in self._bbox_cache:
                    chunk.match_bboxes = self._bbox_cache[cid]
                else:
                    missing.append(chunk)
        return missing

    def _store_bboxes_in_cache(self, chunks: List[SearchResult]) -> None:
        for chunk in chunks or []:
            cid = chunk.chunk_id or 0
            if not cid:
                continue
            # Store even empty lists — they mean "computed, nothing found"
            # and prevent re-reading disk for the same chunk.
            boxes = list(getattr(chunk, "match_bboxes", None) or [])
            self._bbox_cache[cid] = boxes
            self._bbox_cache.move_to_end(cid)
        while len(self._bbox_cache) > self._BBOX_CACHE_CAP:
            self._bbox_cache.popitem(last=False)

    def _schedule_hydrate(self, doc_id: str, chunks: List[SearchResult]) -> None:
        """Hydrate word-level bboxes off the UI thread. Latest request wins:
        while a worker runs, newer requests replace the pending slot."""
        if self._engine is None or not chunks:
            return
        if (self._hydrate_worker is not None
                and self._hydrate_worker.isRunning()):
            self._hydrate_pending = (doc_id, chunks)
            return
        self._start_hydrate_worker(doc_id, chunks)

    def _start_hydrate_worker(self, doc_id: str, chunks: List[SearchResult]) -> None:
        worker = _HydrateWorker(self._engine, doc_id, chunks)
        self._hydrate_worker = worker

        def _on_done(_doc_id: str, hydrated: list):
            self._store_bboxes_in_cache(hydrated or [])
            pending = self._hydrate_pending
            self._hydrate_pending = None
            if pending is not None:
                # Serve the newest request next (coalesced behind this one).
                self._start_hydrate_worker(pending[0], pending[1])
                return
            # Refresh only when still viewing the hydrated document — and
            # only the snippet panel: re-showing the PDF would snap the
            # page back to the headline hit while the user may have
            # navigated elsewhere. The next snippet click picks up the
            # freshly cached bboxes.
            if (self._current_file is not None
                    and _doc_id == self._current_file.doc_id):
                chunks_now = self._current_search_chunks_for_file()
                self._fill_bboxes_from_cache(chunks_now)
                # Round-6 fix: the FIRST open of a search hit races the
                # async hydration — show_pdf ran before the boxes existed
                # and cleared the highlight, so nothing was drawn until
                # the user clicked away and back. Paint the freshly
                # hydrated boxes onto the still-open PDF in place (no
                # page jump) instead of waiting for the next click.
                self._apply_match_highlight_to_pane(chunks_now)
                self._right_panel.show_file(
                    self._current_file, self._archive_path,
                    chunks_now,
                )

        def _on_fail(_msg: str):
            # Silent: the view simply keeps the chunk-bbox fallback.
            self._hydrate_pending = None

        worker.finished_ok.connect(_on_done)
        worker.failed.connect(_on_fail)
        worker.start()

    def _on_snippet_clicked(self, result: SearchResult):
        """Right-panel snippet click → jump PDF to that chunk's page+bbox.

        Metadata chunks are synthesised (no real bbox); just scroll to page
        1 without drawing a highlight rectangle."""
        if not self._current_file or not self._current_file.file_path:
            return
        self._right_panel.set_active_chunk(result.chunk_id or 0)
        pdf_abs = (self._archive_path / self._current_file.file_path).resolve()
        is_meta = (getattr(result, "chunk_type", "body") == "metadata")
        file_chunks = self._current_search_chunks_for_file()
        # Serve from cache; anything missing hydrates off-thread and the
        # view refreshes via the worker callback (chunk bbox shows meanwhile).
        missing = self._fill_bboxes_from_cache(file_chunks or [])
        if missing:
            self._schedule_hydrate(self._current_file.doc_id, missing)
        match_boxes = None if is_meta else (getattr(result, "match_bboxes", None) or None)
        all_match_boxes = [] if is_meta else self._match_page_boxes(file_chunks)
        is_text_match = self._is_text_search_result(result)
        bbox = (
            None
            if (is_meta or match_boxes or is_text_match)
            else (result.bbox or None)
        )
        # Real bboxes carry non-zero width/height; sanity-check just in case.
        if bbox and len(bbox) == 4 and all(v == 0 for v in bbox):
            bbox = None
        self._pdf_pane.show_pdf(pdf_abs, page=1 if is_meta else (result.page or 1),
                                 bbox=bbox, bboxes=match_boxes,
                                 highlight_style="highlight"
                                 if (all_match_boxes or match_boxes)
                                 else "box",
                                 page_bboxes=all_match_boxes)

    def _current_search_chunks_for_file(self) -> list[SearchResult]:
        if not self._current_file:
            return []
        hit = self._hits_by_doc.get(self._current_file.doc_id)
        if hit is None:
            return []
        return list(hit.chunks or [])

    def _apply_match_highlight_to_pane(self,
                                       chunks: List[SearchResult]) -> None:
        """Paint the hydrated match boxes onto the PDF pane WITHOUT moving
        the page. Used after async bbox hydration: the first open of a
        search hit shows the file before the word-level boxes exist (the
        pane clears its highlight), and this callback then draws them in
        place — previously they only appeared after clicking another
        document and back. No-ops when the user already moved to a
        different document or there is nothing to draw."""
        if self._current_file is None or not self._current_file.file_path:
            return
        pdf_abs = (self._archive_path / self._current_file.file_path).resolve()
        pane = self._pdf_pane
        if getattr(pane, "_pdf_path", None) != str(pdf_abs):
            return  # the pane is already showing a different document
        page_boxes = self._match_page_boxes(chunks or [])
        if not page_boxes:
            return
        pane.highlight_page_regions(page_boxes, "highlight")

    # ------ External-open helpers ------

    def _on_open_external(self, abs_path: str):
        try:
            if sys.platform.startswith("win"):
                os.startfile(abs_path)  # noqa: WPS-110
            elif sys.platform == "darwin":
                subprocess.run(["open", abs_path])
            else:
                subprocess.run(["xdg-open", abs_path])
        except Exception as e:
            self.log_message.emit(f"Mở file thất bại: {e}", "err")

    def _on_show_in_folder(self, abs_path: str):
        try:
            if sys.platform.startswith("win"):
                subprocess.run(["explorer", "/select,", abs_path])
            elif sys.platform == "darwin":
                subprocess.run(["open", "-R", abs_path])
            else:
                subprocess.run(["xdg-open", str(Path(abs_path).parent)])
        except Exception as e:
            self.log_message.emit(f"Mở thư mục thất bại: {e}", "err")

    def _on_export_dossier_zip_clicked(self) -> None:
        dossiers: list[DossierRow] = []
        if self._mode == self._MODE_DOSSIERS:
            if not self._selected_dossier_ids:
                QMessageBox.information(
                    self,
                    "Xuất hồ sơ nén",
                    "Hãy tick chọn một hoặc nhiều hồ sơ, hoặc bấm Chọn tất cả.",
                )
                return
            selected = set(self._selected_dossier_ids)
            dossiers = [
                d for d in self._fetch_dossiers()
                if d.dossier_id in selected
            ]
        elif self._mode == self._MODE_FILES and self._current_dossier is not None:
            dossier = self._fetch_dossier_by_id(self._current_dossier.dossier_id)
            if dossier is not None:
                dossiers = [dossier]

        if not dossiers:
            QMessageBox.information(
                self, "Xuất hồ sơ nén", "Chưa có hồ sơ hợp lệ để xuất."
            )
            return

        out_dir = QFileDialog.getExistingDirectory(
            self, translations.localize_text("Chọn thư mục để lưu file ZIP")
        )
        if not out_dir:
            return

        from PySide6.QtWidgets import QProgressDialog
        progress = QProgressDialog(
            "Đang xuất hồ sơ nén…", "Hủy", 0, len(dossiers), self
        )
        progress.setWindowTitle("Xuất hồ sơ nén")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)

        exported: list[str] = []
        errors: list[str] = []
        copied_total = 0
        skipped_total = 0
        for idx, dossier in enumerate(dossiers, start=1):
            if progress.wasCanceled():
                break
            progress.setValue(idx - 1)
            progress.setLabelText(
                f"Đang xuất {idx}/{len(dossiers)}: {self._dossier_code_text(dossier)}"
            )
            QApplication.processEvents()
            try:
                zip_path, copied, skipped = self._export_one_dossier_zip(
                    dossier, out_dir
                )
                exported.append(zip_path)
                copied_total += copied
                skipped_total += skipped
            except Exception as e:
                errors.append(f"{self._dossier_code_text(dossier)}: {e}")
        progress.setValue(len(dossiers))
        progress.close()

        if exported:
            self.log_message.emit(
                f"Kho: đã xuất {len(exported)} hồ sơ nén, "
                f"{copied_total} PDF → {out_dir}"
                + (f" ({skipped_total} PDF thiếu file nguồn)" if skipped_total else ""),
                "success",
            )
        if errors:
            self.log_message.emit(
                "Kho: một số hồ sơ xuất lỗi: " + " | ".join(errors[:5]),
                "err",
            )

        if not exported:
            QMessageBox.critical(
                self,
                "Xuất hồ sơ nén",
                translations.localize_text("Không xuất được hồ sơ nào.")
                + "\n" + "\n".join(errors[:8]),
            )
            return

        msg = "\n".join((
            translations.localize_text(
                f"Đã xuất {len(exported)} file ZIP với {copied_total} PDF."
            ),
            translations.localize_text(f"Thư mục: {out_dir}"),
        ))
        if errors:
            msg += "\n\n" + translations.localize_text(
                f"Có {len(errors)} hồ sơ lỗi, xem nhật ký để biết chi tiết."
            )
        QMessageBox.information(self, "Xuất hồ sơ nén", msg)

    # ------ ZIP → Kho direct import (no OCR/KIE re-run) ------

    def _on_import_zip_clicked(self):
        """Pick one or many exported HSLTCQ ZIPs and import their dossiers
        straight into Kho lưu trữ. Docs with a bundled `.json.zst` sidecar
        use its blocks/KIE fields/annotations as-is; legacy docs (no
        sidecar) get a canonical synthesized from the PDF text layer and
        KIE fields from the workbook. Nothing is re-OCRed."""
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            translations.localize_text("Chọn các file ZIP hồ sơ để nhập vào Kho"),
            "",
            translations.localize_text("ZIP Files (*.zip)"),
        )
        if paths:
            self._import_zip_paths(paths)

    def _import_zip_paths(self, zip_paths: list[str]):
        """Shared back-end for the "Nhập từ ZIP" button and ZIP drag&drop:
        unpack/parse every ZIP into an import job on a worker thread (with
        its own progress dialog), then — in `_continue_zip_kho_import` —
        confirm once and push all dossiers into Kho via one
        `_ZipKhoImportWorker` (no OCR/KIE re-run). ZIPs are extracted under
        `temp/zip_kho_<ts>_<n>/` and cleaned up by the workers once every
        doc is copied into the repo."""
        from PySide6.QtWidgets import QProgressDialog

        if (getattr(self, "_zip_kho_worker", None) is not None
                or getattr(self, "_zip_parse_worker", None) is not None):
            QMessageBox.information(
                self, "Nhập từ ZIP",
                "Một lệnh nhập ZIP khác đang chạy. Vui lòng đợi hoàn tất.",
            )
            return

        zip_paths = [
            str(p) for p in zip_paths
            if str(p).lower().endswith(".zip") and os.path.isfile(str(p))
        ]
        if not zip_paths:
            return

        stamp = time.strftime("%Y%m%d_%H%M%S")

        # Phase 1 — unpack/parse every ZIP on a worker thread, with its own
        # progress dialog. Extraction is the slow part and used to run on
        # the GUI thread before any dialog existed: with many/large ZIPs the
        # screen froze and the import progress bar only appeared long after
        # the click. Phase 2 (codes dialogs → confirm → import) continues in
        # `_continue_zip_kho_import` once parsing finishes.
        parse_progress = QProgressDialog(
            "Đang đọc và giải nén các file ZIP…", "Hủy",
            0, len(zip_paths), self,
        )
        parse_progress.setWindowTitle("Nhập từ ZIP")
        parse_progress.setWindowModality(Qt.WindowModality.WindowModal)
        parse_progress.setMinimumDuration(0)
        parse_progress.setAutoClose(False)
        parse_progress.setAutoReset(False)
        parse_progress.setValue(0)
        parse_progress.show()

        parse_worker = _ZipKhoParseWorker(zip_paths, stamp)

        def on_parse_progress(done: int, total: int, name: str):
            parse_progress.setLabelText(
                f"Đang đọc và giải nén: {name}  ({done}/{total})"
            )
            parse_progress.setValue(done)

        def on_parsed(jobs: list, problems: list):
            parse_progress.close()
            self._continue_zip_kho_import(jobs, problems)

        def on_parse_failed(error_msg: str):
            parse_progress.close()
            self.log_message.emit(
                f"Repository: ZIP parse failed — {error_msg}", "err",
            )
            QMessageBox.critical(
                self, "Lỗi", f"Đọc ZIP thất bại:\n{error_msg}",
            )

        def on_parse_thread_finished():
            if getattr(self, "_zip_parse_worker", None) is parse_worker:
                self._zip_parse_worker = None
            parse_worker.deleteLater()

        parse_worker.progress.connect(on_parse_progress)
        parse_worker.finished_ok.connect(on_parsed)
        parse_worker.failed.connect(on_parse_failed)
        parse_worker.finished.connect(on_parse_thread_finished)
        parse_progress.canceled.connect(parse_worker.cancel)
        # Hold a reference so Python doesn't GC the QThread mid-run.
        self._zip_parse_worker = parse_worker
        parse_worker.start()

    def _continue_zip_kho_import(self, jobs: list[dict], problems: list[str]):
        """Phase 2 of the ZIP → Kho import, back on the GUI thread once
        `_ZipKhoParseWorker` has unpacked every archive: fill in missing
        identity codes (legacy generic-named ZIPs), confirm once, then run
        one `_ZipKhoImportWorker` for all dossiers — no OCR/KIE re-run."""
        # Generic ZIP names (e.g. HSLTCQ.zip): the identity codes are not in
        # the file name. Ask the operator once — the codes only live on this
        # import job.
        for job in jobs:
            codes = job["codes"]
            if not (codes.ma_dinh_danh and codes.fonds):
                dlg = _LegacyZipCodesDialog(job["zip_name"], codes, self)
                if dlg.exec() == QDialog.DialogCode.Accepted:
                    dlg.apply_to(codes)

        kept: list[dict] = []
        for job in jobs:
            codes = job["codes"]
            if not (codes.ma_dinh_danh and codes.fonds):
                problems.append(
                    f"{job['zip_name']}: thiếu mã định danh / mã phông trong tên ZIP"
                )
                shutil.rmtree(job["temp_root"], ignore_errors=True)
            elif not job["docs"]:
                problems.append(
                    f"{job['zip_name']}: không có văn bản PDF trong ZIP"
                )
                shutil.rmtree(job["temp_root"], ignore_errors=True)
            else:
                kept.append(job)
        jobs = kept

        if problems:
            self.log_message.emit(
                "Kho: ZIP bỏ qua khi nhập — " + " | ".join(problems[:5]),
                "info",
            )
        if not jobs:
            QMessageBox.warning(
                self, "Nhập từ ZIP",
                "Không nhập được hồ sơ nào:\n"
                + "\n".join(problems[:8]),
            )
            return

        total = sum(len(j["docs"]) for j in jobs)
        text_layer_total = sum(j["no_companion"] for j in jobs)
        lines = []
        for job in jobs:
            c = job["codes"]
            lines.append(
                f"• {c.ma_dinh_danh}-{c.fonds}-{c.catalog}-{c.dossier_code}"
                f" — {len(job['docs'])} văn bản ({job['zip_name']})"
            )
        text = (
            translations.localize_text(
                f"Nhập {total} văn bản từ {len(jobs)} hồ sơ ZIP vào Kho lưu trữ?"
            )
            + "\n"
            + "\n".join(translations.localize_text(line) for line in lines)
        )
        if text_layer_total:
            text += "\n\n" + translations.localize_text(
                f"({text_layer_total} văn bản không kèm .json.zst — nội dung và "
                "metadata sẽ được lấy từ lớp text PDF)"
            )
        if problems:
            text += "\n\n" + translations.localize_text(
                f"{len(problems)} file ZIP bị bỏ qua, xem nhật ký."
            )
        confirm = QMessageBox.question(
            self, "Nhập vào Kho lưu trữ?", text
        )
        if confirm != QMessageBox.StandardButton.Yes:
            for job in jobs:
                shutil.rmtree(job["temp_root"], ignore_errors=True)
            return

        progress = QProgressDialog(
            "Đang nhập ZIP vào Kho lưu trữ…", "Hủy",
            0, total, self,
        )
        progress.setWindowTitle("Nhập từ ZIP")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.setValue(0)
        progress.show()

        worker = _ZipKhoImportWorker(self._archive_path, jobs)
        # Snapshot for the result dialog: when dedup is OFF, duplicates are
        # kept (already inside `imported`); when ON they were skipped.
        skip_duplicates_hint = _read_skip_duplicate_docs_setting()

        # Same Windows mmap constraint as the Step 3 import: release the
        # screen's Tantivy handles so the writer can take the lock.
        self.release_index_for_writer()

        def on_progress(p):
            done = p.imported + p.skipped + p.failed
            progress.setLabelText(
                f"{p.current_file or ''}  ({done}/{p.total})"
            )
            progress.setValue(done)

        def on_cancelled():
            worker.cancel()

        def _reopen_kho():
            try:
                self.reopen_index_after_writer()
            except Exception as e:
                self.log_message.emit(
                    f"Repository: could not reopen index: {e}", "err",
                )

        def on_finished_ok(result):
            progress.close()
            _reopen_kho()
            import_log = translations.localize_text(
                f"Repository: ZIP imported — {result.imported}/{result.total}"
                f" from {len(jobs)} dossier ZIPs"
            )
            if result.text_layer_imports:
                import_log += translations.localize_text(
                    f", {result.text_layer_imports} qua lớp text PDF"
                )
            if result.duplicates:
                import_log += translations.localize_text(
                    f", {result.duplicates} trùng"
                )
            self.log_message.emit(import_log, "success")
            # Duplicate docs that were KEPT (setting off) are part of
            # `imported`; skipped ones are separate. Surface both counts.
            dup_kept = result.duplicates if not skip_duplicates_hint else 0
            dup_skipped = result.duplicates - dup_kept
            msg = translations.format_import_summary(
                dossiers=len(jobs),
                imported=result.imported,
                duplicates=result.duplicates,
                failed=result.failed,
                duplicate_skipped=dup_skipped,
                duplicate_kept=dup_kept,
            )
            if result.text_layer_imports:
                msg += "\n" + translations.localize_text(
                    f"Documents from PDF text layer (no .json.zst): "
                    f"{result.text_layer_imports}"
                )
            if result.message:
                msg += "\n\n" + translations.localize_text(result.message)
            QMessageBox.information(self, "Nhập từ ZIP hoàn tất", msg)

        def on_failed(error_msg):
            progress.close()
            _reopen_kho()
            self.log_message.emit(f"Repository: ZIP import failed — {error_msg}", "err")
            QMessageBox.critical(self, "Lỗi", f"Nhập ZIP thất bại:\n{error_msg}")

        def on_thread_finished():
            if getattr(self, "_zip_kho_worker", None) is worker:
                self._zip_kho_worker = None
            worker.deleteLater()

        worker.progress.connect(on_progress)
        worker.finished_ok.connect(on_finished_ok)
        worker.failed.connect(on_failed)
        worker.finished.connect(on_thread_finished)
        progress.canceled.connect(on_cancelled)
        # Hold a reference so Python doesn't GC the QThread mid-run.
        self._zip_kho_worker = worker
        worker.start()

    # ------ ZIP drag & drop → Kho import ------

    def dragEnterEvent(self, event):
        """Accept file drags carrying at least one .zip (exported HSLTCQ
        dossier). Internal card-reorder drags use a custom mime type and
        are ignored here (see `_FileCard.dragEnterEvent`)."""
        if self._zip_paths_from_mime(event.mimeData()):
            event.acceptProposedAction()
            return
        event.ignore()

    def dragMoveEvent(self, event):
        # Keep the drop target armed while hovering (Qt only delivers
        # dropEvent to widgets that accepted the preceding move).
        if self._zip_paths_from_mime(event.mimeData()):
            event.acceptProposedAction()
            return
        event.ignore()

    def dropEvent(self, event):
        paths = self._zip_paths_from_mime(event.mimeData())
        if paths:
            event.acceptProposedAction()
            self._import_zip_paths(paths)
            return
        event.ignore()

    @staticmethod
    def _zip_paths_from_mime(md) -> list[str]:
        """Local .zip file paths carried by a drag/drop mime payload."""
        if md is None or not md.hasUrls():
            return []
        return [
            url.toLocalFile() for url in md.urls()
            if url.isLocalFile()
            and url.toLocalFile().lower().endswith(".zip")
        ]

    # ------ CRUD: dossier edit / delete ------

    def _on_edit_dossier(self, dossier: DossierRow) -> None:
        """Open the same DossierInfoDialog used in Bước 1, pre-filled with
        the row's identity. If the 4 identity codes change, the operation
        also renames child PDFs, moves their OCR companions, and updates
        the stored file paths."""
        from scanindex.core.digitization.session import IdentityCodes
        from scanindex.ui.dialogs.archive_session_dialog import DossierInfoDialog
        from scanindex.core.repository import admin
        initial = IdentityCodes(
            ma_dinh_danh=dossier.ma_dinh_danh,
            ma_phong=dossier.fonds,
            ten_phong=dossier.fonds_name,
            muc_luc=dossier.catalog,
            ten_muc_luc=dossier.catalog_name,
            ho_so=dossier.dossier_code,
            title=dossier.title,
            is_unstructured=dossier.is_unstructured,
            thoi_han_bao_quan=dossier.retention,
            tinh_trang_vat_ly=dossier.physical_state,
            nhiem_ky=dossier.term,
            chuyen_de=dossier.topic,
            chu_thich=dossier.note,
        )
        dlg = DossierInfoDialog(
            initial=initial,
            seed_for_unstructured=f"existing-{dossier.dossier_id}",
            parent=self,
        )
        if dlg.exec() != dlg.DialogCode.Accepted:
            return
        codes = dlg.result_codes()
        if codes is None:
            return
        old_key = (
            f"{dossier.ma_dinh_danh}-{dossier.fonds}-"
            f"{dossier.catalog}-{dossier.dossier_code}"
        )
        new_key = (
            f"{codes.ma_dinh_danh}-{codes.ma_phong}-"
            f"{codes.muc_luc}-{codes.ho_so}"
        )
        code_changed = old_key != new_key
        if code_changed:
            ask = QMessageBox.question(
                self,
                "Đổi mã hồ sơ?",
                "Thao tác này sẽ đổi tên toàn bộ PDF, dữ liệu OCR đi kèm "
                "và chuyển thư mục lưu trữ.\n\n"
                f"{old_key}\n→ {new_key}\n\n"
                f"Số văn bản sẽ đổi tên: {dossier.doc_count}\n\n"
                "Tiếp tục?",
            )
            if ask != QMessageBox.StandardButton.Yes:
                return
        try:
            if code_changed:
                self._pdf_pane.clear()
                QApplication.processEvents()
            stats = admin.relabel_dossier(
                self._store, dossier.dossier_id,
                ma_dinh_danh=codes.ma_dinh_danh,
                fonds=codes.ma_phong,
                catalog=codes.muc_luc,
                dossier_code=codes.ho_so,
                title=codes.title,
                is_unstructured=codes.is_unstructured,
                fonds_name=codes.ten_phong,
                catalog_name=codes.ten_muc_luc,
                retention=codes.thoi_han_bao_quan,
                term=codes.nhiem_ky,
                storage_unit=codes.ho_so,
                physical_state=codes.tinh_trang_vat_ly,
                topic=codes.chuyen_de,
                note=codes.chu_thich,
            )
        except Exception as e:
            QMessageBox.critical(self, "Lỗi", f"Không cập nhật được hồ sơ: {e}")
            return
        if stats.code_changed:
            self.log_message.emit(
                f"Đã đổi mã hồ sơ {stats.old_key} → {stats.new_key}; "
                f"đổi tên {stats.renamed_docs} PDF và dữ liệu OCR đi kèm.",
                "success",
            )
        else:
            self.log_message.emit("Đã cập nhật thông tin hồ sơ.", "success")
        self._show_dossier_list()

    def _on_delete_dossier(self, dossier: DossierRow) -> None:
        from scanindex.core.repository import admin
        ask = QMessageBox.question(
            self, "Xóa hồ sơ?",
            f"Xóa hồ sơ '{dossier.title or dossier.dossier_code}' "
            f"và toàn bộ {dossier.doc_count} văn bản trong đó? "
            f"Không thể hoàn tác.",
        )
        if ask != QMessageBox.StandardButton.Yes:
            return
        try:
            self._pdf_pane.clear()
            self._right_panel.show_dossier(DossierRow(
                dossier_id=0, title="", fonds="", catalog="", dossier_code="",
                doc_count=0, page_count=0, start_date="", end_date="",
            ))
            QApplication.processEvents()
            if not self.release_index_for_writer():
                QMessageBox.warning(
                    self, "Kho đang bận",
                    "Kho đang ghi chỉ mục nền. Vui lòng thử lại sau vài giây.",
                )
                return
            self._index.open()  # admin needs writer access
            stats = admin.delete_dossier(self._store, self._index, dossier.dossier_id)
        except Exception as e:
            QMessageBox.critical(self, "Lỗi", f"Xóa thất bại: {e}")
            return
        finally:
            try:
                self._index.close()
                self._index.open()
            except Exception:
                pass
        self.log_message.emit(
            f"Xóa hồ sơ: {stats.deleted_docs} văn bản, "
            f"{stats.deleted_chunks} đoạn, "
            f"~{stats.freed_bytes // 1024} KB"
            + (f" - còn lỗi file: {'; '.join(stats.errors[:3])}" if stats.errors else ""),
            "success" if not stats.errors else "info",
        )
        self._show_dossier_list()

    # ------ CRUD: file multi-select + bulk delete + add ------

    def _on_file_reorder_requested(self, dragged_doc_id: str,
                                   target_doc_id: str,
                                   insert_after: bool) -> None:
        if (self._mode != self._MODE_FILES
                or self._current_dossier is None
                or self._store is None):
            return
        dossier = self._current_dossier
        files = self._fetch_files_for_dossier(dossier.dossier_id)
        current_ids = [file.doc_id for file in files]
        try:
            ordered_ids = _reordered_doc_ids(
                current_ids, dragged_doc_id, target_doc_id, insert_after
            )
        except ValueError as e:
            QMessageBox.warning(self, "Sắp xếp văn bản", str(e))
            return
        if ordered_ids == current_ids:
            return

        # If the stored so_thu_tu sequence has gaps (a slot was skipped,
        # e.g. for a secret doc not scanned), warn before reordering: the
        # move re-stamps numbering to a flat 1..N and would clobber the
        # reserved slot. A contiguous (or all-blank) sequence reorders
        # silently.
        if not _files_so_thu_tu_is_contiguous(files):
            reply = QMessageBox.warning(
                self,
                "Kéo thả sẽ đánh lại số thứ tự",
                "Số thứ tự hiện tại của các văn bản chưa đủ liên tiếp "
                "(đã bỏ một vị trí). Việc kéo thả sẽ đánh lại số thứ tự tuần tự "
                "(1, 2, 3…). Vui lòng kiểm tra chính xác thứ tự của từng văn bản "
                "trước khi xác nhận.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        active_doc_id = self._active_doc_id
        try:
            from scanindex.core.repository import admin
            self._pdf_pane.release_file_handles(timeout=5.0)
            stats = admin.reorder_dossier_documents(
                self._store, dossier.dossier_id, ordered_ids
            )
        except Exception as e:
            if active_doc_id:
                current = self._fetch_file_by_doc_id(active_doc_id)
                if current is not None:
                    self._show_file(current)
            QMessageBox.critical(
                self, "Sắp xếp văn bản",
                f"Không sắp xếp lại được văn bản:\n{e}",
            )
            return

        self.log_message.emit(
            f"Đã sắp xếp lại {len(ordered_ids)} văn bản; "
            f"đổi tên {stats.renamed_docs} PDF theo số thứ tự.",
            "success",
        )
        self._show_files_in_dossier(dossier)
        if active_doc_id:
            updated = self._fetch_file_by_doc_id(active_doc_id)
            if updated is not None:
                self._show_file(updated)

    def _on_file_selection_changed(self, doc_id: str, checked: bool) -> None:
        if checked:
            self._selected_doc_ids.add(doc_id)
        else:
            self._selected_doc_ids.discard(doc_id)
        self._update_selection_toolbar()

    def _on_dossier_selection_changed(self, dossier_id: int, checked: bool) -> None:
        if checked:
            self._selected_dossier_ids.add(dossier_id)
        else:
            self._selected_dossier_ids.discard(dossier_id)
        self._update_selection_toolbar()

    def _update_selection_toolbar(self) -> None:
        if self._mode == self._MODE_DOSSIERS:
            n = len(self._selected_dossier_ids)
            total = len(self._dossier_cards_by_id)
            self._btn_export_dossier_zip.setVisible(total > 0)
            self._btn_export_dossier_zip.setEnabled(n > 0)
            self._btn_export_dossier_zip.setText(
                f"Xuất {n} ZIP" if n > 0 else "Xuất ZIP"
            )
            self._btn_clear_selection.setVisible(total > 0)
            self._btn_clear_selection.setText(
                "Bỏ chọn" if n > 0 else "Chọn tất cả"
            )
            self._btn_bulk_delete.setVisible(n > 0)
            if n > 0:
                self._btn_bulk_delete.setText("🗑︎")
                self._btn_bulk_delete.setToolTip(f"Xóa {n} hồ sơ")
            self._reflow_action_bar()
            return

        if self._mode == self._MODE_FILES:
            n = len(self._selected_doc_ids)
            self._btn_export_dossier_zip.setVisible(True)
            self._btn_export_dossier_zip.setEnabled(self._current_dossier is not None)
            self._btn_export_dossier_zip.setText("Xuất ZIP")
            self._btn_clear_selection.setVisible(n > 0)
            self._btn_clear_selection.setText("Bỏ chọn")
            self._btn_bulk_delete.setVisible(n > 0)
            if n > 0:
                self._btn_bulk_delete.setText("🗑︎")
                self._btn_bulk_delete.setToolTip(f"Xóa {n} văn bản")
            self._reflow_action_bar()
            return

        self._btn_export_dossier_zip.setVisible(False)
        self._btn_clear_selection.setVisible(False)
        self._btn_bulk_delete.setVisible(False)
        self._reflow_action_bar()

    def _toggle_select_all_dossiers(self) -> None:
        if self._mode != self._MODE_DOSSIERS:
            return
        ids = list(self._dossier_cards_by_id.keys())
        if not ids:
            return
        select = len(self._selected_dossier_ids) != len(ids)
        self._selected_dossier_ids = set(ids) if select else set()
        for dossier_id, card in self._dossier_cards_by_id.items():
            card.set_checked(select)
        self._update_selection_toolbar()

    def _clear_selection(self) -> None:
        if self._mode == self._MODE_DOSSIERS and not self._selected_dossier_ids:
            self._toggle_select_all_dossiers()
            return
        for did in list(self._selected_doc_ids):
            card = self._file_cards_by_id.get(did)
            if card is not None:
                card.set_checked(False)
        self._selected_doc_ids.clear()
        for dossier_id in list(self._selected_dossier_ids):
            card = self._dossier_cards_by_id.get(dossier_id)
            if card is not None:
                card.set_checked(False)
        self._selected_dossier_ids.clear()
        self._update_selection_toolbar()

    def _on_bulk_delete_selected(self) -> None:
        if self._mode == self._MODE_DOSSIERS:
            self._on_bulk_delete_dossiers()
            return
        if self._mode == self._MODE_FILES:
            self._on_bulk_delete_files()

    def _on_bulk_delete_dossiers(self) -> None:
        if not self._selected_dossier_ids:
            return
        from scanindex.core.repository import admin
        ids = list(self._selected_dossier_ids)
        n = len(ids)
        ask = QMessageBox.question(
            self, "Xóa hồ sơ?",
            f"Xóa {n} hồ sơ đã chọn và toàn bộ văn bản bên trong? "
            "Không thể hoàn tác.",
        )
        if ask != QMessageBox.StandardButton.Yes:
            return
        try:
            self._pdf_pane.clear()
            self._right_panel.show_dossier(DossierRow(
                dossier_id=0, title="", fonds="", catalog="", dossier_code="",
                doc_count=0, page_count=0, start_date="", end_date="",
            ))
            QApplication.processEvents()
            if not self.release_index_for_writer():
                QMessageBox.warning(
                    self, "Kho đang bận",
                    "Kho đang ghi chỉ mục nền. Vui lòng thử lại sau vài giây.",
                )
                return
            self._index.open()
            stats = admin.delete_dossiers_bulk(self._store, self._index, ids)
        except Exception as e:
            QMessageBox.critical(self, "Lỗi", f"Xóa thất bại: {e}")
            return
        finally:
            try:
                self._index.close()
                self._index.open()
            except Exception:
                pass
        self.log_message.emit(
            f"Xóa {n} hồ sơ: {stats.deleted_docs} văn bản, "
            f"{stats.deleted_chunks} đoạn, ~{stats.freed_bytes // 1024} KB"
            + (f" - còn lỗi file: {'; '.join(stats.errors[:3])}" if stats.errors else ""),
            "success" if not stats.errors else "info",
        )
        self._selected_dossier_ids.clear()
        self._dossier_cards_by_id.clear()
        self._show_dossier_list()

    def _on_bulk_delete_files(self) -> None:
        if not self._selected_doc_ids:
            return
        from scanindex.core.repository import admin
        n = len(self._selected_doc_ids)
        ask = QMessageBox.question(
            self, "Xóa văn bản?",
            f"Xóa {n} văn bản đã chọn? Không thể hoàn tác.",
        )
        if ask != QMessageBox.StandardButton.Yes:
            return
        ids = list(self._selected_doc_ids)
        try:
            if not self.release_index_for_writer():
                QMessageBox.warning(
                    self, "Kho đang bận",
                    "Kho đang ghi chỉ mục nền. Vui lòng thử lại sau vài giây.",
                )
                return
            self._index.open()
            stats = admin.delete_documents_bulk(self._store, self._index, ids)
        except Exception as e:
            QMessageBox.critical(self, "Lỗi", f"Xóa thất bại: {e}")
            return
        finally:
            try:
                self._index.close()
                self._index.open()
            except Exception:
                pass
        self.log_message.emit(
            f"Xóa {stats.deleted_docs} văn bản, {stats.deleted_chunks} đoạn,"
            f" ~{stats.freed_bytes // 1024} KB",
            "success" if not stats.errors else "info",
        )
        if self._current_dossier is not None:
            self._show_files_in_dossier(self._current_dossier)

    def _fetch_document_kie_fields(self, doc_id: str) -> dict:
        if self._store is None or not doc_id:
            return {col: "" for col in KIE_COLUMNS}
        cols = ", ".join(KIE_COLUMNS)
        row = self._store.connect().execute(
            f"SELECT {cols} FROM documents WHERE doc_id = ?",
            (doc_id,),
        ).fetchone()
        if row is None:
            return {col: "" for col in KIE_COLUMNS}
        return {col: row[col] or "" for col in KIE_COLUMNS}

    def _body_chunk_count_for_doc(self, doc_id: str) -> int:
        if self._store is None or not doc_id:
            return 0
        row = self._store.connect().execute(
            "SELECT COUNT(*) AS n FROM chunks "
            "WHERE doc_id = ? AND chunk_type = 'body' "
            "AND indexed_status != 'deleted'",
            (doc_id,),
        ).fetchone()
        return int(row["n"] or 0) if row else 0

    def _refresh_current_file_after_metadata_edit(self, doc_id: str,
                                                  chunk_hits=None) -> None:
        updated = self._fetch_file_by_doc_id(doc_id)
        if updated is None:
            return
        if self._mode == self._MODE_FILES and self._current_dossier is not None:
            dossier = self._current_dossier
            self._show_files_in_dossier(dossier)
            self._show_file(updated)
            return
        if self._mode == self._MODE_SEARCH:
            hit = self._hits_by_doc.get(doc_id)
            if hit is not None:
                hit.file_row = updated
                chunk_hits = hit.chunks
            self._show_file(updated, chunk_hits=chunk_hits or None)
            return
        self._show_file(updated)

    def _on_edit_current_file_metadata(self) -> None:
        if self._current_file is None or self._store is None or self._index is None:
            return
        existing = self._metadata_edit_dialog
        if existing is not None:
            try:
                if existing.isVisible():
                    existing.raise_()
                    existing.activateWindow()
                    return
            except RuntimeError:
                self._metadata_edit_dialog = None

        doc_id = self._current_file.doc_id
        fields = self._fetch_document_kie_fields(doc_id)
        pdf_path = (
            (self._archive_path / self._current_file.file_path).resolve()
            if self._current_file.file_path
            else Path(self._current_file.file_name or doc_id)
        )
        body_count = self._body_chunk_count_for_doc(doc_id)
        dlg = _AddFileMetadataDialog(
            pdf_path=pdf_path,
            body_chunk_count=body_count,
            initial_doc_type=fields.get("kie_doc_type") or self._current_file.doc_type,
            initial_fields=fields,
            window_title="Sửa metadata tài liệu",
            info_text=(
                "Chỉnh metadata đang lưu trong Kho. Sau khi bấm Lưu, "
                "chỉ mục tìm kiếm metadata sẽ được cập nhật lại."
            ),
            parent=self,
        )
        chunk_hits = None
        if self._mode == self._MODE_SEARCH:
            hit = self._hits_by_doc.get(doc_id)
            chunk_hits = hit.chunks if hit is not None else None

        dlg.setModal(False)
        dlg.setWindowModality(Qt.WindowModality.NonModal)
        dlg.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        dlg.accepted.connect(
            lambda d=dlg, did=doc_id, hits=chunk_hits: (
                self._apply_current_file_metadata_edit(did, d, hits)
            )
        )
        dlg.destroyed.connect(lambda *_: setattr(self, "_metadata_edit_dialog", None))
        self._metadata_edit_dialog = dlg
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _apply_current_file_metadata_edit(self, doc_id: str, dlg,
                                          chunk_hits=None) -> None:
        if self._store is None or self._index is None:
            return
        from scanindex.core.repository import admin

        new_fields = dlg.get_fields()
        try:
            if not self.release_index_for_writer():
                QMessageBox.warning(
                    self, "Kho đang bận",
                    "Kho đang ghi chỉ mục nền. Vui lòng thử lại sau vài giây.",
                )
                return
            self._index.open()
            admin.update_document_metadata(self._store, self._index, doc_id, new_fields)
        except Exception as e:
            QMessageBox.critical(self, "Lỗi", f"Không cập nhật được metadata:\n{e}")
            return
        finally:
            try:
                self._index.close()
            except Exception:
                pass
            self.reopen_index_after_writer()
        self.log_message.emit("Đã cập nhật metadata tài liệu.", "success")
        self._refresh_current_file_after_metadata_edit(doc_id, chunk_hits=chunk_hits)

    def _review_kie_before_add(self, *, pdf_path: Path,
                               canonical_json_path: Path,
                               index: int,
                               total: int) -> Optional[dict]:
        from PySide6.QtWidgets import QDialog
        from scanindex.ui.digitization.extraction_step import ArchiveStep2Kie

        dlg = QDialog(self)
        dlg.setWindowTitle(f"Kiểm tra KIE ({index}/{total})")
        dlg.setModal(True)
        dlg.setMinimumSize(1100, 720)
        dlg.resize(1280, 820)
        dlg.setStyleSheet(f"QDialog {{ background: {COLOR_BG}; }}")

        outer = QVBoxLayout(dlg)
        outer.setContentsMargins(SP[3], SP[3], SP[3], SP[3])
        outer.setSpacing(SP[2])

        title = QLabel(f"Kiểm tra KIE {index}/{total}: {pdf_path.name}")
        title.setStyleSheet(f"color: {COLOR_TEXT}; font: 600 14px '{FONT_UI}';")
        outer.addWidget(title)

        hint = QLabel("Sửa bbox nếu cần, bấm Lưu và tiếp tục để đưa văn bản vào Kho.")
        hint.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font: 11px '{FONT_UI}';")
        outer.addWidget(hint)

        step2 = ArchiveStep2Kie(parent=dlg)
        step2.set_review_mode(True, show_file_list=False)
        step2.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        result: dict[str, object] = {}
        try:
            step2.set_documents([{
                "pdf_path": str(pdf_path),
                "ocr_path": str(pdf_path),
                "output_path": str(pdf_path),
                "json_path": str(canonical_json_path),
                "status": "Done",
            }], default_status="Done")
        except Exception as e:
            QMessageBox.critical(self, "Mở KIE Viewer thất bại", str(e))
            return None
        outer.addWidget(step2, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        btn_cancel = QPushButton("Hủy")
        btn_cancel.setFixedHeight(34)
        btn_cancel.setStyleSheet(
            f"QPushButton {{ background: {COLOR_PANEL}; color: {COLOR_TEXT};"
            f" border: 1px solid {COLOR_BORDER}; border-radius: 4px;"
            f" padding: 6px 14px; font: 12px '{FONT_UI}'; }}"
        )
        btn_ok = QPushButton("Lưu và tiếp tục")
        btn_ok.setFixedHeight(34)
        btn_ok.setStyleSheet(
            f"QPushButton {{ background: {COLOR_GREEN}; color: white;"
            f" border: 1px solid {COLOR_GREEN}; border-radius: 4px;"
            f" padding: 6px 14px; font: 600 12px '{FONT_UI}'; }}"
            f"QPushButton:hover {{ background: {COLOR_GREEN_HOVER}; }}"
        )
        row.addWidget(btn_cancel)
        row.addWidget(btn_ok)
        outer.addLayout(row)

        def accept_after_save():
            try:
                if step2.pdf_viewer.is_dirty():
                    if not step2.pdf_viewer.save_now():
                        return
                docs = step2.get_documents()
                if docs:
                    result["metadata"] = dict(docs[0].get("metadata") or {})
                    result["doc"] = docs[0]
                dlg.accept()
            except Exception as e:
                QMessageBox.critical(self, "Lưu KIE thất bại", str(e))

        def reject_after_check():
            if step2.confirm_unsaved_before_leave():
                dlg.reject()

        btn_ok.clicked.connect(accept_after_save)
        btn_cancel.clicked.connect(reject_after_check)

        try:
            if dlg.exec() == QDialog.DialogCode.Accepted:
                return result
            return None
        finally:
            try:
                step2.pdf_viewer.clear()
            except Exception:
                pass

    def _apply_step2_metadata_to_kie_fields(self, kie_fields: dict,
                                            metadata: dict) -> dict:
        out = {col: (kie_fields.get(col) or "") for col in KIE_COLUMNS}

        def value(key: str) -> str:
            return " ".join(str(metadata.get(key) or "").replace("\xa0", " ").split())

        issue_org = value("co_quan_ban_hanh")
        if issue_org:
            out["kie_issue_org_name"] = issue_org
            out["kie_issue_org_superior"] = ""
        doc_type = value("loai_van_ban")
        if doc_type:
            out["kie_doc_type"] = doc_type
        doc_number = value("so_van_ban")
        doc_symbol = value("ky_hieu")
        if doc_number and doc_symbol:
            out["kie_doc_number_symbol"] = f"Số: {doc_number}/{doc_symbol}"
        elif doc_number:
            out["kie_doc_number_symbol"] = f"Số: {doc_number}"
        elif doc_symbol:
            out["kie_doc_number_symbol"] = doc_symbol
        issue_date = value("ngay_ban_hanh")
        if issue_date:
            out["kie_place_date"] = issue_date
        subject = value("trich_yeu")
        if subject:
            out["kie_doc_subject"] = subject
        language = value("ngon_ngu")
        if language:
            out["kie_language"] = language
        signer = value("nguoi_ky")
        if signer:
            out["kie_signer_name"] = signer
        secrecy = value("do_mat")
        if secrecy:
            out["kie_secrecy_mark"] = secrecy
        return out

    def _on_add_file_clicked(self) -> None:
        """Add a single PDF to the currently-open dossier.

        Default flow mirrors Digitization Step 2 for one document: OCR,
        automatic KIE page selection, LayoutLMv3 extraction, then user
        confirmation before the OCRed PDF is added and indexed.
        """
        if self._current_dossier is None:
            return
        target_dossier_id = self._current_dossier.dossier_id
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            translations.localize_text("Chọn file PDF cần thêm"),
            "",
            translations.localize_text("PDF (*.pdf)"),
        )
        if not paths:
            return
        pdf_paths = [Path(p) for p in paths if Path(p).exists()]
        if not pdf_paths:
            return

        from scanindex.infra.data_versioning import get_active_settings_path
        cfg_path = Path(get_active_settings_path())
        kie_mode = "layoutlmv3"
        enable_correction = False
        if cfg_path.exists():
            cfg = configparser.ConfigParser()
            try:
                cfg.read(cfg_path, encoding="utf-8")
                if cfg.has_section("KIE"):
                    kie_mode = (
                        cfg.get("KIE", "Mode", fallback="layoutlmv3")
                        or "layoutlmv3"
                    )
                if cfg.has_section("OCR"):
                    enable_correction = cfg.getboolean(
                        "OCR", "CorrectEnabled", fallback=False
                    )
            except Exception:
                pass

        prepare_worker = _PrepareAddFileWorker(
            pdf_paths,
            kie_mode=kie_mode,
            enable_correction=enable_correction,
        )
        self._prepare_add_worker = prepare_worker

        from PySide6.QtWidgets import QProgressDialog
        prep_prog = QProgressDialog(
            f"Chuẩn bị 0/{len(pdf_paths)}", "Hủy",
            0, len(pdf_paths), self,
        )
        prep_prog.setWindowTitle("Thêm văn bản")
        prep_prog.setWindowModality(Qt.WindowModality.WindowModal)
        prep_prog.setMinimumDuration(0)
        prep_prog.setAutoClose(False)
        prep_prog.setAutoReset(False)
        prep_prog.setValue(0)
        prep_prog.canceled.connect(prepare_worker.cancel)
        prep_prog.show()

        def cleanup_temp(path_text: str = ""):
            if path_text:
                try:
                    shutil.rmtree(Path(path_text), ignore_errors=True)
                except Exception:
                    pass

        def on_prepare_progress(progress):
            if isinstance(progress, dict):
                text = str(progress.get("message") or "").strip()
                total = int(progress.get("total") or len(pdf_paths))
                done = int(progress.get("done") or 0)
                prep_prog.setRange(0, max(1, total))
                prep_prog.setValue(max(0, min(done, total)))
            else:
                text = str(progress or "").strip()
            if text:
                prep_prog.setLabelText(text[:240])

        def start_import(prepared_list):
            prep_prog.close()
            self._prepare_add_worker = None
            if isinstance(prepared_list, dict):
                prepared_list = [prepared_list]
            prepared_list = list(prepared_list or [])
            if not prepared_list:
                QMessageBox.warning(self, "Thêm văn bản", "OCR/KIE không trả về file hợp lệ.")
                return

            accepted_items: list[dict] = []
            work_dirs = {
                str(item.get("work_dir") or "")
                for item in prepared_list if item.get("work_dir")
            }
            total = len(prepared_list)
            for index, prepared in enumerate(prepared_list, start=1):
                source_pdf = Path(prepared.get("source_pdf") or "")
                ocr_pdf = Path(prepared.get("output_pdf") or "")
                output_json = Path(prepared.get("output_json") or "")
                if not ocr_pdf.exists():
                    for wd in work_dirs:
                        cleanup_temp(wd)
                    QMessageBox.warning(
                        self, "Thêm văn bản",
                        f"OCR/KIE đã chạy nhưng không tìm thấy PDF OCR tạm: {source_pdf.name}",
                    )
                    return

                if not output_json.exists():
                    for wd in work_dirs:
                        cleanup_temp(wd)
                    QMessageBox.warning(
                        self, "Thêm văn bản",
                        f"OCR/KIE đã chạy nhưng không tìm thấy JSON KIE tạm: {source_pdf.name}",
                    )
                    return

                review_result = self._review_kie_before_add(
                    pdf_path=ocr_pdf,
                    canonical_json_path=output_json,
                    index=index,
                    total=total,
                )
                if review_result is None:
                    for wd in work_dirs:
                        cleanup_temp(wd)
                    return

                try:
                    from scanindex.core.canonical_io import load_canonical
                    from scanindex.core.repository.importer import _extract_raw_kie_fields
                    canonical = load_canonical(output_json)
                    kie_fields = _extract_raw_kie_fields(canonical)
                    kie_fields = self._apply_step2_metadata_to_kie_fields(
                        kie_fields,
                        dict(review_result.get("metadata") or {}),
                    )
                    ann_block = canonical.get("annotations") or {}
                    kie_annotation_json = json.dumps(ann_block, ensure_ascii=False)
                except Exception as e:
                    for wd in work_dirs:
                        cleanup_temp(wd)
                    QMessageBox.critical(
                        self, "Đọc KIE thất bại",
                        f"Không đọc được dữ liệu KIE sau khi sửa: {e}",
                    )
                    return

                body_chunks = self._extract_body_chunks(ocr_pdf, output_json)
                if not kie_fields.get("kie_doc_subject"):
                    for wd in work_dirs:
                        cleanup_temp(wd)
                    QMessageBox.warning(
                        self, "Thiếu thông tin",
                        "Phải nhập Trích yếu trước khi lưu.",
                    )
                    return

                accepted_items.append({
                    "source_pdf": str(source_pdf),
                    "pdf_path": ocr_pdf,
                    "kie_fields": kie_fields,
                    "body_chunks": body_chunks,
                    "kie_annotation_json": kie_annotation_json,
                })

            worker = _AddFilesWorker(
                self._archive_path,
                target_dossier_id,
                accepted_items,
            )
            self._add_worker = worker

            import_prog = QProgressDialog(
                f"Đang thêm vào Kho 0/{len(accepted_items)}", "Hủy",
                0, len(accepted_items), self,
            )
            import_prog.setWindowTitle("Thêm văn bản")
            import_prog.setWindowModality(Qt.WindowModality.WindowModal)
            import_prog.setMinimumDuration(0)
            import_prog.setAutoClose(False)
            import_prog.setAutoReset(False)
            import_prog.setValue(0)
            import_prog.show()

            if not self.release_index_for_writer():
                import_prog.close()
                for wd in work_dirs:
                    cleanup_temp(wd)
                QMessageBox.warning(
                    self, "Kho đang bận",
                    "Kho đang ghi chỉ mục nền. Vui lòng thử lại sau vài giây.",
                )
                self._add_worker = None
                worker.deleteLater()
                return

            def on_import_progress(progress):
                if isinstance(progress, dict):
                    total_p = int(progress.get("total") or len(accepted_items))
                    done_p = int(progress.get("done") or 0)
                    import_prog.setRange(0, max(1, total_p))
                    import_prog.setValue(max(0, min(done_p, total_p)))
                    text = str(progress.get("message") or "").strip()
                    if text:
                        import_prog.setLabelText(text[:240])

            def on_finished_ok(doc_ids):
                import_prog.close()
                for wd in work_dirs:
                    cleanup_temp(wd)
                self.reopen_index_after_writer()
                count = len(doc_ids or [])
                QMessageBox.information(
                    self, "Thêm văn bản",
                    f"Đã OCR/KIE và thêm {count} văn bản vào hồ sơ.",
                )
                if self._current_dossier is not None:
                    self._show_files_in_dossier(self._current_dossier)
                self._refresh_status()

            def on_failed(msg: str):
                import_prog.close()
                for wd in work_dirs:
                    cleanup_temp(wd)
                self.reopen_index_after_writer()
                self.log_message.emit(msg, "err")
                lines = [line.strip() for line in str(msg).splitlines() if line.strip()]
                short_msg = lines[0] if lines else "Không thêm được văn bản vào Kho."
                QMessageBox.critical(self, "Thêm văn bản thất bại", short_msg)

            def on_thread_finished():
                if getattr(self, "_add_worker", None) is worker:
                    self._add_worker = None
                worker.deleteLater()

            worker.progress.connect(on_import_progress)
            worker.finished_ok.connect(on_finished_ok)
            worker.failed.connect(on_failed)
            worker.finished.connect(on_thread_finished)
            import_prog.canceled.connect(lambda: None)
            worker.start()

        def on_prepare_failed(msg: str):
            prep_prog.close()
            self._prepare_add_worker = None
            self.log_message.emit(msg, "err")
            lines = [line.strip() for line in str(msg).splitlines() if line.strip()]
            short_msg = lines[0] if lines else "OCR/KIE thất bại."
            QMessageBox.critical(self, "OCR/KIE thất bại", short_msg)

        def on_prepare_thread_finished():
            if getattr(self, "_prepare_add_worker", None) is prepare_worker:
                self._prepare_add_worker = None
            prepare_worker.deleteLater()

        prepare_worker.progress.connect(on_prepare_progress)
        prepare_worker.finished_ok.connect(start_import)
        prepare_worker.failed.connect(on_prepare_failed)
        prepare_worker.finished.connect(on_prepare_thread_finished)
        prepare_worker.start()
        return

    def _extract_body_chunks(self, pdf_path: Path, canonical_json_path: Path | None = None):
        """Use canonical OCR JSON for chunks, with PDF text layer as fallback."""
        try:
            import fitz
            from scanindex.core.repository.chunker import Block, chunk_blocks
        except Exception as e:
            self.log_message.emit(f"Repository: chunker import failed: {e}", "err")
            return []
        if canonical_json_path:
            try:
                from scanindex.core.canonical_io import load_canonical
                canonical = load_canonical(canonical_json_path)
                blocks = extract_blocks_from_canonical(canonical)
                if blocks:
                    return chunk_blocks(blocks)
            except Exception as e:
                self.log_message.emit(f"Kho: đọc OCR JSON để tạo chunk thất bại: {e}", "err")
        blocks = []
        try:
            with fitz.open(str(pdf_path)) as doc:
                for pi, page in enumerate(doc):
                    raw = page.get_text("blocks") or []
                    raw.sort(key=lambda b: (b[1], b[0]))
                    for bi, blk in enumerate(raw):
                        if len(blk) < 5:
                            continue
                        x0, y0, x1, y1, text = blk[:5]
                        if not text or not text.strip():
                            continue
                        h = float(y1) - float(y0)
                        line_count = max(1, text.count("\n") + 1)
                        fs = h / line_count if line_count else h
                        blocks.append(Block(
                            page=pi + 1, block_idx=bi,
                            text=text.strip(),
                            bbox=(float(x0), float(y0), float(x1), float(y1)),
                            font_size=fs,
                        ))
        except Exception as e:
            self.log_message.emit(f"Repository: extract blocks failed: {e}", "err")
            return []
        return chunk_blocks(blocks)

    # ------ Cleanup ------

    def closeEvent(self, e):
        try:
            self._discard_search_list_cache()
            if self._prepare_add_worker and self._prepare_add_worker.isRunning():
                self._prepare_add_worker.cancel()
                self._prepare_add_worker.wait(3000)
            if self._add_worker and self._add_worker.isRunning():
                self._add_worker.wait(5000)
            if self._index is not None:
                self._index.close()
            if self._store is not None:
                self._store.close()
        except Exception:
            pass
        super().closeEvent(e)

    # ------ Public refresh hook (called when external code mutates Kho) ------

    def refresh_after_import(self):
        """Called by main_window each time the user navigates into Kho.

        We deliberately do NOT recreate the engine on every screen entry.
        SQLite WAL + autocommit means the existing connection sees rows
        committed by Step 3's import without reopen. Tantivy needs an
        explicit reopen to see writes from a different writer instance."""
        self._discard_search_list_cache()
        if self._store is None:
            self._open_store()
        else:
            # Reopen the read-side index so Step 3's commits become visible.
            try:
                if self._index is not None:
                    self._index.close()
                    self._index.open()
            except Exception as e:
                self.log_message.emit(f"Repository: reload index failed: {e}", "err")
            self._refresh_status()
        if self._mode == self._MODE_DOSSIERS:
            self._show_dossier_list()

    def reset_archive_data(self) -> Path:
        """Destructive reset requested from Settings after typed confirm."""
        if getattr(self, "_prepare_add_worker", None) is not None:
            worker = self._prepare_add_worker
            if worker is not None and worker.isRunning():
                raise RuntimeError("Kho đang OCR/KIE văn bản mới. Hãy chờ tác vụ hoàn tất rồi thử lại.")
        if getattr(self, "_add_worker", None) is not None:
            worker = self._add_worker
            if worker is not None and worker.isRunning():
                raise RuntimeError("Kho đang thêm văn bản. Hãy chờ tác vụ hoàn tất rồi thử lại.")
        if self._index is not None:
            self._index.close()
        if self._store is not None:
            self._store.close()
        self._index = None
        self._store = None
        self._engine = None
        self._importer = None
        self._discard_search_list_cache()
        self._search_hits = []
        self._hits_by_doc = {}
        self._active_doc_id = ""
        self._selected_doc_ids.clear()
        self._selected_dossier_ids.clear()

        from scanindex.infra.data_versioning import get_active_db_filename
        store = ArchiveStore(
            self._archive_path, db_filename=get_active_db_filename()
        )
        store.reset_archive_data()
        store.close()

        self._open_store()
        self._show_dossier_list()
        self._discard_search_list_cache()
        return self._archive_path

    def release_index_for_writer(self) -> bool:
        """Close our Tantivy handle so an external writer (Step 3
        import worker) can safely create the writer lock. On Windows,
        Tantivy's writer fails with ACCESS_DENIED on `.pos` files when a
        second `Index` instance has them mmap-mapped read-only."""
        try:
            if self._index is not None:
                self._index.close()
            return True
        except Exception as e:
            self.log_message.emit(f"Repository: release index failed: {e}", "err")
            return False

    def reopen_index_after_writer(self) -> None:
        """Re-open after the external writer commits + closes its handle."""
        try:
            if self._index is not None:
                self._index.open()
        except Exception as e:
            self.log_message.emit(f"Repository: reopen index failed: {e}", "err")
        self._refresh_status()
        if self._mode == self._MODE_DOSSIERS:
            self._show_dossier_list()


KhoLuuTruScreen = RepositoryScreen

