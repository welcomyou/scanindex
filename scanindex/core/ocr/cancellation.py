"""Hủy dùng chung tầng core cho các luồng OCR.

Tầng UI (vd `secret_file_scan_screen`) phải **alias** lớp này thay vì subclass:

    from scanindex.core.ocr.cancellation import CoreScanCancelled as _ScanCancelled

Lý do: engine (core) bắt `CoreScanCancelled` để truyền nguyên trạng lệnh hủy qua
các wrapper `except Exception` rộng; nếu UI tạo subclass và engine tự raise lớp
cha thì `except _ScanCancelled` của UI sẽ không bắt được instance lớp cha.
"""
from __future__ import annotations


class CoreScanCancelled(Exception):
    """Lệnh hủy do người dùng/kháach gọi — truyền nguyên trạng qua mọi tầng.

    KHÔNG được các wrapper OCR biến thành fallback tuần tự hay shutdown pool:
    đây không phải lỗi hạ tầng."""


class PoolResetError(RuntimeError):
    """Request chưa terminal thuộc một thế hệ pool vừa bị reset/dựng lại.

    Caller nhận lỗi hữu hạn này thay vì chờ `AsyncResult` cũ mãi (terminate
    không biến handle cũ thành lỗi)."""


class SubmissionBusy(RuntimeError):
    """Không nộp được ngay do admission đầy — KHÔNG phải lỗi pool, không được
    dùng làm lý do chuyển đường OCR singleton/fallback."""
