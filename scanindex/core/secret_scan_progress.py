"""Persisted progress for the secret-file scan so an interrupted run
(power loss, crash, cancel) can resume instead of rescanning the folder.

Journal design (append-only JSONL, one file per (folder, mode) under
``<base>/scan_progress/`` — outside ``temp/`` on purpose: the startup
``cleanup_stale_temp_dirs()`` wipe must not destroy resume state):

    line 1:  {"t":"h","v":1,"folder":...,"mode":...,"started_at":...}
    then:    {"t":"f","p":"rel/path.pdf","s":"ok"}
             {"t":"f","p":"rel/path.pdf","s":"err","e":"message"}
             {"t":"m","d":{...SecretScanMatch fields...}}

Each processed file appends one or two short lines and fsyncs — O(1) per
file, so a million-file folder costs the same per file as a hundred-file
one (a rewrite-whole-file scheme would be O(n²) bytes written). Resume
replays the journal; a torn tail line from a power cut mid-append simply
ends the replay at the last good line. When appended lines outgrow the
snapshot already in the journal (or a 10k-line floor), the journal is
compacted once into header+snapshot lines, so total compaction writes
stay O(n) even for huge folders.

States are keyed by SHA-1 of (absolute folder path, mode) — same folder
scanned in "Tìm nhanh" vs "Tìm kỹ" keeps separate journals. Two app
instances racing on the same folder only risk duplicated work; the screen
itself blocks re-entry while a scan is running.
"""
from __future__ import annotations

import hashlib
import json
import os
import time

STATE_VERSION = 1
STATE_DIR_NAME = "scan_progress"
DEFAULT_MAX_AGE_DAYS = 30
# Compaction floor: never compact a journal smaller than this many lines —
# rewrites must amortise, not chase every append.
_JOURNAL_FLOOR_LINES = 10_000


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def progress_dir() -> str:
    from scanindex.infra.paths import get_base_dir

    return os.path.join(get_base_dir(), STATE_DIR_NAME)


def journal_path(folder: str, mode: str) -> str:
    key = hashlib.sha1(
        f"{os.path.normcase(os.path.abspath(folder))}|{mode}".encode("utf-8")
    ).hexdigest()[:16]
    return os.path.join(progress_dir(), f"secret_scan_{key}.jsonl")


def registry_path() -> str:
    """Global per-file registry shared by ALL scan roots — lets a scan of
    ``a/`` reuse the results of a finished ``a/b/c`` scan (and vice versa)."""
    return os.path.join(progress_dir(), "file_registry.jsonl")


def clear_all() -> int:
    """Delete every scan-progress artefact (resume journals + the global
    file registry). Used by the screen's "Xóa lịch sử quét" button and
    safe to call mid-life of the app. Returns the number of files removed."""
    directory = progress_dir()
    if not os.path.isdir(directory):
        return 0
    removed = 0
    try:
        entries = os.listdir(directory)
    except OSError:
        return 0
    for name in entries:
        if name.startswith(("secret_scan_", "file_registry")):
            try:
                os.remove(os.path.join(directory, name))
                removed += 1
            except OSError:
                pass
    return removed


