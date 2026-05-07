from __future__ import annotations

import argparse
import csv
import json
import os
import pickle
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import fitz
import numpy as np
import torch
from docx import Document
from lxml import html
from omegaconf import OmegaConf
from PIL import Image
from rapidfuzz.distance import Levenshtein
from torch.nn.utils.rnn import pad_sequence
from torch.utils.data import DataLoader
from transformers import AutoTokenizer


@dataclass
class TablePred:
    scan: str
    name: str
    rows: int
    cols: int
    text: str
    pred_html: str
    elapsed_sec: float


def clean_text(text: str) -> str:
    import re

    return re.sub(r"\s+", " ", text or "").strip()


def docx_tables(path: Path) -> list[tuple[int, int, str]]:
    doc = Document(str(path))
    out = []
    for table in doc.tables:
        text = clean_text(" ".join(cell.text for row in table.rows for cell in row.cells))
        out.append((len(table.rows), len(table.columns), text))
    return out


def data_tables(tables: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    return [t for t in tables if t[:2] != (1, 2)]


def shape_score(gt: tuple[int, int], out: tuple[int, int]) -> float:
    gr, gc = gt
    or_, oc = out
    if gr <= 0 or gc <= 0 or or_ <= 0 or oc <= 0:
        return 0.0
    return (min(gr, or_) / max(gr, or_)) * (min(gc, oc) / max(gc, oc))


def text_acc(gt: str, out: str) -> float:
    gt = clean_text(gt)
    out = clean_text(out)
    if not gt and not out:
        return 1.0
    if not gt:
        return 0.0
    return max(0.0, 1.0 - Levenshtein.distance(gt, out) / len(gt))


def compare_tables(gt_tables: list[tuple[int, int, str]], out_tables: list[tuple[int, int, str]]) -> dict[str, Any]:
    n = max(len(gt_tables), len(out_tables))
    details = []
    shape_scores = []
    text_scores = []
    for idx in range(n):
        gt = gt_tables[idx] if idx < len(gt_tables) else (0, 0, "")
        out = out_tables[idx] if idx < len(out_tables) else (0, 0, "")
        ss = shape_score(gt[:2], out[:2])
        ta = text_acc(gt[2], out[2])
        shape_scores.append(ss)
        text_scores.append(ta)
        details.append({
            "index": idx,
            "gt_shape": list(gt[:2]),
            "out_shape": list(out[:2]),
            "shape_score": ss,
            "text_acc": ta,
        })
    return {
        "count": len(out_tables),
        "gt_count": len(gt_tables),
        "shape_acc": sum(shape_scores) / n if n else 1.0,
        "text_acc": sum(text_scores) / n if n else 1.0,
        "details": details,
    }


def load_layout_table_bboxes(json_path: Path) -> list[tuple[int, tuple[float, float, float, float]]]:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    out = []
    for page_idx, page in enumerate(data.get("pages", []), 1):
        for region in page.get("layout_regions", []):
            if region.get("type") != "table":
                continue
            bbox = region.get("bbox_pdf")
            if bbox and len(bbox) >= 4:
                out.append((page_idx, tuple(float(v) for v in bbox[:4])))
    out.sort(key=lambda item: (item[0], item[1][1]))
    return out


def iter_ocr_lines(json_path: Path) -> dict[int, list[dict[str, Any]]]:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    return {page_idx: page.get("lines", []) for page_idx, page in enumerate(data.get("pages", []), 1)}


def line_xywh(line: dict[str, Any]) -> tuple[float, float, float, float]:
    if all(k in line for k in ("x", "y", "w", "h")):
        return (float(line["x"]), float(line["y"]), float(line["w"]), float(line["h"]))
    bbox = line.get("bbox") or [0, 0, 0, 0]
    x0, y0, x1, y1 = (float(v) for v in bbox[:4])
    return (x0, y0, x1 - x0, y1 - y0)


def crop_table(page: fitz.Page,
               page_lines: list[dict[str, Any]],
               bbox: tuple[float, float, float, float],
               out_path: Path,
               dpi: int,
               pad_pt: float) -> list[dict[str, Any]]:
    scale = dpi / 72.0
    rect = fitz.Rect(
        max(0.0, bbox[0] - pad_pt),
        max(0.0, bbox[1] - pad_pt),
        min(page.rect.width, bbox[2] + pad_pt),
        min(page.rect.height, bbox[3] + pad_pt),
    )
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=rect, alpha=False)
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    img.save(out_path)

    rec = []
    for line in page_lines:
        text = clean_text(line.get("text") or line.get("ocr_text") or "")
        if not text:
            continue
        x, y, w, h = line_xywh(line)
        cx, cy = x + w / 2.0, y + h / 2.0
        if not (rect.x0 <= cx <= rect.x1 and rect.y0 <= cy <= rect.y1):
            continue
        x0 = (max(x, rect.x0) - rect.x0) * scale
        y0 = (max(y, rect.y0) - rect.y0) * scale
        x1 = (min(x + w, rect.x1) - rect.x0) * scale
        y1 = (min(y + h, rect.y1) - rect.y0) * scale
        if x1 <= x0 or y1 <= y0:
            continue
        rec.append({"bbox": [float(x0), float(y0), float(x1), float(y1)], "text": text})
    return rec


