"""Independent experimental ScreenAI GroupRPN detector and line OCR.

Produces original-image xyxy bboxes and clockwise quads (TL, TR, BR, BL).
Decode parameters target ScreenAI 148.13; grouping is a local approximation,
not Google's exact postprocessor. No ScreenAI DLL or reference boxes are used.
See SCREENAI_TFLITE_BBOX.md for validation and limitations.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import time

import numpy as np
from PIL import Image, ImageDraw
from ai_edge_litert.interpreter import Interpreter

from screenai_tflite_probe import DEFAULT_MODELS, DETECTOR, LineRecognizer


def decode_head(raw, stride, anchor, scale_x, scale_y, threshold=0.5):
    grid = raw[0]
    cutoff = math.log(threshold / (1 - threshold))
    ys, xs = np.where(grid[:, :, 0] >= cutoff)
    boxes = []
    for y, x in zip(ys, xs):
        v = grid[y, x]
        cx = (x + 0.5 + float(v[1])) * stride
        cy = (y + 0.5 + float(v[2])) * stride
        w = anchor * math.exp(float(np.clip(v[3], -8, 8)))
        h = anchor * math.exp(float(np.clip(v[4], -8, 8)))
        angle = math.atan2(float(v[6]), float(v[5]))
        c, s = math.cos(angle), math.sin(angle)
        quad = [
            [(cx + dx * c - dy * s) / scale_x, (cy + dx * s + dy * c) / scale_y]
            for dx, dy in [
                (-w / 2, -h / 2),
                (w / 2, -h / 2),
                (w / 2, h / 2),
                (-w / 2, h / 2),
            ]
        ]
        xy = np.array(quad)
        boxes.append(
            {
                "bbox": [
                    float(xy[:, 0].min()),
                    float(xy[:, 1].min()),
                    float(xy[:, 0].max()),
                    float(xy[:, 1].max()),
                ],
                "quad": quad,
                "score": 1 / (1 + math.exp(-float(v[0]))),
                "angle_degrees": math.degrees(angle),
            }
        )
    return boxes


def nms(boxes, threshold=0.4):
    if not boxes:
        return []
    xy = np.asarray([b["bbox"] for b in boxes])
    area = np.prod(np.maximum(0, xy[:, 2:] - xy[:, :2]), axis=1)
    order = np.argsort([b["score"] for b in boxes])[::-1]
    keep = []
    while order.size:
        index = int(order[0])
        keep.append(boxes[index])
        other = order[1:]
        inter = np.prod(
            np.maximum(
                0,
                np.minimum(xy[index, 2:], xy[other, 2:])
                - np.maximum(xy[index, :2], xy[other, :2]),
            ),
            axis=1,
        )
        iou = inter / np.maximum(area[index] + area[other] - inter, 1e-6)
        order = other[iou <= threshold]
    return keep


def group_pieces(pieces, gap_factor=0.8):
    """Join locally aligned text fragments; does not consume reference boxes."""
    if not pieces:
        return []
    points = np.asarray([p["quad"] for p in pieces])
    centers = points.mean(axis=1)
    directions = points[:, 1] - points[:, 0]
    widths = np.linalg.norm(directions, axis=1)
    directions /= np.maximum(widths[:, None], 1e-6)
    heights = np.linalg.norm(points[:, 3] - points[:, 0], axis=1)
    parents = list(range(len(pieces)))

    def find(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    for i in range(len(pieces)):
        delta = centers[i + 1 :] - centers[i]
        normal = np.array([-directions[i, 1], directions[i, 0]])
        along = np.abs(delta @ directions[i])
        across = np.abs(delta @ normal)
        min_h = np.minimum(heights[i], heights[i + 1 :])
        max_h = np.maximum(heights[i], heights[i + 1 :])
        gap = along - (widths[i] + widths[i + 1 :]) / 2
        cosine = directions[i + 1 :] @ directions[i]
        candidates = np.where(
            (max_h <= 1.75 * min_h)
            & (across <= 0.25 * max_h)
            & (gap <= gap_factor * min_h)
            & (cosine >= math.cos(math.radians(15)))
        )[0]
        for local in candidates:
            a, b = find(i), find(i + 1 + int(local))
            if a != b:
                parents[b] = a
    groups = {}
    for i in range(len(pieces)):
        groups.setdefault(find(i), []).append(i)
    lines = []
    for indices in groups.values():
        weights = np.asarray([pieces[i]["score"] for i in indices])
        direction = np.average(directions[indices], axis=0, weights=weights)
        direction /= max(np.linalg.norm(direction), 1e-6)
        normal = np.array([-direction[1], direction[0]])
        all_points = points[indices].reshape(-1, 2)
        u, v = all_points @ direction, all_points @ normal
        quad = np.asarray(
            [
                direction * x + normal * y
                for x, y in [
                    (u.min(), v.min()),
                    (u.max(), v.min()),
                    (u.max(), v.max()),
                    (u.min(), v.max()),
                ]
            ]
        )
        lines.append(
            {
                "bbox": [
                    float(quad[:, 0].min()),
                    float(quad[:, 1].min()),
                    float(quad[:, 0].max()),
                    float(quad[:, 1].max()),
                ],
                "quad": quad.tolist(),
                "score": float(weights.mean()),
                "pieces": sum(pieces[i].get("pieces", 1) for i in indices),
                "angle_degrees": math.degrees(math.atan2(direction[1], direction[0])),
            }
        )
    return nms(lines, 0.4)


def reading_order(boxes):
    """Simple row order, not a document layout/column reading-order model."""
    rows = []
    for box in sorted(boxes, key=lambda b: (b["bbox"][1] + b["bbox"][3]) / 2):
        x1, y1, x2, y2 = box["bbox"]
        cy, h = (y1 + y2) / 2, y2 - y1
        if rows and abs(cy - rows[-1][0]) < 0.4 * min(h, rows[-1][1]):
            rows[-1][2].append(box)
        else:
            rows.append([cy, h, [box]])
    return [b for _, _, row in rows for b in sorted(row, key=lambda b: b["bbox"][0])]


def refine_with_groups(lines, groups):
    """Use same-line group predictions to bridge punctuation/space breaks.

    Reject paragraph-sized groups. Group heads are hints, not ground truth.
    """
    for group in sorted(
        groups,
        key=lambda g: (g["bbox"][2] - g["bbox"][0]) * (g["bbox"][3] - g["bbox"][1]),
    ):
        q = np.asarray(group["quad"])
        u = q[1] - q[0]
        v = q[3] - q[0]
        w, h = np.linalg.norm(u), np.linalg.norm(v)
        u = u / max(w, 1e-6)
        v = v / max(h, 1e-6)
        indices = []
        heights = []
        centers = []
        for i, line in enumerate(lines):
            points = np.asarray(line["quad"]) - q[0]
            direction = points[1] - points[0]
            direction /= max(np.linalg.norm(direction), 1e-6)
            if abs(float(direction @ u)) < math.cos(math.radians(15)):
                continue
            xs, ys = points @ u, points @ v
            lw, lh = np.ptp(xs), np.ptp(ys)
            overlap = max(0, min(w, xs.max()) - max(0, xs.min()))
            if (
                overlap >= 0.7 * lw
                and 0 <= xs.mean() <= w
                and -0.1 * h <= ys.mean() <= 1.1 * h
                and lh <= 1.5 * h
            ):
                indices.append(i)
                heights.append(lh)
                centers.append(ys.mean())
        if len(indices) < 2:
            continue
        typical = max(heights)
        if h > 1.6 * typical or h < 0.6 * typical or np.ptp(centers) > 0.45 * typical:
            continue
        points = np.concatenate([np.asarray(lines[i]["quad"]) for i in indices])
        xs, ys = points @ u, points @ v
        merged = np.asarray(
            [
                x * u + y * v
                for x, y in [
                    (xs.min(), ys.min()),
                    (xs.max(), ys.min()),
                    (xs.max(), ys.max()),
                    (xs.min(), ys.max()),
                ]
            ]
        )
        line = {
            "bbox": [
                float(merged[:, 0].min()),
                float(merged[:, 1].min()),
                float(merged[:, 0].max()),
                float(merged[:, 1].max()),
            ],
            "quad": merged.tolist(),
            "score": max(lines[i]["score"] for i in indices),
            "angle_degrees": math.degrees(math.atan2(u[1], u[0])),
            "pieces": sum(lines[i]["pieces"] for i in indices),
        }
        lines = [x for i, x in enumerate(lines) if i not in indices] + [line]
    # A punctuation fragment can survive NMS inside a complete line: suppress
    # it by oriented containment, not IoU (which is tiny for such a fragment).
    keep = []
    for line in sorted(
        lines,
        key=lambda b: (b["bbox"][2] - b["bbox"][0]) * (b["bbox"][3] - b["bbox"][1]),
        reverse=True,
    ):
        contained = False
        for parent in keep:
            q = np.asarray(parent["quad"])
            u = q[1] - q[0]
            v = q[3] - q[0]
            w, h = np.linalg.norm(u), np.linalg.norm(v)
            local = np.asarray(line["quad"]) - q[0]
            xs, ys = local @ (u / max(w, 1e-6)), local @ (v / max(h, 1e-6))
            if (
                xs.min() >= -0.03 * h
                and xs.max() <= w + 0.03 * h
                and ys.min() >= -0.03 * h
                and ys.max() <= h + 0.03 * h
            ):
                contained = True
                break
        if not contained:
            keep.append(line)
    return keep


def crop_quad(image, quad, padding=0.12):
    """Rectify an oriented line with local padding; Pillow fills outside white."""
    q = np.asarray(quad, dtype=np.float64)
    u, v = q[1] - q[0], q[3] - q[0]
    w, h = np.linalg.norm(u), np.linalg.norm(v)
    if w < 1 or h < 1:
        raise ValueError("Degenerate line quadrilateral")
    u, v = u / w, v / h
    pad = max(2, h * padding)
    q = q + np.asarray([-u - v, u - v, u + v, -u + v]) * pad
    # Pillow QUAD order: upper-left, lower-left, lower-right, upper-right.
    return image.transform(
        (max(1, round(w + 2 * pad)), max(1, round(h + 2 * pad))),
        Image.Transform.QUAD,
        tuple(q[[0, 3, 2, 1]].ravel()),
        Image.Resampling.BICUBIC,
        fillcolor="white",
    )


def save_overlay(image, boxes, path):
    overlay = image.convert("RGB").copy()
    draw = ImageDraw.Draw(overlay)
    for index, box in enumerate(boxes):
        draw.polygon(
            [tuple(p) for p in box["quad"]],
            outline="red",
            width=max(2, round(image.width / 800)),
        )
        draw.text((box["bbox"][0], box["bbox"][1] - 12), str(index), fill="blue")
    overlay.thumbnail((1600, 1600))
    overlay.save(path)


class Detector:
    def __init__(self, model_dir=DEFAULT_MODELS, threads=2, max_side=2048):
        if max_side < 160 or max_side > 4096:
            raise ValueError("max_side must be between 160 and 4096")
        self.interpreter = Interpreter(
            model_path=str(Path(model_dir) / DETECTOR), num_threads=threads
        )
        self.model_sha256 = hashlib.sha256(
            (Path(model_dir) / DETECTOR).read_bytes()
        ).hexdigest()
        self.max_side = max_side
        self.inputs = self.interpreter.get_input_details()

    def detect(self, image, threshold=0.5):
        if not 0 < threshold < 1:
            raise ValueError("threshold must be between zero and one")
        start = time.perf_counter()
        image = image.convert("L")
        scales = []
        tensors = []
        for d, limit in zip(self.inputs, [self.max_side, 1280, 640, 160]):
            scale = limit / max(image.size)
            content_w, content_h = max(1, round(image.width * scale)), max(
                1, round(image.height * scale)
            )
            width, height = (
                math.ceil(content_w / 32) * 32,
                math.ceil(content_h / 32) * 32,
            )
            source = Image.new("L", (width, height), 255)
            source.paste(
                image.resize((content_w, content_h), Image.Resampling.BILINEAR), (0, 0)
            )
            tensor = np.asarray(source, dtype=np.uint8)[None, :, :, None]
            self.interpreter.resize_tensor_input(d["index"], tensor.shape, strict=False)
            tensors.append(tensor)
            scales.append((content_w / image.width, content_h / image.height))
        self.interpreter.allocate_tensors()
        for d, tensor in zip(self.inputs, tensors):
            self.interpreter.set_tensor(d["index"], tensor)
        invoke_start = time.perf_counter()
        self.interpreter.invoke()
        invoke_ms = (time.perf_counter() - invoke_start) * 1000
        raw = {
            d["name"]: self.interpreter.get_tensor(d["index"])
            for d in self.interpreter.get_output_details()
        }
        if not all(np.isfinite(a).all() for a in raw.values()):
            raise RuntimeError("Detector returned nonfinite tensors")
        boxes = []
        # Group heads predict complete text groups; first six heads predict pieces.
        for index, source, stride, anchor in [
            (6, 0, 8, 16),
            (7, 0, 32, 64),
            (8, 1, 8, 16),
            (9, 1, 32, 64),
            (10, 2, 32, 64),
        ]:
            decoded = decode_head(
                raw[f"Identity_{index}"], stride, anchor, *scales[source], threshold
            )
            for box in decoded:
                box["head"] = index
            boxes.extend(decoded)
        groups = nms(boxes)
        pieces = []
        for index, source, stride, anchor in [
            (0, 0, 8, 16),
            (1, 0, 32, 64),
            (2, 1, 8, 16),
            (3, 1, 32, 64),
            (4, 2, 32, 64),
            (5, 3, 32, 64),
        ]:
            name = "Identity" if index == 0 else f"Identity_{index}"
            decoded = decode_head(raw[name], stride, anchor, *scales[source], threshold)
            for piece in decoded:
                piece["head"] = index
            pieces.extend(decoded)
        pieces = nms(pieces, 0.6)
        boxes = group_pieces(group_pieces(pieces), gap_factor=0.45)
        boxes = reading_order(refine_with_groups(boxes, groups))
        # Preserve quadrilaterals, but make exposed axis-aligned boxes valid image coordinates.
        for box in boxes:
            x1, y1, x2, y2 = box["bbox"]
            box["bbox"] = [
                max(0, min(image.width, x1)),
                max(0, min(image.height, y1)),
                max(0, min(image.width, x2)),
                max(0, min(image.height, y2)),
            ]
        boxes = [
            b
            for b in boxes
            if b["bbox"][2] > b["bbox"][0] and b["bbox"][3] > b["bbox"][1]
        ]
        return {
            "boxes": boxes,
            "groups": groups,
            "piece_count": len(pieces),
            "invoke_ms": invoke_ms,
            "image_size": list(image.size),
            "bbox_format": "xyxy pixels in original image",
            "independent_detection": True,
            "decoder": "experimental GroupRPN pieces + geometric line grouping",
            "model_sha256": self.model_sha256,
            "litert_version": importlib.metadata.version("ai-edge-litert"),
            "elapsed_ms": (time.perf_counter() - start) * 1000,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("--models", type=Path, default=DEFAULT_MODELS)
    parser.add_argument("--output", type=Path, default=Path("temp/screenai_bbox"))
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument("--max-side", type=int, default=2048)
    parser.add_argument("--recognize", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    image = Image.open(args.image).convert("RGB")
    detector = Detector(args.models, max_side=args.max_side)
    result = detector.detect(image, args.threshold)
    recognizer = LineRecognizer(args.models) if args.recognize else None
    for index, box in enumerate(result["boxes"]):
        if recognizer:
            crop = crop_quad(image, box["quad"])
            box["text"] = recognizer.recognize_line(crop)["text"]
    save_overlay(image, result["boxes"], args.output / f"{args.image.stem}_boxes.png")
    (args.output / f"{args.image.stem}_boxes.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "count": len(result["boxes"]),
                "invoke_ms": result["invoke_ms"],
                "boxes": result["boxes"][:3],
            },
            ensure_ascii=True,
        )
    )


if __name__ == "__main__":
    main()