class SecretScanProgress:
    """In-memory scan state backed by an append-only journal file.

    Memory layout is lean on purpose (a million-file folder must fit
    comfortably): done files in a set, errored files in a dict, matches
    in a list — no per-file sub-dicts.
    """

    def __init__(self, folder: str, mode: str):
        self.folder = folder
        self.mode = mode
        self.started_at = _now()
        self._done: set[str] = set()
        self._errors: dict[str, str] = {}
        self.matches: list[dict] = []
        self._fh = None
        # Lines the journal contained as of the last compaction/load (the
        # fresh header counts as 1) and lines appended since — the compact
        # trigger in save() compares these, so the journal roughly doubles
        # before each rewrite and total compaction writes stay O(n).
        self._lines_at_compact = 1
        self._appended_lines = 0

    # ── lifecycle ─────────────────────────────────────────────────────────
    @classmethod
    def create(cls, folder: str, mode: str) -> "SecretScanProgress":
        """Fresh progress: drop any previous journal for this key, then
        write the header line.

        The header is written through a truncating handle ("w") rather
        than append: if a stale progress object from an earlier run in
        this process still holds the old file open, os.remove() fails on
        Windows — truncation guarantees the new run starts from a clean
        header regardless.
        """
        prog = cls(folder, mode)
        prog.discard()
        os.makedirs(progress_dir(), exist_ok=True)
        with open(
            journal_path(folder, mode), "w", encoding="utf-8", newline="\n"
        ) as f:
            f.write(
                json.dumps(
                    {
                        "t": "h",
                        "v": STATE_VERSION,
                        "folder": folder,
                        "mode": mode,
                        "started_at": prog.started_at,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        return prog

    @classmethod
    def load(cls, folder: str, mode: str) -> "SecretScanProgress | None":
        """Replay the journal for (folder, mode) into memory.

        Missing/corrupt/foreign journals read as None — the caller just
        does a fresh scan. A torn trailing line (power cut mid-append)
        ends the replay at the last good line, losing at most the one
        event being written.
        """
        path = journal_path(folder, mode)
        try:
            with open(path, encoding="utf-8") as f:
                raw_lines = f.readlines()
        except OSError:
            return None

        header: dict | None = None
        prog = cls(folder, mode)
        lines_seen = 0
        bad_tail = False
        for raw in raw_lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except ValueError:
                bad_tail = True
                break  # torn tail — keep everything before it
            if not isinstance(event, dict):
                bad_tail = True
                break
            lines_seen += 1
            if header is None:
                if (
                    event.get("t") != "h"
                    or event.get("v") != STATE_VERSION
                    or event.get("folder") != folder
                    or event.get("mode") != mode
                ):
                    return None
                header = event
                continue
            kind = event.get("t")
            if kind == "f":
                rel, status = str(event.get("p") or ""), str(event.get("s") or "")
                if status == "ok":
                    prog._done.add(rel)
                    prog._errors.pop(rel, None)
                elif status == "err":
                    prog._errors[rel] = str(event.get("e") or "")
                    prog._done.discard(rel)
            elif kind == "m" and isinstance(event.get("d"), dict):
                prog.matches.append(event["d"])
        if header is None:
            return None
        prog.started_at = str(header.get("started_at") or prog.started_at)
        prog._lines_at_compact = lines_seen
        if bad_tail:
            # Tự chữa: ghi đè lại file chỉ với phần dữ liệu đọc được, nếu
            # không mọi sự kiện ghi thêm sau này sẽ nằm sau dòng hỏng và
            # không bao giờ được replay nữa.
            try:
                prog._compact()
            except OSError:
                pass
        return prog

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None

    def discard(self) -> None:
        """Delete the journal — scan no longer resumable."""
        self.close()
        path = journal_path(self.folder, self.mode)
        for candidate in (path, path + ".tmp"):
            try:
                os.remove(candidate)
            except OSError:
                pass

    # ── mutation ──────────────────────────────────────────────────────────
    def record_file(self, rel_path: str, status: str, error: str = "") -> None:
        if status == "ok":
            self._done.add(rel_path)
            self._errors.pop(rel_path, None)
        else:
            self._errors[rel_path] = error
            self._done.discard(rel_path)
        event: dict = {"t": "f", "p": rel_path, "s": status}
        if error:
            event["e"] = error
        self._append_line(event)

    def record_matches(self, match_dicts: list[dict]) -> None:
        self.matches.extend(match_dicts)
        for match_dict in match_dicts:
            self._append_line({"t": "m", "d": match_dict})

    def save(self) -> None:
        """Flush + fsync the journal (power-cut durable up to the last
        event), compacting it when appends have outgrown the snapshot."""
        self._open_fh()
        self._fh.flush()
        os.fsync(self._fh.fileno())
        if self._appended_lines > max(
            _JOURNAL_FLOOR_LINES, self._lines_at_compact
        ):
            self._compact()

    def done_files(self) -> set[str]:
        """Files that completed successfully — what a resume must skip.
        Errored files are NOT included: resume retries them in case the
        failure was transient."""
        return set(self._done)

    def stats(self) -> tuple[int, int, int]:
        """(done, error, found) counts for the resume prompt."""
        return len(self._done), len(self._errors), len(self.matches)

    # ── internals ─────────────────────────────────────────────────────────
    def _open_fh(self) -> None:
        if self._fh is None:
            self._fh = open(
                journal_path(self.folder, self.mode),
                "a",
                encoding="utf-8",
                newline="\n",
            )

    def _append_line(self, event: dict) -> None:
        self._open_fh()
        self._fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._appended_lines += 1

    def _compact(self) -> None:
        """Rewrite the journal as header + one line per record, then keep
        appending. Closes the handle first — os.replace() on a file that
        is open for writing fails on Windows."""
        self.close()
        path = journal_path(self.folder, self.mode)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(
                json.dumps(
                    {
                        "t": "h",
                        "v": STATE_VERSION,
                        "folder": self.folder,
                        "mode": self.mode,
                        "started_at": self.started_at,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            for rel in sorted(self._done):
                f.write(
                    json.dumps({"t": "f", "p": rel, "s": "ok"}, ensure_ascii=False)
                    + "\n"
                )
            for rel, error in sorted(self._errors.items()):
                event: dict = {"t": "f", "p": rel, "s": "err"}
                if error:
                    event["e"] = error
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
            for match_dict in self.matches:
                f.write(
                    json.dumps({"t": "m", "d": match_dict}, ensure_ascii=False)
                    + "\n"
                )
        os.replace(tmp, path)
        self._lines_at_compact = (
            1 + len(self._done) + len(self._errors) + len(self.matches)
        )
        self._appended_lines = 0


class FileRegistry:
    """Global "this file was already scanned" registry — one JSONL journal
    shared by every scan root and mode.

    An entry is keyed by (normalised absolute path, scan mode) and stores
    size + mtime + app version + the matches found. A lookup only hits
    when ALL of those still match, so:
      - scanning parent ``a/`` after a finished ``a/b/c`` scan skips the
        unchanged b/c files (the cross-folder case),
      - edited/new files are always re-scanned (size/mtime differ),
      - an app upgrade invalidates old entries (version differs),
      - "Tìm nhanh" entries never satisfy a "Tìm kỹ" scan (mode differs).

    Same append-only + compact mechanics as SecretScanProgress: O(1)
    per file, torn tail tolerated on replay, errors never recorded (a
    failed file is retried on the next scan).
    """

    def __init__(self):
        # key -> (size, mtime, version, matches list)
        self._entries: dict[str, tuple[int, float, str, list[dict]]] = {}
        self._fh = None
        self._lines_at_compact = 1
        self._appended_lines = 0

    @staticmethod
    def _key(path: str, mode: str) -> str:
        return os.path.normcase(os.path.abspath(path)) + "\x00" + mode

    @classmethod
    def load(cls) -> "FileRegistry":
        """Replay the registry journal. Missing/corrupt data degrades to
        an empty registry — scanning simply proceeds without cache."""
        reg = cls()
        try:
            with open(registry_path(), encoding="utf-8") as f:
                raw_lines = f.readlines()
        except OSError:
            return reg
        lines_seen = 0
        bad_tail = False
        for raw in raw_lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except ValueError:
                bad_tail = True
                break  # torn tail
            if not isinstance(event, dict):
                bad_tail = True
                break
            lines_seen += 1
            if event.get("t") != "r":
                continue
            key = str(event.get("k") or "")
            payload = event.get("x")
            if key and isinstance(payload, list):
                reg._entries[key] = (
                    event.get("s") or 0,
                    event.get("m") or 0.0,
                    str(event.get("v") or ""),
                    payload,
                )
        reg._lines_at_compact = lines_seen
        if bad_tail:
            # Tự chữa như SecretScanProgress.load — cắt bỏ đuôi hỏng để các
            # bản ghi thêm sau này vẫn được replay.
            try:
                reg._compact()
            except OSError:
                pass
        return reg

    def lookup(
        self, path: str, size: int, mtime: float, mode: str, version: str
    ) -> list[dict] | None:
        """Matches recorded for this exact unchanged file, or None."""
        entry = self._entries.get(self._key(path, mode))
        if entry is None:
            return None
        entry_size, entry_mtime, entry_version, matches = entry
        if entry_size != size or entry_mtime != mtime:
            return None
        if entry_version != version:
            return None
        return list(matches)

    def record(
        self,
        path: str,
        size: int,
        mtime: float,
        mode: str,
        version: str,
        matches: list[dict],
    ) -> None:
        key = self._key(path, mode)
        self._entries[key] = (size, mtime, version, list(matches))
        self._append_line(
            {
                "t": "r",
                "k": key,
                "s": size,
                "m": mtime,
                "v": version,
                "x": list(matches),
            }
        )

    def save(self) -> None:
        self._open_fh()
        self._fh.flush()
        os.fsync(self._fh.fileno())
        if self._appended_lines > max(
            _JOURNAL_FLOOR_LINES, self._lines_at_compact
        ):
            self._compact()

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None

    def discard(self) -> None:
        self.close()
        path = registry_path()
        for candidate in (path, path + ".tmp"):
            try:
                os.remove(candidate)
            except OSError:
                pass

    def _open_fh(self) -> None:
        if self._fh is None:
            path = registry_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self._fh = open(path, "a", encoding="utf-8", newline="\n")

    def _append_line(self, event: dict) -> None:
        self._open_fh()
        self._fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._appended_lines += 1

    def _compact(self) -> None:
        self.close()
        path = registry_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"t": "h", "v": STATE_VERSION}) + "\n")
            for key in sorted(self._entries):
                size, mtime, version, matches = self._entries[key]
                f.write(
                    json.dumps(
                        {
                            "t": "r",
                            "k": key,
                            "s": size,
                            "m": mtime,
                            "v": version,
                            "x": matches,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        os.replace(tmp, path)
        self._lines_at_compact = 1 + len(self._entries)
        self._appended_lines = 0


def prune_stale(max_age_days: int = DEFAULT_MAX_AGE_DAYS) -> int:
    """Remove journal files untouched for max_age_days (and orphan .tmp
    leftovers). Called before each scan start so abandoned states don't
    accumulate. Returns the number of files removed."""
    directory = progress_dir()
    if not os.path.isdir(directory):
        return 0
    cutoff = time.time() - max_age_days * 86400
    tmp_cutoff = time.time() - 86400
    removed = 0
    try:
        entries = os.listdir(directory)
    except OSError:
        return 0
    for name in entries:
        if not name.startswith("secret_scan_"):
            continue
        if not (name.endswith(".jsonl") or name.endswith(".jsonl.tmp")):
            continue
        path = os.path.join(directory, name)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        remove = mtime < cutoff
        if name.endswith(".tmp"):
            remove = mtime < tmp_cutoff
        if remove:
            try:
                os.remove(path)
                removed += 1
            except OSError:
                pass
    return removed
