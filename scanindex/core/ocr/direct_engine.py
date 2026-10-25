"""
direct_ocr_engine.py - Drop-in replacement for chrome_ocr_engine.py
====================================================================
Uses chrome_screen_ai.dll directly (via screen_ai_ocr.py) instead of
launching Chrome browser + Selenium.

Produces _ocr.pdf: original page as background + invisible text overlay.
Text is rendered at LINE level (not word level) for proper alignment,
matching Chrome's "Save as PDF" output quality.
"""

import copy
import os
import re
import threading
import logging
import atexit
import unicodedata
from collections import deque

from scanindex.core.canonical_io import (
    DOCX_EXPORT_PROFILE_V1,
    save_canonical,
    load_canonical,
    resolve_profile,
)
from scanindex.core.ocr.screen_ai import ScreenAIOCR
from scanindex.core.kie.json_utils import (
    decorate_layout_regions,
    make_document_stub,
    make_line_record,
    make_page_record,
    make_word_record,
)
from scanindex.core.ocr.text_normalizer import (
    OCR_TEXT_NORMALIZATION,
    sanitize_ocr_surface_text as _shared_sanitize_ocr_surface_text,
)
from scanindex.infra.paths import get_base_dir, get_app_dir
from scanindex.core.runtime.shutdown import register_stage_stopper

logger = logging.getLogger(__name__)

_LATIN_VIET_RE = re.compile(r"[A-Za-zÀ-ỹĐđ]")
_DISALLOWED_SCRIPT_PREFIXES = {"CYRILLIC", "GREEK", "ARABIC"}
_CONFUSABLE_LATIN_MAP = {
    # Cyrillic → Latin (visually identical glyphs)
    "\u0410": "A", "\u0430": "a",  # А/а
    "\u0412": "B", "\u0432": "b",  # В/в
    "\u0421": "C", "\u0441": "c",  # С/с
    "\u0415": "E", "\u0435": "e",  # Е/е
    "\u041D": "H", "\u043D": "h",  # Н/н  ← was missing, caused word blanking
    "\u0406": "I", "\u0456": "i",  # І/і (Ukrainian)
    "\u0408": "J", "\u0458": "j",  # Ј/ј (Serbian)
    "\u041A": "K", "\u043A": "k",  # К/к
    "\u041C": "M", "\u043C": "m",  # М/м
    "\u041E": "O", "\u043E": "o",  # О/о
    "\u0420": "P", "\u0440": "p",  # Р/р
    "\u0405": "S", "\u0455": "s",  # Ѕ/ѕ (Macedonian DZE)
    "\u0422": "T", "\u0442": "t",  # Т/т
    "\u0423": "Y", "\u0443": "y",  # У/у
    "\u0425": "X", "\u0445": "x",  # Х/х
    # Greek → Latin (visually identical glyphs)
    "\u0391": "A", "\u03B1": "a",  # Α/α
    "\u0392": "B",                  # Β
    "\u0395": "E", "\u03B5": "e",  # Ε/ε
    "\u0397": "H", "\u03B7": "n",  # Η/η (Eta)
    "\u0399": "I", "\u03B9": "i",  # Ι/ι
    "\u039A": "K", "\u03BA": "k",  # Κ/κ
    "\u039C": "M",                  # Μ (Mu)
    "\u039D": "N",                  # Ν (Nu)
    "\u039F": "O", "\u03BF": "o",  # Ο/ο
    "\u03A1": "P", "\u03C1": "p",  # Ρ/ρ
    "\u03A4": "T",                  # Τ (Tau)
    "\u03A5": "Y", "\u03C5": "y",  # Υ/υ (Upsilon)
    "\u03A7": "X", "\u03C7": "x",  # Χ/χ
    "\u0396": "Z",                  # Ζ (Zeta)
}


