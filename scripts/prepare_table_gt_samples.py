from __future__ import annotations

import argparse
import html
import io
import json
import re
import shutil
import tarfile
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from datasets import load_dataset
from huggingface_hub import HfFileSystem, hf_hub_download


ROOT = Path(__file__).resolve().parents[1]


def safe_stem(value: str, fallback: str) -> str:
    value = Path(value or fallback).stem or fallback
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    return value[:120] or fallback


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def serializable_row(row: dict[str, Any], skip: set[str] | None = None) -> dict[str, Any]:
    skip = skip or set()
    out: dict[str, Any] = {}
    for key, value in row.items():
        if key in skip:
            continue
        if isinstance(value, bytes):
            out[key] = value.decode("utf-8", errors="replace")
        else:
            out[key] = value
    return out


def save_pil_image(image: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)


def html_grid(html_text: str) -> dict[str, Any]:
    soup = BeautifulSoup(html_text or "", "lxml")
    trs = soup.find_all("tr")
    grid: list[list[str]] = []
    occupied: dict[tuple[int, int], str] = {}
    max_cols = 0
    for r_idx, tr in enumerate(trs):
        row: list[str] = []
        c_idx = 0
        for cell in tr.find_all(["td", "th"], recursive=False):
            while (r_idx, c_idx) in occupied:
                row.append(occupied[(r_idx, c_idx)])
                c_idx += 1
            try:
                rowspan = max(1, int(cell.get("rowspan", 1)))
            except ValueError:
                rowspan = 1
            try:
                colspan = max(1, int(cell.get("colspan", 1)))
            except ValueError:
                colspan = 1
            text = html.unescape(cell.get_text(" ", strip=True))
            for dr in range(rowspan):
                for dc in range(colspan):
                    rr = r_idx + dr
                    cc = c_idx + dc
                    occupied[(rr, cc)] = text if dr == 0 and dc == 0 else ""
            for dc in range(colspan):
                row.append(text if dc == 0 else "")
            c_idx += colspan
        while (r_idx, c_idx) in occupied:
            row.append(occupied[(r_idx, c_idx)])
            c_idx += 1
        max_cols = max(max_cols, len(row))
        grid.append(row)
    for row in grid:
        row.extend([""] * (max_cols - len(row)))
    return {"rows": len(grid), "cols": max_cols, "cells": grid}


def pubtables_cell_grid(row: dict[str, Any]) -> list[list[str]]:
    rows = int(row.get("rows") or 0)
    cols = int(row.get("cols") or 0)
    raw_cells = row.get("cells") or []
    if raw_cells and isinstance(raw_cells[0], list):
        raw_cells = raw_cells[0]
    texts = ["".join(cell.get("tokens", []) or []) for cell in raw_cells if isinstance(cell, dict)]
    grid: list[list[str]] = []
    for r_idx in range(rows):
        start = r_idx * cols
        grid.append(texts[start : start + cols] + [""] * max(0, cols - len(texts[start : start + cols])))
    return grid


def collect_pubtables(out_dir: Path, count: int) -> dict[str, Any]:
    dataset_dir = out_dir / "pubtables_1m_otsl"
    samples: list[dict[str, Any]] = []
    ds = load_dataset("docling-project/PubTables-1M_OTSL", split="test", streaming=True)
    for idx, row in enumerate(ds):
        if idx >= count:
            break
        stem = safe_stem(row.get("filename", ""), f"pubtables_{idx:03d}")
        sample_dir = dataset_dir / f"{idx:03d}_{stem}"
        image_path = sample_dir / f"{stem}.jpg"
        save_pil_image(row["image"], image_path)
        gt = serializable_row(row, {"image"})
        gt["source_dataset"] = "docling-project/PubTables-1M_OTSL"
        gt["cell_text_grid"] = pubtables_cell_grid(row)
        gt["shape"] = {"rows": int(row.get("rows") or 0), "cols": int(row.get("cols") or 0)}
        write_json(sample_dir / "groundtruth.json", gt)
        samples.append(
            {
                "id": f"pubtables_{idx:03d}",
                "image": str(image_path),
                "groundtruth": str(sample_dir / "groundtruth.json"),
                "rows": gt["shape"]["rows"],
                "cols": gt["shape"]["cols"],
            }
        )
    return {"dataset": "PubTables-1M", "status": "ready", "sample_count": len(samples), "samples": samples}


