from __future__ import annotations

import os
from typing import Callable, Iterable, Protocol

import fitz


class PdfSegment(Protocol):
    start_page: int
    end_page: int
    name: str


def _log(log_cb: Callable[[str], None] | None, message: str) -> None:
    if not log_cb:
        return
    try:
        log_cb(message)
    except Exception:
        pass


def split_pdf_segments_preserving_appearances(
    source_pdf: str,
    output_dir: str,
    segments: Iterable[PdfSegment],
    *,
    log_cb: Callable[[str], None] | None = None,
) -> list[str]:
    """Extract contiguous PDF segments while preserving visual signatures.

    Signed PDFs often store the red stamp / signing timestamp as widget
    appearance streams. PyMuPDF's form-copy cache is source-document scoped,
    so reusing one source handle for many output documents can drop later
    signature appearances. Reopen the source for each segment, then bake
    annotations/widgets into permanent page content.
    """
    if not source_pdf:
        raise ValueError("source_pdf is required")
    if not os.path.exists(source_pdf):
        raise FileNotFoundError(source_pdf)
    os.makedirs(output_dir, exist_ok=True)

    out_paths: list[str] = []
    for index, segment in enumerate(segments, start=1):
        start_page = int(segment.start_page)
        end_page = int(segment.end_page)
        name = (getattr(segment, "name", "") or f"segment_{index:03d}.pdf").strip()
        dst_path = os.path.join(output_dir, name)

        dst = fitz.open()
        try:
            with fitz.open(source_pdf) as src:
                if start_page < 0 or end_page < start_page or end_page >= len(src):
                    raise ValueError(
                        f"invalid segment range {start_page}-{end_page} for {source_pdf}"
                    )
                dst.insert_pdf(
                    src,
                    from_page=start_page,
                    to_page=end_page,
                    annots=1,
                    widgets=1,
                )

            bake = getattr(dst, "bake", None)
            if callable(bake):
                try:
                    bake(annots=True, widgets=True)
                except Exception as exc:
                    _log(
                        log_cb,
                        f"[pdf-split] annotation bake skipped for {name}: {exc}",
                    )
            dst.save(dst_path, deflate=True, garbage=4)
        finally:
            dst.close()
        out_paths.append(dst_path)

    return out_paths