def _env_int(name, default):
    try:
        return max(1, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def _env_int_allow_zero(name, default):
    """Parser cho cửa sổ staging: 0 = TẮT staging (hợp lệ) — không ép min 1
    như `_env_int` (P2-2: trước đây 0 bị ép thành 1, nhánh off không chạy)."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default

# ===========================================================================
# Module-level singleton & hybrid OCR pool
# ===========================================================================
_ocr_instance = None
_ocr_lock = threading.Lock()
_init_lock = threading.Lock()

# Page-level pool: N processes, each loads ONE ScreenAI DLL. Pool size
# = number of OCR pages that can run concurrently across the whole app
# (regardless of how many input files are being processed).
_NUM_PAGE_WORKERS = _env_int("DIRECT_OCR_NUM_PAGE_WORKERS", 2)
# Legacy env vars kept for backwards compat — they map to the new model:
#   total_workers = NUM_PROCESSES * NUM_DLL_PER_PROCESS
_LEGACY_NUM_PROCESSES = os.environ.get("DIRECT_OCR_NUM_PROCESSES")
_LEGACY_NUM_DLL_PER_PROCESS = os.environ.get("DIRECT_OCR_NUM_DLL_PER_PROCESS")
if _LEGACY_NUM_PROCESSES and _LEGACY_NUM_DLL_PER_PROCESS:
    try:
        _NUM_PAGE_WORKERS = max(1, int(_LEGACY_NUM_PROCESSES) * int(_LEGACY_NUM_DLL_PER_PROCESS))
    except ValueError:
        pass

# Kept as references for older call sites that still inspect them
_NUM_PROCESSES = _NUM_PAGE_WORKERS
_NUM_DLL_PER_PROCESS = 1

_mp_pool = None  # multiprocessing.Pool with NUM_PAGE_WORKERS processes
_mp_pool_lock = threading.Lock()
# Recycle a worker after this many tasks (guards against native DLL leaks).
# Default unchanged; env override exists for long-run leak/RSS measurement
# only — benchmark before changing the default (higher values trade RSS
# containment for per-process warm-up).
# Giá trị tắt tường minh (B7 — PLAN v5.1 §4.5-B.1): "off"/"none"/"disabled"/
# "0"/"-1" → None = KHÔNG recycle. Parser trước đây ép max(1, env) nên không
# thể tắt; sentinel này ánh xạ sang None ngay tại Pool constructor.
_MP_MAXTASKSPERCHILD_OFF = {"off", "none", "disabled", "-1", "0"}


def _parse_maxtasksperchild(name: str = "DIRECT_OCR_MAXTASKSPERCHILD", default: int = 100):
    raw = os.environ.get(name, "").strip().lower()
    if raw in _MP_MAXTASKSPERCHILD_OFF:
        return None
    return _env_int(name, default)


_MP_MAXTASKSPERCHILD = _parse_maxtasksperchild()

# ===========================================================================
# Admission gate + token registry (v4)
#
# Một gate toàn cục cho MỌI submission vào pool (submit_page + collector).
# - Slot chỉ được nhả khi task NATIVE kết thúc: qua callback/error_callback
#   của apply_async, hoặc janitor thu hồi token đã terminal mà callback bị lỡ
#   (worker chết đột ngột có thể không sinh callback nào). KHÔNG nhả theo
#   deadline — task kẹt giữ slot và được xử lý bằng quy trình phục hồi.
# - Tuổi thọ tối đa một request chưa xác định kết thúc native:
#   OCRTOOL_REQUEST_MAX_AGE (giây). Vượt hạn → request đánh dấu mất (caller
#   nhận PoolResetError hữu hạn) + có thể kích hoạt phục hồi thế hệ pool.
# - Kill-switch OCRTOOL_DISABLE_GLOBAL_INFLIGHT=1: tắt GIỚI HẠN scheduling;
#   registry/ janitor/ deferred cleanup vẫn chạy.
# ===========================================================================
from scanindex.core.ocr.cancellation import (  # noqa: E402
    CoreScanCancelled,
    PoolResetError,
    SubmissionBusy,
)

_REQUEST_MAX_AGE = float(_env_int("OCRTOOL_REQUEST_MAX_AGE", 600))
_ADMISSION_WAIT_S = float(_env_int("OCRTOOL_ADMISSION_WAIT_S", 300))
# GĐ W (PLAN v5.1 §4.2): staged warmup + supervisor ACK.
# OCRTOOL_WARMUP_STAGE = k (tối đa k worker nạp DLL/model đồng thời; 0 = tắt
# staging — initializer tự nạp đầy như trước, chấp nhận rủi ro stampede).
# OCRTOOL_WARMUP_DEADLINE_S = deadline MỖI worker từ lúc nhận permit tới ACK.
_WARMUP_STAGE_DEFAULT = _env_int_allow_zero("OCRTOOL_WARMUP_STAGE", 4)
_WARMUP_DEADLINE_S = float(_env_int("OCRTOOL_WARMUP_DEADLINE_S", 120))
# P1-01: CHỜ permit nạp khi mọi đường load (warmup, self-load task OCR đầu,
# recycle respawn) đi qua cùng limiter theo worker incarnation. P1-F (review 2):
# hết hạn = FAIL-CLOSED (`LoadPermitTimeout`) — task OCR trả sentinel
# `worker_init_failed` / warmup ACK failed → `_request_pool_recovery`
# (terminate chứng minh holder dừng) → generation mới, limiter mới, thang
# retry hữu hạn. Không nạp ngoài budget để "tránh chờ".
_WARMUP_LOAD_PERMIT_WAIT_S = float(_env_int("OCRTOOL_WARMUP_LOAD_WAIT_S", 300))
_GATE_DISABLED = os.environ.get("OCRTOOL_DISABLE_GLOBAL_INFLIGHT", "").lower() in {
    "1", "true"
}
_admission_lock = threading.Lock()
_admission_cond = threading.Condition(_admission_lock)
_admission_tokens: dict[int, dict] = {}
_admission_next_id = iter(range(1, 1 << 62))
_pool_generation = 0
_gate_sem = None  # BoundedSemaphore, dựng theo thế hệ pool (từ _get_pool)
# Generation mà `_mp_pool` đang thuộc về (P1-B): None = chưa có pool/chưa theo
# dõi (đường test giả lập); -1 = pool đang dying (shutdown/recovery) — mọi
# submission phải bị từ chối cho tới khi pool thế hệ mới được publish.
_mp_pool_gen: int | None = None
# Trạng thái vòng đời pool cho telemetry (PLAN v5.1 §4.1 — schema GĐ R).
_pool_state = "idle"
_recovering_gen: int | None = None
_janitor_thread: threading.Thread | None = None
_janitor_enabled = True  # test có thể tắt thread nền
_live_inputs: dict[str, int] = {}
_deferred_cleanups: list[tuple[str, float]] = []
_admission_metrics = {
    "submitted": 0,
    "busy_rejections": 0,
    "completed": 0,
    "errors": 0,
    "reaped": 0,
    "lost": 0,
    "resets": 0,
    "recovery_failures": 0,
    "queue_wait_ms": deque(maxlen=512),   # submit → worker bắt đầu
    "admission_wait_ms": deque(maxlen=512),  # caller bắt đầu yêu cầu → có slot
    "ocr_ms": deque(maxlen=512),          # worker bắt đầu → worker xong
    # Tổng cộng toàn lượt (P2-H): deque 512 không đủ tính busy%/tổng.
    "ocr_ms_total": 0,
    "ocr_ms_count": 0,
    "run_first_submit_at": None,  # monotonic — mốc wall toàn lượt
}


def _gate_capacity() -> int:
    """Số slot admission MỤC TIÊU (requested) = số worker pool; env nguyên
    dương ghi đè.

    KHÔNG dùng `_env_int` ở đây vì nó ép min 1 — cần phân biệt "không đặt
    override" (dùng capacity) với số nguyên dương do người đo đặt."""
    raw = os.environ.get("OCRTOOL_GLOBAL_IN_FLIGHT_PAGES", "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value > 0:
            return value
    return max(1, get_parallel_capacity())


class _AdmissionGate:
    """Gate admission với capacity THAY ĐỔI ĐƯỢC trong cùng generation (W-6,
    hợp đồng gate-A của REVIEW_READINESS A).

    - KHÔNG thay object gate giữa chừng → callback của token cũ luôn release
      vào ĐÚNG gate của thế hệ mình.
    - `ready=0` ⇒ acquire(blocking=False) luôn False (Busy/warming) — không ép
      min 1 rồi nộp vào worker chưa sẵn sàng.
    - `set_capacity()` ghi TARGET mới; admission so `held` với TARGET (P2-1)
      — shrink không rút token sống nhưng cũng KHÔNG mở lại slot sai: acquire
      2, target 0, release 1 vẫn bị chặn tới khi target tăng lại.
    - `_value` giữ tương thích kiểu BoundedSemaphore (test v4 đọc `_value`);
      `capacity` = mức đồng thời khả dụng hiện tại = max(held, target).
    """

    def __init__(self, capacity: int):
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._target = max(0, int(capacity))
        self._held = 0

    @property
    def capacity(self) -> int:
        with self._lock:
            return max(self._held, self._target)

    @property
    def target(self) -> int:
        with self._lock:
            return self._target

    @property
    def _value(self) -> int:
        with self._lock:
            return max(0, self._target - self._held)

    def set_capacity(self, new_capacity: int) -> None:
        with self._cond:
            self._target = max(0, int(new_capacity))
            self._cond.notify_all()

    def acquire(self, blocking: bool = False, timeout: float | None = None) -> bool:
        import time as _t

        with self._cond:
            if blocking:
                end = None if timeout is None else _t.monotonic() + float(timeout)
                while self._held >= self._target:
                    if end is None:
                        self._cond.wait(0.05)
                        continue
                    remaining = end - _t.monotonic()
                    if remaining <= 0:
                        return False
                    self._cond.wait(min(0.05, remaining))
            else:
                if self._held >= self._target:
                    return False
            self._held += 1
            return True

    def release(self) -> None:
        with self._cond:
            if self._held > 0:
                self._held -= 1
            self._cond.notify_all()


def _ensure_gate_locked() -> None:
    """Gate phải tồn tại cùng thế hệ pool — gọi khi tạo pool và khi thiếu."""
    global _gate_sem
    if _gate_sem is None:
        _gate_sem = _AdmissionGate(_gate_capacity())


# ---------------------------------------------------------------------------
# GĐ W — Warmup supervisor: sổ cái permit theo incarnation, ACK, staging.
# Thiết kế: initializer CHỈ nhẹ (stdio + lưu path); DLL/model do CHÍNH worker
# tự nạp khi chạy `_warmup_task` (do supervisor post theo đợt k) hoặc khi nhận
# task OCR đầu tiên (`_ensure_worker_loaded`). Không dùng semaphore liên
# process (probe 21/10: permit bị mất vĩnh viễn khi holder crash).
# ---------------------------------------------------------------------------
_warmup_lock = threading.Lock()
_warmup = {
    "generation": None,       # generation đang warmup
    "requested": 0,           # N worker hiệu lực thế hệ này (sau thang retry)
    "stage_k": 0,             # cửa sổ nạp đồng thời hiện tại
    "in_flight": 0,           # số warmup task đã post chưa về
    # Sổ cái ACK THEO INCARNATION (P1-02): pid -> {rss_mb, t_load_s, ack_at}.
    # Chỉ pid SỐNG (còn nằm trong pool hiện tại) được tính là ready sống.
    "acked_pids": {},
    "failed": [],             # [{pid, error}]
    "t_start": None,
    "deadline_timer": None,
    "done": False,
    # P1-02: deadline có ready>0 → suy giảm; ACK path VẪN MỞ để worker nạp muộn
    # tiếp tục ramp gate (dù không còn post warmup task mới). Shutdown/recovery
    # đặt done mà KHÔNG đặt degraded → ACK bị bỏ hẳn.
    "degraded": False,
    "gate": None,             # gate của generation này (set_capacity theo ACK)
}
_warmup_retries = 0  # đếm số lần dựng lại generation do warmup hụt deadline


def _warmup_stage_plan(requested: int) -> tuple[int, int]:
    """(số worker hiệu lực, cửa sổ k) theo thang retry hữu hạn của plan:
    lần đầu N/k=4; retry 1: N/k=1; retry 2: max(1, floor(N/2))/k=1; hết —
    lần thứ 4 KHÔNG tạo generation mới (fail-loud, `OCRWarmupError`).
    Staging tắt (`OCRTOOL_WARMUP_STAGE<=0`) → k=N: post đủ một lần, không có
    cửa sổ (P2-2: trước đây 0 bị ép thành 1 nên nhánh off không chạy đúng)."""
    attempts = _warmup_retries
    if attempts == 0:
        if _WARMUP_STAGE_DEFAULT <= 0:
            return requested, requested
        k = _WARMUP_STAGE_DEFAULT
        return requested, min(k, requested)
    if attempts == 1:
        return requested, 1
    if attempts == 2:
        return max(1, requested // 2), 1
    raise OCRWarmupError(
        f"OCR warmup failed after {attempts} attempts — không tạo generation "
        "thứ tư (plan v5.1 §4.2.0). Vui lòng khởi động lại ứng dụng."
    )


def _live_worker_pids(pool) -> set | None:
    """Tập PID worker ĐANG SỐNG của pool (P1-02: liveness cho sổ ACK).
    Trả None khi không đọc được (pool giả/test/không có `_pool`) — caller
    ĐẾM MỌI ACK (không prune: prune trên pool giả sẽ xóa nhầm ACK thật)."""
    try:
        procs = getattr(pool, "_pool", None)
        if procs is None:
            return None  # pool giả/test — không có danh sách process thật
        live = set()
        for p in procs:
            try:
                pid = p.pid
                if pid and p.is_alive():
                    live.add(pid)
            except Exception:
                continue
        return live
    except Exception:
        return None


def _prune_dead_acks_locked() -> int:
    """D1 (re-ACK theo incarnation): sổ ACK CHỈ ĐƯỢC ĐẾM worker SỐNG —
    entry của worker đã chết/recycle bị LOẠI NGAY, không dùng sổ PID lịch sử
    làm capacity (pid chết còn trong sổ = gate mở rộng oan cho worker không
    còn native). Gọi giữ `_warmup_lock`. Trả số entry bị loại."""
    live = _live_worker_pids(_mp_pool)
    if live is None:
        return 0
    dead = [pid for pid in _warmup["acked_pids"] if pid not in live]
    for pid in dead:
        _warmup["acked_pids"].pop(pid, None)
    if dead:
        logger.warning(
            "warmup: loại %d ACK của worker đã chết/recycle (còn sống=%d) — "
            "gate capacity chỉ đếm ready LIVE",
            len(dead), len(live),
        )
    return len(dead)


def _ocr_stage_stopper(soft: bool) -> None:
    """D1 stopper stage W: soft = ngừng nhận việc mới (worker chạy nốt);
    hard = terminate NGAY toàn bộ worker process (dừng hữu hạn)."""
    global _mp_pool
    with _mp_pool_lock:
        pool = _mp_pool
        if pool is None:
            return
        if not soft:
            _mp_pool = None
    from scanindex.core.runtime.shutdown import track_executor
    track_executor(pool)
    if soft:
        pool.close()
    else:
        pool.terminate()


register_stage_stopper("W-ocr-pool", _ocr_stage_stopper)


class OCRWarmupError(RuntimeError):
    """Warmup pool thất bại sau các lần retry hữu hạn — fail-loud."""


def _worker_rss_mb() -> int | None:
    try:
        from scanindex.infra.process_metrics import sample_tree
        return int(sample_tree()["parent_rss_mb"])
    except Exception:
        logger.warning("Worker RSS unavailable", exc_info=True)
        return None


def _load_worker_runtime(dll_path: str, model_dir: str) -> None:
    """Nạp DLL ScreenAI + model trong worker process (gọi MỘT lần/process).

    P1-01: MỌI đường nạp (warmup task, self-load task OCR đầu, recycle respawn)
    đi qua CÙNG permit nạp do supervisor cấp (`_worker_load_sem`, cửa sổ k)
    — không còn đường self-load ngoài limiter. P1-F: hết hạn xin permit =
    fail-closed (`LoadPermitTimeout`) — recovery là đường duy nhất mở lại
    quyền nạp (terminate chứng minh holder dừng, generation mới có limiter
    mới), bất biến ≤k không bao giờ bị phá để tránh chờ."""
    global _worker_ocr, _worker_dll_path, _worker_init_error
    if _worker_ocr is not None:
        return
    with _load_permit_scope():
        try:
            ocr = ScreenAIOCR(dll_path=dll_path, model_dir=model_dir)
            ocr.initialize()
            _worker_ocr = ocr
            _worker_dll_path = dll_path
            _worker_init_error = None
        except Exception as exc:
            _worker_ocr = None
            _worker_dll_path = dll_path
            _worker_init_error = f"{type(exc).__name__}: {exc}"
            logger.exception("ScreenAI worker initialization failed")
            raise


class LoadPermitTimeout(RuntimeError):
    """P1-F (review 2): không xin được permit nạp trong `_WARMUP_LOAD_PERMIT_WAIT_S`
    — FAIL-CLOSED: KHÔNG được nạp ngoài budget (permit có thể đang được giữ
    bởi worker còn sống/treo trong DLL). Caller xử lý như `worker_init_failed`
    → collector/supervisor kích hoạt `_request_pool_recovery` (terminate dừng
    thật holder treo) → generation mới có limiter mới, theo thang retry hữu
    hạn. Trước đây nhánh này fail-open (nạp không permit) — phá bất biến ≤k."""


def _load_permit_scope():
    """Context manager giữ permit nạp + cập nhật probe đồng thời (quan sát
    mức nạp cực đại trong test/telemetry). Không có limiter → no-op.

    P1-F: hết thời gian xin permit → raise `LoadPermitTimeout` (fail-closed),
    KHÔNG tiếp tục nạp. Thu hồi quyền nạp chỉ qua recovery chứng minh holder
    dừng (terminate) — không mở tải native ngoài budget để tránh chờ."""
    import contextlib

    @contextlib.contextmanager
    def _scope():
        sem = _worker_load_sem
        probe = _worker_load_probe
        permit = None
        if sem is not None:
            try:
                permit = sem.acquire(timeout=_WARMUP_LOAD_PERMIT_WAIT_S)
            except Exception as exc:
                raise LoadPermitTimeout("Không nhận được load permit") from exc
            if not permit:
                raise LoadPermitTimeout(
                    f"load permit chờ quá {_WARMUP_LOAD_PERMIT_WAIT_S:.0f}s "
                    "— fail-closed (holder có thể còn sống/treo); recovery "
                    "phải chứng minh holder dừng trước khi nạp lại"
                )
        if probe is not None:
            try:
                with probe.get_lock():
                    probe.value += 1
            except Exception:
                probe = None
        try:
            yield
        finally:
            if probe is not None:
                try:
                    with probe.get_lock():
                        probe.value -= 1
                except Exception:
                    pass
            if permit and sem is not None:
                try:
                    sem.release()
                except Exception:
                    pass

    return _scope()


def _simulate_worker_load() -> None:
    """Test-mode (OCRTOOL_WARMUP_TEST_MODE=1): mô phỏng thời gian nạp DLL
    đi ĐÚNG đường permit/probe như nạp thật — cho phép test giới hạn mức
    nạp đồng thời với Pool thật mà không đụng DLL."""
    import time as _t

    sleep_s = float(os.environ.get("OCRTOOL_WARMUP_TEST_SLEEP", "0") or 0)
    with _load_permit_scope():
        if sleep_s > 0:
            _t.sleep(sleep_s)


def _ensure_worker_loaded() -> None:
    """Self-load trong worker (task OCR đầu tiên hoặc warmup task) — an toàn
    respawn: worker recycle tái khởi tạo nhẹ, lần task kế tự nạp lại. Nạp đi
    qua permit chung của supervisor (P1-01) — kể cả respawn."""
    global _worker_ocr
    if _worker_ocr is not None:
        return
    if _worker_dll_path is None:
        raise RuntimeError("worker runtime paths not initialized")
    if os.environ.get("OCRTOOL_WARMUP_TEST_MODE") == "1":
        _simulate_worker_load()
        _worker_ocr = _SENTINEL_LOADED
        return
    _load_worker_runtime(_worker_dll_path, _worker_model_dir)


_SENTINEL_LOADED = object()


def _warmup_task(gen: int = 0):
    """Chạy TRONG worker: nạp runtime, trả thống kê ACK (pid/generation/rss/
    thời gian). Generation gắn TỪ payload để supervisor nhận diện ACK của thế
    hệ nào (P1-02 — không đối chiếu state toàn cục đang thay đổi).
    Env `OCRTOOL_WARMUP_TEST_MODE=1` (kế thừa qua spawn) → mô phỏng nạp qua
    permit, không đụng DLL — dùng cho test supervisor với Pool thật."""
    import os as _os
    import time as _t

    t0 = _t.perf_counter()
    err = None
    try:
        _ensure_worker_loaded()
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
    return {
        "ok": err is None,
        "pid": _os.getpid(),
        "generation": int(gen),
        "rss_mb": _worker_rss_mb(),
        "t_load_s": round(_t.perf_counter() - t0, 2),
        "error": err,
    }


def _on_warmup_ack(stats, gen: int) -> None:
    """ACK warmup — generation bind TẠI LÚC POST (P1-02): callback đối chiếu
    với generation của chính nó, không đọc lại state toàn cục đang thay đổi
    (ACK thế hệ cũ không thể lọt vào sổ thế hệ mới). CHỈ bookkeeping nhanh."""
    import time as _t

    pid = (stats or {}).get("pid")
    with _warmup_lock:
        if gen != _pool_generation:
            return  # ACK của thế hệ khác — bỏ
        if _warmup["done"]:
            if not _warmup["degraded"]:
                return  # shutdown/recovery đã đóng sổ — bỏ
            # Suy giảm do deadline: KHÔNG post thêm, nhưng ACK muộn vẫn được
            # nhận để ramp gate (log "ACK hụt tự phục hồi" phải đúng thật).
            if stats and stats.get("ok") and pid is not None \
                    and pid not in _warmup["acked_pids"]:
                _warmup["acked_pids"][pid] = {
                    "rss_mb": stats.get("rss_mb"),
                    "t_load_s": stats.get("t_load_s"),
                    "ack_at": _t.monotonic(),
                }
                # D1: gate chỉ ramp theo ready LIVE.
                _prune_dead_acks_locked()
                gate = _warmup["gate"]
                if gate is not None:
                    gate.set_capacity(
                        min(len(_warmup["acked_pids"]), _warmup["requested"])
                    )
                logger.info(
                    "warmup late ACK (degraded) %d/%d (pid=%s) — gate ramp",
                    len(_warmup["acked_pids"]), _warmup["requested"], pid,
                )
            return
        if stats and not stats.get("ok"):
            _warmup["failed"].append({"pid": pid, "error": stats.get("error")})
            logger.error("warmup ACK FAILED pid=%s: %s", pid, stats.get("error"))
        elif pid is not None and pid not in _warmup["acked_pids"]:
            _warmup["acked_pids"][pid] = {
                "rss_mb": stats.get("rss_mb"),
                "t_load_s": stats.get("t_load_s"),
                "ack_at": _t.monotonic(),
            }
            logger.info(
                "warmup ready %d/%d (pid=%s, rss=%sMB, t_load=%ss)",
                len(_warmup["acked_pids"]), _warmup["requested"], pid,
                stats.get("rss_mb"), stats.get("t_load_s"),
            )
        _warmup["in_flight"] = max(0, _warmup["in_flight"] - 1)
        # D1: ready = số ACK của worker CÒN SỐNG — prune dead trước khi đếm.
        _prune_dead_acks_locked()
        ready = len(_warmup["acked_pids"])
        gate = _warmup["gate"]
        if gate is not None:
            gate.set_capacity(min(ready, _warmup["requested"]))
        # P1-01: post TIẾP theo CỬA SỔ k — không post hết remaining sau ACK
        # đầu (trước đây N=8, k=2 → ACK đầu post 6 task, in_flight=7).
        _repost_or_finish_locked()


def _repost_or_finish_locked() -> None:
    """Đánh giá lại sau mỗi ACK/error khi đang giữ `_warmup_lock`: post thêm
    NHIỀU NHẤT một cửa sổ k task (nếu còn dư) hoặc chốt DONE khi đủ N. Dùng
    chung cho ACK/error; KHÔNG tự gọi lại chính nó (post không repost —
    đánh giá kế tiếp diễn ra ở ACK/error kế)."""
    ready = len(_warmup["acked_pids"])
    remaining = _warmup["requested"] - _warmup["in_flight"] - ready \
        - len(_warmup["failed"])
    if remaining > 0:
        window = _warmup["stage_k"] if _warmup["stage_k"] > 0 else remaining
        _post_warmup_tasks_locked(min(window, remaining))
        if _warmup["in_flight"] <= 0 and ready < _warmup["requested"]:
            logger.error(
                "warmup: post thất bại toàn bộ (ready=%d, failed=%d) — chờ "
                "deadline để suy giảm/recovery",
                ready, len(_warmup["failed"]),
            )
    elif ready >= _warmup["requested"]:
        _finish_warmup_locked()
    elif _warmup["in_flight"] <= 0:
        # Chỉ là ngõ cụt THẬT khi không còn gì đang bay; còn in_flight>0 là
        # trạng thái chờ bình thường (ACK đang về, không post thêm).
        logger.error(
            "warmup error: không còn warmup task để post (ready=%d, "
            "failed=%d) — chờ deadline để suy giảm/recovery",
            ready, len(_warmup["failed"]),
        )


def _post_warmup_tasks_locked(count: int) -> None:
    """Post thêm warmup task — gọi khi đang giữ `_warmup_lock`. Generation
    bind vào callback NGAY TẠI ĐÂY (closure) để ACK muộn sau khi thế hệ đổi
    không lọt sổ mới. apply_async ném lỗi đồng bộ được xử lý NỘI BỘ (không
    gọi lại `_on_warmup_error` — nó lấy lại `_warmup_lock` non-reentrant,
    deadlock — P2-2)."""
    global _warmup
    pool = _mp_pool
    if pool is None or count <= 0:
        return
    gen = _pool_generation
    _warmup["in_flight"] += count
    for _ in range(count):
        try:
            pool.apply_async(
                _warmup_task, (gen,),
                callback=lambda s, g=gen: _on_warmup_ack(s, g),
                error_callback=lambda e, g=gen: _on_warmup_error(e, g),
            )
        except Exception as exc:
            # Xử lý failure NGAY TRONG lock này — không re-enter lock.
            _warmup["in_flight"] = max(0, _warmup["in_flight"] - 1)
            _warmup["failed"].append({"pid": None, "error": str(exc)})
            logger.error("warmup post FAILED (sync): %s", exc)


def _on_warmup_error(exc, gen: int = 0) -> None:
    with _warmup_lock:
        if gen != _pool_generation:
            return
        if _warmup["done"] and not _warmup["degraded"]:
            return
        if _warmup["done"]:
            return  # degraded: không post thêm — bỏ error của task cũ
        _warmup["failed"].append({"pid": None, "error": str(exc)})
        _warmup["in_flight"] = max(0, _warmup["in_flight"] - 1)
        _repost_or_finish_locked()


def _finish_warmup_locked() -> None:
    import time as _t

    if _warmup["done"]:
        return
    _warmup["done"] = True
    timer = _warmup.get("deadline_timer")
    if timer is not None:
        timer.cancel()
    logger.info(
        "warmup DONE gen=%s ready=%d/%d failed=%d trong %.1fs",
        _warmup["generation"], len(_warmup["acked_pids"]),
        _warmup["requested"], len(_warmup["failed"]),
        (_t.monotonic() - _warmup["t_start"]) if _warmup["t_start"] else 0.0,
    )


def _warmup_deadline_tick() -> None:
    """Deadline MỖI worker: nếu generation chưa xong warmup sau deadline →
    0 ACK: quarantine + dựng lại theo thang retry (k=1 rồi N/2) — terminate
    của recovery là thứ DỪNG thật worker đang init treo (W-3). Có ready>0:
    suy giảm với capacity=ready (KHÔNG đập generation, KHÔNG hủy timer), và
    CHỈ ĐỦ: giữ ACK path mở (`degraded`) để worker nạp muộn vẫn ramp gate
    (P1-02 — trước đây đóng sổ luôn, mâu thuẫn log 'tự phục hồi')."""
    global _warmup_retries
    with _warmup_lock:
        if _warmup["done"] or _warmup["generation"] != _pool_generation:
            return
        # D1: ready chỉ đếm worker SỐNG (sổ ACK không phải sổ PID lịch sử).
        _prune_dead_acks_locked()
        ready = len(_warmup["acked_pids"])
        if ready > 0:
            # Có bộ phận sẵn sàng: KHÔNG đập cả generation — chạy suy giảm
            # với capacity = ready (log loud), warmup coi như kết thúc.
            logger.error(
                "warmup DEADLINE: chỉ %d/%d worker sẵn sàng — chạy suy giảm "
                "với effective=%d (ACK muộn vẫn ramp gate; worker nạp treo "
                "sẽ tự nạp ở task đầu hoặc bị recovery sau hụt deadline)",
                ready, _warmup["requested"], ready,
            )
            _warmup["done"] = True
            _warmup["degraded"] = True
            timer = _warmup.get("deadline_timer")
            if timer is not None:
                timer.cancel()
                _warmup["deadline_timer"] = None
            return
        _warmup_retries += 1
        logger.error(
            "warmup DEADLINE sau %.0fs mà 0/%d worker ACK — quarantine thế "
            "hệ %s, dựng lại theo thang retry (lần %d)",
            _WARMUP_DEADLINE_S, _warmup["requested"],
            _warmup["generation"], _warmup_retries,
        )
    _request_pool_recovery("warmup deadline exceeded")


def _start_warmup_supervisor_locked(requested: int, stage_k: int) -> None:
    """Khởi động supervisor cho thế hệ vừa publish. Gọi khi đang giữ
    `_warmup_lock`; gate capacity bắt đầu 0 (ready=0 ⇒ Busy/warming) rồi
    ramp theo ACK — không bao giờ chặn `_get_pool()`."""
    import time as _t

    with _admission_lock:
        gate = _gate_sem
    _warmup.update({
        "generation": _pool_generation,
        "requested": requested,
        "stage_k": stage_k,
        "in_flight": 0,
        "acked_pids": {},
        "failed": [],
        "t_start": _t.monotonic(),
        "deadline_timer": None,
        "done": False,
        "degraded": False,
        "gate": gate,
    })
    if gate is not None:
        gate.set_capacity(0)  # ready=0 ⇒ Busy/warming, hủy được ở caller
    logger.info(
        "warmup starting gen=%s requested=%d stage=%d (deadline %.0fs)",
        _pool_generation, requested, stage_k, _WARMUP_DEADLINE_S,
    )
    timer = threading.Timer(_WARMUP_DEADLINE_S, _warmup_deadline_tick)
    timer.daemon = True
    timer.start()
    _warmup["deadline_timer"] = timer
    _post_warmup_tasks_locked(min(stage_k, requested))



def _admission_snapshot_metrics() -> str:
    import time as _t

    def _pct(values, q):
        ordered = sorted(values)
        if not ordered:
            return None
        return ordered[min(int(len(ordered) * q), len(ordered) - 1)]

    m = _admission_metrics

    def _fmt(name, values):
        if not values:
            return f"{name} n/a"
        p50 = _pct(values, 0.5)
        p95 = _pct(values, 0.95)
        return f"{name} p50={p50}ms p95={p95}ms"

    busy = ""
    first = m.get("run_first_submit_at")
    if first is not None and m["ocr_ms_count"]:
        wall_s = max(_t.monotonic() - first, 0.001)
        cap = max(1, _gate_capacity())
        # Ước lượng worker-bận trên worker-wall (P2-H): tổng ocr_ms chia
        # (capacity × wall). Đây là busy% worker-wall, KHÔNG phải CPU%.
        busy_pct = 100.0 * (m["ocr_ms_total"] / 1000.0) / (cap * wall_s)
        busy = f" ocr_total={m['ocr_ms_count']} task/{m['ocr_ms_total']}ms worker_busy_est={min(busy_pct, 999):.0f}%"
    return (
        f"[admission] gen={_pool_generation} state={_pool_state} "
        f"submitted={m['submitted']} completed={m['completed']} "
        f"errors={m['errors']} busy={m['busy_rejections']} "
        f"reaped={m['reaped']} lost={m['lost']} resets={m['resets']} "
        f"recovery_failures={m['recovery_failures']}"
        f"{busy} "
        f"{_fmt('worker_start_wait', m['queue_wait_ms'])} "
        f"{_fmt('admission_wait', m['admission_wait_ms'])} "
        f"{_fmt('ocr_ms', m['ocr_ms'])}"
    )


def admission_metrics_summary() -> str:
    """Dòng tổng hợp cho log cuối lượt (gọi từ UI thread, chỉ đọc)."""
    with _admission_lock:
        return _admission_snapshot_metrics()


def _kie_telemetry() -> object:
    """Backlog KIE từ coordinator GĐ K (nếu đang chạy); không import cứng."""
    try:
        from scanindex.core.secret_scan_kie import telemetry as _kie_tel

        return _kie_tel()
    except Exception:
        return "n/a (stage K không hoạt động)"


def _prep_telemetry() -> object:
    """Hàng đợi prep từ executor GĐ P (nếu bật); không import cứng."""
    try:
        from scanindex.core.preprocessing.prep_worker import telemetry as _p_tel

        return _p_tel()
    except Exception:
        return "n/a (stage P không hoạt động)"


def pool_telemetry() -> dict:
    """Telemetry trạng thái pool — schema chung của GĐ R (PLAN v5.1 §4.1).

    Các trường thuộc giai đoạn sau trả "n/a ..." — không bịa số; stage tương
    ứng cung cấp thay. Chỉ đọc trạng thái nội bộ, gọi từ thread log/UI được."""
    with _admission_lock:
        inflight = sum(
            1 for info in _admission_tokens.values() if not info["done"]
        )
        gate = _gate_sem
        if _GATE_DISABLED or gate is None:
            effective = 0
        elif isinstance(gate, _AdmissionGate):
            effective = gate.capacity
        else:  # BoundedSemaphore do test gán — capacity ước lượng theo config
            effective = _gate_capacity()
    with _warmup_lock:
        if _warmup["generation"] == _pool_generation:
            acked = dict.fromkeys(_warmup["acked_pids"])
            warming = not _warmup["done"]
        else:
            acked = None
            warming = False
    # P1-02/P2-7: ready sống = ACK ∩ PID worker đang sống của pool hiện tại
    # (trước đây đếm cả PID đã chết/recycle — 'ever-acked', không phải live).
    if acked is None:
        ready_live = None
    else:
        with _mp_pool_lock:
            pool_now = _mp_pool
        live = _live_worker_pids(pool_now) if pool_now is not None else None
        ready_live = (
            len(acked) if live is None
            else sum(1 for pid in acked if pid in live)
        )
    ready_live_disp = (
        ready_live if ready_live is not None
        else "n/a (pool chưa publish ở thế hệ này)"
    )
    load_probe_val = None
    if _load_probe_ref is not None:
        try:
            load_probe_val = int(_load_probe_ref.value)
        except Exception:
            load_probe_val = None
    return {
        "generation": _pool_generation,
        "state": ("warming" if warming else _pool_state),
        "requested_workers": int(_NUM_PAGE_WORKERS),
        "effective_capacity": effective,
        # Stage W ĐÃ TRIỂN KHAI: số worker ACK đang SỐNG của thế hệ hiện tại.
        "ready_live_workers": ready_live_disp,
        "warmup_loads_in_flight": load_probe_val,
        "ocr_inflight": inflight,
        "leases_held": len(_admission_tokens),
        "live_inputs": sum(_live_inputs.values()),
        "recovery_failures": _admission_metrics["recovery_failures"],
        # Stage K (pipeline KIE): backlog queued/running/chờ-commit + byte.
        "kie_backlog": _kie_telemetry(),
        # Stage P (prep process): hàng đợi prep + byte buffer.
        "prep_backlog": _prep_telemetry(),
        # Harness benchmark (stage B) đo CPU/RSS cây ngoài process này.
        "cpu_rss_tree": "n/a (stage B)",
    }


def pool_telemetry_line() -> str:
    t = pool_telemetry()
    return (
        f"[telemetry] gen={t['generation']} state={t['state']} "
        f"req={t['requested_workers']} eff_cap={t['effective_capacity']} "
        f"ready={t['ready_live_workers']} inflight={t['ocr_inflight']} "
        f"leases={t['leases_held']} live_inputs={t['live_inputs']} "
        f"kie={t['kie_backlog']} prep={t['prep_backlog']} cpu_rss={t['cpu_rss_tree']}"
    )


def _fail_token_locked(token_id: int, exc: Exception,
                       release_resources: bool = True) -> None:
    """Đánh dấu request THẤT BẠI hữu hạn: caller nhận lỗi qua error-sink.

    P1-A (REVIEW v2): trạng thái **outcome** (done/error/sink) tách khỏi
    trạng thái **lease** (token trong registry + gate slot + live-input).
    `release_resources=False` (request lost, native CÓ THỂ CÒN CHẠY): caller
    nhận lỗi ngay nhưng lease GIỮ. Lần gọi SAU với `release_resources=True`
    (shutdown/recovery đã chứng minh native dừng, hoặc janitor thấy `ar.ready`)
    PHẢI thu được lease dù outcome đã báo trước đó — release idempotent qua
    pop. Thu hồi lease KHÔNG bị chặn bởi guard done."""
    info = _admission_tokens.get(token_id)
    if info is not None and not info["done"]:
        info["done"] = True
        info["error"] = exc
        sink = info.get("sink")
        if sink is not None:
            sink.append(exc)
    if release_resources:
        _admission_release_locked(token_id)


def _janitor_tick() -> None:
    """Một nhịp janitor: thu hồi token terminal-bị-lỡ, xử lý request quá tuổi
    thọ (giữ NGUYÊN lease tài nguyên đến khi native kết thúc/thế hệ bị dừng),
    chạy deferred cleanup."""
    import time as _t

    now = _t.monotonic()
    lost_tokens: list[tuple[int, str]] = []
    with _admission_lock:
        for token_id, info in list(_admission_tokens.items()):
            ar = info.get("ar")
            # (a) task đã terminal (callback bị lỡ HOẶC lost trước đó giờ
            # native thật sự xong) → nhả lease BẤT KỂ outcome đã báo chưa
            # (P1-A: janitor được thu lease terminal kể cả khi done=True).
            if ar is not None and ar.ready():
                if not info.get("reaped"):
                    info["reaped"] = True
                    _admission_metrics["reaped"] += 1
                _admission_release_locked(token_id)
                continue
            if info["done"]:
                # outcome đã báo; lease chỉ nhả khi native terminal (nhánh a)
                # hoặc shutdown/recovery chứng minh thế hệ đã dừng.
                continue
            # (b) vượt tuổi thọ mà KHÔNG xác định được kết thúc native →
            # caller nhận lỗi NGAY nhưng slot/live-input GIỮ NGUYÊN (native
            # có thể còn chạy) — chỉ phục hồi thế hệ mới giải phóng được.
            if now - info["submitted_at"] > _REQUEST_MAX_AGE:
                _admission_metrics["lost"] += 1
                lost_tokens.append((token_id, info.get("page_idx", -1)))
                _fail_token_locked(
                    token_id,
                    PoolResetError(
                        f"OCR request lost (no terminal state after "
                        f"{_REQUEST_MAX_AGE:.0f}s), page "
                        f"{info.get('page_idx', -1) + 1}",
                    ),
                    release_resources=False,
                )
        _cond_notify_all_locked()
        # (c) deferred cleanup: chỉ khi KHÔNG còn request sống tham chiếu.
        remaining = []
        for path, since in _deferred_cleanups:
            norm = _norm_path_key(path)
            if _has_live_input_under(norm):
                remaining.append((path, since))
                continue
            if not _deferred_rmtree(path):
                remaining.append((path, since))
        _deferred_cleanups[:] = remaining
    for token_id, page_idx in lost_tokens:
        _request_pool_recovery(f"request lost (token {token_id}, page {page_idx + 1})")


def _janitor_loop() -> None:
    import time

    while _janitor_enabled:
        time.sleep(1.0)
        try:
            _janitor_tick()
        except Exception:
            logger.exception("admission janitor tick failed")


def _ensure_janitor() -> None:
    global _janitor_thread
    if _janitor_thread is not None and _janitor_thread.is_alive():
        return
    _janitor_thread = threading.Thread(
        target=_janitor_loop, daemon=True, name="ocr-admission-janitor"
    )
    _janitor_thread.start()


def _norm_path_key(path: str) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _deferred_rmtree(path: str) -> bool:
    import shutil

    try:
        if os.path.isdir(path):
            shutil.rmtree(path)
        deleted = not os.path.exists(path)
        if deleted:
            # Keep storage charged through cleanup deferred by K/P leases.
            import sys
            spool = sys.modules.get("scanindex.core.secret_scan_spool")
            if spool is not None:
                spool.forget_deleted_tree(path)
        return deleted
    except Exception:
        logger.exception("deferred rmtree failed: %s", path)
        return False


class PagePostOcrError(RuntimeError):
    """Lỗi lắp record/dò phía caller trong `on_page_done` — lỗi THẬT của trang,
    phải truyền ra ngoài (không được nuốt thành 'không nộp')."""

    def __init__(self, page_idx: int, cause: str):
        super().__init__(f"post-OCR page {page_idx + 1}: {cause}")
        self.page_idx = page_idx
        self.cause = cause


def cleanup_path_when_idle(path: str) -> bool:
    """Xóa thư mục tạm NGAY nếu không còn request sống tham chiếu (trực tiếp
    hoặc bên trong); ngược lại ghi vào danh sách dọn muộn. Trả True nếu đã
    xóa ngay, False nếu defer.

    Cả cleanup từng file lẫn cleanup thư mục cha cuối lượt phải đi qua đây —
    không được rmtree trực tiếp khi task native có thể còn mở file bên trong."""
    key = _norm_path_key(path)
    with _admission_lock:
        if _has_live_input_under(key):
            _deferred_cleanups.append((path, 0.0))
            _ensure_janitor()
            return False
        # Serialize lease acquisition against deletion, including external K/P leases.
        return _deferred_rmtree(path)


def _has_live_input_under(key: str) -> bool:
    """Có request sống mở file == key hoặc nằm BÊN TRONG thư mục key?"""
    prefix = key.rstrip("\\/") + os.sep
    for k in _live_inputs:
        if k == key or k.startswith(prefix):
            return True
    return False


def acquire_input_lease(path: str) -> None:
    """Ghi nhận quyền giữ input NGÔIÀ pool OCR (GĐ K/P): coordinator KIE đọc
    canonical, prep process đọc/ghi chunk — cleanup hai tầng (janitor) phải
    chờ các lease này nhả trước khi rmtree. Release BẮT BUỘC sau khi dùng xong
    (finally)."""
    key = _norm_path_key(path)
    with _admission_lock:
        _live_inputs[key] = _live_inputs.get(key, 0) + 1


def release_input_lease(path: str) -> None:
    key = _norm_path_key(path)
    with _admission_lock:
        if _live_inputs.get(key):
            _live_inputs[key] -= 1
            if _live_inputs[key] <= 0:
                _live_inputs.pop(key, None)
        _cond_notify_all_locked()


def _cond_notify_all_locked() -> None:
    _admission_cond.notify_all()


def _admission_release_locked(token_id: int) -> None:
    """Nhả lease một request — IDEMPOTENT (P1-A): token không còn thì bỏ qua.

    P1-D: token thuộc thế hệ CŨ vẫn giữ lease khi recovery terminate thất bại
    được đánh dấu `foreign_charged` — budget thế hệ mới đã bị TRỪ trước một
    slot cho nó, nên khi native cũ kết thúc (janitor nhả) slot hoàn về gate
    hiện tại. Không có flag → chỉ token cùng thế hệ mới được hoàn slot."""
    info = _admission_tokens.pop(token_id, None)
    if info is None:
        return
    key = info.get("path_key")
    if key and _live_inputs.get(key):
        _live_inputs[key] -= 1
        if _live_inputs[key] <= 0:
            _live_inputs.pop(key, None)
    if (
        not _GATE_DISABLED
        and _gate_sem is not None
        and (
            info["generation"] == _pool_generation
            or info.get("foreign_charged")
        )
    ):
        try:
            _gate_sem.release()
        except ValueError:
            logger.exception("admission slot over-release (token %s)", token_id)
    _cond_notify_all_locked()


def try_submit_page(
    input_path: str,
    page_idx: int,
    render_annots: bool = True,
    dpi: int | None = None,
    *,
    wait_started_at: float | None = None,
):
    """Thử nộp một trang, KHÔNG chờ. Trả về `(state, handle)`:

    - `("submitted", _AdmissionResult)` — đã vào pool (token giữ tới khi task
      native kết thúc; caller KHÔNG tự nhả slot). `handle.get()` trả
      `(page_idx, result)` — hợp đồng public 2 phần tử (P1-03).
    - `("busy", None)` — admission đầy; caller quay lại vòng thu kết quả,
      KHÔNG được coi là lỗi hay hoàn thành.
    - `("error", exception)` — lỗi hạ tầng (PoolResetError/file not found/...).

    `wait_started_at` (P2-H): mốc monotonic khi caller BẮT ĐẦU yêu cầu trang
    này (trước các lần Busy) — `admission_wait_ms` đo từ đó, không chỉ từ lần
    try thành công. Caller qua vòng retry (submit_page/collector) truyền để
    metric đúng ngữ nghĩa.

    Giao dịch submission (P1-07): chọn pool + generation → acquire → đăng ký
    token (kèm error-sink TRƯỚC khi commit) → apply_async trong try — ném lỗi
    thì rollback token; pool chết giữa chừng → ("error", …)."""
    if _GATE_DISABLED:
        return _try_submit_no_gate(input_path, page_idx, render_annots, dpi)
    import time as _t

    # 0. Quarantine: đang phục hồi → không được chạm pool (tránh dựng pool
    # mới vô ích giữa lúc thế hệ cũ bị khai tử).
    if _recovering_gen == _pool_generation:
        return ("error", PoolResetError("pool generation is recovering"))

    # 1. Pool/generation trước — gate tồn tại cùng thế hệ (P1-02).
    try:
        pool = _get_pool()
    except Exception as exc:
        return ("error", exc)
    _ensure_janitor()

    # 2. Handle + sink tồn tại TRƯỚC khi commit (reset sớm không mất lỗi).
    token_id = next(_admission_next_id)
    handle = _AdmissionResult(token_id, _pool_generation, None)

    # 3. Đoạn đồng bộ: acquire + đăng ký (không chờ ở đây).
    attempt_started = wait_started_at if wait_started_at is not None else _t.monotonic()
    with _admission_lock:
        if (
            handle.generation != _pool_generation
            or _recovering_gen == _pool_generation
            # P1-B: pool được chọn TRƯỚC khi shutdown/recovery khai tử thế
            # hệ → mọi generation hiện tại đều không còn pool hợp lệ cho tới
            # khi pool thế hệ mới được publish (_mp_pool_gen quay lại thật).
            # None = chưa theo dõi (đường test giả lập `_get_pool`) → bỏ qua.
            or (_mp_pool_gen is not None and _mp_pool_gen != handle.generation)
        ):
            return ("error", PoolResetError("pool generation changed during submit"))
        _ensure_gate_locked()
        if not _gate_sem.acquire(blocking=False):
            _admission_metrics["busy_rejections"] += 1
            return ("busy", None)
        info = {
            "generation": handle.generation,
            "path_key": _norm_path_key(input_path),
            "ar": None,
            "done": False,
            "error": None,
            "submitted_at": _t.monotonic(),
            "page_idx": page_idx,
            "sink": handle.error_sink,
        }
        _admission_tokens[token_id] = info
        _live_inputs[info["path_key"]] = _live_inputs.get(info["path_key"], 0) + 1
        m = _admission_metrics
        m["submitted"] += 1
        if m["run_first_submit_at"] is None:
            m["run_first_submit_at"] = attempt_started
        m["admission_wait_ms"].append(int((_t.monotonic() - attempt_started) * 1000))

    # 4. Commit: apply_async — ném lỗi thì rollback token (P1-07.3).
    try:
        ar = pool.apply_async(
            _worker_ocr_single_tokenized,
            ((token_id, input_path, page_idx, bool(render_annots), dpi, _t.monotonic()),),
            callback=_on_pool_task_done,
            error_callback=_make_error_callback(token_id),
        )
    except Exception as exc:
        with _admission_lock:
            _admission_release_locked(token_id)
        return ("error", exc)

    # 5. Gắn ar vào CẢ registry lẫn handle (handle.ar None → caller đọc được
    # kết quả); nếu reset xảy ra giữa apply_async và lúc này, sink đã nhận
    # lỗi từ trước (đăng ký sink TRƯỚC commit).
    handle.ar = ar
    with _admission_lock:
        info = _admission_tokens.get(token_id)
        if info is not None:
            info["ar"] = ar
    return ("submitted", handle)


def _try_submit_no_gate(input_path, page_idx, render_annots, dpi):
    """Đường kill-switch: không giới hạn, vẫn đo telemetry qua tokenized task."""
    import time as _t

    gen_at_select = _pool_generation
    try:
        pool = _get_pool()
    except Exception as exc:
        return ("error", exc)
    _ensure_janitor()

    token_id = next(_admission_next_id)
    handle = _AdmissionResult(token_id, _pool_generation, None)
    with _admission_lock:
        if (
            handle.generation != _pool_generation
            or _recovering_gen == _pool_generation
            or gen_at_select != _pool_generation
        ):
            # P1-B: pool đã chọn không còn thuộc thế hệ hiện tại — từ chối
            # thay vì đăng ký token mới-gen lên pool cũ.
            return ("error", PoolResetError("pool generation changed during submit"))
        info = {
            "generation": _pool_generation,
            "path_key": _norm_path_key(input_path),
            "ar": None,
            "done": False,
            "error": None,
            "submitted_at": _t.monotonic(),
            "page_idx": page_idx,
            "sink": handle.error_sink,
        }
        _admission_tokens[token_id] = info
        m = _admission_metrics
        m["submitted"] += 1
        if m["run_first_submit_at"] is None:
            m["run_first_submit_at"] = _t.monotonic()
        key = info["path_key"]
        _live_inputs[key] = _live_inputs.get(key, 0) + 1
    try:
        ar = pool.apply_async(
            _worker_ocr_single_tokenized,
            ((token_id, input_path, page_idx, bool(render_annots), dpi, _t.monotonic()),),
            callback=_on_pool_task_done,
            error_callback=_make_error_callback(token_id),
        )
    except Exception as exc:
        with _admission_lock:
            _admission_release_locked(token_id)
        return ("error", exc)
    handle.ar = ar
    with _admission_lock:
        info = _admission_tokens.get(token_id)
        if info is not None:
            info["ar"] = ar
    return ("submitted", handle)


def _make_error_callback(token_id):
    """error_callback của Pool nhận EXCEPTION (không phải payload) — bind
    token theo request (P2-12)."""

    def _cb(exc):
        _on_pool_task_error(token_id, exc)

    return _cb


def _on_pool_task_done(result):
    """Callback thành công của apply_async — CHỈ bookkeeping, phải nhanh.
    Task native XONG → nhả lease (kể cả với token đã đánh dấu lost)."""
    try:
        token_id, _page_idx, _result, timing = result
        with _admission_lock:
            _admission_metrics["queue_wait_ms"].append(
                int(timing.get("queue_wait_ms", 0))
            )
            ocr_ms = int(timing.get("ocr_ms", 0))
            _admission_metrics["ocr_ms"].append(ocr_ms)
            _admission_metrics["ocr_ms_total"] += ocr_ms
            _admission_metrics["ocr_ms_count"] += 1
            _admission_metrics["completed"] += 1
            _admission_release_locked(token_id)
    except Exception:
        logger.exception("pool task done callback failed")


def _on_pool_task_error(token_id, exc):
    """Callback lỗi của apply_async — nhả lease, phải nhanh."""
    try:
        with _admission_lock:
            _admission_metrics["errors"] += 1
            _admission_release_locked(token_id)
    except Exception:
        logger.exception("pool task error callback failed")


class _AdmissionResult:
    """Handle kết quả gắn token. `.get()` trả `(page_idx, result)` — HỢP ĐỒNG
    PUBLIC 2 PHẦN TỬ (token/timing là nội bộ, P1-03). `.get()` có thể raise
    `PoolResetError` (lost/reset/shutdown) hoặc `multiprocessing.TimeoutError`
    khi hết hạn chờ."""

    __slots__ = ("token_id", "generation", "ar", "_error", "timing")

    def __init__(self, token_id: int, generation: int, ar):
        self.token_id = token_id
        self.generation = generation
        self.ar = ar
        self.timing: dict | None = None
        self._error: list = []  # error-sink: janitor/reset đẩy lỗi vào đây

    @property
    def error_sink(self) -> list:
        return self._error

    def ready(self) -> bool:
        if self._error:
            return True
        with _admission_lock:
            info = _admission_tokens.get(self.token_id)
            if info is not None and info.get("error") is not None:
                return True
        return bool(self.ar.ready()) if self.ar is not None else True

    def get(self, timeout=None):
        """Chờ kết quả, kiểm tra error-sink ĐỊNH KỲ (≤0,25 s/lượt) để lỗi
        lost/reset đến đúng hạn thay vì chờ nguyên timeout. Hết hạn raise
        `multiprocessing.TimeoutError`."""
        import multiprocessing
        import time as _t

        deadline = None
        if timeout is not None:
            deadline = _t.monotonic() + float(timeout)
        while True:
            if self._error:
                raise self._error[0]
            with _admission_lock:
                info = _admission_tokens.get(self.token_id)
                if info is not None and info.get("error") is not None:
                    raise PoolResetError(str(info["error"]))
            if self.ar is None:
                return (None, None)
            remaining = None
            if deadline is not None:
                remaining = deadline - _t.monotonic()
                if remaining <= 0:
                    raise multiprocessing.TimeoutError("OCR result wait timed out")
            slice_wait = 0.25 if remaining is None else min(0.25, max(remaining, 0.001))
            try:
                raw = self.ar.get(timeout=slice_wait)
            except multiprocessing.TimeoutError:
                if deadline is not None:
                    # P1-C: hết MỘT slice ≠ hết deadline tổng — poll tiếp cho
                    # tới khi có kết quả, terminal outcome hoặc hết deadline
                    # thật. Đầu vòng lặp đã kiểm tra lại error-sink.
                    if deadline - _t.monotonic() > 0:
                        continue
                    # hết deadline tổng: kiểm tra nốt sink rồi raise kiểu đúng
                    if self._error:
                        raise self._error[0]
                    with _admission_lock:
                        info = _admission_tokens.get(self.token_id)
                        if info is not None and info.get("error") is not None:
                            raise PoolResetError(str(info["error"]))
                    raise
                # không deadline: vòng lại kiểm tra sink rồi đợi tiếp
                continue
            # raw = (token_id, page_idx, result, timing) từ worker tokenized
            try:
                _token_id, page_idx, result, timing = raw
                self.timing = timing
                return (page_idx, result)
            except (TypeError, ValueError):
                # handle prebaked/hợp đồng cũ trả (page_idx, result)
                return raw

    def wait(self, timeout=None):
        try:
            self.get(timeout=timeout)
        except Exception:
            pass


def submit_page(
    input_path: str,
    page_idx: int,
    render_annots: bool = True,
    cancel_check=None,
    wait_check=None,
):
    """Nộp một trang — hợp đồng cũ: trả handle `AsyncResult`-like, caller
    `.ready()/.get()`. Không bao giờ trả None vì 'bận' (bận chỉ là chờ).

    - `cancel_check`: gọi định kỳ lúc chờ slot; True → raise CoreScanCancelled.
    - `wait_check`: gọi định kỳ lúc chờ slot; True → tạm dừng chờ (nội bộ
      feeder dùng để tôn trọng pause chủ đích) — raise CoreScanCancelled khi
      muốn hủy hẳn. Trả về khi nộp thành công hoặc raise lỗi thật."""
    import time as _t

    warned = False
    reset_retries = 0
    loop_started: float | None = None  # P2-H: mốc "caller bắt đầu yêu cầu"
    while True:
        state, handle = try_submit_page(input_path, page_idx, render_annots)
        if state == "submitted":
            if loop_started is not None:
                # Caller đã Busy/chờ qua các lần trước: ghi entry full-wait
                # (admission_wait đo từ lúc bắt đầu yêu cầu, không chỉ lần
                # try thành công). Entry attempt-level do try_submit tự ghi.
                with _admission_lock:
                    _admission_metrics["admission_wait_ms"].append(
                        int((_t.monotonic() - loop_started) * 1000)
                    )
            return handle
        if loop_started is None:
            loop_started = _t.monotonic()
        if state == "error":
            if isinstance(handle, PoolResetError) and reset_retries < 3:
                reset_retries += 1
                _t.sleep(0.05)
                if _recovering_gen is None:
                    continue
            raise handle
        # busy → chờ bounded rồi thử lại; tôn trọng pause/cancel trong lúc chờ
        if not warned:
            warned = True
        deadline = _t.monotonic() + 0.05
        while _t.monotonic() < deadline:
            if cancel_check is not None and cancel_check():
                raise CoreScanCancelled("cancelled while waiting for admission")
            if wait_check is not None and wait_check():
                raise CoreScanCancelled("paused while waiting for admission")
            _t.sleep(0.01)

OCR_DPI = 240
OCR_TEXT_NORMALIZATION = "latin_vi_canonical_v2"


def _init_layout_analyzers(log=None):
    """Initialize primary table layout plus auxiliary DocLayNet semantics."""
    primary = None
    auxiliary = None
    layout_module = None
    try:
        from scanindex.core.tables import layout_analyzer as la
        layout_module = la
        if la.is_available():
            primary = la.get_analyzer()
            if primary and log:
                log("DocLayout-YOLO DocStructBench layout analysis enabled", "info")
        if (
            os.environ.get("OCRTOOL_DISABLE_DOCLAYNET_LAYOUT") != "1"
            and hasattr(la, "is_doclaynet_available")
            and la.is_doclaynet_available()
        ):
            auxiliary = la.get_doclaynet_analyzer()
            if auxiliary and log:
                log("DocLayout-YOLO DocLayNet semantic analysis enabled", "info")
    except Exception:
        pass
    return primary, auxiliary, layout_module


def _analyze_combined_layout_regions(page, page_idx, page_w, page_h, analyzers, log=None):
    primary, auxiliary, layout_module = analyzers
    if not primary and not auxiliary:
        return []
    try:
        import fitz
        from PIL import Image

        dpi = OCR_DPI
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=mat, annots=True)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        scale_x = page_w / pix.width
        scale_y = page_h / pix.height

        primary_regions = []
        auxiliary_regions = []
        if primary:
            primary_regions = decorate_layout_regions(
                primary.analyze_page(img),
                page_index=page_idx,
                scale_x=scale_x,
                scale_y=scale_y,
            )
        if auxiliary:
            auxiliary_regions = decorate_layout_regions(
                auxiliary.analyze_page(img),
                page_index=page_idx,
                scale_x=scale_x,
                scale_y=scale_y,
            )

        if layout_module and hasattr(layout_module, "merge_auxiliary_layout_regions"):
            layout_regions = layout_module.merge_auxiliary_layout_regions(
                primary_regions,
                auxiliary_regions,
            )
        else:
            layout_regions = list(primary_regions or [])
        if log:
            aux_count = max(0, len(layout_regions) - len(primary_regions))
            log(
                f"  Page {page_idx + 1}: {len(layout_regions)} layout regions "
                f"({len(primary_regions)} primary + {aux_count} semantic)",
                "debug",
            )
        return layout_regions
    except Exception as e:
        if log:
            log(f"  Layout analysis failed: {e}", "debug")
        return []


# --- Worker process state (1 ScreenAI instance per worker process) ---
_worker_ocr = None       # the worker's ScreenAIOCR instance
_worker_dll_path = None  # loaded DLL path, kept for diagnostics
_worker_model_dir = None  # model dir (GĐ W: initializer nhẹ chỉ lưu path)
_worker_init_error = None
_worker_load_lock = threading.Lock()
# P1-01: limiter nạp DÙNG CHUNG mọi đường load trong worker (warmup/self-load/
# respawn) — cấp bởi supervisor qua initializer, kế thừa cho worker respawn.
_worker_load_sem = None   # BoundedSemaphore(k) liên process; None = không giới hạn
_worker_load_probe = None  # Value('i') đếm nạp đang chạy (quan sát/test)
# Bản tham chiếu PARENT của probe — chỉ để telemetry (worker tự dùng bản kế thừa).
_load_probe_ref = None


def _worker_init(dll_path, model_dir, load_sem=None, load_probe=None):
    """Initializer NHẸ (GĐ W): chỉ patch stdio + LƯU path + nhận limiter nạp —
    DLL/model do chính worker nạp khi chạy `_warmup_task` (theo đợt k do
    supervisor kiểm soát) hoặc ở task OCR đầu tiên (`_ensure_worker_loaded`).
    Nhờ vậy: (i) không bao giờ có N process nạp DLL đồng thời lúc spawn;
    (ii) worker recycle tái khởi tạo rẻ, lần task kế tự nạp lại — VẪN qua
    cùng permit nạp (P1-01).
    D1: TỰ-GÁN Windows Job của parent (ownership tree) TRƯỚC mọi native —
    recycle respawn cũng vào job (env handle kế thừa)."""
    global _worker_dll_path, _worker_model_dir, _worker_init_error
    global _worker_load_sem, _worker_load_probe

    from scanindex.infra.bootstrap import bootstrap_worker
    bootstrap_worker()
    try:
        from scanindex.infra.mp_safety import ensure_valid_stdio
        ensure_valid_stdio()
    except Exception:
        pass
    _worker_dll_path = dll_path
    _worker_model_dir = model_dir
    _worker_init_error = None
    _worker_load_sem = load_sem
    _worker_load_probe = load_probe


def _worker_ocr_single(input_path, page_idx, render_annots=True, dpi=None):
    """OCR a single page using this worker's DLL instance.

    ``dpi`` overrides the module-wide OCR_DPI (default 240). Callers like the
    secret-file scanner pass a lower DPI (e.g. 200) for faster stamp detection.

    GĐ W: runtime tự nạp tại đây nếu worker chưa nạp (task đầu tiên hoặc
    worker recycle mới) — nạp lỗi trả sentinel `worker_init_failed` như cũ
    (collector điều phối recovery)."""
    import os as _os

    if _os.environ.get("OCRTOOL_WARMUP_TEST_MODE") == "1":
        # Hook test integration (Pool thật, không nạp DLL): kết quả OCR giả.
        if _worker_ocr is None:
            _ensure_worker_loaded()  # Exercise replacement-worker load permits too.
        return (page_idx, {
            "lines_data": [{"text": "TEST", "x0": 0, "x1": 10, "y0": 0, "y1": 10}],
            "words_data": [], "render_width": 10, "render_height": 10,
        })
    if _worker_ocr is None:
        try:
            with _worker_load_lock:
                if _worker_ocr is None:
                    _ensure_worker_loaded()
        except Exception as exc:
            logger.exception("ScreenAI worker runtime load failed")
            return (page_idx, {
                "worker_init_failed": True,
                "error": f"{type(exc).__name__}: {exc}",
            })

    try:
        import fitz
        from PIL import Image

        doc = fitz.open(input_path)
        page = doc[page_idx]
        page_w = page.rect.width
        page_h = page.rect.height

        render_dpi = dpi if dpi is not None else OCR_DPI
        mat = fitz.Matrix(render_dpi / 72, render_dpi / 72)
        pix = page.get_pixmap(matrix=mat, annots=bool(render_annots))
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        render_w, render_h = pix.width, pix.height
        doc.close()

        scale_x = page_w / render_w
        scale_y = page_h / render_h

        result = _worker_ocr.perform_ocr(img)
        ocr_lines = result.get("lines", [])

        lines_data, words_data = _ocr_result_to_page_data(
            page_idx, ocr_lines, scale_x, scale_y
        )

        return (page_idx, {
            "lines_data": lines_data,
            "words_data": words_data,
            "render_width": render_w,
            "render_height": render_h,
        })

    except Exception as exc:
        logger.exception("ScreenAI worker OCR failed")
        return (page_idx, {"ocr_error": True,
                           "error": f"{type(exc).__name__}: {exc}"})


def _worker_ocr_single_tokenized(payload):
    """Wrapper picklable gắn token: đo thời gian chờ hàng đợi + thời gian OCR
    để telemetry phân biệt 'đã nộp' với 'đang chạy native'. Trả về
    `(token_id, page_idx, result, timing)` — callback `_on_pool_task_done`
    dựa vào token_id để nhả slot."""
    import time as _t

    (token_id, input_path, page_idx, render_annots, dpi, submitted_at) = payload
    t_start = _t.monotonic()
    queue_wait_ms = int((t_start - submitted_at) * 1000)
    result = _worker_ocr_single(input_path, page_idx, render_annots=render_annots, dpi=dpi)[1]
    ocr_ms = int((_t.monotonic() - t_start) * 1000)
    return (token_id, page_idx, result, {"queue_wait_ms": queue_wait_ms, "ocr_ms": ocr_ms})


def _worker_ocr_batch(args):
    """Worker function: OCR a list of pages sequentially.

    Used by the legacy file-level path; in the new pipeline the orchestrator
    submits one page per `apply_async` call instead of batching."""
    input_path, page_indices = args
    return [_worker_ocr_single(input_path, pi) for pi in page_indices]


def _get_pool():
    """Get or create the page-level OCR pool (lazy init, thread-safe).

    Returns a SINGLE `multiprocessing.Pool` with `_NUM_PAGE_WORKERS` worker
    processes. Each worker is fully independent (one DLL each, no shared
    state).

    GĐ W: initializer NHẸ — DLL/model do supervisor điều phối nạp theo đợt k
    (`OCRTOOL_WARMUP_STAGE`, mặc định 4) qua `_warmup_task`; gate capacity
    ramp theo số worker ACK (ready=0 ⇒ Busy/warming). `_get_pool()` KHÔNG
    bao giờ chờ warmup (trả ngay sau khi publish)."""
    global _mp_pool, _mp_pool_gen, _pool_state, _load_probe_ref
    from scanindex.core.runtime.shutdown import accepting_work, track_executor
    if not accepting_work():
        raise RuntimeError("Runtime is stopping")
    if _mp_pool is not None:
        return _mp_pool
    # Thang retry warmup kiểm tra TRƯỚC khi spawn — lần thứ 4 fail-loud
    # (OCRWarmupError) mà không tạo generation mới (plan §4.2.0).
    # P1-02: Pool dựng với SỐ PROCESS HIỆU LỰC đã chọn (retry 2 của N=8 là
    # 4 process, không phải 8) — gate/ready phản ánh đúng RAM/process thật.
    requested, stage_k = _warmup_stage_plan(_NUM_PAGE_WORKERS)
    with _mp_pool_lock:
        if _mp_pool is not None:
            return _mp_pool

        import multiprocessing
        from scanindex.infra.mp_safety import (
            patch_multiprocessing_spawn_sys_path,
            sanitized_sys_path_for_spawn,
        )

        dll_path, model_dir = _find_screen_ai_paths()
        if not dll_path:
            raise FileNotFoundError("OCR library not installed")

        patch_multiprocessing_spawn_sys_path()
        from scanindex.infra.windows_job import ensure_app_job
        ensure_app_job()
        # P1-01: limiter nạp liên process (cửa sổ k) — mọi đường load của
        # worker (warmup/self-load/respawn) đi qua một permit duy nhất. Chỉ
        # dựng khi staging BẬT và k < N (staging tắt hoặc k=N: không cần).
        load_sem = None
        load_probe = None
        if 0 < stage_k < requested:
            load_sem = multiprocessing.BoundedSemaphore(stage_k)
            load_probe = multiprocessing.Value("i", 0)
        with sanitized_sys_path_for_spawn():
            pool = multiprocessing.Pool(
                processes=requested,
                initializer=_worker_init,
                initargs=(dll_path, model_dir, load_sem, load_probe),
                maxtasksperchild=_MP_MAXTASKSPERCHILD,  # None = tắt recycle (B7)
            )

        _mp_pool = pool
        track_executor(pool)
        _load_probe_ref = load_probe
        # Gate phải tồn tại TỪ THẾ HỆ ĐẦU (P1-02): None = không giới hạn là
        # lỗi; sau shutdown/lazy-init cũng phải dựng lại trước khi nhận nộp.
        with _admission_lock:
            _ensure_gate_locked()
            # P1-B: công bố generation mà pool này thuộc về — submission vào
            # thế hệ khác sẽ bị từ chối ở đoạn đồng bộ thay vì nộp nhầm pool.
            _mp_pool_gen = _pool_generation
            _pool_state = "ready"
        # GĐ W: supervisor staged warmup (không semaphore liên process).
        with _warmup_lock:
            _start_warmup_supervisor_locked(requested, stage_k)
        logger.info(
            "Page-level OCR pool ready: %d/%d worker processes (1 DLL each, "
            "admission budget %d, warmup stage=%d, load limiter=%s, "
            "maxtasksperchild=%s)",
            requested, _NUM_PAGE_WORKERS,
            _gate_capacity(),
            stage_k,
            "on (k=%d)" % stage_k if load_sem is not None else "off",
            _MP_MAXTASKSPERCHILD if _MP_MAXTASKSPERCHILD is not None else "off",
        )
        return _mp_pool


def get_page_pool():
    """Public accessor returning the lazily-initialized page-level pool."""
    return _get_pool()


class _PrebakedAsyncResult:
    """Stub `AsyncResult` for pages whose OCR result was computed previously.

    Lets the archive pipeline reuse cached page_results from Step 1 without
    re-running OCR in Step 2, while presenting the same `.ready()` / `.get()`
    interface that pipeline_engine expects."""
    __slots__ = ("_page_idx", "_result")

    def __init__(self, page_idx: int, page_result: dict):
        self._page_idx = page_idx
        self._result = page_result

    def ready(self) -> bool:
        return True

    def successful(self) -> bool:
        return True

    def get(self, timeout=None):
        return (self._page_idx, self._result)

    def wait(self, timeout=None):
        return None


def make_prebaked_async_result(page_idx: int, page_result: dict) -> _PrebakedAsyncResult:
    """Wrap an already-OCRed page_result so it can be returned in lieu of a
    real `pool.apply_async()` call."""
    return _PrebakedAsyncResult(page_idx, page_result)


def ocr_one_page(input_path: str, page_idx: int, timeout: float = 120.0,
                 render_annots: bool = True):
    """Synchronous helper: submit one page and wait for the result.

    Returns the page dict (lines/words/render dims) or None on failure.
    `worker_init_failed` KHÔNG còn shutdown pool chung — chuyển cho supervisor
    phục hồi tập trung (trước đây 1 trang init lỗi giết cả pool dùng chung)."""
    ar = submit_page(input_path, page_idx, render_annots=render_annots)
    try:
        _, result = ar.get(timeout=timeout)
        if isinstance(result, dict) and result.get("worker_init_failed"):
            _request_pool_recovery(
                f"worker init failed on single page {page_idx + 1}: "
                f"{result.get('error')}"
            )
            return None
        return result
    except Exception:
        return None


def page_has_render_annotations(input_path: str, page_idx: int) -> bool:
    """Return True when a page has PDF annotations/widgets that pixmap rendering
    can hide with ``annots=False``."""
    import fitz  # PyMuPDF - lazy import

    doc = fitz.open(input_path)
    try:
        if page_idx < 0 or page_idx >= len(doc):
            return False
        page = doc[page_idx]
        try:
            annots = page.annots()
            if annots is not None:
                for _annot in annots:
                    return True
        except Exception:
            pass
        try:
            widgets = page.widgets()
            if widgets is not None:
                for _widget in widgets:
                    return True
        except Exception:
            pass
        return False
    finally:
        doc.close()


def replace_canonical_page_with_page_result(
    canonical_json_path: str,
    source_pdf_path: str,
    page_idx: int,
    page_result: dict,
    *,
    render_annots: bool,
    canonical_profile: str | None = None,
) -> dict:
    """Replace one page's OCR payload in a canonical JSON file.

    Used by archive KIE to swap the signer page to a clean OCR pass where PDF
    annotations are hidden. This updates only the JSON consumed by KIE; it does
    not rewrite the visible PDF.
    """
    import fitz  # PyMuPDF - lazy import

    ocr_data = load_canonical(canonical_json_path)

    doc = fitz.open(source_pdf_path)
    try:
        if page_idx < 0 or page_idx >= len(doc):
            raise IndexError(f"page_idx={page_idx} outside PDF page_count={len(doc)}")
        page = doc[page_idx]
        page_w = page.rect.width
        page_h = page.rect.height
    finally:
        doc.close()

    lines_data = copy.deepcopy(page_result.get("lines_data") or [])
    words_data = copy.deepcopy(page_result.get("words_data") or [])
    coord_flipped = _normalize_page_coord_to_top_left(lines_data, words_data, page_h)

    existing = None
    pages = ocr_data.setdefault("pages", [])
    for idx, page in enumerate(pages):
        try:
            existing_page_idx = int(page.get("page_index", idx))
        except (TypeError, ValueError):
            existing_page_idx = idx
        if existing_page_idx == int(page_idx):
            existing = page
            break

    applied_rotation = 0
    if isinstance(existing, dict):
        try:
            applied_rotation = int(existing.get("applied_rotation", 0) or 0) % 360
        except (TypeError, ValueError):
            applied_rotation = 0

    page_data = make_page_record(
        page_index=page_idx,
        width=page_w,
        height=page_h,
        render_width=page_result.get("render_width", 0),
        render_height=page_result.get("render_height", 0),
        applied_rotation=applied_rotation,
    )
    page_data["coord_origin"] = "top-left"
    if coord_flipped:
        page_data["coord_origin_source"] = "normalized_from_bottom_left"
    page_data["lines"] = lines_data
    page_data["words"] = words_data
    page_data["ocr_render_annots"] = bool(render_annots)
    page_data["kie_render_annots"] = bool(render_annots)
    if not render_annots:
        page_data["kie_ocr_override"] = {
            "reason": "signature_page_without_pdf_annotations",
            "source_page_index": int(page_idx),
        }

    replaced = False
    for idx, page in enumerate(pages):
        try:
            existing_page_idx = int(page.get("page_index", idx))
        except (TypeError, ValueError):
            existing_page_idx = idx
        if existing_page_idx == int(page_idx):
            pages[idx] = page_data
            replaced = True
            break
    if not replaced:
        pages.append(page_data)
        pages.sort(key=lambda item: int(item.get("page_index", 0)))

    kie_pipeline = ocr_data.setdefault("pipeline", {}).setdefault("kie", {})
    kie_pipeline["signature_page_clean_ocr"] = {
        "page_index": int(page_idx),
        "render_annots": bool(render_annots),
        "line_count": len(lines_data),
        "word_count": len(words_data),
    }

    save_canonical(canonical_json_path, ocr_data, profile=canonical_profile)

    return {
        "page_index": int(page_idx),
        "line_count": len(lines_data),
        "word_count": len(words_data),
        "coord_flipped": bool(coord_flipped),
    }


def _cancel_warmup_for_generation() -> None:
    """Đánh dấu warmup của thế hệ hiện tại kết thúc (shutdown/recovery) —
    ACK về sau bị bỏ (KHÔNG phải degraded: sổ đóng hẳn), deadline timer bị
    hủy."""
    with _warmup_lock:
        _warmup["done"] = True
        _warmup["degraded"] = False
        timer = _warmup.get("deadline_timer")
        if timer is not None:
            timer.cancel()
            _warmup["deadline_timer"] = None


def _reserve_foreign_budget_locked() -> None:
    """P1-D: lease thế hệ cũ còn sống (native CÓ THỂ CÒN CHẠY) phải chiếm
    chỗ trong budget gate hiện tại — tổng request native chưa xong của app
    không vượt trần. Gọi khi đang giữ `_admission_lock`, sau khi gate mới
    đã được dựng."""
    foreign = [
        info for info in _admission_tokens.values()
        if info["generation"] < _pool_generation
    ]
    if foreign and _gate_sem is not None and not _GATE_DISABLED:
        reserved = 0
        for info in foreign:
            if _gate_sem.acquire(blocking=False):
                info["foreign_charged"] = True
                reserved += 1
            else:
                break
        if reserved < len(foreign):
            logger.warning(
                "reserve budget cho %d lease cũ chỉ được %d slot (gate đã "
                "đầy) — submission mới sẽ Busy tới khi lease cũ nhả",
                len(foreign), reserved,
            )


def shutdown_pool():
    """Đóng thế hệ pool hiện tại: mọi request chưa terminal của thế hệ đó nhận
    `PoolResetError` qua error-sink (không ai chờ handle cũ vô hạn), pool
    terminate/join — terminate xong native chắc chắn dừng → nhả lease. Chỉ
    đóng — KHÔNG tự dựng pool mới (lần OCR sau sẽ lazy-init).

    P1-B (REVIEW v2): shutdown KHÔNG đụng request của thế hệ khác — snapshot
    tập token thuộc thế hệ bị đóng ngay khi bắt đầu; pha sau terminate chỉ
    kết thúc/thu hồi lease đúng các token đó. Request đăng ký vào thế hệ mới
    (sau khi bump) không bị đánh lỗi, không mất lease.

    P1-03: terminate/join THẤT BẠI KHÔNG phải bằng chứng native đã dừng —
    KHÔNG nhả lease, KHÔNG mất tham chiếu vô thức: giữ token/lease thế hệ cũ,
    trừ budget gate thế hệ mới (như recovery-failure), chờ janitor thấy task
    thật kết thúc (`ar.ready`) hoặc thế hệ sau được chứng minh dừng."""
    global _mp_pool, _pool_generation, _gate_sem, _mp_pool_gen, _pool_state
    _cancel_warmup_for_generation()
    with _admission_lock:
        old_gen = _pool_generation
        _pool_generation += 1
        _gate_sem = None
        _mp_pool_gen = -1  # pool đang dying — không nhận submission
        _pool_state = "stopping"
        _cond_notify_all_locked()
        # Snapshot thế hệ bị đóng — pha sau KHÔNG quét toàn bộ registry.
        shutdown_ids = [
            tid for tid, info in _admission_tokens.items()
            if info["generation"] == old_gen
        ]
        for token_id in shutdown_ids:
            _fail_token_locked(token_id, PoolResetError("pool shut down"),
                               release_resources=False)
    terminate_ok = True
    with _mp_pool_lock:
        pool = _mp_pool
        _mp_pool = None
    if pool is not None:
        from scanindex.core.runtime.shutdown import track_executor
        track_executor(pool)
        try:
            pool.terminate()
            pool.join()
        except Exception:
            terminate_ok = False
            logger.exception(
                "OCR pool shutdown: terminate/join FAILED — native có thể "
                "còn chạy; GIỮ lease tới khi native kết thúc thật (P1-03)"
            )
    # Chỉ khi terminate thành công (hoặc không có pool) → native dừng hẳn →
    # nhả lease của ĐÚNG thế hệ đã snapshot.
    with _admission_lock:
        for token_id in shutdown_ids:
            _fail_token_locked(token_id, PoolResetError("pool shut down"),
                               release_resources=terminate_ok)
        _mp_pool_gen = None  # lazy-init kế tiếp sẽ publish thế hệ mới
        if terminate_ok:
            _pool_state = "idle"
        else:
            _admission_metrics["recovery_failures"] += 1
            _pool_state = "ready"  # lazy-init thế hệ mới vẫn được phép
            _ensure_gate_locked()
            _reserve_foreign_budget_locked()
        _cond_notify_all_locked()


def _request_pool_recovery(reason: str) -> None:
    """Phục hồi thế hệ pool: quarantine (chặn nộp mới) → terminate/join →
    nếu terminate THÀNH CÔNG: mọi request của thế hệ cũ — cả chưa terminal
    LẪN lost-đã-báo-lỗi — đều được kết thúc và NHẢ lease (native đã dừng hẳn,
    P1-A); nếu terminate THẤT BẠI: báo recovery failure, GIỮ lease, vẫn dựng
    thế hệ mới nhưng TRỪ budget cho các lease cũ còn sống (P1-D — không vượt
    trần native toàn app). Single-flight: một lần phục hồi mỗi thế hệ."""
    global _pool_generation, _gate_sem, _recovering_gen, _mp_pool, _mp_pool_gen
    global _pool_state
    _cancel_warmup_for_generation()
    with _admission_lock:
        gen = _pool_generation
        if _recovering_gen == gen:
            return
        _recovering_gen = gen
        _gate_sem = None  # quarantine: try_submit thấy recovering → error
        _mp_pool_gen = -1
        _pool_state = "quarantined"
        _admission_metrics["resets"] += 1
        _cond_notify_all_locked()
    logger.warning("OCR pool recovery started: %s (generation %s)", reason, gen)
    terminate_ok = True
    with _mp_pool_lock:
        pool = _mp_pool
        _mp_pool = None
    if pool is not None:
        from scanindex.core.runtime.shutdown import track_executor
        track_executor(pool)
        try:
            pool.terminate()
            pool.join()
        except Exception:
            terminate_ok = False
            logger.exception("pool terminate during recovery FAILED — giữ lease")
    with _admission_lock:
        # Kết thúc mọi request của thế hệ bị đóng; với token lost (done=True)
        # thì thu lease khi native đã được chứng minh dừng (P1-A).
        for token_id, info in list(_admission_tokens.items()):
            if info["generation"] != gen:
                continue
            if not info["done"]:
                _fail_token_locked(
                    token_id,
                    PoolResetError(f"pool recovered: {reason}"),
                    release_resources=terminate_ok,
                )
            elif terminate_ok:
                _admission_release_locked(token_id)
        _pool_generation = gen + 1
        _ensure_gate_locked()
        if not terminate_ok:
            _admission_metrics["recovery_failures"] += 1
            # P1-D: các lease thế hệ cũ còn sống phải chiếm chỗ trong budget
            # thế hệ mới — tổng request native chưa xong của app không vượt
            # trần. Khi native cũ kết thúc (janitor nhả foreign_charged),
            # slot hoàn về gate hiện tại.
            _reserve_foreign_budget_locked()
        _recovering_gen = None
        _pool_state = "ready"
        # Không còn pool (`_mp_pool` đã None): trả `_mp_pool_gen` về None =
        # "chưa có pool được publish" — lazy-init kế tiếp sẽ publish thế hệ
        # mới. Giữ -1 ở đây sẽ chặn submission mãi trong đường test giả lập.
        _mp_pool_gen = None
        _cond_notify_all_locked()
    if terminate_ok:
        logger.warning("OCR pool recovery done: new generation %s", _pool_generation)
    else:
        logger.error(
            "OCR pool recovery INCOMPLETE (native có thể còn chạy) — thế hệ "
            "mới %s vẫn được dựng với budget trừ cho %d lease cũ; lease giữ "
            "đến khi native tự kết thúc",
            _pool_generation,
            sum(1 for i in _admission_tokens.values()
                if i["generation"] < _pool_generation),
        )


atexit.register(shutdown_pool)


def _find_unicode_font():
    """Find a TTF font that supports Vietnamese characters."""
    candidates = [
        os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "arial.ttf"),
        os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "times.ttf"),
        os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "calibri.ttf"),
        os.path.join(get_app_dir(), "models", "fonts", "NotoSans-Regular.ttf"),
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def _get_model_dir():
    """Get the screen_ai model base directory."""
    return os.path.join(get_app_dir(), "models", "screen_ai")


