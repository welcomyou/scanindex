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
import threading
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


# ---------------------------------------------------------------------------
# D0 — AtomicJsonlWriter: MỘT owner ghi MỘT file, fail-closed khi phục hồi
# lỗi ghi thất bại. Hai lớp journal/registry dùng CHUNG để không còn hai bản
# logic recovery dễ lệch nhau.
#
# Hợp đồng (review chốt §3):
# - Write lỗi (partial write / os.write trả 0 / lỗi I/O): thử cắt file về
#   offset trước khi ghi; cắt THÀNH CÔNG → state HEALTHY, raise lỗi gốc
#   (caller retry được — dữ liệu trên đĩa chắc chắn sạch); cắt THẤT BẠI →
#   state POISONED + đóng handle + TỪ CHỐI mọi append/save kế tiếp — không
#   bao giờ nhận record mới trên dòng hỏng (reload sẽ dừng ở dòng hỏng và
#   mất mọi event đứng sau — "ACK giả" của probe review).
# - fsync lỗi: raise OSError, state giữ HEALTHY (byte đã vào nhân OS, tail
#   nguyên vẹn — retry fsync an toàn; durability chưa chứng minh nên caller
#   chưa được ACK).
# - Recovery có kiểm soát: khóa owner → backup file hỏng (.corrupt-<ts>) →
#   replay prefix hợp lệ (load tự chữa) → snapshot tmp + fsync + replace.
# ---------------------------------------------------------------------------
class WriterPoisonedError(RuntimeError):
    """Writer đang POISONED — mọi append/save bị từ chối (fail-closed).

    Caller phải coi dữ liệu CHƯA bền vững: không ACK, không discard journal,
    giữ nghĩa vụ retry; recovery có kiểm soát là đường duy nhất mở lại."""


class RegistryBusyError(RuntimeError):
    """Khóa single-writer registry bị giữ bởi tiến trình khác (D0) — lượt
    quét chạy KHÔNG cache registry thay vì hai tiến trình cùng ghi/truncate
    một file mà không phối hợp."""


