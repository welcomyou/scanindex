"""
MainWindow — Central application window for ScanIndex.
Contains all business logic, processing pipeline, and signal wiring.
Ported from OCRApp class in ocr_app.py (3222 lines) → modular PySide6.
"""
import os
import re
import sys
import time
import threading
import hashlib
import configparser
import concurrent.futures
import json

from PySide6.QtWidgets import (
    QMainWindow, QSplitter, QStackedWidget, QWidget, QVBoxLayout,
    QFileDialog, QMessageBox, QApplication, QProgressDialog, QLabel,
    QInputDialog,
)
from PySide6.QtCore import Qt, QTimer, Slot, QThread, Signal
from PySide6.QtGui import QFont

from scanindex.ui.theme import (
    COLOR_BG, COLOR_SURFACE, COLOR_BORDER, COLOR_TEXT_SECONDARY, FONT_UI, SP,
    LOG_INFO, LOG_ERROR, LOG_DEBUG, LOG_SUCCESS,
    STATUS_KEY_MAP, STATUS_COLOR_MAP
)
from scanindex.ui.signals import AppSignals
from scanindex.ui.icons import load_all_icons
from scanindex.ui.widgets.log_panel import LogPanel
from scanindex.ui.widgets.splash_overlay import SplashOverlay
from scanindex.ui.pdf_to_word import DnDTab
from scanindex.ui.digitization import ArchiveContainer
from scanindex.ui.tabs.settings_tab import SettingsTab
from scanindex.ui.tabs.about_tab import AboutTab
from scanindex.ui.dialogs.comparison_dialog import ComparisonDialog
from scanindex.ui.dialogs.text_preview_dialog import TextPreviewDialog
from scanindex.ui.screens import (
    HomeScreen, AccuracyScreen, RepositoryScreen, ScreenContainer,
    FUNCTION_HOME, FUNCTION_PDF_TO_WORD, FUNCTION_DIGITIZATION,
    FUNCTION_REPOSITORY,
    FUNCTION_SETTINGS, FUNCTION_ABOUT, FUNCTION_ACCURACY,
)
from scanindex.ui.model_manager import (
    ModelManager,
    GROUP_CORE_OCR, GROUP_CORRECTION, GROUP_TABLE_EXTRACTION, GROUP_KIE,
    GROUP_ARCHIVE_SPLITTER, GROUP_LABELS,
)

from scanindex.infra import translations
from scanindex.infra.translations import get_text
from scanindex.infra import paths as portable_utils
from scanindex.infra.paths import get_resource_path

# Heavy modules — all None at startup; populated lazily by ModelManager loaders
# when user navigates into a screen that needs them.
direct_ocr_engine = None
file_utils = None
pypdf = None
correction_engine = None
pdf_utils = None
table_anchored_merger = None

SCREEN_AI_WORKERS_PER_FILE = 4


def per_file_pre_workers(parallel_files: int) -> int:
    """Cap preprocessing thread count so N parallel files don't oversubscribe CPU.
    Each file's pre_process_pdf would otherwise grab cpu_count-1 threads."""
    parallel_files = max(1, int(parallel_files))
    return max(1, (os.cpu_count() or 4) // parallel_files)


def _run_preprocess_and_ocr(input_path, output_path, num_pages, parallel_files,
                             update_callback, debug_mode, wait_per_page,
                             comparison_interval, source_document_path,
                             ocr_log_callback=None):
    """Shared helper: preprocess (with thread cap + rotation metadata) then OCR.

    Returns (res, msg). Caller is responsible for cleaning up the preprocessed file."""
    try:
        from scanindex.core.preprocessing import preprocessing
    except ImportError:
        sys.path.append(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "src"))
        import preprocessing

    pre_path = os.path.join(os.path.dirname(input_path), f"pre_{os.path.basename(input_path)}")

    pre_workers = per_file_pre_workers(parallel_files)
    pre_result = preprocessing.pre_process_pdf(
        input_path, pre_path,
        update_callback=update_callback,
        debug_mode=debug_mode,
        max_workers=pre_workers,
        return_metadata=True,
    )

    if isinstance(pre_result, tuple) and len(pre_result) == 3:
        pre_ok, _, pre_meta = pre_result
    else:
        pre_ok = pre_result[0] if isinstance(pre_result, tuple) else False
        pre_meta = {}

    rotations = (pre_meta or {}).get("page_rotations") or None
    target_input = pre_path if pre_ok and os.path.exists(pre_path) else input_path

    res, msg = direct_ocr_engine.process_pdf(
        target_input, output_path, num_pages=num_pages,
        update_callback=ocr_log_callback or update_callback,
        wait_per_page=wait_per_page,
        comparison_interval=comparison_interval,
        source_document_path=source_document_path,
        preprocess_rotations=rotations,
    )

    if pre_ok and os.path.exists(pre_path):
        try:
            os.remove(pre_path)
        except Exception:
            pass

    return res, msg


def _log_optional(log_callback, msg, level=LOG_INFO):
    if not log_callback:
        return
    try:
        log_callback(msg, level)
    except TypeError:
        log_callback(msg)


def _cleanup_ocr_intermediate(pdf_path, source_path=None, log_callback=None):
    """Remove OCR PDF artifacts after DOCX export succeeds.

    The OCR PDF is still the internal input for DOCX generation, but in Word
    output mode it should not remain as the user-facing result.
    """
    if not pdf_path:
        return
    try:
        abs_pdf = os.path.abspath(pdf_path)
        if source_path and abs_pdf == os.path.abspath(source_path):
            return
        if not abs_pdf.lower().endswith(".pdf"):
            return
        for path in (abs_pdf, abs_pdf + ".json"):
            if os.path.exists(path):
                os.remove(path)
                _log_optional(
                    log_callback,
                    f"Removed intermediate OCR artifact: {os.path.basename(path)}",
                    LOG_INFO,
                )
    except Exception as e:
        _log_optional(
            log_callback,
            f"Could not remove intermediate OCR artifact: {e}",
            LOG_ERROR,
        )


def _docx_output_for_ocr_pdf(pdf_path):
    base, _ = os.path.splitext(pdf_path)
    if base.lower().endswith("_ocr"):
        base = base[:-4]
    return base + "_final.docx"


class _KhoImportWorker(QThread):
    """Background thread for `Importer.import_dossier`. The model load
    (Embedder ~540 MB ONNX, Reranker if used later) and per-doc embedding
    must NOT run on the Qt main thread — otherwise the progress dialog
    (and the rest of the GUI) freezes for the whole import."""
    progress = Signal(object)        # ImportProgress
    finished_ok = Signal(object)     # ImportProgress
    failed = Signal(str)

    def __init__(self, archive_path, codes, docs):
        super().__init__()
        self._archive_path = archive_path
        self._codes = codes
        self._docs = docs
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            from scanindex.core.repository.store import ArchiveStore
            from scanindex.core.repository.indexer import HybridIndex
            from scanindex.core.repository.embedder import Embedder
            from scanindex.core.repository.importer import Importer

            res = None
            store = ArchiveStore(self._archive_path)
            with store:
                idx = HybridIndex(self._archive_path)
                idx.open()
                try:
                    embedder = Embedder()
                    importer = Importer(store, idx, embedder)
                    res = importer.import_dossier(
                        self._codes, self._docs,
                        progress_cb=lambda p: self.progress.emit(p),
                        cancel_check=lambda: self._cancel,
                        build_semantic=False,
                    )
                finally:
                    idx.close()
            if res is not None:
                self.finished_ok.emit(res)
        except Exception as e:
            import traceback
            self.failed.emit(f"{e}\n{traceback.format_exc()}")