def check_ocr_status():
    """
    Check ScreenAI availability without downloading.
    Returns ScreenAIStatus for UI to decide next action.
    """
    from scanindex.core.ocr.screen_ai_downloader import check_screen_ai
    return check_screen_ai(_get_model_dir())


def install_ocr(status, progress_callback=None, log_callback=None):
    """
    Install ScreenAI (copy from Chrome or download).
    Call ONLY after user consents.
    Returns (lib_path, model_path, color_type).
    """
    from scanindex.core.ocr.screen_ai_downloader import install_screen_ai
    return install_screen_ai(
        _get_model_dir(), status,
        progress_callback=progress_callback,
        log_callback=log_callback
    )


def _find_screen_ai_paths():
    """Auto-detect DLL and model directory using downloader."""
    from scanindex.core.ocr.screen_ai_downloader import (
        check_screen_ai, ScreenAIStatus, verify_installed_library,
    )
    status = check_screen_ai(_get_model_dir())
    if status.status == ScreenAIStatus.FOUND_LOCAL:
        # Re-verify on EVERY load: install-time checks don't protect a
        # user-writable portable folder against later DLL swapping.
        verify_installed_library(status.lib_path)
        return status.lib_path, status.model_path
    return None, None


def _get_ocr():
    """Get or create the singleton ScreenAIOCR instance (thread-safe)."""
    global _ocr_instance
    if _ocr_instance is not None:
        return _ocr_instance
    with _init_lock:
        if _ocr_instance is not None:
            return _ocr_instance
        dll_path, model_dir = _find_screen_ai_paths()
        if not dll_path:
            raise FileNotFoundError(
                "Thư viện OCR chưa được cài đặt. "
                "Vui lòng cài đặt trong Settings hoặc khởi động lại app.")
        logger.info(f"Initializing ScreenAI OCR from: {model_dir}")
        ocr = ScreenAIOCR(dll_path=dll_path, model_dir=model_dir)
        ocr.initialize()
        logger.info("ScreenAI OCR initialized successfully")
        _ocr_instance = ocr
        return _ocr_instance


