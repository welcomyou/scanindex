from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import correction_engine
import direct_ocr_engine
import pdf_utils
from src.preprocessing import pre_process_pdf
from train_kie.common import (
    artifact_corrected_canonical_json,
    artifact_corrected_ocr_pdf,
    build_paths,
    iter_documents,
    load_manifest,
    save_manifest,
)


def _compute_parallel_plan(selected_count: int, requested_jobs: int) -> tuple[int, bool, int | None]:
    cpu_count = os.cpu_count() or 1
    planned_jobs = max(1, min(requested_jobs, cpu_count, selected_count or 1))
    per_doc_ocr_workers = direct_ocr_engine.get_parallel_capacity()
    allow_page_parallel = (
        per_doc_ocr_workers > 1
        and planned_jobs * per_doc_ocr_workers <= cpu_count
    )

    # When OCR itself already fans out to the full CPU budget, keep preprocess
    # single-threaded per document so we do not oversubscribe the machine before OCR.
    if planned_jobs > 1 and allow_page_parallel:
        preprocess_workers = 1
    elif planned_jobs == 1:
        preprocess_workers = None
    else:
        preprocess_workers = max(1, cpu_count // planned_jobs)

    return planned_jobs, allow_page_parallel, preprocess_workers


def extract_canonical_text(canonical_json_path: str) -> str:
    # Build correction input directly from the OCR canonical JSON so we avoid
    # pypdf layout extraction on the OCR PDF, which can blow up memory on
    # pathological pages with huge whitespace grids.
    with open(canonical_json_path, "r", encoding="utf-8") as f:
        doc = json.load(f)

    page_texts = []
    for page in sorted(doc.get("pages", []), key=lambda item: item.get("page_index", 0)):
        words_by_id = {
            word.get("id"): word
            for word in page.get("words", [])
            if isinstance(word, dict) and word.get("id")
        }
        line_texts = []
        for line in sorted(page.get("lines", []), key=lambda item: item.get("order", 0)):
            text = ""
            word_ids = list(line.get("word_ids") or [])
            if word_ids:
                parts = []
                for word_id in word_ids:
                    word = words_by_id.get(word_id)
                    if not word:
                        continue
                    parts.append(word.get("text", ""))
                    if word.get("has_space_after", True):
                        parts.append(" ")
                text = "".join(parts).rstrip()
            if not text:
                text = line.get("text") or line.get("ocr_text") or ""
            line_texts.append(text)
        page_texts.append("\n".join(line_texts).rstrip())
    return "\n\n".join(page_texts).strip()


def _process_one_document(task: dict) -> dict:
    relative_pdf_path = task["relative_pdf_path"]
    ocr_pdf = Path(task["ocr_pdf"])
    canonical_json = Path(task["canonical_json"])
    corrected_ocr_pdf = Path(task["corrected_ocr_pdf"])
    corrected_canonical_json = Path(task["corrected_canonical_json"])
    preprocessed_pdf = Path(task["preprocessed_pdf"])
    force = task["force"]
    no_preprocess = task["no_preprocess"]
    no_correct = task["no_correct"]

    ocr_pdf.parent.mkdir(parents=True, exist_ok=True)
    corrected_ocr_pdf.parent.mkdir(parents=True, exist_ok=True)
    preprocessed_pdf.parent.mkdir(parents=True, exist_ok=True)

    if force:
        for candidate in [ocr_pdf, canonical_json, corrected_ocr_pdf, corrected_canonical_json, preprocessed_pdf]:
            if candidate.exists():
                candidate.unlink()

    ocr_input_path = task["source_pdf_path"]
    preprocess_applied = False
    preprocess_rotations = None
    if not no_preprocess:
        prep_success, prep_message, prep_meta = pre_process_pdf(
            task["source_pdf_path"],
            str(preprocessed_pdf),
            max_workers=task.get("preprocess_workers"),
            return_metadata=True,
        )
        if not prep_success:
            return {
                "relative_pdf_path": relative_pdf_path,
                "status": "failed",
                "message": f"preprocess: {prep_message}",
            }
        ocr_input_path = str(preprocessed_pdf)
        preprocess_applied = True
        preprocess_rotations = (prep_meta or {}).get("page_rotations") or None

    success, message = direct_ocr_engine.process_pdf(
        ocr_input_path,
        str(ocr_pdf),
        source_document_path=task["source_pdf_path"],
        allow_page_parallel=task.get("allow_page_parallel", True),
        preprocess_rotations=preprocess_rotations,
    )
    if not success:
        return {
            "relative_pdf_path": relative_pdf_path,
            "status": "failed",
            "message": f"ocr: {message}",
        }
    if not canonical_json.exists():
        return {
            "relative_pdf_path": relative_pdf_path,
            "status": "failed",
            "message": "ocr: canonical companion JSON was not written",
        }

    correction_applied = False
    if not no_correct:
        correction_engine.init_client()
        raw_text = extract_canonical_text(str(canonical_json))
        corrected_text = correction_engine.correct_text(raw_text)
        corr_success, corr_message = pdf_utils.create_corrected_pdf(
            str(ocr_pdf),
            str(corrected_ocr_pdf),
            raw_text,
            corrected_text,
        )
        if not corr_success:
            return {
                "relative_pdf_path": relative_pdf_path,
                "status": "failed",
                "message": f"correction: {corr_message}",
            }
        if not corrected_ocr_pdf.exists() or not corrected_canonical_json.exists():
            return {
                "relative_pdf_path": relative_pdf_path,
                "status": "failed",
                "message": "correction: corrected PDF/JSON artifacts were not written",
            }
        correction_applied = corrected_text != raw_text

    return {
        "relative_pdf_path": relative_pdf_path,
        "status": "done",
        "message": None,
        "preprocess_applied": preprocess_applied,
        "preprocessed_pdf": str(preprocessed_pdf.resolve()) if preprocess_applied else None,
        "ocr_pdf": str(ocr_pdf.resolve()),
        "canonical_json": str(canonical_json.resolve()),
        "corrected_ocr_pdf": str(corrected_ocr_pdf.resolve()) if not no_correct else None,
        "corrected_canonical_json": str(corrected_canonical_json.resolve()) if not no_correct else None,
        "correction_applied": correction_applied,
    }


def _apply_result(entry: dict, result: dict, paths, manifest: dict) -> None:
    if result["status"] != "done":
        entry["status"]["ocr"] = "failed"
        entry["meta"]["last_error"] = result["message"]
        save_manifest(paths, manifest)
        print(f"FAILED {entry['relative_pdf_path']}: {result['message']}")
        return

    entry["status"]["ocr"] = "done"
    entry["meta"]["last_error"] = None
    entry["meta"]["correction_applied"] = result["correction_applied"]
    entry["meta"]["preprocess_applied"] = result["preprocess_applied"]
    entry["meta"]["preprocessed_pdf"] = result["preprocessed_pdf"]
    entry["artifacts"]["ocr_pdf"] = result["ocr_pdf"]
    entry["artifacts"]["canonical_json"] = result["canonical_json"]
    entry["artifacts"]["corrected_ocr_pdf"] = result.get("corrected_ocr_pdf")
    entry["artifacts"]["corrected_canonical_json"] = result.get("corrected_canonical_json")
    save_manifest(paths, manifest)
    print(f"DONE {entry['relative_pdf_path']}")


def main():
    parser = argparse.ArgumentParser(description="Run OCR + optional correction over project PDFs.")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--split", choices=["train", "val", "test"])
    parser.add_argument("--subdir", help="Only process one relative subdir prefix.")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--jobs", type=int, default=1, help="Number of PDFs to process in parallel.")
    parser.add_argument("--no-preprocess", action="store_true", help="Skip rotate/deskew preprocessing before OCR.")
    parser.add_argument("--no-correct", action="store_true", help="Skip correction stage.")
    args = parser.parse_args()

    paths = build_paths(args.project_root)
    manifest = load_manifest(paths)
    selected = iter_documents(manifest, split=args.split, subdir=args.subdir, limit=args.limit)
    planned_jobs, allow_page_parallel, preprocess_workers = _compute_parallel_plan(
        len(selected),
        args.jobs,
    )
    pending = []
    for entry in selected:
        ocr_pdf = Path(entry["artifacts"]["ocr_pdf"])
        canonical_json = Path(entry["artifacts"]["canonical_json"])
        corrected_ocr_pdf = Path(entry["artifacts"].get("corrected_ocr_pdf") or artifact_corrected_ocr_pdf(paths, entry))
        corrected_canonical_json = Path(entry["artifacts"].get("corrected_canonical_json") or artifact_corrected_canonical_json(paths, entry))
        entry["artifacts"]["corrected_ocr_pdf"] = str(corrected_ocr_pdf)
        entry["artifacts"]["corrected_canonical_json"] = str(corrected_canonical_json)
        preprocessed_pdf = paths.temp_root / "preprocessed" / Path(entry["relative_pdf_path"]).with_suffix(".preprocessed.pdf")
        correction_ready = args.no_correct or corrected_canonical_json.exists()
        if not args.force and entry["status"].get("ocr") == "done" and canonical_json.exists() and correction_ready:
            print(f"SKIP OCR {entry['relative_pdf_path']}")
            continue
        pending.append((entry, {
            "relative_pdf_path": entry["relative_pdf_path"],
            "source_pdf_path": entry["source_pdf_path"],
            "ocr_pdf": str(ocr_pdf),
            "canonical_json": str(canonical_json),
            "corrected_ocr_pdf": str(corrected_ocr_pdf),
            "corrected_canonical_json": str(corrected_canonical_json),
            "preprocessed_pdf": str(preprocessed_pdf),
            "force": args.force,
            "no_preprocess": args.no_preprocess,
            "no_correct": args.no_correct,
            "allow_page_parallel": allow_page_parallel,
            "preprocess_workers": preprocess_workers,
        }))

    if not pending:
        return

    jobs = max(1, min(planned_jobs, len(pending)))
    per_doc_ocr_workers = direct_ocr_engine.get_parallel_capacity() if allow_page_parallel else 1
    print(
        "PROCESSING "
        f"{len(pending)} document(s) with jobs={jobs}, "
        f"page_parallel={'on' if allow_page_parallel else 'off'}, "
        f"ocr_workers_per_doc={per_doc_ocr_workers}, "
        f"preprocess_workers_per_doc={preprocess_workers or 'auto'}"
    )

    if jobs == 1:
        for entry, task in pending:
            print(f"OCR {entry['relative_pdf_path']}")
            _apply_result(entry, _process_one_document(task), paths, manifest)
    else:
        future_map = {}
        with ProcessPoolExecutor(max_workers=jobs) as executor:
            for entry, task in pending:
                future = executor.submit(_process_one_document, task)
                future_map[future] = entry
            for future in as_completed(future_map):
                entry = future_map[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = {
                        "relative_pdf_path": entry["relative_pdf_path"],
                        "status": "failed",
                        "message": f"worker: {exc}",
                    }
                _apply_result(entry, result, paths, manifest)


if __name__ == "__main__":
    main()

