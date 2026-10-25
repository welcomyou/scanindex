# -*- coding: utf-8 -*-
"""Prep process pool — GĐ P (PLAN v5.1 §4.4 P-b).

Xoay/deskew (`pre_process_pdf`, 4 nhánh A/B/C/D) chạy trong PROCESS riêng sở
hữu Document fitz của mình — tách khỏi file-worker thread (không còn fitz
dùng chéo không khóa giữa các file) và GỐI được với OCR của nhóm trang trước.

Hợp đồng tài nguyên (REVIEW_READINESS C + review W/K/P 22/10):
- Số process: `OCRTOOL_PREP_PROCESSES` (khởi điểm 2 — điểm thử, không phải
  cấu hình tối ưu); cửa sổ job chưa hoàn tất tối đa `2 × P_prep`, chờ cửa sổ
  CÓ DEADLINE (không chờ vô hạn).
- Native job chỉ ghi file STAGING riêng (không bao giờ ghi `dst_pdf` trực
  tiếp — P1-04: trước đây timeout nhả lease trong khi native còn ghi cùng
  output với fallback inline, hai writer một file); publish (rename) chỉ khi
  future kết thúc thành công.
- Lease input/staging giữ TỚI KHI future THẬT KẾT THÚC (P1-04: timeout của
  vòng `result(timeout)` KHÔNG chứng minh native dừng — future chạy hết và
  kết quả bị bỏ, lease do done-callback nhả).
- Lỗi executor/timeout → `handled=False`; caller giữ file resumable,
  KHÔNG mở thêm native prep trong parent ngoài trần process.
Module top-level, không import Qt/screen — an toàn Windows spawn.
"""
from __future__ import annotations

import atexit
import os
import threading
import time
import uuid

_lock = threading.Lock()
_executor = None
_window: threading.BoundedSemaphore | None = None
_inflight = 0
_executor_factory = None  # test inject (ThreadPool); None = ProcessPool thật
_owned_processes: dict = {}

# Deadline chờ cửa sổ prep khi 2×P_prep slot đều bận — hết deadline trả
# handled=False (resumable), không treo file-worker vô hạn.
_WINDOW_WAIT_DEFAULT_S = 120.0


def _shutdown_at_exit() -> None:
    shutdown(wait=False)


atexit.register(_shutdown_at_exit)


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def processes() -> int:
    return _env_int("OCRTOOL_PREP_PROCESSES", 2)


def enabled() -> bool:
    """Bật prep process — mặc định OFF (đường inline giữ nguyên)."""
    return os.environ.get("OCRTOOL_PREP_MODE", "").strip().lower() == "prep-process"


def _window_slots() -> int:
    return 2 * processes()


def _window_wait_s() -> float:
    raw = os.environ.get("OCRTOOL_PREP_WINDOW_WAIT_S", "").strip()
    if raw:
        try:
            return max(1.0, float(raw))
        except ValueError:
            pass
    return _WINDOW_WAIT_DEFAULT_S


def _prep_init() -> None:
    """Initializer pool prep (D1): stdio an toàn + TỰ-GÁN Windows Job của
    parent TRƯỚC mọi import native — không có worker prep chạy ngoài tree."""
    from scanindex.infra.bootstrap import bootstrap_worker
    bootstrap_worker()


def _ensure_executor():
    global _executor, _window
    from scanindex.core.runtime.shutdown import accepting_work
    if not accepting_work():
        raise RuntimeError("Runtime is stopping")
    with _lock:
        if _executor is None:
            if _executor_factory is not None:
                _executor = _executor_factory(processes())
            else:
                import multiprocessing
                from concurrent.futures import ProcessPoolExecutor
                from scanindex.infra.windows_job import ensure_app_job
                ensure_app_job()

                ctx = multiprocessing.get_context("spawn")
                _executor = ProcessPoolExecutor(
                    max_workers=processes(), mp_context=ctx,
                    initializer=_prep_init,
                )
            _window = threading.BoundedSemaphore(_window_slots())
        return _executor


def prep_chunk(src_pdf: str, dst_pdf: str, max_workers: int | None = None):
    """Chạy TRONG process prep: ĐÚNG hàm preprocessing hiện tại — parity theo
    cấu trúc (cùng tham số, cùng hậu xử lý), không tái cài đặt transform.

    `dst_pdf` ở đây là file STAGING của job — native không bao giờ ghi trực
    tiếp vào output cuối của caller (P1-04)."""
    from scanindex.core.preprocessing import preprocessing

    return preprocessing.pre_process_pdf(
        src_pdf,
        dst_pdf,
        update_callback=None,
        debug_mode=False,
        max_workers=min(max(1, int(max_workers or 4)),
                        _env_int("OCRTOOL_PREP_INNER_WORKERS", 4),
                        max(1, os.cpu_count() or 4)),
        return_metadata=True,
    )


