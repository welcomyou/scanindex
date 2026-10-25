"""Parent-owned Windows Job: spawn workers attach by name, then close their handle."""
from __future__ import annotations

import atexit
import ctypes
from ctypes import wintypes
import os
import threading
import time
import uuid

_ENV_JOB_NAME = "OCRTOOL_JOB_NAME"
_primary_handle: int | None = None
_owner_pid: int | None = None
_lock = threading.RLock()
_kernel = None


def available() -> bool:
    return os.name == "nt"


def _k32():
    global _kernel
    if _kernel is None:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            "OpenJobObjectW": ([wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
            "GetCurrentProcess": ([], wintypes.HANDLE),
            "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            "QueryInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
            "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            "IsProcessInJob": ([wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)], wintypes.BOOL),
            "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (args, result) in signatures.items():
            fn = getattr(k, name)
            fn.argtypes, fn.restype = args, result
        _kernel = k
    return _kernel


def _check(ok):
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _BASIC_LIMIT(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD),
    ]


class _EXTENDED_LIMIT(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BASIC_LIMIT), ("IoInfo", _IO_COUNTERS)] + [
        (name, ctypes.c_size_t) for name in (
            "ProcessMemoryLimit", "JobMemoryLimit", "PeakProcessMemoryUsed", "PeakJobMemoryUsed")]


class _ACCOUNTING(ctypes.Structure):
    _fields_ = [(name, ctypes.c_longlong) for name in (
        "TotalUserTime", "TotalKernelTime", "ThisPeriodTotalUserTime", "ThisPeriodTotalKernelTime")] + [
        (name, wintypes.DWORD) for name in (
            "TotalPageFaultCount", "TotalProcesses", "ActiveProcesses", "TotalTerminatedProcesses")]


def ensure_app_job() -> int | None:
    """Create a parent-owned Job; failure prevents native worker creation."""
    global _primary_handle, _owner_pid
    if not available():
        return None
    with _lock:
        if _primary_handle is not None and _owner_pid == os.getpid():
            return _primary_handle
        import multiprocessing
        if multiprocessing.current_process().name != "MainProcess":
            raise RuntimeError("Only the application parent may create the worker Job")
        k = _k32()
        name = "Local\\OCRTool-" + uuid.uuid4().hex
        handle = k.CreateJobObjectW(None, name)
        _check(handle)
        try:
            info = _EXTENDED_LIMIT()
            info.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
            _check(k.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)))
        except BaseException:
            k.CloseHandle(handle)
            raise
        _primary_handle, _owner_pid = handle, os.getpid()
        os.environ[_ENV_JOB_NAME] = name
        os.environ.pop("OCRTOOL_JOB_HANDLE", None)
        return handle


def attach_current_process() -> bool:
    """Attach and verify membership before returning READY; never keep a child handle."""
    if not available():
        return True
    name = os.environ.get(_ENV_JOB_NAME, "")
    if not name:
        return False
    k = _k32()
    handle = k.OpenJobObjectW(0x0001 | 0x0004, False, name)  # ASSIGN_PROCESS | QUERY
    _check(handle)
    try:
        current = k.GetCurrentProcess()
        member = wintypes.BOOL()
        _check(k.IsProcessInJob(current, handle, ctypes.byref(member)))
        if not member.value:
            _check(k.AssignProcessToJobObject(handle, current))
        _check(k.IsProcessInJob(current, handle, ctypes.byref(member)))
        if not member.value:
            raise RuntimeError("Worker Job membership was not confirmed")
    finally:
        _check(k.CloseHandle(handle))
    return True


def active_process_count() -> int:
    if not available() or not _primary_handle:
        return 0
    info = _ACCOUNTING()
    _check(_k32().QueryInformationJobObject(
        _primary_handle, 1, ctypes.byref(info), ctypes.sizeof(info), None))
    return int(info.ActiveProcesses)


def terminate_app_job_tree(timeout_s: float = 2.0) -> bool:
    """Terminate and wait for an empty Job; never create one at shutdown."""
    if not available() or not _primary_handle or _owner_pid != os.getpid():
        return False
    _check(_k32().TerminateJobObject(_primary_handle, 1))
    deadline = time.monotonic() + max(0, timeout_s)
    while True:
        if active_process_count() == 0:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(.02, max(0, deadline - time.monotonic())))


def close_app_job() -> None:
    global _primary_handle, _owner_pid
    with _lock:
        if _primary_handle is not None and _owner_pid == os.getpid():
            _check(_k32().CloseHandle(_primary_handle))
            _primary_handle = _owner_pid = None
            os.environ.pop(_ENV_JOB_NAME, None)


atexit.register(close_app_job)
