"""Persistent rotating file log for the whole app.

Why this exists
---------------
The UI LogPanel is in-memory only (capped, lost on exit) and the
infrastructure paths (schema conversion, index rebuild, startup
migration) used bare ``print()`` — which goes nowhere in a windowed
PyInstaller build. Support/diagnostics therefore had *no* durable trail.

Design
------
- One rotating file: ``<base_dir>/logs/app.log`` (2 MB × 3 backups,
  UTF-8). Falls back to ``%TEMP%/scanindex_logs`` when the portable
  folder is read-only.
- The handler is attached to the ``scanindex`` logger at INFO, so every
  ``logging.getLogger("scanindex.…")`` in the codebase lands in the file
  automatically (propagation is disabled there to avoid double-printing
  via third-party root handlers).
- :func:`write` is the escape hatch for code without a logger (the
  startup migration, UI signal mirrors). It echoes to stdout as well, so
  console launches keep their output.
- Lazy + failure-proof: setup never raises; when even the fallback dir
  fails, calls become no-ops.
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LEVEL_MAP = {
    "info": logging.INFO,
    "success": logging.INFO,
    "warning": logging.WARNING,
    "warn": logging.WARNING,
    "err": logging.ERROR,
    "error": logging.ERROR,
    "debug": logging.DEBUG,
}

_logger: logging.Logger | None = None
_setup_tried = False


def _target_dir() -> Path:
    """Logs next to the app (portable) — else the user's temp dir."""
    try:
        from scanindex.infra.paths import get_base_dir

        base = Path(get_base_dir()) / "logs"
        base.mkdir(parents=True, exist_ok=True)
        probe = base / ".write_probe"
        probe.touch(exist_ok=True)
        probe.unlink(missing_ok=True)
        return base
    except Exception:
        pass
    fallback = Path(tempfile.gettempdir()) / "scanindex_logs"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def setup() -> bool:
    """Attach the rotating file handler once. Idempotent; never raises."""
    global _logger, _setup_tried
    if _logger is not None:
        return True
    if _setup_tried:
        return False
    _setup_tried = True
    try:
        handler = RotatingFileHandler(
            _target_dir() / "app.log",
            maxBytes=2_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s | %(message)s"
        ))
        logger = logging.getLogger("scanindex")
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        # scanindex owns its handler; don't double-emit through the root
        # logger's console handlers (when a library configured them).
        logger.propagate = False
        _logger = logger
        logger.info("---- app log session start (pid=%s) ----", os.getpid())
        return True
    except Exception:
        _logger = None
        return False


def write(msg: str, level: str = "info") -> None:
    """File-log one message (also echoed to stdout when a console exists).

    Level accepts the UI LogPanel vocabulary ("err"/"success"/…). Safe to
    call before setup()/from any thread; failures degrade to no-ops.
    """
    lvl = _LEVEL_MAP.get(str(level).lower(), logging.INFO)
    if sys.stdout is not None:
        try:
            print(msg, flush=True)
        except Exception:
            pass
    if not setup():
        return
    try:
        assert _logger is not None
        _logger.log(lvl, str(msg))
    except Exception:
        pass


def log_path() -> Path | None:
    """Path of the active log file (for the Settings "open logs" affordance)."""
    if not setup():
        return None
    for handler in logging.getLogger("scanindex").handlers:
        if isinstance(handler, RotatingFileHandler):
            return Path(handler.baseFilename)
    return None