# ===========================================================================
# Line-level text rendering
# ===========================================================================

def _normalize_ocr_lookalikes(text):
    if not text:
        return text
    normalized = unicodedata.normalize("NFKC", text)
    if not _LATIN_VIET_RE.search(normalized):
        return normalized
    if not any(ch in _CONFUSABLE_LATIN_MAP for ch in normalized):
        return normalized
    return "".join(_CONFUSABLE_LATIN_MAP.get(ch, ch) for ch in normalized)


def _has_disallowed_script_letters(text):
    for ch in text or "":
        if not ch.isalpha():
            continue
        script_prefix = unicodedata.name(ch, "").split(" ")[0]
        if script_prefix in _DISALLOWED_SCRIPT_PREFIXES:
            return True
    return False


def _sanitize_ocr_surface_text(text):
    return _shared_sanitize_ocr_surface_text(text)


def get_parallel_capacity():
    """Return the maximum per-document OCR worker count for the current config."""
    return max(1, _NUM_PROCESSES * _NUM_DLL_PER_PROCESS)


def _line_text_from_words(words, text_key="text"):
    """Reconstruct line text from word list, respecting has_space_after."""
    parts = []
    for w in words:
        parts.append(w.get(text_key, ""))
        if w.get("has_space_after", True):
            parts.append(" ")
    return "".join(parts).rstrip()


