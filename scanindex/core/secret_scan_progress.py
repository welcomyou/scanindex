"""Persisted progress for the secret-file scan so an interrupted run
(power loss, crash, cancel) can resume instead of rescanning the folder.

Journal design (append-only JSONL, one file per (folder, mode) under
``<base>/scan_progress/`` — outside ``temp/`` on purpose: the startup
``cleanup_stale_temp_dirs()`` wipe must not destroy resume state):

    line 1:  {"t":"h","v":1,"folder":...,"mode":...,"started_at":...}
    then:    {"t":"f","p":"rel/path.pdf","s":"ok"}
             {"t":"f","p":"rel/path.pdf","s":"err","e":"message"}
             {"t":"f","p":"rel/path.pdf","s":"skip","e":"reason"}
             {"t":"m","d":{...SecretScanMatch fields...}}

"skip" marks files that can NEVER be scanned (0 byte, not a real PDF,
PDF with no pages): unlike "err" they are not retried on resume, they
just stop costing time and log noise.

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

STATE_VERSION = 2
# Journal v1 (do bản phát hành trước ghi) vẫn phải đọc được để nâng cấp.
_LEGACY_STATE_VERSION = 1
STATE_DIR_NAME = "scan_progress"
DEFAULT_MAX_AGE_DAYS = 30
# Compaction floor: never compact a journal smaller than this many lines —
# rewrites must amortise, not chase every append.
_JOURNAL_FLOOR_LINES = 10_000


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _norm_path(path: str) -> str:
    """Định danh file chuẩn hóa — nhóm theo đường dẫn tuyệt đối, không theo
    chuỗi relative_path kế thừa (cache thư mục con dùng lại ở thư mục cha
    ghi rel theo thư mục cũ). Phải giữ ĐỒNG NHẤT công thức với
    ``secret_file_scan_screen._norm``."""
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def progress_dir() -> str:
    from scanindex.infra.paths import get_base_dir

    return os.path.join(get_base_dir(), STATE_DIR_NAME)


def _journal_key(folder: str, mode: str) -> str:
    return hashlib.sha1(
        f"{os.path.normcase(os.path.abspath(folder))}|{mode}".encode("utf-8")
    ).hexdigest()[:16]


def journal_path(folder: str, mode: str) -> str:
    """Đường dẫn journal v2. Tách tên file khỏi bản v1 để bảo vệ downgrade:
    bản cũ load journal lạ trả None rồi TẠO MỚI ghi đè cùng đường dẫn — nếu
    dùng chung tên, journal v2 sẽ bị bản cũ phá khi hạ cấp."""
    return os.path.join(
        progress_dir(), f"secret_scan_{_journal_key(folder, mode)}_v2.jsonl"
    )


def legacy_journal_path(folder: str, mode: str) -> str:
    """Journal v1 của bản phát hành trước (header v==1)."""
    return os.path.join(
        progress_dir(), f"secret_scan_{_journal_key(folder, mode)}.jsonl"
    )


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
    comfortably): done files in a set, errored/skipped files in dicts,
    matches in a list — no per-file sub-dicts.
    """

    def __init__(self, folder: str, mode: str):
        self.folder = folder
        self.mode = mode
        self.started_at = _now()
        self._done: set[str] = set()
        self._errors: dict[str, str] = {}
        self._skipped: dict[str, str] = {}
        self.matches: list[dict] = []
        # Version app đã "chấp nhận" kết quả của journal này. Journal bản cũ
        # không có field này → "" → khác version hiện tại.
        self.app_version: str = ""
        # Migration còn dở: {"target": version, "choice": 2|3} — None khi
        # không có hoặc đã xong. Tồn tại bền vững để crash giữa chừng chạy lại
        # được (ý định → nâng registry → xác nhận).
        self.migration: dict | None = None
        # Tập rel path (theo thư mục của journal) các file mật cũ còn chờ
        # quét lại. Chỉ bỏ khỏi tập này khi commit thay thế THÀNH CÔNG.
        self.rescan_pending: set[str] = set()
        self._fh = None
        # Lines the journal contained as of the last compaction/load (the
        # fresh header counts as 1) and lines appended since — the compact
        # trigger in save() compares these, so the journal roughly doubles
        # before each rewrite and total compaction writes stay O(n).
        self._lines_at_compact = 1
        self._appended_lines = 0

    # ── lifecycle ─────────────────────────────────────────────────────────
    @classmethod
    def create(
        cls, folder: str, mode: str, app_version: str = ""
    ) -> "SecretScanProgress":
        """Fresh progress: drop any previous journal for this key, then
        write the header line.

        The header is written through a truncating handle ("w") rather
        than append: if a stale progress object from an earlier run in
        this process still holds the old file open, os.remove() fails on
        Windows — truncation guarantees the new run starts from a clean
        state regardless. The legacy v1 file (if any) is also removed so a
        fresh scan does not keep resurrecting the old journal.
        """
        prog = cls(folder, mode)
        prog.app_version = str(app_version or "")
        prog.discard()
        try:
            os.remove(legacy_journal_path(folder, mode))
        except OSError:
            pass
        os.makedirs(progress_dir(), exist_ok=True)
        header: dict = {
            "t": "h",
            "v": STATE_VERSION,
            "folder": folder,
            "mode": mode,
            "started_at": prog.started_at,
        }
        if prog.app_version:
            header["av"] = prog.app_version
        with open(
            journal_path(folder, mode), "w", encoding="utf-8", newline="\n"
        ) as f:
            f.write(json.dumps(header, ensure_ascii=False) + "\n")
        return prog

    @classmethod
    def load(cls, folder: str, mode: str) -> "SecretScanProgress | None":
        """Replay the journal for (folder, mode) into memory.

        Prefers the v2 journal; falls back to the legacy v1 file (written
        by the previous release) which replays with the same event grammar
        minus the new state events. Missing/corrupt/foreign journals read
        as None — the caller just does a fresh scan. A torn trailing line
        (power cut mid-append) ends the replay at the last good line,
        losing at most the one event being written; per-file replacements
        are single atomic ``rpf`` lines, so a torn tail keeps either the
        old result or the complete new one — never half of each.

        A legacy journal that loads successfully is migrated IN PLACE into
        the v2 file right here: appends only ever go to the v2 path, and a
        v2 file whose first line is not a header replays as None — without
        this rewrite every event appended after a legacy resume would be
        orphaned and the next load would fall back to the stale v1 file
        again, losing the whole session.
        """
        prog = cls._replay(journal_path(folder, mode), folder, mode)
        if prog is None:
            prog = cls._replay(legacy_journal_path(folder, mode), folder, mode)
            if prog is not None:
                try:
                    prog._compact()  # header + toàn bộ state sang file v2
                except OSError:
                    return prog  # ghi hỏng: giữ nguyên v1, lần sau thử lại
                try:
                    os.remove(legacy_journal_path(folder, mode))
                except OSError:
                    pass
        return prog

    @classmethod
    def _replay(
        cls, path: str, folder: str, mode: str
    ) -> "SecretScanProgress | None":
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
                    or event.get("v") not in (STATE_VERSION, _LEGACY_STATE_VERSION)
                    or event.get("folder") != folder
                    or event.get("mode") != mode
                ):
                    return None
                header = event
                prog.app_version = str(header.get("av") or "")
                continue
            kind = event.get("t")
            if kind == "f":
                rel, status = str(event.get("p") or ""), str(event.get("s") or "")
                if status == "ok":
                    prog._done.add(rel)
                    prog._errors.pop(rel, None)
                    prog._skipped.pop(rel, None)
                elif status == "skip":
                    prog._skipped[rel] = str(event.get("e") or "")
                    prog._done.discard(rel)
                    prog._errors.pop(rel, None)
                elif status == "err":
                    prog._errors[rel] = str(event.get("e") or "")
                    prog._done.discard(rel)
                    prog._skipped.pop(rel, None)
            elif kind == "m" and isinstance(event.get("d"), dict):
                prog.matches.append(event["d"])
            elif kind == "av":
                prog.app_version = str(event.get("av") or "")
            elif kind == "mig":
                prog.migration = {
                    "target": str(event.get("target") or ""),
                    "choice": int(event.get("choice") or 0),
                }
            elif kind == "migdone":
                prog.migration = None
            elif kind == "rs" and isinstance(event.get("p"), list):
                prog.rescan_pending = {
                    str(rel) for rel in event["p"] if str(rel or "").strip()
                }
            elif kind == "rpf":
                # Commit thay thế THEO FILE, một dòng nguyên vẹn: file ok +
                # thay toàn bộ matches của file + bỏ khỏi tập quét lại.
                rel = str(event.get("p") or "")
                abs_norm = str(event.get("a") or "")
                new_dicts = [
                    d for d in event.get("m") or [] if isinstance(d, dict)
                ]
                if rel:
                    prog._done.add(rel)
                    prog._errors.pop(rel, None)
                    prog._skipped.pop(rel, None)
                    prog.rescan_pending.discard(rel)
                    prog.rescan_pending.discard(os.path.normcase(rel))
                if abs_norm:
                    prog.matches = [
                        d
                        for d in prog.matches
                        if _norm_path(str(d.get("source_path") or "")) != abs_norm
                    ]
                elif rel:
                    prog.matches = [
                        d
                        for d in prog.matches
                        if str(d.get("relative_path") or "") != rel
                    ]
                prog.matches.extend(new_dicts)
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
        for candidate in (
            path,
            path + ".tmp",
            legacy_journal_path(self.folder, self.mode),
        ):
            try:
                os.remove(candidate)
            except OSError:
                pass

    # ── mutation ──────────────────────────────────────────────────────────
    def record_file(self, rel_path: str, status: str, error: str = "") -> None:
        if status == "ok":
            self._done.add(rel_path)
            self._errors.pop(rel_path, None)
            self._skipped.pop(rel_path, None)
        elif status == "skip":
            self._skipped[rel_path] = error
            self._done.discard(rel_path)
            self._errors.pop(rel_path, None)
        else:
            self._errors[rel_path] = error
            self._done.discard(rel_path)
            self._skipped.pop(rel_path, None)
        event: dict = {"t": "f", "p": rel_path, "s": status}
        if error:
            event["e"] = error
        self._append_line(event)

    def record_matches(self, match_dicts: list[dict]) -> None:
        self.matches.extend(match_dicts)
        for match_dict in match_dicts:
            self._append_line({"t": "m", "d": match_dict})

    # ── versioned-resume state (R1/R2 review) ─────────────────────────────
    def needs_continue(self) -> bool:
        """Còn việc dở phải tiếp tục: migration chưa xác nhận xong hoặc còn
        file mật chờ quét lại. Resume cùng version vẫn phải tiếp tục các
        việc này — không phụ thuộc hộp thoại chọn 3 lựa chọn."""
        return bool(self.migration) or bool(self.rescan_pending)

    def begin_migration(self, target: str, choice: int) -> None:
        """Lưu bền vững ý định nâng cấp: target = version mới, choice = 2|3.
        Ghi TRƯỚC khi đụng registry — crash sau bước này vẫn chạy lại được."""
        self.migration = {"target": str(target), "choice": int(choice)}
        self._append_line(
            {"t": "mig", "target": self.migration["target"],
             "choice": self.migration["choice"]}
        )

    def finish_migration(self) -> None:
        """Xác nhận migration hoàn tất (registry đã lưu bền vững)."""
        self.migration = None
        self._append_line({"t": "migdone"})

    def accept_version(self, version: str) -> None:
        self.app_version = str(version or "")
        self._append_line({"t": "av", "av": self.app_version})

    def set_rescan_pending(self, rels: list[str] | set[str]) -> None:
        """Ghi đè toàn bộ tập file mật còn chờ quét lại (rel theo thư mục
        journal). Replace-whole-set giữ idempotent khi replay/compact."""
        self.rescan_pending = {str(r) for r in rels if str(r or "").strip()}
        self._append_line({"t": "rs", "p": sorted(self.rescan_pending)})

    def commit_replace_file(
        self, rel: str, abs_norm: str, match_dicts: list[dict]
    ) -> None:
        """Commit thay thế kết quả THEO FILE bằng đúng MỘT dòng journal.

        Một dòng = một transaction: replay thấy hoặc toàn bộ kết quả mới
        (file ok + matches thay + bỏ khỏi tập quét lại) hoặc — nếu dòng bị
        cắt dở vì mất điện — giữ nguyên kết quả cũ và file vẫn còn trong
        tập quét lại. KHÔNG dùng cho kết quả lỗi/hủy/skip: những trường
        hợp đó giữ nguyên hàng cũ và trạng thái chưa cập nhật.
        """
        self._done.add(rel)
        self._errors.pop(rel, None)
        self._skipped.pop(rel, None)
        # Bỏ cả dạng normcase: journal do bản bị lỗi chuẩn hóa có thể đang
        # giữ rel chữ thường cho đúng file này (tên trên đĩa có chữ HOA).
        self.rescan_pending.discard(rel)
        self.rescan_pending.discard(os.path.normcase(rel))
        self.matches = [
            d
            for d in self.matches
            if _norm_path(str(d.get("source_path") or "")) != abs_norm
        ]
        self.matches.extend(match_dicts)
        self._append_line(
            {"t": "rpf", "p": rel, "a": abs_norm, "m": list(match_dicts)}
        )

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
        """Files a resume must skip: completed successfully, plus skipped
        as permanently unscannable (0 byte / not a PDF / no pages) — the
        latter are NOT retried, unlike errored files."""
        return set(self._done) | set(self._skipped)

    def ok_files(self) -> set[str]:
        """Riêng các file xử lý THÀNH CÔNG (không gồm skip) — tập được phép
        kế thừa/đóng dấu version khi nâng cấp lịch sử quét (R4)."""
        return set(self._done)

    def stats(self) -> tuple[int, int, int, int]:
        """(done, error, skipped, found) counts for the resume prompt."""
        return (
            len(self._done),
            len(self._errors),
            len(self._skipped),
            len(self.matches),
        )

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
        """Rewrite the journal as header + state + one line per record, then
        keep appending. Closes the handle first — os.replace() on a file that
        is open for writing fails on Windows. Flush + fsync tạm TRƯỚC khi
        replace: snapshot phải xuống đĩa thật rồi mới công bố."""
        self.close()
        path = journal_path(self.folder, self.mode)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            header: dict = {
                "t": "h",
                "v": STATE_VERSION,
                "folder": self.folder,
                "mode": self.mode,
                "started_at": self.started_at,
            }
            if self.app_version:
                header["av"] = self.app_version
            f.write(json.dumps(header, ensure_ascii=False) + "\n")
            # State phiên bản/migration/quét lại PHẢI sống sót qua compact —
            # các event rpf đã được gập vào trạng thái final nên không ghi lại.
            if self.migration:
                f.write(
                    json.dumps(
                        {"t": "mig", **self.migration}, ensure_ascii=False
                    )
                    + "\n"
                )
            if self.rescan_pending:
                f.write(
                    json.dumps(
                        {"t": "rs", "p": sorted(self.rescan_pending)},
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
            for rel, reason in sorted(self._skipped.items()):
                event = {"t": "f", "p": rel, "s": "skip"}
                if reason:
                    event["e"] = reason
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
            for match_dict in self.matches:
                f.write(
                    json.dumps({"t": "m", "d": match_dict}, ensure_ascii=False)
                    + "\n"
                )
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        self._lines_at_compact = (
            1
            + int(bool(self.migration))
            + int(bool(self.rescan_pending))
            + len(self._done)
            + len(self._errors)
            + len(self._skipped)
            + len(self.matches)
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
        # Các key được "kế thừa" từ journal bản cũ khi nâng cấp (không phải
        # kết quả quét thật của version này) — chỉ dùng làm nguồn gốc thống
        # kê, không ảnh hưởng lookup.
        self._inherited: set[str] = set()
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
                if event.get("i"):
                    reg._inherited.add(key)
                else:
                    reg._inherited.discard(key)
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
        *,
        inherited: bool = False,
    ) -> None:
        """Append one registry record. ``inherited=True`` đánh dấu entry kế
        thừa từ journal bản cũ (lựa chọn 2/3 của resume nâng cấp) — nguồn
        gốc thống kê, không đổi hành vi lookup."""
        key = self._key(path, mode)
        self._entries[key] = (size, mtime, version, list(matches))
        if inherited:
            self._inherited.add(key)
        else:
            self._inherited.discard(key)
        event: dict = {
            "t": "r",
            "k": key,
            "s": size,
            "m": mtime,
            "v": version,
            "x": list(matches),
        }
        if inherited:
            event["i"] = 1
        self._append_line(event)

    def bump_versions_exact(
        self, paths, mode: str, version: str
    ) -> int:
        """Đóng dấu version mới cho ĐÚNG các (path, mode) chỉ định.

        Nhận tập key chính xác từ phiên (không duyệt prefix thư mục) để
        không đụng file chưa quét, mode khác hay thư mục anh em. Chỉ đổi
        trường version, giữ nguyên size/mtime/matches — file đã đổi nội
        dung từ lần quét cũ sẽ miss lookup ở lần sau (đúng hành vi). Trả
        số entry thực sự đổi. LƯU Ý: sửa này nằm trong RAM — caller phải
        gọi ``snapshot()`` để lưu bền vững (R3: save() chỉ flush append).
        """
        changed = 0
        for path in paths:
            key = self._key(str(path), mode)
            entry = self._entries.get(key)
            if entry is None:
                continue
            size, mtime, old_version, matches = entry
            if old_version == version:
                continue
            self._entries[key] = (size, mtime, version, matches)
            changed += 1
        return changed

    def inherited_count(self) -> int:
        return len(self._inherited)

    def has_entry(self, path: str, mode: str) -> bool:
        return self._key(path, mode) in self._entries

    def snapshot(self) -> None:
        """Ép ghi toàn bộ registry xuống đĩa: tmp → flush → fsync → replace.
        Bắt buộc sau khi đổi ``_entries`` trực tiếp (bump_versions_exact)."""
        self._compact()

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
                event: dict = {
                    "t": "r",
                    "k": key,
                    "s": size,
                    "m": mtime,
                    "v": version,
                    "x": matches,
                }
                if key in self._inherited:
                    event["i"] = 1
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
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


# ---------------------------------------------------------------------------
# Dấu "người dùng đã xác nhận KHÔNG phải mật"
# ---------------------------------------------------------------------------
# Ghi khi người dùng bấm "Không phải mật" trên một file. Mọi lượt quét sau
# (quét mới, resume dở, cache "Tận dụng kết quả đã quét") tra dấu này trước
# khi đưa dòng lên bảng: file bị mark và chưa đổi (size + mtime khớp) thì
# các dòng mật của nó bị chặn. File thay đổi nội dung → dấu hết hiệu lực,
# dòng hiện lại để người xem xét lại. Tên file cố tình không đặt tiền tố
# "secret_scan_"/"file_registry" và không dùng đuôi .jsonl để clear_all()
# ("Xóa lịch sử quét") và prune_stale() không quét mất — xác nhận của
# người dùng phải sống lâu hơn lịch sử quét.

NOT_SECRET_MARKS_NAME = "not_secret_marks.json"


def not_secret_marks_path() -> str:
    return os.path.join(progress_dir(), NOT_SECRET_MARKS_NAME)


class NotSecretMarks:
    """Map đường dẫn tuyệt đối (đã normalize) → {"size", "mtime", "at"}.

    Toàn bộ file được nạp một lần khi mở screen; mark() ghi lại cả file
    (dấu chỉ thêm khi người dùng bấm nút nên tần suất rất thấp, không cần
    journal append kiểu FileRegistry).
    """

    def __init__(self):
        self._marks: dict[str, dict] = {}

    @classmethod
    def load(cls) -> "NotSecretMarks":
        obj = cls()
        try:
            with open(not_secret_marks_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, OSError, ValueError):
            return obj  # chưa có file / file hỏng: coi như chưa mark gì.
        if isinstance(data, dict):
            for key, value in data.items():
                if isinstance(key, str) and isinstance(value, dict):
                    obj._marks[key] = value
        return obj

    @staticmethod
    def _key(path: str) -> str:
        return os.path.normpath(os.path.abspath(path))

    def is_marked(self, path: str) -> bool:
        """True nếu path bị mark VÀ file trên đĩa chưa đổi từ lúc mark."""
        info = self._marks.get(self._key(path))
        if not info:
            return False
        try:
            st = os.stat(path)
        except OSError:
            return False
        return (
            int(info.get("size", -1)) == int(st.st_size)
            and int(info.get("mtime", -1)) == int(st.st_mtime)
        )

    def mark(self, path: str) -> bool:
        """Ghi dấu cho path theo size+mtime hiện tại. Trả False nếu file
        không stat được (không đánh dấu được — nhưng dòng vẫn bị bỏ khỏi
        danh sách của phiên hiện tại)."""
        try:
            st = os.stat(path)
        except OSError:
            return False
        self._marks[self._key(path)] = {
            "size": int(st.st_size),
            "mtime": int(st.st_mtime),
            "at": _now(),
        }
        self._save()
        return True

    def _save(self) -> None:
        try:
            os.makedirs(progress_dir(), exist_ok=True)
            tmp = not_secret_marks_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._marks, f, ensure_ascii=False, indent=1)
            os.replace(tmp, not_secret_marks_path())
        except OSError:
            # Không chặn luồng người dùng chỉ vì không ghi được dấu; lượt
            # quét sau sẽ hiện lại dòng này (an toàn hướng "hiện thừa").
            pass
