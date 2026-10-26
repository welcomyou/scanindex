# FIX REPORT — trả lời REVIEW implementation W/K/P (22/10/2026)

> **Trạng thái: ĐÃ SỬA THEO REVIEW + ĐÃ QUA TEST.** Cả 10 lỗi P1 của chuyên gia
> đều **đúng** và đã được sửa (P1-01 → P1-09), kèm 4/8 điểm P2 xử lý tại chỗ và
> các điểm còn lại kê khai trung thực ở §5. K/P vẫn mặc định **OFF**. Full suite
> process sạch: **821 passed / 0 failed** (11 phút 58 s — 801 cũ không vỡ + 20
> test mới). Smoke DLL/model thật: **PASS** (§4). **GĐ B vẫn chưa làm** — không
> có suy diễn hiệu năng máy đích từ bản này.

Đối tượng trả lời: [REVIEW_IMPLEMENTATION_wkp_2026-10-22.md](D:/App/ocrtool/REVIEW_IMPLEMENTATION_wkp_2026-10-22.md).
Mã đã sửa (SHA-256 đầu…cuối, sau lượt này): `direct_engine.py`
`8BA87E9458A5D420…A8907ECF`, `secret_scan_kie.py` `E45BDD2E5D14566C…16DFA7F8`,
`prep_worker.py` `2F8397A36C2519D6…8DBAFB40`,
`secret_file_scan_screen.py` `EA3C64ECAAFA2CD6…023889EA`.

## 1. Xác minh review — chuyên gia ĐÚNG hay KHÔNG?

**Đúng cả 10/10 P1.** Tôi tự xác minh bằng đọc mã (probe gốc
`temp/secret_scan_perf_review/` không còn trên đĩa máy này — không chạy lại
được, mọi kết luận dưới đây dựa trên mã và probe mới viết lại):

| ID | Xác minh | Bằng chứng trong mã (trước sửa) |
|---|---|---|
| P1-01 | **Đúng** | `_on_warmup_ack` tính `remaining = N − in_flight − ready − failed` rồi post **toàn bộ** — N=8,k=2: ACK đầu post 6 task, in_flight=7. `_ensure_worker_loaded()` (self-load OCR đầu + recycle) không xin permit nào. |
| P1-02 | **Đúng** | ACK không có generation (callback đối chiếu `_warmup["generation"]` là state toàn cục vừa bị update thành thế hệ mới); `acked_pids` chỉ thêm không lọc chết; deadline partial-ready đặt `done=True` → ACK muộn bị bỏ (mâu thuẫn log "tự phục hồi"); `multiprocessing.Pool(processes=_NUM_PAGE_WORKERS)` luôn dựng N process bất chấp ladder chọn effective=4. |
| P1-03 | **Đúng** | `shutdown_pool()`: terminate/join ném lỗi → `except: pass` → `_mp_pool = None` → nhả **toàn bộ** lease `shutdown_ids` với `release_resources=True`. (`_request_pool_recovery` đúng — chỉ shutdown thiếu.) |
| P1-04 | **Đúng** | `run_prep`: `fut.result(timeout)` hết hạn ném TimeoutError nhưng future vẫn chạy; `finally` nhả cả 2 lease + slot window; caller (`_preprocess_pdf_for_ocr`) nhận handled=False rồi ghi **cùng** `out_pdf` → hai writer một file. Comment trong mã mô tả ngược hành vi thật. |
| P1-05 | **Đúng** | `cancel_all` settle mọi job chưa terminal + nhả lease ngay; comment tự thừa nhận "future đang chạy KHÔNG bị tuyên bố dừng" — caller outcome bị dùng làm native lifecycle. |
| P1-06 | **Đúng** | (1) nhánh `except` trong `_dispatch_locked` settle + chạy callback nhưng **không gọi `_release_lease`**; (2) `submit` acquire lease SAU khi nhả `_cond` (dòng 279 cũ) → cửa sổ race với cancel, job entry=0 nhưng lease=1 vĩnh viễn; (3) retry nộp lại vào đúng executor hỏng, không có cơ chế dựng lại có giới hạn. |
| P1-07 | **Đúng** | `drain()==False` chỉ tạo 1 log info; sau đó vòng lặp commit **tất cả** pending như thường; `registry.record` ghi cache-final với `os.stat` **tại thời điểm commit** → lượt sau cache-hit kết quả thiếu metadata. |
| P1-08 | **Đúng** | Deferred accepted → cả pass cơ quan lẫn pass năm bị bỏ; callback error chỉ log "giữ regex/note cũ" trong khi chưa pass regex nào chạy; job được tạo cả khi không trang nào cần model (không giữ điều kiện skip của 2 pass). |
| P1-09 | **Đúng** | `submit` không có kiểm outstanding — credit chỉ chặn ở dispatch (`running >= max`); docstring của chính module hứa "CHƯA terminal (queued+running)" nhưng mã chỉ đếm running; test cũ asserting "job 3 vào queue" đúng là kiểm semantics khác plan; entry terminal giữ tới khi *mọi* job terminal (`_jobs.clear()` theo điều kiện all-terminal). |
| P1-10 | **Đúng** | `_on_future_done` và `cancel_all` chạy callback + `_release_lease` **giữ `_cond`**; callback scanner có thể quay lại infer trong parent qua `_apply_kie_org_fallback` → cache-miss → `_kie_fields_for_page` infer → `wait_all` không trả đúng hạn. Ngoài ra khi sửa tôi phát hiện thêm: `threading.Condition()` mặc định RLock + future hoàn thành nhanh chạy done-callback **đồng bộ trong `add_done_callback` lúc còn giữ lock** — tức callback còn có thể chạy dưới lock ngay cả ở happy path (đã sửa, xem §2.6). |

