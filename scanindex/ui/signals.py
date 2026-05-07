"""
Custom Qt signals for cross-thread communication.
Replaces tkinter's self.after(0, callback) pattern.
"""
from PySide6.QtCore import QObject, Signal


class AppSignals(QObject):
    """Signals emitted from worker threads, received on GUI thread."""

    # File status updates: (list_type, index, status, output_path, corrected_text)
    status_updated = Signal(str, int, str)
    status_with_output = Signal(str, int, str, str)
    status_with_correction = Signal(str, int, str, str, str)

    # Log messages: (message, level)
    log_message = Signal(str, str)

    # Processing lifecycle
    processing_started = Signal()
    processing_finished = Signal()

    # Model initialization
    models_ready = Signal()

    # Per-screen lazy loading (splash overlay)
    screen_load_status = Signal(str)        # status text update
    screen_load_finished = Signal(str)      # function_id that finished loading
    background_model_status = Signal(str, str)  # group, status text
    background_model_finished = Signal(str, bool)  # group, ok

    # Cache update: (list_type, idx, key, value)
    cache_updated = Signal(str, int, str, str)