def collect_pubtabnet(out_dir: Path, count: int) -> dict[str, Any]:
    dataset_dir = out_dir / "pubtabnet_ocrflux"
    dataset_dir.mkdir(parents=True, exist_ok=True)
    data_path = Path(
        hf_hub_download(
            repo_id="ChatDOC/OCRFlux-pubtabnet-single",
            repo_type="dataset",
            filename="data.jsonl",
        )
    )
    tar_path = Path(
        hf_hub_download(
            repo_id="ChatDOC/OCRFlux-pubtabnet-single",
            repo_type="dataset",
            filename="images.tar.gz",
        )
    )
    metas: list[dict[str, Any]] = []
    with data_path.open("r", encoding="utf-8") as f:
        for idx, line in enumerate(f):
            if idx >= count:
                break
            metas.append(json.loads(line))

    wanted = {meta["image_name"]: i for i, meta in enumerate(metas)}
    found: set[str] = set()
    with tarfile.open(tar_path, "r:gz") as tf:
        for member in tf:
            if not member.isfile():
                continue
            base = Path(member.name).name
            if base not in wanted:
                continue
            idx = wanted[base]
            stem = safe_stem(base, f"pubtabnet_{idx:03d}")
            sample_dir = dataset_dir / f"{idx:03d}_{stem}"
            image_path = sample_dir / base
            image_path.parent.mkdir(parents=True, exist_ok=True)
            src = tf.extractfile(member)
            if src is None:
                continue
            with image_path.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            meta = metas[idx]
            grid = html_grid(meta.get("gt_table", ""))
            gt = {
                "source_dataset": "ChatDOC/OCRFlux-pubtabnet-single",
                "image_name": base,
                "type": meta.get("type"),
                "gt_table": meta.get("gt_table", ""),
                "shape": {"rows": grid["rows"], "cols": grid["cols"]},
                "cell_text_grid": grid["cells"],
            }
            write_json(sample_dir / "groundtruth.json", gt)
            (sample_dir / "groundtruth.html").write_text(meta.get("gt_table", ""), encoding="utf-8")
            found.add(base)
            if len(found) >= len(wanted):
                break

    samples: list[dict[str, Any]] = []
    for idx, meta in enumerate(metas):
        base = meta["image_name"]
        stem = safe_stem(base, f"pubtabnet_{idx:03d}")
        sample_dir = dataset_dir / f"{idx:03d}_{stem}"
        gt_path = sample_dir / "groundtruth.json"
        image_path = sample_dir / base
        gt = json.loads(gt_path.read_text(encoding="utf-8")) if gt_path.exists() else {"shape": {"rows": 0, "cols": 0}}
        samples.append(
            {
                "id": f"pubtabnet_{idx:03d}",
                "image": str(image_path) if image_path.exists() else "",
                "groundtruth": str(gt_path) if gt_path.exists() else "",
                "rows": gt["shape"]["rows"],
                "cols": gt["shape"]["cols"],
            }
        )
    status = "ready" if all(Path(s["image"]).exists() and Path(s["groundtruth"]).exists() for s in samples) else "partial"
    return {"dataset": "PubTabNet", "status": status, "sample_count": len(samples), "samples": samples}


def collect_fintabnet_c_annotations(out_dir: Path, count: int) -> dict[str, Any]:
    dataset_dir = out_dir / "fintabnet_c_official_annotations"
    samples: list[dict[str, Any]] = []
    fs = HfFileSystem()
    tar_path = "datasets/bsmock/FinTabNet.c/FinTabNet.c-PDF_Annotations.tar.gz"
    with fs.open(tar_path, "rb") as stream:
        with tarfile.open(fileobj=stream, mode="r|gz") as tf:
            idx = 0
            for member in tf:
                if idx >= count:
                    break
                if not member.isfile() or not member.name.endswith(".json"):
                    continue
                src = tf.extractfile(member)
                if src is None:
                    continue
                tables = json.loads(src.read().decode("utf-8"))
                table0 = tables[0] if tables else {}
                stem = safe_stem(str(table0.get("structure_id") or Path(member.name).stem), f"fintabnet_c_{idx:03d}")
                sample_dir = dataset_dir / f"{idx:03d}_{stem}"
                gt = {
                    "source_dataset": "bsmock/FinTabNet.c",
                    "note": "Official corrected PDF annotations. This public HF entry does not include renderable image/PDF files.",
                    "key": member.name,
                    "url": "hf://datasets/bsmock/FinTabNet.c/FinTabNet.c-PDF_Annotations.tar.gz",
                    "tables": tables,
                }
                write_json(sample_dir / "groundtruth.json", gt)
                samples.append(
                    {
                        "id": f"fintabnet_c_annotation_{idx:03d}",
                        "image": "",
                        "groundtruth": str(sample_dir / "groundtruth.json"),
                        "rows": len(table0.get("rows") or []),
                        "cols": len(table0.get("columns") or []),
                    }
                )
                idx += 1
    return {
        "dataset": "FinTabNet.c official annotations",
        "status": "groundtruth_only",
        "sample_count": len(samples),
        "samples": samples,
    }


