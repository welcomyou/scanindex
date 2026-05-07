from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import psutil


DEFAULT_CONFIGS = [
    {"name": "t9_i1_b1", "threads": 9, "inter_op_threads": 1, "batch_size": 1},
    {"name": "t9_i1_b2", "threads": 9, "inter_op_threads": 1, "batch_size": 2},
    {"name": "t9_i1_b4", "threads": 9, "inter_op_threads": 1, "batch_size": 4},
    {"name": "t9_i1_b8", "threads": 9, "inter_op_threads": 1, "batch_size": 8},
    {"name": "t11_i1_b1", "threads": 11, "inter_op_threads": 1, "batch_size": 1},
    {"name": "t12_i1_b4", "threads": 12, "inter_op_threads": 1, "batch_size": 4},
    {"name": "t12_i1_b8", "threads": 12, "inter_op_threads": 1, "batch_size": 8},
    {"name": "t12_i2_b4", "threads": 12, "inter_op_threads": 2, "batch_size": 4},
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run LayoutLMv3 ONNX INT8 speed grid with CPU/RAM monitoring.")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--onnx-path", required=True)
    parser.add_argument("--label-rel-prefix", default="batch_0027/")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--configs", nargs="*", help="Optional config names from the default grid.")
    parser.add_argument("--sample-interval", type=float, default=1.0)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--max-length", type=int, default=512)
    parser.add_argument("--stride", type=int, default=128)
    return parser.parse_args()


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    if len(values) == 1:
        return float(values[0])
    pos = (len(values) - 1) * pct
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return float(values[lo])
    return float(values[lo] * (hi - pos) + values[hi] * (pos - lo))


def proc_tree(process: psutil.Process) -> list[psutil.Process]:
    try:
        children = process.children(recursive=True)
    except psutil.Error:
        children = []
    return [process, *children]


def cpu_time(process: psutil.Process) -> float:
    total = 0.0
    for item in proc_tree(process):
        try:
            times = item.cpu_times()
            total += float(times.user) + float(times.system)
        except psutil.Error:
            continue
    return total


def memory_info_mb(process: psutil.Process) -> tuple[float, float]:
    rss = 0.0
    private = 0.0
    for item in proc_tree(process):
        try:
            mem = item.memory_info()
            rss += float(mem.rss)
            full = item.memory_full_info()
            private += float(getattr(full, "private", 0.0) or getattr(full, "uss", 0.0) or 0.0)
        except psutil.Error:
            continue
    return rss / (1024.0 * 1024.0), private / (1024.0 * 1024.0)


def safe_read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def summarize_samples(samples: list[dict[str, float]], logical_cpus: int) -> dict[str, Any]:
    proc_cpu = [row["process_cpu_percent_total"] for row in samples]
    core_equiv = [row["process_core_equivalent"] for row in samples]
    rss = [row["rss_mb"] for row in samples]
    private = [row["private_mb"] for row in samples if row["private_mb"] > 0]
    system_cpu = [row["system_cpu_percent"] for row in samples]
    system_ram = [row["system_ram_percent"] for row in samples]
    return {
        "samples": len(samples),
        "logical_cpus": logical_cpus,
        "process_cpu_percent_total_avg": statistics.fmean(proc_cpu) if proc_cpu else 0.0,
        "process_cpu_percent_total_p50": percentile(proc_cpu, 0.50),
        "process_cpu_percent_total_p95": percentile(proc_cpu, 0.95),
        "process_cpu_percent_total_max": max(proc_cpu) if proc_cpu else 0.0,
        "process_core_equivalent_avg": statistics.fmean(core_equiv) if core_equiv else 0.0,
        "process_core_equivalent_p95": percentile(core_equiv, 0.95),
        "process_core_equivalent_max": max(core_equiv) if core_equiv else 0.0,
        "rss_mb_avg": statistics.fmean(rss) if rss else 0.0,
        "rss_mb_p95": percentile(rss, 0.95),
        "rss_mb_max": max(rss) if rss else 0.0,
        "private_mb_avg": statistics.fmean(private) if private else 0.0,
        "private_mb_p95": percentile(private, 0.95),
        "private_mb_max": max(private) if private else 0.0,
        "system_cpu_percent_avg": statistics.fmean(system_cpu) if system_cpu else 0.0,
        "system_cpu_percent_p95": percentile(system_cpu, 0.95),
        "system_cpu_percent_max": max(system_cpu) if system_cpu else 0.0,
        "system_ram_percent_avg": statistics.fmean(system_ram) if system_ram else 0.0,
        "system_ram_percent_max": max(system_ram) if system_ram else 0.0,
    }