Điểm P2 xác minh nhanh: P2-1 đúng (`set_capacity=max(held,target)` nuốt target thấp); P2-2 đúng (`_post_warmup_tasks_locked` gọi `_on_warmup_error` trong except → lấy lại `_warmup_lock` non-reentrant = deadlock; `OCRTOOL_WARMUP_STAGE=0` bị `_env_int` ép thành 1); P2-5 đúng (stat lúc commit ≠ lúc đọc; fsync thì journal/registry **đã có sẵn** — không phải lỗi); P2-6 đúng (W test gán `_janitor_enabled=False` module-level không restore — đã sửa); P2-7 đúng (prep `in_flight` đọc `_value` semaphore là slot rảnh; KIE `awaiting_commit` hard-code 0; `ready_live` không live).

## 2. Đã sửa gì — theo đúng thứ tự đề nghị §5 của review

### 2.1. Chặn cleanup sớm (P1-03, P1-04, P1-05, P1-06)

- **`shutdown_pool()`** (direct_engine): tách `terminate_ok` — terminate/join ném lỗi → KHÔNG nhả lease (token giữ, `foreign_charged` trừ budget gate thế hệ mới như recovery-failure), janitor thu khi `ar.ready`. Logic reserve budget trích thành `_reserve_foreign_budget_locked()` dùng chung với recovery.
- **`run_prep()`** (prep_worker): native chỉ ghi **staging riêng** (`dst.prep-<uuid>.part`), publish bằng `os.replace` chỉ khi future thành công. Timeout/cancel của vòng chờ KHÔNG nhả gì — đăng ký done-callback dọn lease + window + staging **sau khi future thật kết thúc** (`_JobResources.defer_until_future_done`). `window.acquire` có deadline (`OCRTOOL_PREP_WINDOW_WAIT_S`, mặc định 120 s) + nhận `cancel_event`. Caller inline ghi `out_pdf` an toàn vì native không bao giờ đụng file đó.
- **Cancel KIE** (secret_scan_kie): job future đang chạy → cancel KHÔNG settle, KHÔNG nhả lease — ghi `cancel_result`, `_on_future_done` chốt (cancelled) + nhả lease khi native thật kết thúc. Job chưa chạy (`fut.cancel()` thành công) hoặc queued → settle sạch ngay.
- **Submit/dispatch** (secret_scan_kie): lease acquire **TRƯỚC khi publish**, refcount theo job (`lease_refs`), mọi nhánh từ chối/exception nhả lại đúng ref; dispatch-fail settle kèm release ở caller; `BrokenProcessPool` (exception thật) → hủy executor + dựng lại **tối đa 2 lần** (`_MAX_EXECUTOR_REBUILDS`), hết lượt → `executor_dead` → từ chối nhận mới (caller giữ đường đồng bộ), job chờ settle lỗi có nghĩa vụ retry.
- **Luồng hủy theo lượt** (P2-3): `cancel_run(run_id)` — chỉ hủy job của lượt (theo `payload["run_id"]`), không ngừng nhận của coordinator, không đụng lượt khác. Scanner gắn `run_id` cho từng lượt (truyền qua thread-local `_scan_run_ctx` — **không đổi chữ ký public** của `scan_one_file_for_secret_artifact` để giữ tương thích monkeypatch theo lời gọi 6 tham số vị trí) và gọi `cancel_run` trong `finally` khi lượt bị dừng.

