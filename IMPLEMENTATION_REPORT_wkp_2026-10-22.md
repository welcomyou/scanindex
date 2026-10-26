# IMPLEMENTATION REPORT — GĐ W + K + P (PLAN v5.1) — 22/10/2026

> **Trạng thái: ĐÃ TRIỂN KHAI (core) + ĐÃ QUA TEST.** W 6/6, K 9/9, P 5/5 test riêng; full suite process sạch — **xem §5**. K và P mặc định **OFF** (cấu hình/env mới) — hành vi production hiện tại không đổi cho đến khi bật có chủ đích. **GĐ B (benchmark máy đích 64 CPU/128 GB) CHƯA làm — không suy ra hiệu năng máy lớn từ bản này.**

## 1. GĐ W — Warmup supervisor (`direct_engine.py`)

| Hợp đồng (plan §4.2/§4.2.0) | Triển khai | Test |
|---|---|---|
| Supervisor-accounting, KHÔNG semaphore liên process (probe permit-loss 21/10) | Initializer CHỈ nhẹ (stdio + lưu path); DLL/model do worker **tự nạp** khi chạy `_warmup_task` (do supervisor post theo đợt k) hoặc ở task OCR đầu tiên (`_ensure_worker_loaded`). Không có permit liên process nào để mất | `test_staged_warmup_ramps_gate_and_limits_concurrency` (Pool thật) |
| Staging giới hạn k nạp đồng thời | Supervisor post k task warmup (`OCRTOOL_WARMUP_STAGE`, mặc định 4); ACK về mới post tiếp | test trên: gate ramp theo đợt (≥2 mốc) |
| ACK PID/generation + init-fail có error_code | `_on_warmup_ack/_on_warmup_error`; ACK sai thế hệ bị bỏ; ACK failed ghi `failed[]` + log loud; **rss_mb + t_load_s** trong ACK (psapi) |test deadline + telemetry |
| Deadline từng pha, queue-wait tách load-thực | `_warmup_deadline_tick` (`OCRTOOL_WARMUP_DEADLINE_S`, mặc định 120 s): có ready>0 → **suy giảm** (effective = ready, không đập generation); ready=0 → quarantine + `_request_pool_recovery` (chứng minh generation cũ dừng trước khi dựng mới) | `test_warmup_deadline_degrades_with_partial_ready`; `test_warmup_deadline_zero_ready_triggers_recovery_ladder` (Pool thật, deadline 1 s) |
| Thang retry hữu hạn, không generation thứ tư | `_warmup_stage_plan`: lần 1 N/k=4 → lần 2 k=1 → lần 3 max(1,N/2)/k=1 → **lần 4 `OCRWarmupError` fail-loud** (không spawn) | `test_warmup_stage_plan_ladder` |
| Gate-A: capacity động trong cùng generation | `_AdmissionGate` (Condition + held): `ready=0` ⇒ Busy; `set_capacity` không rút token sống (không xuống dưới held); waiter được đánh thức khi tăng; `_value` giữ tương thích test v4; **không thay object gate** → callback token cũ luôn release đúng gate | `test_gate_capacity_change_preserves_held_tokens`; `test_gate_acquire_blocking_wakes_on_capacity_increase` |
| `_get_pool()` không chặn vô hạn | Pool constructor trả ngay; supervisor chạy nền; publish `_mp_pool_gen` như GĐ R | test ramp |
| Recycle respawn | Worker respawn chạy initializer nhẹ; task kế tự nạp (self-load, sentinel `worker_init_failed` giữ nguyên semantics cho collector recovery) | (ngầm qua self-load; không re-ACK ready-count — đã ghi nhận ở §4 hạn chế) |

Telemetry: `pool_telemetry()["ready_live_workers"]` = số ACK sống của thế hệ hiện tại; `state` thêm `warming`. Log: `warmup starting/ready k/N/DONE/DEADLINE`.

## 2. GĐ K — Pipeline KIE core (`kie/worker.py`, `secret_scan_kie.py` mới; tích hợp scanner)

