"""PDF/A-2b converter sử dụng Ghostscript.

Convert PDF thường sang PDF/A-2b (chuẩn lưu trữ dài hạn) — giữ nguyên JPEG
bytes bằng PassThroughJPEGImages để không tái nén ảnh gốc.

Order trong pipeline ký số:
    insert OCR text layer → convert PDF/A → ký số (pyHanko)

Convert TRƯỚC ký số: signature trong PDF/A-2 vẫn valid.
Convert SAU ký số: có thể phá signature.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional


_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def find_ghostscript() -> Optional[str]:
    """Auto-detect Ghostscript executable. Returns absolute path hoặc None."""
    for name in ("gswin64c", "gswin32c", "gs"):
        p = shutil.which(name)
        if p:
            return p
    if sys.platform == "win32":
        # Common install locations
        for base in (r"C:\Program Files\gs", r"C:\Program Files (x86)\gs"):
            if not os.path.isdir(base):
                continue
            try:
                versions = sorted(os.listdir(base), reverse=True)
            except OSError:
                continue
            for v in versions:
                for exe_name in ("gswin64c.exe", "gswin32c.exe"):
                    candidate = os.path.join(base, v, "bin", exe_name)
                    if os.path.exists(candidate):
                        return candidate
    return None


def find_pdfa_def_ps(gs_path: str) -> Optional[str]:
    """Tìm PDFA_def.ps đi kèm Ghostscript install."""
    if not gs_path:
        return None
    # gs binary nằm ở <install>/bin/gswin64c.exe
    # PDFA_def.ps nằm ở <install>/lib/PDFA_def.ps
    gs_install = os.path.dirname(os.path.dirname(gs_path))
    candidate = os.path.join(gs_install, "lib", "PDFA_def.ps")
    return candidate if os.path.exists(candidate) else None


def is_available() -> bool:
    """Kiểm tra Ghostscript + PDFA_def.ps đầy đủ để convert."""
    gs = find_ghostscript()
    if not gs:
        return False
    return find_pdfa_def_ps(gs) is not None


def convert_to_pdfa(
    input_pdf: str,
    output_pdf: str,
    *,
    version: str = "2",
    timeout: float = 120.0,
) -> tuple[bool, str]:
    """Convert PDF sang PDF/A-{version}b.

    Args:
        input_pdf: source PDF path
        output_pdf: dest PDF path (ghi đè nếu tồn tại)
        version: "1", "2" (khuyến nghị), hoặc "3"
        timeout: giây

    Returns:
        (success, error_message). error_message rỗng khi success.

    Notes:
        - PassThroughJPEGImages=true → ảnh JPEG embed giữ nguyên byte (không re-encode)
        - PDFACompatibilityPolicy=1 → fail nếu file vi phạm PDF/A standard
        - Output có thể lớn hơn input ~5-10% do ICC profile + metadata
    """
    if not os.path.exists(input_pdf):
        return False, f"Input không tồn tại: {input_pdf}"

    gs = find_ghostscript()
    if not gs:
        return False, "Ghostscript không tìm thấy (cài gs10+ hoặc thêm vào PATH)"

    pdfa_def = find_pdfa_def_ps(gs)
    if not pdfa_def:
        return False, f"PDFA_def.ps không tìm thấy gần {gs}"

    if version not in ("1", "2", "3"):
        return False, f"PDF/A version không hợp lệ: {version} (chỉ 1/2/3)"

    args = [
        gs,
        f"-dPDFA={version}",
        "-dBATCH", "-dNOPAUSE", "-dNOOUTERSAVE",
        "-dPDFACompatibilityPolicy=1",
        "-sColorConversionStrategy=UseDeviceIndependentColor",
        "-sDEVICE=pdfwrite",
        "-dPassThroughJPEGImages=true",
        "-dPassThroughJPXImages=true",
        f"-sOutputFile={output_pdf}",
        pdfa_def,
        input_pdf,
    ]

    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=_CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return False, f"Ghostscript timeout sau {timeout}s"
    except Exception as e:
        return False, f"Subprocess error: {e}"

    if result.returncode != 0:
        # Lấy 500 char cuối stderr (Ghostscript verbose)
        tail = (result.stderr or "")[-500:]
        return False, f"gs exit {result.returncode}: {tail}"

    if not os.path.exists(output_pdf) or os.path.getsize(output_pdf) == 0:
        return False, "Output PDF rỗng sau khi convert"

    return True, ""


# ── CLI for manual testing ─────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python pdf_a_converter.py <input.pdf> <output.pdf> [version]")
        print(f"  Ghostscript: {find_ghostscript()}")
        print(f"  PDFA_def.ps: {find_pdfa_def_ps(find_ghostscript() or '')}")
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
