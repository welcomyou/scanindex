"""
End-to-end TableFormer step-cache inference using ONLY ONNX Runtime (no PyTorch).
Runner = encoder + decoder-step (autoregressive loop in Python) + bbox_decoder.

Validates against the golden PyTorch outputs (seq, outputs_class, outputs_coord).
"""
import os, sys, json, time
from pathlib import Path
os.environ["CUDA_VISIBLE_DEVICES"] = ""
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

import numpy as np
import onnxruntime as ort

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_WORK_DIR = REPO_ROOT / "temp" / "tableformer_onnx"
ARTIFACT_ROOT = Path(os.environ.get(
    "TABLEFORMER_ONNX_ARTIFACT_ROOT",
    REPO_ROOT / "models" / "docling_tableformer_v1_stepcache_onnx",
))
VARIANT = os.environ.get("TABLEFORMER_ONNX_VARIANT", "accurate")
ONNX_DIR = ARTIFACT_ROOT / "onnx" / VARIANT
GOLDEN = Path(os.environ.get("TABLEFORMER_ONNX_GOLDEN_DIR", DEFAULT_WORK_DIR / "golden"))


def load_word_map():
    with open(ARTIFACT_ROOT / "tm_config.json", encoding="utf-8") as f:
        return json.load(f)["dataset_wordmap"]


def make_session(path, threads=2):
    so = ort.SessionOptions()
    so.intra_op_num_threads = threads
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    return ort.InferenceSession(path, sess_options=so, providers=["CPUExecutionProvider"])


class TableFormerONNX:
    def __init__(self, onnx_dir=ONNX_DIR, variant=VARIANT, threads=2):
        self.enc = make_session(os.path.join(onnx_dir, f"tableformer_{variant}_encoder.onnx"), threads)
        self.dec = make_session(os.path.join(onnx_dir, f"tableformer_{variant}_decoder_step.onnx"), threads)
        self.bbox = make_session(os.path.join(onnx_dir, f"tableformer_{variant}_bbox_decoder.onnx"), threads)
        self.word_map = load_word_map()
        self.tag_map = self.word_map["word_map_tag"]
        self.rev_tag = {v: k for k, v in self.tag_map.items()}
        # Probe model shapes
        cache_input = next(i for i in self.dec.get_inputs() if i.name == "cache")
        # cache shape e.g. [num_layers, 'prev', 1, dim]
        self.num_layers = int(cache_input.shape[0])
        self.embed_dim = int(cache_input.shape[3])

    def predict(self, img: np.ndarray, max_steps: int = 1024):
        """img: float32 [1, 3, 448, 448] (preprocessed)"""
        # 1) Encoder
        t0 = time.time()
        enc_out, memory = self.enc.run(None, {"image": img})
        t_enc = time.time() - t0

        # 2) Autoregressive decode (Python loop with same correction rules as predict())
        wm = self.tag_map
        decoded_tags = np.array([[wm["<start>"]]], dtype=np.int64)
        cache = np.zeros((self.num_layers, 0, 1, self.embed_dim), dtype=np.float32)
        output_tags = []
        skip_next_tag = True
        prev_tag_ucel = False
        line_num = 0
        first_lcel = True
        bbox_ind = 0
        cur_bbox_ind = -1
        bboxes_to_merge = {}
        tag_H_buf = []

        t1 = time.time()
        for step in range(max_steps):
            logits, last_hidden, cache = self.dec.run(
                None,
                {"decoded_tags": decoded_tags, "memory": memory, "cache": cache},
            )
            new_tag = int(np.argmax(logits, axis=1)[0])

            # Same correction rules as predict()
            if line_num == 0 and new_tag == wm["xcel"]:
                new_tag = wm["lcel"]
            if prev_tag_ucel and new_tag == wm["lcel"]:
                new_tag = wm["fcel"]

            if new_tag == wm["<end>"]:
                output_tags.append(new_tag)
                decoded_tags = np.concatenate(
                    [decoded_tags, np.array([[new_tag]], dtype=np.int64)], axis=0
                )
                break
            output_tags.append(new_tag)

            # Bbox collection logic — replicates predict() exactly
            if not skip_next_tag:
                if new_tag in (wm["fcel"], wm["ecel"], wm["ched"], wm["rhed"],
                               wm["srow"], wm["nl"], wm["ucel"]):
                    tag_H_buf.append(last_hidden[:, 0, :].copy())  # [1, 512]
                    if first_lcel is not True:
                        bboxes_to_merge[cur_bbox_ind] = bbox_ind
                    bbox_ind += 1

            if new_tag != wm["lcel"]:
                first_lcel = True
            else:
                if first_lcel:
                    tag_H_buf.append(last_hidden[:, 0, :].copy())
                    first_lcel = False
                    cur_bbox_ind = bbox_ind
                    bboxes_to_merge[cur_bbox_ind] = -1
                    bbox_ind += 1

            if new_tag in (wm["nl"], wm["ucel"], wm["xcel"]):
                skip_next_tag = True
            else:
                skip_next_tag = False
            prev_tag_ucel = (new_tag == wm["ucel"])
            if new_tag == wm["nl"]:
                line_num += 1

            decoded_tags = np.concatenate(
                [decoded_tags, np.array([[new_tag]], dtype=np.int64)], axis=0
            )
        t_dec = time.time() - t1

        seq = decoded_tags.squeeze().tolist()

        # 3) BBox decoder
        if tag_H_buf:
            # tag_H_stacked: [N, 1, 512] (each entry was [1, 512] -> need [N, 1, 512])
            tag_H_stacked = np.stack([h[None, ...] if h.ndim == 1 else h for h in tag_H_buf], axis=0)
            # Each h is [1, 512], stack -> [N, 1, 512]
            tag_H_stacked = tag_H_stacked.reshape(-1, 1, self.embed_dim).astype(np.float32)
            t2 = time.time()
            cls_logits, coord = self.bbox.run(
                None, {"enc_out": enc_out, "tag_H_stacked": tag_H_stacked}
            )
            t_bbox = time.time() - t2
        else:
            cls_logits = np.empty((0, 3), dtype=np.float32)
            coord = np.empty((0, 4), dtype=np.float32)
            t_bbox = 0.0

        return {
            "seq": seq,
            "outputs_class": cls_logits,
            "outputs_coord": coord,
            "bboxes_to_merge": bboxes_to_merge,
            "timings": {"encoder": t_enc, "decoder": t_dec, "bbox": t_bbox,
                        "total": t_enc + t_dec + t_bbox},
        }