class AtomicJsonlWriter:
    HEALTHY = "HEALTHY"
    POISONED = "POISONED"
    CLOSED = "CLOSED"

    def __init__(self, path: str, label: str = "jsonl"):
        self._path = path
        self._label = label
        self._fd: int | None = None
        self._state = self.CLOSED
        # RLock: recovery chạy GIỮ lock và gọi ngược vào callback của owner
        # (compact → close()) — lock thường sẽ tự deadlock.
        self._lock = threading.RLock()

    # -- trạng thái ---------------------------------------------------------
    @property
    def state(self) -> str:
        return self._state

    @property
    def poisoned(self) -> bool:
        return self._state == self.POISONED

    # -- I/O ----------------------------------------------------------------
    def _open(self) -> None:
        if self._fd is not None:
            return
        if self._state == self.POISONED:
            raise WriterPoisonedError(
                f"[{self._label}] writer bị POISONED — từ chối mở lại "
                f"({self._path}); cần recovery có kiểm soát"
            )
        directory = os.path.dirname(self._path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        # Raw fd (O_APPEND, không buffer) — trạng thái ghi xác định tuyệt đối.
        self._fd = os.open(
            self._path, os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_BINARY", 0)
        )
        self._state = self.HEALTHY

    def _close_fd(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    def append_line(self, event: dict) -> None:
        """Ghi MỘT dòng JSON + '\\n'. Bất đồng, lỗi sẽ được phục hồi hoặc
        poison — KHÔNG BAO GIỜ trả về bình thường khi đĩa còn đuôi hỏng."""
        with self._lock:
            if self._state == self.POISONED:
                raise WriterPoisonedError(
                    f"[{self._label}] writer bị POISONED — từ chối append "
                    f"(event t={event.get('t')!r})"
                )
            self._open()
            data = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
            offset = os.fstat(self._fd).st_size
            try:
                while data:
                    written = os.write(self._fd, data)
                    if written <= 0:
                        # os.write trả 0 = không tiến triển — coi như lỗi,
                        # tránh lặp vô hạn (review §3 D0).
                        raise OSError(
                            f"os.write trả {written} byte (không tiến triển)"
                        )
                    data = data[written:]
            except BaseException as exc:
                self._rollback_or_poison(offset, exc)
                raise

    def sync(self) -> None:
        """fsync — mốc durability. Lỗi fsync raise nhưng KHÔNG poison: byte
        chắc chắn đã write() vào nhân OS và tail nguyên vẹn (mọi append đều
        rollback kiểm chứng), retry fsync là an toàn; durability chỉ được
        xác nhận khi sync() trả về thành công."""
        with self._lock:
            if self._state == self.POISONED:
                raise WriterPoisonedError(
                    f"[{self._label}] writer bị POISONED — từ chối fsync"
                )
            self._open()
            os.fsync(self._fd)

    def close(self) -> None:
        with self._lock:
            self._close_fd()
            # POISONED không bị close xóa sạch: state giữ nguyên để mọi lời
            # gọi kế tiếp vẫn bị từ chối cho đến khi recovery có kiểm soát.
            if self._state != self.POISONED:
                self._state = self.CLOSED

    def _rollback_or_poison(self, offset: int, exc: BaseException) -> None:
        """Cắt file về offset sạch; THÀNH CÔNG → giữ HEALTHY (retry được);
        THẤT BẠI → POISONED + đóng handle (fail-closed, không nuốt lỗi
        phục hồi — review §3: truncate fail mà nuốt sẽ để writer nhận thêm
        record trên dòng hỏng → reload mất kết quả đã ACK)."""
        try:
            os.ftruncate(self._fd, offset)
        except OSError as trunc_exc:
            self._state = self.POISONED
            self._close_fd()
            import logging

            logging.getLogger(__name__).error(
                "[%s] rollback append THẤT BẠI (%s) sau lỗi ghi (%s) — "
                "writer POISONED, từ chối mọi ghi tiếp theo tới khi recovery",
                self._label, trunc_exc, exc,
            )
            return
        # Cắt thành công: đĩa chắc chắn sạch tới offset — HEALTHY, raise lỗi
        # gốc để caller retry (rollback không nuốt lỗi gốc).

    def recover(self, replay_and_rewrite) -> bool:
        """Recovery CÓ KIỂM SOÁT (D0): backup file hỏng → `replay_and_rewrite()`
        (callback của owner: replay prefix hợp lệ + snapshot tmp + fsync +
        replace) → mở writer mới. Trả True khi mở lại được writer HEALTHY.
        Gọi khi KHÔNG còn producer nào đang append (owner khóa lâu)."""
        with self._lock:
            self._close_fd()
            backup = f"{self._path}.corrupt-{time.strftime('%Y%m%d_%H%M%S')}"
            try:
                if os.path.exists(self._path):
                    try:
                        os.replace(self._path, backup)
                    except OSError:
                        # Windows: replace đè không được khi đích tồn tại —
                        # xóa backup cũ (nếu có) rồi thử lại một lần.
                        try:
                            os.remove(backup)
                            os.replace(self._path, backup)
                        except OSError:
                            return False
                ok = False
                try:
                    replay_and_rewrite()
                    ok = True
                finally:
                    if not ok:
                        # Không sửa được: trả lại file gốc để không mất dữ
                        # liệu — recovery thất bại là có đoán được, mất
                        # backup thì không.
                        if os.path.exists(self._path) and os.path.exists(backup):
                            try:
                                os.remove(self._path)
                            except OSError:
                                pass
                        try:
                            os.replace(backup, self._path)
                        except OSError:
                            pass
                if not ok:
                    return False
                self._state = self.HEALTHY
                self._open()
                return self._state == self.HEALTHY
            except OSError:
                return False


def _recover_jsonl_replay(path: str, replay_cls, *args) -> None:
    """Rebuild `path` từ prefix hợp lệ: replay bằng `replay_cls.load` (tự cắt
    đuôi hỏng + compact) rồi snapshot bền vững. Dùng bởi recovery của hai
    lớp journal/registry."""
    obj = replay_cls.load(*args) if args else replay_cls.load()
    if obj is not None:
        obj._compact()



def progress_dir() -> str:
    from scanindex.infra.paths import get_base_dir
    root = os.path.join(get_base_dir(), STATE_DIR_NAME)
    namespace = os.environ.get("SCANINDEX_PROGRESS_NAMESPACE", "")
    if namespace:
        if not namespace.replace("-", "").replace("_", "").isalnum():
            raise ValueError("Invalid progress namespace")
        return os.path.join(root, namespace)
    return root


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


def _backup_corrupt(path: str) -> str | None:
    """D0: backup file hỏng TRƯỚC khi tự chữa (recovery có kiểm soát phải
    giữ được bằng chứng gốc). Trả đường dẫn backup, None khi không có gì để
    backup. Backup KHÔNG dùng tiền tố dọn của clear_all/prune — dữ liệu hỏng
    chỉ bị xóa tay."""
    if not os.path.exists(path):
        return None
    backup = f"{path}.corrupt-{time.strftime('%Y%m%d_%H%M%S')}"
    try:
        with open(path, "rb") as src, open(backup, "wb") as dst:
            while True:
                chunk = src.read(1 << 20)
                if not chunk:
                    break
                dst.write(chunk)
        return backup
    except OSError:
        return None


def clear_all() -> int:
    """Delete every scan-progress artefact (resume journals + the global
    file registry). Used by the screen's "Xóa lịch sử quét" button and
    safe to call mid-life of the app. Returns the number of files removed."""
    directory = progress_dir()
    if not os.path.isdir(directory):
        return 0
    spool_root = os.path.join(directory, "kie_spool")
    if os.path.isdir(spool_root):
        from scanindex.core.secret_scan_spool import get_ledger
        get_ledger()  # Refuse deletion while another app owns this storage.
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
    if os.path.isdir(spool_root):
        from scanindex.core.ocr.direct_engine import cleanup_path_when_idle
        cleanup_path_when_idle(spool_root)
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
        # R3-06: matches lưu THEO FILE — bucket key là đường dẫn tuyệt đối
        # đã chuẩn hóa (`_norm_path`). Commit thay thế thay ĐÚNG bucket
        # O(số match mới) thay vì duyệt + chuẩn hóa lại TOÀN BỘ matches tích
        # lũy trong khóa chung mỗi file (trước đây O(tổng matches)/commit →
        # gần bậc hai theo quy mô kho). `.matches` là view phẳng tái tạo
        # THỨ TỰ cũ: bucket thay thế được pop + gắn lại CUỐI (đúng semantics
        # "xóa khỏi nơi cũ, nối cuối" của bản append phẳng).
        self._matches_by_file: dict[str, list[dict]] = {}
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
        # D2 — durable KIE spool: rel -> {canonical(spool path), hash, ordinals,
        # matches(baseline), fp, status}. Job KIE đã nhận: OCR xong, metadata
        # chờ — restart chỉ resubmit KIE (OCR-call = 0) khi snapshot còn hợp
        # lệ. Bền qua journal event "kspool" (ghi TRƯỚC khi worker thả workdir).
        self._kie_spool: dict[str, dict] = {}
        self._writer = AtomicJsonlWriter(
            journal_path(folder, mode), label="journal"
        )
        # Lines the journal contained as of the last compaction/load (the
        # fresh header counts as 1) and lines appended since — the compact
        # trigger in save() compares these, so the journal roughly doubles
        # before each rewrite and total compaction writes stay O(n).
        self._lines_at_compact = 1
        self._appended_lines = 0

    # ── matches theo file (R3-06) ──────────────────────────────────────────
    @property
    def matches(self) -> list[dict]:
        """View phẳng theo thứ tự bucket (giữ đúng thứ tự journal cũ)."""
        out: list[dict] = []
        for bucket in self._matches_by_file.values():
            out.extend(bucket)
        return out

    @staticmethod
    def _match_key_for(match_dict: dict) -> str:
        return _norm_path(str(match_dict.get("source_path") or ""))

    def _match_bucket_append(self, match_dicts: list[dict]) -> None:
        for d in match_dicts:
            key = self._match_key_for(d)
            bucket = self._matches_by_file.get(key)
            if bucket is None:
                self._matches_by_file[key] = [d]
            else:
                bucket.append(d)

    def _match_bucket_replace(
        self, abs_norm: str, match_dicts: list[dict]
    ) -> None:
        """Thay toàn bộ kết quả của ĐÚNG một file: pop bucket + gắn lại cuối
        — tái tạo đúng vị trí "nối cuối" của bản append phẳng."""
        self._matches_by_file.pop(abs_norm, None)
        self._matches_by_file[abs_norm] = list(match_dicts)

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
                prog._match_bucket_append([event["d"]])
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
            elif kind == "kspool":
                # D2: nghĩa vụ metadata KIE của một file (None = xóa).
                rel = str(event.get("p") or "")
                info = event.get("x")
                if rel:
                    if isinstance(info, dict) and info:
                        prog._kie_spool[rel] = info
                    else:
                        prog._kie_spool.pop(rel, None)
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
                    prog._match_bucket_replace(abs_norm, new_dicts)
                elif rel:
                    # Hi hữu (rpf thiếu "a"): lọc theo relative_path như bản
                    # cũ, rồi nối kết quả mới vào cuối.
                    kept: dict[str, list[dict]] = {}
                    for key, bucket in prog._matches_by_file.items():
                        bucket2 = [
                            d
                            for d in bucket
                            if str(d.get("relative_path") or "") != rel
                        ]
                        if bucket2:
                            kept[key] = bucket2
                    prog._matches_by_file = kept
                    prog._match_bucket_append(new_dicts)
        if header is None:
            return None
        prog.started_at = str(header.get("started_at") or prog.started_at)
        prog._lines_at_compact = lines_seen
        if bad_tail:
            # Tự chữa (D0: backup file hỏng trước): ghi đè lại file chỉ với
            # phần dữ liệu đọc được, nếu không mọi sự kiện ghi thêm sau này
            # sẽ nằm sau dòng hỏng và không bao giờ được replay nữa.
            try:
                _backup_corrupt(journal_path(folder, mode))
                prog._compact()
            except OSError:
                pass
        return prog

    def close(self) -> None:
        self._writer.close()

    @property
    def poisoned(self) -> bool:
        """D0: writer journal đang POISONED — mọi ghi bị từ chối; caller
        phải giữ nghĩa vụ retry + báo lỗi lưu, KHÔNG được discard journal
        (còn dữ liệu trên đĩa cần recovery có kiểm soát)."""
        return self._writer.poisoned

    def recover_writer(self) -> bool:
        """Recovery CÓ KIỂM SOÁT của writer journal (D0): rebuild snapshot từ
        RAM (RAM là hợp nhất nhất quán của mọi state đã chốt — event ghi dở
        trên đĩa bị bỏ khi viết lại) + backup file hỏng. CHỈ gọi khi không
        producer nào đang append (owner giữ state_lock)."""
        return self._writer.recover(self._compact)

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
        self._match_bucket_append(match_dicts)
        for match_dict in match_dicts:
            self._append_line({"t": "m", "d": match_dict})

    # ── versioned-resume state (R1/R2 review) ─────────────────────────────
    def needs_continue(self) -> bool:
        """Còn việc dở phải tiếp tục: migration chưa xác nhận xong hoặc còn
        file mật chờ quét lại. Resume cùng version vẫn phải tiếp tục các
        việc này — không phụ thuộc hộp thoại chọn 3 lựa chọn."""
        return bool(self.migration) or bool(self.rescan_pending) or bool(self.kie_spool_pending())

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

    # ── durable KIE spool (D2) ─────────────────────────────────────────────
    def set_kie_spool(self, rel: str, info: dict | None) -> None:
        """Đăng ký/xóa nghĩa vụ metadata KIE của MỘT file. ``info=None`` xóa
        (file hết nghĩa vụ KIE-only — chuyển quét lại toàn bộ). Event thay
        thế nguyên record → replay/compact idempotent."""
        if info is None:
            self._kie_spool.pop(rel, None)
        else:
            from scanindex.core.secret_scan_spool import normalize_record
            info = normalize_record(info)
            self._kie_spool[str(rel)] = dict(info)
        self._append_line({"t": "kspool", "p": str(rel), "x": info})

    def kie_spool(self, rel: str) -> dict | None:
        info = self._kie_spool.get(rel)
        if info is None:
            return None
        from scanindex.core.secret_scan_spool import normalize_record
        return normalize_record(info)

    def kie_spool_pending(self) -> set[str]:
        """Các rel CÒN nghĩa vụ KIE-only: có spool record nhưng CHƯA có kết
        quả bền vững (chưa trong done/skip). File đã commit (kể cả metadata
        lỗi → rpf baseline + rescan_pending) tự rời tập — lượt sau quét OCR
        thật theo luật đã có."""
        done = self.done_files()
        return {
            rel for rel, info in self._kie_spool.items()
            if rel not in done or rel in self.rescan_pending
            or not info.get("metadata_applied", False)
        }

    def kie_spool_gc(self, *, durability_confirmed: bool = False,
                     rel: str | None = None) -> int:
        """Dọn spool snapshot của file KHÔNG còn nghĩa vụ (đã done/skip).
        File còn pending giữ nguyên — restart KIE-only cần snapshot. Trả số
        thư mục spool đã xóa."""
        if not durability_confirmed:
            return 0
        from scanindex.core.ocr.direct_engine import cleanup_path_when_idle
        removed = 0
        root = os.path.realpath(os.path.join(progress_dir(), "kie_spool"))
        # Per-file ACK cleanup must not scan the whole history.
        records = ([(rel, self._kie_spool[rel])] if rel in self._kie_spool else []) \
            if rel is not None else list(self._kie_spool.items())
        for rel, info in records:
            pending = ((rel not in self._done and rel not in self._skipped)
                       or rel in self.rescan_pending
                       or not info.get("metadata_applied", False))
            if not pending:
                spool_dir = str(info.get("spool_dir") or "")
                if not spool_dir:
                    continue
                target = os.path.realpath(spool_dir)
                if target == root or os.path.commonpath([root, target]) != root:
                    raise ValueError("KIE spool cleanup outside owned spool root")
                if cleanup_path_when_idle(spool_dir) and not os.path.exists(spool_dir):
                    self.set_kie_spool(rel, None)
                    removed += 1
        return removed

    def commit_replace_file(
        self, rel: str, abs_norm: str, match_dicts: list[dict]
    ) -> None:
        """Commit thay thế kết quả THEO FILE bằng đúng MỘT dòng journal.

        Một dòng = một transaction: replay thấy hoặc toàn bộ kết quả mới
        (file ok + matches thay + bỏ khỏi tập quét lại) hoặc — nếu dòng bị
        cắt dở vì mất điện — giữ nguyên kết quả cũ và file vẫn còn trong
        tập quét lại. KHÔNG dùng cho kết quả lỗi/hủy/skip: những trường
        hợp đó giữ nguyên hàng cũ và trạng thái chưa cập nhật.

        R3-06: thay ĐÚNG bucket của file (O(số match mới), chuẩn hóa path
        MỘT lần cho từng match mới) thay vì duyệt + chuẩn hóa TOÀN BỘ
        matches tích lũy trong khóa chung mỗi commit — phần đó tăng gần
        bậc hai theo quy mô kho và không song song hóa được. Log rpf giữ
        nguyên — replay idempotent như cũ.
        """
        self._done.add(rel)
        self._errors.pop(rel, None)
        self._skipped.pop(rel, None)
        # Bỏ cả dạng normcase: journal do bản bị lỗi chuẩn hóa có thể đang
        # giữ rel chữ thường cho đúng file này (tên trên đĩa có chữ HOA).
        self.rescan_pending.discard(rel)
        self.rescan_pending.discard(os.path.normcase(rel))
        if abs_norm:
            self._match_bucket_replace(abs_norm, match_dicts)
        else:
            # Hi hữu (abs_norm rỗng): lọc theo relative_path như bản cũ.
            kept: dict[str, list[dict]] = {}
            for key, bucket in self._matches_by_file.items():
                bucket2 = [
                    d
                    for d in bucket
                    if str(d.get("relative_path") or "") != rel
                ]
                if bucket2:
                    kept[key] = bucket2
            self._matches_by_file = kept
            self._match_bucket_append(match_dicts)
        self._append_line(
            {"t": "rpf", "p": rel, "a": abs_norm, "m": list(match_dicts)}
        )

    def save(self) -> None:
        """fsync the journal (power-cut durable up to the last event),
        compacting it when appends have outgrown the snapshot. Writer raw fd
        (D0): append lỗi đã rollback/poison ở `_append_line`; fsync lỗi raise
        (durability chưa chứng minh → caller chưa ACK) nhưng không poison."""
        self._writer.sync()
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
    def _append_line(self, event: dict) -> None:
        # D0: writer fail-closed dùng chung — append lỗi được rollback về
        # offset sạch (retry được) hoặc POISONED (từ chối mọi ghi kế tiếp);
        # không bao giờ để đuôi hỏng giữa file một cách âm thầm.
        self._writer.append_line(event)
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
            # D2: nghĩa vụ KIE-only sống sót qua compact (các file còn pending).
            for rel in sorted(self._kie_spool):
                if rel in self._done or rel in self._skipped:
                    continue
                f.write(
                    json.dumps(
                        {"t": "kspool", "p": rel, "x": self._kie_spool[rel]},
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
            flat_matches = self.matches  # view phẳng — flatten đúng 1 lần
            for match_dict in flat_matches:
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
            + len(flat_matches)
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
        self._writer = AtomicJsonlWriter(registry_path(), label="registry")
        self._writer_lock_fh = None
        # D0: True khi khóa single-writer bị giữ bởi tiến trình khác — mọi
        # record() bị từ chối; lookup vẫn đọc được (fail-safe không cache).
        self._readonly = False
        self._lines_at_compact = 1
        self._appended_lines = 0

    @property
    def readonly(self) -> bool:
        return self._readonly

    @staticmethod
    def _key(path: str, mode: str) -> str:
        return os.path.normcase(os.path.abspath(path)) + "\x00" + mode

    @classmethod
    def load(cls) -> "FileRegistry":
        """Replay the registry journal. Missing/corrupt data degrades to
        an empty registry — scanning simply proceeds without cache."""
        reg = cls()
        # D0: khóa single-writer ACQUIRE TRƯỚC khi replay (kể cả khi file
        # chưa tồn tại — registry trống vẫn phải giữ quyền ghi sắp tới).
        try:
            reg._acquire_writer_lock()
        except RegistryBusyError:
            import logging

            reg._readonly = True
            logging.getLogger(__name__).info(
                "registry: khóa single-writer bị giữ bởi tiến trình khác — "
                "chạy KHÔNG cache registry (lookup vẫn đọc snapshot cũ)"
            )
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
                    str(event.get("st") or "clean"),
                )
                if event.get("i"):
                    reg._inherited.add(key)
                else:
                    reg._inherited.discard(key)
        reg._lines_at_compact = lines_seen
        if bad_tail:
            # Tự chữa như SecretScanProgress.load — backup file hỏng rồi cắt
            # bỏ đuôi hỏng để các bản ghi thêm sau này vẫn được replay (D0:
            # recovery có kiểm soát phải backup trước khi sửa).
            try:
                _backup_corrupt(registry_path())
                reg._compact()
            except OSError:
                pass
        return reg

    def lookup(
        self, path: str, size: int, mtime: float, mode: str, version: str
    ) -> list[dict] | None:
        """Matches recorded for this exact unchanged file, or None."""
        hit = self.lookup_with_status(path, size, mtime, mode, version)
        return None if hit is None else hit[0]

    def lookup_with_status(
        self, path: str, size: int, mtime: float, mode: str, version: str
    ) -> tuple[list[dict], str] | None:
        """(matches, status) cho file không đổi, hoặc None.

        Status được bảo toàn xuyên suốt cache: entry "unconfirmed" (LightGBM
        lỗi — dương có cảnh báo) KHÔNG bao giờ được đọc lại như kết quả sạch
        đã hoàn tất."""
        entry = self._entries.get(self._key(path, mode))
        if entry is None:
            return None
        entry_size, entry_mtime, entry_version, matches = entry[:4]
        status = entry[4] if len(entry) > 4 else "clean"
        if entry_size != size or entry_mtime != mtime:
            return None
        if entry_version != version:
            return None
        return list(matches), str(status)

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
        status: str = "clean",
    ) -> None:
        """Append one registry record. ``inherited=True`` đánh dấu entry kế
        thừa từ journal bản cũ (lựa chọn 2/3 của resume nâng cấp) — nguồn
        gốc thống kê, không đổi hành vi lookup. ``status`` đi xuyên suốt
        cache (clean / confirmed_positive / unconfirmed).

        R3-01: append THẤT BẠI phải trả RAM về ĐÚNG trạng thái đĩa (entry
        cũ nguyên vẹn / không có entry mới) rồi NÂM lỗi lên caller. Giữ entry
        trong RAM khi đĩa chưa có sẽ để lookup ăn cache của kết quả chưa bền
        vững, và `save()` sau đó không tái tạo được event chưa từng ghi được
        (flush chỉ đẩy buffer sẵn có)."""
        if self._readonly:
            raise RegistryBusyError(
                "registry đang ở chế độ chỉ-đọc (khóa single-writer thuộc "
                "tiến trình khác) — không ghi kết quả"
            )
        key = self._key(path, mode)
        previous = self._entries.get(key)
        prev_inherited = key in self._inherited
        self._entries[key] = (size, mtime, version, list(matches), str(status))
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
        if str(status) != "clean":
            event["st"] = str(status)
        if inherited:
            event["i"] = 1
        try:
            self._append_line(event)
        except BaseException:
            if previous is None:
                self._entries.pop(key, None)
            else:
                self._entries[key] = previous
            if prev_inherited:
                self._inherited.add(key)
            else:
                self._inherited.discard(key)
            raise

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
            size, mtime, old_version, matches = entry[:4]
            status = entry[4] if len(entry) > 4 else "clean"
            if old_version == version:
                continue
            self._entries[key] = (size, mtime, version, matches, str(status))
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
        """fsync journal (power-cut durable up to the last event), compacting
        it when appends have outgrown the snapshot. Writer raw fd (D0): fsync
        lỗi raise (durability chưa chứng minh) nhưng không poison."""
        self._writer.sync()
        if self._appended_lines > max(
            _JOURNAL_FLOOR_LINES, self._lines_at_compact
        ):
            self._compact()

    def close(self) -> None:
        self._writer.close()
        self._release_writer_lock()

    @property
    def poisoned(self) -> bool:
        """D0: writer registry đang POISONED — mọi ghi bị từ chối; lookup
        RAM vẫn đọc được (dữ liệu cũ chưa bị phá) nhưng KHÔNG được ACK kết
        quả mới qua registry này cho tới recovery có kiểm soát."""
        return self._writer.poisoned

    def recover_writer(self) -> bool:
        """Recovery CÓ KIỂM SOÁT của writer registry (D0): rebuild snapshot
        từ RAM + backup file hỏng. CHỈ gọi khi không producer nào đang append
        (owner giữ state_lock)."""
        return self._writer.recover(self._compact)

    def discard(self) -> None:
        self.close()
        path = registry_path()
        for candidate in (path, path + ".tmp"):
            try:
                os.remove(candidate)
            except OSError:
                pass

    # -- khóa single-writer theo data directory (D0) ------------------------
    # Registry là sổ TOÀN APP: hai instance không được ghi/truncate cùng file
    # mà không phối hợp. Khóa = msvcrt.locking 1 byte trên file
    # `file_registry.jsonl.writerlock`, giữ suốt đời registry, nhả ở close().
    _WRITER_LOCK_NAME = "registry.writerlock"

    def _acquire_writer_lock(self) -> None:
        if self._writer_lock_fh is not None or os.name != "nt":
            return  # nền không Windows: bỏ qua (dev/linux), Windows là đích
        import msvcrt

        path = os.path.join(progress_dir(), self._WRITER_LOCK_NAME)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fh = open(path, "a+b")
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            fh.close()
            raise RegistryBusyError(
                "registry đang được mở bởi một tiến trình ScanIndex khác "
                "(khóa single-writer) — lượt này chạy KHÔNG cache registry"
            )
        self._writer_lock_fh = fh

    def _release_writer_lock(self) -> None:
        if self._writer_lock_fh is None:
            return
        try:
            import msvcrt

            self._writer_lock_fh.seek(0)
            msvcrt.locking(
                self._writer_lock_fh.fileno(), msvcrt.LK_UNLCK, 1
            )
        except OSError:
            pass
        finally:
            try:
                self._writer_lock_fh.close()
            except OSError:
                pass
            self._writer_lock_fh = None

    def _append_line(self, event: dict) -> None:
        # D0: writer fail-closed dùng chung — append lỗi rollback (retry
        # được) hoặc POISONED (từ chối mọi ghi kế tiếp tới khi recovery).
        self._writer.append_line(event)
        self._appended_lines += 1

    def _compact(self) -> None:
        self.close()
        path = registry_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"t": "h", "v": STATE_VERSION}) + "\n")
            for key in sorted(self._entries):
                entry = self._entries[key]
                size, mtime, version, matches = entry[:4]
                status = entry[4] if len(entry) > 4 else "clean"
                event: dict = {
                    "t": "r",
                    "k": key,
                    "s": size,
                    "m": mtime,
                    "v": version,
                    "x": matches,
                }
                if str(status) != "clean":
                    event["st"] = str(status)
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
