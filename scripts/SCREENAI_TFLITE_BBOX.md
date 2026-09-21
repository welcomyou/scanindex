# Independent ScreenAI TFLite bounding boxes and OCR

The prototype now takes a page image, detects text, constructs line boxes,
rectifies tilted lines, and recognizes Vietnamese text using standalone LiteRT.
It does not load the ScreenAI DLL or consume precomputed reference boxes.

Implementation: `screenai_tflite_bbox.py`. Evaluation:
`screenai_tflite_bbox_eval.py`. It reuses `LineRecognizer` from the earlier
`screenai_tflite_probe.py`, so no new runtime dependency is required.

## Output

Each item in `boxes` has:

- `bbox`: `[x_min, y_min, x_max, y_max]`, clipped to the **original image** in
  pixel coordinates. This is a bounding box for a line/fragment, not each word.
- `quad`: four oriented corners in order top-left, top-right, bottom-right,
  bottom-left relative to the text direction. Corners may extend outside the
  image at an edge; rectification fills out-of-image pixels white.
- `angle_degrees`: estimated orientation in image coordinates.
- `score`: aggregated detection score, not calibrated OCR confidence.
- `text`: recognized text when `--recognize` is supplied.

The JSON also includes image size, detector model hash, LiteRT version, timing
and raw group proposals. The group proposals can cover whole paragraphs and
must not be mistaken for the final line boxes.

## Validation, 2026-09-20

Model package: ScreenAI 148.13. Windows x64, Python 3.12, LiteRT 2.2.0,
CPU/XNNPACK, two threads. No training or model-weight changes.

| Check | Result |
| --- | --- |
| Five generated pages: upright, scaled 0.6x / 1.4x, rotated +7 / -12 degrees | 30/30 line boxes matched at IoU >= 0.5; no extra boxes |
| Synthetic box mean matched IoU | 0.917 |
| Text on those synthetic boxes | 29/30 lines exact |
| Blank image | Zero boxes |
| Existing 15-page scan set | 460 predicted boxes, compared with 446 native ScreenAI boxes |
| One-to-one matched boxes at IoU >= 0.5 | 437 |
| Precision / recall relative to native ScreenAI | 95.00% / 97.98% |
| Mean IoU of the 437 matched boxes | 0.822 |
| Matched boxes with text identical to native ScreenAI | 380/437 |
| Detection + rectification + recognition, 15 pages | About 12.7 seconds on this desktop |

IoU is intersection area divided by union area of the axis-aligned boxes.
Matching greedily selects highest IoU pairs, one prediction and one reference
per match. The real-document numbers measure **agreement with native ScreenAI**,
not accuracy against manually labeled box ground truth. There are 23 unmatched
predictions and nine unmatched reference boxes. Extra fragments and different
line grouping both affect this comparison. The generated fixtures have known
geometry and known text.

The datasets were used during development, so these are development checks,
not an independent quality benchmark. Timing excludes image loading, model
initialization, reference generation, drawing and JSON serialization. No Android
device performance has been measured.

Detailed output is under `temp/screenai_bbox/evaluation/`: `summary.json`, one
JSON per page with boxes/text/reference/matches, and `*_overlay.png` previews.
The reference helper was extended to retain original native boxes separately
from padded recognition crops; padding is not included in IoU comparisons.

## How decoding works

The native package's detector config identifies logarithmic width/height
scaling, sigmoid confidence, anchor centers at 0.5, and six head scales of
16, 64, 16, 64, 64, 64. The embedded protobuf descriptor was inspected read-only
to identify configuration field names. That inspection is research evidence;
the standalone decoder neither reads nor loads the DLL at runtime.

Synthetic images with known positions and sizes established a working
interpretation of seven output channels:

```text
score = sigmoid(channel[0])
cx = (grid_x + 0.5 + channel[1]) * stride
cy = (grid_y + 0.5 + channel[2]) * stride
width  = head_scale * exp(channel[3])
height = head_scale * exp(channel[4])
angle = atan2(channel[6], channel[5])
```

Rotate the four rectangle corners around `(cx, cy)`, then undo the corresponding
input resize to return original-image coordinates. This implementation is an
experimentally validated interpretation, not a recovered copy of Google's
postprocessing code.

The network is fully convolutional enough to accept resized input tensors via
LiteRT. The prototype uses four aspect-preserving images with longest sides
2048, 1280, 640, 160, padded with white to multiples of 32. Input dimensions
originally stored in the model need not force a 4096-square inference.

| Output | Source image | Stride | Scale | Role |
| --- | --- | --- | --- | --- |
| Identity, Identity_1 | 2048 branch | 8, 32 | 16, 64 | Fragments |
| Identity_2, Identity_3 | 1280 branch | 8, 32 | 16, 64 | Fragments |
| Identity_4 | 640 branch | 32 | 64 | Fragments |
| Identity_5 | 160 branch | 32 | 64 | Fragments |
| Identity_6, Identity_7 | 2048 branch | 8, 32 | 16, 64 | Groups |
| Identity_8, Identity_9 | 1280 branch | 8, 32 | 16, 64 | Groups |
| Identity_10 | 640 branch | 32 | 64 | Groups |

Initial decoding of only the group heads produced paragraph-sized boxes. The
working pipeline instead suppresses duplicate fragments, joins locally aligned
fragments by height/angle/gap, then uses compatible same-line group proposals to
bridge spaces and punctuation. It rejects paragraph-height groups and groups
whose orientation conflicts with the fragments. Oriented containment removes
punctuation detections duplicated inside complete lines.

The final quadrilateral is rectified before the existing 32x168 windowed line
recognizer runs. Reading order is only a row-based heuristic.

## Run

From the repository root, using the earlier isolated environment:

```powershell
# Detect boxes, recognize text, and save JSON + image overlay.
.venv_screenai_probe/Scripts/python scripts/screenai_tflite_bbox.py temp/ocrv6_vs_screenai_benchmark/page_images/page_001_240dpi.png --recognize --output temp/screenai_bbox/demo

# Omit --recognize for detection only. Input can be another PNG/JPEG image.
# --threshold defaults to 0.5; --max-side defaults to 2048.

# Synthetic validation, including scale, rotation and blank controls.
.venv_screenai_probe/Scripts/python scripts/screenai_tflite_bbox_eval.py

# Optional comparison preparation: ONLY this helper loads native ScreenAI.
.venv_screenai_probe/Scripts/python scripts/screenai_tflite_reference.py --pages temp/ocrv6_vs_screenai_benchmark/page_images --output temp/screenai_bbox/reference

# Run independently, then compare against the saved reference manifest.
.venv_screenai_probe/Scripts/python scripts/screenai_tflite_bbox_eval.py --manifest temp/screenai_bbox/reference/manifest.json
```

Dependencies remain in `requirements-screenai-tflite.txt`. On non-Windows,
synthetic validation accepts `--font /path/to/a/font.ttf`. Local model files and
scan/reference datasets are not bundled with the scripts.

## Remaining limitations

- The fragment grouping is an approximation; dense tables, large gaps, columns,
  small punctuation and curved text can still split or merge incorrectly.
- IoU suppression uses axis-aligned envelopes, so intersecting rotated lines
  and complex layouts need further work.
- The full page reading order is not Google's layout model and does not rebuild
  table structure. Bounding-box agreement does not establish full-document CER.
- Recognition is greedy CTC without the native engine's language/FST handling;
  text can differ even when boxes match.
- Input resizing and grouping are validated on this model version and these
  development fixtures, not guaranteed for every ScreenAI version.
- This is a standalone desktop prototype; it has not been integrated into the
  production application or tested in an Android APK yet.
