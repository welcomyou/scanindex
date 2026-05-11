"""High-level archive pipeline: input folder → OCR → correction → KIE → outputs.

Bridges the BatchPipeline (OCR + correction + KIE serialised per file) with
the assembly + output side (writing _ocr.pdf, canonical JSON, Excel).

Usage from the GUI:

    runner = ArchiveRunner(
        input_dir, output_dir,
        kie_mode="layoutlmv3",
        on_event=lambda kind, payload: ...,   # for UI progress
    )
    runner.start()
    runner.wait()
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Callable, Optional

from scanindex.core.ocr import direct_engine as direct_ocr_engine
from scanindex.core.pipeline.batch_pipeline import (
    BatchPipeline, FileTask,
    EVENT_PIPELINE_DONE, EVENT_FILE_OCR_DONE, EVENT_FILE_COMPLETE,
    EVENT_CORRECTION_START, EVENT_CORRECTION_DONE,
    EVENT_KIE_START, EVENT_KIE_DONE, EVENT_FILE_FAILED, EVENT_PAGE_DONE,
    EVENT_FILE_QUEUED,
)


@dataclass
class ArchiveResult:
    file_name: str
    status: str = "Pending"             # Pending | OCR | Correcting | KIE | Done | Failed
    output_pdf: str = ""
    output_json: str = ""
    annotation: dict | None = None
    error: str = ""
    timing: dict = field(default_factory=dict)


def write_aggregated_excel(tasks_or_docs, excel_path: str,
                            identity=None) -> str:
    """Aggregate per-file metadata into a single Excel workbook.

    Accepts either a list of `FileTask` objects (KIE annotation comes from
    `task.kie_annotation`) or a list of GUI-style document dicts (each with
    a `metadata` map plus optional `annotation`). When `identity` is given
    (an `IdentityCodes` dataclass), the dossier-level "Hồ sơ" sheet is
    populated from it. Returns the path written."""
    from scanindex.core.digitization.metadata_export import annotation_to_row, build_hoso_row, write_excel

    # Section-1 form keys → Văn bản sheet column. User-edited form values
    # take precedence over the raw KIE annotation so that Độ mật / Ngôn ngữ
    # / etc. dropdown choices the operator made on the screen flow into
    # the exported Excel even when the underlying KIE prediction differs.
    _FORM_TO_COLUMN = {
        "co_quan_ban_hanh": "Tên cơ quan, tổ chức ban hành văn bản",
        "loai_van_ban":     "Tên loại văn bản",
        "so_van_ban":       "Số của văn\xa0bản",  # NBSP — see archive_output.EXCEL_COLUMNS
        "ky_hieu":          "Ký hiệu của văn bản",
        "ngay_ban_hanh":    "Ngày, tháng, năm văn bản",
        "trich_yeu":        "Trích yếu nội dung",
        "ngon_ngu":         "Ngôn ngữ",
        "nguoi_ky":         "Người ký",
        "do_mat":           "Độ mật",
    }

    # Lightweight validators for two fields that have UI red-border
    # checks in Step 2: dates and "Số văn bản". Anything that doesn't
    # parse here gets dropped instead of leaking junk into the workbook
    # (matches the "ko parse được thì ko lấy" rule the operator asked
    # for). Date parser accepts the same shapes as the form widget:
    # "14/05/2026", "14-05-2026", "14.5.2026", "12121988" (DDMMYYYY),
    # "121288" (DDMMYY).
    import re as _re
    _date_sep_re = _re.compile(
        r"^\s*(\d{1,2})[\s/.\-](\d{1,2})[\s/.\-](\d{2,4})\s*$"
    )
    _date_8digit_re = _re.compile(r"^\s*(\d{2})(\d{2})(\d{4})\s*$")
    _date_6digit_re = _re.compile(r"^\s*(\d{2})(\d{2})(\d{2})\s*$")
    _number_input_re = _re.compile(r"^\s*\d+[A-Za-z]?\s*$")

    def _normalize_date(text: str) -> str:
        for re_ in (_date_sep_re, _date_8digit_re, _date_6digit_re):
            m = re_.match(text)
            if m:
                d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
                break
        else:
            return ""
        if y < 100:
            y = 2000 + y if y < 50 else 1900 + y
        if not (1 <= mo <= 12 and 1 <= d <= 31 and 1900 <= y <= 9999):
            return ""
        return f"{d:02d}/{mo:02d}/{y:04d}"

    def _apply_form_overrides(row: dict, meta: dict):
        for form_key, col in _FORM_TO_COLUMN.items():
            v = (meta.get(form_key) or "")
            if not isinstance(v, str):
                v = str(v)
            v = v.strip()
            if not v:
                continue
            if form_key == "co_quan_ban_hanh":
                v = " ".join(v.split())
                if not v:
                    continue
            # "Thường" is the UI default for non-secret docs — the HSLTCQ
            # convention is an empty cell, so collapse it back to blank.
            if form_key == "do_mat" and v.lower() == "thường":
                row[col] = ""
                continue
            if form_key == "ngay_ban_hanh":
                v = _normalize_date(v)
                if not v:
                    continue   # bad date → keep KIE-derived value if any
            elif form_key == "so_van_ban":
                if not _number_input_re.match(v):
                    continue   # not a number-like token → don't override
            row[col] = v

    def _hydrate_annotation_from_json(entry: dict) -> dict:
        """KIE pipeline writes the annotation to disk (`<stem>_ocr.pdf.json`)
        but only caches it on `doc["annotation"]` when the user clicks
        the row in Step 2. Files whose row was never opened still have a
        valid annotation — read it from disk so the export covers every
        finished doc, not just the inspected ones."""
        json_path = entry.get("json_path") or ""
        if not json_path:
            output_path = entry.get("output_path") or ""
            if output_path:
                json_path = output_path + ".json"
        if not json_path or not os.path.exists(json_path):
            return {}
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                canonical = json.load(f)
        except Exception:
            return {}
        ann = canonical.get("annotations") or {}
        if not ann.get("field_instances"):
            return {}
        try:
            from scanindex.core.kie.postprocess import apply_layoutlmv3_schema_postprocess
            ann = apply_layoutlmv3_schema_postprocess(canonical, ann)
        except Exception:
            pass
        return ann

    rows = []
    docs_for_hoso: list[dict] = []
    for entry in tasks_or_docs:
        if isinstance(entry, dict):
            # GUI document — prefer annotation if present, else fall back to
            # the form metadata dict directly.
            file_id = (
                os.path.basename(entry.get("export_file_name", "") or "")
                or os.path.basename(entry.get("pdf_path", "") or "")
                or "unknown.pdf"
            )
            ann = entry.get("annotation") or {}
            if not ann:
                ann = _hydrate_annotation_from_json(entry)
            meta = entry.get("metadata", {}) or {}
            # `annotation_to_row` always returns a dict keyed by the
            # 20 HSLTCQ columns (with sensible defaults). Calling it
            # unconditionally — even when KIE didn't run for this file
            # — guarantees the row lines up with the Excel schema; the
            # previous `{"pdf_path": file_id, **meta}` short-circuit
            # produced rows keyed by *form* keys (co_quan_ban_hanh, …),
            # which `write_excel` couldn't look up by column name and
            # rendered as blank rows in the workbook.
            row = annotation_to_row(ann, file_id)
            _apply_form_overrides(row, meta)
            rows.append(row)
            docs_for_hoso.append(entry)
        else:
            ann = getattr(entry, "kie_annotation", None) or {}
            file_id = getattr(entry, "file_id", "") or "unknown.pdf"
            rows.append(annotation_to_row(ann, file_id))
            docs_for_hoso.append({
                "pdf_path": getattr(entry, "input_path", "") or file_id,
            })
    hoso_row = build_hoso_row(identity, rows, docs_for_hoso) if identity else None
    write_excel(rows, excel_path, hoso_row=hoso_row)
    return excel_path


@dataclass
class FileSpec:
    """Caller-provided description of one input file. `pre_ocr_cache`, when
    set, is consulted by the pipeline to skip OCR for already-processed
    pages (handed off from Step 1 of the archive screen)."""
    input_path: str
    file_id: str = ""
    source_document_path: Optional[str] = None
    source_page_indices: Optional[list[int]] = None
    pre_ocr_cache: dict = field(default_factory=dict)  # page_idx -> page_dict
    selected_pages: Optional[list[int]] = None
    from_step1: bool = False


def _unique_output_pdf_path(output_dir: str, stem: str) -> str:
    """Return a non-existing OCR output path for this run.

    On Windows the Step 2 PDF viewer can keep the previous `_ocr.pdf` open.
    Reusing the same path makes PyMuPDF fail while trying to remove the old
    file. Use a run-local suffix when the canonical target already exists.
    """
    base = os.path.join(output_dir, f"{stem}_ocr.pdf")
    if not os.path.exists(base) and not os.path.exists(base + ".json"):
        return base
    for index in range(2, 10000):
        candidate = os.path.join(output_dir, f"{stem}_ocr_r{index}.pdf")
        if not os.path.exists(candidate) and not os.path.exists(candidate + ".json"):
            return candidate
    raise RuntimeError(f"cannot allocate output path for {stem!r} in {output_dir}")


def _unique_preprocessed_pdf_path(output_dir: str, stem: str) -> str:
    pre_dir = os.path.join(output_dir, "_preprocessed")
    os.makedirs(pre_dir, exist_ok=True)
    base = os.path.join(pre_dir, f"{stem}_pre.pdf")
    if not os.path.exists(base):
        return base
    for index in range(2, 10000):
        candidate = os.path.join(pre_dir, f"{stem}_pre_r{index}.pdf")
        if not os.path.exists(candidate):
            return candidate
    raise RuntimeError(f"cannot allocate preprocessed path for {stem!r} in {pre_dir}")


def _same_path(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def _is_step1_handoff(spec: FileSpec) -> bool:
    if getattr(spec, "from_step1", False):
        return True
    return bool(
        spec.pre_ocr_cache
        and spec.source_document_path
        and not _same_path(spec.source_document_path, spec.input_path)
    )


class ArchiveRunner:
    """Orchestrate a folder- or file-list-level archive run."""

    def __init__(self,
                 output_dir: str,
                 input_dir: Optional[str] = None,
                 file_specs: Optional[list] = None,
                 kie_mode: Optional[str] = None,
                 on_event: Optional[Callable[[str, dict], None]] = None,
                 log_cb: Optional[Callable[[str], None]] = None,
                 write_excel_on_done: bool = True,
                 use_signer_page_selector: bool = True,
                 enable_correction: bool = True):
        """Provide either `input_dir` (folder scan) or `file_specs` (explicit
        list — used by Step 1 to feed pre-cut segments with cached OCR).

        `enable_correction=False` skips both the correction-model warmup AND
        the per-file correction pass, so no edits are written to OCR text.
        Honors the user's "Bật sửa chính tả" setting end-to-end."""
        self.input_dir = input_dir
        self.file_specs = file_specs
        self.output_dir = output_dir
        self.kie_mode = self._normalize_kie_mode(kie_mode)
        self.on_event = on_event or (lambda k, p: None)
        self.log = log_cb or (lambda m: None)
        self.write_excel_on_done = write_excel_on_done
        self.use_signer_page_selector = use_signer_page_selector
        self.enable_correction = enable_correction

        self.results: dict[str, ArchiveResult] = {}     # file_id -> result
        self._results_lock = threading.Lock()
        self._pipeline: Optional[BatchPipeline] = None
        self._done_event = threading.Event()
        # file_id -> {page_idx: page_dict} pre-OCR cache supplied by caller.
        # Looked up by `ocr_submit` to avoid re-OCRing pages Step 1 already did.
        self._pre_ocr_cache: dict[str, dict[int, dict]] = {}
        self._tasks_completed: list[FileTask] = []

        os.makedirs(self.output_dir, exist_ok=True)

    @staticmethod
    def _normalize_kie_mode(mode: Optional[str]) -> str:
        key = (mode or "").strip().lower().replace("-", "_")
        if key in {"", "layoutlmv3_visual"}:
            return "layoutlmv3"
        if key == "layoutlmv3":
            return key
        raise ValueError(f"Invalid KIE mode {mode!r}; expected 'layoutlmv3'.")

    # ── public ──────────────────────────────────────────────────────

    def start(self) -> None:
        thread = threading.Thread(target=self._run, name="archive-runner", daemon=True)
        thread.start()

    def wait(self, timeout: Optional[float] = None) -> bool:
        return self._done_event.wait(timeout=timeout)

    def cancel(self) -> None:
        if self._pipeline is not None:
            self._pipeline.cancel()

    # ── workers ─────────────────────────────────────────────────────

    def _run(self) -> None:
        try:
            self._run_inner()
        finally:
            self._done_event.set()
            self.on_event("pipeline_done", {})

    def _scan_pdfs(self) -> list[str]:
        pdfs: list[str] = []
        for root, _, files in os.walk(self.input_dir):
            for f in sorted(files):
                if f.lower().endswith(".pdf"):
                    pdfs.append(os.path.join(root, f))
        return pdfs

    def _run_inner(self) -> None:
        from scanindex.core.pdf.utils import create_corrected_pdf
        from scanindex.core.correction import engine as correction_engine
        from scanindex.core.kie import engine as kie_engine
        from scanindex.core.digitization.metadata_export import (
            annotation_to_row, write_enriched_canonical_json, write_excel,
        )

        # Build the source list — explicit specs win over folder scan
        if self.file_specs:
            specs = list(self.file_specs)
        elif self.input_dir:
            specs = [FileSpec(input_path=p, file_id=os.path.basename(p),
                              source_document_path=p)
                     for p in self._scan_pdfs()]
        else:
            specs = []

        if not specs:
            self.log("No input files")
            return

        self.log(f"Found {len(specs)} PDF file(s)")

        step1_handoff_by_file = {
            (spec.file_id or os.path.basename(spec.input_path)): _is_step1_handoff(spec)
            for spec in specs
        }
        needs_ocr_pool = any(not from_step1 for from_step1 in step1_handoff_by_file.values())

        # Pre-warm pool + (optional) correction + KIE. Step 1 handoff files
        # already have page OCR in `pre_ocr_cache`; do not start OCR workers
        # again unless the user bypassed Step 1 and loaded PDFs from a folder.
        if needs_ocr_pool:
            self.log("Warming OCR pool...")
            direct_ocr_engine._get_pool()
        else:
            self.log("Dùng OCR cache từ Bước 1 — không khởi động OCR lại")
        if self.enable_correction:
            if hasattr(correction_engine, "proton_ct2_available") and not correction_engine.proton_ct2_available():
                self.log("Sửa chính tả Proton CT2 không có trong bản portable — bỏ qua correction")
                self.enable_correction = False
            else:
                self.log("Warming correction model...")
                ok = correction_engine.init_client(log_callback=lambda *a: self.log(" ".join(str(x) for x in a)))
                if ok is False:
                    self.log("Sửa chính tả: không tải được model — bỏ qua correction")
                    self.enable_correction = False
        else:
            self.log("Sửa chính tả: TẮT (theo cấu hình) — bỏ qua warmup correction")
        self.log(f"Warming KIE ({self.kie_mode})...")
        if not kie_engine.warmup_kie(self.kie_mode, log_cb=self.log):
            raise RuntimeError(f"KIE warmup failed for mode={self.kie_mode}")

        # Build tasks
        tasks: list[FileTask] = []
        from scanindex.core.preprocessing import preprocessing
        from scanindex.core.preprocessing.preprocessing import classify_pdf
        for spec in specs:
            file_id = spec.file_id or os.path.basename(spec.input_path)
            from_step1 = step1_handoff_by_file.get(file_id, False)
            stem = os.path.splitext(file_id)[0]
            out_pdf = _unique_output_pdf_path(self.output_dir, stem)
            out_json = out_pdf + ".json"
            original_input_path = spec.input_path
            task_input_path = original_input_path
            preprocess_rotations = None
            if from_step1:
                if not spec.pre_ocr_cache:
                    raise RuntimeError(
                        f"[{file_id}] Step 1 OCR cache is empty; refusing to OCR again in Step 2"
                    )
                self.log(f"[{file_id}] Dùng OCR cache từ Bước 1; bỏ qua preprocess/OCR")
            else:
                pre_pdf = _unique_preprocessed_pdf_path(self.output_dir, stem)
                self.log(f"[{file_id}] Preprocess geometry before OCR...")
                pre_result = preprocessing.pre_process_pdf(
                    original_input_path,
                    pre_pdf,
                    update_callback=lambda m, lvl="info", fid=file_id: self.log(f"[{fid}] {m}"),
                    debug_mode=False,
                    max_workers=max(1, min(4, os.cpu_count() or 4)),
                    return_metadata=True,
                )
                if isinstance(pre_result, tuple) and len(pre_result) == 3:
                    pre_ok, pre_msg, pre_meta = pre_result
                else:
                    pre_ok = bool(pre_result[0]) if isinstance(pre_result, tuple) else bool(pre_result)
                    pre_msg = pre_result[1] if isinstance(pre_result, tuple) and len(pre_result) > 1 else ""
                    pre_meta = {}
                if pre_ok and os.path.exists(pre_pdf):
                    task_input_path = pre_pdf
                    preprocess_rotations = (pre_meta or {}).get("page_rotations") or None
                    self.log(f"[{file_id}] Geometry PDF ready: {pre_msg}")
                else:
                    self.log(f"[{file_id}] Preprocess failed/empty, using original PDF: {pre_msg}")
            task = FileTask(
                file_id=file_id, input_path=task_input_path,
                output_pdf_path=out_pdf, output_json_path=out_json,
                source_document_path=spec.source_document_path or original_input_path,
            )
            task.preprocess_rotations = preprocess_rotations
            task.preprocessed_pdf_path = task_input_path if task_input_path != original_input_path else ""
            task.selected_pages = list(spec.selected_pages) if spec.selected_pages else None
            task.source_page_indices = list(spec.source_page_indices) if spec.source_page_indices else None
            task.from_step1_cache = bool(from_step1)
            # Step 1 handoff uses the visual OCR cache and may no longer have
            # the temporary split PDF by the time KIE assembles output, so keep
            # it on the OCR-backed path instead of the native-text copy path.
            task.is_digital = (
                False if from_step1
                else (classify_pdf(original_input_path) == "digital")
            )
            tasks.append(task)
            with self._results_lock:
                self.results[file_id] = ArchiveResult(file_name=file_id)
            if spec.pre_ocr_cache:
                self._pre_ocr_cache[file_id] = dict(spec.pre_ocr_cache)

        # Map input_path -> file_id so the OCR adapter can look up the cache
        # (BatchPipeline only hands us `input_path` + `page_idx` per submit).
        path_to_id = {t.input_path: t.file_id for t in tasks}
        path_to_task = {t.input_path: t for t in tasks}

        # Define stage callables
        def ocr_submit(input_path: str, page_idx: int):
            file_id = path_to_id.get(input_path)
            task = path_to_task.get(input_path)
            cache = self._pre_ocr_cache.get(file_id) if file_id else None
            if cache is not None:
                cached = cache.get(page_idx)
                if cached is not None:
                    return direct_ocr_engine.make_prebaked_async_result(
                        page_idx, cached
                    )
            if task is not None and getattr(task, "from_step1_cache", False):
                raise RuntimeError(
                    f"[{file_id}] Step 1 OCR cache missing page {page_idx + 1}; "
                    "refusing to OCR again in Step 2"
                )
            return direct_ocr_engine.submit_page(input_path, page_idx)

        def run_correction(task: FileTask) -> str:
            # Build raw text from page results, run chunked correction
            raw_lines: list[str] = []
            for pi in sorted(task.page_results):
                for ln in task.page_results[pi].get("lines_data", []):
                    t = (ln.get("text") or "").strip()
                    if t:
                        raw_lines.append(t)
            raw = "\n".join(raw_lines)
            task.raw_text = raw
            # Digital PDFs keep the source PDF and use merged native/OCR JSON for
            # KIE, so do not write a correction overlay back onto the PDF.
            if task.is_digital:
                self.log(f"[{task.file_id}] Skip correction: digital PDF uses merged native/OCR KIE JSON")
                return raw
            # Honor the user's "Bật sửa chính tả" setting: when off, return the
            # raw OCR text as-is so no edits are made to the output PDF.
            if not self.enable_correction:
                return raw
            return correction_engine.correct_text(raw) if raw.strip() else raw

        def run_kie(task: FileTask) -> dict:
            # Assemble the OCR PDF + canonical JSON BEFORE running KIE
            try:
                self._assemble_outputs(task)
            except Exception as e:
                self.log(f"[{task.file_id}] assemble failed: {e}")
                task.error = f"assemble: {e}"
                raise
            try:
                if task.selected_pages is None and self.use_signer_page_selector:
                    task.selected_pages = self._select_pages_for_kie(task)
                self._prepare_signature_page_clean_ocr_for_kie(task)
                # Surface the KIE pre-conditions so silent empty-result bugs
                # (model failed to load, no pages selected, schema mismatch)
                # are visible in the log instead of looking like "KIE done".
                model_ready = kie_engine.is_kie_ready(self.kie_mode)
                self.log(
                    f"[{task.file_id}] KIE: mode={self.kie_mode} "
                    f"ready={model_ready} "
                    f"selected_pages={task.selected_pages}"
                )
                ann = kie_engine.extract_metadata_kie(
                    task.output_json_path,
                    mode=self.kie_mode,
                    selected_pages=task.selected_pages,
                )
                n_fields = len((ann or {}).get("field_instances", []))
                if n_fields == 0:
                    self.log(
                        f"[{task.file_id}] KIE WARN: 0 field_instances returned. "
                        f"Form sẽ trống. Model loaded={model_ready}, "
                        f"selected_pages={task.selected_pages}"
                    )
                else:
                    self.log(f"[{task.file_id}] KIE: {n_fields} field_instances")
                # Write enriched JSON
                write_enriched_canonical_json(task.output_json_path, ann)
                return ann
            except Exception as e:
                self.log(f"[{task.file_id}] KIE failed: {e}")
                traceback.print_exc()
                raise

        def on_pipeline_event(evt, task, payload):
            self._on_pipeline_event(evt, task, payload)

        # Run
        self._pipeline = BatchPipeline(
            ocr_submit=ocr_submit,
            run_correction=run_correction,
            run_kie=run_kie,
            on_event=on_pipeline_event,
            log_cb=self.log,
        )
        for task in tasks:
            self._pipeline.add_file(task)
        self._pipeline.mark_no_more_files()

        self.log(">>> Pipeline START <<<")
        t0 = time.time()
        self._pipeline.start()
        self._pipeline.wait()
        dt = time.time() - t0

        self._tasks_completed = list(tasks)

        if self.write_excel_on_done:
            try:
                excel_path = os.path.join(self.output_dir, "MetaDuLieu.xlsx")
                write_aggregated_excel(tasks, excel_path)
                self.log(f"Excel written: {excel_path}")
            except Exception as e:
                self.log(f"Excel write failed: {e}")

        self.log(f"=== Archive complete in {dt:.1f}s ({len(tasks)} files) ===")

    # ── output assembly ─────────────────────────────────────────────

    def _assemble_outputs(self, task: FileTask) -> None:
        """Build `_ocr.pdf` + canonical JSON for this file.

        When every page is already in `task.page_results`, assemble directly
        from that OCR layer. If page results are missing for non-digital files,
        run the normal OCR+assemble path. Digital files must not use the
        native-only path here because page 0 needs native+OCR merging and later
        signer pages need the visual OCR layer."""
        have_all_pages = (
            task.num_pages > 0
            and len(task.page_results) >= task.num_pages
            and all(i in task.page_results for i in range(task.num_pages))
        )
        failed_pages = []
        for i, result in sorted(task.page_results.items()):
            result = result or {}
            if (
                not (result.get("lines_data") or result.get("words_data"))
                and int(result.get("render_width") or 0) <= 0
                and int(result.get("render_height") or 0) <= 0
            ):
                failed_pages.append(i)
        if failed_pages:
            pages = ", ".join(str(i + 1) for i in failed_pages)
            raise RuntimeError(f"OCR page result missing/failed for page(s): {pages}")
        if task.is_digital and not have_all_pages:
            raise RuntimeError("digital PDF requires complete page OCR results for native/OCR layer merge")
        if have_all_pages:
            ok, msg = direct_ocr_engine.assemble_pdf_from_page_results(
                task.input_path, task.output_pdf_path,
                task.page_results,
                source_document_path=task.source_document_path or task.input_path,
                source_page_indices=getattr(task, "source_page_indices", None),
                preprocess_rotations=getattr(task, "preprocess_rotations", None),
                update_callback=lambda m, lvl="info": self.log(m),
                canonical_profile="layoutlmv3_runtime",
                include_layout_analysis=False,
            )
        else:
            ok, msg = direct_ocr_engine.process_pdf(
                task.input_path, task.output_pdf_path,
                num_pages=task.num_pages,
                update_callback=lambda m, lvl="info": self.log(m),
                wait_per_page=1.0, comparison_interval=1.0,
                source_document_path=task.source_document_path or task.input_path,
                allow_page_parallel=True,
                preprocess_rotations=getattr(task, "preprocess_rotations", None),
                canonical_profile="layoutlmv3_runtime",
                include_layout_analysis=False,
            )
        if not ok:
            raise RuntimeError(f"process_pdf failed: {msg}")
        if task.is_digital and task.num_pages > 0:
            try:
                # Keep the visible output as the original digital PDF, but feed
                # KIE a merged JSON: native text wins, OCR contributes only
                # visual-only words such as stamps/handwriting.
                shutil.copy2(task.input_path, task.output_pdf_path)
                from scanindex.core.pdf.text_extractor import merge_native_text_layer_into_canonical_json
                merge_stats = merge_native_text_layer_into_canonical_json(
                    task.output_json_path,
                    task.input_path,
                    merge_pages=[0],
                    canonical_profile="layoutlmv3_runtime",
                )
                self.log(f"[{task.file_id}] Digital layer merge page0: {merge_stats}")
            except Exception as e:
                self.log(f"[{task.file_id}] digital layer merge failed: {e}")
                raise
        # Apply correction overlay to the _ocr.pdf
        if task.raw_text and task.corrected_text and task.raw_text != task.corrected_text:
            try:
                from scanindex.core.pdf.utils import create_corrected_pdf
                create_corrected_pdf(
                    task.output_pdf_path, task.output_pdf_path,
                    task.raw_text, task.corrected_text,
                    log_callback=lambda m, lvl="info": self.log(m),
                )
            except Exception as e:
                self.log(f"[{task.file_id}] correction PDF rewrite failed: {e}")

    def _select_pages_for_kie(self, task: FileTask) -> list[int] | None:
        """Choose pages for KIE: page 0 plus signer-page top-1."""
        try:
            from scanindex.core.digitization import page_splitter as archive_page_splitter

            result = archive_page_splitter.predict_signer_page(task.output_json_path)
            task.page_selection = result
            signer_page = result.get("signer_page")
            if signer_page is None:
                return [0] if task.num_pages > 0 else None
            signer_page = int(signer_page)
            task.signature_page = signer_page
            selected = sorted({0, signer_page})
            self.log(
                f"[{task.file_id}] KIE selected_pages={selected} "
                f"(signer_page={signer_page}, score={result.get('signer_score')})"
            )
            return selected
        except Exception as e:
            self.log(f"[{task.file_id}] signer-page selector failed: {e}")
            raise

    def _prepare_signature_page_clean_ocr_for_kie(self, task: FileTask) -> bool:
        """OCR the LightGBM signer page again without PDF annotations for KIE."""
        signer_page = task.signature_page
        if signer_page is None:
            return False
        try:
            signer_page = int(signer_page)
        except (TypeError, ValueError):
            return False
        if signer_page <= 0:
            # Keep page 0 annotation-aware so first-page numbers/metadata survive.
            return False
        if task.num_pages > 0 and signer_page >= task.num_pages:
            self.log(
                f"[{task.file_id}] KIE clean signer page skipped: "
                f"page={signer_page} outside page_count={task.num_pages}"
            )
            return False
        if getattr(task, "from_step1_cache", False):
            self.log(
                f"[{task.file_id}] KIE clean signer page skipped: "
                "using Step 1 OCR cache"
            )
            return False

        try:
            has_annots = direct_ocr_engine.page_has_render_annotations(
                task.input_path,
                signer_page,
            )
        except Exception as e:
            raise RuntimeError(f"KIE clean signer page annotation check failed: {e}") from e
        if not has_annots:
            self.log(
                f"[{task.file_id}] KIE clean signer page skipped: "
                f"page {signer_page + 1} has no PDF annotations/widgets"
            )
            return False

        self.log(
            f"[{task.file_id}] KIE clean signer page: OCR page "
            f"{signer_page + 1} with annots=False"
        )
        clean_result = direct_ocr_engine.ocr_one_page(
            task.input_path,
            signer_page,
            timeout=180.0,
            render_annots=False,
        )
        if not clean_result:
            raise RuntimeError("KIE clean signer page failed: OCR returned no result")
        if not (clean_result.get("lines_data") or clean_result.get("words_data")):
            raise RuntimeError("KIE clean signer page failed: clean OCR is empty")

        try:
            stats = direct_ocr_engine.replace_canonical_page_with_page_result(
                task.output_json_path,
                task.input_path,
                signer_page,
                clean_result,
                render_annots=False,
                canonical_profile="layoutlmv3_runtime",
            )
        except Exception as e:
            raise RuntimeError(f"KIE clean signer page JSON update failed: {e}") from e

        task.page_results[signer_page] = clean_result
        self.log(
            f"[{task.file_id}] KIE clean signer page ready: "
            f"page={stats.get('page_index') + 1}, "
            f"lines={stats.get('line_count')}, words={stats.get('word_count')}"
        )
        return True

    # ── event routing ───────────────────────────────────────────────

    def _on_pipeline_event(self, evt, task, payload) -> None:
        if task is None:
            self.on_event(evt, {})
            return
        with self._results_lock:
            res = self.results.get(task.file_id)
            if res is None:
                return
            if evt == EVENT_FILE_QUEUED:
                res.status = "OCR"
            elif evt == EVENT_FILE_OCR_DONE:
                res.status = "Correcting"
                res.timing["ocr"] = task.t_ocr_done - task.t_submitted
            elif evt == EVENT_CORRECTION_DONE:
                res.status = "KIE"
                res.timing["correction"] = float(payload or 0)
            elif evt == EVENT_KIE_DONE:
                res.timing["kie"] = float(payload or 0)
                res.annotation = task.kie_annotation
                res.output_pdf = task.output_pdf_path
                res.output_json = task.output_json_path
            elif evt == EVENT_FILE_COMPLETE:
                res.status = "Done"
            elif evt == EVENT_FILE_FAILED:
                res.status = "Failed"
                res.error = task.error or str(payload)
        self.on_event(evt, {"file_id": task.file_id, "task": task})
