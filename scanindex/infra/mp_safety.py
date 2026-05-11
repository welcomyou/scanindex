"""Multiprocessing guards for Windows spawn.

OpenCV's bootstrap briefly prepends ``site-packages/cv2`` to ``sys.path``.
If another thread starts a multiprocessing child during that small window,
Windows spawn copies the polluted path and the child may import
``cv2/typing`` as top-level ``typing`` before our application code runs.
"""
from __future__ import annotations

import os
import sys
import threading
from contextlib import contextmanager

_PATCH_LOCK = threading.Lock()
_PATCHED = False


def _is_cv2_loader_path(path: str) -> bool:
    if not path:
        return False
    try:
        norm = os.path.normcase(os.path.abspath(path))
    except Exception:
        return False
    if os.path.basename(norm) != "cv2":
        return False
    return os.path.exists(os.path.join(norm, "typing", "__init__.py"))


def sanitize_sys_path(paths: list[str] | tuple[str, ...]) -> list[str]:
    """Return ``paths`` without OpenCV's transient loader directory."""
    return [p for p in paths if not _is_cv2_loader_path(p)]


def sanitize_current_sys_path() -> None:
    cleaned = sanitize_sys_path(sys.path)
    if cleaned != sys.path:
        sys.path[:] = cleaned


@contextmanager
def sanitized_sys_path_for_spawn():
    """Temporarily sanitize global ``sys.path`` while creating child workers."""
    original = list(sys.path)
    try:
        sanitize_current_sys_path()
        yield
    finally:
        # Preserve additions made inside the block, but never restore the cv2
        # loader path that can shadow stdlib modules in spawned children.
        sys.path[:] = sanitize_sys_path(sys.path)
        for item in original:
            if item not in sys.path and not _is_cv2_loader_path(item):
                sys.path.append(item)


def patch_multiprocessing_spawn_sys_path() -> None:
    """Filter cv2 loader paths from multiprocessing spawn preparation data."""
    global _PATCHED
    with _PATCH_LOCK:
        if _PATCHED:
            return
        try:
            import multiprocessing.spawn as mp_spawn
        except Exception:
            return
        original = getattr(mp_spawn, "get_preparation_data", None)
        if original is None or getattr(original, "_scanindex_patched", False):
            _PATCHED = True
            return

        def wrapped_get_preparation_data(name):
            data = original(name)
            sys_path = data.get("sys_path")
            if sys_path is not None:
                data["sys_path"] = sanitize_sys_path(sys_path)
            return data

        wrapped_get_preparation_data._scanindex_patched = True
        wrapped_get_preparation_data._scanindex_original = original
        mp_spawn.get_preparation_data = wrapped_get_preparation_data
        _PATCHED = True