def _line_xywh(ln):
    """Return (x, y, w, h) for a line dict.

    Newer canonical JSONs are slimmed by
    `kie_json_utils.slim_canonical_for_layoutlmv3_runtime_in_place` which
    drops the legacy `x/y/w/h` keys but keeps `bbox`. Older JSONs still have
    both. Read whichever is present so callers don't KeyError on slim files.
    """
    if all(key in ln and ln.get(key) is not None for key in ("x", "y", "w", "h")):
        try:
            return (
                float(ln.get("x", 0.0)),
                float(ln.get("y", 0.0)),
                float(ln.get("w", 0.0)),
                float(ln.get("h", 0.0)),
            )
        except (TypeError, ValueError):
            pass
    bbox = ln.get("bbox") or [0.0, 0.0, 0.0, 0.0]
    if len(bbox) < 4:
        return (0.0, 0.0, 0.0, 0.0)
    try:
        x0, y0, x1, y1 = (float(v) for v in bbox[:4])
    except (TypeError, ValueError):
        return (0.0, 0.0, 0.0, 0.0)
    return (x0, y0, max(0.0, x1 - x0), max(0.0, y1 - y0))


def _build_text_page_lines(page, lines_data, font_path):
    """
    Add invisible text overlay at LINE level (not word level).
    Each line is inserted as a single text operation at the line's
    starting position with consistent font size.
    This matches Chrome's PDF output behavior.
    """
    import fitz  # PyMuPDF - lazy import (not needed by screenshot tool)
    font = fitz.Font(fontfile=font_path)
    tw = fitz.TextWriter(page.rect)

    for ln in lines_data:
        text = ln["text"]
        if not text.strip():
            continue
        x, y, _, h = _line_xywh(ln)
        try:
            tw.append(
                pos=(x, y + h * 0.85),  # baseline
                text=text,
                font=font,
                fontsize=ln.get("font_size", max(h * 0.78, 4.0)),
            )
        except Exception:
            continue

    tw.write_text(page, render_mode=3)  # invisible


def _build_text_page_words(page, words_data, font_path, *, fallback_lines=None):
    """Add invisible text overlay at WORD level.

    PyMuPDF derives copy-selection bboxes from the hidden text layer. Rendering
    a whole OCR line as one text run lets font metrics drift across the line,
    so late-line word bboxes no longer match the OCR boxes. Writing each word
    with a horizontal morph keeps the PDF text layer aligned with word bboxes.
    ActualText preserves inter-word spacing even when adjacent bboxes are too
    close for a PDF viewer to infer the space geometrically.

    Fast path: every word is committed through ONE Shape (one content stream)
    and the /ActualText spans are wrapped in a single pass over that stream.
    The legacy strategy (one `insert_text` + stream read/rewrite per word)
    costs superlinearly on dense scan pages (1.7-2.6s/page at ~500-700 words).
    On any structural surprise the batch is rolled back (its stream emptied)
    and the legacy per-word path rebuilds the overlay, so output parity holds
    on every code path — see `_build_text_page_words_batch`.
    """
    if not words_data:
        _build_text_page_lines(page, fallback_lines or [], font_path)
        return
    if _build_text_page_words_batch(page, words_data, font_path):
        return
    _build_text_page_words_legacy(page, words_data, font_path)


def _build_text_page_words_batch(page, words_data, font_path) -> bool:
    """Batched word overlay. Returns True on success; False means the caller
    must run `_build_text_page_words_legacy`.

    Guarantees on False: either the page was never touched, or the single
    committed batch stream has been emptied — so the legacy rebuild can never
    double the overlay or damage the background content.
    """
    import fitz  # PyMuPDF - lazy import (not needed by screenshot tool)
    font = fitz.Font(fontfile=font_path)

    # Phase 1 — pure computation, no page mutation. Filters mirror the
    # legacy path exactly (blank text, non-positive bbox) so parity holds.
    ops = []  # (baseline, text, fontsize, morph matrix, ActualText hex)
    for wd in words_data:
        text = str(wd.get("text") or "")
        if not text.strip():
            continue
        x, y, w, h = _line_xywh(wd)
        if w <= 0 or h <= 0:
            continue
        font_size = max(h * 0.90, 4.0)
        baseline = fitz.Point(x, y + h * 0.81)
        natural_w = max(font.text_length(text, fontsize=font_size), 0.01)
        hscale = max(0.05, min(20.0, w / natural_w))
        actual_text = text + (" " if wd.get("has_space_after", True) else "")
        actual_hex = ("FEFF" + actual_text.encode("utf-16-be").hex()).upper()
        ops.append((baseline, text, font_size, fitz.Matrix(hscale, 1.0), actual_hex))
    if not ops:
        # Nothing survived filtering — same as legacy inserting nothing.
        # Do NOT touch the page's content streams.
        return True

    def fill(shape):
        for baseline, text, font_size, morph, _hex in ops:
            shape.insert_text(
                baseline,
                text,
                fontname="F0",
                fontfile=font_path,
                fontsize=font_size,
                render_mode=3,
                morph=(baseline, morph),
            )

    # Phase 2 — stage the exact same inserts on a scratch page first, so the
    # committed stream bytes are obtained and validated before the real page
    # is modified at all.
    raw = _stage_word_overlay_stream(page.rect.width, page.rect.height, fill)
    if raw is None:
        return False
    wrapped = _wrap_actual_text_spans(raw, [op[4] for op in ops])
    if wrapped is None:
        return False

    # Phase 3 — replay the identical inserts on the real page. The committed
    # bytes must equal the staged bytes; any deviation empties the batch
    # stream and defers to the legacy path.
    xref = None
    try:
        shape = page.new_shape()
        fill(shape)
        shape.commit()
        xref = page.get_contents()[-1]
        if page.parent.xref_stream(xref) != raw:
            raise ValueError("committed stream differs from staged stream")
        page.parent.update_stream(xref, wrapped)
    except Exception:
        if xref is not None:
            try:
                # Emptying the just-committed stream removes the overlay
                # completely (renders/extracts nothing) without touching the
                # page's background content or /Contents structure.
                page.parent.update_stream(xref, b"")
            except Exception:
                pass
        return False
    return True


def _stage_word_overlay_stream(page_w, page_h, fill):
    """Run `fill(shape)` against a scratch page and return the committed
    stream bytes, or None if anything unexpected happens (caller falls back
    to the legacy overlay)."""
    import fitz  # PyMuPDF - lazy import
    try:
        scratch_doc = fitz.open()
        try:
            scratch_page = scratch_doc.new_page(width=page_w, height=page_h)
            shape = scratch_page.new_shape()
            fill(shape)
            shape.commit()
            xrefs = scratch_page.get_contents()
            if not xrefs:
                return None
            return bytes(scratch_doc.xref_stream(xrefs[-1]))
        finally:
            scratch_doc.close()
    except Exception:
        return None


def _wrap_actual_text_spans(raw: bytes, actual_hexes) -> bytes | None:
    """Wrap each staged text block (BT ... ET) with a /Span /ActualText
    BDC..EMC pair, in insertion order — byte-equivalent to the wrapper the
    legacy per-word path produced around its one-word streams.

    Safe without a general PDF parser because every text payload in a staged
    overlay stream is a hex glyph string `<...>`: 'T' is not a hex digit, so
    the byte pairs b"BT"/b"ET" can only be the text-object operators. Any
    count/structure deviation returns None and the caller falls back.
    """
    expected = len(actual_hexes)
    if expected == 0:
        return None
    if raw.count(b"BT") != expected or raw.count(b"ET") != expected:
        return None
    parts = raw.split(b"BT")
    if len(parts) != expected + 1:
        return None
    out = [parts[0]]
    for actual_hex, seg in zip(actual_hexes, parts[1:]):
        if b"ET" not in seg:
            return None
        out.append(f"/Span << /ActualText <{actual_hex}> >> BDC\nBT".encode("ascii"))
        out.append(seg)
        out.append(b"\nEMC\n")
    return b"".join(out)


def _build_text_page_words_legacy(page, words_data, font_path):
    """Original per-word overlay: one `insert_text` + one stream read/rewrite
    per word. Kept verbatim as the correctness fallback for
    `_build_text_page_words_batch` and as the behavioural reference for
    parity tests."""
    import fitz  # PyMuPDF - lazy import (not needed by screenshot tool)
    font = fitz.Font(fontfile=font_path)
    font_name = "F0"
    for wd in words_data:
        text = str(wd.get("text") or "")
        if not text.strip():
            continue
        x, y, w, h = _line_xywh(wd)
        if w <= 0 or h <= 0:
            continue
        font_size = max(h * 0.90, 4.0)
        baseline = fitz.Point(x, y + h * 0.81)
        natural_w = max(font.text_length(text, fontsize=font_size), 0.01)
        hscale = max(0.05, min(20.0, w / natural_w))
        try:
            page.insert_text(
                baseline,
                text,
                fontname=font_name,
                fontfile=font_path,
                fontsize=font_size,
                render_mode=3,
                morph=(baseline, fitz.Matrix(hscale, 1.0)),
                overlay=True,
            )
            content_xref = page.get_contents()[-1]
            content = page.parent.xref_stream(content_xref)
            actual_text = text + (" " if wd.get("has_space_after", True) else "")
            actual_hex = ("FEFF" + actual_text.encode("utf-16-be").hex()).upper()
            page.parent.update_stream(
                content_xref,
                f"/Span << /ActualText <{actual_hex}> >> BDC\n".encode("ascii")
                + content
                + b"\nEMC\n",
            )
        except Exception:
            continue


def _estimate_page_native_dpi(page) -> int:
    """Best-effort DPI của ảnh lớn nhất trên trang.

    Khi trang là scan tràn trang kèm text layer (scan_ocr_low) mà phải
    rasterize để bỏ text cũ, render đúng DPI gốc của ảnh nhúng giữ nguyên
    độ phân giải: render thấp hơn làm giảm DPI (600 → 300), render cao hơn
    chỉ phình file không thêm chi tiết. DPI chỉ kẹp trần 1200 để chặn
    metadata hỏng gây render khổng lồ; trả về OCR_DPI khi không đo được.
    """
    import fitz  # PyMuPDF - lazy import
    page_rect = page.rect
    best_dpi = None
    best_coverage = 0.0
    try:
        for img in page.get_images(full=True):
            xref = img[0]
            iw = img[2]
            ih = img[3]
            if not iw or not ih:
                continue
            # Find the display rect(s) of this image on the page; DPI tính
            # theo rect CÓ ĐỘ PHỦ LỚN NHẤT (rect cuối trong list không phải
            # rect lớn nhất khi ảnh được đặt nhiều lần).
            best_rect = None
            best_rect_area = 0.0
            for r in page.get_image_rects(xref):
                area = r.width * r.height
                if area > best_rect_area:
                    best_rect_area = area
                    best_rect = r
            if best_rect is None:
                continue
            coverage = best_rect_area / max(page_rect.width * page_rect.height, 1)
            if coverage < 0.5:  # not a full-page image, skip
                continue
            dpi = round(((iw / best_rect.width if best_rect.width else 0)
                         + (ih / best_rect.height if best_rect.height else 0)) * 72 / 2)
            if coverage > best_coverage:
                best_coverage = coverage
                best_dpi = max(72, min(dpi, 1200))
    except Exception:
        pass
    return best_dpi or OCR_DPI


