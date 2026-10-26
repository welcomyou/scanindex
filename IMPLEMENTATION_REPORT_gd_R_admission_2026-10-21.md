# IMPLEMENTATION REPORT — GĐ R (đóng nợ đúng đắn P1-A…F + metric + telemetry) — 21/10/2026

> **Trạng thái: ĐÃ TRIỂN KHAI + ĐÃ QUA TEST.** Phạm vi đúng như [PLAN v5.1 §4.1](D:/App/ocrtool/PLAN_ocr_pool_max_throughput_2026-10-21.md) và cửa R của [REVIEW_READINESS](D:/App/ocrtool/REVIEW_READINESS_ocr_pool_max_throughput_v5_1_2026-10-21.md): 6 lỗi P1-A…F (REVIEW IMPLEMENTATION v2), metric đúng ngữ nghĩa (P2-H), telemetry schema chung, sentinel recycle off (B7). **W/K/P chưa triển khai; chưa benchmark máy đích — không suy ra hiệu năng 64 CPU từ bản này.**

## 1. Ánh xạ P1-A…F → sửa → test

| P1 | Sửa trong mã | Test xác nhận |
|---|---|---|
| **A** — lease lost/shutdown không thu hồi sau khi native dừng | `_fail_token_locked` tách **outcome** (done/error/sink, chỉ set một lần) khỏi **lease** (`_admission_release_locked` idempotent qua pop); guard done KHÔNG còn chặn release; janitor nhánh (a) nhả lease khi `ar.ready()` **bất kể outcome đã báo**; recovery thành công nhả lease của **mọi** token thế hệ bị đóng (kể cả lost) | `test_lost_lease_released_after_successful_shutdown` (trước stop: token=1/live=1/cleanup defer → sau stop: 0/0/dir bị xóa); `test_lost_lease_released_when_native_finishes_later` (lost → native xong sau → janitor thu, `reaped≥1`); `test_lost_lease_released_after_successful_recovery` |
| **B** — shutdown đánh lỗi request thế hệ mới | `shutdown_pool` snapshot `shutdown_ids` (token generation bị đóng) ngay pha 1; pha 2 chỉ kết thúc/thu lease đúng snapshot; thêm `_mp_pool_gen` (None=chưa publish, -1=pool dying): submission trong cửa sổ shutdown/recovery bị **từ chối sạch** ở đoạn đồng bộ (không token rò) thay vì nộp nhầm pool cũ | `test_shutdown_generation_scoped_new_request_rejected_then_served` — submit chen trong `terminate()`: nhận `("error", PoolResetError)` không rò token; sau shutdown thế hệ mới phục vụ bình thường; request cũ nhận PoolResetError qua sink |
| **C** — `get(timeout)` ném TimeoutError sau một nhịp 0,25 s | nhánh `except TimeoutError`: còn deadline → `continue` (đầu vòng kiểm lại sink); chỉ raise khi hết deadline **tổng** | `test_get_timeout_waits_full_deadline` — Pool spawn thật, job 0,8 s: `get(timeout=3.0)` trả kết quả (elapsed ≥0,7 s); `get(timeout=0.3)` raise đúng kiểu |
| **D** — terminate lỗi vẫn mở budget mới đầy đủ | recovery terminate thất bại: giữ lease thế hệ cũ + dựng gate mới rồi **trừ đúng 1 slot cho mỗi lease cũ còn sống** (`foreign_charged`); khi native cũ kết thúc, janitor nhả → slot hoàn về gate hiện tại (`_admission_release_locked` hỗ trợ foreign_charged) | `test_recovery_terminate_failure_reserves_budget_then_returns_slot` — budget=1: sau recovery submit mới **Busy** (gate value 0); native cũ xong → janitor thu → gate value 1 → submit mới submitted |
| **E** — lỗi trang hậu tố về trước ứng viên hủy dương tính | `_process_pdf_per_page` thêm `deferred_errors` (keyword): lỗi lắp trang được **giữ theo index** (rel→reason), không raise/skip tại callback; single-worker fallback.defer tương tự; streaming phân loại SAU khi xác nhận ứng viên: lỗi sau dấu chấp nhận = trang thừa (cảnh báo), còn lại = **lỗi bắt buộc → raise** (file lỗi, resume) | `test_suffix_page_error_ready_before_candidate_is_warning` — lỗi trang 2 ready TRƯỚC ứng viên trang 1: vẫn `accepted` + match; `test_required_page_error_with_dropped_candidate_raises` — ứng viên bị loại + lỗi trang → raise `trang 2` (không unconfirmed-ok) |
| **F** — OCR lỗi bắt buộc thành unconfirmed-ok + cache | cùng cơ chế E: lỗi bắt buộc luôn raise ra khỏi streaming → file đi nhánh Exception của `process_one` (error/resumable, không cache hoàn tất); tách rõ "unconfirmed chính đáng" = LightGBM lỗi trên coverage hợp lệ (giữ nguyên chính sách) | test E thứ hai + suite P1-08 cũ vẫn pass (`test_lgbm_error_then_drop_keeps_unconfirmed`) |