def prepare_aux_files(args: argparse.Namespace) -> tuple[Path, Path, Path, dict[str, str]]:
    img_dir = args.out_dir / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    aux_json: dict[str, dict[str, str]] = {}
    aux_rec: dict[str, list[dict[str, Any]]] = {}
    scan_by_name: dict[str, str] = {}
    dummy_html = "<tbody><tr><td></td></tr></tbody>"

    total = 0
    for scan in args.scans:
        ocr_pdf = args.ocr_dir / f"scan{scan}_ocr.pdf"
        bboxes = load_layout_table_bboxes(Path(str(ocr_pdf) + ".json"))
        lines_by_page = iter_ocr_lines(Path(str(ocr_pdf) + ".json"))
        doc = fitz.open(str(ocr_pdf))
        try:
            for idx, (page_num, bbox) in enumerate(bboxes):
                if args.max_tables and total >= args.max_tables:
                    break
                name = f"scan{scan}_table{idx:02d}_p{page_num}.png"
                rec = crop_table(
                    doc[page_num - 1],
                    lines_by_page.get(page_num, []),
                    bbox,
                    img_dir / name,
                    args.dpi,
                    args.pad_pt,
                )
                aux_json[name] = {"html": dummy_html, "type": "simple"}
                aux_rec[name] = rec
                scan_by_name[name] = scan
                total += 1
        finally:
            doc.close()
        if args.max_tables and total >= args.max_tables:
            break

    aux_json_path = args.out_dir / "tflop_aux.json"
    aux_rec_path = args.out_dir / "tflop_rec.pkl"
    aux_json_path.write_text(json.dumps(aux_json, ensure_ascii=False, indent=2), encoding="utf-8")
    with aux_rec_path.open("wb") as f:
        pickle.dump(aux_rec, f)
    (args.out_dir / "scan_by_name.json").write_text(json.dumps(scan_by_name, indent=2), encoding="utf-8")
    return aux_json_path, img_dir, aux_rec_path, scan_by_name


def html_table_to_shape_text(pred_html: str) -> tuple[int, int, str]:
    try:
        root = html.fromstring(pred_html)
    except Exception:
        return (0, 0, clean_text(pred_html))

    table = root.find(".//table")
    if table is None:
        table = root

    grid: list[list[str]] = []
    rowspans: dict[int, list[tuple[int, str]]] = {}
    for row_idx, tr in enumerate(table.findall(".//tr")):
        row: list[str] = []
        col = 0
        for td in tr.findall("./td") + tr.findall("./th"):
            while rowspans.get(col):
                remaining, text = rowspans[col].pop(0)
                row.append(text)
                if remaining > 1:
                    rowspans.setdefault(col, []).append((remaining - 1, text))
                col += 1
            text = clean_text(" ".join(td.itertext()))
            rowspan = int(td.get("rowspan") or 1)
            colspan = int(td.get("colspan") or 1)
            for _ in range(max(1, colspan)):
                row.append(text)
                if rowspan > 1:
                    rowspans.setdefault(col, []).append((rowspan - 1, text))
                col += 1
        while rowspans.get(col):
            remaining, text = rowspans[col].pop(0)
            row.append(text)
            if remaining > 1:
                rowspans.setdefault(col, []).append((remaining - 1, text))
            col += 1
        if row:
            grid.append(row)

    rows = len(grid)
    cols = max((len(row) for row in grid), default=0)
    text = clean_text(" ".join(cell for row in grid for cell in row))
    return (rows, cols, text)