### 2.2. Sửa W chung (P1-01, P1-02, P1-03, P2-1, P2-2)

- **Staging k giữ nguyên suốt warmup**: `_repost_or_finish_locked()` — sau mỗi ACK/error chỉ post **tối đa một cửa sổ k**; đồng thời mọi đường nạp giờ đi qua **một permit liên process duy nhất** (`BoundedSemaphore(k)` cấp theo generation qua initializer, kế thừa cho worker respawn): warmup task, self-load OCR đầu, recycle respawn đều acquire permit (timeout 300 s fail-open — permit rò làm limiter thoái hóa thành no-op chứ không treo worker, đúng bài học probe 21/10). Mức nạp đồng thời quan sát được qua `Value` probe + telemetry `warmup_loads_in_flight`.
- **ACK gắn generation tại lúc post** (closure bind + payload trả `generation`); sổ ACK đổi thành dict theo pid (incarnation) + `_live_worker_pids()` (đọc `pool._pool`) → `ready_live_workers` = ACK ∩ PID đang sống; **deadline partial-ready đặt `degraded`** — ACK muộn vẫn ramp gate (không post thêm), shutdown/recovery mới đóng sổ hẳn; **Pool dựng với effective size** (`processes=requested` sau ladder — retry 2 của N=8 là 4 process).
- **Gate** (P2-1): `set_capacity` ghi `_target` riêng; admission so `held` với `_target` — shrink không rút token sống và không mở lại slot sai; `capacity` = max(held, target) cho telemetry.
- **Deadlock `_on_warmup_error`** (P2-2): post-fail đồng bộ xử lý nội bộ trong lock, không re-enter; `OCRTOOL_WARMUP_STAGE=0` giờ thật sự tắt staging (k=N).
- Deadline theo pha/đợt (queue-wait, stage index trong ACK) **chưa làm** — kê khai §5. Policy với native treo: 0-ready → quarantine+recovery (terminate là thứ dừng thật); partial-ready → degrade + ACK muộn ramp; worker init treo chưa bị dừng riêng.

### 2.3. Sửa trạng thái K (P1-08, P1-10)

