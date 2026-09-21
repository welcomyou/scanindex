"""Experimental ScreenAI model runner using standalone LiteRT (no ScreenAI DLL).

This is a research probe, not the production OCR engine. See the accompanying
report for measured behavior and remaining limitations.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import itertools
import json
from pathlib import Path
import platform
import time
import unicodedata

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from ai_edge_litert.interpreter import Interpreter

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODELS = ROOT / "models/screen_ai/148.13"
RECOGNIZER = "gocr/gocr_models/line_recognition_mobile_convnext320_omni"
DETECTOR = "gocr/gocr_models/detection/gocr_group_rpn_text_detection_model_2024_q4.tflite"


def read_varint(data, pos):
    value = 0
    for shift in range(0, 70, 7):
        byte = data[pos]
        pos += 1
        value |= (byte & 127) << shift
        if byte < 128:
            return value, pos
    raise ValueError("Invalid protobuf varint")


def proto_fields(data):
    """Read wire fields without assuming an unavailable Google protobuf schema."""
    pos = 0
    while pos < len(data):
        tag, pos = read_varint(data, pos)
        field, wire = tag >> 3, tag & 7
        if wire == 0:
            value, pos = read_varint(data, pos)
        elif wire == 2:
            size, pos = read_varint(data, pos)
            value = data[pos:pos + size]
            pos += size
        elif wire in (1, 5):
            size = 8 if wire == 1 else 4
            value = data[pos:pos + size]
            pos += size
        else:
            raise ValueError(f"Unsupported wire type {wire}")
        if pos > len(data):
            raise ValueError("Truncated protobuf")
        yield field, wire, value


def load_labels(path):
    labels = {}
    for field, wire, value in proto_fields(path.read_bytes()):
        if field == 1 and wire == 2:
            entry = {f: v for f, _, v in proto_fields(value)}
            labels[int(entry.get(2, 0))] = entry.get(1, b"").decode("utf-8")
    if set(labels) != set(range(len(labels))):
        raise ValueError("Expected contiguous label IDs starting at zero")
    return labels


def details_json(details):
    return [{k: (d[k].tolist() if isinstance(d[k], np.ndarray) else
                 str(d[k]) if k == "dtype" else d[k])
             for k in ("name", "index", "shape", "shape_signature", "dtype", "quantization")}
            for d in details]


class LineRecognizer:
    def __init__(self, model_dir=DEFAULT_MODELS, threads=2):
        base = Path(model_dir) / RECOGNIZER
        self.labels = load_labels(base / "gocr_mobile_und_label_map.pb")
        self.interpreter = Interpreter(model_path=str(base / "gocr_mobile_und.tflite"), num_threads=threads)
        self.interpreter.allocate_tensors()
        self.input = self.interpreter.get_input_details()[0]
        self.output = next(d for d in self.interpreter.get_output_details()
                           if len(d["shape"]) == 3 and d["shape"][-1] == len(self.labels) + 1)
        self.blank = len(self.labels)

    def decode(self, ids):
        # CTC: collapse repeated tokens BEFORE removing the blank, otherwise
        # two equal characters separated by blank (e.g. 'll') would disappear.
        text = "".join(self.labels[i] for i, _ in itertools.groupby(ids) if i != self.blank)
        return unicodedata.normalize("NFC", text).strip()

    def infer(self, image):
        array = np.asarray(image.convert("L"), dtype=np.uint8)[None, :, :, None]
        if tuple(array.shape) != tuple(self.input["shape"]):
            raise ValueError(f"Expected {self.input['shape']}, got {array.shape}")
        start = time.perf_counter()
        self.interpreter.set_tensor(self.input["index"], array)
        self.interpreter.invoke()
        logits = self.interpreter.get_tensor(self.output["index"])[0]
        ids = np.argmax(logits, axis=-1).tolist()
        return {"text": self.decode(ids), "ids": ids,
                "invoke_ms": (time.perf_counter() - start) * 1000}

    def recognize_line(self, image):
        """Experimental overlapping windows; fixed height, aspect ratio preserved.

        Keep the central 104 pixels from each 168-pixel window (32px context
        on each side). Stitch time steps before CTC collapse, not decoded text.
        This is a local hypothesis, not Google's documented preprocessing.
        """
        start = time.perf_counter()
        image = image.convert("L")
        width = max(1, round(image.width * 32 / image.height))
        image = image.resize((width, 32), Image.Resampling.LANCZOS)
        padded = Image.new("L", (width + 200, 32), 255)
        padded.paste(image, (32, 0))
        ids = []
        invoke_ms = 0.0
        for x in range(0, width, 104):
            result = self.infer(padded.crop((x, 0, x + 168, 32)))
            count = min(26, (width - x + 3) // 4)
            ids.extend(result["ids"][8:8 + count])
            invoke_ms += result["invoke_ms"]
        return {"text": self.decode(ids),
                "windows": (width + 103) // 104, "invoke_ms": invoke_ms,
                "elapsed_ms": (time.perf_counter() - start) * 1000}


def normalized(text):
    return " ".join(unicodedata.normalize("NFC", text).split())


def metrics(prediction, reference):
    from rapidfuzz.distance import Levenshtein
    prediction, reference = normalized(prediction), normalized(reference)
    if not reference:
        raise ValueError("Cannot measure CER/WER against an empty reference")
    chars = Levenshtein.distance(prediction, reference)
    words = Levenshtein.distance(prediction.split(), reference.split())
    return {"cer": chars / len(reference), "wer": words / len(reference.split()),
            "character_edits": chars, "word_edits": words,
            "reference_characters": len(reference), "reference_words": len(reference.split())}


def run_demo(runner, output, fonts):
    texts = ["Hello", "Việt Nam", "Tiếng Việt", "Cộng hòa", "123456789",
             "Cộng hòa xã hội chủ nghĩa Việt Nam", "Độc lập - Tự do - Hạnh phúc",
             "Thành phố Hồ Chí Minh, ngày 20 tháng 9 năm 2026",
             "Kiểm tra nhận dạng tiếng Việt có dấu bằng mô hình độc lập.",
             "ĐƯỜNG TRƯỜNG SƠN - QUẬN BÌNH THẠNH",
             "Số 112233, giá 1.250.000 đồng; tỷ lệ 95,5%.",
             "aaaa bbbb 1111 0000 Hello", ""]
    results = []
    (output / "synthetic").mkdir(exist_ok=True)
    for font_path in fonts:
        font = ImageFont.truetype(str(font_path), 32)
        for text in texts:
            box = font.getbbox(text or " ")
            im = Image.new("L", (max(32, box[2] - box[0] + 12), max(32, box[3] - box[1] + 8)), 255)
            ImageDraw.Draw(im).text((6 - box[0], 4 - box[1]), text, font=font, fill=0)
            path = Path("synthetic") / f"sample_{len(results):03d}.png"
            im.save(output / path)
            results.append({"expected": text, "font": str(font_path), "image": str(path),
                            **runner.recognize_line(im)})
    return {"kind": "synthetic_known_text", "samples": results,
            "exact": sum(r["text"] == r["expected"] for r in results), "count": len(results),
            "metrics": metrics("\n".join(r["text"] for r in results), "\n".join(r["expected"] for r in results))}


def run_manifest(runner, manifest_path, ground_truth):
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    results = []
    for sample in data["samples"]:
        with Image.open(manifest_path.parent / sample["image"]) as image:
            results.append({**sample, **runner.recognize_line(image)})
    prediction = "\n".join(r["text"] for r in results)
    reference = "\n".join(r["reference"] for r in results)
    result = {"kind": "recognition_on_external_crops", "manifest": str(manifest_path.resolve()),
              "detection": data.get("detection"), "count": len(results),
              "exact_vs_reference_normalized": sum(normalized(r["text"]) == normalized(r["reference"]) for r in results),
              "difference_vs_reference_not_accuracy": metrics(prediction, reference),
              "recognition_seconds_excluding_image_loading": sum(r["elapsed_ms"] for r in results) / 1000,
              "reference_full_ocr_seconds": sum(p["seconds"] for p in data.get("pages", [])),
              "samples": results}
    if ground_truth:
        truth = ground_truth.read_text(encoding="utf-8")
        result["ground_truth_file"] = str(ground_truth.resolve())
        result["ground_truth_sha256"] = hashlib.sha256(ground_truth.read_bytes()).hexdigest()
        result["standalone_vs_ground_truth"] = metrics(prediction, truth)
        result["reference_vs_ground_truth"] = metrics(reference, truth)
    return result


def inspect_models(model_dir, threads):
    import tflite
    records = []
    for path in sorted(model_dir.rglob("*.tflite")):
        blob = path.read_bytes()
        model = tflite.Model.GetRootAsModel(blob, 0)
        custom = [model.OperatorCodes(i).CustomCode().decode("utf-8")
                  for i in range(model.OperatorCodesLength()) if model.OperatorCodes(i).CustomCode()]
        record = {"model": str(path.relative_to(model_dir)), "size_bytes": len(blob),
                  "sha256": hashlib.sha256(blob).hexdigest(), "custom_operators": custom}
        try:
            interpreter = Interpreter(model_path=str(path), num_threads=threads)
            interpreter.allocate_tensors()
            record.update(allocated=True, inputs=details_json(interpreter.get_input_details()),
                          outputs=details_json(interpreter.get_output_details()))
        except (ValueError, RuntimeError) as exc:
            record.update(allocated=False, error=str(exc))
        records.append(record)
    return {"kind": "allocation_only_not_inference", "models": records}


def detector_smoke(model_dir, image, output, threads):
    """Exercise the raw network only; resizing here is NOT a verified detector pipeline."""
    interpreter = Interpreter(model_path=str(model_dir / DETECTOR), num_threads=threads)
    interpreter.allocate_tensors()
    inputs = interpreter.get_input_details()
    outputs = interpreter.get_output_details()
    runs = []
    raw = []
    for source in (Image.new("L", image.size, 255), image.convert("L")):
        for d in inputs:
            _, h, w, _ = d["shape"]
            tensor = np.asarray(source.resize((int(w), int(h)), Image.Resampling.BILINEAR), dtype=np.uint8)
            interpreter.set_tensor(d["index"], tensor[None, :, :, None])
        start = time.perf_counter()
        interpreter.invoke()
        runs.append(time.perf_counter() - start)
        raw.append({d["name"]: interpreter.get_tensor(d["index"]) for d in outputs})
    np.savez_compressed(output / "detector_raw.npz", **raw[1])
    records = [{"name": name, "shape": list(array.shape), "finite": bool(np.isfinite(array).all()),
                "mean_abs_difference_from_blank": float(np.mean(np.abs(array - raw[0][name])))}
               for name, array in raw[1].items()]
    if not all(r["finite"] for r in records):
        raise RuntimeError("Detector produced nonfinite values")
    return {"kind": "raw_detector_inference_only", "preprocessing_verified": False,
            "bounding_box_decoder_implemented": False, "inputs": details_json(inputs),
            "blank_invoke_seconds": runs[0], "image_invoke_seconds": runs[1], "outputs": records}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["demo", "recognize", "benchmark", "inspect", "detector-smoke"], nargs="?", default="demo")
    parser.add_argument("--models", type=Path, default=DEFAULT_MODELS)
    parser.add_argument("--output", type=Path, default=ROOT / "temp/screenai_tflite_probe")
    parser.add_argument("--image", type=Path, help="Single line crop for recognize; page for detector-smoke")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--ground-truth", type=Path)
    parser.add_argument("--font", type=Path, action="append", help="Repeat to test several fonts")
    parser.add_argument("--threads", type=int, default=2)
    args = parser.parse_args()
    if args.threads < 1:
        parser.error("--threads must be positive")
    if args.mode in ("recognize", "detector-smoke") and not args.image:
        parser.error("--image is required for this mode")
    if args.mode == "benchmark" and not args.manifest:
        parser.error("--manifest is required for benchmark")
    args.output.mkdir(parents=True, exist_ok=True)
    if args.mode == "inspect":
        result = inspect_models(args.models, args.threads)
    elif args.mode == "detector-smoke":
        with Image.open(args.image) as image:
            result = detector_smoke(args.models, image, args.output, args.threads)
    else:
        runner = LineRecognizer(args.models, args.threads)
        if args.mode == "demo":
            fonts = args.font or [Path("C:/Windows/Fonts/arial.ttf"), Path("C:/Windows/Fonts/times.ttf"), Path("C:/Windows/Fonts/arialbd.ttf")]
            result = run_demo(runner, args.output, fonts)
        elif args.mode == "benchmark":
            result = run_manifest(runner, args.manifest, args.ground_truth)
        else:
            with Image.open(args.image) as image:
                result = {"image": str(args.image.resolve()), **runner.recognize_line(image)}
        result["recognizer_input"] = details_json([runner.input])[0]
        result["recognizer_logits"] = details_json([runner.output])[0]
        result["blank_id"] = runner.blank
        model_path = args.models / RECOGNIZER / "gocr_mobile_und.tflite"
        result["model_sha256"] = hashlib.sha256(model_path.read_bytes()).hexdigest()
    result["environment"] = {"platform": platform.platform(), "python": platform.python_version(),
                             "litert": importlib.metadata.version("ai-edge-litert"), "threads": args.threads}
    path = args.output / f"{args.mode}_results.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {k: v for k, v in result.items() if k not in ("samples", "models", "outputs", "inputs", "recognizer_input", "recognizer_logits")}
    print(json.dumps(summary, ensure_ascii=True, indent=2))
    print(f"Saved {path.resolve()}")


if __name__ == "__main__":
    main()
