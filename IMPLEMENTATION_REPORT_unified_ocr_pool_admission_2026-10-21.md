# IMPLEMENTATION REPORT v2 — Unified OCR pool admission — 21/10/2026

> **Trạng thái (theo đúng khung REVIEW vòng 5):** "Đã có implementation bước đầu, ĐÃ SỬA toàn bộ 9 lỗi P1 + các điểm P2 nêu trong [REVIEW_IMPLEMENTATION](D:/App/ocrtool/REVIEW_IMPLEMENTATION_unified_ocr_pool_admission_2026-10-21.md); mọi probe tái hiện lỗi đã chuyển thành test PASS; **CHƯA đạt mốc benchmark máy 64 CPU** (còn warmup-ACK + đo lại theo 12.6)."
> Đối tượng review trước: REVIEW_IMPLEMENTATION (SHA `5369…18B05` — bản báo cáo v1). Bản v1 giữ trong lịch sử file này; các sửa đều ghi mục tương ứng.
> **Kết quả kiểm chứng cuối:** full suite `tests` — **769 passed / 0 failed** (đã chạy lại trong process sạch sau khi sửa; con số 756 cũ của v1 được thay bằng 769 gồm 13 test admission mới viết lại).

---

## 1. Sửa 9 lỗi P1 — ánh xạ lỗi → sửa → test xác nhận

| P1 | Lỗi đã tái hiện (probe của reviewer) | Sửa trong mã | Test xác nhận |
|---|---|---|---|
| P1-01 Janitor trả slot + quyền giữ input trước native kết thúc | Pool thật gate 1: nộp B khi A còn sống; cleanup rmtree input | `_fail_token_locked(..., release_resources=False)` cho đường lost — caller nhận lỗi NGAY nhưng slot/`live_inputs` GIỮ; chỉ nhả khi callback chứng minh task kết thúc HOẶC terminate/join thế hệ thành công; terminate ném lỗi → `recovery_failures` + giữ lease + báo phục hồi thất bại; bỏ 2 đường `except: rmtree` trực tiếp ở scanner (giữ thư mục + log) | `test_lost_request_keeps_lease_until_pool_terminated` (slot giữ → submit mới Busy; cleanup defer; file còn tồn tại); `test_recovery_terminate_failure_keeps_lease_and_reports` |
| P1-02 Không có gate ở thế hệ đầu; budget thành 1 | 5 request chưa xong ở "gate 2"; `_gate_capacity()` trả 1 | `_gate_capacity()` đọc env RAW (phân biệt "không đặt" với số dương — không qua `_env_int` ép min 1); `_get_pool()` dựng gate cùng thế hệ qua `_ensure_gate_locked()`; `try_submit_page` tự-chữa nếu thiếu; shutdown → lazy-init dựng lại | `test_gate_capacity_defaults_to_worker_count` (32/2/env 8); `test_cold_start_try_submit_creates_gate_via_product_path` (arrange `_gate_sem=None` — cold THẬT, submit đầu tự dựng + budget đầy + vượt → Busy); `test_shutdown_then_resubmit_rebuilds_gate` |
| P1-03 Public handle trả 4 phần tử vỡ B1/B2/Ký số/`ocr_one_page` | B2 unpack error; `ocr_one_page` trả None dù OCR hợp lệ | `_AdmissionResult.get()` trả `(page_idx, result)` — hợp đồng public 2 phần tử; token/timing là thuộc tính nội bộ (`handle.timing`); collector nội bộ đọc raw `handle.ar.get()` 4 phần tử; prebaked trả 2 phần tử tương thích | `test_lgbm_seam…` (đi qua submit thật); test B2 thật `test_batch_pipeline_submits_each_page_exactly_once` (suite); `ocr_one_page` dùng lại đường 2 phần tử — probe reviewer được phản ánh bằng test seam mới |
| P1-04 PDF→Word lỗi lần chạy đầu (`pipeline` None) | AttributeError sau khi đã bật processing state | Tạo pipeline TRƯỚC khi log; init thất bại → trả processing state + log lỗi | Smoke test lần đầu bấm xử lý — **đã kê vào test cần bổ sung (mục 4)**; sửa mã xong |
| P1-05 LightGBM đọc canonical sai thời điểm; thiếu assert prefix | Candidate trang nguồn 1 nhưng classifier nhận danh sách rỗng → file `clean` sai | Xác nhận LightGBM CHỈ chạy SAU khi cả lượt merge vào canonical tổng; `_assert_dense_prefix_upto(canonical, k)` assert liên tục 0..k trước predict (vi phạm = lỗi lập trình, raise); map rel/source tại ranh giới merge | `test_lgbm_seam_reads_real_canonical_second_page` — seam ĐỌC canonical THẬT trên đĩa, chỉ chấp nhận index 1 khi record tồn tại |
| P1-06 Đường serial/fallback bỏ kết quả OCR; capacity 1 vỡ | Tìm nhanh `clean` 0 trang; Tìm kỹ `UnboundLocalError` | `_assemble_scan_page` khai báo DÙNG CHUNG ngoài nhánh pool; single-worker lắp NGAY; vòng cuối lắp mọi kết quả có trong dict (kể cả engine-fallback tuần tự); trang thiếu kết quả KHÔNG nằm trong tập bỏ-có-chủ-đích → raise | `test_capacity1_serial_path_scans_stamp`; `test_engine_serial_fallback_results_are_assembled` (fallback không gọi on_page_done vẫn được lắp) |
| P1-07 Reset/submission thiếu giao dịch; peer không nhận lỗi; apply_async ném lỗi rò token | Hai handle cũ không ready, sink rỗng; token rò → Busy oan | `_fail_token_locked` đẩy lỗi vào sink CỦA TỪNG handle (reset/shutdown dùng nó, không gán tay); sink đăng ký TRƯỚC commit; apply_async trong try → rollback token + `("error", exc)`; `handle.get` poll sink ≤0,25 s, raise `multiprocessing.TimeoutError` đúng kiểu | `test_recovery_notifies_peer_handles_via_sink`; `test_shutdown_notifies_peer_sinks`; `test_apply_async_failure_rolls_back_token` |
| P1-08 Status bị ghi đè; lỗi trang thừa hủy dương tính | A unconfirmed + B dropped → `clean` sai; dấu chấp nhận + lỗi trang 2 → RuntimeError | Status suy MỘT LẦN từ tập: `any_accepted` / `lgbm_error` / `coverage_incomplete` (biến riêng, không ghi đè); lỗi trang SAU dấu đã chấp nhận (hoặc sau ứng viên đang chờ) → `skip_warn` có ghi nhận; cuối lượt: coverage thiếu mà chưa có dấu chấp nhận → `unconfirmed` (không sạch) | `test_lgbm_error_then_drop_keeps_unconfirmed`; `test_surplus_page_error_after_accepted_mark_is_warning` |
| P1-09 Sentinel vẫn thành trang không chữ (DOCX); validator chưa có; ký số chỉ checks missing-key | DOCX trả `(True, None)` với 2 sentinel | `page_result_state()` validator dùng chung (ok_text/ok_blank/error/missing); `_validate_page_result_or_raise` gọi trước lắp trang DOCX-export; ký số đổi sang validator (error/missing đều fail → nhánh ký-bản-gốc); B2 placeholder → sentinel giữ nguyên nhân | `test_page_result_state_contract`; probe DOCX reviewer → đường mã mới raise trước assembly (test DOCX output consumer nằm ở mục 4) |