- **Baseline trước enqueue + planner giữ điều kiện skip**: `_kie_needed_pages()` tính hợp các trang thực sự cần model (org-pass bật ∧ mã không tra được ∧ chưa có cache; year-pass bật ∧ ≥3 ứng viên ngày ∧ chưa có cache). Không trang nào cần → trả None → **không tạo job**, đường đồng bộ tự xử. Có cần → áp baseline **pure-regex** (`allow_model=False`) rồi mới enqueue đúng các trang đó. Callback chỉ đọc **cache** (không bao giờ infer trong parent — cửa KIE-đồng-bộ-ngược bị đóng); nhánh ready áp fields từ cache và nâng cấp baseline (ghi chú giải mật strip-then-apply — idempotent, không đúp note); nhánh error giữ nguyên baseline (= kết quả đường sync khi model lỗi).
- **Callback ra ngoài lock**: job 4 pha `queued → running → completing → terminal`; applier chạy SAU khi nhả `_cond`; terminal chỉ chốt sau khi applier xong → hợp đồng "drain True = metadata đã áp" giữ nguyên, còn "drain False" giờ là tín hiệu thật (P1-07 tiêu thụ). Done-callback của future đăng ký **ngoài lock** (chống happy-path lock-held do RLock). `wait_all` bỏ nhánh thoát-sớm theo `_shutting_down` — job native đang chạy phải được chờ kết thúc thật.
- Lưu ý phát hiện khi sửa (bổ sung cho review): thiết kế cũ có **hai** đường callback-dưới-lock (giữ `_cond` tường minh, và add_done_callback đồng bộ trong lock qua RLock) — cả hai đã đóng.

### 2.4. Bounded end-to-end (P1-07, P1-09, P2-5)

- **Credit bao toàn queue**: `submit` đếm `len(_jobs)` (chỉ chứa job chưa terminal = queued+running+completing) — vượt `2×Pkie` → từ chối nhận, log lý do, caller giữ đường đồng bộ. Entry terminal **pop ngay** khi settle (RAM không tăng theo kho; dedup chỉ áp dụng job chưa terminal — đúng mục đích dedup).
- **Commit theo ACK từng file**: entry pending đăng ký `on_applied` → callback deferred commit ngay từ thread coordinator (idempotent qua cờ per-entry, có khóa); drain cuối lượt chỉ còn phần callback chưa kịp về. Artifact/canonical không còn giữ tới cuối lượt.
- **Drain timeout KHÔNG cache-final** (P1-07): `_deferred_cache_final(artifact, path, scan_stat)` — metadata chưa `applied` → KHÔNG ghi registry + **đưa rel vào `rescan_pending`** (journal giữ, lượt sau quét lại; hàng UI vẫn phát nhưng được log loud "KHÔNG dùng làm cache").
- **Fingerprint** (P2.5): stat nguồn chụp **ngay sau quét** (`scan_stat`), commit so lại hiện tại — lệch → không cache (`source-changed`), log rõ.

### 2.5. Telemetry (P2-7, phần sửa được)

- `ready_live_workers` = ACK sống thật (P1-02); `prep.in_flight` = counter đếm job chạy thật (`window_free` tách riêng); KIE `awaiting_commit` = số job pha `completing` thật; thêm `outstanding`, `executor_rebuilds`, `executor_dead`, `warmup_loads_in_flight`.

### 2.6. Test (P2-6)

20 test mới/cập nhật:
- `test_warmup_supervisor_w.py` (6 → **14**): gate shrink giữ target; stale-ACK thế hệ cũ bị bỏ; degraded nhận ACK muộn ramp gate; `ready_live` chỉ đếm PID sống; shutdown terminate-fail GIỮ lease + foreign_charged + janitor thu sau khi native xong; **Pool dựng 4 process sau ladder của N=8**; **load-limiter k=2 chặn mức nạp đồng thời với Pool thật, có OCR xen giữa warmup và recycle** (`maxtasksperchild=1`); `_janitor_enabled` chuyển sang fixture autouse có restore.
- `test_kie_coordinator_k.py` (9 → **17**): credit từ chối job vượt (thay semantics cũ); cancel giữ lease khi native chạy + nhả sau khi thật xong; dispatch-fail nhả lease; BrokenProcessPool dựng lại có giới hạn rồi từ chối nhận mới (assert số lần nộp ≤ 4); `wait_all` trả đúng deadline dù callback kẹt (chạy từ thread khác); `cancel_run` chỉ hủy đúng lượt, không ngừng nhận; planner bỏ trang không cần model; model_missing giữ baseline regex parity với sync; ready nâng cấp năm KIE + không đúp note; quyết định cache-final (metadata-pending / source-changed / đủ điều kiện).
- `test_prep_process_p.py` (5 → **9**): timeout giữ lease tới terminal + native không clobber output inline + staging dọn sạch; deadline cửa sổ; `cancel_event`; telemetry `in_flight` đếm chạy thật.
- Test cũ `test_execution_credit_limits_outstanding` **đã đổi semantics** theo plan (job 3 bị từ chối — đúng điểm review chỉ ra test cũ "đang kiểm chứng semantics khác plan").

