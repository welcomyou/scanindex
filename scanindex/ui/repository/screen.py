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
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import Qt, Signal, QThread
from PySide6.QtGui import QImage, QPainter, QPen, QColor, QPixmap
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QComboBox, QFrame, QScrollArea, QSplitter,
    QFileDialog, QMessageBox, QProgressBar, QGridLayout,
    QToolButton, QApplication, QCheckBox, QSizePolicy,
)

from scanindex.ui.screens.screen_base import ScreenContent
from scanindex.ui.theme import (
    COLOR_ACCENT, COLOR_ACCENT_HOVER, COLOR_BG, COLOR_BORDER,
    COLOR_BORDER_DEFAULT, COLOR_ELEVATED, COLOR_GREEN, COLOR_GREEN_HOVER,
    COLOR_INPUT, COLOR_PANEL, COLOR_RED, COLOR_RED_HOVER, COLOR_SURFACE,
    COLOR_TEXT, COLOR_TEXT_MUTED, COLOR_TEXT_SECONDARY,
    COMBOBOX_DROPDOWN_QSS,
    BUTTON_PRIMARY_QSS,
    FONT_UI, RADIUS_MD, RADIUS_SM, SP,
)
from scanindex.ui.widgets.fuzzy_combobox import FuzzyComboBox
from scanindex.ui.widgets.pdf_viewer_widget import PdfViewerWidget
from scanindex.infra.paths import get_base_dir

from scanindex.core.repository import constants as C
from scanindex.core.repository.store import ArchiveStore
from scanindex.core.repository.indexer import HybridIndex
from scanindex.core.digitization.session import IdentityCodes
from scanindex.core.repository.importer import (
    Importer, ImportProgress, KIE_COLUMNS, KIE_LABELS,
    extract_blocks_from_canonical,
)
from scanindex.core.repository.search_engine import SearchEngine, SearchResult
from scanindex.core.repository.repair import run_startup_repair
from scanindex.core.repository.tokenizer import to_no_diacritic


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
    finished_ok = Signal(list)
    failed = Signal(str)

    def __init__(self, engine: SearchEngine, query: str, filters: dict, mode: str):
        super().__init__()
        self._engine = engine
        self._query = query
        self._filters = filters
        self._mode = mode

    def run(self):
        try:
            results = self._engine.search(self._query, self._filters, self._mode)
            self.finished_ok.emit(results)
        except Exception as e:
            self.failed.emit(str(e))


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
            doc_id = ""
            store = ArchiveStore(self._archive_path)
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

            imported = []
            total = len(self._items)
            store = ArchiveStore(self._archive_path)
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
                output_pdf = Path(getattr(task, "output_pdf_path", "") or "")
                output_json = Path(getattr(task, "output_json_path", "") or "")
                if not output_pdf.exists() or not output_json.exists():
                    raise RuntimeError(
                        f"{task.file_id}: Thiếu PDF OCR hoặc JSON KIE sau khi xử lý"
                    )

                with open(output_json, "r", encoding="utf-8") as f:
                    canonical = json.load(f)
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
    """Read [Repository] path from settings.ini; default to <base>/repository."""
    base = Path(get_base_dir())
    cfg_path = base / "settings.ini"
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


def _write_repository_path_setting(path: Path) -> None:
    cfg_path = Path(get_base_dir()) / "settings.ini"
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
        max_dist = 0 if len(qt) <= 2 else (1 if len(qt) <= 5 else 2)
    else:
        if len(qt) <= 2:
            if not allow_short_fuzzy:
                return False
            return qt == token
        max_dist = 1 if len(qt) <= 5 else 2
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
            for i in range(0, len(word_spans) - n + 1):
                window = word_spans[i:i + n]
                if all(
                    _display_fuzzy_token_match(
                        qt,
                        tok,
                        allow_short_fuzzy=allow_short_fuzzy,
                    )
                    for qt, (_, _, tok) in zip(qtokens, window)
                ):
                    spans.append((window[0][0], window[-1][1]))
            if not spans:
                matched_by_query = {
                    qt: any(
                        _display_fuzzy_token_match(
                            qt,
                            token,
                            allow_short_fuzzy=allow_short_fuzzy,
                        )
                        for _, _, token in word_spans
                    )
                    for qt in qtokens
                }
                if all(matched_by_query.values()):
                    for start, end, token in word_spans:
                        if any(
                            _display_fuzzy_token_match(
                                qt,
                                token,
                                allow_short_fuzzy=allow_short_fuzzy,
                            )
                            for qt in qtokens
                        ):
                            spans.append((start, end))
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
        return text
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 100:
        y = 2000 + y if y < 50 else 1900 + y
    return f"{d:02d}/{mo:02d}/{y:04d}"


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


def _file_summary_parts(file: "FileRow") -> list[str]:
    parts: list[str] = []
    doc_number = _format_doc_number(file.doc_number)
    date = _format_issue_date(file.issue_date)
    org = _format_issue_org(file.issue_org, file.issue_org_superior)
    signer = _single_line(file.signer_name)
    if doc_number:
        parts.append(doc_number)
    if date:
        parts.append(f"ngày {date}")
    if org:
        parts.append(org)
    if signer:
        parts.append(signer)
    return parts


def _file_summary_text(file: "FileRow") -> str:
    return ", ".join(_file_summary_parts(file))


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
        return "Hồ sơ chưa phân loại"
    return _dossier_code_line(dossier) or "Hồ sơ"


def _dossier_stats_text(dossier: "DossierRow",
                        *,
                        doc_count: Optional[int] = None,
                        page_count: Optional[int] = None) -> str:
    docs = int(doc_count if doc_count is not None else (dossier.doc_count or 0))
    pages = int(page_count if page_count is not None else (dossier.page_count or 0))
    bits = []
    if docs:
        bits.append(f"{docs} tài liệu")
    if pages:
        bits.append(f"{pages} trang")
    if dossier.start_date or dossier.end_date:
        span = " - ".join(filter(None, (dossier.start_date, dossier.end_date)))
        if span:
            bits.append(span)
    return " · ".join(bits)


def _dossier_status_html(dossier: "DossierRow", *, doc_count: Optional[int] = None) -> str:
    title = html.escape(_dossier_display_title(dossier))
    stats = html.escape(_dossier_stats_text(dossier, doc_count=doc_count))
    if not stats:
        return f"<span style='color:{COLOR_TEXT};font-weight:600'>{title}</span>"
    return (
        f"<span style='color:{COLOR_TEXT};font-weight:600'>{title}</span>"
        f"<span style='color:{COLOR_TEXT_MUTED}'> · {stats}</span>"
    )


