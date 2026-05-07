"""Centralized model lifecycle: load / release model groups based on which
screen the user is on. Each screen declares required groups; when switching
screens we release groups not needed and load missing ones.

Groups:
  core_ocr        -- ScreenAI DLL process pool (always needed for any OCR run)
  correction      -- Proton CT2 model (Vietnamese text correction)
  table_extraction-- GMFT + Docling v1 table structure models (PDF -> DOCX)
  kie             -- LayoutLMv3 (key information extraction for archive)
  archive_splitter-- LightGBM page splitter status group (loaded outside manager)
"""
from __future__ import annotations

import gc
import logging
import threading
from typing import Callable, Iterable

logger = logging.getLogger(__name__)


GROUP_CORE_OCR = "core_ocr"
GROUP_CORRECTION = "correction"
GROUP_TABLE_EXTRACTION = "table_extraction"
GROUP_KIE = "kie"
GROUP_ARCHIVE_SPLITTER = "archive_splitter"

GROUP_LABELS = {
    GROUP_CORE_OCR: "ScreenAI OCR engine",
    GROUP_CORRECTION: "Proton CT2 correction model",
    GROUP_TABLE_EXTRACTION: "GMFT + Docling table extraction models",
    GROUP_KIE: "KIE inference (LayoutLMv3)",
    GROUP_ARCHIVE_SPLITTER: "LightGBM splitter",
}


class ModelManager:
    """Tracks which model groups are loaded and orchestrates load/release.

    Loaders/releasers are registered by app startup; lookups are case-sensitive."""

    def __init__(self):
        self._loaded: set[str] = set()
        self._lock = threading.RLock()
        self._loaders: dict[str, Callable[[Callable[[str], None]], None]] = {}
        self._releasers: dict[str, Callable[[Callable[[str], None]], None]] = {}

    def register(self, group: str,
                 loader: Callable[[Callable[[str], None]], None],
                 releaser: Callable[[Callable[[str], None]], None] | None = None):
        with self._lock:
            self._loaders[group] = loader
            if releaser is not None:
                self._releasers[group] = releaser

    def is_loaded(self, group: str) -> bool:
        with self._lock:
            return group in self._loaded

    def loaded_groups(self) -> set[str]:
        with self._lock:
            return set(self._loaded)

    def ensure_loaded(self, group: str, log_cb: Callable[[str], None] | None = None) -> bool:
        log_cb = log_cb or (lambda m: None)
        with self._lock:
            if group in self._loaded:
                return True
            loader = self._loaders.get(group)
        if loader is None:
            log_cb(f"[ModelManager] No loader registered for '{group}'")
            return False
        try:
            log_cb(f"Đang tải {GROUP_LABELS.get(group, group)}...")
            loader(log_cb)
        except Exception as e:
            log_cb(f"[ModelManager] Load '{group}' failed: {e}")
            logger.exception("Failed to load %s", group)
            return False
        with self._lock:
            self._loaded.add(group)
        return True

    def release(self, group: str, log_cb: Callable[[str], None] | None = None) -> None:
        log_cb = log_cb or (lambda m: None)
        with self._lock:
            if group not in self._loaded:
                return
            releaser = self._releasers.get(group)
        if releaser is None:
            return
        try:
            log_cb(f"Đang giải phóng {GROUP_LABELS.get(group, group)}...")
            releaser(log_cb)
        except Exception as e:
            log_cb(f"[ModelManager] Release '{group}' failed: {e}")
            logger.exception("Failed to release %s", group)
        with self._lock:
            self._loaded.discard(group)
        gc.collect()

    def reconcile(self, required: Iterable[str], log_cb: Callable[[str], None] | None = None) -> None:
        """Bring the loaded set to exactly `required`: release extras, load missing."""
        required_set = set(required)
        with self._lock:
            currently = set(self._loaded)
        # Release extras first to free RAM before loading new models
        for grp in sorted(currently - required_set):
            self.release(grp, log_cb)
        for grp in sorted(required_set - currently):
            self.ensure_loaded(grp, log_cb)