## 3. Kết quả test

- `tests/test_warmup_supervisor_w.py` — **14/14**; `tests/test_kie_coordinator_k.py` — **17/17**; `tests/test_prep_process_p.py` — **9/9** (chạy lại 3 lần để kiểm flakiness — ổn định).
- Regression liên quan: admission v4 + R-fixes **38/38**; secret-scan (declass/modes/detection/versioned-resume) **54/54**; toàn bộ nhóm `secret|ocr|warmup|kie|prep|admission|scan` **351/351**.
- **Full suite process sạch: 821 passed / 0 failed** (718,5 s — bao gồm 781 test cũ không vỡ + 40 test W/K/P). Lượt đầu full-suite có 6 test versioned-resume fail do tôi truyền `run_id` qua tham số làm vỡ tương thích fake 6-tham-số — đã chuyển sang thread-local và suite xanh lại (bài học: giữ đúng hợp đồng "lời gọi 6 tham số vị trí" mà mã cũ đã cố tình bảo vệ).

## 4. Smoke DLL/model thật (end-to-end)

- **Smoke W** — `temp/wkp_fix_2026-10-22/smoke_warmup_w.py` (DLL `chrome_screen_ai.dll` 148.13 thật, 4 worker, k=2): pool published t=0,8 s (4 process, eff=4); ACK timeline **[0, 2, 4]** — ramp đúng cửa sổ k=2; **PEAK nạp đồng thời = 2** (limiter giữ k suốt, không chỉ đợt đầu); OCR thật qua worker đã warm trả đúng chữ. Có main-guard (bài học 22/10).
- **Smoke K** — `temp/wkp_fix_2026-10-22/smoke_kie_deferred.py` (ProcessPoolExecutor **spawn thật** + model LayoutLM thật): sync vs deferred **parity metadata** (issue_year 2010 = 2010, declass_due, ghi chú giải mật); `drain=True` ⇔ metadata đã áp; telemetry sau drain `outstanding=0`. Ghi nhận môi trường: máy dev Pkie=1 (cpu_count < 16), LightGBM import lỗi trong harness script (không phải sản phẩm — app thật có hạ tầng import riêng) nên note chứa chuỗi lỗi import khác nhau giữa 2 lượt; smoke so phần metadata, không so nguyên chuỗi note.
- **Smoke P với timeout thật** chưa chạy trên ProcessPool thật (test dùng ThreadPool inject) — kê khai §5.

## 5. Việc còn lại (kê khai trung thực — không tuyên bố phần chưa làm)

