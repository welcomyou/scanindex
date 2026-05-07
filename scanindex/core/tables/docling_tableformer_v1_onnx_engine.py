"""Docling TableFormer v1 accurate ONNX adapter.

The neural TableFormer v1 accurate model runs through ONNX Runtime. The
structure/text matching path intentionally stays aligned with Docling v1's
official implementation: CellMatcher + MatchingPostProcessor + OTSL parsing.
When the exported artifacts are already present, this runtime does not import
or initialize PyTorch.
"""

from __future__ import annotations

import copy
import json
import logging
import threading
from itertools import groupby
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Optional

import cv2
import fitz
import numpy as np
import onnxruntime as ort

from scanindex.core.tables.docling_tableformer_engine import (
    BBox,
    DoclingTableFormerRegion,
    _layout_table_bboxes,
    _ocr_text_cells_for_table,
    _render_crop,
    _table_to_region,
)
from docling_ibm_models.tableformer.data_management.matching_post_processor import (
    MatchingPostProcessor,
)
from docling_ibm_models.tableformer.data_management.tf_cell_matcher import CellMatcher
from docling_ibm_models.tableformer.otsl import otsl_to_html


try:
    from scanindex.infra.paths import get_base_dir
    ROOT = Path(get_base_dir())
except Exception:
    ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ARTIFACT_DIR = ROOT / "models" / "docling_tableformer_v1_stepcache_onnx"
LOG_LEVEL = logging.WARN

cv2.setNumThreads(0)
cv2.ocl.setUseOpenCL(False)


def _artifact_paths(artifact_dir: Path) -> dict[str, Path]:
    artifact_dir = Path(artifact_dir)
    return {
        "encoder": artifact_dir / "docling_v1_encoder.onnx",
        "decoder_step": artifact_dir / "docling_v1_decoder_step.onnx",
        "bbox_head": artifact_dir / "docling_v1_bbox_head.onnx",
        "positional_encoding": artifact_dir / "positional_encoding.npy",
        "word_map_tag": artifact_dir / "word_map_tag.json",
        "tm_config": artifact_dir / "tm_config.json",
    }


def _stepcache_artifact_paths(artifact_dir: Path) -> dict[str, Path]:
    artifact_dir = Path(artifact_dir)
    onnx_dir = artifact_dir / "onnx" / "accurate"
    config_candidates = sorted((artifact_dir / "hf_cache").rglob("model_artifacts/tableformer/accurate/tm_config.json"))
    return {
        "encoder": onnx_dir / "tableformer_accurate_encoder.onnx",
        "decoder_step": onnx_dir / "tableformer_accurate_decoder_step.onnx",
        "bbox_head": onnx_dir / "tableformer_accurate_bbox_decoder.onnx",
        "tm_config": config_candidates[0] if config_candidates else artifact_dir / "tm_config.json",
    }


def is_docling_tableformer_v1_onnx_available(
    artifact_dir: Path = DEFAULT_ARTIFACT_DIR,
) -> bool:
    artifact_dir = _resolve_artifact_dir(artifact_dir)
    paths = _artifact_paths(artifact_dir)
    if all(path.exists() for path in paths.values()):
        return True
    step_paths = _stepcache_artifact_paths(artifact_dir)
    return all(path.exists() for path in step_paths.values())


def _resolve_artifact_dir(artifact_dir: Path) -> Path:
    artifact_dir = Path(artifact_dir)
    paths = _artifact_paths(artifact_dir)
    if all(path.exists() for path in paths.values()):
        return artifact_dir
    step_paths = _stepcache_artifact_paths(artifact_dir)
    if all(path.exists() for path in step_paths.values()):
        return artifact_dir
    return artifact_dir


def _box_cxcywh_to_xyxy(x: np.ndarray) -> np.ndarray:
    x_c, y_c, w, h = np.moveaxis(x, -1, 0)
    return np.stack(
        [
            x_c - 0.5 * w,
            y_c - 0.5 * h,
            x_c + 0.5 * w,
            y_c + 0.5 * h,
        ],
        axis=-1,
    )


def _remove_padding(seq: list[int]) -> list[int]:
    pad_len = 0
    for item in reversed(seq):
        if item != 0:
            break
        pad_len += 1
    if pad_len == 0:
        return seq
    return seq[:-pad_len]


def _otsl_sqr_chk(rs_list: list[str], logdebug: bool = False) -> bool:
    rs_list_split = [
        list(group) for key, group in groupby(rs_list, lambda value: value == "nl") if not key
    ]
    is_square = True
    if len(rs_list_split) > 0:
        init_tag_len = len(rs_list_split[0]) + 1
        for line in rs_list_split:
            line.append("nl")
            if len(line) != init_tag_len:
                is_square = False
    return is_square


