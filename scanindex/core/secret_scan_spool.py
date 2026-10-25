"""Versioned codec shared by KIE spool producers, journal and restart readers."""
from __future__ import annotations

import atexit
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import uuid


class SpoolBudgetError(RuntimeError):
    """Storage unavailable: keep detection resumable and stop new producers."""


_ledgers_lock = threading.Lock()
_ledgers: dict[str, "SpoolLedger"] = {}


def _key(path) -> str:
    return os.path.normcase(os.path.realpath(path))


def _storage_root() -> Path:
    from scanindex.core.secret_scan_progress import progress_dir
    directory = Path(progress_dir()).resolve()
    namespace = os.environ.get("SCANINDEX_PROGRESS_NAMESPACE", "")
    return directory.parent if namespace and directory.name == namespace else directory


class SpoolLedger:
    """One storage owner per data root; count retained blobs AND reservations.

    Recover actual bytes once on startup, including other namespaces, failed
    jobs and orphan/partial copies. Reservations are made before any copy.
    No filesystem walk per submitted file. Never call native lease APIs while
    holding the condition: deletion takes the lease lock before this lock.
    """

    def __init__(self, root: Path):
        self.root = root
        self._cond = threading.Condition()
        self._sizes: dict[str, int] = {}
        self._bytes = 0
        self._fd = None
        root.mkdir(parents=True, exist_ok=True)
        fd = os.open(root / "kie_spool.writerlock", os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0))
        try:
            if os.fstat(fd).st_size == 0:
                os.write(fd, b" ")
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._fd = fd
            for directory, dirs, _ in os.walk(root):
                if Path(directory).name != "kie_spool":
                    continue
                for child in Path(directory).iterdir():
                    size = (sum(p.stat().st_size for p in child.rglob("*") if p.is_file())
                            if child.is_dir() else child.stat().st_size)
                    self._sizes[_key(child)] = size
                    self._bytes += size
                dirs[:] = []
        except BaseException as exc:
            os.close(fd)
            self._fd = None
            raise SpoolBudgetError(f"Không thể sở hữu/đếm KIE spool: {exc}") from exc

    @property
    def budget(self) -> int:
        try:
            mb = max(1, int(os.environ.get("OCRTOOL_KIE_SPOOL_BUDGET_MB", "512")))
        except ValueError:
            mb = 512
        return mb * 1024 * 1024

    def reserve(self, path: str, size: int, cancel=None, stop=None) -> None:
        if size > self.budget:
            raise SpoolBudgetError(f"KIE snapshot {size} byte vượt storage budget {self.budget}")
        try:
            wait = max(0., float(os.environ.get("OCRTOOL_KIE_SPOOL_WAIT_S", "30")))
        except ValueError:
            wait = 30.
        deadline = time.monotonic() + wait
        with self._cond:
            while True:
                if (cancel is not None and cancel.is_set()) or (stop is not None and stop.is_set()):
                    raise SpoolBudgetError("Dừng nhận KIE spool theo yêu cầu hủy/dừng lượt")
                if self._bytes + size <= self.budget:
                    key = _key(path)
                    if key in self._sizes:
                        raise SpoolBudgetError("KIE snapshot reservation already exists")
                    self._sizes[key] = size
                    self._bytes += size
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SpoolBudgetError(
                        f"KIE spool đầy ({self._bytes}/{self.budget} byte); giữ dữ liệu retry và dừng cấp file mới")
                self._cond.wait(min(.1, remaining))

    def forget_deleted(self, path: str) -> None:
        key = _key(path)
        with self._cond:
            # Exact per-file cleanup is O(1); clearing a history tree is O(N).
            keys = [key] if key in self._sizes else [
                p for p in self._sizes if p.startswith(key.rstrip(os.sep) + os.sep)]
            for p in keys:
                self._bytes -= self._sizes.pop(p, 0)
            self._cond.notify_all()

    def telemetry(self) -> dict:
        with self._cond:
            return {"retained_and_reserved_bytes": self._bytes,
                    "storage_budget_bytes": self.budget, "snapshots": len(self._sizes)}

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


def get_ledger() -> SpoolLedger:
    root = _storage_root()
    with _ledgers_lock:
        key = _key(root)
        if key not in _ledgers:
            _ledgers[key] = SpoolLedger(root)
        return _ledgers[key]


def forget_deleted_tree(path: str) -> None:
    """Called only after lease-aware cleanup has confirmed physical deletion."""
    with _ledgers_lock:
        ledgers = list(_ledgers.values())
    key = _key(path)
    for ledger in ledgers:
        root = _key(ledger.root)
        if key == root or root.startswith(key.rstrip(os.sep) + os.sep) or (
                key.startswith(root + os.sep) and "kie_spool" in Path(key).parts):
            ledger.forget_deleted(path)


def close_ledgers() -> None:
    with _ledgers_lock:
        ledgers = list(_ledgers.values())
        _ledgers.clear()
    for ledger in ledgers:
        ledger.close()


def storage_telemetry() -> dict | None:
    with _ledgers_lock:
        ledger = _ledgers.get(_key(_storage_root()))
    return ledger.telemetry() if ledger is not None else None


atexit.register(close_ledgers)


def prepare_snapshot(canonical_path: str, rel: str, mode: str, run_id: str,
                     cancel=None, stop=None) -> dict:
    from scanindex.core.secret_scan_progress import progress_dir
    from scanindex.core.ocr.direct_engine import cleanup_path_when_idle
    ledger = get_ledger()
    directory = Path(progress_dir()) / "kie_spool" / uuid.uuid4().hex
    manifest = json.dumps({"rel": rel, "mode": mode, "run_id": run_id}, ensure_ascii=False).encode("utf-8")
    size = os.path.getsize(canonical_path)
    ledger.reserve(str(directory), size + len(manifest), cancel, stop)
    try:
        directory.mkdir(parents=True)
        snapshot = directory / "canonical.snapshot"
        staging = directory / "canonical.part"
        digest = hashlib.sha256()
        copied = 0
        with open(canonical_path, "rb") as src, open(staging, "wb") as dst:
            while chunk := src.read(1 << 20):
                copied += len(chunk)
                if copied > size:
                    raise SpoolBudgetError("Canonical thay đổi kích thước trong lúc tạo snapshot")
                digest.update(chunk)
                dst.write(chunk)
            if copied != size:
                raise SpoolBudgetError("Canonical thay đổi kích thước trong lúc tạo snapshot")
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(staging, snapshot)
        with open(directory / "manifest.json", "wb") as stream:
            stream.write(manifest)
            # Diagnostic only: the fsynced journal carries the actual resume
            # descriptor/hash. Avoid a second disk barrier per snapshot.
        return {"schema_version": 1, "canonical": str(snapshot),
                "spool_dir": str(directory), "hash": digest.hexdigest()}
    except BaseException:
        # Failed deletion keeps its reservation: never undercount actual bytes.
        cleanup_path_when_idle(str(directory))
        raise


def normalize_record(info: dict) -> dict:
    record = dict(info)
    version = int(record.get("schema_version", 1))
    if version != 1:
        raise ValueError(f"Unsupported KIE spool version: {version}")
    # Read both formats emitted before the producer/consumer contract was unified.
    record["schema_version"] = 1
    record["canonical"] = str(record.get("canonical") or record.get("spool_path") or "")
    record["hash"] = str(record.get("hash") or record.get("sha256") or "")
    record.pop("spool_path", None)
    record.pop("sha256", None)
    if not record["canonical"] or not record["hash"]:
        raise ValueError("KIE spool requires canonical path and SHA-256")
    return record