class MainWindow(QMainWindow):
    """Main application window."""

    def __init__(self):
        super().__init__()
        self._base_window_title = get_text("app_title")
        self.setWindowTitle(self._base_window_title)
        self.resize(1200, 680)

        # --- Signals ---
        self.signals = AppSignals()
        self.signals.status_updated.connect(self._on_status_updated)
        self.signals.status_with_output.connect(self._on_status_with_output)
        self.signals.status_with_correction.connect(self._on_status_with_correction)
        self.signals.log_message.connect(self._on_log_message)
        self.signals.processing_finished.connect(self._on_processing_finished)
        self.signals.models_ready.connect(self._on_models_ready)
        self.signals.cache_updated.connect(self._on_cache_updated)

        # --- State ---
        self.dnd_files = []
        self.is_processing = False
        self.stop_event = threading.Event()
        self.pipeline = None

        self.spinner_chars = ["\u280b", "\u2819", "\u2839", "\u2838", "\u283c", "\u2834",
                              "\u2826", "\u2827", "\u2807", "\u280f"]
        self.spinner_idx = 0

        # Parallel OCR at file level. Each file uses ~4 internal ScreenAI workers.
        self.max_parallel_ocr_files = max(1, (os.cpu_count() or 4) // SCREEN_AI_WORKERS_PER_FILE)
        # DOCX export stays single-process; table models are memory-heavy.
        self.max_export_workers = 1
        self.ocr_executor = None
        self.export_executor = None
        self.correction_lock = threading.Lock()

        # Model lifecycle (lazy-load per screen)
        self.model_manager: ModelManager | None = None

        # Icons
        self.icons = load_all_icons()

        # Config
        self.config = configparser.ConfigParser()
        self.current_language = "en"

        # Load settings before building UI
        self.load_settings()

        # Wipe leftover archive temp dirs from prior runs. Each archive
        # session minted a `./temp/archive_<sid>` subdir; if the app
        # crashed mid-session those folders survive until a new run
        # cleans them up.
        try:
            from scanindex.core.digitization.session import cleanup_stale_temp_dirs
            removed = cleanup_stale_temp_dirs()
            if removed:
                print(f"[startup] Cleaned up {removed} stale archive temp dir(s)")
        except Exception as e:
            print(f"[startup] archive temp cleanup skipped: {e}")

        # --- Build UI ---
        self._build_ui()

        # --- Spinner timer (80ms) ---
        self._spinner_timer = QTimer(self)
        self._spinner_timer.setInterval(80)
        self._spinner_timer.timeout.connect(self._animate_status)
        self._spinner_timer.start()

        # --- Register model loaders (no eager load — lazy per-screen) ---
        # ModelManager tracks which groups are loaded; loaders fire on first
        # navigation into a screen that requires the group. App opens with
        # zero heavy modules in memory.
        self.model_manager = ModelManager()
        self._register_model_groups(self.model_manager)

    # ================================================================
    # UI CONSTRUCTION
    # ================================================================

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        main_layout.addWidget(self.splitter)

        # --- Left panel: stacked screens (Home + functional screens) ---
        self.stack = QStackedWidget()
        self.stack.setMinimumWidth(360)  # narrow enough that tiles can reflow to 1 col

        self.home_screen = HomeScreen()
        self.home_screen.function_selected.connect(self._navigate_to)
        self.stack.addWidget(self.home_screen)

        # Build content widgets (existing tabs reused as content)
        self.dnd_tab = DnDTab(icons=self.icons)
        self.archive_tab = ArchiveContainer(icons=self.icons)
        self.archive_tab.log_message.connect(lambda m: self.log(str(m)))
        self.archive_tab.step1_segments_ready.connect(self._arc_start_from_step1)
        self.settings_tab = SettingsTab(current_language=self.current_language)
        self.about_tab = AboutTab()
        self.accuracy_screen = AccuracyScreen()
        self.repository_screen = RepositoryScreen()
        self.repository_screen.log_message.connect(
            lambda m, lvl: self.log(m, lvl)
        )
        self.repository_screen.semantic_progress_changed.connect(
            self._on_kho_semantic_progress
        )

        # Wrap each in a ScreenContainer (header + back button)
        self.screen_containers: dict[str, ScreenContainer] = {}
        screen_specs = [
            (FUNCTION_PDF_TO_WORD, "Chuyển scan PDF → Word", self.dnd_tab),
            (FUNCTION_DIGITIZATION, "Số hóa lưu trữ", self.archive_tab),
            (FUNCTION_REPOSITORY, "Kho lưu trữ", self.repository_screen),
            (FUNCTION_ACCURACY, "Đo độ chính xác OCR", self.accuracy_screen),
            (FUNCTION_SETTINGS, "Cấu hình", self.settings_tab),
            (FUNCTION_ABOUT, "Giới thiệu", self.about_tab),
        ]
        for key, title, content in screen_specs:
            container = ScreenContainer(
                title=title, content=content,
                busy_check=lambda: self.is_processing,
                cancel_cb=self._cancel_running_task,
            )
            container.back_requested.connect(self._navigate_home)
            self.screen_containers[key] = container
            self.stack.addWidget(container)

        # Archive workflow gets a "Start over" header action so the user
        # can wipe all 3 steps + temp files in one click.
        self.screen_containers[FUNCTION_DIGITIZATION].add_header_action(
            get_text("arc_workflow_reset"),
            self._arc_reset_workflow,
            danger=True,
        )
        self._digitization_header_status = QLabel("")
        self._digitization_header_status.setVisible(False)
        self._digitization_header_status.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; font: 600 12px '{FONT_UI}';"
        )
        self.screen_containers[FUNCTION_DIGITIZATION].add_title_widget(
            self._digitization_header_status
        )
        self._kho_header_status = QLabel("")
        self._kho_header_status.setVisible(False)
        self._kho_header_status.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; font: 600 12px '{FONT_UI}';"
        )
        self.screen_containers[FUNCTION_REPOSITORY].add_header_widget(
            self._kho_header_status
        )
        self._on_kho_semantic_progress(
            self.repository_screen.current_semantic_progress()
        )
        self._background_model_loading: set[str] = set()
        self._background_model_status: dict[str, str] = {}
        self._pending_dnd_start_after_models = False
        self._digitization_splitter_warmup_started = False
        self._digitization_splitter_ready = False
        self._digitization_splitter_failed = False
        self._digitization_kie_failed = False

        # Track which function declares which model groups
        self.screen_model_groups: dict[str, list[str]] = {
            FUNCTION_HOME: [],  # idle on home
            FUNCTION_PDF_TO_WORD: [],  # loaded in background; the screen opens immediately
            FUNCTION_DIGITIZATION: [GROUP_CORE_OCR, GROUP_CORRECTION],
            FUNCTION_REPOSITORY: [],  # manages its own embedder/reranker
            FUNCTION_ACCURACY: [GROUP_CORE_OCR],
            FUNCTION_SETTINGS: [],
            FUNCTION_ABOUT: [],
        }
        self.screen_background_model_groups: dict[str, list[str]] = {
            FUNCTION_PDF_TO_WORD: [GROUP_CORE_OCR, GROUP_CORRECTION, GROUP_TABLE_EXTRACTION],
            # KIE is warmed by a custom background chain after the LightGBM
            # splitter is ready; it must not block Step 1 OCR/splitting.
            FUNCTION_DIGITIZATION: [],
        }

        self.splitter.addWidget(self.stack)

        # --- Right panel: global log ---
        self.log_panel = LogPanel()
        self.log_panel.setMinimumWidth(240)
        self.splitter.addWidget(self.log_panel)

        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setSizes([800, 300])

        # Wire AccuracyScreen's log to the global log panel
        self.accuracy_screen.log_message.connect(
            lambda msg, lvl: self.log(msg, lvl)
        )

        # --- Connect signals ---
        self._connect_signals()

        # --- Apply saved settings to UI ---
        self._apply_settings_to_ui()

        # Show home initially
        self.stack.setCurrentWidget(self.home_screen)
        self._current_function = FUNCTION_HOME

        # Splash overlay (covers the stack while screen-specific models load)
        self.splash_overlay = SplashOverlay(self.stack)
        self.stack.installEventFilter(self.splash_overlay)
        self.splash_overlay.hide()

        # Wire load-status signal: ModelManager will emit each step here
        self.signals.screen_load_status.connect(self.splash_overlay.set_status)
        self.signals.screen_load_finished.connect(self._on_screen_load_finished)
        self.signals.background_model_status.connect(self._on_background_model_status)
        self.signals.background_model_finished.connect(self._on_background_model_finished)

    def _connect_signals(self):
        # DnD tab
        self.dnd_tab.add_files_clicked.connect(self.add_files_dialog)
        self.dnd_tab.process_clicked.connect(self.start_dnd_process)
        self.dnd_tab.stop_clicked.connect(self.stop_ocr)
        self.dnd_tab.clear_clicked.connect(self.clear_list)
        self.dnd_tab.file_list.files_dropped.connect(self.drop_files)
        self.dnd_tab.chk_export.toggled.connect(self._on_pdf_word_export_toggled)

        # Archive tab
        self.archive_tab.browse_input_clicked.connect(self._arc_browse_input)
        self.archive_tab.process_clicked.connect(self._arc_start_process)
        self.archive_tab.stop_clicked.connect(self.stop_ocr)
        self.archive_tab.export_external_clicked.connect(self._arc_export_external)
        self.archive_tab.import_kho_clicked.connect(self._arc_import_to_kho)

        # Settings tab
        self.settings_tab.save_clicked.connect(self.save_settings)
        self.settings_tab.language_changed.connect(self.on_language_change)
        self.settings_tab.model_changed.connect(self.on_model_change)
        self.settings_tab.log_panel_toggled.connect(self._toggle_log_panel)
        self.settings_tab.reset_archive_requested.connect(self._reset_archive_data_from_settings)

    # ================================================================
    # NAVIGATION (QStackedWidget)
    # ================================================================

    def _effective_model_groups(self, function_id: str, groups: list[str]) -> list[str]:
        effective = list(groups or [])
        if GROUP_CORRECTION in effective and not bool(self._saved.get("correct", True)):
            effective.remove(GROUP_CORRECTION)
        if (function_id == FUNCTION_PDF_TO_WORD
                and GROUP_TABLE_EXTRACTION in effective
                and not self.dnd_tab.chk_export.isChecked()):
            effective.remove(GROUP_TABLE_EXTRACTION)
        return effective

    def _navigate_to(self, function_id: str):
        """Switch to a functional screen, reconciling model loads/releases.
        If models need loading, show a splash overlay until done."""
        if function_id == FUNCTION_HOME:
            self._navigate_home()
            return
        if (function_id != getattr(self, "_current_function", None)
                and not self._confirm_current_screen_can_leave()):
            return
        container = self.screen_containers.get(function_id)
        if container is None:
            self.log(f"Unknown screen: {function_id}", LOG_ERROR)
            return
        if function_id != FUNCTION_PDF_TO_WORD:
            self._pending_dnd_start_after_models = False

        required = self._effective_model_groups(
            function_id, self.screen_model_groups.get(function_id, []))
        background = set(self._effective_model_groups(
            function_id, self.screen_background_model_groups.get(function_id, [])))
        # Honor the global "Bật sửa chính tả" setting: if disabled, never load
        # the correction model — even on screens that would normally use it.
        if GROUP_CORRECTION in required and not bool(self._saved.get("correct", True)):
            required.remove(GROUP_CORRECTION)
        required_set = set(required)

        # Switch to the screen first so the overlay covers the right area
        self.stack.setCurrentWidget(container)
        self._current_function = function_id

        # Kho lưu trữ caches the dossier list at construction time. If the
        # user just imported a new dossier via Step 3, refresh on entry so
        # they don't see a stale empty kho.
        if function_id == FUNCTION_REPOSITORY:
            try:
                self.repository_screen.refresh_after_import()
            except Exception as e:
                self.log(f"Kho refresh failed: {e}", LOG_ERROR)

        # Passive screens (Settings, About, Kho lưu trữ — required=[]) MUST
        # NOT touch the loaded set: keep whatever a previous feature loaded
        # so the user can pop into Settings and back without paying a reload.
        # Models are only released when the user enters a different feature
        # screen whose required groups differ from what is loaded.
        if not required:
            self._start_background_model_loads(function_id)
            return

        currently = self.model_manager.loaded_groups() if self.model_manager else set()
        keep_loaded_background = currently & background
        reconcile_target = required_set | keep_loaded_background
        missing_foreground = required_set - currently
        extras_to_release = currently - reconcile_target
        if not missing_foreground and not extras_to_release:
            self._start_background_model_loads(function_id)
            return  # already in the desired foreground state

        # Show overlay BEFORE starting work so user sees feedback immediately
        title_map = {
            FUNCTION_PDF_TO_WORD: "Đang chuẩn bị: Chuyển scan PDF → Word",
            FUNCTION_DIGITIZATION: "Đang chuẩn bị: Số hóa lưu trữ",
            FUNCTION_REPOSITORY: "Đang mở Kho lưu trữ",
            FUNCTION_ACCURACY: "Đang chuẩn bị: Đo độ chính xác OCR",
            FUNCTION_SETTINGS: "Đang chuẩn bị: Cấu hình",
            FUNCTION_ABOUT: "Đang chuẩn bị: Giới thiệu",
        }
        self.splash_overlay.show_loading(
            title=title_map.get(function_id, "Đang chuẩn bị..."),
            initial_status="Khởi động tải thư viện...",
        )

        def worker():
            try:
                self.model_manager.reconcile(
                    reconcile_target,
                    log_cb=lambda m: (
                        self.signals.log_message.emit(m, LOG_INFO),
                        self.signals.screen_load_status.emit(m),
                    ),
                )
            finally:
                self.signals.screen_load_finished.emit(function_id)

        threading.Thread(target=worker, daemon=True).start()

    def _on_screen_load_finished(self, function_id: str):
        """Hide splash overlay when reconcile finishes."""
        self.splash_overlay.hide()
        self._start_background_model_loads(function_id)

    def _start_background_model_loads(self, function_id: str):
        if self.model_manager is None:
            return
        groups = self._effective_model_groups(
            function_id, self.screen_background_model_groups.get(function_id, []))
        for group in groups:
            if self.model_manager.is_loaded(group) or group in self._background_model_loading:
                continue
            self._background_model_loading.add(group)
            label = GROUP_LABELS.get(group, group)
            self.signals.background_model_status.emit(
                group, f"Loading thư viện {label}..."
            )

            def worker(g=group):
                ok = False
                try:
                    ok = self.model_manager.ensure_loaded(
                        g,
                        log_cb=lambda m: (
                            self.signals.log_message.emit(m, LOG_INFO),
                            self.signals.background_model_status.emit(g, str(m)),
                        ),
                    )
                finally:
                    self.signals.background_model_finished.emit(g, bool(ok))

            threading.Thread(
                target=worker,
                name=f"bg-model-{group}",
                daemon=True,
            ).start()

        if function_id == FUNCTION_DIGITIZATION:
            self._start_digitization_warmup_chain()

    def _start_digitization_warmup_chain(self):
        """Warm Step 1 splitter first, then KIE, without blocking Step 1."""
        if self.model_manager is None:
            return
        if self._digitization_splitter_ready:
            if not self.model_manager.is_loaded(GROUP_KIE):
                self._start_background_model_loads_for_groups([GROUP_KIE])
            self._refresh_digitization_model_status()
            return
        if self._digitization_splitter_warmup_started:
            return
        self._digitization_splitter_warmup_started = True
        self._digitization_splitter_failed = False
        self._background_model_loading.add(GROUP_ARCHIVE_SPLITTER)
        self.signals.background_model_status.emit(
            GROUP_ARCHIVE_SPLITTER,
            "Loading LightGBM splitter...",
        )
        self._refresh_digitization_model_status()

        def worker():
            ok = False
            t0 = time.monotonic()
            try:
                from scanindex.core.digitization import page_splitter as archive_page_splitter
                archive_page_splitter.load_model()
                archive_page_splitter.load_signer_model()
                self.signals.log_message.emit(
                    f"[digitization-warmup] LightGBM splitter ready in {time.monotonic() - t0:.1f}s",
                    LOG_INFO,
                )
                ok = True
            except Exception as e:
                self.signals.log_message.emit(
                    f"[digitization-warmup] LightGBM splitter warmup failed: {e}",
                    LOG_ERROR,
                )
            finally:
                self.signals.background_model_finished.emit(GROUP_ARCHIVE_SPLITTER, ok)

        threading.Thread(
            target=worker,
            name="digitization-lightgbm-warmup",
            daemon=True,
        ).start()

    def _start_background_model_loads_for_groups(self, groups: list[str]):
        if self.model_manager is None:
            return
        for group in groups:
            if self.model_manager.is_loaded(group) or group in self._background_model_loading:
                continue
            self._background_model_loading.add(group)
            label = GROUP_LABELS.get(group, group)
            self.signals.background_model_status.emit(
                group, f"Loading library {label}..."
            )

            def worker(g=group):
                ok = False
                try:
                    ok = self.model_manager.ensure_loaded(
                        g,
                        log_cb=lambda m: (
                            self.signals.log_message.emit(m, LOG_INFO),
                            self.signals.background_model_status.emit(g, str(m)),
                        ),
                    )
                finally:
                    self.signals.background_model_finished.emit(g, bool(ok))

            threading.Thread(
                target=worker,
                name=f"bg-model-{group}",
                daemon=True,
            ).start()

    def _on_pdf_word_export_toggled(self, checked: bool):
        if checked and getattr(self, "_current_function", None) == FUNCTION_PDF_TO_WORD:
            self._start_background_model_loads(FUNCTION_PDF_TO_WORD)

    def _pdf_to_word_run_groups(self) -> list[str]:
        return self._effective_model_groups(
            FUNCTION_PDF_TO_WORD,
            self.screen_background_model_groups.get(FUNCTION_PDF_TO_WORD, []),
        )

    def _pdf_to_word_models_ready_for_run(self) -> bool:
        if self.model_manager is None:
            return False
        return all(self.model_manager.is_loaded(g) for g in self._pdf_to_word_run_groups())

    def _queue_dnd_start_until_models_ready(self) -> bool:
        if self._pdf_to_word_models_ready_for_run():
            return False
        if self.model_manager is None:
            self.log("ModelManager chưa sẵn sàng; chưa thể chạy OCR.", LOG_ERROR)
            return True
        self._pending_dnd_start_after_models = True
        self._start_background_model_loads(FUNCTION_PDF_TO_WORD)
        missing = [
            GROUP_LABELS.get(g, g)
            for g in self._pdf_to_word_run_groups()
            if not self.model_manager.is_loaded(g)
        ]
        self.log(
            "Đang tải thư viện cho Chuyển scan PDF -> Word: "
            + ", ".join(missing)
            + ". Tác vụ sẽ tự chạy sau khi tải xong.",
            LOG_INFO,
        )
        return True

    def _on_background_model_status(self, group: str, text: str):
        self._background_model_status[group] = text or GROUP_LABELS.get(group, group)
        self._refresh_background_model_status()
        if group in (GROUP_ARCHIVE_SPLITTER, GROUP_KIE):
            self._refresh_digitization_model_status()

    def _on_background_model_finished(self, group: str, ok: bool):
        self._background_model_loading.discard(group)
        if ok:
            self._background_model_status[group] = (
                f"✓ {GROUP_LABELS.get(group, group)} sẵn sàng"
            )
        else:
            self._background_model_status[group] = (
                f"Không tải được {GROUP_LABELS.get(group, group)}"
            )
        if group == GROUP_ARCHIVE_SPLITTER:
            self._digitization_splitter_warmup_started = False
            self._digitization_splitter_ready = bool(ok)
            self._digitization_splitter_failed = not bool(ok)
            if ok and getattr(self, "_current_function", None) == FUNCTION_DIGITIZATION:
                self._start_background_model_loads_for_groups([GROUP_KIE])
        elif group == GROUP_KIE:
            self._digitization_kie_failed = not bool(ok)
        if group in (GROUP_ARCHIVE_SPLITTER, GROUP_KIE):
            self._refresh_digitization_model_status()
        self._refresh_background_model_status(done=ok)
        if ok:
            QTimer.singleShot(2500, lambda g=group: self._clear_background_model_status(g))
        elif self._pending_dnd_start_after_models and group in self._pdf_to_word_run_groups():
            self._pending_dnd_start_after_models = False
            self.log(
                f"Không tải được {GROUP_LABELS.get(group, group)}; chưa thể chạy Chuyển scan PDF -> Word.",
                LOG_ERROR,
            )
        if (self._pending_dnd_start_after_models
                and getattr(self, "_current_function", None) == FUNCTION_PDF_TO_WORD
                and self._pdf_to_word_models_ready_for_run()):
            self._pending_dnd_start_after_models = False
            QTimer.singleShot(0, self.start_thread)

    def _clear_background_model_status(self, group: str):
        if group in self._background_model_loading:
            return
        self._background_model_status.pop(group, None)
        self._refresh_background_model_status()
        if group in (GROUP_ARCHIVE_SPLITTER, GROUP_KIE):
            self._refresh_digitization_model_status()

    def _refresh_background_model_status(self, *, done: bool = False):
        if not self._background_model_status:
            self.setWindowTitle(getattr(self, "_base_window_title", get_text("app_title")))
            return
        text = next(reversed(self._background_model_status.values()))
        if self._background_model_loading and not done:
            ch = self.spinner_chars[self.spinner_idx % len(self.spinner_chars)]
            text = f"{ch} {text}"
        base = getattr(self, "_base_window_title", get_text("app_title"))
        self.setWindowTitle(f"{base} - {text}")

    def _refresh_digitization_model_status(self):
        label = getattr(self, "_digitization_header_status", None)
        if label is None:
            return
        parts: list[str] = []
        if GROUP_ARCHIVE_SPLITTER in self._background_model_loading:
            parts.append("LightGBM: loading")
        elif self._digitization_splitter_ready:
            parts.append("LightGBM: ready")
        elif self._digitization_splitter_failed:
            parts.append("LightGBM: failed")

        kie_loaded = (
            self.model_manager is not None
            and self.model_manager.is_loaded(GROUP_KIE)
        )
        if GROUP_KIE in self._background_model_loading:
            parts.append("LayoutLMv3: loading in background")
        elif kie_loaded:
            parts.append("LayoutLMv3: ready")
        elif self._digitization_kie_failed:
            parts.append("LayoutLMv3: failed")

        text = " | ".join(parts)
        label.setText(text)
        label.setVisible(bool(text))

    def _navigate_home(self):
        """Back to home — keep all currently loaded models (per design)."""
        if (getattr(self, "_current_function", None) != FUNCTION_HOME
                and not self._confirm_current_screen_can_leave()):
            return
        self.stack.setCurrentWidget(self.home_screen)
        self._current_function = FUNCTION_HOME

    def _confirm_current_screen_can_leave(self) -> bool:
        if getattr(self, "_current_function", None) == FUNCTION_DIGITIZATION:
            try:
                return bool(self.archive_tab.confirm_unsaved_before_leave())
            except Exception as e:
                self.log(f"Archive leave guard failed: {e}", LOG_ERROR)
                return False
        return True

    def _on_kho_semantic_progress(self, progress):
        text = ""
        try:
            if progress and progress.total_chunks > 0 and not progress.done:
                text = (
                    f"Đang lập chỉ mục ngữ nghĩa: còn "
                    f"{progress.pending_docs} file ({progress.percent}%)"
                )
        except Exception:
            text = ""
        if hasattr(self, "_kho_header_status"):
            self._kho_header_status.setText(text)
            self._kho_header_status.setVisible(bool(text))
        try:
            self.home_screen.set_function_status(FUNCTION_REPOSITORY, text)
        except Exception:
            pass

    def _cancel_running_task(self):
        """User confirmed cancel from the back-button dialog. Stops pipeline
        and cleans up obvious temp files (pre_*.pdf in known dirs)."""
        try:
            self.stop_ocr()
        except Exception:
            pass
        if self.pipeline:
            try:
                self.pipeline.stop()
            except Exception:
                pass
        # Best-effort: remove pre_*.pdf temp files in DnD inputs and Archive output
        try:
            for f in self.dnd_files:
                p = f.get("path")
                if not p:
                    continue
                pre = os.path.join(os.path.dirname(p), f"pre_{os.path.basename(p)}")
                if os.path.exists(pre):
                    try:
                        os.remove(pre)
                    except Exception:
                        pass
        except Exception:
            pass
        self.is_processing = False

    # ================================================================
    # SETTINGS
    # ================================================================

    @staticmethod
    def _normalize_kie_mode_setting(value: str | None) -> str:
        if value is None or not str(value).strip():
            return "layoutlmv3"
        mode = str(value).strip().lower().replace("-", "_")
        if mode == "layoutlmv3_visual":
            return "layoutlmv3"
        if mode == "layoutlmv3":
            return mode
        raise ValueError(f"Invalid KIE mode setting {value!r}; expected 'layoutlmv3'.")

    def load_settings(self):
        settings_path = get_resource_path("settings.ini")
        if not os.path.exists(settings_path):
            example = get_resource_path("settings.ini.example")
            if os.path.exists(example):
                try:
                    import shutil
                    shutil.copy(example, settings_path)
                    portable_utils.ensure_writable(settings_path)
                except Exception:
                    pass

        self.config.read(settings_path, encoding="utf-8")

        # Defaults — note `concurrency` now means concurrent OCR PAGES, not files
        try:
            from scanindex.core.digitization.doctype import all_display_names
            doc_type_defaults = all_display_names()
        except Exception:
            doc_type_defaults = []
        catalog_defaults = {
            "document_types": {
                "label": "Thể loại văn bản",
                "values": doc_type_defaults,
                "system": True,
            }
        }
        self._saved = {
            "w_page": "1.0", "w_int": "1.0",
            "concurrency": "4",   # default 4 page workers
            "export_workers": "1",
            "model": "", "gpu": "CPU", "verbose": True,
            "correct": True, "export": True,
            "show_log_panel": True,
            "kie_mode": None,
            "doc_types": doc_type_defaults,
            "catalogs": catalog_defaults,
        }

        if os.path.exists(settings_path):
            try:
                if "General" in self.config:
                    self.current_language = self.config["General"].get("Language", "en")
                    translations.set_lang(self.current_language)

                if "OCR" in self.config:
                    self._saved["w_page"] = self.config["OCR"].get("WaitPerPage", "1.0")
                    self._saved["w_int"] = self.config["OCR"].get("ComparisonInterval", "1.0")

                    # `MaxConcurrentOCR` now means "concurrent OCR pages" across the
                    # whole app (1..2*cpu reasonable; clamp at cpu_count)
                    max_cpu = os.cpu_count() or 4
                    try:
                        conc = int(self.config["OCR"].get("MaxConcurrentOCR", "4"))
                    except ValueError:
                        conc = 4
                    conc = max(1, min(conc, max_cpu))
                    self._saved["concurrency"] = str(conc)
                    self.max_parallel_ocr_files = conc  # legacy attribute name kept

                    self._saved["correct"] = self.config["OCR"].getboolean("CorrectEnabled", True)
                    self._saved["export"] = self.config["OCR"].getboolean("ExportEnabled", True)
                    self._saved["verbose"] = self.config["OCR"].getboolean("VerboseLog", True)
                    self._saved["show_log_panel"] = self.config["OCR"].getboolean("ShowLogPanel", True)

                if "Correction" in self.config:
                    self._saved["model"] = self.config["Correction"].get("Model", "")

                if "KIE" in self.config:
                    self._saved["kie_mode"] = self._normalize_kie_mode_setting(
                        self.config["KIE"].get("Mode")
                    )
                else:
                    raise ValueError("Missing [KIE] section in settings.ini.")

                # DOCX export workers are intentionally fixed at 1. Older
                # settings.ini files may still contain MaxExportWorkers, but
                # the UI no longer exposes or honors it because each export
                # worker can hold a heavy table model in memory.
                self._saved["export_workers"] = "1"
                self.max_export_workers = 1

                if "Catalog" in self.config:
                    raw_catalogs = self.config["Catalog"].get("CatalogsJson", "").strip()
                    if raw_catalogs:
                        try:
                            parsed = json.loads(raw_catalogs)
                            if isinstance(parsed, dict):
                                self._saved["catalogs"] = parsed
                                doc_catalog = parsed.get("document_types", {})
                                if isinstance(doc_catalog, dict):
                                    values = doc_catalog.get("values", [])
                                    if isinstance(values, list):
                                        self._saved["doc_types"] = [str(v) for v in values]
                        except Exception:
                            pass
                try:
                    from scanindex.core.digitization.doctype import all_display_names
                    if not self._saved.get("doc_types"):
                        self._saved["doc_types"] = all_display_names()
                    if not self._saved.get("catalogs"):
                        self._saved["catalogs"] = {
                            "document_types": {
                                "label": "Thể loại văn bản",
                                "values": self._saved["doc_types"],
                                "system": True,
                            }
                        }
                except Exception:
                    pass

                self.log(get_text("msg_settings_loaded", settings_path))
            except Exception as e:
                self.log(f"Failed to load settings: {e}", LOG_ERROR)

    def _apply_settings_to_ui(self):
        s = self._saved
        model_name = s["model"]
        if correction_engine and not model_name:
            model_name = correction_engine.MODEL_PROTON_CT2_OPT
        if correction_engine:
            self.settings_tab.set_model_choices([correction_engine.MODEL_PROTON_CT2_OPT])

        self.settings_tab.set_values(
            wait_page=s["w_page"],
            compare_int=s["w_int"],
            concurrency=s["concurrency"],
            export_workers=s["export_workers"],
            model=model_name,
            verbose=s["verbose"],
            correct=s["correct"],
            export=s["export"],
            show_log_panel=s["show_log_panel"],
            kie_mode=self._normalize_kie_mode_setting(s.get("kie_mode")),
            doc_types=s.get("doc_types", []),
            catalogs=s.get("catalogs"),
        )

        self.dnd_tab.chk_export.setChecked(s["export"])
        self.log_panel.set_verbose(s["verbose"])
        self._toggle_log_panel(s["show_log_panel"])

    def save_settings(self):
        settings_path = get_resource_path("settings.ini")
        vals = self.settings_tab.get_values()

        # Validate OCR pages concurrency (1..cpu_count)
        max_cpu = os.cpu_count() or 4
        try:
            val_ocr = int(vals["concurrency"])
            val_ocr = max(1, min(val_ocr, max_cpu))
        except ValueError:
            val_ocr = 4
        self.max_parallel_ocr_files = val_ocr

        # DOCX export is fixed to one worker; the old user-facing setting was
        # removed to avoid over-allocating memory-heavy table-export workers.
        val_exp = 1
        self.max_export_workers = val_exp

        self.config["General"] = {"Language": self.current_language}
        self.config["OCR"] = {
            "WaitPerPage": vals["wait_page"],
            "ComparisonInterval": vals["compare_int"],
            "MaxConcurrentOCR": str(val_ocr),
            "CorrectEnabled": str(bool(vals.get("correct", True))),
            "ExportEnabled": str(self.dnd_tab.chk_export.isChecked()),
            "VerboseLog": str(vals["verbose"]),
            "ShowLogPanel": str(vals["show_log_panel"]),
        }
        self._saved["correct"] = bool(vals.get("correct", True))
        self.config["Correction"] = {
            "Model": vals["model"],
            "Acceleration": "CPU",
        }
        self.config["TableExtraction"] = {
            "engine": "hybrid",
            "device": "cpu",
            "MaxExportWorkers": str(val_exp),
        }
        old_kie_mode = self._saved.get("kie_mode")
        new_kie_mode = self._normalize_kie_mode_setting(vals.get("kie_mode"))
        self.config["KIE"] = {
            "Mode": new_kie_mode,
        }
        try:
            from scanindex.core.digitization.doctype import serialize_display_names
            catalogs = vals.get("catalogs", {})
            self.config["Catalog"] = {
                "DocumentTypesJson": serialize_display_names(vals.get("doc_types", [])),
                "CatalogsJson": json.dumps(catalogs, ensure_ascii=False),
            }
            self._saved["doc_types"] = vals.get("doc_types", [])
            self._saved["catalogs"] = catalogs
        except Exception:
            pass
        self._saved["kie_mode"] = new_kie_mode
        if old_kie_mode != new_kie_mode:
            try:
                from scanindex.core.kie import engine as kie_engine
                kie_engine.release_kie(None)
            except Exception:
                pass

        if correction_engine:
            model = vals["model"] or correction_engine.MODEL_PROTON_CT2_OPT
            correction_engine.set_model_name(model)
        self.log_panel.set_verbose(vals["verbose"])

        portable_utils.ensure_writable(settings_path)
        try:
            with open(settings_path, "w", encoding="utf-8") as f:
                self.config.write(f)
            self.log(get_text("msg_settings_saved", settings_path))
        except Exception as e:
            self.log(f"Failed to save settings: {e}", LOG_ERROR)

    # ================================================================
    # LOG PANEL TOGGLE
    # ================================================================

    def _toggle_log_panel(self, visible: bool):
        self.log_panel.setVisible(visible)

    def _reset_archive_data_from_settings(self):
        worker = getattr(self, "_arc_kho_worker", None)
        if worker is not None and worker.isRunning():
            QMessageBox.warning(
                self, "Reset dữ liệu kho",
                "Kho đang có tác vụ chuyển dữ liệu. Hãy chờ hoàn tất rồi thử lại.",
            )
            return

        archive_path = getattr(self.repository_screen, "_archive_path", None)
        confirm_word = "XOA KHO"
        ask = QMessageBox.warning(
            self,
            "Reset dữ liệu kho?",
            "Thao tác này sẽ xóa toàn bộ dữ liệu trong Kho lưu trữ nội bộ:\n"
            "- Hồ sơ và metadata\n"
            "- PDF đã chuyển vào kho\n"
            "- Chỉ mục tìm kiếm nhanh và ngữ nghĩa\n\n"
            f"Kho hiện tại: {archive_path}\n\n"
            "Không thể hoàn tác.",
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Ok,
            QMessageBox.StandardButton.Cancel,
        )
        if ask != QMessageBox.StandardButton.Ok:
            return

        typed, ok = QInputDialog.getText(
            self,
            "Xác nhận reset dữ liệu kho",
            f"Gõ chính xác '{confirm_word}' để xác nhận xóa toàn bộ dữ liệu kho:",
        )
        if not ok:
            return
        if typed.strip() != confirm_word:
            QMessageBox.information(
                self, "Reset dữ liệu kho",
                "Chữ xác nhận không đúng. Dữ liệu kho chưa bị xóa.",
            )
            return

        try:
            reset_path = self.repository_screen.reset_archive_data()
        except Exception as e:
            self.log(f"Reset dữ liệu kho thất bại: {e}", LOG_ERROR)
            QMessageBox.critical(
                self, "Reset dữ liệu kho",
                f"Không reset được dữ liệu kho:\n{e}",
            )
            return

        self._on_kho_semantic_progress(
            self.repository_screen.current_semantic_progress()
        )
        self.log(f"Đã reset dữ liệu kho: {reset_path}", LOG_SUCCESS)
        QMessageBox.information(
            self,
            "Reset dữ liệu kho",
            "Đã xóa dữ liệu kho và tạo lại kho rỗng.",
        )

    # ================================================================
    # LANGUAGE
    # ================================================================

    def on_language_change(self, lang_code: str):
        if lang_code == self.current_language:
            return
        self.current_language = lang_code
        translations.set_lang(lang_code)

        # Window title (per-screen titles are baked into ScreenContainer headers)
        self._base_window_title = get_text("app_title")
        self._refresh_background_model_status()

        # Update tab contents
        self.dnd_tab.update_texts()
        self.archive_tab.update_texts()
        self.settings_tab.update_texts()
        self.about_tab.update_texts()

        # Refresh file lists to update status translations
        self.refresh_file_list()

    # ================================================================
    # LOGGING
    # ================================================================

    def log(self, msg: str, level: str = LOG_INFO):
        """Thread-safe log. Can be called from any thread."""
        self.signals.log_message.emit(msg, level)

    def gui_log_callback(self, msg, level=LOG_INFO):
        self.log(msg, level)

    def log_callback(self, msg, level=LOG_INFO):
        self.signals.log_message.emit(msg, level)

    @Slot(str, str)
    def _on_log_message(self, msg, level):
        if hasattr(self, 'log_panel'):
            self.log_panel.append_log(msg, level)

    # ================================================================
    # MODEL INIT
    # ================================================================

    def _register_model_groups(self, mm: ModelManager):
        """Register loader+releaser callbacks for each model group.

        Loaders import their heavy modules on first invocation, so app startup
        does not pay the cost of any model the user does not actually open."""

        def load_core_ocr(log_cb):
            global direct_ocr_engine, file_utils, pypdf, pdf_utils
            if direct_ocr_engine is None:
                log_cb("Tải OCR engine (ScreenAI)...")
                from scanindex.core.ocr import direct_engine as m_ocr
                direct_ocr_engine = m_ocr
            if file_utils is None:
                from scanindex.infra import file_utils as m_file
                file_utils = m_file
            if pypdf is None:
                import pypdf as m_pypdf
                pypdf = m_pypdf
            if pdf_utils is None:
                from scanindex.core.pdf import utils as m_pdf
                pdf_utils = m_pdf
            # Spawn the multiprocessing pool so DLL workers fork now,
            # avoiding first-OCR latency on the user's first run.
            log_cb("Khởi động OCR pool (ScreenAI DLL workers)...")
            try:
                direct_ocr_engine._get_pool()
            except Exception as e:
                log_cb(f"OCR pool init failed (will retry on first OCR): {e}")

        def release_core_ocr(log_cb):
            if direct_ocr_engine is None:
                return
            try:
                direct_ocr_engine.shutdown_pool()
            except Exception as e:
                log_cb(f"OCR pool shutdown error: {e}")

        def load_correction(log_cb):
            global correction_engine
            if correction_engine is None:
                log_cb("Tải module sửa chính tả (Proton CT2)...")
                try:
                    from scanindex.core.correction import engine as m_corr
                    correction_engine = m_corr
                except Exception as e:
                    log_cb(f"correction_engine import failed: {e}")
                    return
            try:
                correction_engine.init_client(
                    log_callback=lambda *a: log_cb(" ".join(str(x) for x in a))
                )
            except Exception as e:
                log_cb(f"Correction init error: {e}")
                return
            # Notify UI so settings combo populates with available models.
            self.signals.models_ready.emit()

        def release_correction(log_cb):
            if correction_engine is None:
                return
            try:
                # Best-effort: drop module-level model refs if the engine exposes them
                for attr in ("_pt_tokenizer", "_pt_translator", "_pt_model"):
                    if hasattr(correction_engine, attr):
                        setattr(correction_engine, attr, None)
            except Exception:
                pass

        def load_table_extraction(log_cb):
            global table_anchored_merger
            log_cb("Tải table_anchored_merger (GMFT + Docling v1 ONNX)...")
            try:
                from scanindex.core.tables import docx_exporter as m_table
                table_anchored_merger = m_table
            except Exception as e:
                log_cb(f"table_anchored_merger import failed: {e}")
            log_cb("Dùng pipeline chính: DocLayout bbox + GMFT-ONNX + Docling v1 step-cache ONNX")

        def release_table_extraction(log_cb):
            # Tear down the export ProcessPool (workers hold GMFT in their
            # own processes). New runs will respawn it lazily.
            try:
                if self.export_executor is not None:
                    self.export_executor.shutdown(wait=False)
                    self.export_executor = None
            except Exception as e:
                log_cb(f"Export pool shutdown error: {e}")

        def load_kie(log_cb):
            mode = self._normalize_kie_mode_setting(self._saved.get("kie_mode"))
            self._saved["kie_mode"] = mode
            log_cb(f"Tải KIE inference (mode={mode})...")
            from scanindex.core.kie import engine as kie_engine
            if not kie_engine.warmup_kie(mode, log_cb=log_cb):
                raise RuntimeError(f"KIE warmup failed for mode={mode}")

        def release_kie(log_cb):
            try:
                from scanindex.core.kie import engine as kie_engine
                kie_engine.release_kie(None)
            except Exception:
                pass

        mm.register(GROUP_CORE_OCR, load_core_ocr, release_core_ocr)
        mm.register(GROUP_CORRECTION, load_correction, release_correction)
        mm.register(GROUP_TABLE_EXTRACTION, load_table_extraction, release_table_extraction)
        mm.register(GROUP_KIE, load_kie, release_kie)

    @Slot()
    def _on_models_ready(self):
        self.log(get_text("msg_info_models_ready"))
        if correction_engine:
            self.settings_tab.set_model_choices([correction_engine.MODEL_PROTON_CT2_OPT])
            self._apply_settings_to_ui()

    def on_model_change(self, choice):
        def _change():
            if correction_engine:
                correction_engine.set_model_name(choice, log_callback=self.gui_log_callback)
                self.gui_log_callback(f"Model changed to: {choice}")
        threading.Thread(target=_change, daemon=True).start()

    # ================================================================
    # FILE MANAGEMENT
    # ================================================================

    def add_files_dialog(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Select PDF Files", "", "PDF Files (*.pdf)"
        )
        count = 0
        existing = {item["path"] for item in self.dnd_files}
        for f in files:
            if f not in existing:
                self.dnd_files.append({"path": f, "status": "Pending", "output_path": None})
                existing.add(f)
                count += 1
        if count > 0:
            self.refresh_file_list()
            self.log(get_text("msg_added_files", count))

    def drop_files(self, file_paths: list):
        count = 0
        existing = {item["path"] for item in self.dnd_files}
        for f in file_paths:
            if f not in existing:
                self.dnd_files.append({"path": f, "status": "Pending", "output_path": None})
                existing.add(f)
                count += 1
        if count > 0:
            self.refresh_file_list()
            self.log(get_text("msg_added_drop", count))

    def remove_file(self, idx):
        if 0 <= idx < len(self.dnd_files):
            status = self.dnd_files[idx]["status"]
            if status in ["Processing", "OCR Processing", "Correcting..."]:
                return
            del self.dnd_files[idx]
            self.refresh_file_list()

    def clear_list(self):
        if self.is_processing:
            return
        self.dnd_files = []
        self.refresh_file_list()

    def refresh_file_list(self):
        self.dnd_tab.file_list.populate(self.dnd_files)
        # Connect item signals
        for i in range(len(self.dnd_files)):
            w = self.dnd_tab.file_list.get_widget(i)
            if w:
                w.rerun_clicked.connect(self.re_run_file)
                w.view_raw_clicked.connect(
                    lambda idx: self.show_text_preview(idx, "raw", "dnd"))
                w.view_corrected_clicked.connect(
                    lambda idx: self.show_corrected_content(idx, "dnd"))
                w.view_metadata_clicked.connect(
                    lambda idx: self.show_metadata_dialog(idx, "dnd"))
                w.remove_clicked.connect(self.remove_file)

    # ================================================================
    # STATUS UPDATES (from worker threads via signals)
    # ================================================================

    def update_item_status(self, list_type, idx, status, output_path=None, corrected_text=None):
        """Thread-safe status update — emits signal."""
        if corrected_text:
            self.signals.status_with_correction.emit(list_type, idx, status,
                                                      output_path or "", corrected_text)
        elif output_path:
            self.signals.status_with_output.emit(list_type, idx, status, output_path)
        else:
            self.signals.status_updated.emit(list_type, idx, status)

    @Slot(str, int, str)
    def _on_status_updated(self, list_type, idx, status):
        if list_type == "archive":
            self._arc_update_doc(idx, status)
            return
        if 0 <= idx < len(self.dnd_files):
            self.dnd_files[idx]["status"] = status
            w = self.dnd_tab.file_list.get_widget(idx)
            if w:
                w.update_status(status)

    @Slot(str, int, str, str)
    def _on_status_with_output(self, list_type, idx, status, output_path):
        if list_type == "archive":
            self._arc_update_doc(idx, status, output_path)
            return
        if 0 <= idx < len(self.dnd_files):
            self.dnd_files[idx]["status"] = status
            if output_path:
                self.dnd_files[idx]["output_path"] = output_path
            w = self.dnd_tab.file_list.get_widget(idx)
            if w:
                w.update_item(self.dnd_files[idx])

    @Slot(str, int, str, str, str)
    def _on_status_with_correction(self, list_type, idx, status, output_path, corrected_text):
        if list_type == "archive":
            self._arc_update_doc(idx, status, output_path)
            return
        if 0 <= idx < len(self.dnd_files):
            self.dnd_files[idx]["status"] = status
            if output_path:
                self.dnd_files[idx]["output_path"] = output_path
            if corrected_text:
                self.dnd_files[idx]["corrected_text"] = corrected_text
            w = self.dnd_tab.file_list.get_widget(idx)
            if w:
                w.update_item(self.dnd_files[idx])

    def _arc_update_doc(self, idx, status, output_path=None):
        """Update archive document status and refresh archive tab UI.

        Per-row status drives the file list visual: greyed/spinner/bright/red.
        Progress bar removed in favour of inline row-level state."""
        docs = getattr(self, '_arc_documents', [])
        if 0 <= idx < len(docs):
            docs[idx]["status"] = status
            if output_path:
                docs[idx]["output_path"] = output_path
            # Push the new state to the archive tab's list row
            self.archive_tab.update_doc_status(idx, status)
            # If all done/failed, finalize
            terminal = ("Done", "Corrected", "OCR Done", "Failed", "Done (Export Failed)")
            done = sum(1 for d in docs if d["status"] in terminal)
            if done == len(docs):
                self.archive_tab.set_processing_state(False)
                self.log(f"Archive: All {len(docs)} files processed", LOG_SUCCESS)

    @Slot(str, int, str, str)
    def _on_cache_updated(self, list_type, idx, key, value):
        if list_type == "archive":
            docs = getattr(self, '_arc_documents', [])
            # Special keys driven from the worker thread:
            if key == "_progress":
                try:
                    cur, total = (int(p) for p in (value or "0/1").split("/"))
                except Exception:
                    cur, total = 0, max(1, len(docs))
                self.archive_tab.set_progress(cur, total)
                return
            if key == "_refresh":
                # File `idx` completed — if the user is currently viewing it,
                # reload the viewer (now showing the OCR output instead of input).
                # `_current_doc_idx` lives on Step 2 (`archive_tab.archive_tab`
                # aliases it via a legacy property); the container itself does
                # not expose the attribute.
                step2 = getattr(self.archive_tab, "archive_tab",
                                self.archive_tab)
                cur = getattr(step2, "_current_doc_idx", -1)
                if cur == idx:
                    self.archive_tab.refresh_current_doc()
                return
            if 0 <= idx < len(docs):
                docs[idx][key] = value
            return
        if 0 <= idx < len(self.dnd_files):
            self.dnd_files[idx][key] = value

    def update_file_cache(self, idx, key, value, list_type="dnd"):
        self.signals.cache_updated.emit(list_type, idx, key, str(value) if value else "")

    # ================================================================
    # PREVIEW / COMPARISON
    # ================================================================

    def show_text_preview(self, idx, mode="raw", list_type="dnd"):
        try:
            if idx < 0 or idx >= len(self.dnd_files):
                return
            item = self.dnd_files[idx]
            pdf_path = item.get("output_path")
            if not pdf_path or not os.path.exists(pdf_path):
                return

            content = item.get("original_text", "")
            if not content:
                with open(pdf_path, "rb") as f_in:
                    reader = pypdf.PdfReader(f_in)
                    for page in reader.pages:
                        content += page.extract_text(extraction_mode="layout") + "\n\n"
                self.dnd_files[idx]["original_text"] = content

            title = get_text("win_raw_text", os.path.basename(pdf_path))
            dlg = TextPreviewDialog(title, content, parent=self)
            dlg.show()
        except Exception as e:
            QMessageBox.critical(self, get_text("msg_error"),
                                 f"Failed to read PDF text:\n{str(e)}")

    def show_corrected_content(self, idx, list_type="dnd"):
        if not (0 <= idx < len(self.dnd_files)):
            return
        item = self.dnd_files[idx]
        corrected = item.get("corrected_text") or ""
        fname = os.path.basename(item["path"])

        original = item.get("original_text", "")
        if not original and item.get("output_path") and os.path.exists(item["output_path"]):
            try:
                with open(item["output_path"], "rb") as f_in:
                    reader = pypdf.PdfReader(f_in)
                    for p in reader.pages:
                        original += p.extract_text(extraction_mode="layout") + "\n\n"
                self.dnd_files[idx]["original_text"] = original
            except Exception:
                original = "(Could not read original)"

        if not corrected:
            corrected = original

        dlg = ComparisonDialog(fname, original, corrected, file_idx=idx, parent=self)
        dlg.reprocess_requested.connect(
            lambda fidx, text: self._save_corrected_from_comparison(fidx, text, dlg))
        dlg.show()

    def show_metadata_dialog(self, idx, list_type="dnd"):
        if not (0 <= idx < len(self.dnd_files)):
            return
        item = self.dnd_files[idx]
        metadata = item.get("metadata")
        if not metadata:
            return
        fname = os.path.basename(item["path"])
        from scanindex.ui.dialogs.metadata_dialog import MetadataDialog
        dlg = MetadataDialog(metadata, filename=fname, parent=self)
        dlg.show()

    def _save_corrected_from_comparison(self, idx, corrected_text, dialog):
        try:
            if idx is None or idx < 0 or idx >= len(self.dnd_files):
                QMessageBox.critical(self, get_text("msg_error"), "Invalid file index")
                return

            item = self.dnd_files[idx]
            pdf_path = item.get("output_path")
            if not pdf_path or not os.path.exists(pdf_path):
                QMessageBox.critical(self, get_text("msg_error"), "Source PDF not found")
                return

            original_text = item.get("original_text", "")
            if not original_text:
                with open(pdf_path, "rb") as f_in:
                    reader = pypdf.PdfReader(f_in)
                    for page in reader.pages:
                        original_text += page.extract_text(extraction_mode="layout") + "\n\n"

            src_pdf = pdf_path
            output_path = pdf_path
            baseline_text = original_text

            self.log(f"Saving corrected PDF: {output_path}")
            success, msg = pdf_utils.create_corrected_pdf(
                src_pdf, output_path, baseline_text, corrected_text,
                log_callback=self.gui_log_callback
            )

            if success:
                QMessageBox.information(self, get_text("msg_save_success"),
                                        get_text("msg_save_content", output_path, msg))
                self.log(f"Corrected PDF saved: {output_path}")
                self.dnd_files[idx]["corrected_text"] = corrected_text
                self.dnd_files[idx]["output_path"] = output_path
                self.dnd_files[idx]["status"] = "Corrected"
                self.refresh_file_list()

                cached_orig = self.dnd_files[idx].get("original_text", "")
                dialog.refresh_state(cached_orig, corrected_text)

                # Export if enabled
                if self.dnd_tab.chk_export.isChecked():
                    self._export_from_comparison(idx, output_path)
            else:
                QMessageBox.critical(self, get_text("msg_error"), msg)
                self.log(f"Failed to save: {msg}", LOG_ERROR)
        except Exception as e:
            QMessageBox.critical(self, get_text("msg_error"),
                                 f"Failed to save corrected PDF:\n{str(e)}")
            self.log(f"Error saving PDF: {e}", LOG_ERROR)

    def _export_from_comparison(self, idx, pdf_src):
        if not os.path.exists(pdf_src):
            return
        item = self.dnd_files[idx]
        input_pdf = item["path"]
        base_in = os.path.splitext(input_pdf)[0]
        final_docx = base_in + "_final.docx"
        self.log(f"Queueing Export: {final_docx}")

        if self.export_executor is None:
            from scanindex.core.tables.export_worker import init_export_worker
            self.export_executor = concurrent.futures.ProcessPoolExecutor(
                max_workers=self.max_export_workers,
                initializer=init_export_worker)

        from scanindex.core.tables.export_worker import run_export_task
        future = self.export_executor.submit(
            run_export_task, pdf_src, final_docx,
            metadata=item.get("metadata"))

        def done_cb(fut):
            try:
                if not fut.cancelled():
                    res = fut.result()
                    if res["success"]:
                        self.gui_log_callback(f"Export Success: {res['path']}")
                        _cleanup_ocr_intermediate(pdf_src, input_pdf, self.gui_log_callback)
                        self.signals.status_with_output.emit(
                            "dnd", idx, "Done", final_docx)
                    else:
                        self.gui_log_callback(f"Export Failed: {res['msg']}")
            except Exception as e:
                self.gui_log_callback(f"Export Error: {e}")

        future.add_done_callback(done_cb)

    # ================================================================
    # ARCHIVE TAB
    # ================================================================

    def _arc_reset_workflow(self):
        """Header "↻ Bắt đầu lại" button — wipe every step's UI state and
        the per-session temp dir after confirming with the user."""
        confirm = QMessageBox(self)
        confirm.setWindowTitle(get_text("arc_workflow_reset_title"))
        confirm.setIcon(QMessageBox.Icon.Question)
        confirm.setText(get_text("arc_workflow_reset_title"))
        confirm.setInformativeText(get_text("arc_workflow_reset_confirm"))
        confirm.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        confirm.setDefaultButton(QMessageBox.StandardButton.No)
        if confirm.exec() != QMessageBox.StandardButton.Yes:
            return

        runner = getattr(self, "_archive_runner", None)
        if runner is not None:
            try:
                runner.cancel()
            except Exception:
                pass
            self._archive_runner = None
        self._arc_documents = []
        self._arc_completed_count = 0
        self.is_processing = False
        try:
            self.archive_tab.reset_workflow()
        except Exception as e:
            self.log(f"Archive: reset failed: {e}", LOG_ERROR)
            return
        self.log("Archive: workflow reset (temp wiped)", LOG_INFO)

    def _arc_browse_input(self):
        # If Step 2 already has a populated document list, picking a new
        # folder will replace it — confirm + cancel any in-flight runner so
        # the user doesn't silently lose Step 1 results or interrupt KIE.
        if not self.archive_tab.confirm_unsaved_before_leave():
            return
        existing_docs = self.archive_tab.get_documents()
        if existing_docs:
            ok = QMessageBox.question(
                self, "Xác nhận",
                "Bạn đã có danh sách file trong Bước 2. "
                "Chọn thư mục mới sẽ huỷ tiến trình hiện tại — tiếp tục?",
            )
            if ok != QMessageBox.StandardButton.Yes:
                return
            prev = getattr(self, "_archive_runner", None)
            if prev is not None:
                try:
                    prev.cancel()
                except Exception:
                    pass
            self.archive_tab.set_documents([])
        d = QFileDialog.getExistingDirectory(self, "Chọn thư mục chứa PDF")
        if not d:
            return

        # After picking the folder we ALSO need dossier identity so the
        # output filenames + Kho upsert key are well-defined. Same modal
        # dialog as Step 1 — pre-fill with whatever the session already has.
        from scanindex.ui.dialogs.archive_session_dialog import DossierInfoDialog
        session = self.archive_tab.session
        dlg = DossierInfoDialog(
            initial=session.identity,
            seed_for_unstructured=session.session_id,
            parent=self,
        )
        if not dlg.exec():
            self.log("Archive: folder pick cancelled (no identity)", LOG_INFO)
            return
        session.identity = dlg.result_codes()

        # Switch Step 2 back to "from folder" mode in case it was on step1
        try:
            self.archive_tab._step2.set_source_mode("folder")
        except Exception:
            pass
        self.archive_tab.set_input_folder(d)

    def _arc_start_process(self):
        """Start the new 3-stage archive pipeline (page-level OCR + correction
        + KIE) on every PDF in the input folder. KIE intermediates go to the
        per-session temp dir (`<temp>/_step2_kie/`); the user only picks an
        external folder later via Step 3's "Xuất hồ sơ nén"."""
        in_dir = self.archive_tab.get_input_folder()
        if not in_dir:
            QMessageBox.warning(self, "Warning", "Please select an input folder.")
            return
        out_dir = self.archive_tab.session.step2_kie_dir()
        self.archive_tab.set_output_folder(out_dir)

        try:
            pdf_files = []
            for root, _, files in os.walk(in_dir):
                for name in files:
                    if name.lower().endswith(".pdf"):
                        pdf_files.append(os.path.join(root, name))
            pdf_files = sorted(pdf_files)
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
            return
        if not pdf_files:
            QMessageBox.information(self, "Info", "No PDF files found.")
            return

        self.log(
            f"Archive: Found {len(pdf_files)} PDF file(s) recursively in {in_dir}"
        )

        # Mirror to UI documents list
        self._arc_documents = [
            {"pdf_path": f, "path": f, "output_path": None, "ocr_path": None,
             "json_path": None, "metadata": {}, "zones": {}, "status": "Pending"}
            for f in pdf_files
        ]
        self.archive_tab.set_documents(self._arc_documents)
        step2 = getattr(self.archive_tab, "_step2", None) \
            or getattr(self.archive_tab, "archive_tab", None)
        if step2 is not None and getattr(step2, "pdf_viewer", None) is not None:
            try:
                step2.pdf_viewer.clear()
                step2._current_doc_idx = -1
            except Exception:
                pass
        self.archive_tab.set_processing_state(True)
        self.archive_tab.set_progress(0, len(self._arc_documents))

        # Resolve KIE mode from settings
        kie_mode = self._normalize_kie_mode_setting(self._saved.get("kie_mode"))
        self._saved["kie_mode"] = kie_mode
        self.is_processing = True

        from scanindex.core.digitization.runner import ArchiveRunner
        from scanindex.core.pipeline.batch_pipeline import (
            EVENT_PAGE_DONE, EVENT_FILE_OCR_DONE,
            EVENT_CORRECTION_START, EVENT_CORRECTION_DONE,
            EVENT_KIE_START, EVENT_KIE_DONE,
            EVENT_FILE_COMPLETE, EVENT_FILE_FAILED, EVENT_PIPELINE_DONE,
        )

        # Counter for completed files (drives progress bar)
        self._arc_completed_count = 0

        # State machine: a row only animates while work is *actually* running
        # on it. Between stages (e.g. OCR done but correction queue is busy
        # with another file), status reverts to "Pending" so the row goes
        # gray. Stage starts (PAGE_DONE / CORRECTION_START / KIE_START) flip
        # status back to active. Done / Failed are terminal.
        def on_event(evt, payload):
            file_id = payload.get("file_id") if isinstance(payload, dict) else None
            if file_id is not None:
                for i, doc in enumerate(self._arc_documents):
                    if os.path.basename(doc["path"]) != file_id:
                        continue
                    changed = False
                    if evt == EVENT_PAGE_DONE:
                        # First completed page = at least one of this file's
                        # pages was actually running on a worker. Light up the
                        # row. Subsequent PAGE_DONE events are no-ops here.
                        pd = doc.get("_pages_done", 0) + 1
                        doc["_pages_done"] = pd
                        if pd == 1 and doc.get("status") == "Pending":
                            doc["status"] = "OCR..."
                            changed = True
                    elif evt == EVENT_FILE_OCR_DONE:
                        # All pages done; file is now waiting in correction
                        # queue. Queue wait != active → revert to Pending.
                        doc["status"] = "Pending"
                        changed = True
                    elif evt == EVENT_CORRECTION_START:
                        doc["status"] = "Correcting..."
                        changed = True
                    elif evt == EVENT_CORRECTION_DONE:
                        # Waiting in KIE queue
                        doc["status"] = "Pending"
                        changed = True
                    elif evt == EVENT_KIE_START:
                        doc["status"] = "KIE..."
                        changed = True
                    elif evt == EVENT_KIE_DONE:
                        task = payload.get("task")
                        if task is not None:
                            doc["output_path"] = task.output_pdf_path
                            doc["json_path"] = task.output_json_path
                            doc["selected_pages"] = getattr(task, "selected_pages", None)
                            doc["signature_page"] = getattr(task, "signature_page", None)
                            doc["page_selection"] = getattr(task, "page_selection", {})
                        # Brief lull before FILE_COMPLETE — gray
                        doc["status"] = "Pending"
                        changed = True
                    elif evt == EVENT_FILE_COMPLETE:
                        doc["status"] = "Done"
                        self._arc_completed_count += 1
                        self.signals.cache_updated.emit(
                            "archive", i, "_progress",
                            f"{self._arc_completed_count}/{len(self._arc_documents)}",
                        )
                        # If the user is currently viewing this file, swap
                        # the viewer from the input PDF to the OCR output.
                        self.signals.cache_updated.emit(
                            "archive", i, "_refresh", "1",
                        )
                        changed = True
                    elif evt == EVENT_FILE_FAILED:
                        doc["status"] = "Failed"
                        self._arc_completed_count += 1
                        changed = True
                    if changed:
                        self.signals.status_updated.emit("archive", i, doc["status"])
                    break
            if evt == EVENT_PIPELINE_DONE:
                self.is_processing = False
                self.signals.processing_finished.emit()

        self._archive_runner = ArchiveRunner(
            output_dir=out_dir,
            input_dir=in_dir,
            kie_mode=kie_mode,
            on_event=on_event,
            log_cb=lambda m: self.signals.log_message.emit(str(m), LOG_INFO),
            write_excel_on_done=False,    # Excel is exported manually via the button
            enable_correction=bool(self._saved.get("correct", True)),
        )
        self._archive_runner.start()
        self.log(f"Archive: pipeline started on {len(pdf_files)} files (KIE={kie_mode})")

    def _arc_start_from_step1(self, documents):
        """Pipeline kicked off from Step 1's "Chuyển bước 2" handoff. The
        document list is already populated in the GUI; here we just build
        FileSpecs (with pre-OCR cache from `session.ocr_cache`) and start
        the runner. Cancels any in-flight previous run first."""
        from scanindex.core.digitization.runner import ArchiveRunner, FileSpec
        from scanindex.core.pipeline.batch_pipeline import (
            EVENT_PAGE_DONE, EVENT_FILE_OCR_DONE,
            EVENT_CORRECTION_START, EVENT_CORRECTION_DONE,
            EVENT_KIE_START, EVENT_KIE_DONE,
            EVENT_FILE_COMPLETE, EVENT_FILE_FAILED, EVENT_PIPELINE_DONE,
        )

        # Cancel any prior runner from a previous Step 1 → Step 2 trip
        prev = getattr(self, "_archive_runner", None)
        if prev is not None:
            try:
                prev.cancel()
            except Exception:
                pass

        # KIE outputs always land in the session-scoped temp dir, regardless
        # of input source (folder pick or Step-1 handoff). The "Xuất hồ sơ
        # nén" button in Step 3 is the only path that writes to a user-chosen
        # external location.
        out_dir = self.archive_tab.session.step2_kie_dir()
        self.archive_tab.set_output_folder(out_dir)

        # IMPORTANT: PySide6 `Signal(list)` deep-copies its payload, so the
        # `documents` we received here is a separate object from the one
        # `archive_tab._on_step1_to_step2` already pushed into Step 2 via
        # `set_documents(docs, ...)`. Mutating our local copy from the
        # pipeline thread (output_path / json_path / status) would leave
        # Step 2's list untouched and `_on_doc_selected` would read None.
        # Anchor on Step 2's own list reference so mutations are visible
        # both in the pipeline event handler and when the user clicks a row.
        step2 = getattr(self.archive_tab, "_step2", None) \
            or getattr(self.archive_tab, "archive_tab", None)
        if step2 is not None and getattr(step2, "_documents", None):
            self._arc_documents = step2._documents
        else:
            self._arc_documents = documents
        if step2 is not None and getattr(step2, "pdf_viewer", None) is not None:
            try:
                step2.pdf_viewer.clear()
                step2._current_doc_idx = -1
            except Exception:
                pass
        self.archive_tab.set_processing_state(True)
        self._arc_completed_count = 0
        kie_mode = self._normalize_kie_mode_setting(self._saved.get("kie_mode"))
        self._saved["kie_mode"] = kie_mode
        self.is_processing = True

        # Build FileSpecs with pre-OCR cache slices from the session
        session = self.archive_tab.session
        specs = []
        for doc in documents:
            seg = doc.get("_step1_segment")
            if seg is None:
                continue
            page_cache = {}
            for local_idx, src_idx in enumerate(seg.page_indices()):
                cached = session.get_cached_page(src_idx)
                if cached is not None:
                    page_cache[local_idx] = cached
            specs.append(FileSpec(
                input_path=doc["pdf_path"],
                file_id=os.path.basename(doc["pdf_path"]),
                source_document_path=doc.get("_step1_source_pdf"),
                pre_ocr_cache=page_cache,
                from_step1=True,
            ))

        # Reuse the same per-event state machine as folder mode
        def on_event(evt, payload):
            file_id = payload.get("file_id") if isinstance(payload, dict) else None
            if file_id is not None:
                for i, doc in enumerate(self._arc_documents):
                    if os.path.basename(doc["path"]) != file_id:
                        continue
                    changed = False
                    if evt == EVENT_PAGE_DONE:
                        pd = doc.get("_pages_done", 0) + 1
                        doc["_pages_done"] = pd
                        if pd == 1 and doc.get("status") == "Pending":
                            doc["status"] = "OCR..."
                            changed = True
                    elif evt == EVENT_FILE_OCR_DONE:
                        doc["status"] = "Pending"; changed = True
                    elif evt == EVENT_CORRECTION_START:
                        doc["status"] = "Correcting..."; changed = True
                    elif evt == EVENT_CORRECTION_DONE:
                        doc["status"] = "Pending"; changed = True
                    elif evt == EVENT_KIE_START:
                        doc["status"] = "KIE..."; changed = True
                    elif evt == EVENT_KIE_DONE:
                        task = payload.get("task")
                        if task is not None:
                            doc["output_path"] = task.output_pdf_path
                            doc["json_path"] = task.output_json_path
                            doc["selected_pages"] = getattr(task, "selected_pages", None)
                            doc["signature_page"] = getattr(task, "signature_page", None)
                            doc["page_selection"] = getattr(task, "page_selection", {})
                            # KIE inference also produces SECRECY_MARK; if
                            # Step 1's page-level detector missed (header
                            # not visible / not on first split page),
                            # promote KIE's signal so the row goes red.
                            ann = getattr(task, "kie_annotation", None) or {}
                            for f in ann.get("field_instances", []) or []:
                                if (f.get("label") == "SECRECY_MARK"
                                        and (f.get("text") or "").strip()):
                                    doc["_secrecy"] = f["text"].strip()
                                    break
                        doc["status"] = "Pending"; changed = True
                    elif evt == EVENT_FILE_COMPLETE:
                        doc["status"] = "Done"
                        self._arc_completed_count += 1
                        self.signals.cache_updated.emit(
                            "archive", i, "_refresh", "1",
                        )
                        changed = True
                    elif evt == EVENT_FILE_FAILED:
                        doc["status"] = "Failed"
                        self._arc_completed_count += 1
                        changed = True
                    if changed:
                        self.signals.status_updated.emit("archive", i, doc["status"])
                    break
            if evt == EVENT_PIPELINE_DONE:
                self.is_processing = False
                self.signals.processing_finished.emit()

        self._archive_runner = ArchiveRunner(
            output_dir=out_dir,
            file_specs=specs,
            kie_mode=kie_mode,
            on_event=on_event,
            log_cb=lambda m: self.signals.log_message.emit(str(m), LOG_INFO),
            write_excel_on_done=False,
            enable_correction=bool(self._saved.get("correct", True)),
        )
        self._archive_runner.start()
        self.log(f"Archive: Step 1→2 pipeline started on {len(specs)} segments (KIE={kie_mode})")

    def _arc_pick_final_pdf(self, doc: dict, stt: int = 0,
                            identity=None) -> str | None:
        """Choose which PDF represents the document for external delivery /
        Kho import. Priority: signed PDF in `<temp>/_step3_signed/` if it
        exists, else the KIE-overlay PDF in `<temp>/_step2_kie/`."""
        kie_pdf = doc.get("output_path") or ""
        if not kie_pdf:
            return None
        signed_dir = self.archive_tab.session.step3_signed_dir()
        if stt > 0:
            signed_name = self._arc_export_pdf_name(identity, stt, kie_pdf)
            signed_pdf = os.path.join(signed_dir, signed_name)
            if os.path.exists(signed_pdf):
                return signed_pdf
        # Backward-compatible lookup for signed files created before Step 3
        # started naming signed outputs by the current dossier identity.
        signed_pdf = os.path.join(signed_dir, os.path.basename(kie_pdf))
        if os.path.exists(signed_pdf):
            return signed_pdf
        if os.path.exists(kie_pdf):
            return kie_pdf
        return None

    @staticmethod
    def _arc_canonical_name(pdf_path: str) -> str:
        """Drop the `_ocr` suffix the KIE pipeline appended so files land in
        Kho / external folders under their archive-canonical name."""
        stem, ext = os.path.splitext(os.path.basename(pdf_path))
        if stem.endswith("_ocr"):
            stem = stem[:-4]
        return f"{stem}{ext}"

    @classmethod
    def _arc_export_pdf_name(cls, identity, stt: int,
                             fallback_pdf_path: str | None = None) -> str:
        """Final HSLTCQ PDF name for external ZIP / Kho storage.

        Always prefer the current dossier identity, so editing dossier info
        in Step 3 before export/import changes the output names even when
        the Step 2 source files were arbitrary names such as `a.pdf`.
        """
        name = ""
        if identity is not None:
            try:
                if identity.is_complete():
                    name = identity.make_segment_name(stt)
            except Exception:
                name = ""
        if not name and fallback_pdf_path:
            name = cls._arc_canonical_name(fallback_pdf_path)
        if not name:
            name = f"document-{stt:03d}.pdf"
        stem, ext = os.path.splitext(name)
        stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", stem).strip(" .")
        ext = ext if ext.lower() == ".pdf" else ".pdf"
        return f"{stem or f'document-{stt:03d}'}{ext}"

    @staticmethod
    def _arc_export_zip_name(identity) -> str:
        parts = [
            getattr(identity, "ma_dinh_danh", "") or "",
            getattr(identity, "ma_phong", "") or "",
            getattr(identity, "muc_luc", "") or "",
            getattr(identity, "ho_so", "") or "",
        ]
        parts = [str(p).strip() for p in parts]
        if not all(parts):
            return "HSLTCQ.zip"
        stem = "-".join(parts)
        stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", stem).strip(" .")
        return f"{stem or 'HSLTCQ'}.zip"

    def _arc_export_external(self):
        """Step 3 button "Xuất hồ sơ nén" — bundle Excel metadata
        + the final PDFs (signed if present, else KIE overlay) for the
        dossier into a single `HSLTCQ.zip` (or `<dossier_code>.zip`)
        written into a user-chosen folder. Layout matches the
        verified-working reference E:/TEMP/HSLTCQ (1).zip:

            HSLTCQ/
              └── METADATA/
                    ├── MetaDuLieu.xlsx     # 4 sheets: Hồ sơ / Văn bản / Ảnh / Video
                    ├── <stem-1>.pdf
                    └── <stem-N>.pdf

        After success, offer to import the dossier into Kho."""
        # Flush pending form edits (Độ mật, Ngôn ngữ, …) into doc["metadata"]
        # so write_aggregated_excel's form-override layer picks them up.
        if not self.archive_tab.confirm_unsaved_before_leave():
            return
        try:
            self.archive_tab._step2._save_current_fields()
        except Exception:
            pass
        documents = self.archive_tab.get_documents()
        if not documents:
            QMessageBox.information(self, "Xuất hồ sơ nén",
                                     "Chưa có hồ sơ để xuất. Hãy chạy Bước 2 trước.")
            return
        out_dir = QFileDialog.getExistingDirectory(
            self, "Chọn thư mục để lưu file ZIP"
        )
        if not out_dir:
            return

        try:
            import tempfile
            import zipfile
            from scanindex.core.digitization.runner import write_aggregated_excel

            identity = self.archive_tab.session.identity
            export_docs = []
            skipped = 0
            for doc in documents:
                stt = len(export_docs) + 1
                src = self._arc_pick_final_pdf(doc, stt=stt, identity=identity)
                if not src or not os.path.isfile(src):
                    skipped += 1
                    continue
                export_name = self._arc_export_pdf_name(
                    identity, stt, src
                )
                doc_for_export = dict(doc)
                doc_for_export["export_source_path"] = src
                doc_for_export["export_file_name"] = export_name
                export_docs.append(doc_for_export)

            if not export_docs:
                QMessageBox.warning(
                    self, "Xuất hồ sơ nén",
                    "Không có PDF hợp lệ để xuất.",
                )
                return

            tmp_xlsx = tempfile.NamedTemporaryFile(
                suffix=".xlsx", delete=False
            )
            tmp_xlsx.close()
            excel_tmp_path = tmp_xlsx.name
            write_aggregated_excel(export_docs, excel_tmp_path, identity=identity)

            # Reference zip layout (verified working on the receiving
            # system): top folder "HSLTCQ/", inner "METADATA/" with the
            # workbook + all PDFs at the same level. The receiving
            # importer matches against these exact path segments.
            zip_name = self._arc_export_zip_name(identity)
            zip_path = os.path.join(out_dir, zip_name)
            if os.path.exists(zip_path):
                base, ext = os.path.splitext(zip_name)
                for i in range(2, 1000):
                    candidate = os.path.join(out_dir, f"{base}_{i}{ext}")
                    if not os.path.exists(candidate):
                        zip_path = candidate
                        break

            copied = 0
            try:
                with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    zf.write(excel_tmp_path, "HSLTCQ/METADATA/MetaDuLieu.xlsx")
                    for doc in export_docs:
                        src = doc["export_source_path"]
                        dst_name = doc["export_file_name"]
                        zf.write(src, f"HSLTCQ/METADATA/{dst_name}")
                        copied += 1
            finally:
                try:
                    os.unlink(excel_tmp_path)
                except Exception:
                    pass

            self.log(
                f"Archive: ZIP exported — {copied} PDF + Excel → {zip_path}"
                + (f" ({skipped} skipped without source)" if skipped else ""),
                LOG_SUCCESS,
            )
        except Exception as e:
            self.log(f"Archive: Export failed: {e}", LOG_ERROR)
            QMessageBox.critical(self, "Lỗi", f"Xuất thất bại: {e}")
            return

        # After successful external export, offer to also load the dossier
        # into the internal Kho — independent action, user can decline.
        ask = QMessageBox(self)
        ask.setWindowTitle("Chuyển vào Kho?")
        ask.setIcon(QMessageBox.Icon.Question)
        ask.setText("Đã xuất hồ sơ nén.")
        ask.setInformativeText("Bạn có muốn chuyển bộ hồ sơ này vào Kho lưu trữ nội bộ không?")
        ask.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        ask.setDefaultButton(QMessageBox.StandardButton.No)
        if ask.exec() == QMessageBox.StandardButton.Yes:
            self._arc_import_to_kho()

    @staticmethod
    def _kho_embedder_cached() -> bool:
        """True if the embedding model is already on disk. Acceptable
        forms:
          1. E5 mix50 ONNX fp32 at `models/archive_models/e5-small-mix50-v2-onnx-fp32/`
          2. E5 mix50 ONNX int8 at `models/archive_models/e5-small-mix50-v2-onnx-int8/`
        Either avoids an on-demand Hugging Face download surprise."""
        try:
            from scanindex.infra.paths import get_base_dir
            base = os.path.join(get_base_dir(), "models", "archive_models")
            # Form 0: E5 mix50 ONNX fp32 (quality-first preferred backend)
            if os.path.isfile(os.path.join(base, "e5-small-mix50-v2-onnx-fp32", "model.onnx")):
                return True
            # Form 1: E5 mix50 ONNX int8
            if os.path.isfile(os.path.join(base, "e5-small-mix50-v2-onnx-int8", "model_quantized.onnx")):
                return True
        except Exception:
            return False
        return False

    def _arc_import_to_kho(self):
        """Step 3 button "Chuyển vào Kho" — push the current dossier into
        the internal Kho lưu trữ. Standalone: user can call this without
        first exporting externally."""
        if not self.archive_tab.confirm_unsaved_before_leave():
            return
        documents = self.archive_tab.get_documents()
        if not documents:
            QMessageBox.information(self, "Chuyển vào Kho",
                                     "Chưa có hồ sơ để chuyển. Hãy chạy Bước 2 trước.")
            return

        session = self.archive_tab.session
        identity = getattr(session, "identity", None)
        if identity is None or not identity.is_complete():
            QMessageBox.warning(
                self, "Chuyển vào Kho",
                "Thiếu mã định danh hồ sơ. Hãy quay về Bước 1 nhập đủ "
                "mã định danh, mã phông, mục lục, hồ sơ trước khi chuyển vào Kho.",
            )
            return

        # Pick final PDF + canonical JSON for each doc. We prefer the signed
        # variant (Step 3 output) but fall back to the KIE PDF for files the
        # user chose not to sign.
        docs_to_import = []
        skipped_no_data = 0
        for doc in documents:
            kie_pdf = doc.get("output_path") or ""
            json_path = doc.get("json_path") or ""
            if not json_path and kie_pdf:
                json_path = kie_pdf + ".json"
            if not (kie_pdf and os.path.exists(kie_pdf)
                    and json_path and os.path.exists(json_path)):
                skipped_no_data += 1
                continue
            stt = len(docs_to_import) + 1
            final_pdf = (
                self._arc_pick_final_pdf(doc, stt=stt, identity=identity)
                or kie_pdf
            )
            target_name = self._arc_export_pdf_name(
                identity, stt, final_pdf
            )
            docs_to_import.append({
                "pdf_path": final_pdf,
                "canonical_json_path": json_path,
                "target_file_name": target_name,
            })
        if not docs_to_import:
            QMessageBox.warning(self, "Chuyển vào Kho",
                                 "Không có file hợp lệ để chuyển (thiếu PDF hoặc canonical JSON).")
            return

        from scanindex.core.repository.importer import DossierCodes
        codes = DossierCodes(
            ma_dinh_danh=identity.ma_dinh_danh,
            fonds=identity.ma_phong,
            catalog=identity.muc_luc,
            dossier_code=identity.ho_so,
            fonds_name=getattr(identity, "ten_phong", ""),
            catalog_name=getattr(identity, "ten_muc_luc", ""),
            title=identity.title or f"Hồ sơ {identity.ho_so}",
            is_unstructured=identity.is_unstructured,
            retention=identity.thoi_han_bao_quan,
            term=(identity.nhiem_ky or "")[:10],
            storage_unit=identity.ho_so,            # Đơn vị bảo quản số = ho_so
            physical_state=identity.tinh_trang_vat_ly,
            topic=identity.chuyen_de,
            note=identity.chu_thich,
        )

        from scanindex.ui.repository.screen import _read_archive_path_setting
        archive_path = _read_archive_path_setting()

        progress = QProgressDialog(
            "Đang chuyển vào Kho lưu trữ…", "Hủy",
            0, len(docs_to_import), self,
        )
        progress.setWindowTitle("Chuyển vào Kho")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.setValue(0)
        progress.show()

        worker = _KhoImportWorker(archive_path, codes, docs_to_import)

        # The Kho screen opens its own HybridIndex (Tantivy + FAISS) at
        # app startup for fast browse/search. On Windows, Tantivy's
        # writer can't create the new .pos / .term files while another
        # Index instance has them mmap-mapped — we get ACCESS_DENIED.
        # Release Kho's handles for the duration of the import; reopen
        # on completion so the user sees the freshly-imported dossier.
        try:
            self.repository_screen.release_index_for_writer()
        except Exception as e:
            self.log(f"Archive: could not release Kho index: {e}", LOG_ERROR)

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
                self.repository_screen.reopen_index_after_writer()
            except Exception as e:
                self.log(f"Archive: could not reopen Kho index: {e}", LOG_ERROR)

        def on_finished_ok(result):
            progress.close()
            _reopen_kho()
            msg = (f"Đã chuyển: {result.imported}\n"
                   f"Bỏ qua (đã có trong Kho): {result.skipped}\n"
                   f"Lỗi: {result.failed}\n\n"
                   "Có thể tìm nhanh ngay. Chỉ mục ngữ nghĩa sẽ chạy nền.")
            QMessageBox.information(self, "Chuyển vào Kho hoàn tất", msg)
            self.log(
                f"Archive: imported to Kho — {result.imported}/{result.total}",
                LOG_SUCCESS,
            )
            if result.failed == 0 and result.imported > 0:
                ask = QMessageBox.question(
                    self, "Xóa file tạm?",
                    "Hồ sơ đã được chuyển vào Kho an toàn.\n"
                    "Bạn có muốn xóa thư mục tạm của hồ sơ này không?",
                )
                if ask == QMessageBox.StandardButton.Yes:
                    try:
                        self.archive_tab.reset_workflow()
                    except Exception as e:
                        self.log(
                            f"Archive: cleanup after Kho import failed: {e}",
                            LOG_ERROR,
                        )

        def on_failed(error_msg):
            progress.close()
            _reopen_kho()
            self.log(f"Archive: import to Kho failed: {error_msg}", LOG_ERROR)
            QMessageBox.critical(
                self, "Lỗi",
                f"Chuyển vào Kho thất bại:\n{error_msg}",
            )

        def on_thread_finished():
            if getattr(self, "_arc_kho_worker", None) is worker:
                self._arc_kho_worker = None
            worker.deleteLater()

        worker.progress.connect(on_progress)
        worker.finished_ok.connect(on_finished_ok)
        worker.failed.connect(on_failed)
        worker.finished.connect(on_thread_finished)
        progress.canceled.connect(on_cancelled)
        # Hold a reference so Python doesn't GC the QThread mid-run.
        self._arc_kho_worker = worker
        worker.start()

    # ================================================================
    # PROCESSING CONTROL
    # ================================================================

    def set_processing_state(self, is_running):
        self.is_processing = is_running
        self.dnd_tab.set_processing_state(is_running)
        self.archive_tab.set_processing_state(is_running)
        # Note: with QStackedWidget there are no tabs to disable. The Back
        # button on each ScreenContainer already handles cancel-confirm when
        # `is_processing` is True (see ScreenContainer._on_back_clicked).

    def stop_ocr(self):
        if self.is_processing:
            self.log("Stopping requested... waiting for current processes to finish.", LOG_ERROR)
            if self.pipeline:
                self.pipeline.stop()
            self.stop_event.set()

    @Slot()
    def _on_processing_finished(self):
        self.set_processing_state(False)

    def start_dnd_process(self):
        if not self.dnd_files:
            return
        if self._queue_dnd_start_until_models_ready():
            return
        self.start_thread()

    def start_thread(self):
        if self.pipeline:
            self.pipeline.stop()

        vals = self.settings_tab.get_values()
        try:
            cfg_wait_page = float(vals["wait_page"])
        except ValueError:
            cfg_wait_page = 1.0
        try:
            cfg_wait_int = float(vals["compare_int"])
        except ValueError:
            cfg_wait_int = 1.0
        try:
            max_w = max(1, int(vals["concurrency"]))
        except ValueError:
            max_w = 4

        config = {
            "wait_page": cfg_wait_page,
            "wait_int": cfg_wait_int,
            "max_workers": max_w,
            "do_correct": bool(self._saved.get("correct", True)),
            "do_metadata": False,
            "do_export": self.dnd_tab.chk_export.isChecked(),
            "force_rerun": False,
        }

        pipeline_items = []
        for i, f in enumerate(self.dnd_files):
            target_path = f.get("output_path")
            if target_path and not str(target_path).lower().endswith(".pdf"):
                target_path = None
            pipeline_items.append({
                "index": i, "dnd_item": f,
                "target_path": target_path, "list_type": "dnd",
            })

        if not pipeline_items:
            return

        self.stop_event.clear()
        self.set_processing_state(True)
        self.log(f"Starting Pipeline (Parallel OCR files: {max_w}, ~{max_w * SCREEN_AI_WORKERS_PER_FILE} OCR workers)...")

        self.pipeline = ProcessingPipeline(self, pipeline_items, config)
        self.pipeline.start()

    # ================================================================
    # RE-RUN SINGLE FILE
    # ================================================================

    def re_run_file(self, idx):
        if idx < 0 or idx >= len(self.dnd_files):
            return
        item = self.dnd_files[idx]

        # Cleanup old derivative files
        out_path = item.get("output_path")
        try:
            src_base, _ = os.path.splitext(item.get("path", ""))
            if out_path:
                cleanup_paths = {
                    out_path,
                    out_path + ".json",
                }
                base, ext = os.path.splitext(out_path)
                cleanup_paths.add(base + "_final.docx")
                if str(out_path).lower().endswith("_ocr.pdf"):
                    cleanup_paths.add(_docx_output_for_ocr_pdf(out_path))
            else:
                cleanup_paths = set()
            if src_base:
                cleanup_paths.update({
                    src_base + "_ocr.pdf",
                    src_base + "_ocr.pdf.json",
                    src_base + "_final.docx",
                })
            for p in cleanup_paths:
                if p and os.path.exists(p):
                    os.remove(p)
        except Exception:
            pass

        self.dnd_files[idx]["status"] = "Pending"
        if out_path and not os.path.exists(out_path):
            self.dnd_files[idx]["output_path"] = None
        self.dnd_files[idx]["corrected_text"] = ""
        self.dnd_files[idx]["original_text"] = ""
        self.refresh_file_list()

        self.stop_event.clear()
        t = threading.Thread(target=self._run_single_file, args=(idx,), daemon=True)
        t.start()

    def _run_single_file(self, idx):
        """Dedicated worker for re-running a single file through the pipeline."""
        if idx < 0 or idx >= len(self.dnd_files):
            return
        f = self.dnd_files[idx]
        do_correct = bool(self._saved.get("correct", True))
        do_export = self.dnd_tab.chk_export.isChecked()

        vals = self.settings_tab.get_values()
        try:
            cfg_wait_page = float(vals["wait_page"])
        except ValueError:
            cfg_wait_page = 1.0
        try:
            cfg_wait_int = float(vals["compare_int"])
        except ValueError:
            cfg_wait_int = 1.0

        self.log(f"Re-running {os.path.basename(f['path'])}...")

        fpath = f["path"]
        fname = os.path.basename(fpath)

        self.update_item_status("dnd", idx, "OCR Processing")

        # Calc output
        if f.get("target_ocr_path"):
            final_out = f["target_ocr_path"]
        else:
            base, ext = os.path.splitext(fpath)
            final_out = base + "_ocr" + ext
        os.makedirs(os.path.dirname(final_out), exist_ok=True)

        # OCR
        if os.path.exists(final_out):
            self.log(f"  > Output exists ({os.path.basename(final_out)}). Skipping OCR.")
            res, msg, dt = True, None, 0
            self.update_item_status("dnd", idx, "OCR Done", final_out)
        else:
            num_pages = 1
            try:
                r = pypdf.PdfReader(fpath)
                num_pages = len(r.pages)
            except Exception:
                pass

            if self.ocr_executor is None:
                self.ocr_executor = concurrent.futures.ThreadPoolExecutor(
                    max_workers=self.max_parallel_ocr_files)

            def _ocr_work():
                return _run_preprocess_and_ocr(
                    input_path=fpath,
                    output_path=final_out,
                    num_pages=num_pages,
                    parallel_files=self.max_parallel_ocr_files,
                    update_callback=self.log_callback,
                    debug_mode=self.settings_tab.chk_verbose.isChecked(),
                    wait_per_page=cfg_wait_page,
                    comparison_interval=cfg_wait_int,
                    source_document_path=fpath,
                )

            future = self.ocr_executor.submit(_ocr_work)
            t0 = time.time()
            res, msg = future.result()
            dt = time.time() - t0

        current_status = "Failed"
        current_out = None

        if res:
            if dt > 0:
                self.log(f"  > OCR completed in {dt:.2f}s")
            ui_status = "OCR Done" if not do_correct else "OCR Processing"
            self.update_item_status("dnd", idx, ui_status, final_out)
            current_status = "OCR Done"
            current_out = final_out
        else:
            self.log(f"  > OCR Failed: {msg}", LOG_ERROR)
            self.update_item_status("dnd", idx, "Failed")
            return

        if not do_correct and not do_export:
            self.update_item_status("dnd", idx, "Done")
            return

        # Correction
        if current_status == "OCR Done" and do_correct:
            self.update_item_status("dnd", idx, "Correcting...")
            try:
                raw = ""
                r = pypdf.PdfReader(current_out)
                for p in r.pages:
                    raw += p.extract_text(extraction_mode="layout") + "\n\n"
                self.dnd_files[idx]["original_text"] = raw

                # Skip correction nếu file là digital — text native đã chính xác,
                # correction model train cho OCR errors có thể thay đổi sai.
                from scanindex.core.pdf.text_extractor import is_digital_ocr_output
                if is_digital_ocr_output(current_out):
                    self.log("  > Skip correction: digital PDF (text native)")
                    corrected_text = raw
                    dt = 0.0
                else:
                    with self.correction_lock:
                        t0 = time.time()
                        corrected_text = correction_engine.correct_text(raw)
                        dt = time.time() - t0

                self.log(f"  > Correction completed in {dt:.2f}s")

                pdf_utils.create_corrected_pdf(
                    current_out, current_out, raw, corrected_text,
                    log_callback=self.gui_log_callback)

                self.update_item_status("dnd", idx, "Corrected",
                                        current_out, corrected_text=corrected_text)
                current_status = "Corrected"
            except Exception as e:
                self.log(f"  > Correction failed: {e}", LOG_ERROR)
                self.update_item_status("dnd", idx, "OCR Done")

        if current_status == "Corrected" and not do_export:
            self.update_item_status("dnd", idx, "Done")
            return

        # Export
        if do_export and current_status in ("Corrected", "OCR Done"):
            src_pdf = current_out

            if src_pdf and os.path.exists(src_pdf):
                self.update_item_status("dnd", idx, "Exporting...")
                try:
                    final_docx = _docx_output_for_ocr_pdf(src_pdf)

                    if self.export_executor is None:
                        from scanindex.core.tables.export_worker import init_export_worker
                        self.export_executor = concurrent.futures.ProcessPoolExecutor(
                            max_workers=self.max_export_workers,
                            initializer=init_export_worker)

                    from scanindex.core.tables.export_worker import run_export_task
                    doc_metadata = f.get("metadata")
                    future = self.export_executor.submit(
                        run_export_task, os.path.abspath(src_pdf), os.path.abspath(final_docx),
                        metadata=doc_metadata)
                    res = future.result()

                    if res["success"]:
                        self.log(f"  > Export completed: {os.path.basename(final_docx)}")
                        _cleanup_ocr_intermediate(src_pdf, fpath, self.log)
                        self.update_item_status("dnd", idx, "Done", final_docx)
                    else:
                        self.log(f"  > Export Failed: {res['msg']}", LOG_ERROR)
                        self.update_item_status("dnd", idx, "Done (Export Failed)")
                except Exception as e:
                    self.log(f"  > Export Failed: {e}", LOG_ERROR)
                    self.update_item_status("dnd", idx, "Done (Export Failed)")

        if current_status == "OCR Done" and not do_correct and not do_export:
            self.update_item_status("dnd", idx, "Done")

    # ================================================================
    # ANIMATION
    # ================================================================

    def _animate_status(self):
        self.spinner_idx = (self.spinner_idx + 1) % len(self.spinner_chars)
        char = self.spinner_chars[self.spinner_idx]
        animated = ["Processing", "OCR Processing", "Correcting", "Exporting", "Correcting..."]

        for i, item in enumerate(self.dnd_files):
            w = self.dnd_tab.file_list.get_widget(i)
            if w:
                status = item["status"]
                base = status.replace(" \u25d0", "").strip()
                if any(s in base for s in animated):
                    display = translations.get_text(STATUS_KEY_MAP.get(base, base))
                    w.set_spinner_text(f"{display} {char}")
        if getattr(self, "_background_model_loading", None):
            self._refresh_background_model_status()

    # ================================================================
    # CLOSE
    # ================================================================

    def closeEvent(self, event):
        self.save_settings()
        self.stop_ocr()
        if self.export_executor:
            self.export_executor.shutdown(wait=False)
        if self.ocr_executor:
            self.ocr_executor.shutdown(wait=False)
        try:
            self.archive_tab.cleanup()
        except Exception:
            pass
        event.accept()