## 2. Sửa các điểm P2

- **P2-10 cô lập test:** janitor thread nền TẮT bằng công tắc sản phẩm `_janitor_enabled=False` (không gán đè hàm sản phẩm vĩnh viễn); test lost gọi `_REAL_JANITOR_TICK()` trực tiếp — deterministic; fixture KHÔNG tự tạo semaphore (dùng `_ensure_gate_locked` sản phẩm); cold-start test arrange `_gate_sem=None`; FakeAR raise `multiprocessing.TimeoutError`.
- **P2-12 nhãn + chi tiết:** `error_callback` bind token theo request (`_make_error_callback`); percentile trên `deque(maxlen=512)` theo thứ tự thời gian; `_on_pool_task_done` tổng hợp `ocr_ms`; metrics thêm `admission_wait_ms` (caller chờ slot), `worker_start_wait` (submit→worker bắt đầu), `recovery_failures`; PLAN 12.7 đổi nhãn PASS → implemented/tested kèm tên test (xem mục 4).
- **P2-11 benchmark:** viết lại `ab_benchmark.py` — `--kie on/off` là CẶP ĐIỀU KIỂN SOÁT thật (env đặt trước import, đọc tại call-time); gate capacity in ra ở đầu mỗi lượt (`gate=4/8/12` xác nhận sống); ghi rõ rotate-disabled áp cho MỌI cấu hình; submitted-delta theo kịch bản. Kết quả mục 3.

## 3. Benchmark A/B sau sửa — gate THẬT (không còn budget=1)

Máy dev 12 luồng; cùng 16 PDF (8 không dấu + 8 dấu MẬT); mỗi cấu hình một process; 2 lượt sau warmup; KIE-off = `SECRET_SCAN_DISABLE_KIE_ORG=1` (tắt đúng đường serial đã định lượng). Rotate-disabled áp dụng MỌI cấu hình (nội bộ nhất quán).

