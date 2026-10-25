"""Bounded shutdown with retained process ownership and verified exit."""
from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)
_lock = threading.RLock()
_stoppers: list = []
_owned: dict = {}
_shutting_down = False
_result: bool | None = None
_finished = threading.Event()
GRACE_DEFAULT_S = 3.0
DEADLINE_DEFAULT_S = 10.0


def accepting_work() -> bool:
    with _lock:
        return not _shutting_down


def register_stage_stopper(name: str, stopper) -> None:
    with _lock:
        if not any(n == name for n, _ in _stoppers):
            _stoppers.append((name, stopper))


def _alive(proc) -> bool:
    try:
        return proc.is_alive()
    except ValueError:
        # multiprocessing.Process.close() is permitted only after exit.
        return not getattr(proc, "_closed", False)


def track_executor(executor) -> list:
    """Snapshot before Executor.shutdown clears _processes; retain across rebuilds."""
    from multiprocessing.process import BaseProcess
    if executor is None:
        return []
    processes = getattr(executor, "_processes", None)
    if isinstance(processes, dict):
        procs = list(processes.values())
    else:
        pool = getattr(executor, "_pool", None)
        procs = list(pool) if isinstance(pool, (list, tuple)) else []
    with _lock:
        for key, proc in list(_owned.items()):
            if not _alive(proc):
                _owned.pop(key, None)
        for proc in procs:
            if isinstance(proc, BaseProcess) and proc.pid is not None:
                _owned[id(proc)] = proc
    return [p for p in procs if isinstance(p, BaseProcess)]


def terminate_processes(procs) -> bool:
    ok = True
    for proc in procs:
        try:
            if _alive(proc):
                proc.terminate()
        except Exception:
            ok = False
            logger.exception("Cannot terminate owned worker pid=%s", getattr(proc, "pid", None))
    return ok


def _phase(stages, soft: bool, deadline: float) -> bool:
    """Run callbacks off the caller; a stuck callback cannot consume the whole close."""
    pending = []
    for name, stopper in stages:
        event, outcome = threading.Event(), []
        def invoke(fn=stopper, done=event, result=outcome, stage=name):
            try:
                result.append(fn(soft) is not False)
            except Exception:
                logger.exception("Stage %s shutdown soft=%s failed", stage, soft)
                result.append(False)
            finally:
                done.set()
        threading.Thread(target=invoke, name=f"stop-{name}", daemon=True).start()
        pending.append((event, outcome))
    ok = True
    for event, result in pending:
        if not event.wait(max(0, deadline - time.monotonic())) or not result or not result[0]:
            ok = False
    return ok


def shutdown_all(deadline_s: float = DEADLINE_DEFAULT_S,
                 grace_s: float = GRACE_DEFAULT_S) -> bool:
    """True only after hard callbacks finish and all retained workers exit in time."""
    global _shutting_down, _result
    deadline = time.monotonic() + max(0, float(deadline_s))
    with _lock:
        owner = not _shutting_down
        if owner:
            _shutting_down = True
            stages = list(_stoppers)
    if not owner:
        _finished.wait(max(0, deadline - time.monotonic()))
        with _lock:
            return _finished.is_set() and _result is True
    confirmed = False
    try:
        soft_end = min(deadline, time.monotonic() + max(0, grace_s))
        soft_ok = _phase(stages, True, soft_end)
        while time.monotonic() < soft_end:
            with _lock:
                live = [p for p in _owned.values() if _alive(p)]
            if not live:
                break
            time.sleep(min(.02, max(0, soft_end - time.monotonic())))
        # Leave time for the independent Job kill and process-exit confirmation.
        hard_end = time.monotonic() + max(0, deadline - time.monotonic()) / 2
        hard_ok = _phase(stages, False, hard_end)
        with _lock:
            owned = list(_owned.values())
        terminate_ok = terminate_processes(owned)
        from scanindex.infra import windows_job
        job_ok = True
        if windows_job.available() and windows_job._primary_handle is not None:
            job_ok = windows_job.terminate_app_job_tree(max(0, deadline - time.monotonic()))
        for proc in owned:
            try:
                proc.join(max(0, deadline - time.monotonic()))
            except ValueError:
                if not getattr(proc, "_closed", False):
                    raise
        confirmed = (
            soft_ok and hard_ok and terminate_ok and job_ok
            and all(not _alive(proc) for proc in owned)
            and time.monotonic() <= deadline
        )
        with _lock:
            if confirmed:
                _owned.clear()
        return confirmed
    except Exception:
        logger.exception("Runtime shutdown could not confirm exit")
        return False
    finally:
        with _lock:
            _result = confirmed
            _finished.set()


def reset_for_tests() -> None:
    global _shutting_down, _result
    with _lock:
        _stoppers[:] = [(n, fn) for n, fn in _stoppers if not n.startswith("test-")]
        _shutting_down, _result = False, None
        _finished.clear()
        for key, proc in list(_owned.items()):
            if not _alive(proc):
                _owned.pop(key, None)