def load_tflop(repo_dir: Path, model_dir: Path, device: str, dtype: str):
    sys.path.insert(0, str(repo_dir))
    from tflop.datamodule.datasets.tflop import TFLOPTestDataset
    from tflop.model.model.TFLOP import TFLOP
    from tflop.model.model.TFLOP_Config import TFLOPConfig
    from tflop.utils import custom_format_html, decode_OTSL_seq, resolve_missing_config

    tokenizer = AutoTokenizer.from_pretrained(str(model_dir))
    cfg_data = json.loads((model_dir / "config.json").read_text(encoding="utf-8"))
    input_size = cfg_data.get("input_size", [768, 768])
    exp_cfg_dict = dict(cfg_data)
    exp_cfg_dict["input_size"] = {"width": int(input_size[0]), "height": int(input_size[1])}
    exp_cfg_dict["use_OTSL"] = True
    exp_config = OmegaConf.create(exp_cfg_dict)

    model_cfg_dict = {
        k: v for k, v in cfg_data.items()
        if k in TFLOPConfig.get_member_variables()
    }
    model_cfg_dict = resolve_missing_config(model_cfg_dict)
    model = TFLOP(
        config=TFLOPConfig(**model_cfg_dict),
        tokenizer=tokenizer,
        data_ids=["C-tag"],
    )

    state = torch.load(model_dir / "pytorch_model.bin", map_location="cpu")
    encoder_state = {k[len("encoder."):]: v for k, v in state.items() if k.startswith("encoder.")}
    decoder_state = {k[len("decoder."):]: v for k, v in state.items() if k.startswith("decoder.")}
    model.encoder.load_state_dict(encoder_state)
    model.decoder.load_state_dict(decoder_state)

    if dtype == "auto":
        dtype = "float16" if device == "cuda" else "float32"
    if dtype == "float16":
        model.half()
    elif dtype == "bfloat16":
        model.bfloat16()
    elif dtype == "float32":
        model.float()
    else:
        raise ValueError(f"Unsupported dtype: {dtype}")

    model.to(device)
    model.eval()
    return model, tokenizer, exp_config, TFLOPTestDataset, custom_format_html, decode_OTSL_seq, dtype


def run_inference(args: argparse.Namespace,
                  aux_json: Path,
                  img_dir: Path,
                  aux_rec: Path,
                  scan_by_name: dict[str, str]) -> list[TablePred]:
    if args.threads:
        torch.set_num_threads(args.threads)

    device = args.device
    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    model, tokenizer, exp_config, dataset_cls, custom_format_html, decode_OTSL_seq, dtype = load_tflop(
        args.repo_dir, args.model_dir, device, args.dtype
    )

    dataset = dataset_cls(
        tokenizer=tokenizer,
        split="test",
        config=exp_config,
        aux_json_path=str(aux_json),
        aux_img_path=str(img_dir),
        aux_rec_pkl_path=str(aux_rec),
    )
    dataloader = DataLoader(dataset, batch_size=args.batch_size, shuffle=False, num_workers=0)
    preds: list[TablePred] = []

    with torch.inference_mode():
        for batch in dataloader:
            image_tensors = batch[0]
            decoder_input_ids = batch[1]
            coord_input_idx = batch[2]
            coord_input_length = batch[3]
            prompt_end_idxs = batch[4]
            cell_texts = batch[6]
            file_names = batch[7]

            decoder_prompts = pad_sequence(
                [input_id[: end_idx + 1] for input_id, end_idx in zip(decoder_input_ids, prompt_end_idxs)],
                batch_first=True,
            )
            if dtype == "float16":
                image_tensors = image_tensors.half()
            elif dtype == "bfloat16":
                image_tensors = image_tensors.bfloat16()
            else:
                image_tensors = image_tensors.float()

            image_tensors = image_tensors.to(device)
            decoder_prompts = decoder_prompts.to(device)
            pointer_args = {
                "coord_input_idx": coord_input_idx.to(device),
                "coord_input_length": coord_input_length.to(device),
            }

            if device == "cuda":
                torch.cuda.synchronize()
            t0 = time.perf_counter()
            output = model.inference(
                image_tensors=image_tensors,
                prompt_tensors=decoder_prompts,
                return_json=False,
                return_attentions=False,
                pointer_args=pointer_args,
            )
            if device == "cuda":
                torch.cuda.synchronize()
            elapsed = time.perf_counter() - t0

            batch_size = output["text_to_dr_coord"].shape[0]
            for i in range(batch_size):
                token_ids = output["output_sequences"][i]
                token_seq = tokenizer.convert_ids_to_tokens(token_ids)
                cell_text_data = cell_texts[i].split("<special_cell_text_sep>")
                decoded = decode_OTSL_seq(
                    otsl_token_seq=token_seq,
                    pointer_tensor=output["text_to_dr_coord"][i],
                    cell_text_data=cell_text_data,
                )
                _raw, pred_html = custom_format_html(decoded, tokenizer)
                rows, cols, text = html_table_to_shape_text(pred_html)
                name = file_names[i]
                preds.append(TablePred(
                    scan=scan_by_name.get(name, ""),
                    name=name,
                    rows=rows,
                    cols=cols,
                    text=text,
                    pred_html=pred_html,
                    elapsed_sec=elapsed / max(1, batch_size),
                ))
                print(f"{name}: {rows}x{cols}, {elapsed / max(1, batch_size):.2f}s", flush=True)

    return preds