class _JobResources:
    """Lease input + staging + slot cửa sổ của MỘT job prep. Nhả ĐÚNG MỘT
    LẦN dù bằng đường thành công hay done-callback của future chạy quá
    timeout (P1-04: lease thuộc native job, không thuộc vòng chờ của caller)."""

    def __init__(self, src_pdf: str, staging: str, window):
        self.src_pdf = src_pdf
        self.staging = staging
        self.window = window
        self._lock = threading.Lock()
        self._released = False

    def release(self) -> None:
        from scanindex.core.ocr.direct_engine import (
            release_input_lease,
        )

        with self._lock:
            if self._released:
                return
            self._released = True
        release_input_lease(self.staging)
        release_input_lease(self.src_pdf)
        if self.window is not None:
            try:
                self.window.release()
            except ValueError:
                pass
        global _inflight
        with _lock:
            _inflight = max(0, _inflight - 1)

    def defer_until_future_done(self, fut) -> None:
        """Future CHƯA kết thúc (timeout/cancel của caller) — đăng ký dọn sau
        khi native thật kết thúc: nhả lease/window, xóa staging (kết quả muộn
        bị bỏ, caller đã đi inline)."""
        import logging

        logger = logging.getLogger(__name__)

        def _cleanup(_fut=None) -> None:
            self.release()
            try:
                if os.path.exists(self.staging):
                    os.remove(self.staging)
            except OSError:
                logger.warning(
                    "prep staging muộn không xóa được: %s", self.staging
                )

        try:
            fut.add_done_callback(_cleanup)
        except Exception:
            _cleanup()


