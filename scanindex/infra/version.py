"""Auto-versioning từ git tags (SemVer).

Pattern lấy từ d:/App/asr-vn/core/version.py.

Cách dùng:
    from scanindex.infra.version import get_version
    version = get_version()   # "1.0.0" hoặc "1.0.0+3.a3f8c0e"

Cách đánh số:
    git tag v1.0.0           → build ra "1.0.0"        (release chính thức)
    commit thêm 3 lần        → build ra "1.0.0+3.a3f8c" (dev build)
    git tag v1.1.0           → build ra "1.1.0"        (release mới)

    MAJOR  — thay đổi lớn, breaking change
    MINOR  — tính năng mới
    PATCH  — sửa bug, chỉnh nhỏ
    +BUILD — tự động: số commits sau tag + git hash (chỉ khi dev build)

Portable build:
    build_portable.bat ghi version vào VERSION file ở project root TRƯỚC
    khi chạy PyInstaller. Spec bundle file đó. Khi chạy portable
    (không có .git), get_version() đọc từ VERSION file.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

# Fallback nếu không có git và không có VERSION file
_FALLBACK_VERSION = "1.0.0"

_cached_version: str | None = None


def _project_root() -> Path:
    """Project root = 3 levels up from scanindex/infra/version.py."""
    return Path(__file__).resolve().parents[2]


def get_version() -> str:
    """Trả về version string. Cache sau lần gọi đầu.

    Tier order:
      1. git describe — when running inside a working .git tree, the tag
         is the source of truth. A stale VERSION file left at project
         root from a previous build_portable run must NOT shadow the
         current tag (was the cause of v1.0.1 build producing
         dist\\ScanIndex-1.0.0\\).
      2. VERSION file — only used when git is unavailable (portable
         frozen bundle has no .git).
      3. Hard-coded fallback.
    """
    global _cached_version
    if _cached_version is not None:
        return _cached_version

    # 1. Thử đọc từ git (dev tree)
    version = _read_git_version()
    if version:
        _cached_version = version
        return version

    # 2. Thử đọc từ VERSION file (portable bundle without .git)
    version = _read_version_file()
    if version:
        _cached_version = version
        return version

    # 3. Fallback
    _cached_version = _FALLBACK_VERSION
    return _cached_version


def _read_version_file() -> str | None:
    """Đọc VERSION file nếu tồn tại (portable build)."""
    version_file = _project_root() / "VERSION"
    if not version_file.exists():
        return None
    try:
        v = version_file.read_text(encoding="utf-8").strip()
        return v or None
    except OSError:
        return None


def _read_git_version() -> str | None:
    """Đọc version từ `git describe --tags --long --match 'v*'`."""
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--long", "--match", "v*"],
            capture_output=True, text=True,
            cwd=str(_project_root()), timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0:
            return None
        desc = result.stdout.strip()
        if not desc.startswith("v"):
            return None
        # "v1.0.0-3-ga3f8c0e" → version_tag="1.0.0", commits_after="3", git_hash="a3f8c0e"
        desc = desc[1:]
        parts = desc.rsplit("-", 2)
        if len(parts) != 3:
            return None
        version_tag, commits_after, git_hash_raw = parts
        git_hash = git_hash_raw[1:] if git_hash_raw.startswith("g") else git_hash_raw
        if commits_after == "0":
            return version_tag
        return f"{version_tag}+{commits_after}.{git_hash[:7]}"
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def get_version_short() -> str:
    """Version ngắn gọn, bỏ build metadata. VD: "1.0.0" (kể cả dev build)."""
    return get_version().split("+", 1)[0]


def get_build_info() -> dict:
    """Trả về dict chi tiết về version hiện tại."""
    v = get_version()
    parts = v.split("+", 1)
    info = {
        "version": parts[0],
        "full": v,
        "is_release": len(parts) == 1,
    }
    if len(parts) > 1:
        build = parts[1].split(".", 1)
        info["commits_after"] = int(build[0]) if build[0].isdigit() else 0
        info["git_hash"] = build[1] if len(build) > 1 else ""
    return info