def main():
    print("[*] Loading ONNX runner...")
    runner = TableFormerONNX(threads=2)
    print(f"    num_layers={runner.num_layers}, embed_dim={runner.embed_dim}")

    img = np.load(os.path.join(GOLDEN, "input.npy")).astype(np.float32)
    print(f"    image shape: {img.shape}")

    print("[*] Running ONNX inference...")
    out = runner.predict(img, max_steps=1024)
    seq_o = out["seq"]
    cls_o = out["outputs_class"]
    box_o = out["outputs_coord"]
    print(f"    seq length: {len(seq_o)}, bboxes: {cls_o.shape[0]}")
    print(f"    timings: enc={out['timings']['encoder']:.3f}s "
          f"dec={out['timings']['decoder']:.3f}s "
          f"bbox={out['timings']['bbox']:.3f}s "
          f"total={out['timings']['total']:.3f}s")

    # Compare to golden
    seq_g = np.load(os.path.join(GOLDEN, "seq.npy")).tolist()
    cls_g = np.load(os.path.join(GOLDEN, "outputs_class.npy"))
    box_g = np.load(os.path.join(GOLDEN, "outputs_coord.npy"))

    print("[*] Comparing to golden PyTorch outputs...")
    print(f"    seq length:  ONNX={len(seq_o)}, PyTorch={len(seq_g)}, equal={seq_o == seq_g}")
    if seq_o == seq_g:
        print("    seq: 100% token equality")
    else:
        # Find first divergence
        for i, (a, b) in enumerate(zip(seq_o, seq_g)):
            if a != b:
                print(f"    first divergence at index {i}: ONNX={runner.rev_tag.get(a)}({a}), PyTorch={runner.rev_tag.get(b)}({b})")
                break

    if cls_o.shape == cls_g.shape:
        d_cls = float(np.abs(cls_o - cls_g).max())
        d_box = float(np.abs(box_o - box_g).max())
        print(f"    outputs_class: max abs diff = {d_cls:.3e}")
        print(f"    outputs_coord: max abs diff = {d_box:.3e}")
    else:
        print(f"    SHAPE MISMATCH: ONNX class {cls_o.shape} vs PyTorch class {cls_g.shape}")

    print("[OK] End-to-end ONNX runner complete.")


if __name__ == "__main__":
    main()