def _dominant_image_cs_name(page) -> str:
    """cs-name của ảnh tràn trang trên trang ('' nếu không có)."""
    import fitz  # PyMuPDF - lazy import
    page_area = max(float(page.rect.width * page.rect.height), 1.0)
    best_name = ""
    best_cov = 0.0
    try:
        for info in page.get_image_info(xrefs=True) or []:
            bbox = fitz.Rect(info.get("bbox") or (0, 0, 0, 0))
            cov = (bbox.width * bbox.height) / page_area
            if cov > best_cov:
                best_cov = cov
                best_name = str(info.get("cs-name") or "")
    except Exception:
        pass
    return best_name if best_cov >= 0.5 else ""


def _rasterize_page_to_jpeg_background(doc, src_page, *, page_w, page_h, bake_angle=0, jpeg_quality=85):
    """Create a fresh text-free page whose visual is a JPEG rasterization of
    ``src_page``.

    Used for scan_ocr_low pages that are NOT a single full-page image (those
    get the verbatim-image rebuild in ``_rebuild_text_page_with_original_images``
    instead). Rasterizing drops the text layer while keeping the scanned
    image, rendered at the embedded image's own DPI (giữ nguyên DPI gốc —
    xem ``_estimate_page_native_dpi``). A grayscale scan renders grayscale
    (không bị đổi thành RGB). JPEG encoding (not the default PNG/Flate)
    keeps the output size close to the source.
    """
    import fitz  # PyMuPDF - lazy import
    dpi = _estimate_page_native_dpi(src_page)
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    if bake_angle:
        matrix = matrix * fitz.Matrix(fitz.Identity).prerotate(bake_angle)
    cs_name = _dominant_image_cs_name(src_page).upper()
    if "GRAY" in cs_name:
        pix = src_page.get_pixmap(matrix=matrix, annots=True,
                                  colorspace=fitz.csGRAY)
    else:
        pix = src_page.get_pixmap(matrix=matrix, annots=True)
    # Save as JPEG and insert the JPEG stream directly. PyMuPDF's
    # insert_image(pixmap=...) re-encodes as PNG/Flate (~20-30x larger for a
    # scan); passing a JPEG filename embeds the DCT stream verbatim.
    import os
    import tempfile
    jpg_fd, jpg_path = tempfile.mkstemp(suffix=".jpg")
    os.close(jpg_fd)
    try:
        pix.save(jpg_path, output="jpeg", jpg_quality=jpeg_quality)
        new_page = doc.new_page(width=page_w, height=page_h)
        new_page.insert_image(new_page.rect, filename=jpg_path)
        return new_page
    finally:
        try:
            os.remove(jpg_path)
        except OSError:
            pass


# Định dạng ảnh được phép chèn lại nguyên stream (extract_image trả về đúng
# bytes gốc — JPEG giữ nguyên từng byte, PNG/JPX lossless) khi dựng lại trang
# scan + text layer mà không rasterize.
_VERBATIM_IMAGE_EXTS = {"jpeg", "jpg", "jpx", "jp2", "png"}


def _rebuild_text_page_with_original_images(doc_out, doc_in, src_page,
                                            page_w, page_h, bake_angle=0):
    """Xây lại trang 'scan + text layer' giữ NGUYÊN ảnh gốc (không rasterize).

    Trang scan kèm text layer (scan_ocr_low) trước đây bị rasterize: render
    lại cả trang + re-encode JPEG làm mất một thế hệ nén, và gray bị thành
    RGB. Khi trang chỉ gồm đúng MỘT ảnh tràn trang (đúng dạng scan thật),
    ta chèn lại chính stream ảnh gốc lên trang mới không có text:
    - JPEG: byte giữ nguyên 100% (md5 giống hệt) → DPI/màu/nén không đổi;
    - PNG/JPX: chèn lossless (pixel giống hệt).
    Trả về trang mới, hoặc None khi trang không đúng dạng (nhiều ảnh, có
    smask trong suốt, ảnh không tràn trang…) — caller rasterize như cũ.
    """
    import fitz  # PyMuPDF - lazy import
    try:
        images = src_page.get_images(full=True) or []
    except Exception:
        return None
    if len(images) != 1:
        return None
    img = images[0]
    xref, smask, iw, ih = img[0], img[1], img[2], img[3]
    if smask or not iw or not ih:
        return None
    try:
        rects = src_page.get_image_rects(xref)
    except Exception:
        return None
    if len(rects) != 1:
        return None
    page_area = max(float(src_page.rect.width * src_page.rect.height), 1.0)
    if (rects[0].width * rects[0].height) / page_area < 0.7:
        return None
    try:
        info = doc_in.extract_image(xref)
    except Exception:
        return None
    if not isinstance(info, dict):
        return None
    ext = str(info.get("ext") or "").lower()
    img_bytes = info.get("image") or b""
    if ext not in _VERBATIM_IMAGE_EXTS or not img_bytes:
        return None
    # Chèn lại ĐÚNG rect gốc (giữ nguyên vị trí/tỉ lệ như trang nguồn);
    # bake_angle=180 chỉ xử lý được khi ảnh gần tràn trang (rect xoay 180°
    # trùng rect cũ), lệch nhau thì trả None cho caller rasterize.
    use_rect = rects[0]
    if bake_angle:
        if (rects[0].width * rects[0].height) / page_area < 0.99:
            return None
        import fitz  # PyMuPDF - lazy import
        use_rect = fitz.Rect(0, 0, page_w, page_h)
    new_page = doc_out.new_page(width=page_w, height=page_h)
    new_page.insert_image(use_rect, stream=img_bytes,
                          rotate=int(bake_angle or 0))
    return new_page


def _copy_source_page_background(doc_out, doc_in, page_idx, page_w, page_h, bake_angle=0):
    """
    Copy one source page into the output PDF as a text-free background.

    Three cases:
    - Page already has a native text layer (scan_ocr_low: a scanned page that
      was OCR'd before, so it carries both a full-page image AND a text layer).
      Rasterize it to a JPEG image so the old text layer is gone before the
      ScreenAI overlay is added — otherwise importing to Kho yields double
      text (native + overlay). Rasterizing at the embedded image's own DPI and
      re-encoding as JPEG keeps the size close to the original.
    - No baked transform and no text layer (a plain scan): copy the page object
      verbatim via ``insert_pdf`` so page-level metadata such as /Rotate=270
      survives. show_pdf_page would flatten it into a portrait canvas and
      visually clip landscape scans.
    - A baked rotation is needed: rebuild via show_pdf_page (rare).
    """
    import fitz  # PyMuPDF - lazy import
    src_page = doc_in[page_idx]
    if _page_has_text_layer(src_page):
        # Ưu tiên chèn lại ảnh gốc nguyên vẹn (không mất chất); chỉ
        # rasterize khi trang không đúng dạng "1 ảnh tràn trang".
        rebuilt = _rebuild_text_page_with_original_images(
            doc_out, doc_in, src_page, page_w, page_h, bake_angle=bake_angle
        )
        if rebuilt is not None:
            return rebuilt
        return _rasterize_page_to_jpeg_background(
            doc_out, src_page, page_w=page_w, page_h=page_h, bake_angle=bake_angle
        )
    if not bake_angle:
        doc_out.insert_pdf(doc_in, from_page=page_idx, to_page=page_idx)
        return doc_out[-1]
    new_page = doc_out.new_page(width=page_w, height=page_h)
    new_page.show_pdf_page(new_page.rect, doc_in, page_idx, rotate=bake_angle)
    return new_page


def _page_has_text_layer(page) -> bool:
    """True when the page is a scanned page that also carries a native text
    layer (scan_ocr_low), i.e. it would double up with the ScreenAI
    invisible-text overlay.

    Only flag pages that are BOTH a full-page scan (a large background image)
    AND carry a text layer. A pure digital page (text vector, little/no image)
    must NOT be rasterised — doing so would turn an editable text PDF into a
    flat image and break DOCX export.
    """
    try:
        if not (page.get_text("text") or "").strip():
            return False
    except Exception:
        return False
    # Require a near-full-page image: that's what distinguishes a scanned
    # page with an OCR text layer from a born-digital text page.
    import fitz  # PyMuPDF - lazy import
    try:
        page_area = max(float(page.rect.width * page.rect.height), 1.0)
        infos = page.get_image_info() or []
    except Exception:
        return False
    for info in infos:
        try:
            bbox = fitz.Rect(info.get("bbox"))
        except Exception:
            continue
        coverage = max(0.0, float(bbox.width * bbox.height) / page_area)
        if coverage >= 0.80:
            return True
    return False


def _append_backgrounds_preserving_inline_images(doc_out, doc_in, background_specs):
    """
    Append source-page backgrounds in source order.

    Important PyMuPDF quirk:
    - Repeated ``insert_pdf(... from_page=i, to_page=i)`` on a multi-page source
      can drop later inline / transparency-backed image layers (for example a
      red circular stamp PNG placed above the main scan image).
    - Inserting a contiguous range in ONE ``insert_pdf`` call preserves those
      layers.

    Strategy:
    - Pages that carry a native text layer are handled individually via
      `_copy_source_page_background` (which rasterizes them to drop the text).
      They can never go through the batch insert_pdf path: insert_pdf would
      copy the very text layer we need to remove.
    - Other consecutive pages with ``bake_angle == 0`` are batched into a
      single range insert_pdf call (preserves inline/transparency image layers).
    - A baked rotation is handled individually via show_pdf_page.
    """
    range_start = None
    range_end = None

    def flush_copy_range():
        nonlocal range_start, range_end
        if range_start is not None and range_end is not None:
            doc_out.insert_pdf(doc_in, from_page=range_start, to_page=range_end)
        range_start = None
        range_end = None

    for spec in background_specs:
        page_idx = spec["page_idx"]
        bake_angle = int(spec.get("bake_angle", 0) or 0)
        src_page = doc_in[page_idx]
        has_text = _page_has_text_layer(src_page)

        # Text-layer pages must be rasterized individually; never batch them
        # (insert_pdf would copy the native text we need to drop).
        if bake_angle == 0 and not has_text:
            if range_start is None:
                range_start = range_end = page_idx
            elif page_idx == range_end + 1:
                range_end = page_idx
            else:
                flush_copy_range()
                range_start = range_end = page_idx
            continue

        flush_copy_range()
        _copy_source_page_background(
            doc_out,
            doc_in,
            page_idx,
            spec["page_w"],
            spec["page_h"],
            bake_angle=bake_angle,
        )

    flush_copy_range()


def _normalize_page_coord_to_top_left(lines_data, words_data, page_h):
    """Flip bbox Y to top-left if OCR emitted bottom-left coords.

    ScreenAI occasionally auto-rotates an upside-down image internally to
    read the text, but reports bbox coordinates in the ORIGINAL (upside-down)
    pixel frame. The result is a page whose OCR text is correct but whose
    bbox Y direction is reversed -- i.e. the logically "top" line of the
    document has a LARGE Y value instead of a small one. That convention
    mismatch is a silent train/inference hazard.

    This function detects the case by comparing the average Y of the first 3
    lines (in reading order) to the last 3, and if first > last + 50 it
    flips Y in place for every line and word so the whole page is in
    top-left coords. Returns True if a flip was applied.

    The check uses line order extremes so isolated page-number boxes
    appended at the end of the list cannot fool the decision.
    """
    if not lines_data or len(lines_data) < 6:
        return False
    try:
        by_order = sorted(lines_data, key=lambda L: L.get("order", 0))
        first_ys = [float(L.get("y", 0)) for L in by_order[:3]]
        last_ys = [float(L.get("y", 0)) for L in by_order[-3:]]
        if sum(first_ys) / len(first_ys) <= sum(last_ys) / len(last_ys) + 50.0:
            return False
    except Exception:
        return False
    for rec in list(lines_data) + list(words_data):
        y_new = float(page_h) - (float(rec.get("y", 0)) + float(rec.get("h", 0)))
        rec["y"] = round(y_new, 2)
        bbox = rec.get("bbox")
        if bbox and len(bbox) == 4:
            x0, y0, x1, y1 = bbox
            rec["bbox"] = [x0, round(float(page_h) - y1, 2), x1, round(float(page_h) - y0, 2)]
    return True


def _ocr_result_to_page_data(page_idx, ocr_lines, scale_x, scale_y):
    """
    Convert OCR result lines to structured page data.
    Returns (lines_data, words_data) where:
      - lines_data: for PDF rendering (line-level text + position)
      - words_data: for JSON companion (word-level for correction)
    """
    lines_data = []
    words_data = []

    for line in ocr_lines:
        line_bbox = line.get("bounding_box")
        raw_line_text = (line.get("utf8_string", "") or "").strip()
        line_text = _sanitize_ocr_surface_text(raw_line_text)
        line_words = line.get("words", [])

        if not line_words and not line_text:
            continue

        # Collect word-level data
        page_line_words = []
        line_index = len(lines_data)
        for word in line_words:
            bbox = word.get("bounding_box")
            raw_text = (word.get("utf8_string", "") or "").strip()
            text = _sanitize_ocr_surface_text(raw_text)
            if not bbox or not text:
                continue
            word_index = len(page_line_words)
            page_line_words.append(make_word_record(
                page_index=page_idx,
                line_index=line_index,
                word_index=word_index,
                text=text,
                x=bbox["x"] * scale_x,
                y=bbox["y"] * scale_y,
                w=bbox["width"] * scale_x,
                h=bbox["height"] * scale_y,
                has_space_after=word.get("has_space_after", True),
                confidence=word.get("confidence", 0),
                fg_gray=word.get("foreground_gray", 128),
                content_type=word.get("content_type", 0),
                ocr_text=raw_text,
            ))
            page_line_words[-1]["source_layer"] = "ocr"

        # Preserve line-level OCR even when word boxes are missing.
        if not page_line_words and line_text and line_bbox:
            page_line_words.append(make_word_record(
                page_index=page_idx,
                line_index=line_index,
                word_index=0,
                text=line_text,
                x=line_bbox["x"] * scale_x,
                y=line_bbox["y"] * scale_y,
                w=line_bbox["width"] * scale_x,
                h=line_bbox["height"] * scale_y,
                has_space_after=False,
                confidence=line.get("confidence", 0),
                fg_gray=line.get("foreground_gray", 128),
                content_type=line.get("content_type", 0),
                ocr_text=raw_line_text or line_text,
            ))
            page_line_words[-1]["source_layer"] = "ocr"

        if not page_line_words:
            continue

        words_data.extend(page_line_words)

        # Line-level data: use line bbox for position, reconstruct text from words
        if line_bbox:
            lx = line_bbox["x"] * scale_x
            ly = line_bbox["y"] * scale_y
            lw = line_bbox["width"] * scale_x
            lh = line_bbox["height"] * scale_y
        else:
            # Fallback: derive from first/last word
            lx = page_line_words[0]["x"]
            ly = min(w["y"] for w in page_line_words)
            lw = max(w["x"] + w["w"] for w in page_line_words) - lx
            lh = max(w["h"] for w in page_line_words)

        # Always rebuild from words so line.text stays consistent with word.text.
        display_text = _line_text_from_words(page_line_words, text_key="text")
        display_ocr_text = raw_line_text or _line_text_from_words(page_line_words, text_key="ocr_text")

        # Font size: consistent per line, derived from line height
        font_size = max(lh * 0.78, 4.0)

        # Compute average fg_gray for this line from its words
        word_grays = [w["fg_gray"] for w in page_line_words if w.get("fg_gray", 128) != 128]
        avg_fg_gray = round(sum(word_grays) / len(word_grays)) if word_grays else 128

        line_record = make_line_record(
            page_index=page_idx,
            line_index=line_index,
            text=display_text,
            x=lx,
            y=ly,
            w=lw,
            h=lh,
            font_size=font_size,
            block_id=line.get("block_id", 0),
            paragraph_id=line.get("paragraph_id", 0),
            confidence=line.get("confidence", 0),
            content_type=line.get("content_type", 0),
            fg_gray=avg_fg_gray,
            word_ids=[w["id"] for w in page_line_words],
            ocr_text=display_ocr_text,
        )
        line_record["source_layer"] = "ocr"
        lines_data.append(line_record)

    return lines_data, words_data


# ===========================================================================
# Public API
# ===========================================================================

def check_dependencies():
    return setup_library()


def setup_library():
    dll_path, model_dir = _find_screen_ai_paths()
    if dll_path and os.path.exists(dll_path):
        return True, None
    return False, "chrome_screen_ai.dll not found in models/screen_ai/"


def _preprocess_metadata_from_legacy_rotations(preprocess_metadata, preprocess_rotations):
    """Normalize old and new preprocess metadata into the manifest contract."""
    if isinstance(preprocess_metadata, dict):
        data = dict(preprocess_metadata)
    else:
        data = {}
    if preprocess_rotations and "page_rotations" not in data:
        data["page_rotations"] = list(preprocess_rotations)
    return data


def _pick_native_text_page(original_doc, visual_doc, page_idx: int):
    """Prefer the original vector page when it shares the visual coordinate space."""
    visual_page = visual_doc[page_idx]
    try:
        original_page = original_doc[page_idx]
    except Exception:
        return visual_page
    if (
        abs(float(original_page.rect.width) - float(visual_page.rect.width)) <= 1.0
        and abs(float(original_page.rect.height) - float(visual_page.rect.height)) <= 1.0
    ):
        return original_page
    return visual_page


def _attach_docx_manifest_to_page(page_record: dict, manifest_entry: dict) -> None:
    """Stamp provenance on a canonical DOCX-export page record."""
    page_record["coord_origin"] = "top-left"
    page_record["coord_space"] = "visual_page_after_preprocess"
    page_record["source_mode"] = manifest_entry.get("source_mode") or "scan"
    page_record["text_source"] = manifest_entry.get("text_source") or "ocr"
    page_record["visual_source"] = dict(manifest_entry.get("visual_source") or {})
    page_record["preprocess"] = dict(manifest_entry.get("preprocess") or {})
    page_record["classification"] = dict(manifest_entry.get("classification") or {})


def _source_layer_for_page_mode(source_mode: str, text_source: str) -> str:
    if source_mode == "digital" or text_source == "native":
        return "native"
    if source_mode == "mixed" or "+" in str(text_source):
        return "mixed"
    return "ocr"


def _ensure_page_source_layer(page_record: dict, manifest_entry: dict) -> None:
    fallback = _source_layer_for_page_mode(
        str(manifest_entry.get("source_mode") or "scan"),
        str(manifest_entry.get("text_source") or "ocr"),
    )
    for word in page_record.get("words") or []:
        if isinstance(word, dict):
            word.setdefault("source_layer", fallback if fallback != "mixed" else "ocr")
    for line in page_record.get("lines") or []:
        if isinstance(line, dict):
            if line.get("source_layer"):
                continue
            line_word_ids = set(line.get("word_ids") or [])
            word_layers = {
                str(word.get("source_layer") or "")
                for word in page_record.get("words") or []
                if isinstance(word, dict) and word.get("id") in line_word_ids
            }
            word_layers.discard("")
            line["source_layer"] = next(iter(word_layers)) if len(word_layers) == 1 else fallback


def _ocr_page_record_from_result(page_idx: int, page, page_result: dict, manifest_entry: dict) -> tuple[dict, bool]:
    lines_data = copy.deepcopy((page_result or {}).get("lines_data") or [])
    words_data = copy.deepcopy((page_result or {}).get("words_data") or [])
    coord_flipped = _normalize_page_coord_to_top_left(lines_data, words_data, page.rect.height)

    page_record = make_page_record(
        page_index=page_idx,
        width=page.rect.width,
        height=page.rect.height,
        render_width=(page_result or {}).get("render_width", 0),
        render_height=(page_result or {}).get("render_height", 0),
        applied_rotation=(manifest_entry.get("preprocess") or {}).get("rotation", 0),
    )
    page_record["lines"] = lines_data
    page_record["words"] = words_data
    if coord_flipped:
        page_record["coord_origin_source"] = "normalized_from_bottom_left"
    _attach_docx_manifest_to_page(page_record, manifest_entry)
    _ensure_page_source_layer(page_record, manifest_entry)
    return page_record, coord_flipped


