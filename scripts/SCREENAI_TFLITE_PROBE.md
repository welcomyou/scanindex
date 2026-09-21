# ScreenAI models with standalone LiteRT: experiment results

**Update:** independent bounding-box decoding and full-page line OCR now have a
working prototype. See [SCREENAI_TFLITE_BBOX.md](SCREENAI_TFLITE_BBOX.md). This file
records the earlier recognition-only experiment and its original limitations.

Tested on 2026-09-20, Windows x64, Python 3.12.0, ai-edge-litert 2.2.0,
CPU/XNNPACK, 2 threads. Model package: `models/screen_ai/148.13`.

## Outcome

**Standalone recognition of Vietnamese line images works.**
`screenai_tflite_probe.py` uses LiteRT's own interpreter, not the ScreenAI DLL,
its exported TFLite functions, or the production Python binding.

This is not an Android application or an independent full-page OCR pipeline yet.
Android CPU execution is a plausible next experiment, not a verified result.

| Experiment | Result |
| --- | --- |
| Synthetic text, 3 fonts (Arial, Times New Roman, Arial Bold) | 39/39 exact, including 3 blank images; Vietnamese accents, long lines, repeated characters and numbers |
| Recognition of 446 line crops from 15 existing scan pages | Runs successfully; 379/446 lines exactly match native ScreenAI after whitespace/NFC normalization |
| Standalone recognition CER / WER against existing ground truth | **3.9901% / 7.6220%** |
| Native ScreenAI 148.13 CER / WER against the same ground truth | **3.9625% / 6.2047%** |
| Allocation of all 19 `.tflite` files | 18 succeed; language-ID model fails on unresolved `NGramHash` |
| Detector inference on a real page and a blank control | Both run; all 11 output tensors are finite and change with image content |

CER is character edit distance divided by reference character count; WER uses
words. Lower is better. Evaluation normalizes Unicode to NFC and collapses
whitespace, while preserving case, punctuation and accents. The reference has
14,511 normalized characters / 3,175 words. Standalone recognition makes 579
character edits and 242 word edits; native ScreenAI makes 575 / 197.

**Benchmark scope:** the line boxes, crops and their order come from native
ScreenAI in a separate process (`screenai_tflite_reference.py`). The benchmark
isolates recognition and does not demonstrate independent detection. Native
ScreenAI predictions are not ground truth; the accuracy metrics above use the
existing `temp/ocrv6_vs_screenai_benchmark/groundtruth.txt` file. Neither reference
text nor ground truth is passed to the model or decoder. No language correction,
dictionary, beam search, FST or language-ID model is used.

The 39 synthetic cases and real scans are exploratory checks, not an unseen
representative evaluation set. No claim of equivalent quality across documents,
handwriting, scripts or devices follows from these results.

## Recognition implementation

- Model: `gocr_mobile_und.tflite`, 3,017,688 bytes (about 2.88 MiB).
- Label map: `gocr_mobile_und_label_map.pb`, 12,358 bytes. The probe reads its
  protobuf wire fields: repeated field 1 entries, entry field 1 UTF-8 label,
  entry field 2 integer ID. An omitted ID defaults to zero; an omitted label
  defaults to the empty string. It verifies contiguous IDs.
- Input: grayscale `uint8`, `[1, 32, 168, 1]`. Pass pixel bytes directly; do not
  divide by 255 and then cast back to `uint8`.
- Text output: `StatefulPartitionedCall:2`, `[1, 42, 1293]`, quantized `uint8`.
  Scalar quantization preserves argmax ordering; no softmax is needed for greedy
  decoding. There are 1,292 labels and CTC blank ID 1,292.
- Resize a pre-cropped horizontal line to height 32, preserving aspect ratio.
  Use 168px windows with 32px context on either side, advancing 104px. Keep the
  central 26 of 42 time steps and stitch token IDs before CTC collapse.
- CTC collapse merges consecutive equal IDs, then removes blank IDs. This order
  preserves repeated characters separated by blanks. Finally normalize to NFC
  and trim edge whitespace.

