r"""Isolated secret-scan benchmark; count only persisted results with complete metadata.

python scripts/bench_secret_scan.py --corpus D:\corpus_bench --runs 3
    --data-dir D:\bench_data --profile W=16,F=8,CHUNK=8,KIE_PROC=2,PREP_PROC=2 --force-rescan
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
import uuid

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
DEFAULT_PROFILE = {"W": 16, "F": 8, "CHUNK": 8, "KIE_PROC": 2, "PREP_PROC": 2, "WARMUP_K": 4}


def _write(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _sha(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024**2), b""):
            digest.update(block)
    return digest.hexdigest()


def apply_profile(profile: dict) -> None:
    effective = {**DEFAULT_PROFILE, **profile}
    if set(effective) != set(DEFAULT_PROFILE) or any(int(v) <= 0 for v in effective.values()):
        raise ValueError("Profile keys must be W,F,CHUNK,KIE_PROC,PREP_PROC,WARMUP_K with positive values")
    names = {"W": "DIRECT_OCR_NUM_PAGE_WORKERS", "F": "SECRET_SCAN_MAX_FILE_WORKERS",
             "CHUNK": "SECRET_SCAN_STREAM_CHUNK", "KIE_PROC": "OCRTOOL_KIE_PROCESSES",
             "PREP_PROC": "OCRTOOL_PREP_PROCESSES", "WARMUP_K": "OCRTOOL_WARMUP_STAGE"}
    for key, env in names.items():
        os.environ[env] = str(effective[key])
    os.environ.update(OCRTOOL_KIE_DEFERRED="1", OCRTOOL_PREP_MODE="prep-process",
                      LAYOUTLMV3_ONNX_THREADS="2", SECRET_SCAN_DISABLE_KIE_YEAR="0",
                      SECRET_SCAN_DISABLE_KIE_ORG="0")


def prepare_data_dir(data_dir: str, corpus: str | None = None) -> None:
    from scanindex.infra.paths import get_app_dir
    target, app = Path(data_dir).resolve(), Path(get_app_dir()).resolve()
    if target == Path(target.anchor) or target == app or target in app.parents:
        raise ValueError("Benchmark data-dir cannot be the app directory or its ancestor")
    if corpus:
        source = Path(corpus).resolve()
        if target == source or target in source.parents or source in target.parents:
            raise ValueError("Benchmark data-dir and corpus must be separate trees")
    marker = target / ".ocrtool-benchmark"
    if target.exists() and any(target.iterdir()) and not marker.is_file():
        raise ValueError("Nonempty data-dir is not owned by this benchmark")
    target.mkdir(parents=True, exist_ok=True)
    if not marker.exists():
        marker.write_text("ocrtool-secret-scan-benchmark-v1\n", encoding="ascii")
    if marker.read_text(encoding="ascii").strip() != "ocrtool-secret-scan-benchmark-v1":
        raise ValueError("Invalid benchmark data-dir marker")
    os.environ["SCANINDEX_BASE_DIR"] = str(target)


def acquire_benchmark_lock(path: Path) -> int:
    """An OS lock tolerates stale files, but is released on parent crash."""
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0))
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b" ")
        os.lseek(fd, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BaseException:
        os.close(fd)
        raise


def force_rescan_namespace(data_dir: str) -> str:
    """Keep warmed pools, but use fresh durable state; never delete live spool."""
    from scanindex.core import secret_scan_kie as sk
    from scanindex.core.preprocessing import prep_worker as pw
    from scanindex.core.ocr import direct_engine as de
    kie = sk.telemetry()
    if isinstance(kie, dict) and any(kie.get(k, 0) for k in ("outstanding", "deliveries_pending")):
        raise RuntimeError("Cannot change namespace while KIE is active")
    if pw.telemetry().get("in_flight", 0):
        raise RuntimeError("Cannot change namespace while prep is active")
    with de._admission_lock:
        if de._live_inputs:
            raise RuntimeError("Cannot change namespace while native input leases exist")
    namespace = "run_" + uuid.uuid4().hex
    os.environ["SCANINDEX_PROGRESS_NAMESPACE"] = namespace
    return namespace


def model_preflight() -> dict:
    from scanindex.infra.paths import get_app_dir
    from scanindex.core.ocr import direct_engine as de
    from scanindex.core.kie import engine as kie
    from scanindex.core.digitization import page_splitter
    dll, ocr = de._find_screen_ai_paths()
    model_dir = Path(kie._layoutlmv3_dir())
    required = [Path(dll) if dll else Path(get_app_dir()) / "MISSING_SCREEN_AI",
                model_dir / "layoutlmv3_fontgray_norm_final_epoch25.int8.onnx",
                model_dir / "label_list.json", model_dir / "tokenizer_config.json",
                Path(get_app_dir()) / "models/orientation/PP-LCNet_x1_0_doc_ori.onnx"]
    splitter = next((p for p in page_splitter._candidate_model_paths() if p.is_file()), None)
    if splitter is None:
        raise FileNotFoundError("LightGBM doc-start model missing")
    required.append(splitter)
    missing = [str(p) for p in required if not p.is_file()]
    if missing:
        raise FileNotFoundError(f"Missing installed models: {missing}")
    files = set(required)
    for directory in (Path(ocr), model_dir):
        files.update(p for p in directory.rglob("*") if p.is_file())
    hashes = {str(p): _sha(p) for p in sorted(files)}
    app_root = Path(get_app_dir())
    portable_hashes = {str(p.relative_to(app_root)) if p.is_relative_to(app_root) else p.name: hashes[str(p)]
                       for p in sorted(files)}
    fingerprint = hashlib.sha256(json.dumps(portable_hashes, sort_keys=True).encode()).hexdigest()
    return {"model_fp": fingerprint, "files": hashes}


class Timeline:
    def __init__(self, out_path: str, interval: float = 1.0):
        self._out, self._interval = out_path, interval
        self._stop = threading.Event()
        self._thread = None
        self._previous = {}
        self._last = None
        self.error = None

    def _sample(self) -> dict:
        from scanindex.infra.process_metrics import sample_tree
        from scanindex.core.ocr import direct_engine as de
        from scanindex.core import secret_scan_kie as sk
        from scanindex.core.preprocessing import prep_worker as pw
        from scanindex.core.secret_scan_progress import progress_dir
        now = time.monotonic()
        row = {"t": now, "wall": time.strftime("%H:%M:%S")}
        try:
            row.update(sample_tree())
            cores = None
            if self._last is not None:
                delta = sum(max(0, p["cpu_s"] - self._previous.get(p["identity"], p["cpu_s"]))
                            for p in row["processes"])
                cores = delta / max(.001, now - self._last)
            row["tree_cpu_cores"] = cores
            row["tree_cpu_percent_machine"] = None if cores is None else 100 * cores / (os.cpu_count() or 1)
            self._previous = {p["identity"]: p["cpu_s"] for p in row["processes"]}
            self._last = now
        except Exception as exc:
            row["metrics_error"] = str(exc)
            self.error = str(exc)
        row.update(ocr_admission=de.pool_telemetry(), kie=sk.telemetry(), prep=pw.telemetry())
        from scanindex.core.secret_scan_spool import storage_telemetry
        row["kie_spool_storage"] = storage_telemetry()
        spool_bytes = 0
        for path in Path(progress_dir(), "kie_spool").rglob("*"):
            try:
                if path.is_file():
                    spool_bytes += path.stat().st_size
            except FileNotFoundError:
                pass  # Per-file durable GC may remove a snapshot during sampling.
        row["kie_spool_bytes"] = spool_bytes
        return row

    def _loop(self):
        try:
            with open(self._out, "w", encoding="utf-8", buffering=1) as fh:
                while not self._stop.is_set():
                    try:
                        fh.write(json.dumps(self._sample(), ensure_ascii=False) + "\n")
                    except Exception as exc:
                        self.error = str(exc)
                        fh.write(json.dumps({"sampling_error": str(exc)}) + "\n")
                    self._stop.wait(self._interval)
        except Exception as exc:
            self.error = str(exc)

    def start(self):
        self._thread = threading.Thread(target=self._loop, name="bench-timeline", daemon=True)
        self._thread.start()

    def stop_and_save(self):
        self._stop.set()
        if self._thread:
            self._thread.join(3)
            if self._thread.is_alive():
                self.error = "Sampler did not stop"
        return self._out


def run_one_pass(corpus: str, mode_flag: str, meta_out: str, timeout_s=21600) -> dict:
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QObject, Slot
    from scanindex.ui.screens.secret_file_scan_screen import SecretFileScanScreen, _iter_supported_files
    from scanindex.core import secret_scan_progress as ssp
    from scanindex.infra.version import get_version
    app = QApplication.instance()
    if app is None:
        raise RuntimeError("QApplication must exist before constructing the scan screen")
    screen = SecretFileScanScreen()
    screen.history_checkbox.setChecked(True)
    summary, errors = {}, []
    class Receiver(QObject):
        @Slot(object)
        def finished(self, payload):
            summary.update(payload)
    receiver = Receiver()
    screen._scan_finished.connect(receiver.finished)
    def run():
        try:
            screen._run_worker(corpus, mode_flag == "firstpage", None)
        except BaseException as exc:
            errors.append(repr(exc))
    worker = threading.Thread(target=run, name="bench-scan", daemon=True)
    t0 = time.monotonic()
    worker.start()
    try:
        while worker.is_alive():
            app.processEvents()
            if time.monotonic() - t0 >= timeout_s:
                screen._cancel_event.set()
                raise TimeoutError("Benchmark scan exceeded deadline")
            time.sleep(.01)
        app.processEvents()
        if errors or not summary:
            raise RuntimeError(f"Scan did not produce a summary: {errors}")
        mode = str(summary["mode"])
        progress = ssp.SecretScanProgress.load(corpus, mode)
        try:
            pending = len(progress.rescan_pending) if progress else 0
            pending += len(progress.kie_spool_pending()) if progress else 0
        finally:
            if progress:
                progress.close()
        registry = ssp.FileRegistry.load()
        try:
            complete = 0
            for name in _iter_supported_files(corpus):
                st = os.stat(name)
                if registry.lookup_with_status(name, st.st_size, st.st_mtime, mode, get_version()) is not None:
                    complete += 1
        finally:
            registry.close()
        elapsed = time.monotonic() - t0
        cache = int(summary.get("cache_skipped", 0))
        summary.update(wall_s=elapsed, durable_complete=complete, durable_new=max(0, complete-cache),
                       persisted_pending=pending, namespace=os.environ["SCANINDEX_PROGRESS_NAMESPACE"])
        summary["benchmark_pass"] = (
            summary.get("durability_confirmed") is True and not summary.get("cancelled")
            and not any(summary.get(k, 0) for k in ("failures", "junk", "rescan_pending", "metadata_pending"))
            and pending == 0 and complete == summary["total"] and complete > 0)
        summary["files_per_min"] = (max(0, complete-cache) * 60 / elapsed) if summary["benchmark_pass"] else None
        _write(meta_out, summary)
        return summary
    finally:
        if not worker.is_alive():
            screen.deleteLater()
            receiver.deleteLater()
            app.processEvents()


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    import multiprocessing
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--mode", choices=["thorough", "firstpage"], default="thorough")
    parser.add_argument("--profile", default="W=16,F=8,CHUNK=8,KIE_PROC=2,PREP_PROC=2")
    parser.add_argument("--force-rescan", action="store_true")
    parser.add_argument("--out-dir")
    parser.add_argument("--timeout-s", type=float, default=21600)
    args = parser.parse_args(argv)
    result_code, lock_fd, lock_path, out_dir, app = 2, None, None, None, None
    try:
        if args.runs <= 0 or args.timeout_s <= 0 or not Path(args.corpus).is_dir():
            raise ValueError("A corpus directory, positive runs and timeout are required")
        profile = dict(DEFAULT_PROFILE)
        for item in args.profile.split(","):
            key, value = item.split("=", 1)
            profile[key.strip()] = int(value)
        apply_profile(profile)  # BEFORE imports that read worker settings once.
        prepare_data_dir(args.data_dir, args.corpus)
        lock_path = Path(args.data_dir).resolve() / ".benchmark-running"
        lock_fd = acquire_benchmark_lock(lock_path)
        os.environ["SCANINDEX_PROGRESS_NAMESPACE"] = "run_" + uuid.uuid4().hex
        out_dir = Path(args.out_dir) if args.out_dir else Path(args.data_dir) / "bench_results" / uuid.uuid4().hex
        out_dir.mkdir(parents=True, exist_ok=True)
        from scanindex.infra.bootstrap import bootstrap_harness
        bootstrap_harness()
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        from scanindex.ui.screens import secret_file_scan_screen as ss
        from scanindex.core.ocr import direct_engine as de
        from scanindex.core import secret_scan_kie as sk
        from scanindex.core.preprocessing import prep_worker as pw
        files = list(ss._iter_supported_files(args.corpus))
        if not files:
            raise ValueError("Corpus contains no supported documents")
        models = model_preflight()
        effective = {"W": de._NUM_PAGE_WORKERS, "F": ss._file_worker_count(len(files)),
                     "CHUNK": ss._stream_chunk_size(), "KIE_PROC": sk._default_processes(),
                     "PREP_PROC": pw.processes(), "KIE_ENABLED": sk.deferred_enabled(),
                     "PREP_ENABLED": pw.enabled(), "KIE_THREADS": os.environ["LAYOUTLMV3_ONNX_THREADS"]}
        if getattr(sys, "frozen", False):
            code_hashes = {"executable": _sha(sys.executable)}
        else:
            code_hashes = {str(p.relative_to(REPO)): _sha(p) for p in
                          [REPO / "ocr_app.py", Path(__file__)] + sorted((REPO / "scanindex").rglob("*.py"))}
        _write(out_dir / "manifest.json", {"requested": profile, "effective": effective,
               "models": models, "corpus": str(Path(args.corpus).resolve()), "mode": args.mode,
               "python": sys.version, "logical_cpus": os.cpu_count(),
               "frozen": bool(getattr(sys, "frozen", False)), "code_hashes": code_hashes})
        results = []
        for index in range(args.runs):
            if args.force_rescan:
                force_rescan_namespace(args.data_dir)
            sampler = Timeline(str(out_dir / f"timeline_run{index+1}.jsonl"))
            sampler.start()
            try:
                summary = run_one_pass(args.corpus, args.mode, str(out_dir / f"meta_run{index+1}.json"), args.timeout_s)
            finally:
                sampler.stop_and_save()
            summary["sampling_error"] = sampler.error
            results.append(summary)
            _write(out_dir / "results.json", results)
            print(json.dumps(summary, ensure_ascii=False))
            if not summary["benchmark_pass"] or sampler.error:
                raise RuntimeError("Incomplete scan or invalid metrics; benchmark rejected")
        result_code = 0
    except Exception as exc:
        print(f"BENCH FAILED: {exc}", file=sys.stderr)
        if out_dir is not None:
            _write(out_dir / "failure.json", {"error": repr(exc)})
    finally:
        if app is not None:
            from scanindex.core.runtime.shutdown import shutdown_all
            if not shutdown_all():
                print("BENCH FAILED: worker exit unconfirmed", file=sys.stderr)
                result_code = 2
            app.processEvents()
        if lock_fd is not None:
            os.close(lock_fd)
        from scanindex.infra.windows_job import close_app_job
        close_app_job()
    return result_code


if __name__ == "__main__":
    raise SystemExit(main())