## 2. Metric + telemetry (P2-H, B7, schema GĐ R)

- **`admission_wait_ms` đo từ lúc caller bắt đầu yêu cầu trang**: collector theo dõi wait theo từng trang (`page_wait_started`), `submit_page` theo dõi theo vòng lặp; caller đã Busy chờ thì entry full-wait được ghi khi nộp thành công (entry attempt-level vẫn do `try_submit_page` ghi — mỗi trang chỉ thêm entry full-wait khi CÓ chờ). Test: `test_admission_wait_measured_from_request_start` (chờ 0,35 s → entry ≥ 250 ms).
- **Tổng cộng toàn lượt + busy%**: `ocr_ms_total/ocr_ms_count/run_first_submit_at`; summary thêm `ocr_total=N task/Xms worker_busy_est=P%` — busy% **worker-wall** (tổng ocr_ms / (capacity × wall)), không phải CPU%. Test: `test_summary_contains_totals_and_busy_estimate`.
- **Telemetry schema** (`pool_telemetry()` / `pool_telemetry_line()`): `generation, state(idle|ready|quarantined|stopping), requested_workers, effective_capacity, ready_live_workers="n/a (stage W)", ocr_inflight, leases_held, live_inputs, recovery_failures, kie_backlog="n/a (stage K)", prep_backlog="n/a (stage P)", cpu_rss_tree="n/a (stage B)"`. Log cuối lượt quét cạnh admission summary. Test: `test_pool_telemetry_schema`.
- **Recycle off (B7)**: `DIRECT_OCR_MAXTASKSPERCHILD=off|none|disabled|0|-1` → `None` tại Pool constructor (tên chuẩn DUY NHẤT — đã sửa §5 plan cho khớp). Test: `test_recycle_off_sentinel`.

## 3. Lưu ý hành vi (release-note)

1. Lượt trước "ok" có thể thành Lỗi: OCR lỗi bắt buộc không còn bị nuốt thành unconfirmed/0-match (đúng chính sách GĐ 0 đã kê).
2. Submission trong cửa sổ shutdown/recovery nhận `PoolResetError("pool generation changed during submit")` và được caller retry — thay vì nộp nhầm pool đang chết.
3. Sau recovery terminate-thất-bại, gate thế hệ mới bị trừ budget cho lease cũ → submit mới có thể Busy lâu hơn cho tới khi native cũ kết thúc — chấp nhận có chủ đích (bất biến trần native toàn app).
4. Summary `[admission]` có thêm `state=` và `worker_busy_est=`; dòng `[telemetry]` mới cuối lượt.

## 4. Kết quả test

- Bộ mới `tests/test_ocr_admission_r_fixes.py`: **12/12 passed**.
- Suite liên quan (admission v4 + secret_scan_modes + archive_runner + R): **59/59 passed**.
- Full suite (process sạch, 21/10 sau khi sửa): **xem §5 — kết quả được ghi khi lần chạy hoàn tất**.
- Điều chỉnh test cũ có chủ đích: `test_lost_request_keeps_lease_until_pool_terminated` (v4) đổi assertion cuối theo hợp đồng P1-A (sau recovery thành công lease = 0) — đúng yêu cầu REVIEW v2 §2.A "test phải kiểm tra cả sau stop thành công"; fixture v4 reset thêm `_mp_pool_gen/_pool_state` + metric mới (P2-I isolation).

## 5. Kết quả full suite

**781 passed / 0 failed** (11 phút 22 giây, process sạch, 21/10 sau khi sửa — log: `temp/pytest_full_r.log`). Gồm 769 test cũ (0 vỡ) + 12 test GĐ R mới.

## 6. Còn lại (không thuộc R)

Warmup supervisor (W — cần chốt gate-A đã ghi plan §4.2.0), pipeline KIE (K1/K2 — cần đồng bộ PLAN KIE §5), prep process (P-b), benchmark máy đích (B). Khối lượng này KHÔNG được tính là đã tối ưu máy 64 CPU/128 GB.