def run_prep(src_pdf: str, dst_pdf: str, max_workers: int | None = None,
             timeout: float = 600.0,
             cancel_event: threading.Event | None = None
             ) -> tuple[bool, dict | None, str | None]:
    """Chạy một chunk prep qua pool. Trả `(handled, meta, error)`:
    - handled=True: `dst_pdf` đã sẵn sàng (meta chứa `page_rotations` nếu có).
    - handled=False: error nêu lý do — caller giữ file resumable. Khi lý
      do là timeout/cancel, native prep CÓ THỂ VẪN CHẠY: input giữ lease tới
      khi job thật kết thúc, native chỉ ghi staging riêng (không đụng
      `dst_pdf`). Không chạy thêm prep inline."""
    from scanindex.core.ocr.direct_engine import (
        acquire_input_lease,
        release_input_lease,
    )

    try:
        ex = _ensure_executor()
    except Exception as exc:
        return False, None, f"{type(exc).__name__}: {exc}"
    window = _window

    # Cửa sổ 2×P_prep có DEADLINE + hủy được (P1-04: trước đây acquire chờ
    # vô hạn). Hết deadline → handled=False (inline) không giữ tài nguyên nào.
    window_deadline = time.monotonic() + min(float(timeout), _window_wait_s())
    acquired = False
    while True:
        if cancel_event is not None and cancel_event.is_set():
            return False, None, "canceled: bị hủy trước khi chạy prep"
        if window is None:
            acquired = True
            break
        if window.acquire(blocking=True, timeout=0.1):
            acquired = True
            break
        if time.monotonic() > window_deadline:
            return False, None, "cửa sổ prep đầy quá hạn — giữ file để thử lại"

    global _inflight
    with _lock:
        _inflight += 1
    # P1-04: native ghi STAGING riêng — không bao giờ chạm `dst_pdf` của
    # caller; publish (rename) chỉ khi future thành công.
    staging = f"{dst_pdf}.prep-{uuid.uuid4().hex}.part"
    acquire_input_lease(src_pdf)
    acquire_input_lease(staging)
    res = _JobResources(src_pdf, staging, window if acquired else None)
    fut = None
    try:
        try:
            fut = ex.submit(prep_chunk, src_pdf, staging, max_workers)
            _remember_executor(ex)
        except Exception as exc:
            res.release()
            try:
                if os.path.exists(staging):
                    os.remove(staging)
            except OSError:
                pass
            return False, None, f"{type(exc).__name__}: {exc}"
        if cancel_event is not None and cancel_event.is_set():
            if fut.cancel():
                res.release()
                try:
                    if os.path.exists(staging):
                        os.remove(staging)
                except OSError:
                    pass
                return False, None, "canceled: bị hủy trước khi prep bắt đầu"
        # P2-3 (review 2): chờ theo LÁT ngắn và phản ứng với cancel_event —
        # một `fut.result(timeout=600)` duy nhất không phản ứng với việc
        # người dùng bấm Stop sau khi job đã bắt đầu.
        import concurrent.futures as _cf

        deadline = time.monotonic() + float(timeout)
        result = None
        while True:
            slice_s = min(0.5, max(0.05, deadline - time.monotonic()))
            done_set, _ = _cf.wait([fut], timeout=slice_s)
            if fut in done_set:
                try:
                    result = fut.result()
                except Exception as exc:
                    # Timeout/cancel CỦA VÒNG CHỜ không kết thúc future:
                    # Idempotent cleanup owns leases/staging on error too.
                    res.defer_until_future_done(fut)
                    return False, None, (
                        f"{type(exc).__name__}: {exc} (native prep có thể "
                        "còn chạy — input giữ lease tới khi xong; giữ file "
                        "để lượt sau thử lại)"
                    )
                break
            if cancel_event is not None and cancel_event.is_set():
                if fut.cancel():
                    # Chưa kịp chạy — native chắc chắn không chạy: dọn ngay.
                    res.release()
                    try:
                        if os.path.exists(staging):
                            os.remove(staging)
                    except OSError:
                        pass
                    return False, None, (
                        "canceled: bị hủy trong lúc prep chờ chạy"
                    )
                # R2-04: future ĐANG CHẠY thật — trả caller NGAY với trạng
                # thái canceled (không chờ deadline gốc — caller thật dùng
                # timeout 600 s, Stop sẽ bị kẹt gần 10 phút); lease/window
                # giữ tới khi future thật kết thúc qua done-callback, native
                # không bị tuyên bố dừng oan.
                res.defer_until_future_done(fut)
                return False, None, (
                    "canceled: bị hủy khi prep đang chạy — input giữ lease "
                    "tới khi native kết thúc; caller KHÔNG fallback inline"
                )
            if time.monotonic() >= deadline:
                res.defer_until_future_done(fut)
                return False, None, (
                    f"TimeoutError: prep quá {timeout:.0f}s (native prep có "
                    "thể còn chạy — input giữ lease tới khi xong; giữ file "
                    "để lượt sau thử lại)"
                )
        ok = bool(result[0]) if isinstance(result, tuple) else bool(result)
        meta = {}
        if isinstance(result, tuple) and len(result) >= 3:
            meta = result[2] or {}
        # Future đã kết thúc → native chứng minh dừng → nhả lease an toàn.
        res.release()
        if ok and os.path.exists(staging):
            os.replace(staging, dst_pdf)
            return True, meta, None
        try:
            if os.path.exists(staging):
                os.remove(staging)
        except OSError:
            pass
        return False, None, f"prep không tạo output (ok={ok})"
    except BaseException:
        if fut is not None and not fut.done():
            res.defer_until_future_done(fut)
        else:
            res.release()
        raise


def telemetry() -> dict:
    with _lock:
        active = _executor is not None
        max_p = processes()
        inflight = _inflight
        window = _window
    return {
        "enabled": enabled(),
        "processes": max_p,
        "window": _window_slots(),
        # P2-7: số job ĐANG CHẠY thật (trước đây đọc `_value` semaphore — số
        # slot RẢNH, báo ngược).
        "in_flight": inflight,
        "window_free": getattr(window, "_value", 0) if window else 0,
        "active": active,
    }


def shutdown(wait: bool = False) -> None:
    global _executor, _window
    with _lock:
        ex = _executor
        _executor = None
        _window = None
    if ex is not None:
        _remember_executor(ex)
        ex.shutdown(wait=wait)


def _remember_executor(ex):
    from scanindex.core.runtime.shutdown import track_executor
    for proc in track_executor(ex):
        _owned_processes[id(proc)] = proc


def _prep_stage_stopper(soft: bool) -> bool:
    """D1 stopper stage P: soft = ngừng nhận + cancel futures; hard =
    terminate process prep (native treo — dừng hữu hạn)."""
    global _executor, _window
    with _lock:
        ex = _executor
        if not soft:
            _executor = None
            _window = None
    if soft:
        if ex is not None:
            _remember_executor(ex)
            ex.shutdown(wait=False, cancel_futures=True)
        return True
    _remember_executor(ex)
    from scanindex.core.runtime.shutdown import terminate_processes
    return terminate_processes(list(_owned_processes.values()))


from scanindex.core.runtime.shutdown import register_stage_stopper  # noqa: E402

register_stage_stopper("P-prep-pool", _prep_stage_stopper)
