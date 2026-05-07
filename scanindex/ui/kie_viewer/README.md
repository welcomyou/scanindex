# KIE Viewer

Desktop GUI (PySide6) for reviewing and editing Vietnamese administrative-document
Key Information Extraction (KIE) outputs produced by the KIE pipeline.

## Files

| File | Purpose |
|------|---------|
| `kie_viewer.py` | Main app — `KieViewer` window, all widgets, signals, render pipeline |
| `__main__.py` | Entry point so the package is runnable: `python -m kie_viewer` |
| `__init__.py` | Re-exports `main` for use as a library |
| `kie_viewer_config.json` | Runtime paths (input/output/ocr dirs). Loaded on startup. |
| `audit_viewer_issues.py` | Standalone audit script — scans labeled JSONs for patterns the viewer should catch |

## Run

From the repo root (`d:\App\ocrtool\`):

```bash
python -m kie_viewer
# or
python kie_viewer/kie_viewer.py
```

A custom config path can be passed via the `KIE_VIEWER_CONFIG` env var.

## Configuration

`kie_viewer_config.json` controls the three working directories the viewer
walks. Edit directly or use the in-app **Cấu hình** button:

```json
{
  "input_dir":  "D:/path/to/json_input",
  "output_dir": "D:/path/to/json_output_labeled",
  "ocr_dir":    "D:/path/to/ocr"
}
```

## Dependencies (already in repo's main env)

- PySide6, PyMuPDF (fitz), opencv-python, numpy, onnxruntime, orjson (optional)
- `kie_core.labeling_workspace.validate_label_output_detailed` for save-time validation
- `kie_core.inference_pipeline.detect_secrecy_mark` for classified-document detection
- `scanindex.core.preprocessing.preprocessing` for the orientation classifier

## Audit script

```bash
python kie_viewer/audit_viewer_issues.py
```

Prints a markdown report to stdout, suggesting viewer improvements based on
real labeled batch data. Read-only — does not modify any files.
