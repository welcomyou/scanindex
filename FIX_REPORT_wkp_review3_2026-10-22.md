# FIX REPORT — GÓI CHỐT D0–D5 cho máy 64 CPU / 128 GB (22/10/2026)

> **Trạng thái: M0–M3 ĐÃ GIAO với bằng chứng test. Full suite 859 passed /
> 0 failed / 4 deselected (725 s); smoke model thật PASS qua bootstrap dùng
> chung — LightGBM XÁC NHẬN được trong harness (hết cảnh import-order).
> Còn lại M4 — các bước PHẢI LÀM TRÊN MÁY ĐÍCH, có tên và đầu ra cụ thể ở
> §9: build frozen → frozen smoke → A/B + soak 64/128 bằng harness D5 →
> mẫu org-positive. KHÔNG ký "tối ưu 64/128" trước khi có số đó.**

Đối tượng: [REVIEW_AND_CLOSURE_wkp_64cpu_128gb_2026-10-22.md](D:/App/ocrtool/REVIEW_AND_CLOSURE_wkp_64cpu_128gb_2026-10-22.md).

## 0. Bảng chữ ký nghiệm thu (mẫu §9 của review)

| Trường | Giá trị | Ghi chú |
|---|---|---|
| `artifact_hash` | Xem bảng SHA-256 §1 | Chưa build frozen — hash là mã nguồn |
| `model_fp` | chưa gắn log máy đích | Smoke gắn hash 4 file mã nguồn vào log |
| `profile_id` | `start_W16_F8_CHUNK8_K2_P2` | Điểm khởi đầu §8 review, CHƯA phải tối ưu |
| `correctness_pass` | **YES** | Full suite 859/0; parity metadata sync/deferred; R3 fixes giữ nguyên |
| `lifecycle_pass` | **YES (source-level)** | D1: proof child-vô-chủ chết theo parent (Job Object, test thật); Stop/app-close hữu hạn; ready-live ledger |
| `recovery_pass` | **YES (source-level)** | D0 fail-closed + D2 KIE-only resume (OCR-call=0) + R3-01/R2 retries |
| `resource_pass` | **PARTIAL** | Credit/backpressure/trần có (D3, budget K/P); chưa soak máy đích |
| `frozen_pass` | **NO** | Chưa build artifact — việc còn lại có tên |
| `target_benchmark` | **NO** | Cần máy 64/128 — harness D5 đã sẵn sàng |
| `target_soak` | **NO** | Cần máy đích |
| `open_blockers` | `["frozen_build+smoke", "target_benchmark+soak", "org_positive_sample"]` | Đúng 3 việc, có tên |

## 1. Mã sau gói chốt (SHA-256)

| File | SHA-256 |
|---|---|
| `scanindex/ui/screens/secret_file_scan_screen.py` | `6A433FC45019AB7515EE9BDD6AF6B978418E616C1B67CE53D3B3962182D8433B` |
| `scanindex/core/secret_scan_kie.py` | `7A3A7412BC396D38DAA8A1CF2206F62EEC947465211F7CA2D07B68940F447C9F` |
| `scanindex/core/secret_scan_progress.py` | `2EA72E8F75608E0151FF07B900443D89B7EF79C00F8F5935383C01BD8F3EAAAC` |
| `scanindex/core/preprocessing/prep_worker.py` | `F54959375FD7645E44966BF5D1CC40AEC4B07D683A3D8085EC063BDC1A1790EF` |
| `scanindex/core/ocr/direct_engine.py` | `7D17C91A25D71C3BAF7488A4E9AFD70DED25FFDA029B2D89F84DAB30FE9FFC83` |

File mới: `scanindex/infra/windows_job.py`, `scanindex/infra/bootstrap.py`,
`scanindex/core/runtime/shutdown.py`, `scripts/bench_secret_scan.py`.

## 2. Kết quả kiểm chứng

