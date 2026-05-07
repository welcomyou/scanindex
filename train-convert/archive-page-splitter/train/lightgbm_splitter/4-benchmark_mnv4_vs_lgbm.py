from __future__ import annotations

import argparse
import gc
import json
import os
import statistics
import threading
import time
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
import psutil
import timm
import torch
from PIL import Image
from timm.data import create_transform, resolve_model_data_config


class MemorySampler:
    def __init__(self, interval: float = 0.01) -> None:
        self.process = psutil.Process(os.getpid())
        self.interval = interval
        self.baseline = self.process.memory_info().rss
        self.peak = self.baseline
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "MemorySampler":
        self.baseline = self.process.memory_info().rss
        self.peak = self.baseline
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *args: Any) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self.peak = max(self.peak, self.process.memory_info().rss)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.peak = max(self.peak, self.process.memory_info().rss)
            except Exception:
                pass
            time.sleep(self.interval)

    @property
    def peak_mb(self) -> float:
        return self.peak / (1024 * 1024)

    @property
    def delta_mb(self) -> float:
        return max(0.0, (self.peak - self.baseline) / (1024 * 1024))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def collect_images(image_root: Path, limit: int) -> list[Path]:
    paths = sorted(image_root.rglob("*.png"))
    if limit > 0:
        paths = paths[:limit]
    if not paths:
        raise RuntimeError(f"no PNG images found under {image_root}")
    return paths


def chunks(items: list[Any], size: int) -> list[list[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def benchmark_mnv4(model_name: str, image_paths: list[Path], batch_size: int, threads: int) -> dict[str, Any]:
    torch.set_num_threads(max(1, threads))
    gc.collect()
    with MemorySampler() as mem_load:
        t0 = time.perf_counter()
        model = timm.create_model(model_name, pretrained=True)
        model.eval()
        config = resolve_model_data_config(model)
        transform = create_transform(**config, is_training=False)
        load_sec = time.perf_counter() - t0

    # Warmup.
    warm_paths = image_paths[: min(len(image_paths), max(batch_size * 2, 8))]
    with torch.inference_mode():
        for group in chunks(warm_paths, batch_size):
            batch = torch.stack([transform(Image.open(path).convert("RGB")) for path in group])
            _ = model(batch)

    gc.collect()
    preprocess_times: list[float] = []
    infer_times: list[float] = []
    total_pages = 0
    with MemorySampler() as mem_run:
        with torch.inference_mode():
            for group in chunks(image_paths, batch_size):
                t_pre = time.perf_counter()
                tensors = []
                for path in group:
                    with Image.open(path) as img:
                        tensors.append(transform(img.convert("RGB")))
                batch = torch.stack(tensors)
                pre_sec = time.perf_counter() - t_pre
                t_inf = time.perf_counter()
                _ = model(batch)
                inf_sec = time.perf_counter() - t_inf
                preprocess_times.append(pre_sec)
                infer_times.append(inf_sec)
                total_pages += len(group)

    pre_total = sum(preprocess_times)
    inf_total = sum(infer_times)
    total = pre_total + inf_total
    result = {
        "model_name": model_name,
        "batch_size": batch_size,
        "threads": threads,
        "pages": total_pages,
        "load_sec": load_sec,
        "load_peak_mb": mem_load.peak_mb,
        "load_delta_mb": mem_load.delta_mb,
        "run_peak_mb": mem_run.peak_mb,
        "run_delta_mb": mem_run.delta_mb,
        "preprocess_total_sec": pre_total,
        "inference_total_sec": inf_total,
        "end_to_end_total_sec": total,
        "preprocess_ms_per_page": pre_total * 1000.0 / max(1, total_pages),
        "inference_ms_per_page": inf_total * 1000.0 / max(1, total_pages),
        "end_to_end_ms_per_page": total * 1000.0 / max(1, total_pages),
        "input_size": config.get("input_size"),
    }
    del model
    gc.collect()
    return result


def benchmark_lgbm(project_root: Path) -> dict[str, Any]:
    tasks = {
        "doc_start": {
            "csv": project_root / "dataset" / "doc_start_pages.csv",
            "model_dir": project_root / "models" / "doc_start",
        },
        "signer_page": {
            "csv": project_root / "dataset" / "signer_pages.csv",
            "model_dir": project_root / "models" / "signer_page",
        },
    }
    out: dict[str, Any] = {}
    for task, spec in tasks.items():
        gc.collect()
        with MemorySampler() as mem_load:
            t0 = time.perf_counter()
            metadata = json.loads((spec["model_dir"] / "metadata.json").read_text(encoding="utf-8"))
            model = joblib.load(spec["model_dir"] / "model.joblib")
            df = pd.read_csv(spec["csv"])
            load_sec = time.perf_counter() - t0
        features = metadata["features"]
        for feature in features:
            df[feature] = pd.to_numeric(df[feature], errors="coerce").fillna(0.0)
        X = df[features]
        # Warmup.
        _ = model.predict_proba(X.head(min(256, len(X))))[:, 1]
        gc.collect()
        with MemorySampler() as mem_run:
            t1 = time.perf_counter()
            _ = model.predict_proba(X)[:, 1]
            predict_sec = time.perf_counter() - t1
        rows = len(df)
        out[task] = {
            "rows": rows,
            "features": features,
            "load_csv_model_sec": load_sec,
            "load_peak_mb": mem_load.peak_mb,
            "load_delta_mb": mem_load.delta_mb,
            "predict_sec": predict_sec,
            "predict_ms_per_page": predict_sec * 1000.0 / max(1, rows),
            "predict_peak_mb": mem_run.peak_mb,
            "predict_delta_mb": mem_run.delta_mb,
        }
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark MNv4 CPU speed/RAM against LightGBM splitter.")
    parser.add_argument("--image-root", default=r"D:\tmp\Train_20260413_143844_YOLO_KIE\dataset\images\test")
    parser.add_argument("--project-root", default=r"D:\tmp\Train_20260413_143844_LGBM_SPLITTER_RELPOS")
    parser.add_argument("--output", default=r"D:\tmp\Train_20260413_143844_LGBM_SPLITTER_RELPOS\reports\mnv4_vs_lgbm_benchmark.json")
    parser.add_argument("--limit-images", type=int, default=300)
    parser.add_argument("--threads", type=int, default=max(1, (os.cpu_count() or 4) // 2))
    parser.add_argument(
        "--models",
        default="mobilenetv4_conv_medium.e500_r224_in1k,mobilenetv4_conv_medium.e500_r256_in1k",
    )
    args = parser.parse_args()

    image_paths = collect_images(Path(args.image_root), args.limit_images)
    report = {
        "image_root": args.image_root,
        "project_root": args.project_root,
        "threads": args.threads,
        "image_count": len(image_paths),
        "mnv4": [],
        "lgbm": benchmark_lgbm(Path(args.project_root)),
    }
    for model_name in [item.strip() for item in args.models.split(",") if item.strip()]:
        for batch_size in (1, 8):
            report["mnv4"].append(benchmark_mnv4(model_name, image_paths, batch_size, args.threads))
    write_json(Path(args.output), report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
