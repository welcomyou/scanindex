"""
Entry point for ScanIndex (PySide6 version).
"""
import os
import sys
import multiprocessing

from scanindex.infra.mp_safety import (
    ensure_valid_stdio,
    patch_multiprocessing_spawn_sys_path,
    sanitize_current_sys_path,
)

ensure_valid_stdio()
sanitize_current_sys_path()
patch_multiprocessing_spawn_sys_path()


def _patch_six_meta_path_importer():
    """Avoid PyInstaller/PySide6 inspect crashes on Python 3.12."""
    try:
        import six
    except Exception:
        return
    for importer in sys.meta_path:
        if (
            importer.__class__.__name__ == "_SixMetaPathImporter"
            and not hasattr(importer, "_path")
        ):
            importer._path = []


# Portable/offline setup must run before any torch/transformers import.
try:
    from scanindex.infra import paths as portable_utils
except Exception as exc:
    print(f"[Portable] Warning: portable paths import failed: {exc}")
else:
    try:
        portable_utils.ensure_runtime_config_files()
    except Exception as exc:
        print(f"[Portable] Warning: runtime config bootstrap failed: {exc}")
    try:
        portable_utils.setup_offline_mode()
    except Exception as exc:
        print(f"[Portable] Warning: setup_offline_mode failed: {exc}")

# FORCE CPU ONLY — must be before any torch import
os.environ["CUDA_VISIBLE_DEVICES"] = ""
os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"

# Pre-import pandas BEFORE PySide6 to avoid shiboken circular import crash
try:
    _patch_six_meta_path_importer()
    import pandas  # noqa: F401
except ImportError:
    pass


def _apply_ocr_page_worker_setting():
    """Read MaxConcurrentOCR from settings.ini and export it as
    DIRECT_OCR_NUM_PAGE_WORKERS so core/ocr/direct_engine.py picks it up.

    Why this is needed: direct_engine computes `_NUM_PAGE_WORKERS` exactly
    once at import time from that env var (default 2). The UI setting
    "MaxConcurrentOCR" previously only controlled the *file-level* thread
    pool size, NOT the actual number of OCR worker processes — so setting it
    to 3 in the UI had no effect on real page parallelism. By mapping the UI
    setting onto the env var here (before any heavy module import), the user
    value becomes the true process count.

    We do NOT override a value the user set explicitly via the environment
    (power-user / portable flag), and we clamp to 1..cpu_count to avoid
    oversubscription. This single OCR pool is shared app-wide, so it governs
    page parallelism for both PDF→Word and Archive Digitization.
    """
    if os.environ.get("DIRECT_OCR_NUM_PAGE_WORKERS"):
        return  # explicit env wins — don't clobber it
    try:
        from scanindex.infra.paths import get_resource_path
        import configparser
        path = get_resource_path("settings.ini")
        if not os.path.exists(path):
            return
        cfg = configparser.ConfigParser()
        cfg.read(path, encoding="utf-8")
        if "OCR" not in cfg:
            return
        raw = cfg["OCR"].get("MaxConcurrentOCR", "").strip()
        if not raw:
            return
        val = max(1, int(raw))
        val = min(val, os.cpu_count() or 4)
        os.environ["DIRECT_OCR_NUM_PAGE_WORKERS"] = str(val)
    except Exception as exc:
        # Never let settings parsing break app startup — the engine simply
        # falls back to its built-in default.
        print(f"[OCR] Warning: could not apply MaxConcurrentOCR: {exc}")


def main():
    multiprocessing.freeze_support()

    # Apply OCR page-worker count from settings.ini BEFORE any heavy module
    # (direct_engine reads DIRECT_OCR_NUM_PAGE_WORKERS once at import time).
    _apply_ocr_page_worker_setting()

    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import Qt

    # Enable High DPI scaling
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    from scanindex.infra.version import get_version_short
    _ver = get_version_short()

    app = QApplication(sys.argv)
    app.setApplicationName("ScanIndex")
    app.setApplicationDisplayName(f"ScanIndex {_ver}")
    app.setApplicationVersion(_ver)

    # Show splash IMMEDIATELY so user sees feedback during the unavoidable
    # Python + Qt + main_window module import (~1.5s on cold start). The splash
    # itself depends only on lightweight modules already imported.
    from scanindex.ui.splash_screen import SplashScreen
    splash = SplashScreen()
    splash.show()
    app.processEvents()

    # Apply theme + font AFTER splash is visible so heavy QSS parse does not
    # delay first paint of the splash.
    from PySide6.QtGui import QFont
    from scanindex.ui.theme import APP_STYLESHEET, FONT_UI
    app.setStyleSheet(APP_STYLESHEET)
    app.setFont(QFont(FONT_UI, 13))
    splash.set_status("Đang khởi tạo giao diện...")
    app.processEvents()

    # Construct the main window. Heavy AI modules (OCR engine, correction
    # model, GMFT, KIE) are loaded lazily by ModelManager only when the user
    # navigates into a screen that requires them.
    from scanindex.ui.main_window import MainWindow
    splash.set_status("Đang chuẩn bị màn hình chính...")
    app.processEvents()
    window = MainWindow()
    window.show()
    splash.close()
    # Keep reference to prevent GC
    app._main_window = window

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