class _PureDoclingV1OnnxTableFormer:
    def __init__(self, artifact_dir: Path, num_threads: int = 4):
        paths = _artifact_paths(artifact_dir)
        self._export_format = "fixed_cache"
        if not all(path.exists() for path in paths.values()):
            paths = _stepcache_artifact_paths(artifact_dir)
            self._export_format = "step_cache"
        self._config = json.loads(paths["tm_config"].read_text(encoding="utf-8"))
        if "word_map_tag" in paths and paths["word_map_tag"].exists():
            self._word_map = json.loads(paths["word_map_tag"].read_text(encoding="utf-8"))
        else:
            self._word_map = self._config["dataset_wordmap"]["word_map_tag"]
        self._rev_word_map = {value: key for key, value in self._word_map.items()}
        self._pe = (
            np.load(paths["positional_encoding"]).astype(np.float32)
            if "positional_encoding" in paths and paths["positional_encoding"].exists()
            else None
        )
        self._remove_padding = self._config.get("model", {}).get("type") == "TableModel02"
        self.enable_post_process = not self._config.get("predict", {}).get(
            "disable_post_process", False
        )
        self._cell_matcher = CellMatcher(self._config)
        self._post_processor = MatchingPostProcessor(self._config)
        self._logger = logging.getLogger(self.__class__.__name__)

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = max(1, int(num_threads))
        opts.inter_op_num_threads = 1
        providers = ["CPUExecutionProvider"]
        self.encoder = ort.InferenceSession(
            str(paths["encoder"]), sess_options=opts, providers=providers
        )
        self.decoder_step = ort.InferenceSession(
            str(paths["decoder_step"]), sess_options=opts, providers=providers
        )
        self.bbox_head = ort.InferenceSession(
            str(paths["bbox_head"]), sess_options=opts, providers=providers
        )
        if self._export_format == "step_cache":
            cache_input = next(
                input_meta
                for input_meta in self.decoder_step.get_inputs()
                if input_meta.name == "cache"
            )
            self._cache_layers = int(cache_input.shape[0])
            self._cache_dim = int(cache_input.shape[3])

    def _log(self):
        return self._logger

    def _prepare_image(self, mat_image: np.ndarray) -> np.ndarray:
        norm = self._config["dataset"]["image_normalization"]
        mean = np.array(norm["mean"])
        std = np.array(norm["std"])
        resized_size = int(self._config["dataset"]["resized_image"])

        img = (mat_image.astype(np.float32) - 255.0 * mean) / std
        img = cv2.resize(
            img,
            dsize=(resized_size, resized_size),
            interpolation=cv2.INTER_LINEAR,
        )
        img = img.transpose(2, 1, 0)
        return np.asarray(img / 255.0, dtype=np.float32)[None, ...]

    def _run_onnx_predict(
        self,
        image_batch: np.ndarray,
        max_steps: int,
    ) -> tuple[list[int], np.ndarray, np.ndarray]:
        if self._export_format == "step_cache":
            return self._run_step_cache_onnx_predict(image_batch, max_steps)
        if self._pe is None:
            raise RuntimeError("fixed-cache TableFormer ONNX export is missing positional_encoding.npy")
        enc_out, memory = self.encoder.run(
            None,
            {"images": image_batch.astype(np.float32, copy=False)},
        )
        max_len = int(self._pe.shape[0])
        raw_cache_full = np.zeros((max_len, 1, 512), dtype=np.float32)
        layer_cache_full = np.zeros((6, max_len, 1, 512), dtype=np.float32)
        current_tag = np.array([[self._word_map["<start>"]]], dtype=np.int64)
        seq = [self._word_map["<start>"]]
        output_tags: list[int] = []
        tag_h_buf: list[np.ndarray] = []

        skip_next_tag = True
        prev_tag_ucel = False
        line_num = 0
        first_lcel = True
        bboxes_to_merge: dict[int, int] = {}
        cur_bbox_ind = -1
        bbox_ind = 0

        while len(output_tags) < max_steps:
            pos = len(seq) - 1
            position_pe = self._pe[pos : pos + 1]
            current_pos_mask = np.zeros((max_len, 1, 1), dtype=np.float32)
            current_pos_mask[pos, 0, 0] = 1.0
            key_padding_mask = np.zeros((1, max_len), dtype=bool)
            key_padding_mask[:, pos + 1 :] = True

            logits, raw_current, layer_current, tag_h = self.decoder_step.run(
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
            raw_cache_full[pos : pos + 1] = raw_current
            layer_cache_full[:, pos : pos + 1] = layer_current
            new_tag = int(np.argmax(logits[0], axis=0))

            if line_num == 0 and new_tag == self._word_map["xcel"]:
                new_tag = self._word_map["lcel"]
            if prev_tag_ucel and new_tag == self._word_map["lcel"]:
                new_tag = self._word_map["fcel"]

            if new_tag == self._word_map["<end>"]:
                output_tags.append(new_tag)
                seq.append(new_tag)
                break

            output_tags.append(new_tag)

            if not skip_next_tag and new_tag in [
                self._word_map["fcel"],
                self._word_map["ecel"],
                self._word_map["ched"],
                self._word_map["rhed"],
                self._word_map["srow"],
                self._word_map["nl"],
                self._word_map["ucel"],
            ]:
                tag_h_buf.append(tag_h[0].astype(np.float32))
                if first_lcel is not True:
                    bboxes_to_merge[cur_bbox_ind] = bbox_ind
                bbox_ind += 1

            if new_tag != self._word_map["lcel"]:
                first_lcel = True
            elif first_lcel:
                tag_h_buf.append(tag_h[0].astype(np.float32))
                first_lcel = False
                cur_bbox_ind = bbox_ind
                bboxes_to_merge[cur_bbox_ind] = -1
                bbox_ind += 1

            skip_next_tag = new_tag in [
                self._word_map["nl"],
                self._word_map["ucel"],
                self._word_map["xcel"],
            ]
            prev_tag_ucel = new_tag == self._word_map["ucel"]
            if new_tag == self._word_map["nl"]:
                line_num += 1

            seq.append(new_tag)
            current_tag = np.array([[new_tag]], dtype=np.int64)

        if tag_h_buf:
            tag_h = np.stack(tag_h_buf, axis=0).astype(np.float32)
            outputs_class, outputs_coord = self.bbox_head.run(
                None,
                {"enc_out": enc_out, "tag_h": tag_h},
            )
        else:
            outputs_class = np.empty((0, 3), dtype=np.float32)
            outputs_coord = np.empty((0, 4), dtype=np.float32)

        return self._merge_span_bboxes(outputs_class, outputs_coord, bboxes_to_merge, seq)

    def _run_step_cache_onnx_predict(
        self,
        image_batch: np.ndarray,
        max_steps: int,
    ) -> tuple[list[int], np.ndarray, np.ndarray]:
        enc_out, memory = self.encoder.run(
            None,
            {"image": image_batch.astype(np.float32, copy=False)},
        )
        decoded_tags = np.array([[self._word_map["<start>"]]], dtype=np.int64)
        cache = np.zeros((self._cache_layers, 0, 1, self._cache_dim), dtype=np.float32)
        output_tags: list[int] = []
        tag_h_buf: list[np.ndarray] = []

        skip_next_tag = True
        prev_tag_ucel = False
        line_num = 0
        first_lcel = True
        bboxes_to_merge: dict[int, int] = {}
        cur_bbox_ind = -1
        bbox_ind = 0

        for _step in range(max_steps):
            logits, last_hidden, cache = self.decoder_step.run(
                None,
                {
                    "decoded_tags": decoded_tags,
                    "memory": memory,
                    "cache": cache,
                },
            )
            new_tag = int(np.argmax(logits, axis=1)[0])

            if line_num == 0 and new_tag == self._word_map["xcel"]:
                new_tag = self._word_map["lcel"]
            if prev_tag_ucel and new_tag == self._word_map["lcel"]:
                new_tag = self._word_map["fcel"]

            if new_tag == self._word_map["<end>"]:
                output_tags.append(new_tag)
                decoded_tags = np.concatenate(
                    [decoded_tags, np.array([[new_tag]], dtype=np.int64)], axis=0
                )
                break

            output_tags.append(new_tag)

            if not skip_next_tag and new_tag in [
                self._word_map["fcel"],
                self._word_map["ecel"],
                self._word_map["ched"],
                self._word_map["rhed"],
                self._word_map["srow"],
                self._word_map["nl"],
                self._word_map["ucel"],
            ]:
                tag_h_buf.append(last_hidden[:, 0, :].copy().astype(np.float32))
                if first_lcel is not True:
                    bboxes_to_merge[cur_bbox_ind] = bbox_ind
                bbox_ind += 1

            if new_tag != self._word_map["lcel"]:
                first_lcel = True
            elif first_lcel:
                tag_h_buf.append(last_hidden[:, 0, :].copy().astype(np.float32))
                first_lcel = False
                cur_bbox_ind = bbox_ind
                bboxes_to_merge[cur_bbox_ind] = -1
                bbox_ind += 1

            skip_next_tag = new_tag in [
                self._word_map["nl"],
                self._word_map["ucel"],
                self._word_map["xcel"],
            ]
            prev_tag_ucel = new_tag == self._word_map["ucel"]
            if new_tag == self._word_map["nl"]:
                line_num += 1

            decoded_tags = np.concatenate(
                [decoded_tags, np.array([[new_tag]], dtype=np.int64)], axis=0
            )

        seq = decoded_tags.squeeze().tolist()
        if tag_h_buf:
            tag_h_stacked = np.stack(
                [hidden[None, ...] if hidden.ndim == 1 else hidden for hidden in tag_h_buf],
                axis=0,
            )
            tag_h_stacked = tag_h_stacked.reshape(-1, 1, self._cache_dim).astype(np.float32)
            outputs_class, outputs_coord = self.bbox_head.run(
                None,
                {"enc_out": enc_out, "tag_H_stacked": tag_h_stacked},
            )
        else:
            outputs_class = np.empty((0, 3), dtype=np.float32)
            outputs_coord = np.empty((0, 4), dtype=np.float32)

        return self._merge_span_bboxes(outputs_class, outputs_coord, bboxes_to_merge, seq)

    @staticmethod
    def _merge_bbox(bbox1: np.ndarray, bbox2: np.ndarray) -> np.ndarray:
        new_w = (bbox2[0] + bbox2[2] / 2) - (bbox1[0] - bbox1[2] / 2)
        new_h = (bbox2[1] + bbox2[3] / 2) - (bbox1[1] - bbox1[3] / 2)
        new_left = bbox1[0] - bbox1[2] / 2
        new_top = min((bbox2[1] - bbox2[3] / 2), (bbox1[1] - bbox1[3] / 2))
        return np.array(
            [new_left + new_w / 2, new_top + new_h / 2, new_w, new_h],
            dtype=np.float32,
        )

    @classmethod
    def _merge_span_bboxes(
        cls,
        outputs_class: np.ndarray,
        outputs_coord: np.ndarray,
        bboxes_to_merge: dict[int, int],
        seq: list[int],
    ) -> tuple[list[int], np.ndarray, np.ndarray]:
        merged_class = []
        merged_coord = []
        boxes_to_skip = set()
        for box_ind in range(len(outputs_coord)):
            if box_ind in bboxes_to_merge:
                target = bboxes_to_merge[box_ind]
                if 0 <= target < len(outputs_coord):
                    boxes_to_skip.add(target)
                    merged_coord.append(cls._merge_bbox(outputs_coord[box_ind], outputs_coord[target]))
                    merged_class.append(outputs_class[box_ind])
            elif box_ind not in boxes_to_skip:
                merged_coord.append(outputs_coord[box_ind])
                merged_class.append(outputs_class[box_ind])
        if not merged_coord:
            return (
                seq,
                np.empty((0, 3), dtype=np.float32),
                np.empty((0, 4), dtype=np.float32),
            )
        return (
            seq,
            np.stack(merged_class, axis=0).astype(np.float32),
            np.stack(merged_coord, axis=0).astype(np.float32),
        )

    @staticmethod
    def _deletebbox(listofbboxes, index):
        return [bbox for i, bbox in enumerate(listofbboxes) if i not in index]

    def _remove_bbox_span_desync(self, prediction):
        index_to_delete_from = 0
        indexes_to_delete = []
        for html_elem in prediction["html_seq"]:
            if html_elem == "<td>":
                index_to_delete_from += 1
            if html_elem == ">":
                index_to_delete_from += 1
                indexes_to_delete.append(index_to_delete_from)
        return self._deletebbox(prediction["bboxes"], indexes_to_delete)

    def _check_bbox_sync(self, prediction):
        count_bbox = len(prediction["bboxes"])
        count_td = 0
        for html_elem in prediction["html_seq"]:
            if html_elem == "<td>" or html_elem == ">":
                count_td += 1
            if html_elem in ["fcel", "ecel", "ched", "rhed", "srow"]:
                count_td += 1
        if count_bbox != count_td:
            return False, self._remove_bbox_span_desync(prediction)
        return True, prediction["bboxes"]

    @staticmethod
    def _merge_tf_output(docling_output, pdf_cells):
        tf_output = []
        tf_cells_map = {}

        for docling_item in docling_output:
            r_idx = str(docling_item["start_row_offset_idx"])
            c_idx = str(docling_item["start_col_offset_idx"])
            cell_key = c_idx + "_" + r_idx
            if cell_key not in tf_cells_map:
                tf_cells_map[cell_key] = {
                    "bbox": docling_item["bbox"],
                    "row_span": docling_item["row_span"],
                    "col_span": docling_item["col_span"],
                    "start_row_offset_idx": docling_item["start_row_offset_idx"],
                    "end_row_offset_idx": docling_item["end_row_offset_idx"],
                    "start_col_offset_idx": docling_item["start_col_offset_idx"],
                    "end_col_offset_idx": docling_item["end_col_offset_idx"],
                    "indentation_level": docling_item["indentation_level"],
                    "text_cell_bboxes": [],
                    "column_header": docling_item["column_header"],
                    "row_header": docling_item["row_header"],
                    "row_section": docling_item["row_section"],
                }

            for pdf_cell in pdf_cells:
                if pdf_cell["id"] == docling_item["cell_id"]:
                    text_cell_bbox = {
                        "b": pdf_cell["bbox"][3],
                        "l": pdf_cell["bbox"][0],
                        "r": pdf_cell["bbox"][2],
                        "t": pdf_cell["bbox"][1],
                        "token": pdf_cell["text"],
                    }
                    tf_cells_map[cell_key]["text_cell_bboxes"].append(text_cell_bbox)

        for key in tf_cells_map:
            tf_output.append(tf_cells_map[key])
        return tf_output

    @staticmethod
    def _resize_img(image, width=None, height=None, inter=cv2.INTER_AREA):
        dim = None
        h, w = image.shape[:2]
        sf = 1.0
        if width is None and height is None:
            return image, sf
        if width is None:
            r = height / float(h)
            sf = r
            dim = (int(w * r), height)
        else:
            r = width / float(w)
            sf = r
            dim = (width, int(h * r))
        return cv2.resize(image, dim, interpolation=inter), sf

    def multi_table_predict(
        self,
        iocr_page,
        table_bboxes,
        do_matching=True,
        correct_overlapping_cells=False,
        sort_row_col_indexes=True,
    ):
        multi_tf_output = []
        page_image = iocr_page["image"]
        page_image_resized, scale_factor = self._resize_img(page_image, height=1024)

        for table_bbox in table_bboxes:
            table_bbox[0] = table_bbox[0] * scale_factor
            table_bbox[1] = table_bbox[1] * scale_factor
            table_bbox[2] = table_bbox[2] * scale_factor
            table_bbox[3] = table_bbox[3] * scale_factor

            table_image = page_image_resized[
                round(table_bbox[1]) : round(table_bbox[3]),
                round(table_bbox[0]) : round(table_bbox[2]),
            ]
            tf_responses, predict_details = self.predict(
                iocr_page,
                table_bbox,
                table_image,
                scale_factor,
                None,
                correct_overlapping_cells,
                do_matching=do_matching,
            )

            if sort_row_col_indexes:
                indexing_start_cols = []
                indexing_start_rows = []
                for tf_response_cell in tf_responses:
                    start_col_offset_idx = tf_response_cell["start_col_offset_idx"]
                    start_row_offset_idx = tf_response_cell["start_row_offset_idx"]
                    if start_col_offset_idx not in indexing_start_cols:
                        indexing_start_cols.append(start_col_offset_idx)
                    if start_row_offset_idx not in indexing_start_rows:
                        indexing_start_rows.append(start_row_offset_idx)

                indexing_start_cols.sort()
                indexing_start_rows.sort()
                max_end_col_idx = 0
                max_end_row_idx = 0
                for tf_response_cell in tf_responses:
                    tf_response_cell["start_col_offset_idx"] = indexing_start_cols.index(
                        tf_response_cell["start_col_offset_idx"]
                    )
                    tf_response_cell["end_col_offset_idx"] = (
                        tf_response_cell["start_col_offset_idx"]
                        + tf_response_cell["col_span"]
                    )
                    max_end_col_idx = max(
                        max_end_col_idx, tf_response_cell["end_col_offset_idx"]
                    )
                    tf_response_cell["start_row_offset_idx"] = indexing_start_rows.index(
                        tf_response_cell["start_row_offset_idx"]
                    )
                    tf_response_cell["end_row_offset_idx"] = (
                        tf_response_cell["start_row_offset_idx"]
                        + tf_response_cell["row_span"]
                    )
                    max_end_row_idx = max(
                        max_end_row_idx, tf_response_cell["end_row_offset_idx"]
                    )
                predict_details["num_cols"] = max_end_col_idx
                predict_details["num_rows"] = max_end_row_idx
            else:
                otsl_seq = predict_details["prediction"]["rs_seq"]
                predict_details["num_cols"] = otsl_seq.index("nl")
                predict_details["num_rows"] = otsl_seq.count("nl")

            multi_tf_output.append(
                {"tf_responses": tf_responses, "predict_details": predict_details}
            )
            table_bbox[0] = table_bbox[0] / scale_factor
            table_bbox[1] = table_bbox[1] / scale_factor
            table_bbox[2] = table_bbox[2] / scale_factor
            table_bbox[3] = table_bbox[3] / scale_factor
        return multi_tf_output

    def predict(
        self,
        iocr_page,
        table_bbox,
        table_image,
        scale_factor,
        eval_res_preds=None,
        correct_overlapping_cells=False,
        do_matching=True,
    ):
        max_steps = self._config["predict"]["max_steps"]
        image_batch = self._prepare_image(table_image)
        prediction = {}

        if eval_res_preds is not None:
            prediction["bboxes"] = eval_res_preds["bboxes"]
            pred_tag_seq = eval_res_preds["tag_seq"]
            prediction["classes"] = eval_res_preds.get("classes", [])
        elif self._config["predict"]["bbox"]:
            pred_tag_seq, outputs_class, outputs_coord = self._run_onnx_predict(
                image_batch, max_steps
            )
            prediction["bboxes"] = (
                []
                if outputs_coord is None or len(outputs_coord) == 0
                else _box_cxcywh_to_xyxy(outputs_coord).tolist()
            )
            prediction["classes"] = (
                []
                if outputs_class is None or len(outputs_class) == 0
                else np.argmax(outputs_class, axis=1).tolist()
            )
            if self._remove_padding:
                pred_tag_seq = _remove_padding(pred_tag_seq)
        else:
            pred_tag_seq, _, _ = self._run_onnx_predict(image_batch, max_steps)
            if self._remove_padding:
                pred_tag_seq = _remove_padding(pred_tag_seq)

        prediction["tag_seq"] = pred_tag_seq
        prediction["rs_seq"] = [self._rev_word_map[ind] for ind in pred_tag_seq[1:-1]]
        prediction["html_seq"] = otsl_to_html(prediction["rs_seq"], False)
        _otsl_sqr_chk(prediction["rs_seq"], False)

        sync, corrected_bboxes = self._check_bbox_sync(prediction)
        if not sync:
            prediction["bboxes"] = corrected_bboxes

        matching_details = {
            "table_cells": [],
            "matches": {},
            "pdf_cells": [],
            "prediction_bboxes_page": [],
        }
        scaled_table_bbox = [
            table_bbox[0] / scale_factor,
            table_bbox[1] / scale_factor,
            table_bbox[2] / scale_factor,
            table_bbox[3] / scale_factor,
        ]

        if len(prediction["bboxes"]) > 0:
            if do_matching:
                matching_details = self._cell_matcher.match_cells(
                    iocr_page, scaled_table_bbox, prediction
                )
            else:
                matching_details = self._cell_matcher.match_cells_dummy(
                    iocr_page, scaled_table_bbox, prediction
                )

        if (
            do_matching
            and len(prediction["bboxes"]) > 0
            and len(iocr_page["tokens"]) > 0
            and self.enable_post_process
        ):
            matching_details = self._post_processor.process(
                matching_details, correct_overlapping_cells
            )

        if do_matching:
            docling_output = self._generate_tf_response(
                matching_details["table_cells"],
                matching_details["matches"],
            )
            docling_output.sort(key=lambda item: item["cell_id"])
            matching_details["docling_responses"] = docling_output
            tf_output = self._merge_tf_output(docling_output, matching_details["pdf_cells"])
        else:
            tf_output = self._generate_tf_response_dummy(matching_details["table_cells"])
            tf_output.sort(key=lambda item: item["cell_id"])
            matching_details["docling_responses"] = tf_output

        return tf_output, matching_details

    @staticmethod
    def _generate_tf_response_dummy(table_cells):
        tf_cell_list = []
        for table_cell in table_cells:
            colspan_val = table_cell.get("colspan_val", 1)
            rowspan_val = table_cell.get("rowspan_val", 1)
            row_id = table_cell["row_id"]
            column_id = table_cell["column_id"]
            cell_bbox = {
                "b": table_cell["bbox"][3],
                "l": table_cell["bbox"][0],
                "r": table_cell["bbox"][2],
                "t": table_cell["bbox"][1],
                "token": "",
            }
            tf_cell_list.append(
                {
                    "cell_id": table_cell["cell_id"],
                    "bbox": cell_bbox,
                    "row_span": rowspan_val,
                    "col_span": colspan_val,
                    "start_row_offset_idx": row_id,
                    "end_row_offset_idx": row_id + rowspan_val,
                    "start_col_offset_idx": column_id,
                    "end_col_offset_idx": column_id + colspan_val,
                    "indentation_level": 0,
                    "text_cell_bboxes": [],
                    "column_header": table_cell["label"] == "ched",
                    "row_header": table_cell["label"] == "rhed",
                    "row_section": table_cell["label"] == "srow",
                }
            )
        return tf_cell_list

    @staticmethod
    def _generate_tf_response(table_cells, matches):
        tf_cell_list = []
        for pdf_cell_id, pdf_cell_matches in matches.items():
            tf_cell = {
                "bbox": {},
                "row_span": 1,
                "col_span": 1,
                "start_row_offset_idx": -1,
                "end_row_offset_idx": -1,
                "start_col_offset_idx": -1,
                "end_col_offset_idx": -1,
                "indentation_level": 0,
                "text_cell_bboxes": [{}],
                "column_header": False,
                "row_header": False,
                "row_section": False,
            }
            tf_cell["cell_id"] = int(pdf_cell_id)
            row_ids = set()
            column_ids = set()
            labels = set()

            for match in pdf_cell_matches:
                table_cell_id = match["table_cell_id"]
                candidates = [
                    table_cell
                    for table_cell in table_cells
                    if table_cell["cell_id"] == table_cell_id
                ]
                if len(candidates) == 0:
                    continue
                table_cell = candidates[0]
                row_ids.add(table_cell["row_id"])
                column_ids.add(table_cell["column_id"])
                labels.add(table_cell["label"])

                if table_cell["label"] is not None:
                    if table_cell["label"] in ["ched"]:
                        tf_cell["column_header"] = True
                    if table_cell["label"] in ["rhed"]:
                        tf_cell["row_header"] = True
                    if table_cell["label"] in ["srow"]:
                        tf_cell["row_section"] = True

                tf_cell["start_col_offset_idx"] = table_cell["column_id"]
                tf_cell["end_col_offset_idx"] = table_cell["column_id"] + 1
                tf_cell["start_row_offset_idx"] = table_cell["row_id"]
                tf_cell["end_row_offset_idx"] = table_cell["row_id"] + 1

                if "colspan_val" in table_cell:
                    tf_cell["col_span"] = table_cell["colspan_val"]
                    tf_cell["start_col_offset_idx"] = table_cell["column_id"]
                    tf_cell["end_col_offset_idx"] = (
                        table_cell["column_id"] + tf_cell["col_span"]
                    )
                if "rowspan_val" in table_cell:
                    tf_cell["row_span"] = table_cell["rowspan_val"]
                    tf_cell["start_row_offset_idx"] = table_cell["row_id"]
                    tf_cell["end_row_offset_idx"] = (
                        table_cell["row_id"] + tf_cell["row_span"]
                    )
                if "bbox" in table_cell:
                    table_match_bbox = table_cell["bbox"]
                    tf_cell["bbox"] = {
                        "b": table_match_bbox[3],
                        "l": table_match_bbox[0],
                        "r": table_match_bbox[2],
                        "t": table_match_bbox[1],
                    }

            tf_cell["row_ids"] = list(row_ids)
            tf_cell["column_ids"] = list(column_ids)
            tf_cell["label"] = "None"
            label_list = list(labels)
            if len(label_list) > 0:
                tf_cell["label"] = label_list[0]
            tf_cell_list.append(tf_cell)
        return tf_cell_list


_MODEL_CACHE = {}
_MODEL_LOCK = threading.Lock()


def _get_model(
    artifact_dir: Path = DEFAULT_ARTIFACT_DIR,
    num_threads: int = 4,
) -> _PureDoclingV1OnnxTableFormer:
    artifact_dir = _resolve_artifact_dir(artifact_dir)
    key = (Path(artifact_dir).resolve(), int(num_threads))
    with _MODEL_LOCK:
        if key not in _MODEL_CACHE:
            _MODEL_CACHE[key] = _PureDoclingV1OnnxTableFormer(
                Path(artifact_dir),
                num_threads=num_threads,
            )
        return _MODEL_CACHE[key]


def _tokens_for_table(page_lines: List[object], table_bbox: BBox, image_width: int) -> list[dict]:
    text_cells = _ocr_text_cells_for_table(page_lines, table_bbox)
    bbox_width = table_bbox[2] - table_bbox[0]
    scale = image_width / bbox_width if bbox_width > 0 else 1.0
    tokens = []
    seen_ids = set()
    for cell in text_cells:
        text = str(getattr(cell, "text", "") or "")
        if not text.strip():
            continue
        cell_id = int(getattr(cell, "index", len(tokens)) or 0)
        if cell_id in seen_ids:
            cell_id = max(seen_ids) + 1 if seen_ids else len(tokens)
        seen_ids.add(cell_id)
        cell_bbox = cell.rect.to_bounding_box()
        local_bbox = {
            "l": (cell_bbox.l - table_bbox[0]) * scale,
            "t": (cell_bbox.t - table_bbox[1]) * scale,
            "r": (cell_bbox.r - table_bbox[0]) * scale,
            "b": (cell_bbox.b - table_bbox[1]) * scale,
        }
        coord_origin = getattr(cell_bbox, "coord_origin", None)
        if coord_origin is not None:
            local_bbox["coord_origin"] = coord_origin
        tokens.append({"id": cell_id, "text": text, "bbox": local_bbox})
    return tokens


def _tf_output_to_region(
    table_out: dict,
    page_num: int,
    table_bbox: BBox,
    image_width: int,
) -> Optional[DoclingTableFormerRegion]:
    from docling_core.types.doc import BoundingBox, TableCell

    bbox_width = table_bbox[2] - table_bbox[0]
    scale = image_width / bbox_width if bbox_width > 0 else 1.0
    table_cells = []
    for element in table_out["tf_responses"]:
        tc = TableCell.model_validate(copy.deepcopy(element))
        if tc.bbox is not None:
            tc.bbox = BoundingBox(
                l=tc.bbox.l / scale + table_bbox[0],
                t=tc.bbox.t / scale + table_bbox[1],
                r=tc.bbox.r / scale + table_bbox[0],
                b=tc.bbox.b / scale + table_bbox[1],
                coord_origin=tc.bbox.coord_origin,
            )
        table_cells.append(tc)

    table = SimpleNamespace(
        table_cells=table_cells,
        num_rows=table_out["predict_details"].get("num_rows", 0),
        num_cols=table_out["predict_details"].get("num_cols", 0),
    )
    return _table_to_region(table, page_num, table_bbox)


def detect_tables_docling_tableformer_v1_onnx(
    pdf_path: str,
    logger,
    page_info: dict,
    pdf_lines: List[object],
    layout_regions_by_page: Optional[Dict[int, List[dict]]] = None,
    dpi: int = 144,
    pad_pt: float = 0.0,
    num_threads: int = 4,
    artifact_dir: Path = DEFAULT_ARTIFACT_DIR,
) -> List[DoclingTableFormerRegion]:
    del page_info
    if not layout_regions_by_page or not is_docling_tableformer_v1_onnx_available(artifact_dir):
        return []

    lines_by_page: Dict[int, List[object]] = {}
    for line in pdf_lines or []:
        lines_by_page.setdefault(int(getattr(line, "page", 0) or 0), []).append(line)

    model = _get_model(artifact_dir=artifact_dir, num_threads=num_threads)
    tables: List[DoclingTableFormerRegion] = []
    doc = fitz.open(pdf_path)
    try:
        for page_num in range(1, len(doc) + 1):
            page = doc[page_num - 1]
            layout_bboxes = _layout_table_bboxes(layout_regions_by_page, page_num)
            if not layout_bboxes:
                continue
            page_lines = lines_by_page.get(page_num, [])
            for bbox in layout_bboxes:
                try:
                    image, crop = _render_crop(page, bbox, dpi, pad_pt)
                    cluster_bbox: BBox = (
                        float(crop.x0),
                        float(crop.y0),
                        float(crop.x1),
                        float(crop.y1),
                    )
                    page_input = {
                        "width": image.width,
                        "height": image.height,
                        "image": np.asarray(image),
                        "tokens": _tokens_for_table(page_lines, cluster_bbox, image.width),
                    }
                    table_out = model.multi_table_predict(
                        page_input,
                        [[0.0, 0.0, float(image.width), float(image.height)]],
                        do_matching=True,
                    )[0]
                    region = _tf_output_to_region(
                        table_out,
                        page_num,
                        cluster_bbox,
                        image.width,
                    )
                    if region is not None:
                        region.source = "docling_tableformer"
                        setattr(region, "engine", "docling_tableformer_v1_onnx")
                        tables.append(region)
                except Exception as exc:
                    if logger is not None:
                        logger.log(
                            f"docling_tableformer_v1_onnx crop failed on page {page_num}: {exc}"
                        )
    finally:
        doc.close()

    if logger is not None:
        logger.log(
            f"docling_tableformer_v1_onnx structure recognizer found {len(tables)} tables"
        )
    return tables
