# -*- coding: utf-8 -*-
"""Bootstrap dùng chung cho app / harness / worker (D4).

Vấn đề (review chốt §7): các script smoke/benchmark bỏ qua bootstrap của
``ocr_app.py`` → import-order lỗi LightGBM (PySide6 screen trước six/pandas)
bị quy nhầm cho app thật; worker spawn/frozen cần gắn Windows Job + stdio an
toàn TRƯỚC mọi import native, không được import Qt/screen và không được chạy
migration/UI.

Cách chốt: tách bootstrap thành module này — entry app, harness đo, và mọi
worker entry GỌI CHUNG; assert không còn cảnh báo import bị che ở các mẫu
nghiệm thu. ``bootstrap_worker()`` tối thiểu và không side-effect UI.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def patch_six_meta_path_importer() -> None:
    """Avoid PyInstaller/PySide6 inspect crashes on Python 3.12 — phải chạy
    TRƯỚC khi import pandas/PySide6 (six._SixMetaPathImporter thiếu _path
    làm inspect.getsource crash qua shibokensupport → dateutil/six)."""
    try:
        import six
    except Exception:
        return
    import sys

    for importer in sys.meta_path:
        if (
            importer.__class__.__name__ == "_SixMetaPathImporter"
            and not hasattr(importer, "_path")
        ):
            importer._path = []


def apply_cpu_only_env() -> None:
    """FORCE CPU ONLY — phải trước mọi torch import."""
    os.environ["CUDA_VISIBLE_DEVICES"] = ""
    os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"


def bootstrap_app() -> None:
    """Bootstrap ĐẦY ĐỦ cho entry app + harness đo: stdio, sys.path, migration
    config/version, portable paths, app log, CPU-only, six patch + pre-import
    pandas TRƯỚC PySide6. Thứ tự KHÔNG được đổi (module-level reads của
    theme/signing chạy khi import)."""
    from scanindex.infra.mp_safety import (
        ensure_valid_stdio,
        patch_multiprocessing_spawn_sys_path,
        sanitize_current_sys_path,
    )

    ensure_valid_stdio()
    sanitize_current_sys_path()
    patch_multiprocessing_spawn_sys_path()

    # Version-per-file migration PHẢI chạy trước mọi module đọc settings/sign
    # config lúc import, và trước ensure_runtime_config_files (seeding race).
    try:
        from scanindex.infra.data_versioning import run_startup_migration
    except Exception as exc:
        print(f"[Versioning] Warning: migration module import failed: {exc}")
    else:
        try:
            run_startup_migration()
        except Exception as exc:
            print(f"[Versioning] Warning: startup migration failed: {exc}")

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

    try:
        from scanindex.infra import app_log
        app_log.setup()
    except Exception:
        pass

    apply_cpu_only_env()

    # Pre-import pandas BEFORE PySide6 (shiboken circular import crash).
    try:
        patch_six_meta_path_importer()
        import pandas  # noqa: F401
    except ImportError:
        pass


def bootstrap_worker() -> None:
    """Bootstrap worker spawn/frozen: stdio an toàn + TỰ-GÁN Windows Job
    (ownership tree, D1) TRƯỚC mọi import native. KHÔNG import Qt/screen,
    KHÔNG chạy migration — child không được dựng pool đệ quy hay UI."""
    try:
        from scanindex.infra.mp_safety import ensure_valid_stdio
        ensure_valid_stdio()
    except Exception:
        pass
    from scanindex.infra import windows_job
    import multiprocessing
    if multiprocessing.current_process().name == "MainProcess":
        return  # A direct smoke/synchronous call must never put the app parent in its worker Job.
    if windows_job.available() and not windows_job.attach_current_process():
        raise RuntimeError("Worker cannot load native code outside its parent's Job")
    patch_six_meta_path_importer()
    from scanindex.infra import app_log
    app_log.setup(worker=True)


def bootstrap_harness() -> None:
    """Bootstrap cho harness đo/bench: giống app (để parity import-order,
    LightGBM) + tạo Windows Job ở vai parent cho các worker pool."""
    bootstrap_app()
    from scanindex.infra import windows_job
    windows_job.ensure_app_job()
