# FIX REPORT 2 — trả lời REVIEW FIX REPORT W/K/P (22/10/2026)

> **Trạng thái: ĐÃ SỬA 6/6 điểm chặn P1-A→F + 3 điểm P2-G/H/I; regression sạch.**
> Review lượt 2 **đúng cả 9/9 điểm kỹ thuật** — mỗi điểm đều được kiểm chứng trên
> mã trước khi sửa. K/P vẫn mặc định **OFF**. **Full suite process sạch: 831
> passed / 0 failed** (12 phút 13 s — 821 trước không vỡ + 10 test mới).

Đối tượng trả lời: [REVIEW_FIX_REPORT_wkp_2026-10-22.md](D:/App/ocrtool/REVIEW_FIX_REPORT_wkp_2026-10-22.md).
Mã sau lượt sửa này (SHA-256 đầu…cuối): `direct_engine.py`
`100DCE44B3222CC4…82B71927`, `secret_scan_kie.py` `B69B83C3DAD2F5F3…39157328`,
`prep_worker.py` `53E6E091E0B226A0…88861608`,
`secret_file_scan_screen.py` `60EDAFA316672029…C0DE8C6C`.

## 1. Xác minh review lượt 2

| ID | Verdict | Bằng chứng (trước sửa) |
|---|---|---|
| P1-A | **Đúng** | `_apply_declassification_notes` strip note rồi tính lại nhưng `declass_due` chỉ gán True trong nhánh đủ hạn — KIE nâng 2010→2026: issue_year=2026 nhưng `declass_due=True` cũ còn nguyên; năm không suy được thì toàn bộ derived cũ giữ nguyên. |
| P1-B | **Đúng** | `_cancel_locked` append tuple **2 phần tử** `(job, cbs)` trong khi `cancel_all/cancel_run` unpack 3 → `ValueError: expected 3, got 2` ngay khi có job được chốt cancelled ngay (đường chạy-đang chạy `continue` nên test cũ không chạm). Đồng thời `fut.cancel()` gọi **giữ `_cond`** và **trước** khi đánh dấu intent → done-callback đồng bộ của cancel chạy `_on_future_done` dưới lock, thấy `CancelledError` + attempt=1 + không phải model_missing → **retry ngược ý định hủy**. |
| P1-C | **Đúng** | `_, retry_dispatched = self._dispatch_locked()` — bundle `failed` của lượt retry bị **bỏ**: job settle `dispatch_failed`, pop, nhả lease, nhưng callback_count=0 và drain=True (job đã pop). Nhánh dedup-muộn: job pop + notify **trước** khi chạy `late` → `wait_all` True trong khi applier thứ hai chưa chạy, 2 ref lease còn giữ. |
| P1-D | **Đúng** | `rescan_rel` tính xong nhưng chỉ dùng ở nhánh `replace=False`; nhánh replace gọi `commit_replace_file` (tự xóa rel khỏi rescan_pending) rồi không thêm lại — chính lượt retry lỗi xóa nghĩa vụ retry, `needs_continue` thành False → journal discard. |
| P1-E | **Đúng** | `_commit_entry` chỉ kiểm cờ `committed`; lượt đóng (cancel) xong, callback terminal muộn vẫn `_deliver()` → commit vào journal đã close/discard (event không header), phát thêm hàng UI. |
| P1-F | **Đúng** | `permit is False` → log warning + `permit=None` → **vẫn đi nạp**. Fail-open phá bất biến ≤k đúng như probe (holder còn sống/treo → 2 cùng lúc với k=1). Lập luận "tránh deadlock" của tôi không chấp nhận được vì có đường recovery hữu hạn. |
| P2-G | **Đúng** | `_commit_entry` không remove entry khỏi `pending_commits`, không thả artifact/canonical; entry ↔ closure giữ tham chiếu chéo tới cuối lượt mới `clear()`. |
| P2-H | **Đúng** | Fingerprint chụp **sau** scan chỉ phát hiện đổi giữa post-scan ↔ commit; nguồn đổi TRONG lúc quét được cache dưới fingerprint chụp lại — "hợp thức hóa kết quả cũ". |
| P2-I | **Đúng** | Thứ tự: file-worker finally save (prog+registry) → metadata về sau → `registry.record` qua callback KHÔNG save gì thêm → close cuối lượt; `_append_line` chỉ write-buffer. `committed=True` đặt **trước** commit — lỗi ghi đĩa bị nuốt, entry mất vĩnh viễn. |

## 2. Đã sửa — từng điểm