def _format_repo_stats(dossier_count: int,
                       doc_count: int,
                       page_count: int,
                       chunk_count: int) -> str:
    return (
        f"{int(dossier_count or 0)} hồ sơ · "
        f"{int(doc_count or 0)} tài liệu · "
        f"{int(page_count or 0)} trang · "
        f"{int(chunk_count or 0)} đoạn"
    )


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
            dossier_id=None,
            file_name=head.file_name or "",
            file_path=head.file_path or "",
            subject=head.subject or "",
            doc_number=head.doc_number or "",
            issue_org=head.issue_org or "",
            issue_org_superior=getattr(head, "issue_org_superior", "") or "",
            signer_name=head.signer_name or "",
            issue_date=head.issue_date or "",
            doc_type="",
            secrecy_mark="",
            page_count=0,
            dossier_title=head.dossier_title or "",
        )
        out.append(FileHit(
            file_row=fr,
            chunks=chunks,
            score_total=score_total,
            match_total=match_total,
            match_kind=getattr(head, "match_kind", "") or "",
        ))
    out.sort(key=lambda fh: (fh.match_total, fh.score_total), reverse=True)
    return out


# ---------------------------------------------------------------- Cards


_CARD_QSS = (
    f"QFrame#Card {{ background: {COLOR_SURFACE}; "
    f"  border: 1px solid {COLOR_BORDER}; border-radius: {RADIUS_MD}px; }}"
    f"QFrame#Card:hover {{ border-color: {COLOR_ACCENT}; }}"
    f"QFrame#Card[active=\"true\"] {{ background: {COLOR_ELEVATED}; "
    f"  border-color: {COLOR_ACCENT}; }}"
    f"QFrame#Card QLabel {{ background: transparent; border: none; }}"
)


def _set_card_active(card: QWidget, active: bool) -> None:
    card.setProperty("active", "true" if active else "false")
    card.style().unpolish(card)
    card.style().polish(card)
    card.update()


class _GroupHeader(QLabel):
    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; font: 700 11px '{FONT_UI}';"
            f" padding: 6px 4px 2px 4px; text-transform: uppercase;"
            " background: transparent; border: none;"
        )


class _DossierCard(QFrame):
    """Browse-mode card for one dossier. Body click → emit `clicked`
    (open file list); the small ✏ button on the right → emit `edit_clicked`
    so the host can pop a DossierInfoDialog without losing the body click."""
    clicked = Signal(int)
    edit_clicked = Signal(int)
    selection_changed = Signal(int, bool)

    def __init__(self, dossier: DossierRow, parent=None):
        super().__init__(parent)
        self.dossier = dossier
        self.setObjectName("Card")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(_CARD_QSS)
        h = QHBoxLayout(self)
        h.setContentsMargins(SP[3], SP[2], SP[3], SP[2])
        h.setSpacing(SP[2])

        self._cb = QCheckBox()
        self._cb.setStyleSheet(
            "QCheckBox::indicator { width: 16px; height: 16px; }"
        )
        self._cb.toggled.connect(
            lambda checked: self.selection_changed.emit(
                self.dossier.dossier_id, checked
            )
        )
        h.addWidget(self._cb, 0, Qt.AlignmentFlag.AlignTop)

        # Left side: title + sub-info column (clickable area)
        left = QVBoxLayout()
        left.setSpacing(SP[1])
        title = QLabel("📁 " + _dossier_display_title(dossier))
        title.setWordWrap(True)
        title.setStyleSheet(f"color: {COLOR_TEXT}; font: 600 13px '{FONT_UI}';")
        left.addWidget(title)

        stats_text = _dossier_stats_text(dossier)
        if stats_text:
            sub = QLabel(stats_text)
            sub.setWordWrap(True)
            sub.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font: 11px '{FONT_UI}';")
            left.addWidget(sub)

        codes_line = _dossier_code_line(dossier)
        if codes_line:
            codes_lbl = QLabel(codes_line)
            codes_lbl.setStyleSheet(
                f"color: {COLOR_TEXT_SECONDARY}; font: 11px '{FONT_UI}';"
            )
            left.addWidget(codes_lbl)

        h.addLayout(left, 1)

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
        h.addWidget(btn_edit, 0, Qt.AlignmentFlag.AlignTop)

    def set_checked(self, checked: bool) -> None:
        self._cb.blockSignals(True)
        self._cb.setChecked(checked)
        self._cb.blockSignals(False)

    def set_active(self, active: bool) -> None:
        _set_card_active(self, active)

    def mousePressEvent(self, e):
        # Only emit `clicked` when the press lands on the body, not the
        # checkbox/select gutter/edit button.
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


class _FileCard(QFrame):
    """Browse-mode card for one file inside a dossier. Has a checkbox at
    the top-left for multi-select bulk delete, and a body click area that
    emits `clicked(doc_id)` to open the file."""
    clicked = Signal(str)
    selection_changed = Signal(str, bool)   # (doc_id, checked)

    def __init__(self, file: FileRow, parent=None):
        super().__init__(parent)
        self.file = file
        self.setObjectName("Card")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(_CARD_QSS)
        h = QHBoxLayout(self)
        h.setContentsMargins(SP[3], SP[2], SP[3], SP[2])
        h.setSpacing(SP[2])

        # Checkbox — independent of card body click; toggling it doesn't
        # navigate to the file.
        from PySide6.QtWidgets import QCheckBox
        self._cb = QCheckBox()
        self._cb.setStyleSheet(
            f"QCheckBox::indicator {{ width: 16px; height: 16px; }}"
        )
        self._cb.toggled.connect(
            lambda checked: self.selection_changed.emit(self.file.doc_id, checked)
        )
        h.addWidget(self._cb, 0, Qt.AlignmentFlag.AlignTop)

        v = QVBoxLayout()
        v.setSpacing(SP[1])
        title_text = file.subject or file.file_name or "(không tiêu đề)"
        title = QLabel(title_text)
        title.setWordWrap(True)
        title.setStyleSheet(f"color: {COLOR_TEXT}; font: 600 13px '{FONT_UI}';")
        v.addWidget(title)

        meta_text = _file_summary_text(file)
        if meta_text:
            meta = QLabel(meta_text)
            meta.setWordWrap(True)
            meta.setStyleSheet(f"color: {COLOR_TEXT_SECONDARY}; font: 11px '{FONT_UI}';")
            v.addWidget(meta)

        if file.file_name:
            fn = QLabel(f"📄 {file.file_name}")
            fn.setWordWrap(True)
            fn.setStyleSheet(
                f"color: {COLOR_GREEN}; font: 600 11px '{FONT_UI}';"
            )
            v.addWidget(fn)
        h.addLayout(v, 1)

    def set_checked(self, checked: bool) -> None:
        self._cb.blockSignals(True)
        self._cb.setChecked(checked)
        self._cb.blockSignals(False)

    def set_active(self, active: bool) -> None:
        _set_card_active(self, active)

    def mousePressEvent(self, e):
        # Body click → open file. Clicking the checkbox itself routes
        # through QCheckBox; we only filter that out of the card click.
        if e.button() == Qt.MouseButton.LeftButton:
            child = self.childAt(e.position().toPoint())
            from PySide6.QtWidgets import QCheckBox
            if isinstance(child, QCheckBox):
                super().mousePressEvent(e)
                return
            self.clicked.emit(self.file.doc_id)
        super().mousePressEvent(e)


