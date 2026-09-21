"""Validate independent detection against synthetic truth and external reference.

Reference matching happens only AFTER detection. The manifest is never supplied to
Detector or LineRecognizer. Native ScreenAI boxes are comparison data, not GT.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from screenai_tflite_bbox import Detector, crop_quad, save_overlay
from screenai_tflite_probe import DEFAULT_MODELS, LineRecognizer


def match_boxes(predicted, reference, threshold=0.5):
    if not predicted or not reference:
        return []
    p, r = np.asarray(predicted), np.asarray(reference)
    intersection = np.prod(
        np.maximum(
            0,
            np.minimum(p[:, None, 2:], r[None, :, 2:])
            - np.maximum(p[:, None, :2], r[None, :, :2]),
        ),
        axis=2,
    )
    pa = np.prod(np.maximum(0, p[:, 2:] - p[:, :2]), axis=1)
    ra = np.prod(np.maximum(0, r[:, 2:] - r[:, :2]), axis=1)
    iou = intersection / np.maximum(pa[:, None] + ra[None, :] - intersection, 1e-9)
    pairs = [
        (float(iou[a, b]), int(a), int(b)) for a, b in zip(*np.where(iou >= threshold))
    ]
    pairs.sort(reverse=True)
    used_p, used_r, matches = set(), set(), []
    for value, a, b in pairs:
        if a not in used_p and b not in used_r:
            used_p.add(a)
            used_r.add(b)
            matches.append([a, b, value])
    return matches


def synthetic_cases(folder, font_path):
    folder.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", (1280, 960), "white")
    draw = ImageDraw.Draw(im)
    labels = []
    lines = [
        (70, 80, 32, "Việt Nam - Tiếng Việt"),
        (70, 210, 48, "Độc lập - Tự do - Hạnh phúc"),
        (70, 370, 36, "123456789 - 112233"),
        (760, 370, 32, "Cột bên phải"),
        (70, 550, 30, "Kiểm tra khung chữ và nhận dạng trực tiếp bằng LiteRT."),
        (70, 730, 24, "Dòng chữ nhỏ, có dấu và ký tự lặp: aaaa 1111."),
    ]
    for x, y, size, text in lines:
        font = ImageFont.truetype(str(font_path), size)
        bbox = draw.textbbox((x, y), text, font=font)
        draw.text((x, y), text, font=font, fill="black")
        labels.append({"bbox": list(bbox), "text": text})
    cases = []
    for name, scale, angle in [
        ("upright", 1, 0),
        ("small", 0.6, 0),
        ("large", 1.4, 0),
        ("rotated", 1, 7),
        ("rotated_negative", 1, -12),
    ]:
        image = im.resize(
            (round(im.width * scale), round(im.height * scale)),
            Image.Resampling.LANCZOS,
        )
        if angle:
            image = image.rotate(angle, Image.Resampling.BICUBIC, fillcolor="white")
        radians = np.radians(angle)
        c, s = np.cos(radians), np.sin(radians)
        center = np.array(image.size) / 2
        truth = []
        for label in labels:
            x1, y1, x2, y2 = np.array(label["bbox"]) * scale
            q = np.array([[x1, y1], [x2, y1], [x2, y2], [x1, y2]])
            q = (q - center) @ np.array([[c, -s], [s, c]]) + center
            truth.append(
                {"text": label["text"], "bbox": [*q.min(axis=0), *q.max(axis=0)]}
            )
        path = folder / f"{name}.png"
        image.save(path)
        cases.append((path, truth))
    path = folder / "blank.png"
    Image.new("RGB", im.size, "white").save(path)
    cases.append((path, []))
    return cases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", type=Path, default=DEFAULT_MODELS)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument(
        "--output", type=Path, default=Path("temp/screenai_bbox/evaluation")
    )
    parser.add_argument("--font", type=Path, default=Path("C:/Windows/Fonts/arial.ttf"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    detector = Detector(args.models)
    recognizer = LineRecognizer(args.models)
    results = []
    cases = [
        ("synthetic", p, truth)
        for p, truth in synthetic_cases(args.output / "fixtures", args.font)
    ]
    if args.manifest:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        for page in manifest["pages"]:
            path = Path(page["image"])
            reference = [
                {"bbox": s["native_bbox"], "text": s["reference"]}
                for s in manifest["samples"]
                if s["page"] == path.name
            ]
            cases.append(("native_reference", path, reference))
    for kind, path, reference in cases:
        image = Image.open(path).convert("RGB")
        start = time.perf_counter()
        result = detector.detect(image)
        for box in result["boxes"]:
            box["text"] = recognizer.recognize_line(crop_quad(image, box["quad"]))[
                "text"
            ]
        result["full_ocr_seconds"] = time.perf_counter() - start
        matches = match_boxes(
            [b["bbox"] for b in result["boxes"]], [r["bbox"] for r in reference]
        )
        result.update(
            image=str(path.resolve()), kind=kind, reference=reference, matches=matches
        )
        save_overlay(image, result["boxes"], args.output / f"{path.stem}_overlay.png")
        (args.output / f"{path.stem}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        exact = sum(
            result["boxes"][a]["text"] == reference[b]["text"] for a, b, _ in matches
        )
        summary = {
            "image": path.name,
            "kind": kind,
            "predictions": len(result["boxes"]),
            "references": len(reference),
            "matched_iou_0_5": len(matches),
            "matched_iou_sum": sum(x[2] for x in matches),
            "exact_text_matched": exact,
            "full_ocr_seconds": result["full_ocr_seconds"],
        }
        results.append(summary)
        print(json.dumps(summary), flush=True)
    totals = {}
    for kind in {r["kind"] for r in results}:
        rows = [r for r in results if r["kind"] == kind]
        p, r, m = (
            sum(row[key] for row in rows)
            for key in ("predictions", "references", "matched_iou_0_5")
        )
        totals[kind] = {
            "predictions": p,
            "references": r,
            "matches": m,
            "precision": m / max(p, 1),
            "recall": m / max(r, 1),
            "mean_matched_iou": sum(row["matched_iou_sum"] for row in rows) / max(m, 1),
            "exact_text_matched": sum(row["exact_text_matched"] for row in rows),
            "full_ocr_seconds": sum(row["full_ocr_seconds"] for row in rows),
        }
    (args.output / "summary.json").write_text(
        json.dumps({"totals": totals, "pages": results}, indent=2), encoding="utf-8"
    )
    print(json.dumps(totals, indent=2))


if __name__ == "__main__":
    main()