# ====================================================================
# PROCESSING PIPELINE
# ====================================================================

class ProcessingPipeline:
    """3-phase processing pipeline: OCR (parallel) → Correction (serial) → Export (parallel)."""

    def __init__(self, app: MainWindow, files: list, config: dict):
        self.app = app
        self.files = files
        self.config = config
        self.parallel_files = int(config.get("max_workers", 4))
        self.stop_event = threading.Event()

        if self.app.ocr_executor is None:
            total_workers = self.parallel_files * SCREEN_AI_WORKERS_PER_FILE
            self.app.log(f"Initializing OCR file pool ({self.parallel_files} parallel files, ~{total_workers} OCR workers)")
            self.app.ocr_executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=self.parallel_files)
        self.executor = self.app.ocr_executor

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def _run(self):
        batch_size = self.parallel_files
        batches = [self.files[i:i + batch_size]
                   for i in range(0, len(self.files), batch_size)]

        self.app.log(f"Pipeline: {len(batches)} batches, size {batch_size}")

        for b_idx, batch in enumerate(batches):
            if self.stop_event.is_set():
                break
            self.app.log(f"--- Batch {b_idx + 1}/{len(batches)} ({len(batch)} files) ---")

            # Phase 1: OCR
            ocr_items = []
            for item in batch:
                f = item["dnd_item"]
                if f["status"] not in ("Done", "Corrected", "OCR Done") or self.config["force_rerun"]:
                    ocr_items.append(item)
                else:
                    item["skipped_ocr"] = True

            if ocr_items:
                futures = {self.executor.submit(self._ocr_single, it["dnd_item"], it): it
                           for it in ocr_items}
                for it in ocr_items:
                    self.app.update_item_status(it["list_type"], it["index"], "OCR Processing")
                concurrent.futures.wait(futures)

                for fut in futures:
                    it = futures[fut]
                    try:
                        res, msg, out_path = fut.result()
                        if res:
                            it["dnd_item"]["output_path"] = out_path
                            it["dnd_item"]["status"] = "OCR Done"
                            self.app.update_item_status(it["list_type"], it["index"],
                                                        "OCR Done", out_path)
                            it["ocr_success"] = True
                        else:
                            self.app.log(f"OCR Fail: {msg}", LOG_ERROR)
                            it["dnd_item"]["status"] = "Failed"
                            self.app.update_item_status(it["list_type"], it["index"], "Failed")
                            it["ocr_success"] = False
                    except Exception as e:
                        self.app.log(f"OCR Exception: {e}", LOG_ERROR)
                        self.app.update_item_status(it["list_type"], it["index"], "Failed")
                        it["ocr_success"] = False

            if self.stop_event.is_set():
                break

            # Phase 2: Correction (serial)
            if self.config["do_correct"]:
                for item in batch:
                    if self.stop_event.is_set():
                        break
                    f = item["dnd_item"]
                    can_run = item.get("ocr_success", False)
                    if not can_run and item.get("skipped_ocr") and f["status"] == "OCR Done":
                        can_run = True
                    if can_run:
                        self._correct_single(item)

            if self.stop_event.is_set():
                break

            # Phase 2.5: Metadata Extraction (fast, serial)
            if self.config.get("do_metadata", True):
                for item in batch:
                    if self.stop_event.is_set():
                        break
                    f = item["dnd_item"]
                    if f["status"] in ("Corrected", "OCR Done"):
                        self._extract_metadata_single(item)

            if self.stop_event.is_set():
                break

            # Phase 3: Export (parallel)
            if self.config["do_export"]:
                export_items = []
                for item in batch:
                    f = item["dnd_item"]
                    pdf_to_export = None
                    if f["status"] == "Corrected":
                        pdf_to_export = f.get("output_path")
                    elif f["status"] == "OCR Done" and not self.config["do_correct"]:
                        pdf_to_export = f.get("output_path")
                    if pdf_to_export and os.path.exists(pdf_to_export):
                        item["pdf_to_export"] = pdf_to_export
                        export_items.append(item)

                if export_items:
                    exp_futures = {self.executor.submit(self._export_single, it): it
                                   for it in export_items}
                    concurrent.futures.wait(exp_futures)

        self.app.log("All batches finished.", LOG_SUCCESS)
        self.app.signals.processing_finished.emit()

    def _ocr_single(self, f, item):
        file_path = f["path"]
        if item.get("target_path"):
            final_out = item["target_path"]
        elif f.get("target_ocr_path"):
            final_out = f["target_ocr_path"]
        else:
            base, ext = os.path.splitext(file_path)
            final_out = base + "_ocr" + ext

        os.makedirs(os.path.dirname(final_out), exist_ok=True)

        if os.path.exists(final_out) and not self.config["force_rerun"]:
            self.app.log(f"Output exists {os.path.basename(final_out)}. Skipping OCR.")
            return True, "Exists", final_out

        num_pages = 1
        try:
            with open(file_path, "rb") as f_in:
                num_pages = len(pypdf.PdfReader(f_in).pages)
        except Exception:
            pass

        def _log_cb(msg, level=LOG_INFO):
            verbose = self.app.settings_tab.chk_verbose.isChecked()
            if verbose:
                self.app.log(f"[{os.path.basename(file_path)}] {msg}", level)

        t0 = time.time()
        res, msg = _run_preprocess_and_ocr(
            input_path=file_path,
            output_path=final_out,
            num_pages=num_pages,
            parallel_files=self.parallel_files,
            update_callback=self.app.log_callback,
            debug_mode=self.app.settings_tab.chk_verbose.isChecked(),
            wait_per_page=self.config["wait_page"],
            comparison_interval=self.config["wait_int"],
            source_document_path=file_path,
            ocr_log_callback=_log_cb,
        )
        dt = time.time() - t0

        if res:
            self.app.log(f"OCR Success: {final_out} ({dt:.1f}s)", LOG_SUCCESS)
            return True, None, final_out
        else:
            return False, msg, None

    def _correct_single(self, item):
        idx = item["index"]
        f = item["dnd_item"]
        fname = os.path.basename(f["path"])
        pdf_path = f.get("output_path")
        if not pdf_path or not os.path.exists(pdf_path):
            return

        self.app.update_item_status(item["list_type"], idx, "Correcting...")

        try:
            raw = f.get("original_text", "")
            if not raw:
                # Wait for file to settle
                def get_size(p):
                    try:
                        return os.path.getsize(p)
                    except Exception:
                        return -1

                last_size = get_size(pdf_path)
                for _ in range(5):
                    time.sleep(0.2)
                    curr = get_size(pdf_path)
                    if curr > 0 and curr == last_size:
                        break
                    last_size = curr

                for attempt in range(3):
                    try:
                        raw = ""
                        with open(pdf_path, "rb") as f_in:
                            r = pypdf.PdfReader(f_in)
                            for p in r.pages:
                                raw += p.extract_text(extraction_mode="layout") + "\n\n"
                        if len(raw) > 10:
                            break
                    except Exception:
                        time.sleep(0.5)

                self.app.update_file_cache(idx, "original_text", raw, item.get("list_type", "dnd"))

            # Skip correction nếu file là digital — text native đã chính xác.
            from scanindex.core.pdf.text_extractor import is_digital_ocr_output
            if is_digital_ocr_output(pdf_path):
                self.app.log(f"Skip correction: digital PDF ({pdf_path})", LOG_INFO)
                corrected = raw
                dt = 0.0
            else:
                with self.app.correction_lock:
                    t0 = time.time()
                    corrected = correction_engine.correct_text(raw)
                    dt = time.time() - t0

            # Save
            out2 = pdf_path

            pdf_utils.create_corrected_pdf(
                pdf_path, out2, raw, corrected,
                log_callback=self.app.gui_log_callback)

            self.app.log(f"Correction Success: {out2} ({dt:.1f}s)", LOG_SUCCESS)

            # Cleanup pre file
            pre_file = f.pop("_pre_file", None)
            if pre_file and os.path.exists(pre_file):
                try:
                    os.remove(pre_file)
                except Exception:
                    pass

            f["status"] = "Corrected"
            f["output_path"] = out2
            f["corrected_text"] = corrected
            self.app.update_item_status(
                item["list_type"], idx, "Corrected", out2, corrected_text=corrected)

        except Exception as e:
            self.app.log(f"Correction Error: {e}", LOG_ERROR)
            self.app.update_item_status(item["list_type"], idx, "OCR Done")

    def _extract_metadata_single(self, item):
        """Phase 2.5: Extract document metadata from OCR JSON companion."""
        f = item["dnd_item"]
        output_path = f.get("output_path", "")
        if not output_path:
            return

        # Find JSON companion: try _ocr.pdf.json (always created by OCR)
        json_path = None
        for candidate in [
            output_path + ".json",
        ]:
            if os.path.exists(candidate):
                json_path = candidate
                break

        if not json_path:
            return

        try:
            from scanindex.core.digitization import metadata_extractor as document_metadata_extractor
            metadata = document_metadata_extractor.extract_metadata(
                json_path, log_callback=self.app.log
            )
            f["metadata"] = metadata
            # Trigger UI refresh so "M" button appears
            idx = item["index"]
            self.app.update_item_status(
                item["list_type"], idx, f["status"], output_path)
        except Exception as e:
            self.app.log(f"Metadata extraction error: {e}", LOG_ERROR)

    def _export_single(self, item):
        idx = item["index"]
        pdf_in = item.get("pdf_to_export")
        if not pdf_in or not os.path.exists(pdf_in):
            return

        self.app.update_item_status(item["list_type"], idx, "Exporting...")

        try:
            docx_out = _docx_output_for_ocr_pdf(pdf_in)

            if self.app.export_executor is None:
                from scanindex.core.tables.export_worker import init_export_worker
                self.app.export_executor = concurrent.futures.ProcessPoolExecutor(
                    max_workers=self.app.max_export_workers,
                    initializer=init_export_worker)

            from scanindex.core.tables.export_worker import run_export_task
            t0 = time.time()
            doc_metadata = item["dnd_item"].get("metadata")
            future = self.app.export_executor.submit(
                run_export_task, os.path.abspath(pdf_in), os.path.abspath(docx_out),
                metadata=doc_metadata)
            res = future.result()
            dt = time.time() - t0

            if res["success"]:
                self.app.log(f"Export Success: {docx_out} ({dt:.1f}s)", LOG_SUCCESS)
                item["dnd_item"]["status"] = "Done"
                item["dnd_item"]["output_path"] = docx_out
                _cleanup_ocr_intermediate(
                    pdf_in,
                    item["dnd_item"].get("path"),
                    self.app.log,
                )
                self.app.update_item_status(item["list_type"], idx, "Done", docx_out)
            else:
                self.app.log(f"Export Failed: {res['msg']}", LOG_ERROR)
                self.app.update_item_status(item["list_type"], idx, "Done (Export Failed)")

            # Log export details
            verbose = self.app.settings_tab.chk_verbose.isChecked()
            log_content = res.get("logs", "")
            if verbose and log_content:
                for line in log_content.splitlines():
                    if line.strip():
                        self.app.log(f"  [Exp] {line}", LOG_DEBUG)
        except Exception as e:
            self.app.log(f"Export Exception: {e}", LOG_ERROR)
