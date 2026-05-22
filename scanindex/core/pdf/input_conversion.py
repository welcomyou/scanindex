from __future__ import annotations

import io
import os
from pathlib import Path

import fitz


PDF_INPUT_EXTENSIONS = {".pdf"}
IMAGE_INPUT_EXTENSIONS = {".png", ".bmp", ".jpg", ".jpeg", ".tif", ".tiff"}
SUPPORTED_INPUT_EXTENSIONS = PDF_INPUT_EXTENSIONS | IMAGE_INPUT_EXTENSIONS


def input_suffix(path: str | os.PathLike[str]) -> str:
    return Path(path).suffix.lower()


def is_pdf_path(path: str | os.PathLike[str]) -> bool:
    return input_suffix(path) in PDF_INPUT_EXTENSIONS


def is_image_path(path: str | os.PathLike[str]) -> bool:
    return input_suffix(path) in IMAGE_INPUT_EXTENSIONS


def is_supported_document_path(path: str | os.PathLike[str]) -> bool:
    return input_suffix(path) in SUPPORTED_INPUT_EXTENSIONS


def ocr_pdf_output_path(source_path: str | os.PathLike[str]) -> str:
    base, _ext = os.path.splitext(str(source_path))
    return base + "_ocr.pdf"


def image_page_count(image_path: str | os.PathLike[str]) -> int:
    from PIL import Image, ImageSequence

    with Image.open(image_path) as img:
        return max(1, sum(1 for _ in ImageSequence.Iterator(img)))


def _frame_dpi(frame) -> tuple[float, float]:
    raw = frame.info.get("dpi") or frame.info.get("resolution") or (300, 300)
    try:
        dpi_x = float(raw[0])
        dpi_y = float(raw[1] if len(raw) > 1 else raw[0])
    except Exception:
        dpi_x = dpi_y = 300.0
    if dpi_x <= 0:
        dpi_x = 300.0
    if dpi_y <= 0:
        dpi_y = dpi_x
    return dpi_x, dpi_y


def _rgb_frame(frame):
    from PIL import Image, ImageOps

    image = ImageOps.exif_transpose(frame)
    if image.mode in {"RGBA", "LA"} or (
        image.mode == "P" and "transparency" in image.info
    ):
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        background.alpha_composite(rgba)
        return background.convert("RGB")
    if image.mode != "RGB":
        return image.convert("RGB")
    return image.copy()


def image_to_pdf(image_path: str | os.PathLike[str], output_pdf: str | os.PathLike[str]) -> str:
    """Convert a raster image file into a PDF that the OCR pipeline can process.

    Multi-frame images such as TIFF are kept as multi-page PDFs. Page size is
    derived from image DPI when available, with a 300 DPI fallback for scans.
    """
    from PIL import Image, ImageSequence

    image_path = str(image_path)
    output_pdf = str(output_pdf)
    os.makedirs(os.path.dirname(os.path.abspath(output_pdf)), exist_ok=True)

    doc = fitz.open()
    try:
        with Image.open(image_path) as img:
            for frame in ImageSequence.Iterator(img):
                rgb = _rgb_frame(frame)
                dpi_x, dpi_y = _frame_dpi(frame)
                width_pt = max(1.0, float(rgb.width) * 72.0 / dpi_x)
                height_pt = max(1.0, float(rgb.height) * 72.0 / dpi_y)
                page = doc.new_page(width=width_pt, height=height_pt)

                buffer = io.BytesIO()
                rgb.save(buffer, format="PNG")
                page.insert_image(page.rect, stream=buffer.getvalue())

        if len(doc) == 0:
            raise ValueError(f"No image frames found in {image_path}")
        doc.save(output_pdf, garbage=4, deflate=True, deflate_images=True)
        return output_pdf
    finally:
        doc.close()