def process_pdf_for_docx_export(input_path, output_path, num_pages=None, update_callback=None,
                                source_document_path=None,
                                allow_page_parallel=True,
                                preprocess_rotations=None,
                                preprocess_metadata=None,
                                include_layout_analysis=True):
    """Build the DOCX-export canonical sidecar from a page-wise source manifest.

    This path is intentionally separate from the production KIE OCR path:
    it does not build LayoutLM/KIE tokens and it classifies digital/scan/mixed
    at page level instead of routing the whole file through one branch.
    """
    def log(msg, level="info"):
        if update_callback:
            try:
                update_callback(msg, level)
            except Exception:
                try:
                    update_callback(msg)
                except Exception:
                    pass
        logger.info(msg)

    try:
        import fitz
        from scanindex.core.pdf.docx_page_manifest import (
            build_page_source_manifest,
            manifest_source_mode,
        )
        from scanindex.core.pdf.text_extractor import (
            build_native_page_record,
            merge_native_and_ocr_page_records,
        )

        original_path = source_document_path or input_path
        pre_meta = _preprocess_metadata_from_legacy_rotations(
            preprocess_metadata,
            preprocess_rotations,
        )
        manifest = build_page_source_manifest(
            original_pdf_path=original_path,
            visual_pdf_path=input_path,
            preprocess_metadata=pre_meta,
        )

        original_doc = fitz.open(original_path)
        visual_doc = fitz.open(input_path)
        try:
            total_pages = min(len(visual_doc), len(original_doc), len(manifest))
            if num_pages:
                try:
                    total_pages = min(total_pages, max(0, int(num_pages)))
                except (TypeError, ValueError):
                    pass
            manifest = manifest[:total_pages]

            source_mode = manifest_source_mode(manifest)
            log(f"DOCX export source manifest: {total_pages} page(s), source_mode={source_mode}", "info")

            ocr_page_indices = [
                int(item["page_index"])
                for item in manifest
                if item.get("source_mode") in {"scan", "mixed"}
            ]
            all_page_results = {}
            if ocr_page_indices:
                use_parallel = bool(
                    allow_page_parallel
                    and len(ocr_page_indices) >= 2
                    and _NUM_PAGE_WORKERS > 1
                )
                if use_parallel:
                    try:
                        _get_pool()
                        log(
                            f"Parallel OCR: {_NUM_PAGE_WORKERS} page workers for "
                            f"{len(ocr_page_indices)}/{total_pages} OCR page(s)",
                            "info",
                        )
                        all_page_results = _process_selected_pages_parallel(
                            input_path,
                            ocr_page_indices,
                            total_pages,
                            log,
                        )
                    except Exception as e:
                        log(f"Parallel init failed, using serial OCR: {e}", "info")
                        all_page_results = _process_selected_pages_serial(
                            input_path,
                            visual_doc,
                            ocr_page_indices,
                            total_pages,
                            log,
                        )
                else:
                    all_page_results = _process_selected_pages_serial(
                        input_path,
                        visual_doc,
                        ocr_page_indices,
                        total_pages,
                        log,
                    )
            else:
                log("DOCX export: all pages use native text; OCR skipped", "info")

            font_path = _find_unicode_font() if ocr_page_indices else None
            if ocr_page_indices and not font_path:
                return False, "No Unicode font found (need arial.ttf or similar)"

            _layout_analyzers = (
                _init_layout_analyzers(log)
                if include_layout_analysis
                else (None, None, None)
            )

            ocr_data = make_document_stub(
                input_path=input_path,
                engine="docx_pagewise_export",
                ocr_dpi=OCR_DPI,
                source_path=original_path,
                text_normalization=OCR_TEXT_NORMALIZATION,
                raw_text_preserved=True,
                source_mode=source_mode,
            )
            ocr_data.setdefault("pipeline", {}).setdefault("docx_export", {}).update({
                "page_source_manifest_version": "docx_page_manifest_v1",
                "page_source_manifest": copy.deepcopy(manifest),
                "original_pdf_path": os.path.abspath(original_path),
                "visual_pdf_path": os.path.abspath(input_path),
                "coordinate_contract": "top_left_pdf_points_on_visual_page_after_preprocess",
            })

            pending_text_overlays = []
            background_specs = []
            for page_idx, manifest_entry in enumerate(manifest):
                visual_page = visual_doc[page_idx]
                source_mode_page = manifest_entry.get("source_mode")
                coord_flipped = False

                if source_mode_page == "digital":
                    native_page = _pick_native_text_page(original_doc, visual_doc, page_idx)
                    page_record = build_native_page_record(page_idx, native_page)
                    _attach_docx_manifest_to_page(page_record, manifest_entry)
                    _ensure_page_source_layer(page_record, manifest_entry)
                elif source_mode_page == "mixed":
                    _validate_page_result_or_raise(page_idx, all_page_results.get(page_idx))
                    native_page = _pick_native_text_page(original_doc, visual_doc, page_idx)
                    ocr_record, coord_flipped = _ocr_page_record_from_result(
                        page_idx,
                        visual_page,
                        all_page_results.get(page_idx) or {},
                        manifest_entry,
                    )
                    page_record = merge_native_and_ocr_page_records(page_idx, native_page, ocr_record)
                    _attach_docx_manifest_to_page(page_record, manifest_entry)
                    _ensure_page_source_layer(page_record, manifest_entry)
                else:
                    _validate_page_result_or_raise(page_idx, all_page_results.get(page_idx))
                    page_record, coord_flipped = _ocr_page_record_from_result(
                        page_idx,
                        visual_page,
                        all_page_results.get(page_idx) or {},
                        manifest_entry,
                    )

                layout_regions = _analyze_combined_layout_regions(
                    visual_page,
                    page_idx,
                    visual_page.rect.width,
                    visual_page.rect.height,
                    _layout_analyzers,
                    log,
                )
                if layout_regions:
                    page_record["layout_regions"] = layout_regions

                ocr_data["pages"].append(page_record)
                should_overlay = source_mode_page in {"scan", "mixed"}
                if should_overlay:
                    overlay_words = [
                        w for w in (page_record.get("words") or [])
                        if (w.get("source_layer") or "ocr") != "native"
                    ]
                    overlay_lines = [
                        ln for ln in (page_record.get("lines") or [])
                        if (ln.get("source_layer") or "ocr") != "native"
                    ]
                    pending_text_overlays.append((overlay_words, overlay_lines))
                else:
                    pending_text_overlays.append(([], []))
                background_specs.append({
                    "page_idx": page_idx,
                    "page_w": visual_page.rect.width,
                    "page_h": visual_page.rect.height,
                    "bake_angle": 180 if coord_flipped else 0,
                })

            doc_out = fitz.open()
            _append_backgrounds_preserving_inline_images(doc_out, visual_doc, background_specs)
            if font_path:
                for page_idx, (words_data, lines_data) in enumerate(pending_text_overlays):
                    if page_idx >= len(doc_out):
                        break
                    _build_text_page_words(
                        doc_out[page_idx],
                        words_data or [],
                        font_path,
                        fallback_lines=lines_data or [],
                    )
            doc_out.save(output_path, deflate=True, garbage=4)
            doc_out.close()
        finally:
            original_doc.close()
            visual_doc.close()

        json_path = output_path + ".json.zst"
        save_canonical(json_path, ocr_data, profile="docx_export")

        log(f"DOCX-export OCR completed: {output_path}", "success")
        return True, None

    except Exception as e:
        import traceback
        traceback.print_exc()
        return False, str(e)


def process_pdf(input_path, output_path, num_pages=None, update_callback=None,
                wait_per_page=None, comparison_interval=None,
                source_document_path=None,
                allow_page_parallel=True,
                preprocess_rotations=None,
                canonical_profile=None,
                preprocess_metadata=None,
                include_layout_analysis=True):
    """
    OCR a PDF and produce output with positioned text.
    Drop-in replacement for chrome_ocr_engine.process_pdf().

    Uses page-level multiprocessing when processing multi-page PDFs
    to utilize multiple CPU cores (each worker loads its own DLL instance).

    ``preprocess_rotations`` is an optional list of cardinal rotations
    (0/90/180/270) that upstream preprocessing applied per page; when
    provided it is stored on each canonical page record as
    ``applied_rotation`` so downstream viewers can render the ORIGINAL
    source PDF in the same orientation as the OCR bboxes.
    """
    def log(msg, level="info"):
        if update_callback:
            try:
                update_callback(msg, level)
            except:
                try:
                    update_callback(msg)
                except:
                    pass
        logger.info(msg)

    try:
        import fitz  # PyMuPDF - lazy import
        from scanindex.core.preprocessing.preprocessing import classify_pdf

        if resolve_profile(canonical_profile) == DOCX_EXPORT_PROFILE_V1:
            return process_pdf_for_docx_export(
                input_path,
                output_path,
                num_pages=num_pages,
                update_callback=update_callback,
                source_document_path=source_document_path,
                allow_page_parallel=allow_page_parallel,
                preprocess_rotations=preprocess_rotations,
                preprocess_metadata=preprocess_metadata,
                include_layout_analysis=include_layout_analysis,
            )

        pdf_type = classify_pdf(input_path)
        if pdf_type == "digital":
            from scanindex.core.pdf.text_extractor import extract_digital_pdf_as_ocr

            log("Digital PDF detected: extracting native text layer to OCR-compatible JSON...", "info")
            return extract_digital_pdf_as_ocr(
                input_path,
                output_path,
                source_document_path=source_document_path or input_path,
                update_callback=update_callback,
                canonical_profile=canonical_profile,
            )

        doc_in = fitz.open(input_path)
        total_pages = len(doc_in)

        log(f"Processing {total_pages} pages...", "info")

        font_path = _find_unicode_font()
        if not font_path:
            doc_in.close()
            return False, "No Unicode font found (need arial.ttf or similar)"

        # Determine parallelism: page-level pool with NUM_PAGE_WORKERS workers
        use_parallel = bool(
            allow_page_parallel and
            total_pages >= 2 and
            _NUM_PAGE_WORKERS > 1
        )

        if use_parallel:
            try:
                _get_pool()  # warm pool (lazy init)
                log(f"Parallel OCR: {_NUM_PAGE_WORKERS} page workers for {total_pages} pages", "info")
                all_page_results = _process_pages_parallel(
                    input_path, total_pages, log
                )
            except Exception as e:
                log(f"Parallel init failed, using serial: {e}", "info")
                all_page_results = _process_pages_serial(
                    input_path, doc_in, total_pages, log
                )
        else:
            all_page_results = _process_pages_serial(
                input_path, doc_in, total_pages, log
            )

        if all_page_results is None:
            doc_in.close()
            return False, "OCR processing failed"

        # Try to initialize layout analyzers only for flows that need
        # page-level semantic/table regions, such as PDF-to-Word export.
        _layout_analyzers = (
            _init_layout_analyzers(log)
            if include_layout_analysis
            else (None, None, None)
        )

        # Assemble output PDF and canonical JSON
        ocr_data = make_document_stub(
            input_path=input_path,
            engine="direct_screen_ai",
            ocr_dpi=OCR_DPI,
            source_path=source_document_path or input_path,
            text_normalization=OCR_TEXT_NORMALIZATION,
            raw_text_preserved=True,
            source_mode="scan",
        )

        doc_out = fitz.open()
        pending_text_overlays = []
        background_specs = []
        for page_idx in range(total_pages):
            page = doc_in[page_idx]
            page_w = page.rect.width
            page_h = page.rect.height

            pr = all_page_results[page_idx]
            lines_data = pr["lines_data"]
            words_data = pr["words_data"]

            # Run layout analysis on same page (if available)
            layout_regions = _analyze_combined_layout_regions(
                page,
                page_idx,
                page_w,
                page_h,
                _layout_analyzers,
                log,
            )

            # Normalize bbox Y direction to top-left BEFORE building the
            # canonical page record, so downstream train/inference always sees
            # the same coord convention regardless of whether ScreenAI
            # auto-rotated internally.
            coord_flipped = _normalize_page_coord_to_top_left(lines_data, words_data, page_h)

            # Store in canonical JSON
            applied_rotation = 0
            if preprocess_rotations and page_idx < len(preprocess_rotations):
                try:
                    applied_rotation = int(preprocess_rotations[page_idx] or 0) % 360
                except (TypeError, ValueError):
                    applied_rotation = 0
            page_data = make_page_record(
                page_index=page_idx,
                width=page_w,
                height=page_h,
                render_width=pr.get("render_width", 0),
                render_height=pr.get("render_height", 0),
                applied_rotation=applied_rotation,
            )
            page_data["coord_origin"] = "top-left"
            if coord_flipped:
                page_data["coord_origin_source"] = "normalized_from_bottom_left"
            page_data["lines"] = lines_data
            page_data["words"] = words_data
            if layout_regions:
                page_data["layout_regions"] = layout_regions
            ocr_data["pages"].append(page_data)

            # Create output page. When the coord normalizer detected that
            # ScreenAI had auto-rotated internally (source page is visually
            # upside-down), BAKE the 180° rotation into the content stream
            # via show_pdf_page(rotate=180). This rewrites the content at
            # the vector level -- no rasterization, no quality loss -- so the
            # output renders correctly in EVERY PDF viewer, not just those
            # that honor the /Rotate metadata flag.
            bake_angle = 180 if coord_flipped else 0
            pending_text_overlays.append((words_data, lines_data))
            background_specs.append({
                "page_idx": page_idx,
                "page_w": page_w,
                "page_h": page_h,
                "bake_angle": bake_angle,
            })

        _append_backgrounds_preserving_inline_images(doc_out, doc_in, background_specs)

        for page_idx, (words_data, lines_data) in enumerate(pending_text_overlays):
            if page_idx >= len(doc_out):
                break
            _build_text_page_words(
                doc_out[page_idx],
                words_data,
                font_path,
                fallback_lines=lines_data,
            )

        doc_out.save(output_path, deflate=True, garbage=4)
        doc_out.close()
        doc_in.close()

        json_path = output_path + ".json.zst"
        save_canonical(json_path, ocr_data, profile=canonical_profile)

        log(f"OCR completed: {output_path}", "success")
        return True, None

    except Exception as e:
        import traceback
        traceback.print_exc()
        return False, str(e)


def find_failed_page_results(page_results, total_pages) -> list[tuple[int, str]]:
    """Detect pages whose OCR result is missing or carries an error payload.

    Distinguishes a legitimately blank page (result present with render
    dimensions and zero lines — a valid outcome) from a failed one:
    result missing/None (submit or worker crash) or carrying
    ``worker_init_failed``/``error`` (DLL/engine failure payloads that
    `_worker_ocr_single` returns instead of raising).

    Returns a list of ``(page_index, reason)`` with 0-based ``page_index``;
    ``reason`` is ``"missing"`` or ``"engine-error"``. Callers (Archive
    Step 1) use this to refuse assembling/predicting on incomplete OCR
    instead of silently emitting empty canonical pages.
    """
    failures: list[tuple[int, str]] = []
    for page_idx in range(int(total_pages or 0)):
        try:
            pr = page_results.get(page_idx)
        except Exception:
            pr = None
        if pr is None:
            failures.append((page_idx, "missing"))
            continue
        try:
            if pr.get("worker_init_failed") or pr.get("error"):
                failures.append((page_idx, "engine-error"))
        except AttributeError:
            failures.append((page_idx, "engine-error"))
    return failures


def assemble_pdf_from_page_results(input_path, output_path, all_page_results,
                                   source_document_path=None,
                                   source_page_indices=None,
                                   preprocess_rotations=None,
                                   update_callback=None,
                                   canonical_profile=None,
                                   include_layout_analysis=True,
                                   pdf_output=True):
    """Assemble `_ocr.pdf` + canonical JSON from already-OCRed page_results.

    Skips the OCR phase entirely — used when Step 1 of the archive screen has
    already pre-OCR'd every page and the cache hits. Mirrors the assembly
    half of `process_pdf` (lines 727-850).

    ``pdf_output=False`` builds ONLY the canonical JSON sidecar
    (``<output_path>.json.zst``): no font lookup, no background copy, no
    text overlay, no PDF write. Used by Archive Step 1, where the
    full-source OCR PDF is never consumed downstream (Step 2 reassembles
    per-segment PDFs from the page cache) and only the canonical JSON feeds
    doc-start prediction + secrecy detection. Page geometry/index mapping,
    coordinate normalization and the canonical profile stay identical to
    the full build."""
    def log(msg, level="info"):
        if update_callback:
            try:
                update_callback(msg, level)
            except Exception:
                try:
                    update_callback(msg)
                except Exception:
                    pass
        logger.info(msg)

    try:
        import fitz

        font_path = None
        if pdf_output:
            font_path = _find_unicode_font()
            if not font_path:
                return False, "No Unicode font found (need arial.ttf or similar)"

        source_for_pages = (source_document_path or input_path) if source_page_indices else input_path
        doc_in = fitz.open(source_for_pages)
        if source_page_indices:
            page_map = [int(p) for p in source_page_indices]
            total_pages = len(page_map)
            source_page_count = len(doc_in)
            max_page = source_page_count - 1
            bad_pages = [p for p in page_map if p < 0 or p > max_page]
            if bad_pages:
                doc_in.close()
                return False, (
                    f"Invalid source page index {bad_pages[0] + 1}; "
                    f"source has {source_page_count} pages"
                )
        else:
            page_map = list(range(len(doc_in)))
            total_pages = len(page_map)

        _layout_analyzers = (
            _init_layout_analyzers(log)
            if include_layout_analysis
            else (None, None, None)
        )

        ocr_data = make_document_stub(
            input_path=input_path,
            engine="direct_screen_ai",
            ocr_dpi=OCR_DPI,
            source_path=source_document_path or input_path,
            text_normalization=OCR_TEXT_NORMALIZATION,
            raw_text_preserved=True,
            source_mode="scan",
        )

        doc_out = fitz.open() if pdf_output else None
        pending_text_overlays = []
        background_specs = []
        for page_idx in range(total_pages):
            source_page_idx = page_map[page_idx]
            page = doc_in[source_page_idx]
            page_w = page.rect.width
            page_h = page.rect.height

            pr = all_page_results.get(page_idx) or {
                "lines_data": [],
                "words_data": [],
                "render_width": 0,
                "render_height": 0,
            }
            lines_data = pr.get("lines_data", [])
            words_data = pr.get("words_data", [])

            layout_regions = _analyze_combined_layout_regions(
                page,
                page_idx,
                page_w,
                page_h,
                _layout_analyzers,
                log,
            )

            coord_flipped = _normalize_page_coord_to_top_left(lines_data, words_data, page_h)

            applied_rotation = 0
            if preprocess_rotations and page_idx < len(preprocess_rotations):
                try:
                    applied_rotation = int(preprocess_rotations[page_idx] or 0) % 360
                except (TypeError, ValueError):
                    applied_rotation = 0
            page_data = make_page_record(
                page_index=page_idx,
                width=page_w,
                height=page_h,
                render_width=pr.get("render_width", 0),
                render_height=pr.get("render_height", 0),
                applied_rotation=applied_rotation,
            )
            page_data["coord_origin"] = "top-left"
            if coord_flipped:
                page_data["coord_origin_source"] = "normalized_from_bottom_left"
            page_data["lines"] = lines_data
            page_data["words"] = words_data
            if layout_regions:
                page_data["layout_regions"] = layout_regions
            ocr_data["pages"].append(page_data)

            bake_angle = 180 if coord_flipped else 0
            if pdf_output:
                pending_text_overlays.append((words_data, lines_data))
                background_specs.append({
                    "page_idx": source_page_idx,
                    "page_w": page_w,
                    "page_h": page_h,
                    "bake_angle": bake_angle,
                })

        if pdf_output:
            _append_backgrounds_preserving_inline_images(doc_out, doc_in, background_specs)

            for page_idx, (words_data, lines_data) in enumerate(pending_text_overlays):
                if page_idx >= len(doc_out):
                    break
                _build_text_page_words(
                    doc_out[page_idx],
                    words_data,
                    font_path,
                    fallback_lines=lines_data,
                )

            doc_out.save(output_path, deflate=True, garbage=4)
            doc_out.close()
        doc_in.close()

        json_path = output_path + ".json.zst"
        save_canonical(json_path, ocr_data, profile=canonical_profile)

        if pdf_output:
            log(f"Assembled (cached OCR): {output_path}", "success")
        else:
            log(f"Assembled canonical JSON only (cached OCR): {json_path}", "success")
        return True, None

    except Exception as e:
        import traceback
        traceback.print_exc()
        return False, str(e)


def assemble_pdf_from_page_results_payload(payload_path: str):
    """Worker-process entry point for Step 2 assembly.

    The assembly path can spend minutes in PyMuPDF/text-layer operations and
    may hold the GIL long enough to starve Qt if it runs in the GUI process.
    Passing only a pickle path keeps ProcessPool argument pickling small.
    """
    import pickle
    import time

    t0 = time.perf_counter()
    try:
        with open(payload_path, "rb") as fh:
            payload = pickle.load(fh)
        ok, msg = assemble_pdf_from_page_results(
            payload["input_path"],
            payload["output_path"],
            payload["page_results"],
            source_document_path=payload.get("source_document_path"),
            source_page_indices=payload.get("source_page_indices"),
            preprocess_rotations=payload.get("preprocess_rotations"),
            update_callback=None,
            canonical_profile=payload.get("canonical_profile"),
            include_layout_analysis=bool(payload.get("include_layout_analysis", True)),
            pdf_output=bool(payload.get("pdf_output", True)),
        )
        return {
            "ok": bool(ok),
            "msg": msg,
            "elapsed_s": time.perf_counter() - t0,
            "output_path": payload.get("output_path"),
        }
    except BaseException as exc:
        import traceback
        return {
            "ok": False,
            "msg": f"{exc}\n{traceback.format_exc()}",
            "elapsed_s": time.perf_counter() - t0,
            "output_path": "",
        }