| Hợp đồng | Triển khai | Test |
|---|---|---|
| Process KIE session riêng, không load theo file | `kie/worker.py`: `warmup_worker()` nạp tokenizer+ONNX 1 lần/process (initializer của ProcessPoolExecutor); `run_job` gọi `_run_layoutlmv3(selected_pages=[ordinal])` **từng trang** — giữ hậu xử lý đường cũ, không đổi kết quả bằng gọi nhiều trang | (unit bằng job_fn giả; parity infer = cùng hàm engine) |
| Hàng đợi có giới hạn: credit + byte | `KieCoordinator`: credit = **2×Pkie** (đếm job ĐANG CHẠY); byte budget 512 MiB (đếm byte thật). Vượt → **từ chối nhận** → caller giữ đường đồng bộ (không mất metadata, không backlog vô hạn) | `test_execution_credit_limits_outstanding`; `test_byte_budget_refusal_keeps_sync_path` |
| Dedup một-future-nhiều-người-nhận | key (canonical normcase, ordinals, model_fp) → cùng `_Job`, danh sách callbacks | `test_dedup_one_future_many_receivers` |
| Retry ĐÚNG 1 lần; model_missing không retry | `_on_future_done`: lỗi tạm thời → re-queue attempt=2 (future cũ xóa để không chiếm credit); `model_missing` terminal ngay | `test_transient_error_retried_once_then_ready`; `test_model_missing_not_retried` |
| Lease input: cleanup không rmtree trước KIE đọc | Coordinator **sở hữu** lease (`acquire_input_lease` lúc submit, `_release_lease` đúng một lần khi terminal — kể cả cancel/error/dispatch-fail) | `test_input_lease_held_until_terminal`; `test_byte_budget_refusal...` (không rò lease) |
| Completion = metadata cuối | `drain()/wait_all()` — `_run_worker` gọi cuối lượt trước commit: mọi file pending được commit MỘT LẦN với metadata đủ; UI/journal semantics không đổi | `test_deferred_scanner_integration`; suite quét cũ 0 vỡ |
| Cancel | `cancel_all`: ngừng nhận, chốt `cancelled` (callback + lease nhả sau khi nhả lock); future đang chạy kết thúc tự nhiên, kết quả bỏ | `test_cancel_all_settles_pending_with_cancelled` |
| KIE lỗi không đổi kết luận về dấu | `artifact.metadata_pending` chỉ cờ metadata; kết luận dấu/status tính trước; lỗi → giữ regex/note cũ như đường đồng bộ | test integration |
| Đồng bộ PLAN KIE §5 (cửa B) | **Đã bổ sung** hợp đồng spool lỗi/cancel vào [PLAN KIE §5](D:/App/ocrtool/PLAN_secret_scan_KIE_pipeline_2026-10-21.md): tách execution credit / storage budget, GC chỉ sau terminal-trung-lập, hết budget → dừng nhận resumable | (tài liệu) |

**Bật bằng** `OCRTOOL_KIE_DEFERRED=1` — **mặc định OFF**. Khi ON: PDF Tìm nhanh/Tìm kỹ bàn giao KIE ngay sau dò dấu (file-worker thả sớm); DOCX giữ đồng bộ. Drain timeout `OCRTOOL_KIE_DRAIN_TIMEOUT_S` (600 s).
**Chưa làm trong K (kê khai trung thực):** journal v3 hai trục trạng thái + UI cập nhật theo revision từng metadata + hiển thị "Bổ sung thông tin A/B, còn C" + xuất Excel giữa lượt đánh dấu pending + resume KIE-only khi app đóng giữa chừng. Hiện trạng: file có KIE pending chưa commit trong lượt → hủy app giữa chừng = lượt sau quét lại file đó (an toàn, chỉ tốn công OCR lại — hợp đồng "không mất metadata" giữ nguyên).

## 3. GĐ P — Prep process (`preprocessing/prep_worker.py` mới; tích hợp scanner)

| Hợp đồng (readiness C) | Triển khai | Test |
|---|---|---|
| CÙNG hàm transform, process riêng | `prep_chunk` gọi `preprocessing.pre_process_pdf(...)` với ĐÚNG tham số đường inline trong process sở hữu fitz riêng — parity theo cấu trúc | `test_prep_process_parity_with_inline` (PDF tổng hợp xoay 90°: rotation metadata + số trang khớp) |
| 2 process / cửa sổ 2×P | `OCRTOOL_PREP_PROCESSES` (mặc định 2); BoundedSemaphore cửa sổ | `test_prep_telemetry_schema` |
| Lease input/output, cleanup sau terminal | `acquire/release_input_lease` quanh future — kể cả hủy/timeout, lease chỉ thả sau khi future kết thúc | `test_prep_lease_released_after_success`; `test_prep_fallback_on_executor_error` |
| Lỗi → fallback inline, không fail lượt | Executor lỗi/handled=False → log + rơi xuống đường inline hiện có | `test_prep_fallback_on_executor_error` |
| Không thêm ô người dùng | Env `OCRTOOL_PREP_MODE=prep-process` — **mặc định OFF (inline)** | `test_prep_telemetry_schema` (enabled=False) |
| Route qua pool khi bật | `_preprocess_pdf_for_ocr` đi prep-process khi env bật | `test_scanner_routes_to_prep_process_when_enabled` |

