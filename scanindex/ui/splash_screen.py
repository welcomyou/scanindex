"""
Splash screen shown during startup while importing heavy dependencies.
Uses QDialog instead of QSplashScreen for more control.
"""
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QProgressBar
)
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QFont

from scanindex.ui.theme import (
    COLOR_SURFACE, COLOR_ACCENT, COLOR_TEXT_MUTED, COLOR_HOVER,
    COLOR_BORDER_DEFAULT, FONT_UI, SP, RADIUS_LG
)
from scanindex.infra import translations


class ImportThread(QThread):
    """Background thread that imports heavy dependencies."""
    error_occurred = Signal(str)
    status_update = Signal(str)  # emits human-friendly load step (library name)

    def __init__(self, import_func):
        super().__init__()
        self._import_func = import_func

    def run(self):
        try:
            # import_func may accept a status callback; pass it if it does
            try:
                self._import_func(lambda msg: self.status_update.emit(msg))
            except TypeError:
                self._import_func()
        except Exception as e:
            self.error_occurred.emit(str(e))


class SplashScreen(QDialog):
    """Splash dialog shown during app startup."""

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.FramelessWindowHint |
                         Qt.WindowType.WindowStaysOnTopHint)
        self.setFixedSize(360, 130)
        self.setStyleSheet(
            f"QDialog {{ "
            f"  background: {COLOR_SURFACE}; "
            f"  border: 1px solid {COLOR_BORDER_DEFAULT}; "
            f"  border-radius: {RADIUS_LG}px; "
            f"}}"
        )
        self._center_on_screen()
        self._setup_ui()
        self._thread = None

    def _center_on_screen(self):
        from PySide6.QtWidgets import QApplication
        screen = QApplication.primaryScreen()
        if screen:
            geo = screen.availableGeometry()
            x = (geo.width() - self.width()) // 2 + geo.x()
            y = (geo.height() - self.height()) // 2 + geo.y()
            self.move(x, y)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(SP[6], SP[6], SP[6], SP[5])
        layout.setSpacing(SP[1])

        # App name
        lbl_name = QLabel(translations.get_text("about_app_name"))
        lbl_name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl_name.setFont(QFont(FONT_UI, 22, QFont.Weight.Bold))
        lbl_name.setStyleSheet(f"color: {COLOR_ACCENT}; background: transparent;")
        layout.addWidget(lbl_name)

        # Status text
        self.lbl_status = QLabel(translations.get_text("splash_loading"))
        self.lbl_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_status.setFont(QFont(FONT_UI, 12))
        self.lbl_status.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; background: transparent;")
        layout.addWidget(self.lbl_status)

        # Progress bar (indeterminate)
        self.progress = QProgressBar()
        self.progress.setFixedHeight(3)
        self.progress.setRange(0, 0)  # Indeterminate
        self.progress.setTextVisible(False)
        self.progress.setStyleSheet(
            f"QProgressBar {{ "
            f"  background: {COLOR_HOVER}; "
            f"  border: none; border-radius: 2px; "
            f"}}"
            f"QProgressBar::chunk {{ "
            f"  background: {COLOR_ACCENT}; "
            f"  border-radius: 2px; "
            f"}}"
        )
        layout.addWidget(self.progress)

    def start_imports(self, import_func, on_done):
        """Start background import thread. Calls on_done() when finished.

        `import_func` may accept an optional status callback (str -> None)
        that updates the splash status line with the library being loaded."""
        self._on_done = on_done
        self._thread = ImportThread(import_func)
        self._thread.finished.connect(self._on_thread_done)
        self._thread.error_occurred.connect(self._on_error)
        self._thread.status_update.connect(self.set_status)
        self._thread.start()

    def set_status(self, text: str):
        """Update the status line shown beneath the app name."""
        self.lbl_status.setText(text)

    def _on_thread_done(self):
        self._on_done()

    def _on_error(self, msg):
        self.lbl_status.setText(f"Error: {msg}")
        self.lbl_status.setStyleSheet(f"color: red; background: transparent;")
