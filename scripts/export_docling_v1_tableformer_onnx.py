from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import onnxruntime as ort
import torch
from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
from docling.datamodel.pipeline_options import TableFormerMode, TableStructureOptions
from docling.models.stages.table_structure.table_structure_model import TableStructureModel


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))


class EncoderBundle(torch.nn.Module):
    def __init__(self, table_model: torch.nn.Module):
        super().__init__()
        self.encoder = table_model._encoder
        self.tag_transformer = table_model._tag_transformer

    def forward(self, images: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        enc_out = self.encoder(images)
        encoder_out = self.tag_transformer._input_filter(
            enc_out.permute(0, 3, 1, 2)
        ).permute(0, 2, 3, 1)
        batch_size = encoder_out.size(0)
        encoder_dim = encoder_out.size(-1)
        enc_inputs = encoder_out.view(batch_size, -1, encoder_dim).permute(1, 0, 2)
        memory = self.tag_transformer._encoder(enc_inputs)
        return enc_out, memory


class IncrementalDecoderFirst(torch.nn.Module):
    def __init__(self, table_model: torch.nn.Module):
        super().__init__()
        self.tag_transformer = table_model._tag_transformer

    def forward(
        self,
        current_tag: torch.Tensor,
        position_pe: torch.Tensor,
        memory: torch.Tensor,
    ) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
        raw = self.tag_transformer._embedding(current_tag) + position_pe
        x = raw
        layer_values = []
        for layer in self.tag_transformer._decoder.layers:
            kv = raw if not layer_values else layer_values[-1]
            y = layer.self_attn(
                x,
                kv,
                kv,
                attn_mask=None,
                key_padding_mask=None,
                need_weights=False,
            )[0]
            x = layer.norm1(x + layer.dropout1(y))
            y = layer.multihead_attn(
                x,
                memory,
                memory,
                attn_mask=None,
                key_padding_mask=None,
                need_weights=False,
            )[0]
            x = layer.norm2(x + layer.dropout2(y))
            y = layer.linear2(layer.dropout(layer.activation(layer.linear1(x))))
            x = layer.norm3(x + layer.dropout3(y))
            layer_values.append(x)
        layer_cache = torch.stack(layer_values, dim=0)
        tag_h = x[-1, :, :]
        logits = self.tag_transformer._fc(tag_h)
        return logits, raw, layer_cache, tag_h


class IncrementalDecoderStep(torch.nn.Module):
    def __init__(self, table_model: torch.nn.Module):
        super().__init__()
        self.tag_transformer = table_model._tag_transformer

    def forward(
        self,
        current_tag: torch.Tensor,
        position_pe: torch.Tensor,
        memory: torch.Tensor,
        raw_cache: torch.Tensor,
        cache: torch.Tensor,
    ) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
        raw = self.tag_transformer._embedding(current_tag) + position_pe
        raw_full = torch.cat([raw_cache, raw], dim=0)
        x = raw
        layer_values = []
        for i, layer in enumerate(self.tag_transformer._decoder.layers):
            kv = raw_full if i == 0 else layer_values[-1]
            y = layer.self_attn(
                x,
                kv,
                kv,
                attn_mask=None,
                key_padding_mask=None,
                need_weights=False,
            )[0]
            x = layer.norm1(x + layer.dropout1(y))
            y = layer.multihead_attn(
                x,
                memory,
                memory,
                attn_mask=None,
                key_padding_mask=None,
                need_weights=False,
            )[0]
            x = layer.norm2(x + layer.dropout2(y))
            y = layer.linear2(layer.dropout(layer.activation(layer.linear1(x))))
            x = layer.norm3(x + layer.dropout3(y))
            prev = cache[i]
            layer_values.append(torch.cat([prev, x], dim=0))
        out_cache = torch.stack(layer_values, dim=0)
        tag_h = x[-1, :, :]
        logits = self.tag_transformer._fc(tag_h)
        return logits, raw_full, out_cache, tag_h


class FixedCacheDecoderStep(torch.nn.Module):
    def __init__(self, table_model: torch.nn.Module):
        super().__init__()
        self.tag_transformer = table_model._tag_transformer

    def forward(
        self,
        current_tag: torch.Tensor,
        position_pe: torch.Tensor,
        memory: torch.Tensor,
        raw_cache_full: torch.Tensor,
        layer_cache_full: torch.Tensor,
        current_pos_mask: torch.Tensor,
        key_padding_mask: torch.Tensor,
    ) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
        raw = self.tag_transformer._embedding(current_tag) + position_pe
        raw_full = raw_cache_full * (1.0 - current_pos_mask) + raw * current_pos_mask
        x = raw
        layer_values = []
        for i, layer in enumerate(self.tag_transformer._decoder.layers):
            if i == 0:
                kv = raw_full
            else:
                kv = layer_cache_full[i - 1] * (1.0 - current_pos_mask) + layer_values[-1] * current_pos_mask
            y = layer.self_attn(
                x,
                kv,
                kv,
                attn_mask=None,
                key_padding_mask=key_padding_mask,
                need_weights=False,
            )[0]
            x = layer.norm1(x + layer.dropout1(y))
            y = layer.multihead_attn(
                x,
                memory,
                memory,
                attn_mask=None,
                key_padding_mask=None,
                need_weights=False,
            )[0]
            x = layer.norm2(x + layer.dropout2(y))
            y = layer.linear2(layer.dropout(layer.activation(layer.linear1(x))))
            x = layer.norm3(x + layer.dropout3(y))
            layer_values.append(x)
        layer_current = torch.stack(layer_values, dim=0)
        tag_h = x[-1, :, :]
        logits = self.tag_transformer._fc(tag_h)
        return logits, raw, layer_current, tag_h


class BBoxHead(torch.nn.Module):
    def __init__(self, table_model: torch.nn.Module):
        super().__init__()
        self.bbox_decoder = table_model._bbox_decoder

    def forward(self, enc_out: torch.Tensor, tag_h: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        encoder_out = self.bbox_decoder._input_filter(enc_out.permute(0, 3, 1, 2)).permute(
            0, 2, 3, 1
        )
        encoder_dim = encoder_out.size(3)
        encoder_out = encoder_out.view(1, -1, encoder_dim)
        num_cells = tag_h.shape[0]
        mean_encoder_out = encoder_out.mean(dim=1)
        h = self.bbox_decoder._init_h(mean_encoder_out).expand(num_cells, -1)
        awe, _ = self.bbox_decoder._attention(encoder_out, tag_h, h)
        gate = self.bbox_decoder._sigmoid(self.bbox_decoder._f_beta(h))
        h = (gate * awe) * h
        classes = self.bbox_decoder._class_embed(h)
        bboxes = self.bbox_decoder._bbox_embed(h).sigmoid()
        return classes, bboxes


def load_v1_model() -> Any:
    options = TableStructureOptions(do_cell_matching=True, mode=TableFormerMode.ACCURATE)
    accelerator = AcceleratorOptions(device=AcceleratorDevice.CPU, num_threads=4)
    docling_model = TableStructureModel(True, None, options, accelerator)
    table_model = docling_model.tf_predictor.get_model()
    table_model.eval()
    return docling_model


def export_models(out_dir: Path, force: bool = False) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "encoder": out_dir / "docling_v1_encoder.onnx",
        "decoder_step": out_dir / "docling_v1_decoder_step.onnx",
        "bbox_head": out_dir / "docling_v1_bbox_head.onnx",
    }
    sidecar_paths = {
        "positional_encoding": out_dir / "positional_encoding.npy",
        "word_map_tag": out_dir / "word_map_tag.json",
        "tm_config": out_dir / "tm_config.json",
    }
    if (
        not force
        and all(path.exists() for path in paths.values())
        and all(path.exists() for path in sidecar_paths.values())
    ):
        return paths

    docling_model = load_v1_model()
    table_model = docling_model.tf_predictor.get_model()
    table_model.eval()

    image = torch.randn(1, 3, 448, 448, dtype=torch.float32)
    start_id = docling_model.tf_predictor._word_map["word_map_tag"]["<start>"]
    current_tag = torch.tensor([[start_id]], dtype=torch.long)
    pos_pe0 = table_model._tag_transformer._positional_encoding.pe[:1].detach().clone()

    with torch.no_grad():
        enc_out, memory = EncoderBundle(table_model)(image)
        max_len = table_model._tag_transformer._positional_encoding.pe.shape[0]
        raw_cache_full = torch.zeros(max_len, 1, 512, dtype=torch.float32)
        layer_cache_full = torch.zeros(6, max_len, 1, 512, dtype=torch.float32)
        current_pos_mask = torch.zeros(max_len, 1, 1, dtype=torch.float32)
        current_pos_mask[0, 0, 0] = 1.0
        key_padding_mask = torch.zeros(1, max_len, dtype=torch.bool)
        key_padding_mask[:, 1:] = True
        _logits2, _raw_current, _layer_current, tag_h2 = FixedCacheDecoderStep(table_model)(
            current_tag,
            pos_pe0,
            memory,
            raw_cache_full,
            layer_cache_full,
            current_pos_mask,
            key_padding_mask,
        )
        bbox_tag_h = tag_h2.repeat(3, 1)

    common_kwargs = {
        "opset_version": 17,
        "do_constant_folding": True,
        "dynamo": False,
    }

    if force or not all(path.exists() for path in paths.values()):
        torch.onnx.export(
            EncoderBundle(table_model).eval(),
            (image,),
            paths["encoder"],
            input_names=["images"],
            output_names=["enc_out", "memory"],
            dynamic_axes={"images": {0: "batch"}, "enc_out": {0: "batch"}, "memory": {1: "batch"}},
            **common_kwargs,
        )
        torch.onnx.export(
            FixedCacheDecoderStep(table_model).eval(),
            (
                current_tag,
                pos_pe0,
                memory,
                raw_cache_full,
                layer_cache_full,
                current_pos_mask,
                key_padding_mask,
            ),
            paths["decoder_step"],
            input_names=[
                "current_tag",
                "position_pe",
                "memory",
                "raw_cache_full",
                "layer_cache_full",
                "current_pos_mask",
                "key_padding_mask",
            ],
            output_names=["logits", "raw_current", "layer_current", "tag_h"],
            dynamic_axes={"memory": {1: "batch"}},
            **common_kwargs,
        )
        torch.onnx.export(
            BBoxHead(table_model).eval(),
            (enc_out, bbox_tag_h),
            paths["bbox_head"],
            input_names=["enc_out", "tag_h"],
            output_names=["classes", "bboxes"],
            dynamic_axes={"tag_h": {0: "num_cells"}, "classes": {0: "num_cells"}, "bboxes": {0: "num_cells"}},
            **common_kwargs,
        )
    np.save(
        sidecar_paths["positional_encoding"],
        table_model._tag_transformer._positional_encoding.pe.detach().cpu().numpy().astype(np.float32),
    )
    sidecar_paths["word_map_tag"].write_text(
        json.dumps(docling_model.tf_predictor._word_map["word_map_tag"], indent=2),
        encoding="utf-8",
    )
    sidecar_paths["tm_config"].write_text(
        json.dumps(docling_model.tf_predictor._config, indent=2, default=str),
        encoding="utf-8",
    )
    return paths


def _merge_bbox_np(bbox1: np.ndarray, bbox2: np.ndarray) -> np.ndarray:
    new_w = (bbox2[0] + bbox2[2] / 2) - (bbox1[0] - bbox1[2] / 2)
    new_h = (bbox2[1] + bbox2[3] / 2) - (bbox1[1] - bbox1[3] / 2)
    new_left = bbox1[0] - bbox1[2] / 2
    new_top = min((bbox2[1] - bbox2[3] / 2), (bbox1[1] - bbox1[3] / 2))
    new_cx = new_left + new_w / 2
    new_cy = new_top + new_h / 2
    return np.array([new_cx, new_cy, new_w, new_h], dtype=np.float32)


def run_onnx_predict(paths: dict[str, Path], image_batch: torch.Tensor, word_map: dict[str, int], max_steps: int = 1024) -> dict[str, Any]:
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = 4
    providers = ["CPUExecutionProvider"]
    enc_sess = ort.InferenceSession(str(paths["encoder"]), sess_options=opts, providers=providers)
    step_sess = ort.InferenceSession(str(paths["decoder_step"]), sess_options=opts, providers=providers)
    bbox_sess = ort.InferenceSession(str(paths["bbox_head"]), sess_options=opts, providers=providers)
    pe = np.load(paths["encoder"].parent / "positional_encoding.npy")

    enc_out, memory = enc_sess.run(None, {"images": image_batch.detach().cpu().numpy().astype(np.float32)})
    current_tag = np.array([[word_map["<start>"]]], dtype=np.int64)
    seq = [word_map["<start>"]]
    output_tags: list[int] = []
    max_len = int(pe.shape[0])
    raw_cache_full = np.zeros((max_len, 1, 512), dtype=np.float32)
    layer_cache_full = np.zeros((6, max_len, 1, 512), dtype=np.float32)
    tag_h_buf: list[np.ndarray] = []

    skip_next_tag = True
    prev_tag_ucel = False
    line_num = 0
    first_lcel = True
    bboxes_to_merge: dict[int, int] = {}
    cur_bbox_ind = -1
    bbox_ind = 0

    timings: list[float] = []
    while len(output_tags) < max_steps:
        pos = len(seq) - 1
        position_pe = pe[pos : pos + 1]
        current_pos_mask = np.zeros((max_len, 1, 1), dtype=np.float32)
        current_pos_mask[pos, 0, 0] = 1.0
        key_padding_mask = np.zeros((1, max_len), dtype=bool)
        key_padding_mask[:, pos + 1 :] = True
        t0 = time.perf_counter()
        logits, raw_current, layer_current, tag_h = step_sess.run(
            None,
            {
                "current_tag": current_tag,
                "position_pe": position_pe,
                "memory": memory,
                "raw_cache_full": raw_cache_full,
                "layer_cache_full": layer_cache_full,
                "current_pos_mask": current_pos_mask,
                "key_padding_mask": key_padding_mask,
            },
        )
        timings.append(time.perf_counter() - t0)
        raw_cache_full[pos : pos + 1] = raw_current
        layer_cache_full[:, pos : pos + 1] = layer_current
        new_tag = int(np.argmax(logits[0], axis=0))

        if line_num == 0 and new_tag == word_map["xcel"]:
            new_tag = word_map["lcel"]
        if prev_tag_ucel and new_tag == word_map["lcel"]:
            new_tag = word_map["fcel"]

        if new_tag == word_map["<end>"]:
            output_tags.append(new_tag)
            seq.append(new_tag)
            break

        output_tags.append(new_tag)

        if not skip_next_tag:
            if new_tag in [
                word_map["fcel"],
                word_map["ecel"],
                word_map["ched"],
                word_map["rhed"],
                word_map["srow"],
                word_map["nl"],
                word_map["ucel"],
            ]:
                tag_h_buf.append(tag_h[0].astype(np.float32))
                if first_lcel is not True:
                    bboxes_to_merge[cur_bbox_ind] = bbox_ind
                bbox_ind += 1

        if new_tag != word_map["lcel"]:
            first_lcel = True
        else:
            if first_lcel:
                tag_h_buf.append(tag_h[0].astype(np.float32))
                first_lcel = False
                cur_bbox_ind = bbox_ind
                bboxes_to_merge[cur_bbox_ind] = -1
                bbox_ind += 1

        if new_tag in [word_map["nl"], word_map["ucel"], word_map["xcel"]]:
            skip_next_tag = True
        else:
            skip_next_tag = False

        prev_tag_ucel = new_tag == word_map["ucel"]
        if new_tag == word_map["nl"]:
            line_num += 1

        seq.append(new_tag)
        current_tag = np.array([[new_tag]], dtype=np.int64)

    if tag_h_buf:
        tag_h = np.stack(tag_h_buf, axis=0).astype(np.float32)
        outputs_class, outputs_coord = bbox_sess.run(None, {"enc_out": enc_out, "tag_h": tag_h})
    else:
        outputs_class = np.empty((0, 3), dtype=np.float32)
        outputs_coord = np.empty((0, 4), dtype=np.float32)

    merged_class = []
    merged_coord = []
    boxes_to_skip = set()
    for box_ind in range(len(outputs_coord)):
        box1 = outputs_coord[box_ind]
        cls1 = outputs_class[box_ind]
        if box_ind in bboxes_to_merge:
            target = bboxes_to_merge[box_ind]
            if 0 <= target < len(outputs_coord):
                box2 = outputs_coord[target]
                boxes_to_skip.add(target)
                merged_coord.append(_merge_bbox_np(box1, box2))
                merged_class.append(cls1)
        else:
            if box_ind not in boxes_to_skip:
                merged_coord.append(box1)
                merged_class.append(cls1)

    if merged_coord:
        outputs_coord = np.stack(merged_coord, axis=0).astype(np.float32)
        outputs_class = np.stack(merged_class, axis=0).astype(np.float32)
    else:
        outputs_coord = np.empty((0, 4), dtype=np.float32)
        outputs_class = np.empty((0, 3), dtype=np.float32)

    return {
        "seq": seq,
        "classes": outputs_class,
        "coords": outputs_coord,
        "decode_sec": float(sum(timings)),
        "avg_step_sec": float(sum(timings) / len(timings)) if timings else 0.0,
        "steps": len(timings),
    }


def compare_probe(paths: dict[str, Path], out_dir: Path) -> dict[str, Any]:
    docling_model = load_v1_model()
    predictor = docling_model.tf_predictor
    table_model = predictor.get_model()
    table_model.eval()
    word_map = predictor._word_map["word_map_tag"]

    import benchmark_current_table_pipeline as gt_bench
    import fitz

    class Quiet:
        def log(self, *args: Any, **kwargs: Any) -> None:
            return None

    ocr_pdf = ROOT / "temp" / "groundtruth5_pipeline" / "scan02_ocr.pdf"
    pdf_lines, layout_regions_by_page, _page_info = gt_bench.prepare_pipeline_inputs(ocr_pdf, Quiet())
    table_bbox = None
    for page_num, regions in sorted(layout_regions_by_page.items()):
        for region in regions:
            if region.get("type") == "table" and region.get("bbox_pdf"):
                table_bbox = (page_num, [float(v) for v in region["bbox_pdf"][:4]])
                break
        if table_bbox:
            break
    if table_bbox is None:
        raise RuntimeError("No scan02 table bbox found")

    doc = fitz.open(str(ocr_pdf))
    page = doc[table_bbox[0] - 1]
    scale = 1024 / float(page.rect.height)
    page_pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
    page_arr = np.frombuffer(page_pix.samples, dtype=np.uint8).reshape(page_pix.height, page_pix.width, page_pix.n)
    if page_pix.n == 4:
        page_arr = page_arr[:, :, :3]
    scaled_bbox = [v * scale for v in table_bbox[1]]
    crop = page_arr[
        round(scaled_bbox[1]) : round(scaled_bbox[3]),
        round(scaled_bbox[0]) : round(scaled_bbox[2]),
    ]
    image_batch = predictor._prepare_image(crop)
    doc.close()

    with torch.no_grad():
        t0 = time.perf_counter()
        pt_seq, pt_classes, pt_coords = table_model.predict(
            image_batch,
            predictor._config["predict"]["max_steps"],
            predictor._config["predict"]["beam_size"],
        )
        pt_sec = time.perf_counter() - t0

    onnx_res = run_onnx_predict(paths, image_batch, word_map, predictor._config["predict"]["max_steps"])
    pt_coords_np = pt_coords.detach().cpu().numpy()
    pt_classes_np = pt_classes.detach().cpu().numpy()
    seq_match = pt_seq == onnx_res["seq"]
    coord_max_abs = float(np.max(np.abs(pt_coords_np - onnx_res["coords"]))) if pt_coords_np.shape == onnx_res["coords"].shape and pt_coords_np.size else None
    class_max_abs = float(np.max(np.abs(pt_classes_np - onnx_res["classes"]))) if pt_classes_np.shape == onnx_res["classes"].shape and pt_classes_np.size else None
    result = {
        "pytorch_sec": pt_sec,
        "onnx_decode_sec": onnx_res["decode_sec"],
        "onnx_avg_step_sec": onnx_res["avg_step_sec"],
        "pytorch_seq_len": len(pt_seq),
        "onnx_seq_len": len(onnx_res["seq"]),
        "seq_match": seq_match,
        "pytorch_seq": pt_seq[:200],
        "onnx_seq": onnx_res["seq"][:200],
        "pytorch_coords_shape": list(pt_coords_np.shape),
        "onnx_coords_shape": list(onnx_res["coords"].shape),
        "coord_max_abs": coord_max_abs,
        "class_max_abs": class_max_abs,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "scan02_parity.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", type=Path, default=ROOT / "temp" / "docling_v1_onnx_export")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args()

    paths = export_models(args.out_dir, force=args.force)
    print(json.dumps({key: str(value) for key, value in paths.items()}, indent=2))
    if args.probe:
        result = compare_probe(paths, args.out_dir)
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