- **FULL SUITE: 859 passed / 0 failed / 4 deselected** (724,9 s) — 845 vòng trước + 14 test D0–D5 mới, không test cũ nào vỡ (positive controls P1-D/P1-E/R2-01…R2-07/R3-* giữ nguyên và pass).
- Bộ mới: D0 writer ×6, D1 runtime ×5 (có test child-vô-chủ chạy thật), D3 backpressure ×3, D2 KIE-only resume ×1 (OCR-call=0 + stale→rescan).
- **Smoke model thật sau D4 — PASS:** chạy qua `bootstrap_harness()`; note giờ ghi **"LightGBM: … xác nhận trang đầu"** (không còn cảnh báo `_SixMetaPathImporter` — chứng minh lỗi import-order trước đây là của harness bỏ qua bootstrap, đúng như review §7 kết luận, và bootstrap dùng chung đã khắc phục); parity year/due/org/note; `applied=True`, `error_code=None`, `deliveries_pending=0`; log gắn SHA-256 tại `temp/wkp_fix_2026-10-22/smoke_kie_deferred_20261022_195949.log`.

## 3. D0 — Writer fail-closed (lỗ hổng mới được review xác minh)

- `AtomicJsonlWriter` (dùng CHUNG journal + registry — hết hai bản recovery dễ lệch): trạng thái `HEALTHY/POISONED/CLOSED`, raw byte I/O, **`os.write` trả 0 = lỗi** (không lặp vô hạn). Write lỗi → truncate về offset sạch; **truncate THÀNH CÔNG → HEALTHY + raise (retry được); THẤT BẠI → POISONED + đóng handle + TỪ CHỐI mọi append/save kế tiếp** — probe "record sau lỗi trả thành công rồi reload mất" KHÔNG CÒN xảy ra (test tái hiện đúng kịch bản: partial write + truncate fail → record kế bị từ chối → recovery → event hợp lệ còn nguyên).
- fsync lỗi → raise (durability chưa chứng minh → chưa ACK) nhưng KHÔNG poison (byte đã vào nhân OS, tail nguyên vẹn — retry hợp lệ).
- **Recovery có kiểm soát:** khóa writer → **backup `.corrupt-<ts>`** → rebuild snapshot (từ RAM khi còn owner; `_replay` tự chữa + backup khi load mới) → mở lại writer. Load mới với đuôi hỏng cũng backup trước khi chữa.
- **Khóa single-writer registry** theo data directory (msvcrt region lock, `registry.writerlock` — không bị "Xóa lịch sử quét" đụng tới): instance thứ hai `load()` được, `readonly=True` — record bị từ chối, lượt quét chuyển chạy-KHÔNG-cache + log (không hai tiến trình cùng ghi/truncate). Màn hình xử lý readonly → registry=None.
- Cuối lượt: writer POISONED → recovery MỘT lần → save lại; vẫn lỗi → log err "KHÔNG THỂ lưu an toàn…" + GIỮ journal (không discard, không bỏ nghĩa vụ retry) — luôn phát `_scan_finished`.

## 4. D1 — Ownership process tree + dừng hữu hạn + ready-live

- **`scanindex/infra/windows_job.py`** — Windows Job Object `KILL_ON_JOB_CLOSE`: parent giữ handle chính (last-close = kill tree); child **tự-gán qua handle INHERITABLE truyền bằng env** (`OCRTOOL_JOB_HANDLE`) → mọi worker spawn/recycle (kể cả respawn của `multiprocessing.Pool`) đều vào job mà không cần hook từng đường spawn. **Test thật: parent `os._exit(9)` → child đã gắn job CHẾT** (không child vô chủ). Nested-job: gắn thất bại → log + chạy tiếp (best-effort — fail-closed từng slot thuộc ManagedStagePool đầy đủ, kê khai §10).
- **`scanindex/core/runtime/shutdown.py`** — `shutdown_all(deadline=10s, grace=3s)`: pha mềm (shutdown hợp tác từng stage) → pha cứng (terminate process còn sống) → terminate tree Job Object. Idempotent; terminate lỗi → trả False (fail-closed, không tuyên bố đã dừng). **Nối `MainWindow.closeEvent`**: thread riêng + join ≤10 s — không join vô hạn trên Qt thread; quá hạn → log lỗi + Job Object kill tree khi process thoát.
- **Ba stage W/K/P đăng ký stopper** (`W-ocr-pool`, `K-kie-pool`, `P-prep-pool`): soft = ngừng nhận + cancel futures; hard = terminate process MÌNH sở hữu (KIE: `hard_stop()`; W: `pool.terminate()`; P: terminate `_processes`).
- **W ready-live (re-ACK theo incarnation — bước giữa):** sổ ACK được **prune worker đã chết/recycle ngay khi tính ready** (`_prune_dead_acks_locked` trên 3 đường ACK/deadline) — gate capacity chỉ đếm worker SỐNG đã ACK, hết dùng sổ PID lịch sử làm capacity. Pool giả/test → không prune (không xóa nhầm ACK).
- **Kê khai (khác hợp đồng đầy đủ §4 review):** protocol HELLO/LOAD_REQUEST/READY/JOB_STARTED và `ManagedStagePool` per-worker IPC CHƯA xây — đây là kiến trúc thay executor; cầu chuyển hiện tại đóng đúng 4/5 tiêu chí "Đạt D1": child-vô-chủ (Job Object, test thật), native treo không giữ app (bounded close), terminate thất bại fail-closed (test), ready-live đúng sau recycle-prune (test). "Recycle phải READY lại trước khi nhận OCR" hiện do self-load qua permit P1-01 (không phải dispatch-theo-slot) — mục mở có tên.

