"""3-stage streaming pipeline for batch document processing.

Stages
------
  STAGE 1: OCR         — page-level via direct_ocr_engine pool (concurrent)
  STAGE 2: Correction  — single-threaded, file-level (Proton CT2)
                         When active, blocks NEW OCR submissions so
                         correction can use 100% CPU. Already-running OCR
                         pages drain naturally (~1-2s).
  STAGE 3: KIE         — single-threaded, file-level (LightGBM | LayoutLMv3)
                         Runs concurrently with OCR (lightweight inference).

Concurrency model
-----------------
- 1 OCR feeder thread reads files → submits per-page tasks to the pool.
  Before each submission it waits on the `feeder_can_run` event so the
  correction worker can pause feeding.
- Per-file completion tracking: when the last page of file X returns,
  enqueue X to correction.
- 1 correction worker thread consumes the correction queue. On each pop,
  it CLEARS `feeder_can_run`, drains in-flight OCR (so CPU is freed for
  correction), runs correction, then SETS the event again.
- 1 KIE worker thread consumes the KIE queue, runs inference per file.
- All worker callbacks emit progress via the `events` callable so the UI
  can update without coupling to Qt directly.

Cancel
------
Setting `cancel_event` flips the workers into shutdown mode: feeder stops
submitting, in-flight pages run to completion, correction/KIE workers
abort their current step at the next checkpoint, queues are drained.
"""
from __future__ import annotations

import os
import queue
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


# ────────────────────────────────────────────────────────────────────
# Data classes
# ────────────────────────────────────────────────────────────────────

@dataclass
class FileTask:
    """A single PDF being shepherded through the pipeline."""
    file_id: str                      # caller-supplied unique id (filename, etc.)
    input_path: str
    output_pdf_path: str              # _ocr.pdf destination
    output_json_path: str             # _ocr.pdf.json destination
    source_document_path: Optional[str] = None
    num_pages: int = 0

    # File classification (set khi tạo task qua classify_pdf).
    # Nếu True → file là digital PDF (text native), correction sẽ bị skip vì
    # text đã chính xác từ nguồn — correction model train cho OCR errors,
    # áp lên text đúng có thể thay đổi sai.
    is_digital: bool = False

    # Internal state
    page_results: dict = field(default_factory=dict)   # page_idx -> page_dict
    pages_remaining: int = 0
    raw_text: Optional[str] = None
    corrected_text: Optional[str] = None
    kie_annotation: Optional[dict] = None
    error: Optional[str] = None
    selected_pages: Optional[list[int]] = None
    source_page_indices: Optional[list[int]] = None
    page_selection: dict = field(default_factory=dict)
    signature_page: Optional[int] = None
    from_step1_cache: bool = False

    # Timing
    t_submitted: float = 0.0
    t_ocr_done: float = 0.0
    t_correction_done: float = 0.0
    t_kie_done: float = 0.0


# Event types emitted to caller
EVENT_FILE_QUEUED = "file_queued"
EVENT_PAGE_DONE = "page_done"
EVENT_FILE_OCR_DONE = "file_ocr_done"
EVENT_CORRECTION_START = "correction_start"
EVENT_CORRECTION_DONE = "correction_done"
EVENT_KIE_START = "kie_start"
EVENT_KIE_DONE = "kie_done"
EVENT_FILE_COMPLETE = "file_complete"
EVENT_FILE_FAILED = "file_failed"
EVENT_PIPELINE_DONE = "pipeline_done"


# ────────────────────────────────────────────────────────────────────
# Pipeline orchestrator
# ────────────────────────────────────────────────────────────────────

