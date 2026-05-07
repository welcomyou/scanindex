from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


PROJECT_SCHEMA_VERSION = "train_kie_project_v1"
DEFAULT_SPLITS = {"train": 0.8, "val": 0.1, "test": 0.1}


@dataclass(frozen=True)
class ProjectPaths:
    root: Path
    manifest: Path
    logs: Path
    ocr_root: Path
    labels_root: Path
    json_input_root: Path
    json_output_labeled_root: Path
    raw_labels_root: Path
    consensus_root: Path
    review_root: Path
    exports_root: Path
    training_root: Path
    onnx_root: Path
    temp_root: Path


def utc_now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def build_paths(project_root: str | os.PathLike[str]) -> ProjectPaths:
    root = Path(project_root).resolve()
    return ProjectPaths(
        root=root,
        manifest=root / "manifest.json",
        logs=root / "logs",
        ocr_root=root / "ocr",
        labels_root=root / "labels",
        json_input_root=root / "json_input",
        json_output_labeled_root=root / "json_output_labeled",
        raw_labels_root=root / "labels" / "raw",
        consensus_root=root / "labels" / "consensus",
        review_root=root / "labels" / "review",
        exports_root=root / "exports",
        training_root=root / "training",
        onnx_root=root / "onnx",
        temp_root=root / "temp",
    )


def ensure_project_layout(paths: ProjectPaths) -> None:
    for path in [
        paths.root,
        paths.logs,
        paths.ocr_root,
        paths.labels_root,
        paths.json_input_root,
        paths.json_output_labeled_root,
        paths.raw_labels_root,
        paths.consensus_root,
        paths.review_root,
        paths.exports_root,
        paths.training_root,
        paths.onnx_root,
        paths.temp_root,
    ]:
        path.mkdir(parents=True, exist_ok=True)


def read_json(path: str | os.PathLike[str], default=None):
    file_path = Path(path)
    if not file_path.exists():
        return default
    with file_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: str | os.PathLike[str], payload) -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = file_path.with_suffix(file_path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    tmp_path.replace(file_path)


def read_jsonl(path: str | os.PathLike[str]) -> list[dict]:
    rows = []
    with Path(path).open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def write_jsonl(path: str | os.PathLike[str], rows: Iterable[dict]) -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with file_path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def stable_doc_id(relative_pdf_path: str) -> str:
    return hashlib.sha1(relative_pdf_path.encode("utf-8")).hexdigest()[:16]


def hash_ratio(value: str, seed: str = "hd36-kie") -> float:
    digest = hashlib.sha1(f"{seed}:{value}".encode("utf-8")).hexdigest()
    bucket = int(digest[:12], 16)
    return bucket / float(16**12 - 1)


def assign_split(relative_pdf_path: str, ratios: dict[str, float] | None = None) -> str:
    ratios = ratios or DEFAULT_SPLITS
    train_cut = ratios["train"]
    val_cut = train_cut + ratios["val"]
    score = hash_ratio(relative_pdf_path)
    if score < train_cut:
        return "train"
    if score < val_cut:
        return "val"
    return "test"


def scan_pdfs(input_root: str | os.PathLike[str]) -> list[Path]:
    root = Path(input_root).resolve()
    return sorted(path for path in root.rglob("*.pdf") if path.is_file())


def relative_output_stem(relative_pdf_path: str) -> Path:
    rel = Path(relative_pdf_path)
    return rel.parent / f"{rel.stem}_ocr"


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z._-]+", "_", (value or "").strip())
    cleaned = cleaned.strip("._-")
    return cleaned or "unnamed"


def label_task_stem(entry: dict) -> str:
    rel = Path(entry["relative_pdf_path"]).with_suffix("")
    readable = "__".join(safe_name(part) for part in rel.parts if part)
    return f"{readable}__{entry['doc_id']}" if readable else entry["doc_id"]


def artifact_ocr_pdf(paths: ProjectPaths, entry: dict) -> Path:
    return Path(str(paths.ocr_root / relative_output_stem(entry["relative_pdf_path"])) + ".pdf")


def artifact_canonical_json(paths: ProjectPaths, entry: dict) -> Path:
    return Path(str(artifact_ocr_pdf(paths, entry)) + ".json")


def artifact_corrected_ocr_pdf(paths: ProjectPaths, entry: dict) -> Path:
    raw_pdf = artifact_ocr_pdf(paths, entry)
    return raw_pdf.with_name(f"{raw_pdf.stem}_corrected{raw_pdf.suffix}")


def artifact_corrected_canonical_json(paths: ProjectPaths, entry: dict) -> Path:
    return Path(str(artifact_corrected_ocr_pdf(paths, entry)) + ".json")


def artifact_label_input(paths: ProjectPaths, entry: dict) -> Path:
    return paths.json_input_root / f"{label_task_stem(entry)}.json"


def artifact_raw_label(paths: ProjectPaths, model_name: str, entry: dict) -> Path:
    safe_model = model_name.replace("/", "__")
    return paths.raw_labels_root / safe_model / f"{entry['doc_id']}.json"


