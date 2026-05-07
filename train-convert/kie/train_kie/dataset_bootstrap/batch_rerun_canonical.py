from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from train_kie.common import build_paths, load_manifest, save_manifest, write_json
from train_kie.labeling_workspace import build_label_task_from_file


def _load_ocr_pipeline_module():
    module_path = Path(__file__).resolve().parent / "2-run_ocr_pipeline.py"
    spec = importlib.util.spec_from_file_location("train_kie_ocr_pipeline", module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load OCR pipeline module from {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _canonical_stats(path: Path) -> dict:
    if not path.exists():
        return {"exists": False, "chars_no_ws": 0, "lines": 0, "pages": 0}
    doc = json.loads(path.read_text(encoding="utf-8"))
    chars = 0
    lines = 0
    pages = len(doc.get("pages") or [])
    for page in doc.get("pages") or []:
        for line in page.get("lines") or []:
            text = (line.get("ocr_text") or line.get("text") or "")
            chars += len(re.sub(r"\s+", "", text))
            lines += 1
    return {
        "exists": True,
        "chars_no_ws": chars,
        "lines": lines,
        "pages": pages,
    }


def _batched_label_input_path(json_input_root: Path, sorted_index: int, filename: str) -> Path:
    batch_number = (sorted_index // 100) + 1
    batch_name = f"batch_{batch_number:04d}"
    return json_input_root / batch_name / filename


@dataclass
class BatchDoc:
    input_json_path: Path
    output_filename: str
    relative_pdf_path: str
    canonical_json_path: Path
    entry: dict


def _load_batch_docs(project_root: Path, batch_name: str, manifest: dict) -> list[BatchDoc]:
    batch_dir = project_root / "json_input" / batch_name
    if not batch_dir.exists():
        raise FileNotFoundError(f"Batch input directory not found: {batch_dir}")

    entries_by_rel = {entry["relative_pdf_path"]: entry for entry in manifest.get("documents", [])}
    docs: list[BatchDoc] = []
    for input_json_path in sorted(batch_dir.glob("*.json")):
        task = json.loads(input_json_path.read_text(encoding="utf-8"))
        rel = task["relative_pdf_path"]
        entry = entries_by_rel.get(rel)
        if not entry:
            raise KeyError(f"Manifest missing document for {rel}")
        docs.append(BatchDoc(
            input_json_path=input_json_path,
            output_filename=input_json_path.name,
            relative_pdf_path=rel,
            canonical_json_path=Path(task["source_canonical_json"]),
            entry=entry,
        ))
    return docs


def _find_or_create_backup_dir(project_root: Path, batch_name: str, resume: bool) -> Path:
    backup_root = project_root / "ocr_backup"
    backup_root.mkdir(parents=True, exist_ok=True)
    if resume:
        candidates = sorted(
            backup_root.glob(f"{batch_name}_*"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            return candidates[0]
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    backup_dir = backup_root / f"{batch_name}_{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    return backup_dir


def _load_report(report_path: Path) -> dict:
    if report_path.exists():
        return json.loads(report_path.read_text(encoding="utf-8"))
    return {
        "processed": [],
        "warnings_missing_text_ge_2pct": [],
        "failures": [],
    }


def _save_report(report_path: Path, report: dict) -> None:
    write_json(report_path, report)


def rerun_batch(args) -> int:
    project_root = Path(args.project_root).resolve()
    paths = build_paths(project_root)
    manifest = load_manifest(paths)
    docs = _load_batch_docs(project_root, args.batch_name, manifest)
    ocrpipe = _load_ocr_pipeline_module()

    backup_dir = _find_or_create_backup_dir(project_root, args.batch_name, resume=args.resume)
    report_path = paths.logs / f"{args.batch_name}_ocr_rerun_report.json"
    report = _load_report(report_path)
    report.update({
        "batch": args.batch_name,
        "project_root": str(project_root),
        "backup_dir": str(backup_dir),
        "parallel_capacity": ocrpipe.direct_ocr_engine.get_parallel_capacity(),
        "no_correct": bool(args.no_correct),
        "started_at": report.get("started_at") or datetime.now().isoformat(timespec="seconds"),
        "total_files": len(docs),
    })

    done_names = {row["canonical_json_name"] for row in report.get("processed", [])}
    failed_names = {row["canonical_json_name"] for row in report.get("failures", [])}

    for index, doc in enumerate(docs, 1):
        canonical_name = doc.canonical_json_path.name
        if canonical_name in done_names:
            print(f"SKIP DONE [{index}/{len(docs)}] {doc.relative_pdf_path}", flush=True)
            continue

        backup_path = backup_dir / canonical_name
        if not backup_path.exists() and doc.canonical_json_path.exists():
            shutil.copy2(doc.canonical_json_path, backup_path)

        print(f"OCR [{index}/{len(docs)}] {doc.relative_pdf_path}", flush=True)
        old_stats = _canonical_stats(backup_path)

        entry = doc.entry
        corrected_ocr_pdf = Path(entry["artifacts"].get("corrected_ocr_pdf") or ocrpipe.artifact_corrected_ocr_pdf(paths, entry))
        corrected_canonical_json = Path(entry["artifacts"].get("corrected_canonical_json") or ocrpipe.artifact_corrected_canonical_json(paths, entry))
        preprocessed_pdf = paths.temp_root / "preprocessed" / Path(doc.relative_pdf_path).with_suffix(".preprocessed.pdf")
        task_payload = {
            "relative_pdf_path": doc.relative_pdf_path,
            "source_pdf_path": entry["source_pdf_path"],
            "ocr_pdf": entry["artifacts"]["ocr_pdf"],
            "canonical_json": str(doc.canonical_json_path),
            "corrected_ocr_pdf": str(corrected_ocr_pdf),
            "corrected_canonical_json": str(corrected_canonical_json),
            "preprocessed_pdf": str(preprocessed_pdf),
            "force": False,
            "no_preprocess": False,
            "no_correct": bool(args.no_correct),
            "allow_page_parallel": True,
            "preprocess_workers": None,
        }

        result = ocrpipe._process_one_document(task_payload)
        if result["status"] != "done":
            row = {
                "canonical_json_name": canonical_name,
                "relative_pdf_path": doc.relative_pdf_path,
                "message": result.get("message"),
            }
            report.setdefault("failures", []).append(row)
            failed_names.add(canonical_name)
            entry["status"]["ocr"] = "failed"
            entry["meta"]["last_error"] = result.get("message")
            save_manifest(paths, manifest)
            _save_report(report_path, report)
            print(f"FAILED {doc.relative_pdf_path}: {result.get('message')}", flush=True)
            continue

        entry["status"]["ocr"] = "done"
        entry["meta"]["last_error"] = None
        entry["meta"]["correction_applied"] = result.get("correction_applied")
        entry["meta"]["preprocess_applied"] = result.get("preprocess_applied")
        entry["meta"]["preprocessed_pdf"] = result.get("preprocessed_pdf")
        entry["artifacts"]["ocr_pdf"] = result["ocr_pdf"]
        entry["artifacts"]["canonical_json"] = result["canonical_json"]
        entry["artifacts"]["corrected_ocr_pdf"] = result.get("corrected_ocr_pdf") or entry["artifacts"].get("corrected_ocr_pdf")
        entry["artifacts"]["corrected_canonical_json"] = result.get("corrected_canonical_json") or entry["artifacts"].get("corrected_canonical_json")
        save_manifest(paths, manifest)

        new_stats = _canonical_stats(doc.canonical_json_path)
        missing_ratio = 0.0
        if old_stats["chars_no_ws"] > 0 and new_stats["chars_no_ws"] < old_stats["chars_no_ws"]:
            missing_ratio = (old_stats["chars_no_ws"] - new_stats["chars_no_ws"]) / old_stats["chars_no_ws"]
        row = {
            "canonical_json_name": canonical_name,
            "relative_pdf_path": doc.relative_pdf_path,
            "old_chars_no_ws": old_stats["chars_no_ws"],
            "new_chars_no_ws": new_stats["chars_no_ws"],
            "old_lines": old_stats["lines"],
            "new_lines": new_stats["lines"],
            "missing_ratio": round(missing_ratio, 6),
            "warning_missing_text_ge_2pct": bool(missing_ratio >= 0.02),
        }
        report.setdefault("processed", []).append(row)
        done_names.add(canonical_name)
        if missing_ratio >= 0.02:
            report.setdefault("warnings_missing_text_ge_2pct", []).append(row)
            print(f"WARN missing_text {missing_ratio:.2%} {doc.relative_pdf_path}", flush=True)
        else:
            print(f"DONE {doc.relative_pdf_path}", flush=True)
        _save_report(report_path, report)

    report["completed_at"] = datetime.now().isoformat(timespec="seconds")
    _save_report(report_path, report)

    if args.prepare_inputs:
        print("REBUILD json_input batch payloads...", flush=True)
        for sorted_index, doc in enumerate(docs):
            task_payload = build_label_task_from_file(
                str(doc.canonical_json_path),
                doc_id=doc.entry["doc_id"],
                relative_pdf_path=doc.relative_pdf_path,
            )
            output_path = _batched_label_input_path(paths.json_input_root, sorted_index + args.batch_offset, doc.output_filename)
            write_json(output_path, task_payload)
            doc.entry["artifacts"]["label_input_json"] = str(output_path.resolve())
            doc.entry.setdefault("meta", {})["label_input_batch"] = output_path.parent.name
        save_manifest(paths, manifest)
        print(f"JSON input refreshed for {args.batch_name}", flush=True)

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Backup + rerun canonical OCR for one existing batch directory.")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--batch-name", required=True)
    parser.add_argument("--resume", action="store_true", help="Reuse latest backup dir/report for this batch and skip already processed canonical files.")
    parser.add_argument("--no-correct", action="store_true", help="Skip correction stage. Raw canonical JSON is still regenerated.")
    parser.add_argument("--prepare-inputs", action="store_true", help="Regenerate json_input payloads for this batch after OCR rerun.")
    parser.add_argument("--batch-offset", type=int, default=0, help="Global sorted-file offset used to preserve original batch folder when rebuilding json_input.")
    args = parser.parse_args()
    return rerun_batch(args)


if __name__ == "__main__":
    raise SystemExit(main())