### P1-A — derived metadata tính lại toàn bộ (`secret_file_scan_screen.py`)
Trước khi tính lại, bộ derived được **reset cùng nhau**: `issue_year=0`, `declass_years=0`, `declass_due=False`, `note` đã strip ghi chú cũ; rồi chỉ gán từ nguồn hiện tại. Nhánh không suy được năm (hoặc keyword không có thời hạn) để derived ở trạng thái sạch. Lưu ý semantic được test chốt: cache KIE không suy được năm → cascade rơi về năm **regex** (đúng đường sync-on-error), chỉ khi trang không còn ứng viên ngày nào thì derived rỗng hẳn. Ghi chú/override người dùng không bị đụng (strip chỉ loại segment cùng nhãn).
**Test:** nâng cũ→mới (due→not-due, note biến mất), mới→cũ (not-due→due, đúng 1 note), cụt năm về regex, mất hẳn ứng viên ngày. (`test_declass_recompute_resets_due_when_year_upgraded`, `test_declass_recompute_due_again_when_year_downgraded`)

### P1-B — cancel 3 pha, intent trước `fut.cancel()` (`secret_scan_kie.py`)
`_cancel_locked` tách 3 pha: **(1) giữ lock** — đánh dấu `cancel_result` cho job có future TRƯỚC khi đụng `fut.cancel()`; job queued/không future chốt cancelled ngay; **(2) ngoài lock** — gọi `fut.cancel()` (done-callback đồng bộ nó kích hoạt chạy khi `_cond` rảnh; `_on_future_done` nhìn thấy intent → chốt **cancelled, không retry**); **(3) giữ lock** — job còn non-terminal mà TẤT CẢ future đã `cancelled()` (chưa kịp chạy) → chốt + nhả lease; future đang chạy thật → giữ intent, `_on_future_done` chốt khi native kết thúc. Bundle trả về **đủ 3 phần tử** (sửa ValueError); `cancel_run` trả kèm số job hủy (đếm cả job do callback đồng bộ pha 2 chốt) và **dispatch lại hàng đợi** sau khi credit giải phóng. Scanner: `cancel_run` lỗi trong finally **log exception** thay vì `pass`.
**Test:** job queued-trong-coordinator (không future) — không ValueError; job queued-trong-executor — hủy sạch, `ran==0`, không callback "ready", không retry; lease cuối 0. (`test_cancel_queued_in_executor_no_retry_no_unpack_error`, `test_cancel_job_queued_in_coordinator_tuple_ok`)

### P1-C — không bỏ bundle retry; drain chờ đủ người nhận (`secret_scan_kie.py`)
Retry path giờ nhận **cả** `(failed, to_watch)` — bundle dispatch-fail của lượt retry chạy qua cùng đường delivery (callback + nhả lease) trước khi return. Terminal hóa chuyển thành **vòng lô applier**: chạy callback ngoài lock → lấy lock gom người nhận đến muộn (dedup lúc completing) → chỉ chốt terminal + pop + notify khi **hết người nhận**; `wait_all` (đang đếm job trong `_jobs`, phase `completing` vẫn tính) chỉ trả True sau khi TẤT CẢ applier đã chạy; lease nhả sau cùng.
**Test:** attempt-1 lỗi tạm thời + submit attempt-2 ném exception → callback `dispatch_failed` phải về, `job_fn` chạy đúng 1 lần, lease 0; dedup đến lúc completing + callback đầu kẹt Event → `wait_all(0.2)` False, thả Event mới True, native chạy đúng 1 lần. (`test_retry_dispatch_failure_still_delivers_callback`, `test_dedup_late_callback_holds_drain`)

### P1-D — nghĩa vụ retry độc lập thao tác thay matches (`secret_file_scan_screen.py`)
Nhánh replace: sau `commit_replace_file` (vốn xóa rel khỏi rescan_pending), nếu `cache_final=False` thì **thêm lại ngay** `set_rescan_pending(union)`. Cả hai nhánh giờ giữ nghĩa vụ retry khi metadata chưa hoàn tất.
**Test:** chuỗi 3 lượt (lỗi → lỗi → thành công) reload journal/registry giữa các lượt: 2 lượt đầu `rescan_pending` giữ file + journal không bị discard; lượt 3 gỡ nghĩa vụ + registry cache-final. (Xem §4 về hình thức chạy test.)

### P1-E — gate vòng đời lượt (`secret_file_scan_screen.py`)
Mỗi lượt có `run_gate = {"open": True}` + `run_gate_lock` (vừa là cờ vừa là **barrier**): `_commit_entry` chạy commit **trong** `run_gate_lock` — lượt đóng (finally) phải chờ commit đang bay xong mới `run_gate["open"]=False` rồi mới save/close journal; callback muộn sau điểm đó chỉ được **bỏ** (entry đánh dấu bỏ, không chạm journal/UI). Thứ tự khóa thống nhất gate→state, không có chiều ngược.
**Test:** hủy giữa chừng (drain bị bỏ qua) → gọi `on_applied` 2 lần sau khi lượt đóng → không phát thêm hàng, journal không có dòng mới, gọi lặp an toàn. (Xem §4.)

