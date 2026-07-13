# Build & Versioning Guide

Tài liệu này là **bắt buộc đọc** trước khi build release, bump version, hoặc thay
đổi database schema. Cả developer và AI assistant đều phải nắm các quy tắc dưới
đây để tránh mất dữ liệu người dùng.

> TL;DR — Xem [Quick release](#quick-release) cho quy trình rút gọn và
> [Khi đổi DB schema](#khi-đổi-db-schema-bắt-buộc-3-bước) cho cảnh báo quan trọng
> nhất.

---

## Hai loại version, hai vai trò (QUAN TRỌNG)

ScanIndex phân biệt rõ hai khái niệm version. Nhầm lẫn giữa chúng là nguyên nhân
chính của bug mất dữ liệu:

| Loại version | Giá trị hiện tại | Vai trò | Nơi định nghĩa |
|---|---|---|---|
| **App version** | `1.1.4` | **Tên file** (chống copy đè khi update) | `git tag` → `VERSION` → `get_version_short()` |
| **Schema version** | `8` | **Converter dữ liệu** (chạy khi DB structure đổi) | `scanindex/core/repository/constants.py: SCHEMA_VERSION` |

**Quy tắc:** App version đổi mỗi release → đổi tên file (`settings-1.1.4.ini`,
`repository-1.1.4.db`). Schema version đổi ít khi → chạy converter dữ liệu. Hai
loại này **độc lập**: một release có thể bump app version mà không đổi schema
(chỉ rename file, không convert), hoặc ngược lại.

---

## Tên file version-per-file

Từ 1.1.4, mọi file state người dùng mang version trong tên để thao tác
copy-đè thư mục (Windows Explorer) không bao giờ ghi đè dữ liệu thật:

| File | Tên (app version) | Module sở hữu |
|---|---|---|
| Settings | `settings-<APP>.ini` (vd `settings-1.1.4.ini`) | `data_versioning.get_active_settings_path()` |
| SQLite DB | `repository/repository-<APP>.db` | `data_versioning.get_active_db_filename()` |
| Sign settings | `config/sign_settings-<APP>.json` | `data_versioning.get_active_config_path("config/sign_settings", ".json")` |
| Sign templates | `config/sign_templates-<APP>.json` | tương tự |
| Ignored words | `ignored_words-<APP>.txt` | tương tự |

Các file **không** version (ổn định, dùng chung qua release):
- `repository/pdf/` — blob file PDF, tham chiếu tương đối từ DB
- `repository/tantivy_index/` — chỉ mục dẫn xuất, rebuild được
- `config/sign_stamp_images/` — ảnh con dấu người dùng

---

## Hành vi khi start app

Khi mở ScanIndex, theo thứ tự:

1. **Top-level `ocr_app.py`**: gọi `run_startup_migration()` — merge các file text
   config (settings, sign json, ignored_words) từ tên legacy (vd `settings.ini`)
   sang tên version hiện tại (vd `settings-1.1.4.ini`). Fresh install (không có
   file cũ) → **không ghi file gì**, dùng default trong RAM.
2. **Top-level**: `ensure_runtime_config_files()` — chỉ tạo thư mục
   `config/sign_stamp_images/`, không seed config.
3. **Khi user vào màn Archive**: `_open_store()` gọi `migrate_db_if_needed()` —
   rename DB legacy sang tên version mới + chạy schema converter nếu cần, rồi mở
   `ArchiveStore`.

### Kịch bản copy-đè (worst case thiết kế giải quyết)

| Tình huống | Kết quả |
|---|---|
| Mở fresh 1.1.4 → đóng → copy đè folder 1.1.3 | Không ghi file → không có gì đè → data 1.1.3 nguyên vẹn |
| Mở fresh 1.1.4 (auto-seeded) → copy đè folder 1.1.3 | File khác tên → không đè; migration thấy cả 2 → **data thật 1.1.3 thắng** |
| Mở fresh 1.1.4 → save config → copy đè 1.1.3 | Settings giữ config 1.1.4 (user đã chủ động đặt); DB vẫn lấy data 1.1.3 |

---

## Quy ước SemVer

```
MAJOR.MINOR.PATCH     ví dụ: 1.1.4
   │   │   └─ sửa bug, chỉnh nhỏ        → bump PATCH  (1.1.4 → 1.1.5)
   │   └───── tính năng mới            → bump MINOR  (1.1.4 → 1.2.0)
   └───────── breaking change lớn      → bump MAJOR  (1.1.4 → 2.0.0)
```

Nguồn chân lý là **git tag** (`v1.1.4`). `VERSION` file sinh từ `git describe`
lúc build. Xem `scanindex/infra/version.py` cho chi tiết.

---

## Quick release

Quy trình chuẩn để phát hành bản mới (không đổi DB schema):

```powershell
# 1. Đảm bảo code đã commit + test pass
python -m pytest tests/

# 2. (Tùy chọn) build local để kiểm tra
build_portable.bat quick

# 3. Tạo git tag theo SemVer (tiền tố 'v')
git tag v1.1.5
git push origin v1.1.5
```

Push tag sẽ trigger GitHub Actions `.github/workflows/release.yml` tự động:
checkout tag → build full → đóng gói `dist/ScanIndex-1.1.5.7z` → tạo GitHub
Release "ScanIndex 1.1.5" → upload asset.

**Khi đó version-per-file tự động hoạt động**: user upgrade sẽ thấy migration
rename `settings-1.1.4.ini` → `settings-1.1.5.ini`, `repository-1.1.4.db` →
`repository-1.1.5.db`. Vì schema không đổi, không cần converter.

---

## Khi đổi DB schema (BẮT BUỘC 3 bước)

Khi release có thay đổi cấu trúc database (thêm/xóa cột, đổi table), **phải làm
đủ 3 bước dưới đây cùng lúc**. Bỏ sót bất kỳ bước nào sẽ làm app không mở được DB
(có chủ đích, để chống mất data):

### Bước 1 — Bump schema version

Trong `scanindex/core/repository/constants.py`:
```python
SCHEMA_VERSION = "9"   # đổi từ "8" → "9"
```

### Bước 2 — Viết converter `v_old → v_new`

Trong `scanindex/core/repository/schema_converters.py`, thêm hàm converter và
đăng ký vào `_CONVERTERS`:

```python
def _convert_v8_to_v9(conn):
    """v8 → v9: thêm cột ký hiệu khẩn cấp riêng biệt."""
    conn.executescript("""
        ALTER TABLE documents ADD COLUMN kie_urgency_mark TEXT;
        -- backfill data từ cột cũ nếu cần:
        -- UPDATE documents SET kie_urgency_mark = ... ;
    """)

_CONVERTERS["8"] = _convert_v8_to_v9
```

Quy ước converter:
- Tên hàm: `_convert_v<SRC>_to_v<DST>` (vd `_convert_v8_to_v9`).
- Chỉ làm **thay đổi cộng thêm** (add column, add table). Không xóa cột cũ trong
  cùng bước — nếu cần xóa, dùng [expand-contract](https://martinfowler.com/bliki/ParallelChange.html):
  bước này thêm cột mới, release sau mới xóa cột cũ.
- Chạy trong transaction do `convert_schema_to_latest` quản lý.
- Trước khi converter chạy, `migrate_db_if_needed` đã backup DB sang
  `.preconv.bak` → nếu converter fail, data gốc còn.

### Bước 3 — Bump app version + tag release

```powershell
git tag v1.2.0   # MINOR bump vì có tính năng DB mới
git push origin v1.2.0
```

### Cảnh báo an toàn

Nếu bạn bump `SCHEMA_VERSION` mà **quên** viết converter + đăng ký:
- `convert_schema_to_latest` raise `MissingConverterError`
- App từ chối mở DB, hiện thông báo lỗi (không im lặng khởi tạo DB rỗng)
- **Data người dùng được bảo toàn** — bạn phải viết converter thiếu thì app mới chạy lại được

Đây là cơ chế an toàn có chủ đích. **Không bao giờ** "fix" bằng cách bắt
`MissingConverterError` và bỏ qua — phải viết converter.

### Trường hợp ngoại lệ: DB hoàn toàn mới structure

Nếu schema đổi quá lớn không thể converter tuyến tính (vd rewrite toàn bộ table),
thì:
1. Bump `SCHEMA_VERSION` lên giá trị mới
2. Viết converter **1 bước** từ version cũ gần nhất lên version mới (cho phép
   converter làm nhiều việc, miễn là toàn vẹn dữ liệu)
3. Nếu thật sự không thể giữ data → converter phải export data ra JSON backup
   trước khi rebuild, rồi re-import. **Không bao giờ** xóa data im lặng.

---

## Test converter trước khi release

Khi viết converter mới, **phải** test với DB thật của version cũ:

```powershell
# 1. Copy repository.db từ bản cũ sang tmp
# 2. Chạy converter thủ công để verify:
python -c "
import sqlite3
from scanindex.core.repository import schema_converters as sc
conn = sqlite3.connect('tmp/old_repository.db')
print(sc.convert_schema_to_latest(conn))  # phải trả về (from, to)
conn.close()
"
# 3. Verify data nguyên vẹn (đếm rows, check giá trị)
```

Có test tự động trong `tests/test_data_versioning.py` — thêm test case cho
converter mới theo pattern `test_converter_registered_runs_and_bumps_version`.

---

## Troubleshooting

### User báo "mất data sau khi update"

1. Hỏi user check `repository/` có file `.preconv.bak` không → restore lại.
2. Check log có `[Versioning]` messages — cho biết migration đã chạy gì.
3. Check có file legacy nào còn (tên không version) — migration chưa chạy xong.

### App hiện "MissingConverterError"

User đang chạy bản mới (schema version cao) trên DB cũ mà converter thiếu.
**Giải pháp đúng**: viết converter thiếu, build bản vá, release. Không hướng dẫn
user reset DB.

### App tạo file `settings.ini` rỗng (không phải version-per-file)

Đây là dấu hiệu migration chưa chạy — check `ocr_app.py` top-level có gọi
`run_startup_migration()` trước `ensure_runtime_config_files()` không.

---

## Tham chiếu code

| File | Vai trò |
|---|---|
| `scanindex/infra/data_versioning.py` | Module trung tâm: text migration, DB migration, path resolvers |
| `scanindex/core/repository/schema_converters.py` | Framework converter + `_CONVERTERS` registry |
| `scanindex/core/repository/constants.py` | `SCHEMA_VERSION`, `SQLITE_FILE` |
| `scanindex/core/repository/store.py` | `ArchiveStore(db_filename=...)`, `_wipe_archive_folder` |
| `scanindex/infra/version.py` | `get_version_short()` — nguồn app version |
| `ocr_app.py` | Top-level: `run_startup_migration()` chạy đầu tiên |
| `tests/test_data_versioning.py` | 18 test cover tất cả kịch bản migration |
