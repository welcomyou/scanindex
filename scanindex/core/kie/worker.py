# -*- coding: utf-8 -*-
"""KIE worker process entry — GĐ K (PLAN KIE 21/10 §2.4; PLAN v5.1 §4.3).

Mỗi process KIE: khởi tạo tokenizer + ONNX session MỘT lần, dùng lại nhiều
job; KHÔNG spawn/load model theo file. Mỗi job xử lý MỘT file: lần lượt gọi
`_run_layoutlmv3(..., selected_pages=[ordinal])` trên TỪNG trang duy nhất rồi
gom kết quả — giữ nguyên ngữ cảnh hậu xử lý từng trang của đường đồng bộ,
không đổi kết quả bằng một lời gọi nhiều trang.

Module top-level, KHÔNG import Qt/screen — an toàn Windows spawn/frozen.
KIE ở đây bóc cơ quan ban hành/cấp trên + năm ban hành phục vụ giải mật;
KHÔNG xác định dấu hoặc độ mật.
"""
from __future__ import annotations

import threading

_worker_state = {"warmed": False, "lock": threading.Lock()}


def warmup_worker() -> bool:
    """Nạp tokenizer + ONNX session một lần/process. Trả False khi thiếu
    model (caller báo model_missing, không retry lặp theo từng file).
    D1: bootstrap worker (stdio + tự-gán Windows Job) TRƯỚC khi nạp ONNX."""
    if _worker_state["warmed"]:
        return True
    with _worker_state["lock"]:
        if _worker_state["warmed"]:
            return True
        from scanindex.infra.bootstrap import bootstrap_worker
        bootstrap_worker()
        try:
            from scanindex.core.kie import engine as kie_engine

            ok = bool(kie_engine.warmup_layoutlmv3())
        except Exception:
            ok = False
        _worker_state["warmed"] = ok
        return ok


def run_job(payload: dict) -> dict:
    """Chạy KIE cho MỘT file trên các trang chỉ định.

    payload tối thiểu: job_id, generation, attempt, canonical_path,
    selected_ordinals (ordinal trong canonical — prefix liên tục), model_fp.
    Trả: {job_id, generation, attempt, status: ready|error, error_code,
    fields_by_source: {source_page: [field_dict]}, timing}.
    Lỗi model (error) KHÔNG được lẫn với "ready nhưng không có field"."""
    import time

    timing = {"t0": time.perf_counter()}
    job_id = payload.get("job_id")
    generation = payload.get("generation")
    attempt = payload.get("attempt", 1)

    def _fail(code: str, message: str) -> dict:
        timing["t_total_s"] = round(time.perf_counter() - timing["t0"], 3)
        return {
            "job_id": job_id, "generation": generation, "attempt": attempt,
            "status": "error", "error_code": code, "error": message,
            "fields_by_source": {}, "timing": timing,
        }

    if not warmup_worker():
        return _fail("model_missing", "LayoutLMv3 ONNX không nạp được")

    try:
        from scanindex.core.canonical_io import load_canonical
        from scanindex.core.kie.engine import _run_layoutlmv3

        canonical_path = payload["canonical_path"]
        canon = load_canonical(canonical_path)
        pages = canon.get("pages") or []
        n_pages = len(pages)
        t_infer = 0.0
        fields_by_source: dict[int, list[dict]] = {}
        empty_pages: list[int] = []
        for ordinal in payload.get("selected_ordinals") or []:
            ordinal = int(ordinal)
            if not (0 <= ordinal < n_pages):
                return _fail(
                    "ordinal_out_of_range",
                    f"ordinal {ordinal} ngoài canonical ({n_pages} trang)",
                )
            words = pages[ordinal].get("words") or []
            if not words:
                empty_pages.append(ordinal)
                continue
            t1 = time.perf_counter()
            page_payload = _run_layoutlmv3(
                canonical_path, selected_pages=[ordinal]
            )
            t_infer += time.perf_counter() - t1
            raw = (page_payload or {}).get("field_instances") or []
            fields_by_source[ordinal] = [
                f for f in raw if isinstance(f, dict)
            ]
        timing["t_infer_s"] = round(t_infer, 3)
        timing["t_total_s"] = round(time.perf_counter() - timing["t0"], 3)
        return {
            "job_id": job_id, "generation": generation, "attempt": attempt,
            "status": "ready", "error_code": None, "error": None,
            "fields_by_source": fields_by_source,
            "empty_pages": empty_pages,
            "timing": timing,
        }
    except Exception as exc:  # lỗi tạm thời (crash/IO) — coordinator retry 1 lần
        return _fail(type(exc).__name__, str(exc))