class _SearchHitCard(QFrame):
    """Search-mode card: file with N matching chunks, dedup score badge."""
    clicked = Signal(str)  # doc_id

    def __init__(self, hit: FileHit, parent=None):
        super().__init__(parent)
        self.hit = hit
        self.setObjectName("Card")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(_CARD_QSS)
        v = QVBoxLayout(self)
        v.setContentsMargins(SP[3], SP[2], SP[3], SP[2])
        v.setSpacing(SP[1])

        f = hit.file_row
        title_text = f.subject or f.file_name or "(không tiêu đề)"
        title = QLabel(title_text)
        title.setWordWrap(True)
        title.setStyleSheet(f"color: {COLOR_TEXT}; font: 600 13px '{FONT_UI}';")
        v.addWidget(title)

        meta_text = _file_summary_text(f)
        if meta_text:
            meta = QLabel(meta_text)
            meta.setWordWrap(True)
            meta.setStyleSheet(f"color: {COLOR_TEXT_SECONDARY}; font: 11px '{FONT_UI}';")
            v.addWidget(meta)

        # File name — segments split from the same long source PDF often
        # share KIE metadata; only the file name distinguishes them.
        if f.file_name:
            fn = QLabel(f"📄 {f.file_name}")
            fn.setWordWrap(True)
            fn.setStyleSheet(
                f"color: {COLOR_GREEN}; font: 600 11px '{FONT_UI}';"
            )
            v.addWidget(fn)

        # Footer: keep user-facing labels meaningful; raw scores are internal
        # ranking numbers, not percentages.
        n = len(hit.chunks)
        unit = "thông tin" if all(
            (getattr(c, "chunk_type", "body") or "body") == "metadata"
            for c in hit.chunks
        ) else "đoạn"
        if hit.match_kind == "fuzzy":
            count = int(hit.match_total or 0)
            suffix = f" · {count} lần gần giống" if count > 0 else ""
            footer_text = f"<span style='color:{COLOR_ACCENT}'>{n} {unit} gần giống</span>{suffix}"
        else:
            count = int(hit.match_total or 0)
            suffix = f" · {count} lần xuất hiện" if count > 0 else ""
            footer_text = f"<span style='color:{COLOR_ACCENT}'>{n} {unit} khớp</span>{suffix}"
        footer = QLabel(footer_text)
        footer.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font: 10px '{FONT_UI}';")
        v.addWidget(footer)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
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
        if is_meta:
            badge = "📋 Tóm tắt văn bản"
        else:
            badge = f"Trang {result.page or '?'}"
        kind = getattr(result, "match_kind", "") or ""
        if kind == "fuzzy":
            suffix = " · gần giống"
        else:
            count = int(getattr(result, "match_count", 0) or 0)
            suffix = f" · {count} lần" if count > 0 else ""
        head = QLabel(f"<b style='color:{COLOR_ACCENT}'>{badge}</b>{suffix}")
        head.setStyleSheet(f"color: {COLOR_TEXT_SECONDARY}; font: 11px '{FONT_UI}';")
        v.addWidget(head)

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
        body = QLabel(body_html or "(không có nội dung)")
        body.setTextFormat(Qt.TextFormat.RichText)
        body.setWordWrap(True)
        body.setStyleSheet(f"color: {COLOR_TEXT}; font: 12px '{FONT_UI}';")
        v.addWidget(body)

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
        self._snippet_cards_by_id: dict[int, _SnippetCard] = {}
        self._active_chunk_id = 0
        self._build_ui()

    def _build_ui(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(SP[2], SP[2], SP[2], SP[2])
        v.setSpacing(SP[2])

        # File info card (always visible)
        self._info_box = QLabel("Chọn 1 hồ sơ hoặc văn bản để xem chi tiết")
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
        display_fonds = d.fonds_name or d.fonds
        display_catalog = d.catalog_name or d.catalog
        rows = [
            f"<b>📁 Hồ sơ:</b> {d.title or '(chưa đặt tên)'}",
        ]
        if not _is_unstructured_dossier(d):
            rows.append(
                f"<b>Phông / Mục lục / Hồ sơ:</b> "
                f"{display_fonds} / {display_catalog} / {d.dossier_code}"
            )
        if d.doc_count:
            rows.append(f"<b>Số văn bản:</b> {d.doc_count}")
        if d.page_count:
            rows.append(f"<b>Tổng số trang:</b> {d.page_count}")
        if d.start_date or d.end_date:
            span = " – ".join(filter(None, (d.start_date, d.end_date)))
            if span:
                rows.append(f"<b>Thời gian:</b> {span}")
        self._info_box.setText("<br>".join(rows))
        self._active_chunk_id = 0
        self._snippet_cards_by_id.clear()
        self._set_snippets_visible(False)
        self._set_actions_enabled(False)

    def show_file(self, f: FileRow, archive_path: Path,
                  chunks: Optional[List[SearchResult]] = None):
        rows = []
        if f.dossier_title:
            rows.append(f"<b>Hồ sơ:</b> {f.dossier_title}")
        if f.subject:
            rows.append(f"<b>Trích yếu:</b> {f.subject}")
        meta_text = _file_summary_text(f)
        if meta_text:
            rows.append(f"<b>Thông tin:</b> {meta_text}")
        if f.doc_type:
            rows.append(f"<b>Loại văn bản:</b> {f.doc_type}")
        if f.secrecy_mark:
            color = _secrecy_mark_color(f.secrecy_mark)
            rows.append(
                f"<b>Độ mật:</b> <span style='color:{color}'>{html.escape(f.secrecy_mark)}</span>"
            )
        if f.file_name:
            rows.append(
                f"<b>Tệp:</b> <span style='color:{COLOR_GREEN};"
                f" font-weight:600'>{f.file_name}</span>"
            )
        self._info_box.setText("<br>".join(rows) or "(không có metadata)")

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
        super().__init__(parent, fit_on_load=False)
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
            ("kie_secrecy_mark",        "Độ mật",              "line"),
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
                w.addItems(all_display_names())
                current_text = initial_fields.get(col) or initial_doc_type
                if current_text:
                    w.setCurrentText(current_text)
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
            if col in initial_fields and kind != "combo":
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
                    out[col] = w.currentText().strip()
                elif isinstance(w, QTextEdit):
                    out[col] = w.toPlainText().strip()
                else:
                    out[col] = w.text().strip()
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
        self._busy = False

        self._mode = self._MODE_DOSSIERS
        self._current_dossier: Optional[DossierRow] = None
        self._current_file: Optional[FileRow] = None
        self._search_hits: List[FileHit] = []
        self._hits_by_doc: dict[str, FileHit] = {}
        self._active_doc_id = ""
        self._doc_cards_by_id: dict[str, QFrame] = {}
        # Multi-select state for bulk delete in dossier/file-list modes.
        self._selected_doc_ids: set[str] = set()
        self._file_cards_by_id: dict[str, "_FileCard"] = {}
        self._selected_dossier_ids: set[int] = set()
        self._dossier_cards_by_id: dict[int, "_DossierCard"] = {}
        self._loading_dossier_filters = False

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
        outer.addWidget(self._build_toolbar())
        # Metadata filter sits directly under the search bar so the user
        # can fill the criteria fields next to the "Tìm" button. The
        # dossier-context action bar (Back / Add / Export) lives below.
        self._filter_panel = self._build_filter_panel()
        self._filter_panel.setVisible(False)
        outer.addWidget(self._filter_panel)
        outer.addWidget(self._build_action_bar())
        self.mode_combo.currentIndexChanged.connect(self._on_search_mode_changed)
        self._on_search_mode_changed()

        # 3-column splitter
        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.setChildrenCollapsible(False)
        self._splitter.setStyleSheet(
            f"QSplitter::handle {{ background: {COLOR_BORDER}; width: 3px; }}"
        )
        # Column 1: list
        self._splitter.addWidget(self._build_list_column())
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
        bar.setObjectName("repositoryActionBar")
        bar.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        bar.setStyleSheet(
            f"QFrame#repositoryActionBar {{ background: {COLOR_PANEL};"
            f" border: 1px solid {COLOR_BORDER}; border-radius: {RADIUS_MD}px; }}"
        )
        h = QHBoxLayout(bar)
        h.setContentsMargins(SP[2], SP[1], SP[2], SP[1])
        h.setSpacing(SP[2])
        action_h = 34

        self._btn_back_to_dossiers = QPushButton("← Hồ sơ")
        self._btn_back_to_dossiers.setVisible(False)
        self._btn_back_to_dossiers.setFixedHeight(action_h)
        self._btn_back_to_dossiers.setMinimumWidth(64)
        self._btn_back_to_dossiers.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        self._btn_back_to_dossiers.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_back_to_dossiers.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {COLOR_ACCENT};"
            f" border: 1px solid transparent; border-radius: 4px;"
            f" padding: 0 10px; font: 600 12px '{FONT_UI}'; }}"
            f"QPushButton:hover {{ background: {COLOR_ELEVATED};"
            f" color: {COLOR_TEXT}; border-color: {COLOR_BORDER}; }}"
        )
        self._btn_back_to_dossiers.clicked.connect(self._show_dossier_list)
        h.addWidget(self._btn_back_to_dossiers)

        self._list_count_label = QLabel("Đang tải hồ sơ…")
        self._list_count_label.setMinimumHeight(action_h)
        self._list_count_label.setMinimumWidth(72)
        self._list_count_label.setTextFormat(Qt.TextFormat.RichText)
        self._list_count_label.setWordWrap(True)
        self._list_count_label.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        self._list_count_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self._list_count_label.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; background: {COLOR_SURFACE};"
            f" border: 1px solid {COLOR_BORDER}; border-radius: 4px;"
            f" padding: 6px 10px; font: 12px '{FONT_UI}';"
        )
        h.addWidget(self._list_count_label, 1)

        self._fonds_filter_combo = QComboBox()
        self._fonds_filter_combo.setToolTip("Lọc theo mã phông")
        self._fonds_filter_combo.setFixedHeight(action_h)
        self._fonds_filter_combo.setMinimumWidth(220)
        self._fonds_filter_combo.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        self._style_dossier_filter_combo(self._fonds_filter_combo)
        self._fonds_filter_combo.currentIndexChanged.connect(
            self._on_fonds_filter_changed
        )
        h.addWidget(self._fonds_filter_combo)

        self._catalog_filter_combo = QComboBox()
        self._catalog_filter_combo.setToolTip("Lọc theo số mục lục")
        self._catalog_filter_combo.setFixedHeight(action_h)
        self._catalog_filter_combo.setMinimumWidth(220)
        self._catalog_filter_combo.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        self._style_dossier_filter_combo(self._catalog_filter_combo)
        self._catalog_filter_combo.currentIndexChanged.connect(
            self._on_catalog_filter_changed
        )
        h.addWidget(self._catalog_filter_combo)

        self._btn_add_file = QPushButton("+ Thêm văn bản")
        self._btn_add_file.setVisible(False)
        self._btn_add_file.setFixedHeight(action_h)
        self._btn_add_file.setMinimumWidth(118)
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

        self._btn_export_dossier_zip = QPushButton("Xuất hồ sơ nén")
        self._btn_export_dossier_zip.setVisible(False)
        self._btn_export_dossier_zip.setFixedHeight(action_h)
        self._btn_export_dossier_zip.setMinimumWidth(128)
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
        return bar

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

    def _build_list_column(self) -> QWidget:
        box = QFrame()
        box.setMinimumWidth(240)
        box.setMaximumWidth(420)
        box.setStyleSheet(
            f"background: {COLOR_BG}; border: 1px solid {COLOR_BORDER};"
            f" border-radius: {RADIUS_MD}px;"
        )
        v = QVBoxLayout(box)
        v.setContentsMargins(SP[1], SP[1], SP[1], SP[1])
        v.setSpacing(SP[1])

        # Scrollable card area
        self._list_scroll = QScrollArea()
        self._list_scroll.setWidgetResizable(True)
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
        v.addWidget(self._list_scroll, 1)
        return box

    def _build_toolbar(self) -> QWidget:
        bar = QFrame()
        bar.setStyleSheet(
            f"QFrame {{ background: {COLOR_SURFACE}; border: 1px solid {COLOR_BORDER};"
            f" border-radius: {RADIUS_MD}px; }}"
        )
        h = QHBoxLayout(bar)
        h.setContentsMargins(SP[2], SP[2], SP[2], SP[2])
        h.setSpacing(SP[2])

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText(
            "Nhập từ khóa tự nhiên (số văn bản, tên người ký, trích yếu, nội dung...)"
        )
        self.search_input.returnPressed.connect(self._on_search_clicked)
        h.addWidget(self.search_input, 1)

        self.btn_filter = QToolButton()
        self.btn_filter.setText("▾")
        self.btn_filter.setToolTip("Tìm kiếm nâng cao")
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

        self.btn_clear_search = QPushButton("Hủy")
        self.btn_clear_search.setVisible(False)
        self.btn_clear_search.setFixedHeight(26)
        self.btn_clear_search.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_clear_search.setStyleSheet(
            f"QPushButton {{ background: {COLOR_RED}; color: white;"
            f" border: 1px solid {COLOR_RED}; border-radius: {RADIUS_SM}px;"
            f" padding: 0 12px; font: 600 12px '{FONT_UI}'; }}"
            f"QPushButton:hover {{ background: {COLOR_RED_HOVER};"
            f" border-color: {COLOR_RED_HOVER}; }}"
            f"QPushButton:pressed {{ background: #991b1b; border-color: #991b1b; }}"
        )
        self.btn_clear_search.clicked.connect(self._clear_search)
        h.addWidget(self.btn_clear_search)

        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Trong Nội dung", "content")
        self.mode_combo.addItem("Trong Metadata", "metadata")
        h.addWidget(self.mode_combo)

        # "Import folder…" removed: import path is now Số hóa lưu trữ
        # → Bước 3 → "Chuyển vào Kho". Direct folder import is gone so
        # there's exactly ONE place to ingest data into Kho.

        self.btn_settings = QPushButton("⚙ Vị trí kho")
        self.btn_settings.clicked.connect(self._pick_archive_path)
        h.addWidget(self.btn_settings)

        return bar

    def _build_status_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("repositoryHeaderInfo")
        bar.setFixedHeight(30)
        bar.setMinimumWidth(220)
        bar.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        bar.setStyleSheet(
            f"QFrame#repositoryHeaderInfo {{ background: {COLOR_PANEL}; border: 1px solid {COLOR_BORDER};"
            f" border-radius: {RADIUS_MD}px; }}"
        )
        h = QHBoxLayout(bar)
        h.setContentsMargins(SP[2], 2, SP[2], 2)
        h.setSpacing(SP[2])

        self._path_label = QLabel("")
        self._path_label.setMinimumWidth(0)
        self._path_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self._path_label.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; font: 11px '{FONT_UI}';"
        )
        h.addWidget(self._path_label, 1)

        self._stats_label = QLabel("")
        self._stats_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self._stats_label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._stats_label.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; font: 11px '{FONT_UI}';"
        )
        h.addWidget(self._stats_label)

        return bar

    def _build_filter_panel(self) -> QWidget:
        panel = QFrame()
        panel.setStyleSheet(
            f"QFrame {{ background: {COLOR_SURFACE}; border: 1px solid {COLOR_BORDER};"
            f" border-radius: {RADIUS_MD}px; }}"
            f"QLabel {{ color: {COLOR_TEXT_SECONDARY}; font: 11px '{FONT_UI}'; }}"
        )
        v = QVBoxLayout(panel)
        v.setContentsMargins(SP[3], SP[2], SP[3], SP[2])
        v.setSpacing(SP[2])

        grid = QGridLayout()
        grid.setHorizontalSpacing(SP[3])
        grid.setVerticalSpacing(SP[2])

        self._filter_inputs: dict[str, QWidget] = {}

        def _add(row: int, col: int, label: str, key: str, width: int = 1):
            grid.addWidget(QLabel(label), row, col * 2)
            le = QLineEdit()
            grid.addWidget(le, row, col * 2 + 1, 1, width)
            self._filter_inputs[key] = le

        def _add_date(row: int, col: int, label: str, key: str):
            grid.addWidget(QLabel(label), row, col * 2)
            w = _DateFilterInput()
            grid.addWidget(w, row, col * 2 + 1)
            self._filter_inputs[key] = w

        def _add_doc_type(row: int, col: int):
            grid.addWidget(QLabel("Loại VB"), row, col * 2)
            combo = FuzzyComboBox()
            try:
                from scanindex.core.digitization.doctype import all_display_names
                combo.addItems(all_display_names())
            except Exception:
                combo.addItems(["Nghị quyết", "Quyết định", "Báo cáo", "Công văn", "Khác"])
            combo.setCurrentIndex(-1)
            grid.addWidget(combo, row, col * 2 + 1)
            self._filter_inputs["doc_type"] = combo
            self._filter_doc_type_combo = combo

        _add(0, 0, "Số ký hiệu", "doc_number")
        _add(0, 1, "Cơ quan", "issue_org")
        _add(0, 2, "Người ký", "signer_name")
        _add_doc_type(1, 0)
        _add(1, 1, "Phông", "fonds")
        _add(1, 2, "Nhiệm kỳ", "term")
        _add_date(2, 0, "Ngày từ", "issue_date_from")
        _add_date(2, 1, "Đến", "issue_date_to")
        _add(2, 2, "Trích yếu", "subject")

        v.addLayout(grid)

        actions = QHBoxLayout()
        actions.addStretch(1)
        btn_reset = QPushButton("Đặt lại")
        btn_reset.clicked.connect(self._reset_filters)
        actions.addWidget(btn_reset)
        btn_apply = QPushButton("Áp dụng")
        btn_apply.setProperty("cssClass", "primary")
        btn_apply.setStyleSheet(BUTTON_PRIMARY_QSS)
        btn_apply.clicked.connect(self._on_search_clicked)
        actions.addWidget(btn_apply)
        v.addLayout(actions)
        return panel

    # ------ Store/Index lifecycle ------

    def _open_store(self):
        try:
            self._store = ArchiveStore(self._archive_path)
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
            self._index = HybridIndex(self._archive_path)
            self._index.open()
            run_startup_repair(
                self._store, self._index,
                log_cb=lambda m: self.log_message.emit(m, "info"),
            )
            self._importer = Importer(self._store, self._index)
            self._engine = SearchEngine(self._store, self._index)
            self._refresh_status()
        except Exception as e:
            QMessageBox.critical(self, "Kho lưu trữ",
                                 f"Không mở được kho:\n{e}")

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
        stats = _format_repo_stats(n_dossiers, n_docs, n_pages, n_chunks)
        self._stats_label.setText(stats)
        self._stats_label.setToolTip(stats)
    def _pick_archive_path(self):
        new_path = QFileDialog.getExistingDirectory(
            self, "Chọn vị trí kho lưu trữ",
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
        while self._list_layout.count() > 1:
            item = self._list_layout.takeAt(0)
            if item is None:
                continue
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _add_card(self, card: QWidget):
        self._list_layout.insertWidget(self._list_layout.count() - 1, card)

    def _set_active_doc_card(self, doc_id: str | None) -> None:
        self._active_doc_id = doc_id or ""
        for cid, card in self._doc_cards_by_id.items():
            if hasattr(card, "set_active"):
                card.set_active(cid == self._active_doc_id)

    # ------ Mode A: dossier list ------

    def _set_dossier_filter_widgets_visible(self, visible: bool) -> None:
        for widget in (
            getattr(self, "_fonds_filter_combo", None),
            getattr(self, "_catalog_filter_combo", None),
        ):
            if widget is not None:
                widget.setVisible(visible)

    def _selected_fonds_filter(self) -> str:
        combo = getattr(self, "_fonds_filter_combo", None)
        if combo is None:
            return ""
        return str(combo.currentData() or "").strip()

    def _selected_catalog_filter(self) -> str:
        combo = getattr(self, "_catalog_filter_combo", None)
        if combo is None:
            return ""
        return str(combo.currentData() or "").strip()

    def _has_active_dossier_filter(self) -> bool:
        return bool(self._selected_fonds_filter() or self._selected_catalog_filter())

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
            "GROUP BY COALESCE(fonds, '') "
            "ORDER BY COALESCE(fonds, '') COLLATE NOCASE"
        ).fetchall()
        return [(r["fonds"] or "", r["fonds_name"] or "") for r in rows]

    def _fetch_catalog_filter_options(self, fonds: str = "") -> list[tuple[str, str]]:
        if self._store is None:
            return []
        where = "WHERE COALESCE(catalog, '') != '' "
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

    def _populate_catalog_filter(self, fonds: str, keep_catalog: str = "") -> None:
        combo = self._catalog_filter_combo
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

    def _populate_dossier_filters(self) -> None:
        if self._loading_dossier_filters:
            return
        self._loading_dossier_filters = True
        try:
            keep_fonds = self._selected_fonds_filter()
            keep_catalog = self._selected_catalog_filter()
            combo = self._fonds_filter_combo
            combo.blockSignals(True)
            try:
                combo.clear()
                combo.addItem("Tất cả phông", "")
                for fonds, fonds_name in self._fetch_fonds_filter_options():
                    combo.addItem(self._combo_label(fonds, fonds_name), fonds)
                idx = combo.findData(keep_fonds)
                combo.setCurrentIndex(idx if idx >= 0 else 0)
                combo.setEnabled(combo.count() > 1)
            finally:
                combo.blockSignals(False)
            self._populate_catalog_filter(self._selected_fonds_filter(), keep_catalog)
        finally:
            self._loading_dossier_filters = False

    def _on_fonds_filter_changed(self) -> None:
        if self._loading_dossier_filters:
            return
        self._loading_dossier_filters = True
        try:
            self._populate_catalog_filter(self._selected_fonds_filter(), "")
        finally:
            self._loading_dossier_filters = False
        if self._mode == self._MODE_DOSSIERS:
            self._show_dossier_list()

    def _on_catalog_filter_changed(self) -> None:
        if self._loading_dossier_filters:
            return
        if self._mode == self._MODE_DOSSIERS:
            self._show_dossier_list()

    def _show_dossier_list(self):
        self._mode = self._MODE_DOSSIERS
        self._current_dossier = None
        self._current_file = None
        self._search_hits = []
        self._hits_by_doc = {}
        self._active_doc_id = ""
        self._doc_cards_by_id.clear()
        self._selected_doc_ids.clear()
        self._file_cards_by_id.clear()
        self._selected_dossier_ids.clear()
        self._dossier_cards_by_id.clear()
        self._btn_back_to_dossiers.setVisible(False)
        self._btn_add_file.setVisible(False)
        self._btn_export_dossier_zip.setVisible(False)
        self._set_dossier_filter_widgets_visible(True)
        self._update_selection_toolbar()
        self.btn_clear_search.setVisible(False)
        self._pdf_pane.clear()
        self._right_panel.show_dossier(DossierRow(
            dossier_id=0, title="(chưa chọn)",
            fonds="", catalog="", dossier_code="",
            doc_count=0, page_count=0, start_date="", end_date="",
        ))
        self._right_panel._info_box.setText(
            "Chọn một hồ sơ ở cột trái để xem các văn bản bên trong."
        )
        self._clear_list()
        if self._store is None:
            self._list_count_label.setText("Chưa mở được kho")
            return
        self._populate_dossier_filters()
        dossiers = self._fetch_dossiers()
        if not dossiers:
            if self._has_active_dossier_filter():
                self._list_count_label.setText("Không có hồ sơ theo bộ lọc.")
            else:
                self._list_count_label.setText(
                    "Kho rỗng. Dùng Bước 3 trong 'Số hóa lưu trữ' để chuyển hồ sơ vào."
                )
            return
        self._list_count_label.setText(f"{len(dossiers)} hồ sơ")
        self._btn_export_dossier_zip.setVisible(True)
        for d in dossiers:
            card = _DossierCard(d)
            card.clicked.connect(lambda _did, dd=d: self._show_files_in_dossier(dd))
            card.edit_clicked.connect(lambda _did, dd=d: self._on_edit_dossier(dd))
            card.selection_changed.connect(self._on_dossier_selection_changed)
            self._dossier_cards_by_id[d.dossier_id] = card
            self._add_card(card)
        self._update_selection_toolbar()

    def _fetch_dossiers(self) -> List[DossierRow]:
        if self._store is None:
            return []
        where_parts: list[str] = []
        params: list[str] = []
        fonds = self._selected_fonds_filter()
        catalog = self._selected_catalog_filter()
        if fonds:
            where_parts.append("COALESCE(d.fonds, '') = ?")
            params.append(fonds)
        if catalog:
            where_parts.append("COALESCE(d.catalog, '') = ?")
            params.append(catalog)
        where_sql = (
            "WHERE " + " AND ".join(where_parts) + " "
            if where_parts else ""
        )
        rows = self._store.connect().execute(
            "SELECT d.dossier_id, d.title, d.ma_dinh_danh, d.fonds, d.fonds_name,"
            "       d.catalog, d.catalog_name,"
            "       d.dossier_code, d.is_unstructured, d.retention, d.term,"
            "       d.storage_unit, d.physical_state, d.topic, d.note,"
            "       d.start_date, d.end_date,"
            "       COUNT(doc.doc_id) AS doc_count,"
            "       COALESCE(SUM(doc.page_count), 0) AS page_count "
            "FROM dossiers d "
            "LEFT JOIN documents doc ON doc.dossier_id = d.dossier_id "
            "    AND doc.indexed_status != 'deleted' "
            f"{where_sql}"
            "GROUP BY d.dossier_id "
            "ORDER BY COALESCE(d.ma_dinh_danh, ''), COALESCE(d.fonds, ''), "
            "         COALESCE(d.catalog, ''), COALESCE(d.dossier_code, ''), "
            "         d.created_at DESC",
            params,
        ).fetchall()
        return [
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
            )
            for r in rows
        ]

    # ------ Mode B: files inside a dossier ------

    def _show_files_in_dossier(self, dossier: DossierRow):
        self._mode = self._MODE_FILES
        self._current_dossier = dossier
        self._current_file = None
        self._active_doc_id = ""
        self._doc_cards_by_id.clear()
        self._selected_doc_ids.clear()
        self._file_cards_by_id.clear()
        self._selected_dossier_ids.clear()
        self._dossier_cards_by_id.clear()
        self._btn_back_to_dossiers.setVisible(True)
        self._btn_add_file.setVisible(True)        # only in file-list mode
        self._btn_export_dossier_zip.setVisible(True)
        self._set_dossier_filter_widgets_visible(False)
        self._update_selection_toolbar()
        self._pdf_pane.clear()
        self._right_panel.show_dossier(dossier)
        self._clear_list()
        files = self._fetch_files_for_dossier(dossier.dossier_id)
        self._list_count_label.setText(_dossier_status_html(dossier, doc_count=len(files)))
        for f in files:
            card = _FileCard(f)
            card.clicked.connect(lambda _did, ff=f: self._show_file(ff))
            card.selection_changed.connect(self._on_file_selection_changed)
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
            "       ds.title AS dossier_title "
            "FROM documents d "
            "LEFT JOIN dossiers ds ON ds.dossier_id = d.dossier_id "
            "WHERE d.dossier_id = ? AND d.indexed_status != 'deleted' "
            "ORDER BY d.file_name",
            (dossier_id,),
        ).fetchall()
        return [self._row_to_file(r) for r in rows]

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
        )

    def _fetch_dossier_by_id(self, dossier_id: int) -> Optional[DossierRow]:
        for dossier in self._fetch_dossiers():
            if dossier.dossier_id == dossier_id:
                return dossier
        return None

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
            f"{kie_cols} "
            "FROM documents d "
            "WHERE d.dossier_id = ? AND d.indexed_status != 'deleted' "
            "ORDER BY d.file_name, d.created_at, d.doc_id",
            (dossier.dossier_id,),
        ).fetchall()
        docs: list[dict] = []
        skipped = 0
        for r in rows:
            pdf_path = (self._archive_path / (r["file_path"] or "")).resolve()
            if not pdf_path.is_file():
                skipped += 1
                continue
            export_name = self._export_pdf_name_for_dossier(
                identity, len(docs) + 1, r["file_name"] or ""
            )
            docs.append({
                "pdf_path": str(pdf_path),
                "export_source_path": str(pdf_path),
                "export_file_name": export_name,
                "annotation": self._annotation_from_repository_doc(r),
                "metadata": {},
            })
        return docs, skipped

    def _export_one_dossier_zip(self, dossier: DossierRow,
                                out_dir: str) -> tuple[str, int, int]:
        import tempfile
        import zipfile
        from scanindex.core.digitization.runner import write_aggregated_excel

        identity = self._identity_from_dossier(dossier)
        export_docs, skipped = self._build_dossier_zip_docs(dossier, identity)
        if not export_docs:
            raise ValueError(
                f"Hồ sơ {self._dossier_code_text(dossier)} không có PDF hợp lệ để xuất"
            )

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
            "       ds.title AS dossier_title "
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
                bbox = None if (is_meta or match_boxes) else (head.bbox or None)
                if bbox and len(bbox) == 4 and all(v == 0 for v in bbox):
                    bbox = None
                self._pdf_pane.show_pdf(pdf_abs, page=head.page or 1,
                                         bbox=bbox,
                                         bboxes=match_boxes,
                                         highlight_style="highlight" if match_boxes else "box")
                self._right_panel.set_active_chunk(head.chunk_id or 0)
            else:
                self._pdf_pane.show_pdf(pdf_abs, page=1, bbox=None)
        else:
            self._pdf_pane.clear()

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
                v = widget.currentText().strip()
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
        for widget in self._filter_inputs.values():
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
        current = combo.currentText().strip()
        try:
            from scanindex.core.digitization.doctype import all_display_names
            values = all_display_names()
        except Exception:
            return
        combo.blockSignals(True)
        try:
            combo.clear()
            combo.addItems(values)
            if current:
                combo.setCurrentText(current)
            else:
                combo.setCurrentIndex(-1)
        finally:
            combo.blockSignals(False)

    def _on_filter_toggle(self, checked: bool):
        if not hasattr(self, "_filter_panel"):
            return
        is_metadata = (self.mode_combo.currentData() or "content") == "metadata"
        self._filter_panel.setVisible(bool(checked and is_metadata))
        if hasattr(self, "btn_filter"):
            self.btn_filter.setText("▴" if checked and is_metadata else "▾")

    def _on_search_mode_changed(self):
        if not hasattr(self, "_filter_panel"):
            return
        mode = self.mode_combo.currentData() or "content"
        is_metadata = mode == "metadata"
        if is_metadata:
            self._refresh_doc_type_filter_choices()
        if hasattr(self, "btn_filter"):
            self.btn_filter.blockSignals(True)
            self.btn_filter.setChecked(is_metadata)
            self.btn_filter.setText("▴" if is_metadata else "▾")
            self.btn_filter.blockSignals(False)
            self.btn_filter.setVisible(is_metadata)
        self._filter_panel.setVisible(is_metadata)

        if is_metadata:
            self.search_input.clear()
            self.search_input.setEnabled(False)
            self.search_input.setPlaceholderText(
                "Vui lòng nhập thông tin vào các trường bên dưới"
            )
        else:
            self.search_input.setEnabled(True)
            self.search_input.setPlaceholderText(
                "Nhập từ khóa tự nhiên (số văn bản, tên người ký, trích yếu, nội dung...)"
            )

    def _clear_search(self):
        """Drop search state and return to dossier-browse mode."""
        self.search_input.clear()
        self._reset_filters()
        self._show_dossier_list()

    def _on_search_clicked(self):
        if self._engine is None:
            return
        if self._busy:
            return
        mode = self.mode_combo.currentData() or "content"
        if mode == "metadata":
            query = ""
            filters = self._collect_filters()
        else:
            query = self.search_input.text().strip()
            filters = {}
        if not query and not filters:
            if mode == "metadata":
                self._list_count_label.setText(
                    "Nhập từ khóa metadata hoặc mở lọc nâng cao."
                )
                self._right_panel._info_box.setText(
                    "Có thể tìm số ký hiệu, ngày tháng, người ký, cơ quan, trích yếu trong ô tìm kiếm; hoặc bấm ▾ để lọc cụ thể."
                )
                return
            # No query → go back to dossier browse.
            self._show_dossier_list()
            return
        self._busy = True
        self.btn_search.setEnabled(False)
        self.btn_clear_search.setEnabled(False)
        self._list_count_label.setText("Đang tìm…")

        if mode == "content":
            self._list_count_label.setText("Đang tìm trong nội dung...")
        elif mode == "metadata":
            self._list_count_label.setText("Đang tìm trong metadata...")

        self._search_worker = SearchWorker(self._engine, query, filters, mode)
        self._search_worker.finished_ok.connect(self._on_search_done)
        self._search_worker.failed.connect(self._on_search_failed)
        self._search_worker.start()

    def _on_search_done(self, results: List[SearchResult]):
        self._busy = False
        self.btn_search.setEnabled(True)
        self._mode = self._MODE_SEARCH
        self._btn_back_to_dossiers.setVisible(False)
        self._btn_add_file.setVisible(False)
        self._btn_export_dossier_zip.setVisible(False)
        self._set_dossier_filter_widgets_visible(False)
        self._selected_doc_ids.clear()
        self._active_doc_id = ""
        self._doc_cards_by_id.clear()
        self._file_cards_by_id.clear()
        self._selected_dossier_ids.clear()
        self._dossier_cards_by_id.clear()
        self._update_selection_toolbar()
        self.btn_clear_search.setVisible(True)
        self.btn_clear_search.setEnabled(True)
        self._search_hits = _group_results_by_file(results)
        self._hits_by_doc = {h.file_row.doc_id: h for h in self._search_hits}
        self._clear_list()
        self._pdf_pane.clear()
        if not self._search_hits:
            self._list_count_label.setText("Không có kết quả.")
            self._right_panel.show_dossier(DossierRow(
                dossier_id=0, title="", fonds="", catalog="", dossier_code="",
                doc_count=0, page_count=0, start_date="", end_date="",
            ))
            if self.mode_combo.currentData() == "metadata":
                self._right_panel._info_box.setText(
                    "Không tìm thấy văn bản khớp metadata."
                )
            else:
                self._right_panel._info_box.setText("Không tìm thấy văn bản phù hợp.")
            return
        self._list_count_label.setText(
            f"{len(self._search_hits)} văn bản khớp"
        )
        mode = self.mode_combo.currentData() or "content"
        if mode == "metadata":
            labels = [
                ("filter", "Metadata khớp điều kiện"),
                ("exact", "Metadata chứa chính xác từ tìm"),
                ("fuzzy", "Metadata chứa từ gần giống"),
            ]
        else:
            labels = [
                ("exact", "Nội dung văn bản chứa chính xác từ tìm"),
                ("fuzzy", "Nội dung văn bản chứa từ gần giống"),
            ]
        for kind, label in labels:
            group = [h for h in self._search_hits if h.match_kind == kind]
            if not group:
                continue
            self._add_card(_GroupHeader(f"{label} ({len(group)})"))
            for hit in group:
                card = _SearchHitCard(hit)
                card.clicked.connect(lambda _did, hh=hit: self._show_search_hit(hh))
                self._doc_cards_by_id[hit.file_row.doc_id] = card
                self._add_card(card)

    def _on_search_failed(self, err: str):
        self._busy = False
        self.btn_search.setEnabled(True)
        self.btn_clear_search.setVisible(True)
        self.btn_clear_search.setEnabled(True)
        self._list_count_label.setText(f"Lỗi: {err}")
        self.log_message.emit(f"Search lỗi: {err}", "err")

    def _show_search_hit(self, hit: FileHit):
        """Headline chunk drives initial PDF page; full chunk list goes to
        the right panel as snippet cards (clickable to jump to bbox)."""
        full = self._fetch_file_by_doc_id(hit.file_row.doc_id) or hit.file_row
        # Carry over dossier_title from search projection if SQL didn't have one.
        if not full.dossier_title and hit.file_row.dossier_title:
            full.dossier_title = hit.file_row.dossier_title
        chunk_hits = [] if hit.match_kind == "filter" else hit.chunks
        if chunk_hits and self._engine is not None:
            self._engine.hydrate_match_bboxes(chunk_hits, limit=48)
        self._show_file(full, chunk_hits=chunk_hits or None)

    def _on_snippet_clicked(self, result: SearchResult):
        """Right-panel snippet click → jump PDF to that chunk's page+bbox.

        Metadata chunks are synthesised (no real bbox); just scroll to page
        1 without drawing a highlight rectangle."""
        if not self._current_file or not self._current_file.file_path:
            return
        self._right_panel.set_active_chunk(result.chunk_id or 0)
        pdf_abs = (self._archive_path / self._current_file.file_path).resolve()
        is_meta = (getattr(result, "chunk_type", "body") == "metadata")
        if (not is_meta
                and self._engine is not None
                and not getattr(result, "match_bboxes", None)):
            self._engine.hydrate_match_bboxes([result], limit=1)
        match_boxes = None if is_meta else (getattr(result, "match_bboxes", None) or None)
        bbox = None if (is_meta or match_boxes) else (result.bbox or None)
        # Real bboxes carry non-zero width/height; sanity-check just in case.
        if bbox and len(bbox) == 4 and all(v == 0 for v in bbox):
            bbox = None
        self._pdf_pane.show_pdf(pdf_abs, page=1 if is_meta else (result.page or 1),
                                 bbox=bbox, bboxes=match_boxes,
                                 highlight_style="highlight" if match_boxes else "box")

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
            self, "Chọn thư mục để lưu file ZIP"
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
                "Không xuất được hồ sơ nào.\n" + "\n".join(errors[:8]),
            )
            return

        msg = (
            f"Đã xuất {len(exported)} file ZIP với {copied_total} PDF.\n"
            f"Thư mục: {out_dir}"
        )
        if errors:
            msg += f"\n\nCó {len(errors)} hồ sơ lỗi, xem nhật ký để biết chi tiết."
        QMessageBox.information(self, "Xuất hồ sơ nén", msg)

    # ------ CRUD: dossier edit / delete ------

    def _on_edit_dossier(self, dossier: DossierRow) -> None:
        """Open the same DossierInfoDialog used in Bước 1, pre-filled with
        the row's identity. If the 4 identity codes change, the operation
        also renames child PDFs and updates their stored file paths."""
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
                "Thao tác này sẽ đổi tên toàn bộ PDF và chuyển thư mục lưu trữ.\n\n"
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
                f"đổi tên {stats.renamed_docs} PDF.",
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
                f"Xuất {n} hồ sơ nén" if n > 0 else "Xuất hồ sơ nén"
            )
            self._btn_clear_selection.setVisible(total > 0)
            self._btn_clear_selection.setText(
                "Bỏ chọn" if n > 0 else "Chọn tất cả"
            )
            self._btn_bulk_delete.setVisible(n > 0)
            if n > 0:
                self._btn_bulk_delete.setText("🗑︎")
                self._btn_bulk_delete.setToolTip(f"Xóa {n} hồ sơ")
            return

        if self._mode == self._MODE_FILES:
            n = len(self._selected_doc_ids)
            self._btn_export_dossier_zip.setVisible(True)
            self._btn_export_dossier_zip.setEnabled(self._current_dossier is not None)
            self._btn_export_dossier_zip.setText("Xuất hồ sơ nén")
            self._btn_clear_selection.setVisible(n > 0)
            self._btn_clear_selection.setText("Bỏ chọn")
            self._btn_bulk_delete.setVisible(n > 0)
            if n > 0:
                self._btn_bulk_delete.setText("🗑︎")
                self._btn_bulk_delete.setToolTip(f"Xóa {n} văn bản")
            return

        self._btn_export_dossier_zip.setVisible(False)
        self._btn_clear_selection.setVisible(False)
        self._btn_bulk_delete.setVisible(False)

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
        from scanindex.core.repository import admin

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
        if dlg.exec() != dlg.DialogCode.Accepted:
            return
        new_fields = dlg.get_fields()
        chunk_hits = None
        if self._mode == self._MODE_SEARCH:
            hit = self._hits_by_doc.get(doc_id)
            chunk_hits = hit.chunks if hit is not None else None
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
            self, "Chọn file PDF cần thêm", "",
            "PDF (*.pdf)",
        )
        if not paths:
            return
        pdf_paths = [Path(p) for p in paths if Path(p).exists()]
        if not pdf_paths:
            return

        cfg_path = Path(get_base_dir()) / "settings.ini"
        kie_mode = "layoutlmv3"
        enable_correction = True
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
                        "OCR", "CorrectEnabled", fallback=True
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
                    from scanindex.core.repository.importer import _extract_raw_kie_fields
                    with open(output_json, "r", encoding="utf-8") as f:
                        canonical = json.load(f)
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
            self.log_message.emit(f"Kho: chunker import failed: {e}", "err")
            return []
        if canonical_json_path:
            try:
                with open(canonical_json_path, "r", encoding="utf-8") as f:
                    canonical = json.load(f)
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
            self.log_message.emit(f"Kho: extract blocks failed: {e}", "err")
            return []
        return chunk_blocks(blocks)

    # ------ Cleanup ------

    def closeEvent(self, e):
        try:
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
        if self._store is None:
            self._open_store()
        else:
            # Reopen the read-side index so Step 3's commits become visible.
            try:
                if self._index is not None:
                    self._index.close()
                    self._index.open()
            except Exception as e:
                self.log_message.emit(f"Kho: reload index failed: {e}", "err")
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
        self._search_hits = []
        self._hits_by_doc = {}
        self._active_doc_id = ""
        self._selected_doc_ids.clear()
        self._selected_dossier_ids.clear()

        store = ArchiveStore(self._archive_path)
        store.reset_archive_data()
        store.close()

        self._open_store()
        self._show_dossier_list()
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
            self.log_message.emit(f"Kho: release index failed: {e}", "err")
            return False

    def reopen_index_after_writer(self) -> None:
        """Re-open after the external writer commits + closes its handle."""
        try:
            if self._index is not None:
                self._index.open()
        except Exception as e:
            self.log_message.emit(f"Kho: reopen index failed: {e}", "err")
        self._refresh_status()
        if self._mode == self._MODE_DOSSIERS:
            self._show_dossier_list()


KhoLuuTruScreen = RepositoryScreen

