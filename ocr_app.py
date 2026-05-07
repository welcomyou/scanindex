"""
Entry point for ScanIndex (PySide6 version).
"""
import os
import sys
import multiprocessing


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


def main():
    multiprocessing.freeze_support()

    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import Qt

    # Enable High DPI scaling
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName("ScanIndex")
    app.setApplicationDisplayName("ScanIndex")

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
    from scanindex.ui.theme import DARK_STYLESHEET, FONT_UI
    app.setStyleSheet(DARK_STYLESHEET)
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
