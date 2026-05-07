"""
Module-level worker functions for ProcessPoolExecutor.
Must be at top level and free of GUI imports to be picklable.
"""
import os


def init_export_worker():
    """Initializer for export worker process. Sets up sys.path; GMFT is lazy-loaded
    on first export task to avoid holding ~1GB per worker before any work arrives."""
    import sys
    if os.getcwd() not in sys.path:
        sys.path.append(os.getcwd())


def run_export_task(src_pdf, final_docx, metadata=None):
    """
    Run export task in worker process.
    Returns: dict result (success, msg, path, logs)
    """
    src_pdf = os.path.abspath(src_pdf)
    final_docx = os.path.abspath(final_docx)

    try:
        import sys
        import importlib

        if 'table_anchored_merger' not in sys.modules:
            if os.getcwd() not in sys.path:
                sys.path.append(os.getcwd())
            import table_anchored_merger
        else:
            import table_anchored_merger
            try:
                importlib.reload(table_anchored_merger)
            except Exception:
                pass

        try:
            success, msg, logs = table_anchored_merger.create_docx_from_pdf(
                src_pdf, final_docx, no_log_file=True, metadata=metadata
            )
        except (ValueError, TypeError):
            success, msg = table_anchored_merger.create_docx_from_pdf(src_pdf, final_docx)
            logs = "Log unavailable (Old module version loaded)"

        return {"success": success, "msg": msg, "path": final_docx, "logs": logs}

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        return {"success": False, "msg": f"Process Crash: {str(e)}\n{tb}", "path": final_docx, "logs": ""}


def isolate_export_process(src_pdf, final_docx, q, metadata=None):
    """Wrapper for multiprocessing.Process target. Puts result into queue."""
    res = run_export_task(src_pdf, final_docx, metadata=metadata)
    q.put(res)
