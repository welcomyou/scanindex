"""Windows process-tree counters without a new runtime dependency."""
from __future__ import annotations

import ctypes as c
from ctypes import wintypes as w
import os


class _ENTRY(c.Structure):
    _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("pid", w.DWORD),
                ("heap", c.c_size_t), ("module", w.DWORD), ("threads", w.DWORD),
                ("parent", w.DWORD), ("priority", w.LONG), ("flags", w.DWORD),
                ("exe", w.WCHAR * 260)]


class _MEMORY(c.Structure):
    _fields_ = [("cb", w.DWORD), ("faults", w.DWORD)] + [
        (name, c.c_size_t) for name in ("peak_rss", "rss", "peak_paged", "paged",
        "peak_nonpaged", "nonpaged", "pagefile", "peak_pagefile", "private")]


class _STATUS(c.Structure):
    _fields_ = [("length", w.DWORD), ("load", w.DWORD)] + [
        (name, c.c_ulonglong) for name in ("total", "available", "page_total",
        "page_available", "virtual_total", "virtual_available", "extended")]


def _bind(dll, name, args, result):
    fn = getattr(dll, name)
    fn.argtypes, fn.restype = args, result
    return fn


def _check(ok):
    if not ok:
        raise c.WinError(c.get_last_error())


def sample_tree(root_pid: int | None = None) -> dict:
    if os.name != "nt":
        raise RuntimeError("Windows process counters unavailable on this platform")
    root_pid = root_pid or os.getpid()
    k = c.WinDLL("kernel32", use_last_error=True)
    ps = c.WinDLL("psapi", use_last_error=True)
    close = _bind(k, "CloseHandle", [w.HANDLE], w.BOOL)
    snapshot = _bind(k, "CreateToolhelp32Snapshot", [w.DWORD, w.DWORD], w.HANDLE)(2, 0)
    if snapshot == c.c_void_p(-1).value:
        _check(False)
    entries = {}
    try:
        first = _bind(k, "Process32FirstW", [w.HANDLE, c.POINTER(_ENTRY)], w.BOOL)
        next_entry = _bind(k, "Process32NextW", [w.HANDLE, c.POINTER(_ENTRY)], w.BOOL)
        row = _ENTRY()
        row.dwSize = c.sizeof(row)
        _check(first(snapshot, c.byref(row)))
        while True:
            entries[int(row.pid)] = (int(row.parent), str(row.exe))
            if not next_entry(snapshot, c.byref(row)):
                if c.get_last_error() != 18:  # ERROR_NO_MORE_FILES
                    _check(False)
                break
    finally:
        close(snapshot)
    owned = {root_pid}
    while True:
        children = {pid for pid, (parent, _) in entries.items() if parent in owned}
        if children <= owned:
            break
        owned |= children
    open_process = _bind(k, "OpenProcess", [w.DWORD, w.BOOL, w.DWORD], w.HANDLE)
    memory = _bind(ps, "GetProcessMemoryInfo", [w.HANDLE, c.POINTER(_MEMORY), w.DWORD], w.BOOL)
    times = _bind(k, "GetProcessTimes", [w.HANDLE] + [c.POINTER(w.FILETIME)] * 4, w.BOOL)
    result, errors = [], {}
    for pid in sorted(owned):
        handle = open_process(0x0400 | 0x0010, False, pid)
        if not handle:
            errors[str(pid)] = c.get_last_error()
            continue
        try:
            counters = _MEMORY()
            counters.cb = c.sizeof(counters)
            _check(memory(handle, c.byref(counters), counters.cb))
            created, exited, kernel, user = (w.FILETIME() for _ in range(4))
            _check(times(handle, c.byref(created), c.byref(exited), c.byref(kernel), c.byref(user)))
            ticks = lambda ft: (int(ft.dwHighDateTime) << 32) | int(ft.dwLowDateTime)
            result.append({"pid": pid, "parent_pid": entries.get(pid, (0, ""))[0],
                           "exe": entries.get(pid, (0, ""))[1],
                           "identity": f"{pid}:{ticks(created)}",
                           "cpu_s": (ticks(kernel) + ticks(user)) / 10_000_000,
                           "rss_mb": counters.rss / 1024**2,
                           "private_mb": counters.private / 1024**2,
                           "peak_rss_mb": counters.peak_rss / 1024**2})
        except OSError as exc:
            errors[str(pid)] = str(exc)
        finally:
            close(handle)
    status = _STATUS()
    status.length = c.sizeof(status)
    _check(_bind(k, "GlobalMemoryStatusEx", [c.POINTER(_STATUS)], w.BOOL)(c.byref(status)))
    parent = next((p for p in result if p["pid"] == root_pid), None)
    if parent is None:
        raise RuntimeError(f"Cannot read parent process counters: {errors}")
    return {"parent_rss_mb": parent["rss_mb"], "tree_rss_mb": sum(p["rss_mb"] for p in result),
            "tree_private_mb": sum(p["private_mb"] for p in result),
            "available_ram_mb": status.available / 1024**2, "processes": result,
            "process_errors": errors}
