"""
Export PDF pages as images for YOLO zone labeling.

Exports 2 loại trang:
  - Trang 1 (p1): chứa co_quan, ngay, so_ky_hieu, trich_yeu
  - Trang ký (sign): chứa nguoi_ky, noi_nhan — tự động tìm bằng keyword

Output: temp/yolo_training/ with 200 DPI PNG images.
        Tên file: {docname}_p1.png, {docname}_sign_p{N}.png

Usage:
    python scripts/export_page1_images.py <folder_with_pdfs> [--dpi 200]
    python scripts/export_page1_images.py D:/temp --dpi 200
"""
import os
import sys
import argparse
import fitz  # PyMuPDF


# Keywords that indicate a signing page (bottom area)
_SIGN_KEYWORDS = [
    "Nơi nhận",
    "NƠI NHẬN",
    "nơi nhận",
    "đã ký",
    "ĐÃ KÝ",
    "T/M",
    "K/T",
    "T/L",
    "TM.",
    "KT.",
    "TL.",
]


def _safe_name(pdf_path):
    """Generate a clean filename from PDF path."""
    base = os.path.splitext(os.path.basename(pdf_path))[0]
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in base)


def _render_page(doc, page_idx, dpi):
    """Render a page to pixmap."""
    page = doc[page_idx]
    mat = fitz.Matrix(dpi / 72.0, dpi / 72.0)
    return page.get_pixmap(matrix=mat, alpha=False)


def _find_sign_page(doc):
    """
    Find the signing page by searching from the LAST page backward.
    The signing page contains "Nơi nhận", "đã ký", "T/M", "K/T", etc.
    in the bottom 40% of the page.

    Returns page index (0-based) or None.
    """
    for page_idx in range(len(doc) - 1, -1, -1):
        page = doc[page_idx]
        page_height = page.rect.height
        # Get text blocks with positions
        blocks = page.get_text("dict").get("blocks", [])
        for block in blocks:
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    text = span.get("text", "")
                    y = span["bbox"][1]
                    # Only check bottom 40% of page
                    if y > page_height * 0.60:
                        for kw in _SIGN_KEYWORDS:
                            if kw in text:
                                return page_idx
    return None


def export_pages(pdf_path, output_dir, dpi=200):
    """
    Export page 1 and signing page from a PDF.
    Returns list of (output_path, page_type) tuples.
    """
    results = []
    try:
        doc = fitz.open(pdf_path)
        if len(doc) == 0:
            return results
        name = _safe_name(pdf_path)

        # --- Page 1: header fields ---
        pix = _render_page(doc, 0, dpi)
        p1_path = os.path.join(output_dir, f"{name}_p1.png")
        pix.save(p1_path)
        results.append((p1_path, "p1"))

        # --- Signing page ---
        sign_idx = _find_sign_page(doc)

        if sign_idx is not None and sign_idx != 0:
            # Different page from page 1
            pix = _render_page(doc, sign_idx, dpi)
            sign_path = os.path.join(output_dir, f"{name}_sign_p{sign_idx + 1}.png")
            pix.save(sign_path)
            results.append((sign_path, f"sign(p{sign_idx + 1})"))
        elif sign_idx == 0:
            # Signing is on page 1 (single-page doc) — already exported
            results[0] = (results[0][0], "p1+sign")
        elif len(doc) > 1:
            # Fallback: export last page even if no keywords found
            last_idx = len(doc) - 1
            pix = _render_page(doc, last_idx, dpi)
            last_path = os.path.join(output_dir, f"{name}_last_p{last_idx + 1}.png")
            pix.save(last_path)
            results.append((last_path, f"last(p{last_idx + 1})"))

        doc.close()
    except Exception as e:
        print(f"  [ERROR] {pdf_path}: {e}")
    return results


def find_pdfs(folder, recursive=True):
    """Find all PDF files in folder."""
    pdfs = []
    if recursive:
        for root, dirs, files in os.walk(folder):
            # Skip common non-document folders
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "node_modules", ".venv")]
            for f in files:
                if f.lower().endswith(".pdf"):
                    pdfs.append(os.path.join(root, f))
    else:
        for f in os.listdir(folder):
            if f.lower().endswith(".pdf"):
                pdfs.append(os.path.join(folder, f))
    return sorted(pdfs)


def main():
    parser = argparse.ArgumentParser(
        description="Export PDF pages (page 1 + signing page) as images for YOLO zone labeling")
    parser.add_argument("folder", help="Folder containing PDF files")
    parser.add_argument("--dpi", type=int, default=200, help="DPI for rendering (default: 200)")
    parser.add_argument("--output", default=None, help="Output directory (default: temp/yolo_training/)")
    parser.add_argument("--no-recursive", action="store_true", help="Don't search subfolders")
    args = parser.parse_args()

    output_dir = args.output or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "temp", "yolo_training")
    os.makedirs(output_dir, exist_ok=True)

    pdfs = find_pdfs(args.folder, recursive=not args.no_recursive)
    print(f"Found {len(pdfs)} PDFs in {args.folder}")
    print(f"Output: {output_dir}")
    print(f"DPI: {args.dpi}")
    print()

    total_images = 0
    for i, pdf_path in enumerate(pdfs):
        pages = export_pages(pdf_path, output_dir, dpi=args.dpi)
        for out_path, page_type in pages:
            total_images += 1
            print(f"  [{total_images:3}] {os.path.basename(pdf_path):50} -> {os.path.basename(out_path):40} [{page_type}]")

    print(f"\nDone: {total_images} images from {len(pdfs)} PDFs -> {output_dir}")
    print(f"\nNext steps:")
    print(f"  1. Upload images to Roboflow (roboflow.com)")
    print(f"  2. Label with 6 classes: co_quan, ngay, so_ky_hieu, trich_yeu, nguoi_ky, noi_nhan")
    print(f"  3. _p1 images: label co_quan, ngay, so_ky_hieu, trich_yeu")
    print(f"  4. _sign images: label nguoi_ky, noi_nhan")


if __name__ == "__main__":
    main()