def write_results(args: argparse.Namespace, preds: list[TablePred]) -> dict[str, Any]:
    by_scan: dict[str, list[TablePred]] = {}
    for pred in preds:
        by_scan.setdefault(pred.scan, []).append(pred)

    comparison = []
    for scan in args.scans:
        gt_docx = args.gt03_docx if scan == "03" else args.groundtruth_dir / f"groundtruth{scan}.docx"
        gt_data = data_tables(docx_tables(gt_docx))
        cur_data = data_tables(docx_tables(args.current_dir / f"scan{scan}_final.docx"))
        out_data = [(p.rows, p.cols, p.text) for p in by_scan.get(scan, [])]
        comparison.append({
            "scan": scan,
            "tflop": compare_tables(gt_data, out_data),
            "current": compare_tables(gt_data, cur_data),
            "tflop_shapes": [[p.rows, p.cols] for p in by_scan.get(scan, [])],
            "current_shapes": [list(t[:2]) for t in cur_data],
            "gt_shapes": [list(t[:2]) for t in gt_data],
            "tflop_time_sec": sum(p.elapsed_sec for p in by_scan.get(scan, [])),
            "tflop_avg_table_sec": (
                sum(p.elapsed_sec for p in by_scan.get(scan, [])) / len(by_scan.get(scan, []))
                if by_scan.get(scan, []) else 0.0
            ),
        })

    with (args.out_dir / "tflop_tables.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["scan", "name", "rows", "cols", "elapsed_sec", "text", "pred_html"])
        for pred in preds:
            writer.writerow([pred.scan, pred.name, pred.rows, pred.cols, f"{pred.elapsed_sec:.6f}", pred.text, pred.pred_html])

    result = {
        "model": "upstage/TFLOP",
        "comparison": comparison,
        "total_time_sec": sum(p.elapsed_sec for p in preds),
        "avg_table_sec": sum(p.elapsed_sec for p in preds) / len(preds) if preds else 0.0,
    }
    (args.out_dir / "comparison.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    with (args.out_dir / "summary.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "scan",
            "tflop_table_count",
            "gt_data_count",
            "tflop_shape_acc",
            "cur_shape_acc",
            "tflop_text_acc",
            "cur_text_acc",
            "tflop_time_sec",
            "tflop_avg_table_sec",
        ])
        for item in comparison:
            writer.writerow([
                item["scan"],
                item["tflop"]["count"],
                item["tflop"]["gt_count"],
                item["tflop"]["shape_acc"],
                item["current"]["shape_acc"],
                item["tflop"]["text_acc"],
                item["current"]["text_acc"],
                item["tflop_time_sec"],
                item["tflop_avg_table_sec"],
            ])
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-dir", type=Path, default=Path(r"D:\App\ocrtool\temp\external\TFLOP"))
    parser.add_argument("--model-dir", type=Path, default=Path(r"D:\App\ocrtool\temp\external\TFLOP_hf_model"))
    parser.add_argument("--ocr-dir", type=Path, default=Path(r"D:\App\ocrtool\temp\groundtruth4_pipeline"))
    parser.add_argument("--current-dir", type=Path, default=Path(r"D:\App\ocrtool\temp\groundtruth4_pipeline"))
    parser.add_argument("--groundtruth-dir", type=Path, default=Path(r"C:\Users\nhquan\Downloads\groundtruth"))
    parser.add_argument("--gt03-docx", type=Path, default=Path(r"D:\App\ocrtool\temp\groundtruth4_scan_word\groundtruth03_converted.docx"))
    parser.add_argument("--out-dir", type=Path, default=Path(r"D:\App\ocrtool\temp\tflop_bench"))
    parser.add_argument("--scans", nargs="+", default=["01", "02", "03", "04"])
    parser.add_argument("--dpi", type=int, default=200)
    parser.add_argument("--pad-pt", type=float, default=2.0)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--device", choices=["auto", "cpu", "cuda"], default="auto")
    parser.add_argument("--dtype", choices=["auto", "float32", "float16", "bfloat16"], default="auto")
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--max-tables", type=int, default=0)
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    aux_json, img_dir, aux_rec, scan_by_name = prepare_aux_files(args)
    preds = run_inference(args, aux_json, img_dir, aux_rec, scan_by_name)
    write_results(args, preds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
