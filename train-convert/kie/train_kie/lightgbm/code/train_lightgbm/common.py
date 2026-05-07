from __future__ import annotations

import json
import os
import random
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


PROJECT_SCHEMA_VERSION = "train_lightgbm_project_v1"
RANDOM_SEED = 1337


@dataclass(frozen=True)
class ProjectPaths:
    root: Path
    manifest: Path
    logs: Path
    exports_root: Path
    reports_root: Path
    training_root: Path
    models_root: Path
    temp_root: Path


def utc_now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def build_paths(project_root: str | os.PathLike[str]) -> ProjectPaths:
    root = Path(project_root).resolve()
    return ProjectPaths(
        root=root,
        manifest=root / "manifest.json",
        logs=root / "logs",
        exports_root=root / "exports",
        reports_root=root / "reports",
        training_root=root / "training",
        models_root=root / "models",
        temp_root=root / "temp",
    )


def ensure_project_layout(paths: ProjectPaths) -> None:
    for path in [
        paths.root,
        paths.logs,
        paths.exports_root,
        paths.reports_root,
        paths.training_root,
        paths.models_root,
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


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z._-]+", "_", (value or "").strip())
    cleaned = cleaned.strip("._-")
    return cleaned or "unnamed"


def seeded_rng(*parts: object) -> random.Random:
    seed = "::".join(str(part) for part in parts)
    return random.Random(seed)