def _process_pages_serial(input_path, doc_in, total_pages, log):
    """Serial OCR processing (original single-threaded path)."""
    from PIL import Image

    ocr = _get_ocr()
    results = {}

    for page_idx in range(total_pages):
        page = doc_in[page_idx]
        page_w = page.rect.width
        page_h = page.rect.height

        log(f"OCR page {page_idx + 1}/{total_pages}...", "debug")

        import fitz
        dpi = OCR_DPI
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=mat, annots=True)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

        scale_x = page_w / pix.width
        scale_y = page_h / pix.height

        with _ocr_lock:
            result = ocr.perform_ocr(img)

        ocr_lines = result.get("lines", [])
        log(f"  Page {page_idx + 1}: {len(ocr_lines)} lines detected", "debug")

        lines_data, words_data = _ocr_result_to_page_data(
            page_idx, ocr_lines, scale_x, scale_y
        )

        results[page_idx] = {
            "lines_data": lines_data,
            "words_data": words_data,
            "render_width": pix.width,
            "render_height": pix.height,
        }

    return results


def _empty_page_ocr_result():
    return {
        "lines_data": [],
        "words_data": [],
        "render_width": 0,
        "render_height": 0,
    }


def _validate_page_result_or_raise(page_idx: int, result) -> None:
    """GĐ 0 (P1-09): trang scan/mixed cần OCR — kết quả sentinel/missing KHÔNG
    được lắp thành trang không chữ rồi báo thành công."""
    state = page_result_state(result)
    if state in {"error", "missing"}:
        raise RuntimeError(
            f"OCR thất bại ở trang {page_idx + 1} (state={state}) — "
            "không xuất trang rỗng thay thế"
        )


def page_result_state(result) -> str:
    """Hợp đồng validator dùng chung (GĐ 0): phân biệt kết quả OCR hợp lệ với
    lỗi — MỌI consumer phải gọi trước khi lắp trang/commit.

    Trả về:
      - ``"ok_text"``   : có chữ (lines/words không rỗng)
      - ``"ok_blank"``  : trang trắng HỢP LỆ (0 dòng nhưng render dims > 0)
      - ``"error"``     : sentinel lỗi / worker_init_failed / shape sai
      - ``"missing"``   : None / không phải dict / thiếu hẳn
    ``error`` và ``missing`` KHÔNG BAO GIỜ được lắp thành trang trắng."""
    if result is None or not isinstance(result, dict):
        return "missing"
    if result.get("ocr_error"):
        return "error"
    if result.get("worker_init_failed"):
        return "error"
    try:
        width = int(result.get("render_width", 0) or 0)
        height = int(result.get("render_height", 0) or 0)
    except (TypeError, ValueError):
        return "error"
    has_text = bool(result.get("lines_data") or result.get("words_data"))
    if has_text:
        return "ok_text"
    if width > 0 and height > 0:
        return "ok_blank"
    return "error"


def _collect_page_ocr_bounded(input_path, page_indices, total_pages, log,
                              page_timeout: float = 120.0, dpi=None, *,
                              max_in_flight: int | None = None,
                              on_page_done=None,
                              stop_check=None,
                              cancel_check=None):
    """Thu thập OCR trang qua admission gate — vòng điều phối liên tục.

    - Không trần ẩn 16 trang: cửa sổ mỗi lượt = `min(số trang, max_in_flight
      hoặc capacity)`; giới hạn TỔNG do token gate đảm nhiệm (12.2.4).
    - `on_page_done(page_idx, result) -> bool|None`: gọi ngay khi một trang có
      kết quả (kể cả sentinel lỗi); trả True → dừng nộp thêm.
    - `stop_check()` → dừng nộp thêm (dừng sớm); `cancel_check()` → raise
      `CoreScanCancelled` (hủy nguyên lượt — request đã nộp bỏ qua, token do
      callback/janitor giải phóng, input tạm được giữ đến hết quyền sử dụng).
    - Lỗi/timeout trang → sentinel `{"ocr_error": True, "error": …}` — KHÔNG
      bao giờ thay bằng trang rỗng "thành công".
    - `Busy` không phải lỗi: khi gate đầy mà pending rỗng, chờ bounded rồi
      thử lại đúng trang đó — không kết thúc sớm, không fallback.

    Trả về `(results dict theo page_idx, cửa sổ đã dùng)`."""
    import time

    page_indices = [
        int(idx) for idx in sorted({int(idx) for idx in page_indices})
        if 0 <= int(idx) < total_pages
    ]
    if not page_indices:
        return {}, 0

    if _GATE_DISABLED:
        window_cap = max_in_flight or _env_int("OCRTOOL_MAX_IN_FLIGHT_PAGES", 16)
    else:
        window_cap = max_in_flight or _gate_capacity()
    window = min(len(page_indices), max(1, int(window_cap)))

    results: dict[int, dict] = {}
    pending: list[tuple[int, object, float]] = []  # (page_idx, handle, submitted_at)
    page_wait_started: dict[int, float] = {}  # P2-H: mốc caller bắt đầu yêu cầu trang
    next_pos = 0
    stopping = False

    wait_started: float | None = None
    while True:
        if cancel_check is not None and cancel_check():
            raise CoreScanCancelled("cancelled during page OCR")

        # ── pha nộp (try-acquire, không bao giờ block vô hạn) ──
        while not stopping and next_pos < len(page_indices) and len(pending) < window:
            if stop_check is not None and stop_check():
                stopping = True
                break
            page_idx_now = page_indices[next_pos]
            state, handle = try_submit_page(input_path, page_idx_now, dpi=dpi)
            if state == "busy":
                if page_idx_now not in page_wait_started:
                    page_wait_started[page_idx_now] = time.monotonic()
                break
            if state == "error":
                raise handle
            wait_started = None
            if page_idx_now in page_wait_started:
                # P2-H: caller đã Busy chờ trang này — ghi entry full-wait
                # (admission_wait từ lúc bắt đầu yêu cầu, không chỉ lần try
                # thành công). Entry attempt-level do try_submit tự ghi.
                with _admission_lock:
                    _admission_metrics["admission_wait_ms"].append(
                        int((time.monotonic() - page_wait_started.pop(page_idx_now)) * 1000)
                    )
            pending.append((page_idx_now, handle, time.monotonic()))
            next_pos += 1

        if stopping:
            # không nộp thêm; vẫn thu nốt các trang đã nộp (giới hạn cửa sổ)
            if not pending:
                break
        elif not pending:
            if next_pos >= len(page_indices):
                break
            # gate đầy từ đầu (caller khác giữ slot) → chờ bounded, thử lại.
            # Deadline tổng (P2-12): chờ admission quá `_ADMISSION_WAIT_S` →
            # các trang chưa nộp đánh dấu lỗi rõ ràng (không treo vô hạn).
            if wait_started is None:
                wait_started = time.monotonic()
            elif time.monotonic() - wait_started > _ADMISSION_WAIT_S:
                raise PoolResetError(
                    f"admission wait exceeded {_ADMISSION_WAIT_S:.0f}s — "
                    f"{len(page_indices) - next_pos} trang chưa nộp"
                )
            time.sleep(0.05)
            continue

        # ── pha thu: chọn trang ready hoặc quá hạn ──
        picked = None
        now = time.monotonic()
        for idx, (page_idx, handle, submitted_at) in enumerate(pending):
            timed_out = (
                page_timeout is not None and now - submitted_at >= float(page_timeout)
            )
            if handle.ready() or timed_out:
                picked = idx
                break
        if picked is None:
            time.sleep(0.02)
            continue

        page_idx, handle, submitted_at = pending.pop(picked)
        timed_out = page_timeout is not None and now - submitted_at >= float(page_timeout)
        page_result = None
        if handle.ready():
            try:
                # nội bộ: đọc RAW 4 phần tử (token/page/result/timing) — hợp
                # đồng public 2 phần tử chỉ áp cho handle ra ngoài (P1-03)
                _token_id, _pi, page_result, _timing = handle.ar.get(timeout=0.1)
            except Exception as e:
                log(f"  Page {page_idx + 1}/{total_pages}: OCR error: {e}", "error")
                page_result = {"ocr_error": True, "error": str(e)}
        elif timed_out:
            # Caller hết hạn chờ: kết luận "caller thấy" ≠ "native đã xong" —
            # trả sentinel; token do callback/janitor giải phóng khi task thật
            # sự kết thúc (không nhả slot giả).
            log(
                f"  Page {page_idx + 1}/{total_pages}: OCR error: caller timeout "
                f"(>{page_timeout:.0f}s)",
                "error",
            )
            page_result = {"ocr_error": True, "error": "caller timeout"}
        else:
            # không ready, chưa quá hạn mà được chọn là trạng thái lệch —
            # vòng sau sẽ chọn lại; bỏ qua nhánh này một cách an toàn.
            pending.insert(0, (page_idx, handle, submitted_at))
            time.sleep(0.02)
            continue
        if isinstance(page_result, dict) and page_result.get("worker_init_failed"):
            detail = page_result.get("error") or "ScreenAI worker initialization failed"
            _request_pool_recovery(f"worker init failed: {detail}")
            raise RuntimeError(str(detail))
        if isinstance(page_result, dict) and page_result.get("ocr_error"):
            log(
                f"  Page {page_idx + 1}/{total_pages}: OCR failed "
                f"({page_result.get('error')})",
                "error",
            )
        results[page_idx] = page_result
        if on_page_done is not None:
            # Lỗi lắp record/dò là lỗi THẬT của trang — bọc PagePostOcrError
            # truyền ra ngoài (wrapper re-raise, KHÔNG fallback tuần tự):
            # ngược lại sẽ tạo "submitted nhưng không có kết quả" rồi bị coi
            # là bỏ qua hợp lệ.
            try:
                if on_page_done(page_idx, page_result):
                    stopping = True
            except CoreScanCancelled:
                raise
            except Exception as e:
                raise PagePostOcrError(page_idx, str(e))

    return results, window


def _process_selected_pages_serial(input_path, doc_in, page_indices, total_pages, log,
                                   dpi: int | None = None,
                                   render_annots: bool = True):
    """Serial OCR for a selected page subset used by page-wise DOCX export.

    ``dpi``/``render_annots`` phải được caller truyền đúng khi fallback để
    kết quả tuần tự khớp kết quả song song (vd quét mật dùng 200 DPI)."""
    from PIL import Image

    ocr = _get_ocr()
    if dpi is None:
        dpi = OCR_DPI
    results = {}
    page_indices = sorted({int(idx) for idx in page_indices if 0 <= int(idx) < total_pages})

    for page_idx in page_indices:
        page = doc_in[page_idx]
        page_w = page.rect.width
        page_h = page.rect.height

        log(f"OCR page {page_idx + 1}/{total_pages}...", "debug")

        import fitz
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=mat, annots=bool(render_annots))
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

        scale_x = page_w / pix.width
        scale_y = page_h / pix.height

        with _ocr_lock:
            result = ocr.perform_ocr(img)

        ocr_lines = result.get("lines", [])
        log(f"  Page {page_idx + 1}: {len(ocr_lines)} lines detected", "debug")

        lines_data, words_data = _ocr_result_to_page_data(
            page_idx, ocr_lines, scale_x, scale_y
        )

        results[page_idx] = {
            "lines_data": lines_data,
            "words_data": words_data,
            "render_width": pix.width,
            "render_height": pix.height,
        }

    return results


def _process_selected_pages_parallel(input_path, page_indices, total_pages, log,
                                     page_timeout: float = 120.0, dpi=None, *,
                                     max_in_flight: int | None = None,
                                     on_page_done=None,
                                     stop_check=None,
                                     cancel_check=None):
    """Submit only OCR-required pages to the shared page-level pool.

    ``dpi`` overrides the worker default (None → OCR_DPI 240).
    Lỗi OCR trang là sentinel `{"ocr_error": …}` — validator phía caller quyết.
    Hủy (`CoreScanCancelled`) truyền nguyên trạng; chỉ lỗi hạ tầng mới
    shutdown + fallback tuần tự, và fallback giữ đúng ``dpi``."""
    import time

    t0 = time.perf_counter()
    page_indices = sorted({int(idx) for idx in page_indices if 0 <= int(idx) < total_pages})

    try:
        results, max_in_flight = _collect_page_ocr_bounded(
            input_path, page_indices, total_pages, log, page_timeout, dpi=dpi,
            max_in_flight=max_in_flight,
            on_page_done=on_page_done,
            stop_check=stop_check,
            cancel_check=cancel_check,
        )

        dt = time.perf_counter() - t0
        log(
            f"Parallel OCR completed: {len(page_indices)} selected page(s) in {dt:.1f}s "
            f"({_NUM_PAGE_WORKERS} workers, max {max_in_flight} queued, "
            f"{dt/max(len(page_indices),1):.2f}s/page avg)",
            "info",
        )
        return results

    except (CoreScanCancelled, PagePostOcrError):
        raise
    except Exception as e:
        if isinstance(e, RuntimeError) and "initialization failed" in str(e):
            raise
        log(f"Parallel OCR failed, falling back to serial: {e}", "error")
        shutdown_pool()
        import fitz
        doc_in = fitz.open(input_path)
        try:
            return _process_selected_pages_serial(
                input_path, doc_in, page_indices, total_pages, log,
                dpi=dpi, render_annots=True,
            )
        finally:
            doc_in.close()


def _process_pages_parallel(input_path, total_pages, log,
                              page_timeout: float = 120.0, dpi=None, *,
                              max_in_flight: int | None = None,
                              on_page_done=None,
                              stop_check=None,
                              cancel_check=None):
    """Submit pages to the page-level pool with bounded in-flight work.

    ``dpi`` overrides the worker default (None → OCR_DPI 240).
    Hủy truyền nguyên trạng; fallback tuần tự giữ đúng ``dpi``."""
    import time

    t0 = time.perf_counter()

    try:
        results, max_in_flight = _collect_page_ocr_bounded(
            input_path, range(total_pages), total_pages, log, page_timeout, dpi=dpi,
            max_in_flight=max_in_flight,
            on_page_done=on_page_done,
            stop_check=stop_check,
            cancel_check=cancel_check,
        )

        dt = time.perf_counter() - t0
        log(f"Parallel OCR completed: {total_pages} pages in {dt:.1f}s "
            f"({_NUM_PAGE_WORKERS} workers, max {max_in_flight} queued, "
            f"{dt/max(total_pages,1):.2f}s/page avg)",
            "info")
        return results

    except (CoreScanCancelled, PagePostOcrError):
        raise
    except Exception as e:
        if isinstance(e, RuntimeError) and "initialization failed" in str(e):
            raise
        log(f"Parallel OCR failed, falling back to serial: {e}", "error")
        shutdown_pool()
        import fitz
        doc_in = fitz.open(input_path)
        result = _process_pages_serial(input_path, doc_in, total_pages, log)
        doc_in.close()
        return result


def rebuild_pdf_with_text(original_input_path, output_path, ocr_json_path,
                          replacements=None, log_callback=None):
    """
    Rebuild PDF with corrected text using stored OCR positions.
    Applies word-level replacements, then re-renders at line level.
    """
    def log(msg, level="info"):
        if log_callback:
            try:
                log_callback(msg, level)
            except:
                pass

    try:
        ocr_data = load_canonical(ocr_json_path)

        font_path = _find_unicode_font()
        if not font_path:
            return False, "No Unicode font found"

        # Find the source PDF for page backgrounds
        # Prefer the original input (no text) over _ocr.pdf (has text overlay)
        input_pdf = original_input_path
        if not os.path.exists(input_pdf):
            stored = (
                ocr_data.get("input_path", "")
                or ocr_data.get("document", {}).get("source_path", "")
            )
            if os.path.exists(stored):
                input_pdf = stored
            else:
                return False, f"Original input PDF not found: {input_pdf}"

        # Detect if source PDF already has text (i.e., it's _ocr.pdf not original)
        # If so, we must render pages as images to avoid copying old text layer
        import fitz  # PyMuPDF - lazy import

        doc_in = fitz.open(input_pdf)
        source_has_text = len(doc_in[0].get_text().strip()) > 0

        replacements = replacements or {}
        doc_out = fitz.open()
        pending_corrected_text = []
        background_specs = []
        total_replaced = 0

        for page_idx, page_data in enumerate(ocr_data["pages"]):
            if page_idx >= len(doc_in):
                break

            page_w = page_data["width"]
            page_h = page_data["height"]

            # Bake rotation when copying from raw source and the canonical
            # JSON says coords were normalized from a bottom-left OCR
            # (source page is visually upside-down).
            need_bake = (
                not source_has_text
                and page_data.get("coord_origin_source") == "normalized_from_bottom_left"
            )
            bake_angle = 180 if need_bake else 0

            if source_has_text:
                new_page = doc_out.new_page(width=page_w, height=page_h)
                # Source is _ocr.pdf (already has text) - render to image to strip text
                pix = doc_in[page_idx].get_pixmap(dpi=200, annots=True)
                new_page.insert_image(new_page.rect, pixmap=pix)
            else:
                background_specs.append({
                    "page_idx": page_idx,
                    "page_w": page_w,
                    "page_h": page_h,
                    "bake_angle": bake_angle,
                })

            # Apply word-level replacements
            corrected_words = []
            for wd in page_data.get("words", []):
                text = wd["text"]
                new_text = replacements.get(text, text)
                if new_text != text:
                    total_replaced += 1
                corrected_words.append({**wd, "text": new_text})

            # Handle multi-word phrase replacements
            for old_phrase, new_phrase in replacements.items():
                if " " not in old_phrase:
                    continue
                old_words = old_phrase.split()
                new_words = new_phrase.split()
                n = len(old_words)
                i = 0
                while i <= len(corrected_words) - n:
                    if all(corrected_words[i + j]["text"] == old_words[j] for j in range(n)):
                        if len(new_words) == n:
                            for j in range(n):
                                corrected_words[i + j]["text"] = new_words[j]
                                total_replaced += 1
                        i += n
                    else:
                        i += 1

            # Rebuild lines from corrected words using original line structure
            corrected_lines = _rebuild_lines_from_words(
                page_data.get("lines", []), corrected_words
            )
            pending_corrected_text.append((corrected_words, corrected_lines))

        if not source_has_text:
            _append_backgrounds_preserving_inline_images(doc_out, doc_in, background_specs)

        for page_idx, (corrected_words, corrected_lines) in enumerate(
            pending_corrected_text
        ):
            if page_idx >= len(doc_out):
                break
            _build_text_page_words(
                doc_out[page_idx],
                corrected_words,
                font_path,
                fallback_lines=corrected_lines,
            )

        doc_out.save(output_path, deflate=True, garbage=4)
        doc_out.close()
        doc_in.close()

        log(f"Rebuilt PDF with {total_replaced} corrections", "debug")
        return True, f"Rebuilt with {total_replaced} corrections"

    except Exception as e:
        import traceback
        traceback.print_exc()
        return False, str(e)


def _rebuild_lines_from_words(original_lines, corrected_words):
    """
    Rebuild line-level text from corrected words, preserving original
    line positions and font sizes.
    """
    if not original_lines or not corrected_words:
        return original_lines or []

    result_lines = []
    word_idx = 0

    for ln in original_lines:
        # Count how many words belong to this line
        # (by matching original line text against sequential words)
        orig_text = ln["text"]
        orig_word_list = orig_text.split()
        n_words = len(orig_word_list)

        # Take next n_words from corrected_words
        if word_idx + n_words <= len(corrected_words):
            line_words = corrected_words[word_idx:word_idx + n_words]
            new_text = _line_text_from_words(line_words)
            word_idx += n_words
        else:
            # Fallback: use remaining words
            remaining = corrected_words[word_idx:]
            new_text = _line_text_from_words(remaining) if remaining else orig_text
            word_idx = len(corrected_words)

        x, y, w, h = _line_xywh(ln)
        result_lines.append({
            "text": new_text,
            "x": x,
            "y": y,
            "w": w,
            "h": h,
            "font_size": ln.get("font_size", max(h * 0.78, 4.0)),
        })

    return result_lines