### P1-F — limiter fail-closed, recovery là đường duy nhất mở lại (`direct_engine.py`)
Hết hạn xin permit → **raise `LoadPermitTimeout`** (không nạp ngoài budget). OCR task trả sentinel `worker_init_failed` (nguyên nhân permit) → collector/`ocr_one_page` kích hoạt `_request_pool_recovery` — terminate là thứ **chứng minh holder dừng**; generation mới có limiter mới; thang retry 3 lần hữu hạn. Warmup task cùng đường: ACK failed với lỗi permit → supervisor đếm failed → deadline → recovery. Bất biến ≤k không bao giờ bị phá để "tránh chờ".
**Test:** held-semaphore + timeout 0,05 s → `_ensure_worker_loaded` raise `LoadPermitTimeout` (<2 s, không treo); `_warmup_task` ACK `ok=False` với lỗi permit. (`test_load_permit_timeout_fails_closed`)

### P2-G — thả artifact sau commit
`_commit_entry` thành công → `entry["artifact"]=None`, `entry["log"]=None`, remove khỏi `pending_commits` (under `state_lock`). Entry và callback không còn giữ artifact/canonical tới cuối lượt. Đã test gián tiếp qua chuỗi P1-D; **chưa có weakref/high-water đo giữa run** — kê khai §6.

### P2-H — fingerprint trước khi đọc
`pre_stat` chụp **trước** `scan_one_file_for_secret_artifact`; `post_stat` sau scan; `pre != post` → `source_unstable` → commit **không cache** + rescan (kết quả trộn phiên bản không được hợp thức hóa). Commit tiếp tục so `pre_stat` với stat hiện tại (đổi giữa chờ commit → không cache). Helper `_deferred_cache_final(artifact, path, scan_stat, source_unstable)` mở rộng + test riêng.

### P2-I — commit bền tại mốc, lỗi ghi đĩa không mất entry
`commit_artifact` kết thúc bằng `prog.save()` + `registry.save()` (flush+fsync) **trong `state_lock`** — commit muộn từ callback bền ngay, không chờ close. Entry có state `committing/committed/error`: `committed` chỉ đặt **sau** commit thành công; lỗi → `committing=False`, entry ở lại, **drain cuối lượt thử lại**; trước `discard` có barrier `registry.save()` (lỗi → giữ journal + log). Trước khi đóng lượt: `run_gate_lock` đóng gate sau khi commit đang bay xong (P1-E) → không có dữ liệu ghi sau fsync cuối.

### Kèm theo (mục 4 review)
- **prep nhận context hủy thật**: `_preprocess_pdf_for_ocr(..., cancel_event=)` → `run_prep(..., cancel_event=)`; `run_prep` chờ future theo **lát 0,5 s** — Stop sau khi job bắt đầu được phản ứng (fut.cancel được nếu chưa chạy; nếu đang chạy → đường timeout-defer giữ lease tới native xong).
- `cancel_run` lỗi được log loud (P1-B phụ).
- `process_one` tự hoàn thiện `kie_state` trần (chỉ hứa `applied`) — fix KeyError 'lock' phát hiện khi viết test.

## 3. Những điểm KHÔNG sửa — phản hồi có lập luận

1. **"Credit đầy fallback KIE đồng bộ trong parent" (§5, K bật thử nghiệm)** — đúng và đây là **thiết kế**: từ chối nhận là fallback an toàn đã log, không phải quên; loại bỏ nó đòi hỏi hàng đợi có ràng buộc + backpressure phức tạp hơn — để GĐ đo đạc quyết định.
2. **Dedup key path+ordinals+model_fp rỗng** — đúng còn thiếu; sửa đòi hỏi canonical/model fingerprint ở GĐ durable spool (P2-4) — không patch lẻ trong lượt này.
3. **`_run_callbacks` nuốt exception rồi vẫn terminal** — đã nhận diện; sửa đầy đủ đòi hỏi protocol applier-ACK hai chiều (journal v3). Ở mức này: lỗi commit đã có nghĩa vụ retry (P2-I) và lỗi callback vẫn log loud; **chưa** đưa callback-error vào trạng thái hoàn tất — kê khai §6.
4. **Incarnation init_seq/re-ACK cho respawn** — đúng còn thiếu; gate vẫn ramp theo ACK lịch sử. Đã kê khai từ report trước, giữ nguyên.

