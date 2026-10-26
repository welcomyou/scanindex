"""Process pool tồn tại lâu cho thẩm định file PDF/TIFF.

PyMuPDF không dùng được đồng thời từ nhiều thread (MuPDF single context)
nên song song hoá phải chạy bằng process riêng (Windows spawn). Pool được
tạo MỘT LẦN và hâm nóng ngay khi tạo — đo thực nghiệm cho thấy pool mới
mỗi lượt quét (mất chi phí spawn + IPC) còn chậm hơn chạy tuần tự khi
quick audit chỉ ~35 ms/file.

Worker chỉ import core (không Qt, không model OCR) để spawn nhanh và an
toàn trên bản portable (PyInstaller + ``freeze_support`` ở entrypoint,
``infra/mp_safety`` giữ nguyên). Mọi kết quả trả về là dataclass thuần —
không truyền ``fitz.Document``/Qt object qua IPC.
"""
from __future__ import annotations

import os
import threading
from concurrent.futures import Future, ProcessPoolExecutor
from dataclasses import dataclass

# 6 worker cho máy nhiều nhân: đo thực nghiệm 48 file đạt ~4,4× với 6
# worker (4 worker ~3,4×). Các file độc lập nhau nên thêm worker vẫn tăng
# gần tuyến tính — mặc định theo số nhân CPU, trần 12 để chi phí spawn/RAM
# hợp lý; vặn được qua biến môi trường OCRTOOL_AUDIT_WORKERS (kho IO nhanh
# có thể tăng hơn, trần 32). Floor 2 để một process bận vẫn có suất cho
# file người dùng đang chọn.
def _configured_workers() -> int:
    try:
        n = int(os.environ.get("OCRTOOL_AUDIT_WORKERS", "0").strip() or "0")
    except ValueError:
        n = 0
    if n >= 1:
        return min(n, 32)
    return max(2, min(12, (os.cpu_count() or 4) - 1))


DEFAULT_WORKERS = _configured_workers()
# Task gộp nhiều file để chia nhỏ overhead IPC; tune 1/2/4/8 theo kho
# thực tế nếu cần (chunk quá lớn làm file đang chọn chờ cả nhóm).
DEFAULT_CHUNK = 4


def _warm_task(_: int) -> int:
    """Task hâm nóng: buộc process spawn và nạp sẵn toàn bộ phụ thuộc
    thẩm định (PyMuPDF + module core, kể cả tên file/chuỗi trang TIFF).

    Phải ngủ ngắn: task hoàn thành tức thì thì executor tái dùng đúng 1
    worker rảnh và không bao giờ spawn đủ số worker — sau đó các chunk đầu
    chạy nối tiếp thay vì song song. Nạp sẵn module core (chứ không chỉ
    fitz) để chunk ĐẦU TIÊN không trả thêm chi phí import — lượt quét mới
    có kết quả hiển thị sớm nhất có thể."""
    import time

    import fitz  # noqa: F401  — nạp MuPDF vào worker trước khi đo
    from scanindex.core import digitization_audit  # noqa: F401

    time.sleep(0.25)
    return os.getpid()


def audit_chunk_task(args: tuple) -> list:
    """Worker top-level: thẩm định một chunk file, trả list kết quả thuần.

    ``sibling_cache`` nội bộ chunk giúp các TIFF cùng thư mục (thường rơi
    vào cùng chunk vì danh sách đã sort) không phải liệt kê lại thư mục.
    """
    paths, quick, policy = args
    from scanindex.core import digitization_audit as da

    sibling_cache: dict = {}
    return [da.audit_file(p, quick=quick, policy=policy,
                          sibling_cache=sibling_cache)
            for p in paths]


def audit_one_task(args: tuple):
    """Worker top-level: thẩm định một file (đường bấm chọn)."""
    path, quick, policy = args
    from scanindex.core import digitization_audit as da

    return da.audit_file(path, quick=quick, policy=policy)


@dataclass
class PoolStatus:
    available: bool
    workers: int = 0


