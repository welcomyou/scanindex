"""Create a temporary OCR PDF for diagnostics.

This is intentionally a real script instead of a ``python -`` snippet because
Windows multiprocessing needs an importable ``__main__`` file when OCR workers
are spawned.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "ocr_app.py").exists():
            return parent
    return Path.cwd()


ROOT = _repo_root()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _resolve_pdf(path_or_glob: str) -> Path:
    p = Path(path_or_glob)
    if p.exists():
        return p
    parent = p.parent if str(p.parent) not in {"", "."} else Path.cwd()
    matches = sorted(parent.glob(p.name))
    if not matches:
        raise FileNotFoundError(path_or_glob)
    return matches[0]


def _log(msg: str, level: str = "info") -> None:
    print(f"[{level}] {msg}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input_pdf")
    parser.add_argument("output_pdf")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--serial", action="store_true")
    args = parser.parse_args()

    os.environ["DIRECT_OCR_NUM_PAGE_WORKERS"] = str(max(1, args.workers))

    input_pdf = _resolve_pdf(args.input_pdf)
    output_pdf = Path(args.output_pdf)
    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    for path in (output_pdf, Path(str(output_pdf) + ".json")):
        if path.exists():
            path.unlink()

    print(f"INPUT {input_pdf}", flush=True)
    print(f"OUTPUT {output_pdf}", flush=True)

    from direct_ocr_engine import process_pdf

    t0 = time.perf_counter()
    ok, err = process_pdf(
        str(input_pdf),
        str(output_pdf),
        update_callback=_log,
        allow_page_parallel=not args.serial,
    )
    elapsed = time.perf_counter() - t0
    print(f"OCR_RESULT ok={ok} err={err!r} elapsed_s={elapsed:.2f}", flush=True)

    if ok:
        import fitz

        doc = fitz.open(str(output_pdf))
        chars = [len(page.get_text()) for page in doc]
        print(f"OCR_PAGES {doc.page_count}", flush=True)
        print(f"OCR_TEXT_CHARS_TOTAL {sum(chars)}", flush=True)
        print(f"OCR_TEXT_CHARS_BY_PAGE {chars}", flush=True)
        doc.close()
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
