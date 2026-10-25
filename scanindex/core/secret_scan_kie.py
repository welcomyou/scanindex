# -*- coding: utf-8 -*-
"""Coordinator KIE quét mật — GĐ K (PLAN KIE 21/10; PLAN v5.1 §4.3-SP/KCAL).

File-worker bàn giao job bền vững (canonical đã ghi đĩa + lease input) rồi
THẢ file-worker NGAY; Pkie process sống lâu có session riêng phục vụ hàng đợi
có giới hạn. Hợp đồng budget (4.3-SP):
- Execution credit: tối đa `2 × Pkie` job CHƯA terminal — ĐẾM CẢ QUEUED
  (P1-09: trước đây chỉ đếm job đã dispatch nên kho lớn vẫn dồn hàng đợi vô
  hạn). Vượt → TỪ CHỐI nhận (caller giữ đường đồng bộ) — không xóa đầu vào
  retry, không mở backlog vô hạn.
- Byte budget: tổng byte canonical của các job chưa terminal ≤ budget
  (mặc định 512 MiB, đếm byte thật). Vượt → TỪ CHỐI nhận.
- Retry: lỗi tạm thời retry ĐÚNG 1 lần (attempt=2), hết → terminal error;
  lỗi model_missing không retry. Executor chết thật (BrokenProcessPool) →
  dựng executor mới CÓ GIỚI HẾN (P1-06); hết lượt dựng → ngừng nhận mới,
  caller giữ đường đồng bộ — không nộp mãi vào executor hỏng.
- Lease: canonical được `acquire_input_lease` TRƯỚC khi publish job (P1-06:
  không còn cửa sổ submit/cancel làm rò lease); coordinator sở hữu lease theo
  refcount và nhả ĐÚNG MỘT LẦN khi terminal — kể cả dispatch-fail/cancel.
  Cancel khi future ĐANG CHẠY không chốt terminal và KHÔNG nhả lease — giữ
  tới khi future thật kết thúc (P1-05: caller outcome không phải bằng chứng
  native dừng).
- Applier (callback) chạy NGOÀI condition lock (P1-10): `wait_all` luôn đánh
  giá được deadline, callback chậm không giữ khóa; job chỉ terminal SAU khi
  callback chạy xong → "drain True = metadata đã áp xong" giữ nguyên, còn
  "drain False (timeout)" nghĩa là metadata CHƯA đủ — caller phải không
  cache-final (xử lý phía scanner).
Không import Qt/screen — chạy trong core; callback do caller cung cấp.
"""
from __future__ import annotations

import atexit
import os
import threading
from concurrent.futures import ThreadPoolExecutor

_lock = threading.Lock()
_coordinator = None

# Số lần được dựng lại executor sau khi chết thật (BrokenProcessPool) trước
# khi ngừng nhận job mới (fail-safe cho coordinator dùng chung theo app).
_MAX_EXECUTOR_REBUILDS = 2


def _shutdown_at_exit() -> None:
    """Đóng app: không chờ vô hạn — hủy nhận việc, cancel futures."""
    shutdown_all(wait=False)


