"""Cache gợi ý thông tin hồ sơ cho Số hóa lưu trữ.

Sau khi người dùng nhập thông tin một hồ sơ, phần mềm nhớ lại:
  * mã phông → tên phông
  * (mã phông, mã mục lục) → tên mục lục
  * nhiệm kỳ gần nhất

Lần sau nhập hồ sơ khác, các trường CÒN TRỐNG được điền tự động theo mã —
người dùng không phải gõ lại (yêu cầu phản hồi: "lưu lại mã phông - tên
phông, mã phông - mã mục lục - tên mục lục, nhiệm kỳ để lần sau tự động
điền giá trị gần nhất"). Cache luôn lấy từ LẦN LƯU HỒ SƠ CUỐI CÙNG của
người dùng: trường trống ở lần lưu cuối thì gợi ý tương ứng cũng bị gỡ,
không mang giá trị của các lần lưu cũ vào gợi ý. Lưu JSON tại
``<base>/config/dossier_info_cache.json``; hộp thoại thông tin hồ sơ có
nút xóa cache. Chỉ dùng trong app, không ảnh hưởng dữ liệu Kho.
"""
from __future__ import annotations

import json
import os

try:
    from scanindex.infra.paths import get_base_dir
except Exception:
    def get_base_dir():
        return os.getcwd()

_CACHE_FILENAME = "dossier_info_cache.json"


def _cache_path() -> str:
    return os.path.join(get_base_dir(), "config", _CACHE_FILENAME)


def load() -> dict:
    """Đọc cache; trả {} nếu chưa có/hỏng. Shape:
    ``{"fonds": {ma_phong: ten_phong}, "catalog": {"phong|ml": ten_muc_luc},
    "last_term": str}``."""
    try:
        with open(_cache_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def save(data: dict) -> None:
    try:
        path = _cache_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def clear() -> None:
    try:
        os.remove(_cache_path())
    except OSError:
        pass


def _norm(value) -> str:
    return str(value or "").strip()


def prefill(identity) -> None:
    """Điền IN-PLACE các trường còn trống của ``IdentityCodes`` từ cache:
    tên phông theo mã phông, tên mục lục theo (mã phông, mã mục lục),
    nhiệm kỳ theo giá trị gần nhất. Không đè giá trị người dùng đã nhập."""
    if identity is None or getattr(identity, "is_unstructured", False):
        return
    data = load()
    ma_phong = _norm(getattr(identity, "ma_phong", ""))
    ma_ml = _norm(getattr(identity, "muc_luc", ""))
    if not _norm(getattr(identity, "ten_phong", "")) and ma_phong:
        ten_phong = _norm(data.get("fonds", {}).get(ma_phong))
        if ten_phong:
            identity.ten_phong = ten_phong
    if not _norm(getattr(identity, "ten_muc_luc", "")) and ma_phong and ma_ml:
        ten_ml = _norm(data.get("catalog", {}).get(f"{ma_phong}|{ma_ml}"))
        if ten_ml:
            identity.ten_muc_luc = ten_ml
    if not _norm(getattr(identity, "nhiem_ky", "")):
        term = _norm(data.get("last_term"))
        if term:
            identity.nhiem_ky = term


def update(identity) -> None:
    """Ghi thông tin hồ sơ VỪA ĐƯỢC LƯU vào cache. Cache luôn phản ánh
    LẦN LƯU CUỐI CÙNG của người dùng: trường mà lần lưu cuối để trống thì
    gợi ý tương ứng bị gỡ — không "resurrect" giá trị của các lần lưu cũ.
    (theo MÃ — tên gõ lần sau tự hiện). Bỏ qua chế độ tự do (UNSTRUCT)
    vì không có mã phông thực."""
    if identity is None or getattr(identity, "is_unstructured", False):
        return
    ma_phong = _norm(getattr(identity, "ma_phong", ""))
    ma_ml = _norm(getattr(identity, "muc_luc", ""))
    ten_phong = _norm(getattr(identity, "ten_phong", ""))
    ten_ml = _norm(getattr(identity, "ten_muc_luc", ""))
    term = _norm(getattr(identity, "nhiem_ky", ""))[:10]
    if not ma_phong:
        return
    data = load()
    fonds = data.setdefault("fonds", {})
    if ten_phong:
        fonds[ma_phong] = ten_phong
    else:
        # Lần lưu cuối không có tên phông → bỏ gợi ý cũ của mã phông này.
        fonds.pop(ma_phong, None)
    catalog = data.setdefault("catalog", {})
    if ma_ml:
        key = f"{ma_phong}|{ma_ml}"
        if ten_ml:
            catalog[key] = ten_ml
        else:
            catalog.pop(key, None)
    # Nhiệm kỳ theo lần lưu cuối — kể cả khi người dùng để trống.
    data["last_term"] = term
    save(data)
