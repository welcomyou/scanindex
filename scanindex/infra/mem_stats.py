"""Best-effort memory snapshot for diagnostics (Windows).

Dùng để trả lời câu hỏi "app có phình RAM trước khi chết không" — log
định kỳ trong lúc quét (main + các worker con) rồi đối chiếu với thời
điểm crash native (Event ID 1000, mã 0xc0000409).

Không phụ thuộc psutil: GlobalMemoryStatusEx cho áp lực commit toàn hệ
thống, GetProcessMemoryInfo cho RSS từng process (main + các con của
multiprocessing). Mọi thứ đều try/except — không bao giờ làm rơi quét;
trả về None khi môi trường không hỗ trợ (non-Windows, missing APIs).
"""
from __future__ import annotations

import ctypes
import os
import sys


def _fmt_gb(value: int) -> str:
    return f"{value / (1024 ** 3):.1f} GB"


def _fmt_mb(value: int) -> str:
    return f"{value / (1024 ** 2):.0f} MB"


def _global_memory_line() -> str | None:
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_uint32),
            ("dwMemoryLoad", ctypes.c_uint32),
            ("ullTotalPhys", ctypes.c_uint64),
            ("ullAvailPhys", ctypes.c_uint64),
            ("ullTotalPageFile", ctypes.c_uint64),
            ("ullAvailPageFile", ctypes.c_uint64),
            ("ullTotalVirtual", ctypes.c_uint64),
            ("ullAvailVirtual", ctypes.c_uint64),
            ("ullAvailExtendedVirtual", ctypes.c_uint64),
        ]

    try:
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return None
        return (
            f"RAM load {stat.dwMemoryLoad}%, "
            f"commit còn {_fmt_gb(stat.ullAvailPageFile)} "
            f"/ tổng {_fmt_gb(stat.ullTotalPageFile)}"
        )
    except Exception:
        return None


def _process_rss(pid: int) -> int | None:
    """Working set (RSS) của một pid, đơn vị bytes; None nếu không đọc được."""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_uint32),
            ("PageFaultCount", ctypes.c_uint32),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    try:
        kernel32 = ctypes.windll.kernel32
        psapi = ctypes.windll.psapi
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return None
        try:
            counters = PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
            if not psapi.GetProcessMemoryInfo(
                handle, ctypes.byref(counters), counters.cb
            ):
                return None
            return int(counters.WorkingSetSize)
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


def memory_snapshot_text() -> str | None:
    """Một dòng tóm tắt RAM: toàn hệ thống + RSS của main và các worker con.

    Ví dụ:
        RAM load 37%, commit còn 41.2 GB / tổng 131.0 GB ·
        RSS: main 812 MB, worker pid=12345 1.2 GB, worker pid=12346 1.1 GB
    """
    if sys.platform != "win32":
        return None
    parts: list[str] = []
    global_line = _global_memory_line()
    if global_line:
        parts.append(global_line)

    rss_parts: list[str] = []
    main_rss = _process_rss(os.getpid())
    if main_rss is not None:
        rss_parts.append(f"main {_fmt_mb(main_rss)}")
    try:
        import multiprocessing

        for child in multiprocessing.active_children():
            rss = _process_rss(child.pid)
            if rss is not None:
                rss_parts.append(f"worker pid={child.pid} {_fmt_mb(rss)}")
    except Exception:
        pass
    if rss_parts:
        parts.append("RSS: " + ", ".join(rss_parts))
    if not parts:
        return None
    return " · ".join(parts)