atexit.register(_shutdown_at_exit)


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def _default_processes() -> int:
    """Pkie khởi điểm theo plan §3: 2 trên máy ≥16 CPU logic, 1 máy nhỏ hơn.
    Override `OCRTOOL_KIE_PROCESSES` (benchmark có chủ đích, phải log)."""
    raw = os.environ.get("OCRTOOL_KIE_PROCESSES", "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return 2 if (os.cpu_count() or 4) >= 16 else 1


def _byte_budget() -> int:
    raw = os.environ.get("OCRTOOL_KIE_PAYLOAD_BUDGET_MB", "").strip()
    mb = _env_int("OCRTOOL_KIE_PAYLOAD_BUDGET_MB", 512) if not raw else _env_int(
        "OCRTOOL_KIE_PAYLOAD_BUDGET_MB", 512
    )
    return mb * 1024 * 1024


def _is_pool_broken(exc: BaseException) -> bool:
    """Executor pool chết THẬT (process worker chết — không phải dict lỗi
    do job tự trả). Chỉ lớp exception này mới kích hoạt dựng executor mới."""
    name = type(exc).__name__
    if name in {"BrokenProcessPool", "BrokenThreadPool"}:
        return True
    try:
        from concurrent.futures.process import BrokenProcessPool

        return isinstance(exc, BrokenProcessPool)
    except Exception:
        return False


class _Job:
    __slots__ = ("payload", "canonical_path", "byte_size", "attempt", "futures",
                 "phase", "result", "callbacks", "error", "lease_refs", "key",
                 "cancel_result")

    PH_QUEUED = "queued"        # đã nhận, chờ credit dispatch
    PH_RUNNING = "running"      # future đang chạy trên executor
    PH_COMPLETING = "completing"  # native xong — applier (callback) đang chạy
    PH_TERMINAL = "terminal"    # chốt hẳn (đã áp callback xong)

    def __init__(self, key: tuple, payload: dict, canonical_path: str,
                 byte_size: int, callbacks: list):
        self.key = key
        self.payload = payload
        self.canonical_path = canonical_path
        self.byte_size = byte_size
        self.attempt = 1
        self.futures: list = []
        self.phase = self.PH_QUEUED
        self.result: dict | None = None
        self.callbacks = callbacks  # các on_done cùng xem một future (dedup)
        self.error: str | None = None
        # Lease do coordinator sở hữu THEO REFCOUNT (P1-06: mỗi đường submit
        # — kể cả dedup — tự acquire 1 ref trước khi publish; terminal nhả
        # đủ refs đúng một lần). Không còn cửa sổ "publish xong mới acquire".
        self.lease_refs = 0
        # P1-05: hủy khi future đang chạy KHÔNG được chốt ngay — ghi kết quả
        # cancelled muốn báo; khi future thật kết thúc, `_on_future_done` chốt
        # bằng dict này (lease giữ tớiNative terminal thật).
        self.cancel_result: dict | None = None


class KieCoordinator:
    """Hàng đợi KIE có giới hạn + pool process session riêng + retry/cancel.

    `executor_factory(max_workers)` inject được cho test (ThreadPool + job_fn
    giả); mặc định ProcessPoolExecutor spawn với initializer warmup session.
    """

    def __init__(self, processes: int | None = None,
                 executor_factory=None, job_fn=None):
        self.processes = processes or _default_processes()
        self.max_outstanding = 2 * self.processes  # execution credit
        self.byte_budget = _byte_budget()
        self._job_fn = job_fn  # None = dùng kie.worker.run_job thật
        self._executor_factory = executor_factory
        self._executor = None
        self._owned_processes: dict = {}
        self._cond = threading.Condition()
        self._jobs: dict[tuple, _Job] = {}  # key dedup → job CHƯA terminal
        self._queued: list[tuple] = []      # key chờ credit dispatch
        self._shutting_down = False
        self._executor_rebuilds = 0
        self._executor_dead = False
        self._last_error = None
        self._unavailable_runs: dict[str, str] = {}
        # R2-03: số delivery (bundle settle — dispatch-fail/cancel/executor-
        # dead) đã POP khỏi `_jobs` nhưng callback CHƯA chạy xong. `wait_all`
        # phải chờ cả bộ đếm này về 0 — job đã pop không còn giữ drain, nên
        # không có counter thì drain True sớm dù applier chưa hoàn tất.
        self._deliveries_inflight = 0

    # -- nội bộ ------------------------------------------------------------

    def _ensure_executor(self):
        from scanindex.core.runtime.shutdown import accepting_work
        if not accepting_work():
            raise RuntimeError("Runtime is stopping")
        if self._executor is not None:
            return self._executor
        if self._executor_factory is not None:
            self._executor = self._executor_factory(self.processes)
        else:
            import multiprocessing
            from concurrent.futures import ProcessPoolExecutor
            from scanindex.core.kie.worker import warmup_worker
            from scanindex.infra.windows_job import ensure_app_job
            ensure_app_job()

            ctx = multiprocessing.get_context("spawn")
            self._executor = ProcessPoolExecutor(
                max_workers=self.processes, mp_context=ctx,
                initializer=warmup_worker,
            )
        return self._executor

    def _note_executor_broken_locked(self) -> None:
        """Executor chết thật (P1-06): hủy tham chiếu + đóng cũ, đếm lần dựng
        lại. Vượt `_MAX_EXECUTOR_REBUILDS` → ngừng nhận job mới (submit từ
        chối, caller giữ đường đồng bộ) — không nộp mãi vào executor hỏng.
        Gọi giữ `_cond`."""
        ex = self._executor
        self._executor = None
        self._executor_rebuilds += 1
        if self._executor_rebuilds >= _MAX_EXECUTOR_REBUILDS:
            self._executor_dead = True
        if ex is not None:
            self._remember_executor(ex)
            try:
                ex.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
        import logging

        logging.getLogger(__name__).error(
            "KIE executor BrokenProcessPool (lần %d/%d) — dựng lại executor "
            "mới cho retry; %s",
            self._executor_rebuilds, _MAX_EXECUTOR_REBUILDS,
            "NGỪNG nhận job mới" if self._executor_dead else "vẫn nhận",
        )

    def _job_fn_resolved(self):
        if self._job_fn is not None:
            return self._job_fn
        from scanindex.core.kie.worker import run_job

        return run_job

    @staticmethod
    def _key(payload: dict) -> tuple:
        return (
            os.path.normcase(os.path.abspath(str(payload.get("canonical_path", "")))),
            tuple(sorted(int(o) for o in payload.get("selected_ordinals") or [])),
            str(payload.get("model_fp", "")),
        )

    def _outstanding_bytes_locked(self) -> int:
        return sum(j.byte_size for j in self._jobs.values())

    def _count_phases_locked(self) -> tuple[int, int, int]:
        queued = running = completing = 0
        for j in self._jobs.values():
            if j.phase == _Job.PH_QUEUED:
                queued += 1
            elif j.phase == _Job.PH_RUNNING:
                running += 1
            elif j.phase == _Job.PH_COMPLETING:
                completing += 1
        return queued, running, completing

    def _dispatch_locked(self) -> tuple[list, list]:
        """Đẩy job từ hàng đợi vào executor khi còn credit. Gọi giữ `_cond`.
        Credit đếm job CHƯA terminal (queued+running+completing, P1-09) nên
        hàng đợi nội bộ không thể vượt credit — tổng job đã nhận bị chặn tại
        `submit`.

        Trả `(failed, to_watch)`: failed = (job, callbacks, result) settle TẠI
        CHỖ do dispatch-fail — caller chạy callback + nhả lease SAU khi nhả
        lock (P1-06: trước đây nhánh này không nhả lease → rò vĩnh viễn).
        to_watch = (job, future) chưa đăng ký done-callback — BẮT BUỘC đăng
        ký SAU KHI NHẢ lock: future hoàn thành cực nhanh sẽ chạy
        `_on_future_done` đồng bộ ngay trong `add_done_callback`; đăng ký
        trong lock nghĩa là callback/applier chạy dưới lock (P1-10)."""
        failed: list = []
        to_watch: list = []
        if self._shutting_down:
            return failed, to_watch
        while self._queued:
            key = self._queued[0]
            job = self._jobs.get(key)
            if job is None or job.phase == _Job.PH_TERMINAL:
                self._queued.pop(0)
                continue
            if self._executor_dead:
                # Executor đã chết vượt ngưỡng — không tạo mới: settle lỗi
                # có nghĩa vụ retry (metadata_error) + nhả lease ở caller.
                self._queued.pop(0)
                result = {
                    "status": "error", "error_code": "executor_dead",
                    "error": "KIE executor hỏng lặp lại — job không chạy",
                    "fields_by_source": {},
                    "generation": job.payload.get("generation"),
                    "job_id": job.payload.get("job_id"),
                }
                self._last_error = result["error"]
                failed.append((job, self._settle_locked(job, result), result))
                continue
            queued, running, completing = self._count_phases_locked()
            if running + completing >= self.max_outstanding:
                return failed, to_watch  # hết credit — chờ job terminal
            self._queued.pop(0)
            job_fn = self._job_fn_resolved()
            payload = dict(job.payload)
            payload["attempt"] = job.attempt
            try:
                ex = self._ensure_executor()
                fut = ex.submit(job_fn, payload)
                self._remember_executor(ex)
            except Exception as exc:
                self._last_error = str(exc)
                if job.attempt == 1:
                    # Dispatch fail coi như lỗi tạm thời: retry ĐÚNG 1 lần
                    # (executor mới sẽ được `_ensure_executor` dựng nếu cũ
                    # đã chết). Hết lượt → settle dispatch_failed.
                    job.attempt = 2
                    self._queued.append(key)
                    continue
                result = {
                    "status": "error", "error_code": "dispatch_failed",
                    "error": str(exc), "fields_by_source": {},
                    "generation": job.payload.get("generation"),
                    "job_id": job.payload.get("job_id"),
                }
                failed.append((job, self._settle_locked(job, result), result))
                continue
            job.futures.append(fut)
            job.phase = _Job.PH_RUNNING
            to_watch.append((job, fut))
        return failed, to_watch

    @staticmethod
    def _last_result_for(job: _Job, error_code: str) -> dict:
        return {
            "status": "error", "error_code": error_code,
            "fields_by_source": {},
            "generation": job.payload.get("generation"),
            "job_id": job.payload.get("job_id"),
        }

    def _settle_locked(self, job: _Job, result: dict) -> list:
        """Chốt terminal + NHẢ entry ngay (P1-09: không giữ entry terminal
        tới khi mọi job xong — RAM không tăng theo kho). Gọi giữ `_cond`;
        trả danh sách callback để caller chạy SAU khi nhả lock.

        R3-04: ownership job → delivery CHUYỂN TRONG CÙNG critical section
        với pop — `_deliveries_inflight` tăng tại đây, giảm ĐÚNG MỘT LẦN khi
        delivery chạy xong callback. Trước đây counter tăng ở `_deliver_bundles`
        (sau khi nhả lock): waiter chạy giữa pop và lần tăng vẫn thấy
        jobs=0 + deliveries=0 → drain True sớm dù applier chưa chạy."""
        job.phase = _Job.PH_TERMINAL
        job.result = result
        self._jobs.pop(job.key, None)
        self._deliveries_inflight += 1
        cbs = list(job.callbacks)
        job.callbacks = []
        self._cond.notify_all()
        return cbs

    def _run_callbacks(self, cbs: list, result: dict) -> None:
        for cb in cbs:
            try:
                cb(result)
            except Exception:
                import logging

                logging.getLogger(__name__).exception(
                    "KIE on_done callback failed (job %s)", result.get("job_id")
                )

    def _deliver_bundles(self, bundles: list) -> None:
        """Chạy callback + nhả lease cho các bundle ĐÃ POP khỏi `_jobs`
        (settle tại chỗ: dispatch-fail/executor-dead/cancel).

        R2-03/R3-04: counter đã tăng tại `_settle_locked` (cùng critical
        section với pop) — hàm này chỉ THỰC THI và giảm đúng một lần/bundle
        trong finally; giữa pop và thực thi, `wait_all` vẫn thấy delivery
        đang nợ ⇒ không trả True sớm."""
        for fjob, fcbs, fresult in bundles:
            try:
                self._run_callbacks(fcbs, fresult)
                self._release_lease(fjob)
            finally:
                with self._cond:
                    self._deliveries_inflight -= 1
                    self._cond.notify_all()

    def _run_counted_callbacks(self, cbs: list, result: dict) -> None:
        """Chạy callback của bundle ĐÃ settle (counter đã tăng tại
        `_settle_locked`) — chỉ giảm counter khi callback chạy xong.

        CHỈ dùng cho callback của bundle đã pop (settle-trực-tiếp). Callback
        "dedup đến muộn" của job còn trong `_jobs` (PH_COMPLETING) phải đi
        `_run_callbacks` thường — job còn giữ drain qua `_jobs`."""
        try:
            self._run_callbacks(cbs, result)
        finally:
            with self._cond:
                self._deliveries_inflight -= 1
                self._cond.notify_all()

    def _release_lease(self, job: _Job) -> None:
        """Nhả lease canonical theo ĐỦ refcount ĐÚNG MỘT LẦN khi job terminal
        — coordinator sở hữu lease (mỗi đường submit acquire 1 ref), callback
        caller không phải lo. Idempotent."""
        refs, job.lease_refs = job.lease_refs, 0
        for _ in range(refs):
            try:
                from scanindex.core.ocr.direct_engine import release_input_lease

                release_input_lease(job.canonical_path)
            except Exception:
                import logging

                logging.getLogger(__name__).exception(
                    "release input lease failed (%s)", job.canonical_path
                )

    def _attach_watchers(self, to_watch: list) -> None:
        """Đăng ký done-callback NGOÀI lock (P1-10): future hoàn thành nhanh
        chạy `_on_future_done` đồng bộ trong add_done_callback — đăng ký dưới
        lock thì applier/callback chạy dưới lock."""
        for wjob, wfut in to_watch:
            wfut.add_done_callback(
                lambda f, j=wjob: self._on_future_done(j, f)
            )

    def _run_failure_bundles(self, failed: list) -> None:
        """Chạy callback + nhả lease cho các bundle settle TẠI CHỖ (dispatch/
        executor-dead). P1-C: KHÔNG được bỏ bundle — callback mất làm scanner
        không biết vòng đời file dù drain vẫn True."""
        self._deliver_bundles(failed)

    def _on_future_done(self, job: _Job, fut) -> None:
        """Future kết thúc: retry/settle/completing. Callback (applier) chạy
        NGOÀI `_cond` (P1-10); job chỉ PH_TERMINAL sau khi TẤT CẢ người nhận
        (kể cả dedup đến muộn) đã nhận applier — `wait_all` True đồng nghĩa
        metadata đã áp đủ (P1-C)."""
        try:
            result = fut.result()
        except Exception as exc:
            result = {
                "status": "error", "error_code": type(exc).__name__,
                "error": str(exc), "fields_by_source": {},
                "generation": job.payload.get("generation"),
                "job_id": job.payload.get("job_id"),
            }
            pool_broken = _is_pool_broken(exc)
        else:
            pool_broken = False
        completing_cbs: list = []
        deliver_result = result
        retry_watch: list = []
        retry_failed: list = []
        retrying = False
        settled_directly = False
        with self._cond:
            if job.phase in (_Job.PH_TERMINAL,):
                return
            if result.get("error_code") == "model_missing":
                # A permanent availability failure closes this producer, not
                # every later scan after the operator repairs the model.
                self._unavailable_runs[str(job.payload.get("run_id") or "")] = "model_missing"
                if len(self._unavailable_runs) > 256:
                    self._unavailable_runs.pop(next(iter(self._unavailable_runs)))
                self._cond.notify_all()
            if job.cancel_result is not None:
                # P1-B: ý định hủy được đánh dấu TRƯỚC mọi `fut.cancel()` —
                # CancelledError (kể cả callback đồng bộ từ cancel) KHÔNG được
                # retry ngược ý định hủy; chốt cancelled ngay tại đây.
                deliver_result = job.cancel_result
                completing_cbs = self._settle_locked(job, deliver_result)
                settled_directly = True
            elif (result.get("status") == "error"
                    and job.attempt == 1
                    and result.get("error_code") not in {"model_missing"}):
                # Retry ĐÚNG 1 lần cho lỗi tạm thời. Executor chết thật thì
                # dựng lại CÓ GIỚI HẾN trước khi retry (P1-06).
                if pool_broken:
                    self._note_executor_broken_locked()
                    if self._executor_dead:
                        completing_cbs = self._settle_locked(job, result)
                        settled_directly = True
                    else:
                        job.futures = []
                        job.attempt = 2
                        job.phase = _Job.PH_QUEUED
                        self._queued.append(job.key)
                        retry_failed, retry_watch = self._dispatch_locked()
                        retrying = True
                else:
                    job.futures = []
                    job.attempt = 2
                    job.phase = _Job.PH_QUEUED
                    self._queued.append(job.key)
                    retry_failed, retry_watch = self._dispatch_locked()
                    retrying = True
            else:
                if pool_broken:
                    self._note_executor_broken_locked()
                # PH_COMPLETING: native xong — applier sắp chạy NGOÀI lock.
                job.result = result
                job.phase = _Job.PH_COMPLETING
                completing_cbs = list(job.callbacks)
                job.callbacks = []
        # Done-callback của future retry ĐĂNG KÝ NGOÀI lock (P1-10).
        self._attach_watchers(retry_watch)
        # P1-C: bundle dispatch-fail của LƯỜI RETRY phải đi cùng đường delivery
        # — trước đây bị bỏ (`_, retry_dispatched = ...`) → callback mất,
        # drain vẫn True.
        self._run_failure_bundles(retry_failed)
        if retrying:
            return
        # -- NGOÀI `_cond` (P1-10): applier chạy theo LÔ; chỉ chốt terminal
        # khi không còn người nhận nào (kể cả dedup đến muộn khi completing —
        # P1-C: pop/notify TRƯỚC khi chạy late là race "drain True ≠ đủ ACK").
        # R2-03/R3-04: lô đầu của nhánh settle-trực-tiếp (cancel/hết-lượt) là
        # delivery của bundle ĐÃ POP — counter đã tăng tại `_settle_locked`,
        # chạy qua `_run_counted_callbacks` để giảm đúng một lần. Lô "late"
        # (dedup đến muộn) thuộc job CÒN trong `_jobs` — drain giữ qua
        # `_jobs`, chạy `_run_callbacks` thường (không đụng counter).
        if settled_directly:
            self._run_counted_callbacks(completing_cbs, deliver_result)
        else:
            self._run_callbacks(completing_cbs, deliver_result)
        while True:
            with self._cond:
                if settled_directly or job.phase == _Job.PH_TERMINAL:
                    # Settle ngay từ đầu (cancel/hết lượt): job đã pop, không
                    # thể có người nhận mới — chỉ còn nợ dispatch phần còn lại.
                    failed, to_watch = self._dispatch_locked()
                    break
                late = job.callbacks
                job.callbacks = []
                if not late:
                    job.phase = _Job.PH_TERMINAL
                    self._jobs.pop(job.key, None)
                    self._cond.notify_all()
                    failed, to_watch = self._dispatch_locked()
                    break
            self._run_callbacks(late, deliver_result)
        self._attach_watchers(to_watch)
        self._run_failure_bundles(failed)
        self._release_lease(job)

    # -- API ----------------------------------------------------------------

    def submit(self, payload: dict, on_done) -> tuple[bool, str | None]:
        """Nhận job. Trả (True, None) nếu nhận; (False, lý do) nếu từ chối —
        caller GIỮ đường đồng bộ (không mất metadata, không backlog vô hạn).

        Giao dịch nhận job (P1-06): acquire lease TRƯỚC khi publish; mọi nhánh
        từ chối/exception nhả lại lease; dedup cộng refcount vào job đang
        chạy (không acquire thêm ngoài job)."""
        from scanindex.core.ocr.direct_engine import acquire_input_lease

        canonical_path = payload.get("canonical_path") or ""
        try:
            byte_size = os.path.getsize(canonical_path)
        except OSError:
            byte_size = 0
        key = self._key(payload)
        acquire_input_lease(canonical_path)
        published = False
        accepted = True
        refusal: str | None = None
        settled: list = []
        to_watch: list = []
        try:
            with self._cond:
                if self._shutting_down:
                    accepted, refusal = False, "coordinator đang tắt"
                elif str(payload.get("run_id") or "") in self._unavailable_runs:
                    accepted, refusal = False, "model_missing: ngừng nhận KIE của lượt này"
                else:
                    existing = self._jobs.get(key)
                    if existing is not None:
                        # dedup: một future, nhiều người nhận — job giữ thêm
                        # 1 ref lease của lượt submit này.
                        existing.lease_refs += 1
                        existing.callbacks.append(on_done)
                        published = True  # ref chuyển ownership cho job
                    elif self._executor_dead:
                        accepted, refusal = False, (
                            "KIE executor hỏng lặp lại — giữ đường đồng bộ"
                        )
                    else:
                        # P1-09: credit đếm TẤT CẢ job chưa terminal (queued
                        # + running + completing) — hàng đợi không tăng theo
                        # kho.
                        outstanding = len(self._jobs)
                        if outstanding >= self.max_outstanding:
                            accepted, refusal = False, (
                                f"execution credit đầy ({outstanding}/"
                                f"{self.max_outstanding}) — giữ đường đồng bộ"
                            )
                        elif byte_size > self.byte_budget:
                            accepted, refusal = False, (
                                f"payload {byte_size} vượt budget "
                                f"{self.byte_budget}"
                            )
                        elif (self._outstanding_bytes_locked() + byte_size) \
                                > self.byte_budget:
                            accepted, refusal = False, (
                                "byte budget đầy — giữ đường đồng bộ"
                            )
                        else:
                            job = _Job(key, dict(payload), canonical_path,
                                       byte_size, [on_done])
                            job.lease_refs = 1
                            self._jobs[key] = job
                            self._queued.append(key)
                            published = True  # job sở hữu ref — terminal tự nhả
                            settled, to_watch = self._dispatch_locked()
        except BaseException:
            if not published:
                # Chưa publish — ref này không ai sở hữu: nhả ngay.
                self._release_submit_lease(canonical_path)
            raise
        if not accepted:
            self._release_submit_lease(canonical_path)
            return False, refusal
        # Đăng ký done-callback NGOÀI lock (P1-10): future hoàn thành nhanh
        # chạy `_on_future_done` đồng bộ trong add_done_callback — nếu còn
        # giữ lock thì applier/callback chạy dưới lock.
        for wjob, wfut in to_watch:
            wfut.add_done_callback(
                lambda f, j=wjob: self._on_future_done(j, f)
            )
        self._deliver_bundles(settled)
        return True, None

    @staticmethod
    def _release_submit_lease(canonical_path: str) -> None:
        try:
            from scanindex.core.ocr.direct_engine import release_input_lease

            release_input_lease(canonical_path)
        except Exception:
            import logging

            logging.getLogger(__name__).exception(
                "release input lease failed (%s)", canonical_path
            )

    def wait_all(self, timeout: float | None = None) -> bool:
        """Chờ MỌI job đã nhận tới terminal (kể cả applier chạy xong).
        True = hết pending + metadata đã áp. Timeout được đánh giá ĐÚNG kể cả
        callback chậm — applier chạy ngoài `_cond` (P1-10).

        R2-03: còn chờ `_deliveries_inflight` về 0 — bundle settle-trực-tiếp
        (dispatch-fail/cancel) đã pop job nhưng callback chưa chạy xong cũng
        giữ drain, trên mọi nhánh kể cả nhánh lỗi.

        `_shutting_down` KHÔNG cho trả về sớm: job native đang chạy phải được
        chờ kết thúc thật (P1-05 — outcome không phải bằng chứng native dừng);
        future bị cancel bởi shutdown sẽ tự settle qua done-callback."""
        import time as _t

        end = None if timeout is None else _t.monotonic() + timeout
        with self._cond:
            while True:
                if not self._jobs and self._deliveries_inflight <= 0:
                    return True
                remaining = None if end is None else end - _t.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._cond.wait(0.05 if remaining is None else min(0.05, remaining))

    def run_error(self, run_id: str) -> str | None:
        with self._cond:
            return self._unavailable_runs.get(run_id)

    def finish_run(self, run_id: str) -> None:
        with self._cond:
            self._unavailable_runs.pop(run_id, None)

    def _cancel_locked(self, predicate=None) -> list:
        """Hủy AN TOÀN (P1-05 + P1-B review 2). Ba pha:

        1. Giữ lock: ĐÁNH DẤU ý định hủy (`cancel_result`) cho job có future
           TRƯỚC khi đụng `fut.cancel()` — done-callback đồng bộ sinh ra từ
           `cancel()` sẽ thấy intent và chốt cancelled, KHÔNG retry ngược; job
           queued/không future → chốt cancelled ngay (native chắc không chạy).
        2. NGOÀI lock: gọi `fut.cancel()` — callback đồng bộ nó kích hoạt chạy
           khi `_cond` rảnh (không còn cửa sổ callback-dưới-lock).
        3. Giữ lock: job vẫn chưa terminal và TẤT CẢ future đã CANCELLED
           (native chắc chắn không chạy) → chốt + nhả lease ngay; future đang
           chạy thật → giữ intent, `_on_future_done` chốt khi native kết thúc.

        `predicate` None = hủy tất cả (cancel_all/shutdown); truyền run_id để
        hủy ĐÚNG một lượt. Trả `(settled, n_cancelled)`: settled là list
        (job, cbs, result) ĐỦ 3 phần tử — caller chạy callback + nhả lease
        SAU khi nhả lock; `n_cancelled` đếm mọi job chốt cancelled NGAY trong
        lời gọi này (kể cả job do callback đồng bộ của `fut.cancel()` ở pha 2
        chốt — P1-B)."""
        settled: list = []
        intent_jobs: list = []
        n_cancelled = 0
        with self._cond:
            if predicate is None:
                self._shutting_down = True
            for job in list(self._jobs.values()):
                if job.phase == _Job.PH_TERMINAL:
                    continue
                if predicate is not None \
                        and job.payload.get("run_id") != predicate:
                    continue
                if job.futures:
                    # Intent TRƯỚC cancel(): _on_future_done (kể cả chạy đồng
                    # bộ do fut.cancel()) nhìn thấy và chốt cancelled.
                    job.cancel_result = self._last_result_for(job, "cancelled")
                    job.cancel_result["error"] = "coordinator cancelled"
                    intent_jobs.append(job)
                    continue
                # R2-07: bundle ĐỦ 3 phần tử (job, callbacks, result) — result
                # tạo MỘT lần, dùng cho cả settle lẫn callback.
                cancelled_result = self._last_result_for(job, "cancelled")
                cancelled_result["error"] = "coordinator cancelled"
                settled.append(
                    (job, self._settle_locked(job, cancelled_result),
                     cancelled_result)
                )
                n_cancelled += 1
            self._cond.notify_all()
        # Pha 2 — NGOÀI lock (P1-B: cancel dưới lock = callback đồng bộ dưới
        # lock; cửa sổ reentrancy này phải đóng).
        for job in intent_jobs:
            for f in list(job.futures):
                try:
                    f.cancel()
                except Exception:
                    pass
        # Pha 3 — job mà mọi future đều CANCELLED (chưa kịp chạy) → chốt ngay.
        # (n_cancelled KHÔNG reset — đếm cộng dồn cả settle của pha 1.)
        with self._cond:
            for job in intent_jobs:
                if job.phase == _Job.PH_TERMINAL:
                    # Callback đồng bộ ở pha 2 đã chốt cancelled (intent đã
                    # đánh dấu → KHÔNG retry) — vẫn tính vào số hủy.
                    n_cancelled += 1
                    continue
                if job.futures and all(f.cancelled() for f in job.futures):
                    # R2-07: bundle đủ 3 phần tử như pha 1.
                    settled.append((job, self._settle_locked(
                        job, job.cancel_result
                    ), job.cancel_result))
                    n_cancelled += 1
                # ngược lại: future đang chạy thật → giữ intent + lease,
                # chờ `_on_future_done` khi native kết thúc.
            self._cond.notify_all()
        return settled, n_cancelled

    def _dispatch_after_release(self) -> None:
        """Sau khi hủy/terminal giải phóng credit: đẩy hàng đợi (caller gọi
        NGOÀI lock). cancel_all (đang tắt) không cần đường này."""
        failed: list = []
        to_watch: list = []
        with self._cond:
            failed, to_watch = self._dispatch_locked()
        self._attach_watchers(to_watch)
        self._run_failure_bundles(failed)

    def cancel_all(self) -> None:
        """Hủy TOÀN BỘ (chỉ dùng khi tắt coordinator — không dùng để hủy một
        lượt quét): ngừng nhận; job chưa chạy chốt cancelled; job đang chạy
        giữ lease tới khi future thật kết thúc."""
        settled, _n = self._cancel_locked(predicate=None)
        self._deliver_bundles(settled)

    def cancel_run(self, run_id: str) -> int:
        """Hủy CÁC JOB THUỘC MỘT LƯỢT quét (theo `payload["run_id"]`) — luồng
        hủy có ownership theo lượt (P2-3): không đụng job của lượt khác, không
        ngừng nhận job mới. Trả số job chốt cancelled NGAY (job đang chạy sẽ
        chốt muộn hơn qua `_on_future_done`)."""
        if not run_id:
            return 0
        settled, n_cancelled = self._cancel_locked(predicate=run_id)
        self._deliver_bundles(settled)
        # credit vừa giải phóng → đẩy job lượt khác đang chờ hàng đợi.
        self._dispatch_after_release()
        return n_cancelled

    def shutdown(self, wait: bool = False) -> None:
        with self._cond:
            self._shutting_down = True
            self._cond.notify_all()
        ex = self._executor
        if ex is not None:
            self._remember_executor(ex)
            try:
                if wait:
                    ex.shutdown(wait=True)
                else:
                    ex.shutdown(wait=False, cancel_futures=True)
            except Exception:
                import logging

                logging.getLogger(__name__).exception(
                    "KIE executor shutdown lỗi — future đang chạy sẽ được "
                    "settle qua done-callback khi kết thúc"
                )

    def _remember_executor(self, ex) -> None:
        from scanindex.core.runtime.shutdown import track_executor
        for proc in track_executor(ex):
            self._owned_processes[id(proc)] = proc

    def hard_stop(self) -> bool:
        """D1: terminate NGAY process worker KIE (native treo không phản ứng
        shutdown hợp tác). CHỈ kill process pool MÌNH sở hữu qua handle —
        không đụng process người dùng khác hay process cùng tên."""
        ex = self._executor
        self._remember_executor(ex)
        from scanindex.core.runtime.shutdown import terminate_processes
        return terminate_processes(list(self._owned_processes.values()))

    def telemetry(self) -> dict:
        with self._cond:
            queued, running, completing = self._count_phases_locked()
            return {
                "queued": queued,
                "running": running,
                "awaiting_commit": completing,
                "bytes_outstanding": self._outstanding_bytes_locked(),
                "processes": self.processes,
                "max_outstanding": self.max_outstanding,
                "outstanding": len(self._jobs),
                # R3-04: delivery đã pop khỏi `_jobs` nhưng callback chưa
                # chạy xong — không thể nhìn thấy qua outstanding/awaiting
                # _commit (chỉ đếm `_jobs`), phải thể hiện riêng để
                # "telemetry 0 nợ" đồng nghĩa với drain True.
                "deliveries_pending": self._deliveries_inflight,
                "executor_rebuilds": self._executor_rebuilds,
                "executor_dead": self._executor_dead,
                "shutting_down": self._shutting_down,
            }


def _MAX_EXECUTOR_REBUILBS_SAFE() -> int:
    return _MAX_EXECUTOR_REBUILDS


def ensure_started(processes: int | None = None,
                   executor_factory=None, job_fn=None) -> KieCoordinator:
    """Coordinator dùng chung theo app (lazy). Test inject factory/job_fn."""
    global _coordinator
    with _lock:
        if _coordinator is None:
            _coordinator = KieCoordinator(
                processes=processes,
                executor_factory=executor_factory,
                job_fn=job_fn,
            )
        return _coordinator


def get_coordinator() -> KieCoordinator | None:
    with _lock:
        return _coordinator


def deferred_enabled() -> bool:
    """Bật đường deferred KIE — mặc định OFF (đường đồng bộ giữ nguyên)."""
    return os.environ.get("OCRTOOL_KIE_DEFERRED", "").lower() in {"1", "true"}


def telemetry() -> dict:
    coord = get_coordinator()
    if coord is None:
        return "n/a (stage K không hoạt động)"
    return coord.telemetry()


def drain(timeout: float | None = None) -> bool:
    """Chờ mọi job KIE terminal + applier chạy xong (gọi cuối lượt quét).
    True = metadata đã áp đủ; False = HẾT THỜI GIAN — caller KHÔNG được coi
    kết quả hiện có là hoàn tất (không cache-final — P1-07)."""
    coord = get_coordinator()
    if coord is None:
        return True
    return coord.wait_all(timeout)


def cancel_run(run_id: str) -> int:
    """Hủy job của MỘT LƯỢT quét (theo run_id) — dùng khi người dùng dừng
    lượt: job đang chạy giữ lease tới khi native thật kết thúc."""
    coord = get_coordinator()
    if coord is None:
        return 0
    return coord.cancel_run(run_id)


def shutdown_all(wait: bool = False) -> None:
    global _coordinator
    with _lock:
        coord = _coordinator
        _coordinator = None
    if coord is not None:
        coord.cancel_all()
        coord.shutdown(wait=wait)


def _kie_stage_stopper(soft: bool) -> None:
    """D1 stopper stage K: soft = ngừng nhận + cancel futures; hard =
    terminate process worker (native treo — dừng hữu hạn)."""
    coord = get_coordinator()
    if coord is None:
        return
    if soft:
        coord.shutdown(wait=False)
    else:
        return coord.hard_stop()


from scanindex.core.runtime.shutdown import register_stage_stopper  # noqa: E402

register_stage_stopper("K-kie-pool", _kie_stage_stopper)