def monitor_process(
    proc: subprocess.Popen,
    progress_path: Path,
    state: dict[str, Any],
    sample_interval: float,
) -> tuple[int, list[dict[str, float]]]:
    logical_cpus = psutil.cpu_count(logical=True) or 1
    process = psutil.Process(proc.pid)
    psutil.cpu_percent(interval=None)
    previous_wall = time.perf_counter()
    previous_cpu = cpu_time(process)
    samples: list[dict[str, float]] = []
    while proc.poll() is None:
        time.sleep(max(0.2, sample_interval))
        now = time.perf_counter()
        current_cpu = cpu_time(process)
        elapsed = max(1e-9, now - previous_wall)
        cpu_delta = max(0.0, current_cpu - previous_cpu)
        core_equiv = cpu_delta / elapsed
        rss_mb, private_mb = memory_info_mb(process)
        sample = {
            "elapsed_seconds": now - state["case_start_time"],
            "process_core_equivalent": core_equiv,
            "process_cpu_percent_total": core_equiv * 100.0 / logical_cpus,
            "rss_mb": rss_mb,
            "private_mb": private_mb,
            "system_cpu_percent": float(psutil.cpu_percent(interval=None)),
            "system_ram_percent": float(psutil.virtual_memory().percent),
        }
        samples.append(sample)
        previous_wall = now
        previous_cpu = current_cpu
        state["last_sample"] = sample
        state["sample_summary"] = summarize_samples(samples, logical_cpus)
        write_json(progress_path, state)
    return int(proc.wait()), samples


def run_case(args: argparse.Namespace, config: dict[str, Any], output_dir: Path, progress_path: Path, state: dict[str, Any]) -> dict[str, Any]:
    name = config["name"]
    case_output = output_dir / f"layout_int8_{name}.json"
    stdout_path = output_dir / f"layout_int8_{name}.stdout.txt"
    stderr_path = output_dir / f"layout_int8_{name}.stderr.txt"
    command = [
        sys.executable,
        str(Path(__file__).with_name("14-evaluate_layoutlmv3_batch_onnx_cpu.py")),
        "--project-root",
        args.project_root,
        "--model-path",
        args.model_path,
        "--onnx-path",
        args.onnx_path,
        "--label-rel-prefix",
        args.label_rel_prefix,
        "--threads",
        str(config["threads"]),
        "--inter-op-threads",
        str(config["inter_op_threads"]),
        "--batch-size",
        str(config["batch_size"]),
        "--max-length",
        str(args.max_length),
        "--stride",
        str(args.stride),
        "--warmup",
        str(args.warmup),
        "--output",
        str(case_output),
    ]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["TOKENIZERS_PARALLELISM"] = "false"
    for key in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        env[key] = str(config["threads"])

    state.update(
        {
            "status": "running",
            "current_case": name,
            "current_config": config,
            "case_start_time": time.perf_counter(),
            "case_output": str(case_output),
            "last_sample": None,
            "sample_summary": None,
        }
    )
    write_json(progress_path, state)
    wall_start = time.perf_counter()
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
        proc = subprocess.Popen(command, cwd=Path(__file__).resolve().parents[1], env=env, stdout=stdout, stderr=stderr)
        return_code, samples = monitor_process(proc, progress_path, state, args.sample_interval)
    wall_seconds = time.perf_counter() - wall_start
    monitor_summary = summarize_samples(samples, psutil.cpu_count(logical=True) or 1)
    result = safe_read_json(case_output)
    case_report = {
        "name": name,
        "config": config,
        "return_code": return_code,
        "wall_seconds_observed": wall_seconds,
        "output": str(case_output),
        "stdout": str(stdout_path),
        "stderr": str(stderr_path),
        "monitor": monitor_summary,
        "timing": result.get("timing", {}),
        "accuracy_all": {
            key: result.get("splits", {}).get("all", {}).get(key)
            for key in ("word_f1", "span_f1", "exact_instance_accuracy", "errors")
        },
    }
    if return_code != 0:
        case_report["error"] = "subprocess returned non-zero exit code"
    return case_report


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    progress_path = output_dir / "grid_progress.json"
    summary_path = output_dir / "grid_summary.json"
    wanted = set(args.configs or [cfg["name"] for cfg in DEFAULT_CONFIGS])
    configs = [cfg for cfg in DEFAULT_CONFIGS if cfg["name"] in wanted]
    unknown = sorted(wanted - {cfg["name"] for cfg in DEFAULT_CONFIGS})
    if unknown:
        raise SystemExit(f"Unknown config names: {unknown}")

    state: dict[str, Any] = {
        "status": "starting",
        "output_dir": str(output_dir),
        "summary": str(summary_path),
        "planned_configs": configs,
        "completed": [],
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    write_json(progress_path, state)
    reports: list[dict[str, Any]] = []
    for config in configs:
        report = run_case(args, config, output_dir, progress_path, state)
        reports.append(report)
        state["completed"] = reports
        state["current_case"] = None
        state["status"] = "case_failed" if report["return_code"] != 0 else "case_completed"
        write_json(progress_path, state)
        write_json(summary_path, {"reports": reports})
        if report["return_code"] != 0:
            break

    state["completed"] = reports
    state["status"] = "completed" if all(item["return_code"] == 0 for item in reports) else "failed"
    state["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    write_json(progress_path, state)
    write_json(summary_path, {"reports": reports})
    print(json.dumps({"summary": str(summary_path), "reports": reports}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