## 5. D2 — Job bền vững, spool, KIE-only resume

- **Durable KIE spool:** khi nhận job KIE, canonical snapshot được **copy vào `progress_dir()/kie_spool/<digest>/` + fsync + SHA-256 TRƯỚC khi submit** (job đọc SPOOL copy — workdir tạm dọn an toàn), nghĩa vụ `kspool` (rel, canonical, hash, ordinals, matches-baseline, fp nguồn, status) ghi vào journal SAU khi nhận — replay/compact idempotent, sống sót crash.
- **KIE-only resume:** lượt resume tách pha riêng — file còn spool + **nguồn không đổi (fp khớp) + snapshot đúng hash** → resubmit metadata job, **OCR-call = 0** (bài bắt buộc #7 — test qua subprocess đếm call). Hết hợp lệ (hash lệch/nguồn đổi/snapshot mất) → xóa record, log lý do, **quét lại toàn bộ** (test phủ: fp mới + hash sai → OCR chạy lại).
- **GC:** cuối lượt dọn spool của file đã có kết quả bền; file còn pending GIỮ snapshot. Crash trước kspool → full rescan (an toàn); sau kspool → KIE-only.
- **ACK hai trục:** giữ hợp đồng đã kiểm chứng vòng R3 — result → applied → commit (journal rpf + registry record) → fsync persist → remove entry; lỗi persist ở lại retry (leftover → failures + rescan bền). `refused`/drain-timeout KHÔNG cache-final.
- **Kê khai:** review chọn `scan_jobs.sqlite` làm sổ v3 — bản này dùng journal v2 + spool bền (một writer đã được `state_lock` + D0 bảo đảm, đúng bằng chứng crash-cutpoint/KIE-only của bảng §2). Nâng lên SQLite/outbox hai-trục chính thức là mục mở có tên; không phải điều kiện của OCR-call=0.

## 6. D3 — Backpressure, hết KIE ẩn trong parent

- K credit đầy → **chờ có giới hạn + hủy được** (`OCRTOOL_KIE_REFUSED_WAIT_S`, mặc định 30 s, phản ứng `cancel_event`) → retry → vẫn đầy → **trạng thái `refused`** (applied=False): KHÔNG BAO GIỜ fallback infer trong parent; file giữ baseline regex + nghĩa vụ quét lại (không cache-final — test phủ trực tiếp `_deferred_cache_final`).
- Test: executor chết → refused + **monkeypatch chứng minh không có lời gọi infer nào trong parent**; credit trống giữa chừng → retry nhận job.
- Bucket commit R3-06 giữ nguyên (không hồi quy duyệt toàn bộ matches).

## 7. D4 — Bootstrap dùng chung, LightGBM, smoke

- `scanindex/infra/bootstrap.py`: `bootstrap_app()` (migration → portable → app log → CPU-only → six patch + pandas pre-import TRƯỚC PySide6), `bootstrap_worker()` (stdio + tự-gán Job, KHÔNG Qt/migration), `bootstrap_harness()`. `ocr_app.py` gọi cùng luồng; **3 worker entry gắn bootstrap_worker** (W `_worker_init`, KIE `warmup_worker`, P `_prep_init` mới).
- **Bằng chứng:** smoke qua bootstrap giờ xác nhận LightGBM ("xác nhận trang đầu") — trước đây harness không qua bootstrap nên LGBM import hỏng, bị quy nhầm cho app (đúng phát hiện §7 review).
- Mẫu org-positive: smoke nhận `--org-sample <pdf>` (file thật bóc được org khác rỗng — assert org khác rỗng + parity sync/deferred org/year/due); không có mẫu → **SKIP có tên trong log** (không tính là đã kiểm chứng pass org). Việc còn lại: lấy 1 file thật từ corpus người dùng.
- Native abort pytest: giữ nguyên quy trình kê khai (subprocess isolation không được coi là đã sửa crash sản phẩm — kiểm trên artifact frozen ở M4).

## 8. D5 — Harness đo máy đích (sẵn sàng, chưa chạy)

- `scripts/bench_secret_scan.py`: `--corpus --runs --data-dir --profile W=16,F=8,CHUNK=8,KIE_PROC=2,PREP_PROC=2 --force-rescan`; **`SCANINDEX_BASE_DIR`** (env mới trong `paths.get_base_dir`) tách toàn bộ state benchmark khỏi app thật; warm-runtime ép OCR lại bằng reset progress CỦA BẢN BENCH (model giữ warm); **timeline 1 Hz JSON** (RSS parent, admission, KIE telemetry + `deliveries_pending`, prep, spool bytes); manifest gắn SHA-256 mã nguồn + env requested/effective; meta per-run (`files_per_min`, failures) — không dùng `_scan_finished` làm mốc durability duy nhất (đối chiếu journal/registry sau lượt).
- Lệnh chạy trên máy đích (sau frozen):
  `python scripts\bench_secret_scan.py --corpus D:\corpus_bench --runs 3 --data-dir D:\bench_data --profile W=16,F=8,CHUNK=8 --force-rescan`
  rồi A/B với baseline bản ổn định, sweep theo §8 (W trước, sau đó F, chunk, K/P) — K/P **bật đủ metadata** khi nghiệm thu, không dùng DISABLE_KIE lấy tốc độ.

## 9. VIỆC CÒN LẠI — M4 trên máy đích (có tên, có đầu ra)

1. **Build frozen** từ dependency đã pin → chạy frozen smoke: Stop lúc chờ admission/prep/KIE/commit; đóng app; kill lúc append/fsync; đối chiếu PID con (Job Object phải bảo đảm 0 child vô chủ), lease/staging, registry/journal, UI/Excel. Đầu ra: log smoke gắn hash executable/modules/models.
2. **A/B + soak 64/128** bằng harness D5: W=8/16/24/32 (F=8, chunk=8) → F=4/8/16/32 → chunk=8/16 → K=1/2/4 (intra-op=2), P=1/2/4; ≥3 lượt/cấu hình, cold/warm-runtime-ép-OCR/cache-hit tách bạch; soak ≥2 vòng recycle + RSS/backlog ổn định. Đầu ra: `results.json` + timeline + manifest — chữ ký tối ưu chỉ khi vượt baseline ngoài nhiễu.
3. **Mẫu org-positive:** 1 file thật bóc được org khác rỗng → smoke `--org-sample`. Đầu ra: dòng `[org-sample] … PASS` trong log gắn hash.
4. (Theo dõi, không chặn rollout có feature-flag): SQLite `scan_jobs` hai trục chính thức + `ManagedStagePool` per-worker IPC — hai mục nâng cấp kiến trúc đã kê khai §5/§4.

## 10. Kê khai sai khác có chủ đích so với đề xuất review

| Đề xuất | Bản chốt này | Lý do |
|---|---|---|
| `scan_jobs.sqlite` (WAL/FULL) | Journal v2 + kspool/spool bền | Đóng đúng bằng chứng bắt buộc (crash-cutpoint, KIE-only OCR=0) với rủi ro thấp hơn; SQLite là nâng cấp kế tiếp có tên |
| `ManagedStagePool` IPC per-worker | Job Object + bounded shutdown + ready-live prune trên executor hiện tại | Thay executor là viết lại W/K/P — làm trong lượt này sẽ phá 189 test đã đạt; 4/5 tiêu chí D1 đã đóng, phần còn lại kê khai |
| Nested-job fail-closed từng slot | Gắn job best-effort + log | Môi trường CI/dev có thể đã nằm trong job; fail-closed đi cùng ManagedStagePool |

Cả ba đều là quyết định triển khai — không phải trì hoãn: bằng chứng bảng §2 review đã đạt ở mức "được phép vận hành"; mục tiêu "tối ưu 64/128" chờ M4 như review quy định.

---
*Lịch sử các vòng trước (R1–R3, chi tiết từng fix + runbook pilot §5 cũ) giữ tại git/temp archive; nội dung runbook đo đã hợp nhất vào harness D5 + §8 review chốt.*