Windowing and preprocessing are experimental approximations, not a recovered
Google specification. Errors at window boundaries, poor crops, rotation and
unusual font sizes remain possible. Word error rate is worse than the full
engine in this experiment even though character error rate is close.

Recognition took about 4.35 seconds for the 446 crops, including resizing,
windowing and greedy decoding but excluding image loading, model initialization,
and detection. The native full-page reference calls took about 7.10 seconds and
include detection, so these timings are **not an equivalent speed comparison**.
They are desktop measurements, not Android estimates.

## Detector and language-ID limitations

The detector accepts four grayscale inputs with spatial dimensions 4096, 1280,
640 and 160. It emits eleven dense tensors with seven channels each, not final
boxes. The smoke test resizes the page to each input as a diagnostic only; this
is not verified ScreenAI preprocessing. It checks finite output and compares it
against a blank-image control, saving raw tensors. Bounding-box geometry,
thresholding, grouping, rotated text and suppression still need implementation.
The real-page invoke measured about 0.58s on this desktop, using two threads.

The language-ID model declares `NGramHash` and `KmeansEmbeddingLookup`; standard
LiteRT fails allocation at the first unresolved operator. The tested Vietnamese
recognizer can be called directly without this model, so language ID is not
required for this limited path. Allocation of the other language models is not
proof that their full recognition pipelines work.

## Reproduce

Run from the repository root in PowerShell. The experiment uses its own virtual
environment; no application dependency or production OCR code is changed.

```powershell
python -m venv .venv_screenai_probe
.venv_screenai_probe/Scripts/python -m pip install -r scripts/requirements-screenai-tflite.txt

# Known synthetic text; on non-Windows pass --font /path/to/a/font.ttf.
.venv_screenai_probe/Scripts/python scripts/screenai_tflite_probe.py demo

# Inspect operators, tensor shapes, model hashes and allocation results.
.venv_screenai_probe/Scripts/python scripts/screenai_tflite_probe.py inspect

# Recognize one already-cropped horizontal line independently.
.venv_screenai_probe/Scripts/python scripts/screenai_tflite_probe.py recognize --image temp/screenai_tflite_probe/reference/crops/page_001_240dpi_007.png

# Diagnostic inference only: this does NOT produce detected text boxes.
.venv_screenai_probe/Scripts/python scripts/screenai_tflite_probe.py detector-smoke --image temp/ocrv6_vs_screenai_benchmark/page_images/page_001_240dpi.png

# Optional reference generation: this command DOES load the ScreenAI DLL.
.venv_screenai_probe/Scripts/python scripts/screenai_tflite_reference.py --pages temp/ocrv6_vs_screenai_benchmark/page_images --output temp/screenai_tflite_probe/reference

# Independent recognition on the exported crops; no DLL loaded in this process.
.venv_screenai_probe/Scripts/python scripts/screenai_tflite_probe.py benchmark --manifest temp/screenai_tflite_probe/reference/manifest.json --ground-truth temp/ocrv6_vs_screenai_benchmark/groundtruth.txt
```

Each mode writes a `*_results.json` file under `temp/screenai_tflite_probe/`.
The benchmark contains every predicted/reference line, timings and metrics.
The model package, scan pages and ground truth are existing local assets and
are not bundled with these scripts. A clean checkout needs those inputs.

Reproducibility identifiers:

- Recognition model SHA-256: `ee1cd2280a9d92c56998d855b2141f5e98b539400788f84ae98287df900375ac`
- Ground truth SHA-256: `e1c65b28de792444575d2fdc4c33ebdf993da46cc5d46e7f90ba992bf16365f5`
- Other model hashes and signatures are in `inspect_results.json`.

## Next Android experiment

Load only the recognition model and label map with Android LiteRT on CPU, using
these same line crops as parity fixtures. Match grayscale resizing, windowing,
CTC and Unicode handling before measuring device latency and memory. Only after
that should full-page text detection be connected or reimplemented. The current
result supports this recognition experiment; it does not establish a working
Android port of the complete ScreenAI engine.