## 4. Ghi nhận kỹ thuật về hình thức test worker-level (minh bạch)

Hai kịch bản worker-level (P1-D chuỗi 3 lượt, P1-E late-commit) **không chạy được in-process trong pytest**: process bị native abort (SIGABRT, faulthandler dump: `Fatal Python error: Aborted`) ngay trong `dataclasses.asdict` trên pool thread ở lượt commit thứ 3 — **chỉ trong môi trường pytest**; cùng đúng kịch bản chạy trực tiếp bằng python thuần: 3/3 sạch. Chưa xác định được thủ phạm gốc (PySide/threads/deepcopy trong pytest-env — ngoài phạm vi sửa này). Xử lý: hai kịch bản chạy qua **subprocess** (`sys.executable -c`, QT offscreen, kịch bản tự assert, exit 0 = đạt) — cô lập như app thật, đúng cách chuyên gia đã dùng cho probe. Chuỗi lượt: metadata-lỗi → metadata-lỗi → metadata-đủ, reload journal/registry giữa các lượt; P1-E: hủy giữa chừng → callback muộn ×2 → không hàng mới, không dòng journal mới, file chưa "ok" (nghĩa vụ quét lại tự nhiên).

Ngoài ra khi chạy regression phát hiện 2 fake test phải cập nhật theo hợp đồng mới (kwarg `cancel_event` của `_preprocess_pdf_for_ocr`; semantics credit lượt trước).

## 5. Kết quả test

- W **15/15**, K **24/24**, P **9/9** (W/K/P chạy lặp lại nhiều lần — ổn định).
- versioned_resume **23/23** (gồm 2 test subprocess mới); secret-scan modes/declass/detection **đã pass** (sau sửa 1 fake kwarg).
- Nhóm regression liên quan (`secret|ocr|warmup|kie|prep|admission|scan`): **361/361** (346 s).
- **Full suite: 831 passed / 0 failed** (733,6 s ≈ 12 phút; 821 trước + 10 test mới — W +1, K +7, resume +2, trừ semantics đổi 1 — đã đối chiếu với số test từng file: W 15, K 24, P 9, versioned_resume 23).

## 6. Việc còn lại (kê khai trung thực — không đổi so với review)

1. **RAM high-water giữa run** (P2-G): mechanism đóng (pop + drop ref) nhưng chưa đo weakref/high-water trên corpus dài; chưa có số RSS.
2. **Applier-ACK protocol** (mục 4): callback/persist lỗi chưa đưa vào trạng thái hoàn tất hai trục — thuộc journal v3.
3. **Dedup fingerprint** (model_fp/canonical hash) — thuộc GĐ durable spool.
4. **Native K/P treo**: prep có window-deadline + cancel + sliced-wait; KIE có drain timeout + cancel theo lượt; **chưa có kill riêng theo job**, `shutdown(wait=False)` vẫn chưa chứng minh process dừng — cần đúng nút Stop/app-close trên Windows + frozen.
5. **Incarnation re-ACK/deadline theo pha** — như các kê khai trước.
6. **Benchmark máy đích** — chưa làm; đồng ý hoàn toàn với §5/§6 của review: không có cơ sở tuyên bố "nhanh hơn" trước khi đo A/B đầu-cuối theo ma trận đề nghị (baseline/candidate riêng, cold/warm, ≥3 lượt, soak vượt recycle, thời gian đến durable-metadata-commit cuối làm mốc nghiệm thu).

## 7. Verdict đề xuất cập nhật

| Phần | Sau lượt sửa này |
|---|---|
| Correctness (P1-A→E) | Đã đóng với test chỉ đích từng probe (kể cả probe của chuyên gia: tuple-arity, retry-mất-callback, dedup-muộn, replace-mất-nghĩa-vụ, late-commit). |
| Nguồn (P1-F) | Fail-closed + recovery hữu hạn; test fail-closed. Còn: thu hồi theo xác nhận process/generation dừng (recovery hiện dùng terminate — vốn là bằng chứng dừng cho generation bị khai tử). |
| Persistence (P2-I) | Commit fsync tại mốc + retry entry lỗi + barrier trước discard. |
| K/P | Vẫn OFF; đủ điều kiện pilot có theo dõi sau regression OFF/ON + Windows/frozen smoke (chưa chạy). |
| W | Vẫn chạy mặc định; điểm còn mở: incarnation re-ACK, deadline theo pha — đã kê khai. |
| B | Chưa làm — chỉ nghiệm thu tốc độ sau khi đo theo ma trận §6 của review. |
