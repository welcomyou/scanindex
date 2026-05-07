# Docling TableFormer v1 ONNX Convert

This folder preserves the reproducible conversion path for the production
Docling TableFormer v1 accurate step-cache ONNX artifacts.

Runtime loads artifacts from:

```text
models/docling_tableformer_v1_stepcache_onnx/
  tm_config.json
  onnx/accurate/tableformer_accurate_encoder.onnx
  onnx/accurate/tableformer_accurate_encoder.onnx.data
  onnx/accurate/tableformer_accurate_decoder_step.onnx
  onnx/accurate/tableformer_accurate_decoder_step.onnx.data
  onnx/accurate/tableformer_accurate_bbox_decoder.onnx
  onnx/accurate/tableformer_accurate_bbox_decoder.onnx.data
```

## Inputs

The converter expects:

- `docling-ibm-models` source checkout at
  `temp/tableformer_onnx/docling-ibm-models`, or `DOCLING_IBM_MODELS_SRC`.
- Hugging Face `docling-project/docling-models` TableFormer artifacts under
  `temp/tableformer_onnx/hf_cache/.../model_artifacts/tableformer`, or
  `DOCLING_MODELS_TABLEFORMER_ROOT`.

## Export

Run from repository root:

```powershell
python train-convert\docling-tableformer-v1\convert\export_docling_v1_tableformer_stepcache_onnx.py accurate
```

The default output is `models/docling_tableformer_v1_stepcache_onnx`. Override
with `TABLEFORMER_ONNX_ARTIFACT_ROOT` when comparing export variants.

## Validate ONNX Step Cache

After export, the reference runner can compare ONNX output against the golden
PyTorch capture emitted by the exporter:

```powershell
python train-convert\docling-tableformer-v1\convert\onnx_stepcache_runner_reference.py
```

The production runtime is `docling_tableformer_v1_onnx_engine.py`. It uses
ONNX Runtime for the neural model and keeps Docling v1's official
`CellMatcher`, `MatchingPostProcessor`, and OTSL response path for structure
matching/post-processing.