class BatchPipeline:
    """Orchestrate OCR → Correction → KIE for a batch of files.

    Construction
    ------------
        pipe = BatchPipeline(
            ocr_submit=lambda path, page_idx: pool.apply_async(...),
            run_correction=lambda task: ...,   # returns corrected str
            run_kie=lambda task: ...,          # returns annotation dict
            on_event=lambda evt, task, payload: ...,
            page_timeout=120.0,
        )
        pipe.add_file(FileTask(...))
        pipe.start()
        pipe.wait()  # or pipe.cancel()
    """

    def __init__(self,
                 ocr_submit: Callable[[str, int], Any],
                 run_correction: Optional[Callable[[FileTask], str]] = None,
                 run_kie: Optional[Callable[[FileTask], dict]] = None,
                 on_event: Optional[Callable[[str, FileTask, Any], None]] = None,
                 log_cb: Optional[Callable[[str], None]] = None,
                 page_timeout: float = 120.0,
                 in_flight_drain_timeout: float = 30.0):
        self._ocr_submit = ocr_submit
        self._run_correction = run_correction
        self._run_kie = run_kie
        self._on_event = on_event or (lambda evt, task, payload: None)
        self._log = log_cb or (lambda m: None)
        self._page_timeout = page_timeout
        self._in_flight_drain_timeout = in_flight_drain_timeout

        # Synchronisation primitives
        self.feeder_can_run = threading.Event()
        self.feeder_can_run.set()           # OCR allowed by default
        self.cancel_event = threading.Event()

        # Queues
        self._files_queued: list[FileTask] = []
        self._lock = threading.Lock()
        self._correction_q: queue.Queue[FileTask] = queue.Queue()
        self._kie_q: queue.Queue[FileTask] = queue.Queue()

        # In-flight OCR tracking: list of (file_task, page_idx, async_result)
        self._in_flight: list[tuple[FileTask, int, Any]] = []
        self._in_flight_lock = threading.Lock()

        # Threads
        self._feeder_thread: Optional[threading.Thread] = None
        self._collector_thread: Optional[threading.Thread] = None
        self._correction_thread: Optional[threading.Thread] = None
        self._kie_thread: Optional[threading.Thread] = None

        # Done flags
        self._all_files_added = False
        self._feeder_done = False
        self._correction_done = False
        self._kie_done = False
        self._pipeline_done_event = threading.Event()

    # ── public API ──────────────────────────────────────────────────

    def add_file(self, task: FileTask) -> None:
        with self._lock:
            self._files_queued.append(task)

    def mark_no_more_files(self) -> None:
        """Tell the pipeline that no more files will be added. Required so the
        feeder/collector can shut down once all queued work is drained."""
        self._all_files_added = True

    def start(self) -> None:
        self._feeder_thread = threading.Thread(target=self._feeder_loop,
                                                name="pipe-feeder", daemon=True)
        self._collector_thread = threading.Thread(target=self._collector_loop,
                                                    name="pipe-collector", daemon=True)
        self._correction_thread = threading.Thread(target=self._correction_loop,
                                                    name="pipe-correction", daemon=True)
        self._kie_thread = threading.Thread(target=self._kie_loop,
                                             name="pipe-kie", daemon=True)
        self._feeder_thread.start()
        self._collector_thread.start()
        self._correction_thread.start()
        self._kie_thread.start()

    def cancel(self) -> None:
        self.cancel_event.set()
        # Unblock any thread waiting on feeder_can_run
        self.feeder_can_run.set()
        # Push sentinels into queues so workers exit promptly
        self._correction_q.put(None)
        self._kie_q.put(None)

    def wait(self, timeout: Optional[float] = None) -> bool:
        """Block until the pipeline finishes (all files done or cancelled)."""
        return self._pipeline_done_event.wait(timeout=timeout)

    # ── internal: feeder ────────────────────────────────────────────

    def _feeder_loop(self) -> None:
        """Iterate over queued files, decompose each into per-page submissions,
        and push them into the OCR pool. Pause when correction sets feeder_can_run=False."""
        try:
            self._feeder_loop_inner()
        except BaseException as e:
            tb = traceback.format_exc()
            self._log(f"[pipeline] feeder_loop crashed: {e}\n{tb}")
        finally:
            self._feeder_done = True

    def _feeder_loop_inner(self) -> None:
        next_idx = 0
        while not self.cancel_event.is_set():
            # Wait for permission to feed (pauses while correction runs)
            if not self.feeder_can_run.is_set():
                self.feeder_can_run.wait(timeout=0.5)
                continue

            with self._lock:
                if next_idx >= len(self._files_queued):
                    if self._all_files_added:
                        break
                    queued_pending = False
                else:
                    task = self._files_queued[next_idx]
                    queued_pending = True

            if not queued_pending:
                time.sleep(0.05)
                continue

            self._on_event(EVENT_FILE_QUEUED, task, None)
            task.t_submitted = time.time()

            # Discover page count if not yet known
            if task.num_pages <= 0:
                if task.source_page_indices:
                    task.num_pages = len(task.source_page_indices)
                else:
                    try:
                        import fitz
                        with fitz.open(task.input_path) as doc:
                            task.num_pages = len(doc)
                    except Exception as e:
                        task.error = f"open failed: {e}"
                        self._on_event(EVENT_FILE_FAILED, task, str(e))
                        next_idx += 1
                        continue

            task.pages_remaining = task.num_pages

            # Submit each page; respect pause flag between submissions
            for page_idx in range(task.num_pages):
                while not self.feeder_can_run.is_set():
                    if self.cancel_event.is_set():
                        return
                    self.feeder_can_run.wait(timeout=0.5)
                if self.cancel_event.is_set():
                    return

                try:
                    ar = self._ocr_submit(task.input_path, page_idx)
                except Exception as e:
                    task.error = f"submit failed: {e}"
                    self._on_event(EVENT_FILE_FAILED, task, str(e))
                    break

                with self._in_flight_lock:
                    self._in_flight.append((task, page_idx, ar))

            next_idx += 1

    # ── internal: collector ─────────────────────────────────────────

    def _collector_loop(self) -> None:
        """Poll in-flight async results, route completed pages to their file
        task, and enqueue files whose pages are all done into the correction
        queue."""
        try:
            self._collector_loop_inner()
        except BaseException as e:
            tb = traceback.format_exc()
            self._log(f"[pipeline] collector_loop crashed: {e}\n{tb}")
        finally:
            # Always send sentinel so correction loop can exit
            self._correction_q.put(None)

    def _collector_loop_inner(self) -> None:
        while not self.cancel_event.is_set():
            with self._in_flight_lock:
                in_flight_snapshot = list(self._in_flight)

            if not in_flight_snapshot:
                if self._feeder_done:
                    break
                time.sleep(0.05)
                continue

            still_pending: list[tuple[FileTask, int, Any]] = []
            files_done_now: list[FileTask] = []

            for task, page_idx, ar in in_flight_snapshot:
                if not ar.ready():
                    still_pending.append((task, page_idx, ar))
                    continue
                try:
                    _, page_result = ar.get(timeout=0.1)
                except Exception as e:
                    page_result = None
                    self._on_event(EVENT_PAGE_DONE, task,
                                   {"page_idx": page_idx, "ok": False, "error": str(e)})
                if page_result is not None:
                    task.page_results[page_idx] = page_result
                    self._on_event(EVENT_PAGE_DONE, task,
                                   {"page_idx": page_idx, "ok": True})
                else:
                    # Insert empty placeholder so page count stays consistent
                    task.page_results[page_idx] = {
                        "lines_data": [],
                        "words_data": [],
                        "render_width": 0,
                        "render_height": 0,
                    }
                task.pages_remaining -= 1
                if task.pages_remaining == 0:
                    files_done_now.append(task)

            with self._in_flight_lock:
                # Replace list — we processed the snapshot
                kept_set = {(id(t), p) for t, p, _ in still_pending}
                self._in_flight = [
                    triple for triple in self._in_flight
                    if (id(triple[0]), triple[1]) in kept_set
                ]

            for task in files_done_now:
                task.t_ocr_done = time.time()
                self._on_event(EVENT_FILE_OCR_DONE, task, None)
                self._correction_q.put(task)

            time.sleep(0.05)

    # ── internal: correction worker ─────────────────────────────────

    def _correction_loop(self) -> None:
        try:
            while not self.cancel_event.is_set():
                try:
                    task = self._correction_q.get(timeout=0.5)
                except queue.Empty:
                    continue
                if task is None:
                    break
                self._process_correction_task(task)
        except BaseException as e:
            tb = traceback.format_exc()
            self._log(f"[pipeline] correction_loop crashed: {e}\n{tb}")
        finally:
            self._correction_done = True
            # Sentinel so kie_loop can exit cleanly even if we crashed
            self._kie_q.put(None)
            # Make sure feeder isn't left paused
            self.feeder_can_run.set()

    def _process_correction_task(self, task: FileTask) -> None:
        """One correction iteration, isolated so a crash on one file doesn't
        kill the worker thread. Always emits CORRECTION_DONE + enqueues to
        KIE, OR fires FILE_FAILED — never leaves the row stuck animating."""
        try:
            self._on_event(EVENT_CORRECTION_START, task, None)
        except Exception:
            traceback.print_exc()
        t0 = time.time()

        # Pause OCR feeder, drain in-flight pages so correction gets full CPU
        self.feeder_can_run.clear()
        self._wait_in_flight_drain(self._in_flight_drain_timeout)

        try:
            if self._run_correction is not None:
                task.corrected_text = self._run_correction(task)
            else:
                task.corrected_text = task.raw_text or ""
            task.t_correction_done = time.time()
            self._on_event(EVENT_CORRECTION_DONE, task, time.time() - t0)
            self._kie_q.put(task)
        except BaseException as e:
            tb = traceback.format_exc()
            task.error = f"correction failed: {e}"
            self._log(f"[pipeline] [{task.file_id}] correction crashed: {e}\n{tb}")
            try:
                self._on_event(EVENT_FILE_FAILED, task, str(e))
            except Exception:
                traceback.print_exc()
        finally:
            # ALWAYS resume feeder, even on error
            self.feeder_can_run.set()

    def _wait_in_flight_drain(self, timeout: float) -> None:
        """Wait until in-flight OCR pages drain (or timeout)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._in_flight_lock:
                if not self._in_flight:
                    return
                # Count only pages that aren't already complete
                still_running = sum(1 for _, _, ar in self._in_flight if not ar.ready())
            if still_running == 0:
                return
            time.sleep(0.1)

    # ── internal: KIE worker ────────────────────────────────────────

    def _kie_loop(self) -> None:
        try:
            while not self.cancel_event.is_set():
                try:
                    task = self._kie_q.get(timeout=0.5)
                except queue.Empty:
                    continue
                if task is None:
                    break
                self._process_kie_task(task)
        except BaseException as e:
            # Loop-level crash — log so the user can see why the pipeline
            # died, and make sure FILE_FAILED is reported for any task we
            # were handling so its row doesn't animate forever.
            tb = traceback.format_exc()
            self._log(f"[pipeline] kie_loop crashed: {e}\n{tb}")
        finally:
            self._kie_done = True
            self._pipeline_done_event.set()
            try:
                self._on_event(EVENT_PIPELINE_DONE, None, None)
            except Exception:
                traceback.print_exc()

    def _process_kie_task(self, task: FileTask) -> None:
        """One KIE iteration, isolated so a crash on one file doesn't kill
        the worker thread. Always emits either KIE_DONE+FILE_COMPLETE or
        FILE_FAILED — never leaves the row stuck in "KIE..." state."""
        try:
            self._on_event(EVENT_KIE_START, task, None)
        except Exception:
            traceback.print_exc()
        t0 = time.time()
        try:
            if self._run_kie is not None:
                task.kie_annotation = self._run_kie(task)
            task.t_kie_done = time.time()
            self._on_event(EVENT_KIE_DONE, task, time.time() - t0)
            self._on_event(EVENT_FILE_COMPLETE, task, None)
        except BaseException as e:
            tb = traceback.format_exc()
            task.error = f"kie failed: {e}"
            self._log(f"[pipeline] [{task.file_id}] KIE crashed: {e}\n{tb}")
            try:
                self._on_event(EVENT_FILE_FAILED, task, str(e))
            except Exception:
                traceback.print_exc()