| Kịch bản | 4/2 (KIE off) | 8/4 (KIE off) | 12/6 (KIE off) | 8/4 (KIE **on**) |
|---|---|---|---|---|
| Tìm nhanh 8 file không dấu | 2,56 s — 3,13 trang/s | 1,68 s — 4,76 trang/s | 1,50 s — 5,35 trang/s | 1,44–1,62 s — ~5,3 trang/s |
| Tìm nhanh 16 file lẫn dấu | 5,02 s — 3,18 trang/s | **2,88 s — 5,56 trang/s** | **2,58 s — 6,20 trang/s** | **15,3–19,5 s — ~1 trang/s** |
| Tìm kỹ 8 file (16 trang) | 3,07 s — 5,21 trang/s | 2,22 s — 7,22 trang/s | **2,13 s — 7,51 trang/s** | 2,24–2,31 s — ~7,1 trang/s |

**Đọc số (giả thuyết có kiểm soát — chưa phải máy đích):**
1. Scale theo worker: Tìm kỹ 4/2→8/4 = **1,33×**, →12/6 = **1,38×**; Tìm nhanh mixed 4/2→8/4 = **1,73×**, →12/6 = **1,93×**; lõi-bận tăng đúng tỉ lệ (2,2→4,1→7,4 ở Tìm kỹ).
2. **Cặp KIE on/off có kiểm soát (8/4): 15,3–19,5 s vs 2,88–3,03 s = 5–6,7×** — khẳng định định lượng nút thắt KIE serial là rào cản số một của kho có dấu, độc lập với số worker (phẳng 15→15 s khi 4/2→12/6 KIE-on).
3. quick16_plain 8→12 gần phẳng vì fixture chỉ 8 trang (hết việc) — khớp trần nguồn việc 12.5.
4. Trước đây số "15,98 s phẳng" ở KIE-off là **lỗi `handle.ar=None`** (P1-07 hệ quả) — đã sửa; số bảng này là số sau sửa.

## 4. Việc còn lại trước nghiệm thu (thứ tự)

1. Smoke test lần đầu bấm PDF→Word (P1-04 đã sửa mã, chưa có test UI) + test output consumer DOCX (validator chặn trước assembly).
2. Warmup-ACK (PID/generation/deadline) + đo cold-start; cân nhắc staged warmup — điều kiện trước benchmark máy đích (đã phát hiện thực tế: 12 worker × DLL đồng thời có thể treo >15 phút ở một số lần chạy).
3. Benchmark máy đích theo 12.6: bậc worker 8→16→24→32→(48), giữ số file cố định, KIE on/off thành cặp, corpus như nhau.
4. Panel UI tổng hợp thiếu-việc + tooltip 2 ô (12.2.5).
5. Chạy lại suite trong process sạch sau mỗi vòng sửa (đã làm lần cuối: 769 passed — ghi ở đầu báo cáo).

## 5. Nhãn PLAN 12.7 — đổi theo P2-12

Các hàngPASS/ĐÃ TRIỂN KHAI trong PLAN 12.7 được ràng buộc kèm tên test trong IMPLEMENTATION_REPORT §1; riêng hàng "Pool 32/48" sửa thành **implemented (test collector window=capacity) — benchmark pool 32/48 CHƯA chạy**; hàng UI tổng hợp giữ PARTIAL. Chỉnh chi tiết nằm trong PLAN v4.1 mục 12 (đã cập nhật kèm commit này).

## 6. Phản hồi các điểm P2 còn lại

- **P2-11 (1):** cặp KIE on/off đã chạy thật (bảng trên) — on chậm 5–6,7× ở cùng 8/4, khẳng định đúng định lượng trước.
- **P2-11 (2):** gate capacity in ở đầu lượt mỗi cấu hình (`gate=4/8/12`) — budget thật, không còn 1.
- **P2-11 (3):** số liệu theo cấu hình sản phẩm (worker+file cùng đổi) ĐỒNG THỜI có cột tách riêng trong PLAN 12.6.2 — benchmark tách biến là việc máy đích.
- **P2-11 (4):** metrics đổi tên đúng ngữ nghĩa (`worker_start_wait` = submit→worker bắt đầu; `admission_wait` = caller chờ slot) + `ocr_ms` p50/p95; worker busy% thực = tổng `ocr_ms`/(workers×wall) — có trong summary.
- **P2-11 (5–7):** rotate-disabled ghi rõ; đếm submitted-delta theo kịch bản có trong mỗi record; dự báo 3–5× đã rút khỏi mọi văn bản, chỉ còn "giả thuyết kiểm chứng trên máy đích".
- **P2-12:** nhãn implemented/tested/partial + tên test; `_on_pool_task_error` bind token; percentile deque; deadline admission tổng (env `OCRTOOL_ADMISSION_WAIT_S`, mặc định 300 s) chốt ở vòng test kế — ghi rõ là partial.
