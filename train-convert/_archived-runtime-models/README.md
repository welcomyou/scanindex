# Archived Table Runtime Artifacts

The unused model payloads previously listed here have been moved to
`temp/unused_models_20260506/`. This folder remains as a pointer so the
train/convert decision record is still easy to follow.

Current production table pipeline:

```text
DocLayout table bbox
  -> GMFT-ONNX structure candidate
  -> Docling TableFormer v1 accurate step-cache ONNX structure candidate
  -> geometry/OCR-fit selector
  -> Postprocess V2 geometry cell text fill
```

Moved artifacts:

| Artifact | New location | Reason |
| --- | --- | --- |
| `docling_tableformer_v1_onnx/` | `temp/unused_models_20260506/table_pipeline/` | Fixed-cache Docling v1 ONNX matched accuracy, but was materially slower than the step-cache ONNX export. |
| `gmft_models/` | `temp/unused_models_20260506/table_pipeline/` | Legacy PyTorch/Hugging Face Table Transformer cache. Runtime now uses `models/gmft_onnx/` instead. |

Benchmark-only engines such as Img2Table, RapidTable SLANet+, and
wired_table_rec_v2 are not loaded by runtime. Their scripts remain under
`scripts/` for future research, with dependencies installed manually when a new
benchmark is needed.