## 4. Env mới (benchmark/điều khiển có chủ đích — log khi bật)

`OCRTOOL_WARMUP_STAGE` (4; 0=tắt staging), `OCRTOOL_WARMUP_DEADLINE_S` (120), `OCRTOOL_WARMUP_TEST_MODE`/`_SLEEP` (chỉ test), `OCRTOOL_KIE_DEFERRED` (OFF), `OCRTOOL_KIE_PROCESSES` (2 nếu ≥16 CPU logic, không thì 1), `OCRTOOL_KIE_PAYLOAD_BUDGET_MB` (512), `OCRTOOL_KIE_DRAIN_TIMEOUT_S` (600), `OCRTOOL_PREP_MODE` (inline), `OCRTOOL_PREP_PROCESSES` (2).

## 5. Kết quả test

- `tests/test_warmup_supervisor_w.py` — 6/6 passed (gồm 2 integration Pool thật: staging ramp + deadline→recovery ladder).
- `tests/test_kie_coordinator_k.py` — 9/9 passed.
- `tests/test_prep_process_p.py` — 5/5 passed (gồm parity cross-process thật).
- **Full suite process sạch (CHẠY CUỐI sau sửa drain-race): 801 passed / 0 failed** (11 phút 21 s — 781 cũ không vỡ + 20 test W/K/P mới; log `temp/pytest_full_wkp2.log`).
- **Smoke DLL thật (end-to-end, 2 worker, k=2)**: `temp/mat_bench/diag_warmup.py` — t=1,0 s pool published (constructor trả ngay, gate=0); t=2,0 s **2/2 worker ACK** (DLL nạp thật, gate ramp 0→2, state warming→ready); t=2,4 s OCR thật trả kết quả qua worker đã warm; retries=0.
- **Smoke KIE thật (tương đương sync/deferred — test #1 PLAN KIE §7)**: `temp/mat_bench/smoke_kie_deferred.py` trên `file anh mat.png`: sync → org "THÀNH ỦY THÀNH PHỐ HỒ CHÍ MINH*", năm 2026, giải mật 10 năm; deferred (sau sửa) → **đồng nhất từng ký tự**, scan phase 3,5 s (KIE chồng ngoài), KIE worker infer 1,58 s/process riêng, 7 field instances; telemetry drain 0 outstanding.
- **Bắt được + sửa 1 race thật trong lúc smoke**: `wait_all` trả về khi job terminal nhưng callback (điền metadata) chạy SAU — commit sau drain sẽ thiếu org/năm. Đã sửa: callback + nhả lease chạy TRƯỚC khi drain báo xong (hợp đồng drain mới: drain trả về = metadata đã áp xong; callback không được gọi API coordinator). Phát hiện nhờ so kết quả sync/deferred trên model thật — đúng tinh thần test #1 của PLAN KIE.
- **Bài học ghi nhận:** lần smoke đầu ra `OCRWarmupError` KHÔNG phải lỗi W — script chẩn đoán thiếu guard `if __name__ == "__main__"` khiến child spawn re-run script → đệ quy dựng pool → child chết → 0 ACK → 3 deadline. Ứng dụng thật (`ocr_app.py`) có hạ tầng spawn riêng (`patch_multiprocessing_spawn_sys_path`) nên không gặp; mọi script harness sau này bắt buộc có main-guard.

## 6. Việc còn lại (nghiêm túc kê khai)

1. **GĐ B**: benchmark máy đích 64 CPU/128 GB theo ma trận plan §4.5 (B1 baseline sau-R → B8 đo sản phẩm), soak recycle, chốt preset. Không được tuyên bố "tận dụng tối ưu 64 CPU" trước khi có số này.
2. **K kế tiếp**: journal v3 hai trục trạng thái + UI cập nhật metadata theo revision + Excel pending-mark + resume KIE-only (đã liệt kê §2).
3. **P kế tiếp**: đo A/B bật/tắt trên corpus thật (trigger: telemetry thiếu-việc prep trội khi W≥16), quyết định flip mặc định.
4. Smoke UI lần đầu bật từng flag trên máy dev trước khi đưa vào bản dựng.