class AuditPool:
    """Pool process hâm nóng dùng chung cho màn rà soát.

    ``ensure_started`` an toàn để gọi nhiều lần; thất bại (môi trường không
    spawn được, EXE portable lạ…) thì đánh dấu ``failed`` và caller chạy
    tuần tự trong thread như trước — thẩm định không bao giờ mất vì pool.
    """

    def __init__(self, max_workers: int | None = None):
        self.max_workers = max_workers or DEFAULT_WORKERS
        self._executor: ProcessPoolExecutor | None = None
        self._warm_futures: list[Future] = []
        self.failed = False
        self.warm_workers = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------ lifecycle

    def ensure_started(self) -> bool:
        """Tạo pool và xếp việc nạp sẵn PyMuPDF vào hàng đợi — KHÔNG chờ.

        Trước đây hàm này chờ spawn đủ worker (2–4 giây, máy có antivirus
        còn lâu hơn) trước khi cho phép quét, khiến thanh tiến độ đứng im
        ở 0% cả khoảng đó. Giờ chỉ tạo executor và đẩy task warm vào FIFO:
        worker được spawn/nạp song song với các chunk đầu tiên (worker nào
        chưa lên sẽ nhận chunk sau); pool lỗi được phát hiện qua exception
        của chunk và caller tự chuyển tuần tự."""
        with self._lock:
            if self.failed:
                return False
            if self._executor is not None:
                return True
            try:
                executor = ProcessPoolExecutor(max_workers=self.max_workers)
                # 2× warm task: buộc spawn đủ worker + nạp sẵn phụ thuộc
                # thẩm định trong từng worker, nằm TRƯỚC các chunk trong
                # FIFO nên chi phí import được hấp thụ nền, không chặn ai.
                self._warm_futures = [
                    executor.submit(_warm_task, i)
                    for i in range(self.max_workers * 2)]
            except Exception:
                self.failed = True
                self._warm_futures = []
                try:
                    executor.shutdown(wait=False, cancel_futures=True)
                except Exception:
                    pass
                return False
            self._executor = executor
            self.warm_workers = self.max_workers
            return True

    def warm_ready(self) -> int:
        """Số worker ĐÃ lên và nạp xong phụ thuộc — đếm pid khác nhau trong
        các warm task đã hoàn thành (mỗi worker trả đúng pid của nó).

        Cho GUI hiển thị tiến độ khởi động ("N/M bộ quét sẵn sàng") thay vì
        nhãn đứng im suốt khoảng spawn — lần đầu trên máy có antivirus có
        thể là hàng chục giây. An toàn gọi từ mọi thread: Future thread-safe,
        chưa có pool thì trả 0."""
        futures = self._warm_futures
        ready: set[int] = set()
        for fut in futures:
            if fut.done():
                try:
                    ready.add(fut.result())
                except Exception:
                    continue
        return len(ready)

    def status(self) -> PoolStatus:
        return PoolStatus(available=self._executor is not None,
                          workers=self.warm_workers)

    def shutdown(self) -> None:
        if self._executor is None:
            return
        executor, self._executor = self._executor, None
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass

    # ------------------------------------------------------------ submit

    def map_chunks(self, paths: list[str], *, quick: bool, policy: str,
                   chunk: int = DEFAULT_CHUNK) -> list[Future]:
        """Submit các chunk file; trả list Future theo thứ tự chunk.

        Caller tự ``result()`` từng Future và tự hủy các Future chưa chạy
        khi cần dừng (``Future.cancel`` chỉ tác dụng trên hàng chờ — job
        đang chạy sẽ chạy nốt, độ dài một chunk bị chặn bởi ``chunk``).
        """
        executor = self._executor
        if executor is None:
            raise RuntimeError("pool chưa khởi động")
        chunks = [paths[i:i + chunk] for i in range(0, len(paths), chunk)]
        return [executor.submit(audit_chunk_task, (c, quick, policy))
                for c in chunks]

    def submit_one(self, path: str, *, quick: bool, policy: str) -> Future:
        executor = self._executor
        if executor is None:
            raise RuntimeError("pool chưa khởi động")
        return executor.submit(audit_one_task, (path, quick, policy))