def collect_fintabnet_renderable(out_dir: Path, count: int) -> dict[str, Any]:
    dataset_dir = out_dir / "fintabnet_renderable"
    samples: list[dict[str, Any]] = []
    ds = load_dataset("katphlab/fintabnet-pubtables-full", "test", split="test", streaming=True)
    for idx, row in enumerate(ds):
        if idx >= count:
            break
        file_name = row.get("file_name") or f"fintabnet_{idx:03d}.jpg"
        stem = safe_stem(file_name, f"fintabnet_{idx:03d}")
        sample_dir = dataset_dir / f"{idx:03d}_{stem}"
        image_path = sample_dir / file_name
        save_pil_image(row["image"], image_path)
        category_ids = row.get("category_ids") or []
        rows = sum(1 for cid in category_ids if int(cid) == 3)
        cols = sum(1 for cid in category_ids if int(cid) == 2)
        gt = serializable_row(row, {"image"})
        gt["source_dataset"] = "katphlab/fintabnet-pubtables-full"
        gt["note"] = "Renderable FinTabNet-derived table image set used for visual pipeline benchmarking."
        gt["shape"] = {"rows": rows, "cols": cols}
        write_json(sample_dir / "groundtruth.json", gt)
        samples.append(
            {
                "id": f"fintabnet_renderable_{idx:03d}",
                "image": str(image_path),
                "groundtruth": str(sample_dir / "groundtruth.json"),
                "rows": rows,
                "cols": cols,
            }
        )
    return {"dataset": "FinTabNet renderable", "status": "ready", "sample_count": len(samples), "samples": samples}


def collect_wtw_status(out_dir: Path) -> dict[str, Any]:
    dataset_dir = out_dir / "wtw"
    dataset_dir.mkdir(parents=True, exist_ok=True)
    status = {
        "dataset": "WTW Dataset",
        "status": "blocked_login_required",
        "sample_count": 0,
        "samples": [],
        "note": (
            "Official GitHub only contains code/demo and links the data to Tianchi. "
            "Tianchi download requires an Alibaba/Tianchi session, so no 10-image groundtruth pack "
            "can be fetched automatically without credentials or a local zip."
        ),
        "official_repo": "https://github.com/wangwen-whu/WTW-Dataset",
        "official_download": "https://tianchi.aliyun.com/dataset/108587",
    }
    write_json(dataset_dir / "status.json", status)
    return status


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", type=Path, default=ROOT / "temp" / "external" / "table_gt_samples")
    parser.add_argument("--count", type=int, default=10)
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "out_dir": str(args.out_dir),
        "count_requested": int(args.count),
        "datasets": [],
    }

    collectors = [
        ("PubTables-1M", lambda: collect_pubtables(args.out_dir, args.count)),
        ("PubTabNet", lambda: collect_pubtabnet(args.out_dir, args.count)),
        ("FinTabNet.c official annotations", lambda: collect_fintabnet_c_annotations(args.out_dir, args.count)),
        ("FinTabNet renderable", lambda: collect_fintabnet_renderable(args.out_dir, args.count)),
        ("WTW Dataset", lambda: collect_wtw_status(args.out_dir)),
    ]
    for name, collector in collectors:
        print(f"[collect] {name}", flush=True)
        try:
            result = collector()
        except Exception as exc:
            result = {"dataset": name, "status": "error", "sample_count": 0, "samples": [], "error": repr(exc)}
        manifest["datasets"].append(result)
        write_json(args.out_dir / "manifest.json", manifest)

    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