1. **Deadline warmup theo pha/đợt** (P2-2): ACK chưa mang queue-wait/stage/init_seq; timer vẫn 120 s toàn cục thay vì ceil(N/k)×t_load. Ladder, staging, generation-bind, effective size, liveness — đã xong.
2. **Native-job deadline/recovery đầy đủ cho K/P** (P2-3): prep có window-deadline + cancel_event; KIE có drain timeout + cancel theo lượt; nhưng chưa có cơ chế **giết riêng** một native job treo (executor-level), `shutdown(wait=False)` chưa chứng minh process dừng.
3. **Durable spool/journal v3 hai trục + UI metadata-revision + Excel pending + resume KIE-only** (P2-4): chưa làm — giữ nguyên như kê khai của báo cáo trước. Ở nhánh timeout hiện tại: hàng UI vẫn phát, KHÔNG cache-final, file vào `rescan_pending` → lượt sau quét lại (an toàn, tốn công OCR lại).
4. **Đo high-water RAM/count trên corpus dài** (P1-09 phần "producer nhanh hơn consumer"): cơ chế đã đóng ở mức credit + commit-theo-ACK + pop-entry-terminal; chưa có benchmark số liệu.
5. **Soak/benchmark máy đích 64 CPU/128 GB (GĐ B)**: chưa làm. K/P vẫn OFF; W chạy mặc định trên pool chung — cần regression + smoke Windows/frozen trước khi coi W hoàn tất (đúng mục 4/§4 review).
6. Disk-full trên publish prep (`os.replace` lỗi) đi vào nhánh handled=False (fallback inline) nhưng chưa có test riêng.
7. Telemetry còn thiếu: busy estimate chưa tích hợp effective capacity theo lượt; timeline producer-idle/lock-wait chưa có schema snapshot.

## 6. Thay đổi mã — để đối chiếu

| File | Nội dung chính |
|---|---|
| `scanindex/core/ocr/direct_engine.py` | Warmup supervisor: staging window, ACK generation-bind, sổ incarnation + liveness, degraded-ramp, effective pool size, load limiter (sem+probe qua initializer), `_repost_or_finish_locked` (không re-enter lock), gate `_target`, `shutdown_pool` terminate_ok + reserve budget. |
| `scanindex/core/secret_scan_kie.py` | Viết lại coordinator: job 4 pha, credit bao toàn queue, refcount lease, acquire-trước-publish, dispatch-fail/broken-pool có bounds + release, cancel giữ lease native + `cancel_run`, callback ngoài lock (kể cả add_done_callback), pop entry terminal, telemetry thật. |
| `scanindex/core/preprocessing/prep_worker.py` | Staging output + publish có điều kiện, `_JobResources` (lease theo native job, defer-cleanup qua done-callback), window deadline + cancel_event, telemetry in_flight thật, hook `_executor_factory` cho test. |
| `scanindex/ui/screens/secret_file_scan_screen.py` | `_kie_needed_pages` (planner giữ skip), baseline `allow_model=False` trước enqueue, callback chỉ đọc cache, `_apply_declassification_notes` idempotent + `allow_model`, `SecretScanArtifact.kie_state`, commit-theo-ACK + `_deferred_cache_final` (P1-07/P2.5), `rescan_pending` cho file thiếu metadata, `cancel_run` theo lượt (run_id thread-local). |

## 7. Đề xuất verdict cập nhật (để chuyên gia đối chiếu)

| Phần | Đề xuất verdict sau lượt sửa này |
|---|---|
| W | Các P1 đã đóng với test + smoke DLL thật (peak-load ≤ k, ACK theo generation, liveness, effective size, shutdown terminate-fail giữ lease). Còn: deadline theo pha. Đề xuất: **đủ điều kiện đưa vào regression rộng + Windows/frozen smoke** trước khi coi hoàn tất. |
| K | P1 đã đóng ở mức prototype (đang OFF). Còn: durable pending/journal v3/UI (K1), native-job kill. Đề xuất: giữ **OFF**, cho phép bật thử nghiệm có kiểm soát khi smoke UI từng nhánh đã chạy. |
| P | P1 đã đóng ở mức prototype (đang OFF). Còn: đo A/B, disk-full test, resource budget ảnh. Đề xuất: giữ **OFF**. |
| R | shutdown terminate-failure đã giữ lease; janitor thu khi `ar.ready`. Đề xuất: regression + chaos probe lại các cửa sổ cancel/dispatch như review đề nghị để xác nhận lease cuối = 0. |
| B | Chưa làm — giữ nguyên trạng thái chưa nghiệm thu hiệu năng máy đích. |
