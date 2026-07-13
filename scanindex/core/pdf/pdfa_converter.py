"""PDF/A-2b converter sử dụng pikepdf.

Convert PDF thường sang PDF/A-2b (chuẩn lưu trữ dài hạn) — thêm sRGB
OutputIntent + XMP metadata pdfaid:part=2/conformance=B mà KHÔNG rewrite
font streams hay content streams.

Tại sao không dùng Ghostscript: Ghostscript ``pdfwrite`` re-embed font khi
tạo PDF/A, và với font TrueType/WinAnsi nó convert sang Type0/Identity-H
subset nhưng KHÔNG generate ToUnicode CMap mới → text extract ra glyph ID
thô = mojibake (tiếng Việt hiển thị thành ký tự Cyrillic rác). pikepdf chỉ
thêm OutputIntent + XMP, giữ nguyên toàn bộ font/text → ToUnicode bảo toàn,
text extract sạch.

Order trong pipeline ký số:
    insert OCR text layer → convert PDF/A → ký số (pyHanko)

Convert TRƯỚC ký số: signature trong PDF/A-2 vẫn valid.
Convert SAU ký số: có thể phá signature.
"""
from __future__ import annotations

import os
import sys
from typing import Optional

import pikepdf

# Đường dẫn ICC profile sRGB bundle trong repo (public domain, ~2.5KB).
_SRGB_ICC = os.path.join(os.path.dirname(__file__), "assets", "srgb.icc")


def is_available() -> bool:
    """Kiểm tra pikepdf + ICC profile sẵn sàng để convert."""
    try:
        import pikepdf  # noqa: F401
    except ImportError:
        return False
    return os.path.isfile(_SRGB_ICC)


def convert_to_pdfa(
    input_pdf: str,
    output_pdf: str,
    *,
    version: str = "2",
    timeout: float = 120.0,
) -> tuple[bool, str]:
    """Convert PDF sang PDF/A-{version}b bằng pikepdf.

    Args:
        input_pdf: source PDF path
        output_pdf: dest PDF path (ghi đè nếu tồn tại)
        version: chỉ hỗ trợ "2" (PDF/A-2b). "1"/"3" được chấp nhận nhưng vẫn
            tạo PDF/A-2b (pikepdf PDF/A-2b tương thích ngược với hầu hết
            verifier).
        timeout: giữ cho tương thích API (không dùng — pikepdf là synchronous).

    Returns:
        (success, error_message). error_message rỗng khi success.

    Notes:
        - KHÔNG rewrite font streams → ToUnicode CMap bảo toàn → tránh
          mojibake khi extract text (bug cố hữu của Ghostscript pdfwrite).
        - Output size ≈ input size + ~3KB (ICC profile).
        - Chỉ thêm sRGB OutputIntent + XMP pdfaid declaration.
    """
    if not os.path.exists(input_pdf):
        return False, f"Input không tồn tại: {input_pdf}"
    if not os.path.isfile(_SRGB_ICC):
        return False, f"ICC profile không tìm thấy: {_SRGB_ICC}"
    if version not in ("1", "2", "3"):
        return False, f"PDF/A version không hợp lệ: {version} (chỉ 1/2/3)"

    try:
        pdf = pikepdf.open(input_pdf)
    except Exception as exc:
        return False, f"Mở PDF lỗi: {exc}"

    try:
        # 1. sRGB OutputIntent (yêu cầu của PDF/A).
        with open(_SRGB_ICC, "rb") as f:
            icc_stream = pikepdf.Stream(pdf, f.read())
        output_intent = pikepdf.Dictionary({
            "/Type": pikepdf.Name.OutputIntent,
            "/S": pikepdf.Name.GTS_PDFA1,
            "/OutputConditionIdentifier": pikepdf.String("sRGB"),
            "/Info": pikepdf.String("sRGB IEC61966-2.1"),
            "/RegistryName": pikepdf.String("http://www.color.org"),
            "/DestOutputProfile": icc_stream,
        })
        # Thay thế OutputIntents hiện có (chỉ giữ 1 sRGB).
        if pikepdf.Name.OutputIntents in pdf.Root:
            del pdf.Root.OutputIntents
        pdf.Root.OutputIntents = pikepdf.Array([output_intent])

        # 2. XMP metadata khai báo PDF/A-2b (pdfaid namespace).
        with pdf.open_metadata(set_pikepdf_as_editor=False) as meta:
            meta["pdfaid:part"] = "2"
            meta["pdfaid:conformance"] = "B"

        pdf.save(output_pdf)
    except Exception as exc:
        try:
            pdf.close()
        except Exception:
            pass
        return False, f"Convert PDF/A lỗi: {exc}"

    try:
        pdf.close()
    except Exception:
        pass

    if not os.path.exists(output_pdf) or os.path.getsize(output_pdf) == 0:
        return False, "Output PDF rỗng sau khi convert"
    return True, ""


# ── CLI for manual testing ─────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python pdfa_converter.py <input.pdf> <output.pdf> [version]")
        print(f"  ICC profile: {_SRGB_ICC}")
        print(f"  Available:   {is_available()}")
        sys.exit(1)
    inp, out = sys.argv[1], sys.argv[2]
    ver = sys.argv[3] if len(sys.argv) > 3 else "2"
    ok, err = convert_to_pdfa(inp, out, version=ver)
    if ok:
        sz_in = os.path.getsize(inp) / 1024
        sz_out = os.path.getsize(out) / 1024
        print(f"OK: {inp} ({sz_in:.0f}KB) → {out} ({sz_out:.0f}KB)")
    else:
        print(f"FAIL: {err}")
        sys.exit(2)
