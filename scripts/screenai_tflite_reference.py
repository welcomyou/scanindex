"""Export ScreenAI line crops for a recognition-only comparison.

Unlike screenai_tflite_probe.py, THIS helper intentionally loads the ScreenAI
native library. Run it in a separate process. Its boxes are not independent
text detection, and its text is a reference prediction, not ground truth.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import time

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", type=Path, default=ROOT / "models/screen_ai/148.13")
    parser.add_argument("--pages", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--library", type=Path)
    args = parser.parse_args()
    pages = sorted(args.pages.glob("*.png"))
    if not pages:
        parser.error("No PNG pages found")
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "crops").mkdir(exist_ok=True)
    spec = importlib.util.spec_from_file_location("screenai_reference", ROOT / "scanindex/core/ocr/screen_ai.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    library = args.library or args.models / "chrome_screen_ai.dll"
    engine = module.ScreenAIOCR(str(library.resolve()), str(args.models.resolve()))
    engine.initialize()
    records = []
    page_results = []
    try:
        for page_path in pages:
            image = Image.open(page_path).convert("RGB")
            start = time.perf_counter()
            result = engine.perform_ocr(image)
            elapsed = time.perf_counter() - start
            texts = []
            for index, line in enumerate(result.get("lines", [])):
                box = line.get("bounding_box")
                text = line.get("utf8_string", "").strip()
                if not box or not text:
                    continue
                x, y, w, h = (box[k] for k in ("x", "y", "width", "height"))
                # Padding is relative to line height, to preserve diacritics.
                pad = max(2, round(h * 0.12))
                bounds = (max(0, int(x - pad)), max(0, int(y - pad)),
                          min(image.width, int(x + w + pad)), min(image.height, int(y + h + pad)))
                if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
                    continue
                crop_path = Path("crops") / f"{page_path.stem}_{index:03d}.png"
                image.crop(bounds).save(args.output / crop_path)
                records.append({"image": crop_path.as_posix(), "reference": text,
                                "page": page_path.name, "bbox": bounds,
                                "native_bbox": [x, y, x + w, y + h]})
                texts.append(text)
            page_results.append({"image": str(page_path.resolve()), "seconds": elapsed,
                                 "text": "\n".join(texts)})
            print(f"{page_path.name}: {len(texts)} lines, {elapsed:.3f}s", flush=True)
    finally:
        engine.shutdown()
    manifest = {"reference_engine": str(library.resolve()), "reference_is_ground_truth": False,
                "detection": "ScreenAI native library, external to standalone recognition",
                "pages": page_results, "samples": records}
    (args.output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