def label_batch_dir_name(entry: dict) -> str | None:
    meta = entry.get("meta") or {}
    batch_name = meta.get("label_input_batch")
    if batch_name:
        return str(batch_name)
    return None


def label_output_dir(paths: ProjectPaths, stage_name: str) -> Path:
    if stage_name == "labeled":
        return paths.json_output_labeled_root
    return paths.root / f"json_output_{safe_name(stage_name)}"


def artifact_label_output(paths: ProjectPaths, stage_name: str, entry: dict) -> Path:
    output_root = label_output_dir(paths, stage_name)
    batch_name = label_batch_dir_name(entry)
    if batch_name:
        output_root = output_root / batch_name
    return output_root / f"{label_task_stem(entry)}.json"


def discover_label_output_models(paths: ProjectPaths) -> list[str]:
    models = []
    if paths.json_output_labeled_root.exists():
        models.append("labeled")
    prefix = "json_output_"
    for path in sorted(paths.root.iterdir()):
        if not path.is_dir() or not path.name.startswith(prefix):
            continue
        model_name = path.name[len(prefix):]
        if model_name not in models:
            models.append(model_name)
    return models


def artifact_consensus_json(paths: ProjectPaths, entry: dict) -> Path:
    return paths.consensus_root / f"{entry['doc_id']}.json"


def build_manifest(input_root: str | os.PathLike[str], project_root: str | os.PathLike[str]) -> dict:
    input_root = Path(input_root).resolve()
    paths = build_paths(project_root)
    ensure_project_layout(paths)

    documents = []
    for pdf_path in scan_pdfs(input_root):
        rel = str(pdf_path.relative_to(input_root)).replace("\\", "/")
        subdir = Path(rel).parts[0] if len(Path(rel).parts) > 1 else ""
        entry = {
            "doc_id": stable_doc_id(rel),
            "relative_pdf_path": rel,
            "source_pdf_path": str(pdf_path),
            "split": assign_split(rel),
            "subdir": subdir,
            "status": {
                "ocr": "pending",
                "ground_truth": "pending",
                "exports": {},
                "training": {},
            },
            "artifacts": {
                "ocr_pdf": str(artifact_ocr_pdf(paths, {"relative_pdf_path": rel})),
                "canonical_json": str(artifact_canonical_json(paths, {"relative_pdf_path": rel})),
                "corrected_ocr_pdf": str(artifact_corrected_ocr_pdf(paths, {"relative_pdf_path": rel})),
                "corrected_canonical_json": str(artifact_corrected_canonical_json(paths, {"relative_pdf_path": rel})),
                "label_input_json": str(artifact_label_input(paths, {
                    "relative_pdf_path": rel,
                    "doc_id": stable_doc_id(rel),
                })),
                "labels": {},
                "ground_truth_json": str(artifact_consensus_json(paths, {"doc_id": stable_doc_id(rel)})),
            },
            "meta": {
                "created_at": utc_now_iso(),
                "last_error": None,
            },
        }
        documents.append(entry)

    manifest = {
        "schema_version": PROJECT_SCHEMA_VERSION,
        "created_at": utc_now_iso(),
        "input_root": str(input_root),
        "project_root": str(paths.root),
        "documents": documents,
    }
    save_manifest(paths, manifest)
    return manifest


def load_manifest(paths: ProjectPaths) -> dict:
    manifest = read_json(paths.manifest)
    if not manifest:
        raise FileNotFoundError(f"Manifest not found: {paths.manifest}")
    return manifest


def save_manifest(paths: ProjectPaths, manifest: dict) -> None:
    manifest["updated_at"] = utc_now_iso()
    write_json(paths.manifest, manifest)


def iter_documents(manifest: dict, split: str | None = None, subdir: str | None = None,
                   limit: int | None = None) -> list[dict]:
    docs = manifest.get("documents", [])
    selected = []
    for entry in docs:
        if split and entry.get("split") != split:
            continue
        if subdir and not entry.get("relative_pdf_path", "").startswith(subdir.replace("\\", "/").rstrip("/") + "/") \
                and entry.get("subdir") != subdir:
            continue
        selected.append(entry)
        if limit and len(selected) >= limit:
            break
    return selected


def find_entry(manifest: dict, doc_id: str) -> dict:
    for entry in manifest.get("documents", []):
        if entry.get("doc_id") == doc_id:
            return entry
    raise KeyError(f"Document not found in manifest: {doc_id}")


def find_last_checkpoint(output_dir: str | os.PathLike[str]) -> str | None:
    root = Path(output_dir)
    if not root.exists():
        return None
    candidates = []
    for path in root.iterdir():
        if path.is_dir() and path.name.startswith("checkpoint-"):
            try:
                step = int(path.name.split("-", 1)[1])
            except (IndexError, ValueError):
                continue
            candidates.append((step, path))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return str(candidates[-1][1])
