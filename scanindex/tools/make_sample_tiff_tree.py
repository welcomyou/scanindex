"""Tạo cây thư mục TIFF mẫu để thử công cụ Thẩm định số hóa.

Cấu trúc theo quy ước CV 14748: tên tệp trang
``<MãĐĐ>-<Phông>-<Mục lục>-<Đơn vị bảo quản>-<Trang 3 số>.tif``
(1 tệp = 1 trang; số trang CHẠY LIÊN TỤC trong thư mục hồ sơ — không
reset theo tài liệu vì sẽ trùng tên tệp). Trang nào thuộc tài liệu nào
được ghi trong ``README.txt`` tạo kèm.

    sample_tiff_tree/
    ├── README.txt
    ├── A38-011-07-0001/   hồ sơ 1 — 3 tài liệu (1+2+3 = 6 trang), toàn bộ ĐẠT
    └── A38-011-07-0002/   hồ sơ 2 — 3 tài liệu (4+1+3 = 8 trang), có 4 lỗi
                           cố ý, mỗi lỗi một loại (vàng nén lossless, đỏ xám
                           150 dpi, đỏ nén JPEG, vàng khổ A5 dưới 600 dpi)

Chạy:
    python -m scanindex.tools.make_sample_tiff_tree [thư-mục-đích]

Mặc định tạo tại ``<gốc dự án>/sample_tiff_tree``. Trang đạt chuẩn là
A4 300 dpi RGB không nén (~26 MB/trang) — toàn bộ bộ mẫu chiếm ~280 MB.
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

MDD, PHONG, ML = "A38", "011", "07"
A4 = (2480, 3508)          # 210×297 mm @ 300 dpi
A5 = (1748, 2480)          # 148×210 mm @ 300 dpi

# Trang "đạt" chuẩn: RGB 8 bit/kênh, A4 300 dpi, không nén.
_OK = dict(size=A4, mode="RGB", color=(234, 240, 247), dpi=(300, 300))

# (đơn vị bảo quản, [(số trang, spec hoặc None = đạt chuẩn, ghi chú)])
DOSSIERS = [
    ("0001", [
        (1, None, "Tài liệu 1 (1 trang) — đạt"),
        (2, None, "Tài liệu 2 (2 trang) — đạt"),
        (3, None, "Tài liệu 2 (2 trang) — đạt"),
        (4, None, "Tài liệu 3 (3 trang) — đạt"),
        (5, None, "Tài liệu 3 (3 trang) — đạt"),
        (6, None, "Tài liệu 3 (3 trang) — đạt"),
    ]),
    ("0002", [
        (1, None, "Tài liệu 1 (4 trang) — đạt"),
        (2, None, "Tài liệu 1 (4 trang) — đạt"),
        (3, dict(_OK, compression="tiff_lzw"),
         "Tài liệu 1 — VÀNG: nén LZW lossless (văn bản quy định không nén)"),
        (4, None, "Tài liệu 1 (4 trang) — đạt"),
        (5, dict(size=(1240, 1754), mode="L", color=128, dpi=(150, 150)),
         "Tài liệu 2 (1 trang) — ĐỎ: thang xám + 150 dpi"),
        (6, None, "Tài liệu 3 (3 trang) — đạt"),
        (7, dict(_OK, compression="jpeg"),
         "Tài liệu 3 — ĐỎ: nén JPEG mất dữ liệu"),
        (8, dict(_OK, size=A5),
         "Tài liệu 3 — VÀNG: khổ ~A5 (≤70% A4) quét 300 dpi, quy định 600 dpi"),
    ]),
]


def _draw_page(size: tuple[int, int], mode: str, color) -> Image.Image:
    """Ảnh nền màu nhạt + vài vạch tối giả dòng chữ cho preview dễ nhìn."""
    img = Image.new(mode, size, color)
    d = ImageDraw.Draw(img)
    w, h = size
    ink = 40 if mode == "L" else 0
    y = int(h * 0.08)
    bottom = int(h * 0.92)
    line_h = max(4, h // 300)
    step = max(18, h // 90)
    while y < bottom:
        d.rectangle([int(w * 0.12), y,
                     int(w * (0.55 + (y // step % 5) * 0.06)), y + line_h],
                    fill=ink)
        y += step
    return img


def make_tree(out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "CÂY TIFF MẪU ĐỂ THẨM ĐỊNH SỐ HÓA",
        "=" * 60,
        "Chọn thư mục này làm 'Thư mục gốc' trong màn Thẩm định số hóa.",
        "Quy ước tên: <MãĐĐ>-<Phông>-<Mục lục>-<ĐVBC>-<Trang 3 số>.tif,",
        "1 tệp = 1 trang; số trang chạy liên tục trong thư mục hồ sơ.",
        "",
    ]
    for dvbc, pages in DOSSIERS:
        hs = out_dir / f"{MDD}-{PHONG}-{ML}-{dvbc}"
        hs.mkdir(exist_ok=True)
        lines.append(f"HỒ SƠ {hs.name} — {len(pages)} trang:")
        for trang, spec, note in pages:
            name = f"{MDD}-{PHONG}-{ML}-{dvbc}-{trang:03d}.tif"
            kw = dict(_OK) if spec is None else dict(spec)
            img = _draw_page(kw.pop("size"), kw.pop("mode"), kw.pop("color"))
            dpi = kw.pop("dpi")
            img.save(hs / name, dpi=dpi, **kw)
            lines.append(f"  {name} — {note}")
        lines.append("")
    (out_dir / "README.txt").write_text("\n".join(lines), encoding="utf-8")
    return out_dir


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    default = Path(__file__).resolve().parents[2] / "sample_tiff_tree"
    out = Path(args[0]) if args else default
    make_tree(out)
    print(f"Đã tạo cây TIFF mẫu tại: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
