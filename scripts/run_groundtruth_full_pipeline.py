from __future__ import annotations

import json
import multiprocessing as mp
import os
import sys
import time
import traceback
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GROUNDTRUTH_DIR = Path(r"C:\Users\nhquan\Downloads\groundtruth")


def _install_repo_path() -> None:
    os.chdir(ROOT)
    root_s = str(ROOT)
    if root_s not in sys.path:
        sys.path.insert(0, root_s)


def _log(message: str) -> None:
    print(time.strftime("%H:%M:%S"), message, flush=True)


def _progress_callback(msg, level="info") -> None:
    text = str(msg)
    lower = text.lower()
    important = (
        "processing" in lower
        or "page" in lower
        or "layout" in lower
        or "ocr completed" in lower
        or "parallel" in lower
        or "deskew" in lower
        or "rotate" in lower
        or "digital" in lower
    )
    if important:
        _log(f"  [{level}] {text}")


def _read_summary(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []


def _write_summary(path: Path, row: dict) -> None:
    rows = [item for item in _read_summary(path) if item.get("file") != row.get("file")]
    rows.append(row)
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def process_one(src: Path, summary_path: Path) -> dict:
    from scanindex.core.preprocessing import preprocessing
    from scanindex.core.ocr import direct_engine as direct_ocr_engine
    from scanindex.core.tables.export_worker import run_export_task

    start = time.perf_counter()
    stem = src.stem
    pre_path = src.with_name(f"pre_{src.name}")
    ocr_path = src.with_name(f"{stem}_ocr.pdf")
    docx_path = src.with_name(f"{stem}_final.docx")
    row = {
        "file": src.name,
        "input": str(src),
        "ocr_pdf": str(ocr_path),
        "ocr_json": str(ocr_path) + ".json",
        "docx": str(docx_path),
        "ok": False,
    }

    try:
        _log(f"=== START {src.name} ===")
        for path in (pre_path, ocr_path, Path(str(ocr_path) + ".json"), docx_path):
            if path.exists():
                path.unlink()
                _log(f"  removed old derivative: {path.name}")

        t0 = time.perf_counter()
        pre_result = preprocessing.pre_process_pdf(
            str(src),
            str(pre_path),
            update_callback=_progress_callback,
            debug_mode=False,
            max_workers=max(1, min(4, os.cpu_count() or 4)),
            return_metadata=True,
        )
        if isinstance(pre_result, tuple) and len(pre_result) == 3:
            pre_ok, pre_msg, pre_meta = pre_result
        else:
            pre_ok = pre_result[0] if isinstance(pre_result, tuple) else bool(pre_result)
            pre_msg = pre_result[1] if isinstance(pre_result, tuple) and len(pre_result) > 1 else ""
            pre_meta = {}

        target_input = pre_path if pre_ok and pre_path.exists() else src
        rotations = (pre_meta or {}).get("page_rotations") or None
        row["preprocess_ok"] = bool(pre_ok)
        row["preprocess_msg"] = pre_msg
        row["preprocessed_input"] = str(target_input)
        row["preprocess_seconds"] = round(time.perf_counter() - t0, 3)
        if target_input.exists():
            row["preprocessed_size_mb"] = round(target_input.stat().st_size / 1024 / 1024, 3)
        _log(
            f"  preprocess ok={pre_ok} target={target_input.name} "
            f"seconds={row['preprocess_seconds']} sizeMB={row.get('preprocessed_size_mb')}"
        )

        t0 = time.perf_counter()
        ok, msg = direct_ocr_engine.process_pdf(
            str(target_input),
            str(ocr_path),
            num_pages=0,
            update_callback=_progress_callback,
            source_document_path=str(src),
            preprocess_rotations=rotations,
            allow_page_parallel=True,
        )
        row["ocr_ok"] = bool(ok)
        row["ocr_msg"] = msg
        row["ocr_seconds"] = round(time.perf_counter() - t0, 3)
        if ocr_path.exists():
            row["ocr_size_mb"] = round(ocr_path.stat().st_size / 1024 / 1024, 3)
        _log(f"  OCR ok={ok} seconds={row['ocr_seconds']} sizeMB={row.get('ocr_size_mb')}")
        if not ok:
            raise RuntimeError(f"OCR failed: {msg}")

        t0 = time.perf_counter()
        export = run_export_task(str(ocr_path), str(docx_path), metadata=None)
        row["export_ok"] = bool(export.get("success"))
        row["export_msg"] = export.get("msg")
        row["export_seconds"] = round(time.perf_counter() - t0, 3)
        if docx_path.exists():
            row["docx_size_mb"] = round(docx_path.stat().st_size / 1024 / 1024, 3)
        row["export_log_tail"] = "\n".join((export.get("logs") or "").splitlines()[-40:])
        _log(
            f"  export ok={export.get('success')} seconds={row['export_seconds']} "
            f"sizeMB={row.get('docx_size_mb')}"
        )
        if not export.get("success"):
            raise RuntimeError(f"Export failed: {export.get('msg')}")

        row["ok"] = True
        return row
    except Exception as exc:
        row["error"] = str(exc)
        row["traceback"] = traceback.format_exc()
        _log(f"  ERROR {src.name}: {exc}")
        _log(row["traceback"])
        return row
    finally:
        try:
            if pre_path.exists():
                pre_path.unlink()
                _log(f"  cleaned temp: {pre_path.name}")
        except Exception as exc:
            _log(f"  cleanup warn {pre_path}: {exc}")
        row["total_seconds"] = round(time.perf_counter() - start, 3)
        _write_summary(summary_path, row)
        _log(f"=== DONE {src.name} ok={row['ok']} total={row['total_seconds']}s ===")


def main() -> int:
    _install_repo_path()
    summary_path = GROUNDTRUTH_DIR / "codex_full_pipeline_summary.json"
    scans = sorted(GROUNDTRUTH_DIR.glob("scan*.pdf"))
    _log(f"Found {len(scans)} scan PDFs in {GROUNDTRUTH_DIR}")
    had_error = False
    for src in scans:
        row = process_one(src, summary_path)
        had_error = had_error or not row.get("ok")
    _log("ALL DONE")
    return 1 if had_error else 0


if __name__ == "__main__":
    mp.freeze_support()
    raise SystemExit(main())
