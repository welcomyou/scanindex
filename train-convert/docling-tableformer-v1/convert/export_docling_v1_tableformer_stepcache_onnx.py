"""
Run the full export+validation pipeline for both 'fast' and 'accurate' variants.
Reuses logic factored from the per-step scripts.
"""
import os, sys, json, argparse, time
from pathlib import Path
os.environ["CUDA_VISIBLE_DEVICES"] = ""
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_WORK_DIR = REPO_ROOT / "temp" / "tableformer_onnx"
ROOT = Path(os.environ.get("TABLEFORMER_ONNX_WORKDIR", DEFAULT_WORK_DIR))
SRC = Path(os.environ.get("DOCLING_IBM_MODELS_SRC", ROOT / "docling-ibm-models"))
sys.path.insert(0, str(SRC))

import numpy as np
import torch
import torch.nn as nn
import cv2
from safetensors.torch import load_model
import onnxruntime as ort

from docling_ibm_models.tableformer.models.table04_rs.tablemodel04_rs import TableModel04_rs

HF_ROOT = Path(os.environ.get(
    "DOCLING_MODELS_TABLEFORMER_ROOT",
    ROOT / "hf_cache"
    / "models--ds4sd--docling-models"
    / "snapshots"
    / "2199320848bb9a8a519d22e4b528185a4f9a6f64"
    / "model_artifacts"
    / "tableformer",
))
ARTIFACT_ROOT = Path(os.environ.get(
    "TABLEFORMER_ONNX_ARTIFACT_ROOT",
    REPO_ROOT / "models" / "docling_tableformer_v1_stepcache_onnx",
))
OUT = ARTIFACT_ROOT / "onnx"
GOLDEN = ROOT / "golden"
os.makedirs(OUT, exist_ok=True)
os.makedirs(GOLDEN, exist_ok=True)


# ----- Wrappers (copied verbatim from per-step scripts) -----------------------

class EncoderBundle(nn.Module):
    def __init__(self, full_model: TableModel04_rs):
        super().__init__()
        self.encoder = full_model._encoder
        self.input_filter = full_model._tag_transformer._input_filter
        self.tag_encoder = full_model._tag_transformer._encoder

    def forward(self, imgs):
        enc_out = self.encoder(imgs)
        f = self.input_filter(enc_out.permute(0, 3, 1, 2)).permute(0, 2, 3, 1)
        bs = f.size(0); dim = f.size(-1)
        f = f.view(bs, -1, dim).permute(1, 0, 2)
        memory = self.tag_encoder(f, mask=None)
        return enc_out, memory


class DecoderStepWrapper(nn.Module):
    def __init__(self, full_model: TableModel04_rs):
        super().__init__()
        tt = full_model._tag_transformer
        self.embedding = tt._embedding
        self.pe_buf = tt._positional_encoding.pe
        self.layers = tt._decoder.layers
        self.fc = tt._fc

    def forward(self, decoded_tags, memory, cache):
        emb = self.embedding(decoded_tags)
        seq_len = emb.shape[0]
        emb = emb + self.pe_buf[:seq_len, :, :]
        output = emb
        new_cache_layers = []
        for i, mod in enumerate(self.layers):
            out_i = mod(output, memory)
            new_cache_layers.append(out_i)
            output = torch.cat([cache[i], out_i], dim=0)
        new_step_cache = torch.stack(new_cache_layers, dim=0)
        new_cache = torch.cat([cache, new_step_cache], dim=1)
        last_hidden = new_cache_layers[-1]
        logits = self.fc(last_hidden.squeeze(0))
        return logits, last_hidden, new_cache


class BBoxDecoderBatched(nn.Module):
    def __init__(self, full_model: TableModel04_rs):
        super().__init__()
        bd = full_model._bbox_decoder
        self.input_filter = bd._input_filter
        self.attention = bd._attention
        self.init_h = bd._init_h
        self.f_beta = bd._f_beta
        self.class_embed = bd._class_embed
        self.bbox_embed = bd._bbox_embed

    def forward(self, enc_out, tag_H_stacked):
        enc_filt = self.input_filter(enc_out.permute(0, 3, 1, 2)).permute(0, 2, 3, 1)
        encoder_dim = enc_filt.size(3)
        encoder_out = enc_filt.reshape(1, -1, encoder_dim)

        N = tag_H_stacked.shape[0]
        mean_enc = encoder_out.mean(dim=1)
        h_single = self.init_h(mean_enc)
        h_batched = h_single.expand(N, -1)

        cell_tag_H = tag_H_stacked.squeeze(1)
        att1 = self.attention._encoder_att(encoder_out)
        att2 = self.attention._tag_decoder_att(cell_tag_H)
        att3 = self.attention._language_att(h_batched)
        combined = att1 + att2.unsqueeze(1) + att3.unsqueeze(1)
        att_score = self.attention._full_att(self.attention._relu(combined)).squeeze(-1)
        alpha = self.attention._softmax(att_score)
        awe = (encoder_out * alpha.unsqueeze(-1)).sum(dim=1)
        gate = torch.sigmoid(self.f_beta(h_batched))
        awe = gate * awe
        h_cell = awe * h_batched

        outputs_class = self.class_embed(h_cell)
        outputs_coord = torch.sigmoid(self.bbox_embed(h_cell))
        return outputs_class, outputs_coord


# ----- Pipeline ---------------------------------------------------------------

def load_model_for(variant):
    weights_dir = os.path.join(HF_ROOT, variant)
    with open(os.path.join(weights_dir, "tm_config.json")) as f:
        cfg = json.load(f)
    cfg["model"]["save_dir"] = weights_dir
    cfg["predict"]["bbox"] = True
    cfg["predict"]["profiling"] = False
    word_map = cfg["dataset_wordmap"]
    init_data = {"word_map": word_map}
    device = torch.device("cpu")
    model = TableModel04_rs(cfg, init_data, device)
    model.eval()
    weights_fp = os.path.join(weights_dir, f"tableformer_{variant}.safetensors")
    missing, unexpected = load_model(model, weights_fp, device="cpu")
    assert not missing and not unexpected
    return model, cfg


def preprocess(img_path, table_bbox, cfg):
    img = cv2.imread(str(img_path))
    x1, y1, x2, y2 = table_bbox
    crop = img[y1:y2, x1:x2]
    target = cfg["dataset"]["resized_image"]
    crop_resized = cv2.resize(crop, (target, target), interpolation=cv2.INTER_CUBIC)
    crop_rgb = cv2.cvtColor(crop_resized, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    mean = np.array(cfg["dataset"]["image_normalization"]["mean"], dtype=np.float32)
    std = np.array(cfg["dataset"]["image_normalization"]["std"], dtype=np.float32)
    crop_norm = (crop_rgb - mean) / std
    chw = np.transpose(crop_norm, (2, 0, 1))[None, ...]
    return torch.from_numpy(chw).float()


def collect_golden(model, img):
    """Run original PyTorch predict() + capture tag_H_buf."""
    with torch.no_grad():
        seq, out_cls, out_box = model.predict(img, max_steps=1024, k=5)
        enc_out_raw = model._encoder(img)

    word_map = model._init_data["word_map"]["word_map_tag"]
    tt = model._tag_transformer
    encoder_in = tt._input_filter(enc_out_raw.permute(0, 3, 1, 2)).permute(0, 2, 3, 1)
    bs, dim = encoder_in.size(0), encoder_in.size(-1)
    inp = encoder_in.view(bs, -1, dim).permute(1, 0, 2)
    encoder_mask = torch.zeros((bs * tt._n_heads, inp.shape[0], inp.shape[0])) == \
                   torch.ones((bs * tt._n_heads, inp.shape[0], inp.shape[0]))
    with torch.no_grad():
        memory = tt._encoder(inp, mask=encoder_mask)

    decoded_tags = torch.LongTensor([word_map["<start>"]]).unsqueeze(1)
    cache = None
    skip_next_tag = True; prev_tag_ucel = False; line_num = 0; first_lcel = True
    bbox_ind = 0; cur_bbox_ind = -1
    tag_H_buf = []
    for step in range(model._max_pred_len):
        emb = tt._positional_encoding(tt._embedding(decoded_tags))
        with torch.no_grad():
            decoded, cache = tt._decoder(emb, memory, cache=cache)
        logits = tt._fc(decoded[-1, :, :])
        new_tag = int(logits.argmax(1).item())
        if line_num == 0 and new_tag == word_map["xcel"]:
            new_tag = word_map["lcel"]
        if prev_tag_ucel and new_tag == word_map["lcel"]:
            new_tag = word_map["fcel"]
        if new_tag == word_map["<end>"]:
            break
        if not skip_next_tag:
            if new_tag in (word_map["fcel"], word_map["ecel"], word_map["ched"],
                           word_map["rhed"], word_map["srow"], word_map["nl"],
                           word_map["ucel"]):
                tag_H_buf.append(decoded[-1, :, :])
                bbox_ind += 1
        if new_tag != word_map["lcel"]:
            first_lcel = True
        else:
            if first_lcel:
                tag_H_buf.append(decoded[-1, :, :])
                first_lcel = False
                cur_bbox_ind = bbox_ind
                bbox_ind += 1
        if new_tag in (word_map["nl"], word_map["ucel"], word_map["xcel"]):
            skip_next_tag = True
        else:
            skip_next_tag = False
        prev_tag_ucel = (new_tag == word_map["ucel"])
        if new_tag == word_map["nl"]:
            line_num += 1
        decoded_tags = torch.cat(
            [decoded_tags, torch.LongTensor([new_tag]).unsqueeze(1)], dim=0
        )
    return seq, out_cls, out_box, enc_out_raw, memory, tag_H_buf


def export_all_for(variant):
    print(f"\n{'=' * 70}\n[{variant.upper()}] export start\n{'=' * 70}")
    model, cfg = load_model_for(variant)
    out_subdir = os.path.join(OUT, variant)
    os.makedirs(out_subdir, exist_ok=True)
    if variant == "accurate":
        ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
        (ARTIFACT_ROOT / "tm_config.json").write_text(
            json.dumps(cfg, indent=2, default=str),
            encoding="utf-8",
        )

    # Preprocess golden image
    test_img = os.path.join(SRC, "tests", "test_data", "samples", "PHM.2013.page_30.png")
    table_bbox = [100, 186, 1135, 525]
    img_t = preprocess(test_img, table_bbox, cfg)

    # PyTorch reference
    print("[*] Running PyTorch predict() to capture golden...")
    seq_g, cls_g, box_g, enc_out_raw, memory_pt, tag_H_buf_pt = collect_golden(model, img_t)
    print(f"    golden seq length: {len(seq_g)}, bboxes: {cls_g.shape[0]}")

    # ----- Encoder -----
    print("[*] Exporting encoder bundle...")
    enc_w = EncoderBundle(model).eval()
    enc_path = os.path.join(out_subdir, f"tableformer_{variant}_encoder.onnx")
    torch.onnx.export(
        enc_w, (img_t,), enc_path,
        input_names=["image"], output_names=["enc_out", "encoder_out"],
        opset_version=18, do_constant_folding=True, export_params=True, dynamo=True,
    )
    enc_size = os.path.getsize(enc_path)
    enc_data = enc_path + ".data"
    enc_data_size = os.path.getsize(enc_data) if os.path.exists(enc_data) else 0

    # ----- Decoder step -----
    print("[*] Exporting decoder-step...")
    dec_w = DecoderStepWrapper(model).eval()
    num_layers = len(dec_w.layers)
    embed_dim = dec_w.embedding.embedding_dim
    ex_decoded_tags = torch.LongTensor([[2], [5]])
    ex_cache = torch.randn(num_layers, 1, 1, embed_dim)
    seq_dim = torch.export.Dim("seq", min=1, max=1024)
    prev_dim = torch.export.Dim("prev", min=0, max=1024)
    dec_path = os.path.join(out_subdir, f"tableformer_{variant}_decoder_step.onnx")
    torch.onnx.export(
        dec_w, (ex_decoded_tags, memory_pt, ex_cache), dec_path,
        input_names=["decoded_tags", "memory", "cache"],
        output_names=["logits", "last_hidden", "new_cache"],
        dynamic_shapes={
            "decoded_tags": {0: seq_dim},
            "memory": None,
            "cache": {1: prev_dim},
        },
        opset_version=18, do_constant_folding=True, export_params=True, dynamo=True,
    )
    dec_size = os.path.getsize(dec_path)
    dec_data = dec_path + ".data"
    dec_data_size = os.path.getsize(dec_data) if os.path.exists(dec_data) else 0

    # ----- BBox decoder -----
    print("[*] Exporting bbox_decoder...")
    bbox_w = BBoxDecoderBatched(model).eval()
    tag_H_stacked = torch.stack(tag_H_buf_pt, dim=0)
    bbox_path = os.path.join(out_subdir, f"tableformer_{variant}_bbox_decoder.onnx")
    n_dim = torch.export.Dim("N", min=1, max=2048)
    torch.onnx.export(
        bbox_w, (enc_out_raw, tag_H_stacked), bbox_path,
        input_names=["enc_out", "tag_H_stacked"],
        output_names=["outputs_class", "outputs_coord"],
        dynamic_shapes={"enc_out": None, "tag_H_stacked": {0: n_dim}},
        opset_version=18, do_constant_folding=True, export_params=True, dynamo=True,
    )
    bbox_size = os.path.getsize(bbox_path)
    bbox_data = bbox_path + ".data"
    bbox_data_size = os.path.getsize(bbox_data) if os.path.exists(bbox_data) else 0

    # ----- End-to-end ONNX validation -----
    print("[*] Validating end-to-end via ONNX runner...")
    so = ort.SessionOptions()
    so.intra_op_num_threads = 2
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    sess_enc = ort.InferenceSession(enc_path, sess_options=so, providers=["CPUExecutionProvider"])
    sess_dec = ort.InferenceSession(dec_path, sess_options=so, providers=["CPUExecutionProvider"])
    sess_bbox = ort.InferenceSession(bbox_path, sess_options=so, providers=["CPUExecutionProvider"])

    img_np = img_t.numpy()
    t0 = time.time()
    enc_out_np, memory_np = sess_enc.run(None, {"image": img_np})
    t_enc = time.time() - t0

    word_map = cfg["dataset_wordmap"]["word_map_tag"]
    decoded_tags = np.array([[word_map["<start>"]]], dtype=np.int64)
    cache = np.zeros((num_layers, 0, 1, embed_dim), dtype=np.float32)
    output_tags = []
    skip_next_tag = True; prev_tag_ucel = False; line_num = 0; first_lcel = True
    bbox_ind = 0; cur_bbox_ind = -1
    tag_H_buf_o = []
    t1 = time.time()
    for step in range(1024):
        logits, last_hidden, cache = sess_dec.run(
            None, {"decoded_tags": decoded_tags, "memory": memory_np, "cache": cache}
        )
        new_tag = int(np.argmax(logits, axis=1)[0])
        if line_num == 0 and new_tag == word_map["xcel"]:
            new_tag = word_map["lcel"]
        if prev_tag_ucel and new_tag == word_map["lcel"]:
            new_tag = word_map["fcel"]
        if new_tag == word_map["<end>"]:
            output_tags.append(new_tag)
            decoded_tags = np.concatenate(
                [decoded_tags, np.array([[new_tag]], dtype=np.int64)], axis=0
            )
            break
        output_tags.append(new_tag)
        if not skip_next_tag:
            if new_tag in (word_map["fcel"], word_map["ecel"], word_map["ched"],
                           word_map["rhed"], word_map["srow"], word_map["nl"],
                           word_map["ucel"]):
                tag_H_buf_o.append(last_hidden[:, 0, :].copy())
                bbox_ind += 1
        if new_tag != word_map["lcel"]:
            first_lcel = True
        else:
            if first_lcel:
                tag_H_buf_o.append(last_hidden[:, 0, :].copy())
                first_lcel = False
                cur_bbox_ind = bbox_ind
                bbox_ind += 1
        if new_tag in (word_map["nl"], word_map["ucel"], word_map["xcel"]):
            skip_next_tag = True
        else:
            skip_next_tag = False
        prev_tag_ucel = (new_tag == word_map["ucel"])
        if new_tag == word_map["nl"]:
            line_num += 1
        decoded_tags = np.concatenate(
            [decoded_tags, np.array([[new_tag]], dtype=np.int64)], axis=0
        )
    t_dec = time.time() - t1
    seq_o = decoded_tags.squeeze().tolist()

    if tag_H_buf_o:
        tag_H_stacked_o = np.stack(
            [h.reshape(1, -1) for h in tag_H_buf_o], axis=0
        ).astype(np.float32)
    else:
        tag_H_stacked_o = np.zeros((0, 1, embed_dim), dtype=np.float32)
    t2 = time.time()
    cls_o, box_o = sess_bbox.run(None, {
        "enc_out": enc_out_np, "tag_H_stacked": tag_H_stacked_o
    })
    t_bbox = time.time() - t2

    # Compare
    cls_g_np = cls_g.numpy(); box_g_np = box_g.numpy()
    seq_eq = (seq_o == seq_g)
    diff_cls = float(np.abs(cls_o - cls_g_np).max()) if cls_o.shape == cls_g_np.shape else float("nan")
    diff_box = float(np.abs(box_o - box_g_np).max()) if box_o.shape == box_g_np.shape else float("nan")
    print(f"  RESULT [{variant}]:")
    print(f"    seq tokens equal: {seq_eq} ({len(seq_o)} vs {len(seq_g)})")
    print(f"    outputs_class max abs diff: {diff_cls:.3e}")
    print(f"    outputs_coord max abs diff: {diff_box:.3e}")
    print(f"    timings: enc={t_enc:.3f}s dec={t_dec:.3f}s bbox={t_bbox:.3f}s "
          f"total={t_enc + t_dec + t_bbox:.3f}s")
    print(f"    encoder size:    {enc_size/1024:.1f} KB graph + {enc_data_size/1024/1024:.2f} MB external")
    print(f"    decoder size:    {dec_size/1024:.1f} KB graph + {dec_data_size/1024/1024:.2f} MB external")
    print(f"    bbox dec size:   {bbox_size/1024:.1f} KB graph + {bbox_data_size/1024/1024:.2f} MB external")
    return {
        "variant": variant,
        "seq_equal": seq_eq,
        "diff_cls": diff_cls,
        "diff_box": diff_box,
        "timings": {"enc": t_enc, "dec": t_dec, "bbox": t_bbox,
                    "total": t_enc + t_dec + t_bbox},
        "sizes_mb": {
            "encoder": (enc_size + enc_data_size) / 1024 / 1024,
            "decoder": (dec_size + dec_data_size) / 1024 / 1024,
            "bbox": (bbox_size + bbox_data_size) / 1024 / 1024,
        },
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("variants", nargs="*", default=["fast", "accurate"])
    args = parser.parse_args()
    results = []
    for v in args.variants:
        results.append(export_all_for(v))
    print("\n" + "=" * 70 + "\nSUMMARY\n" + "=" * 70)
    for r in results:
        print(f"  {r['variant']:10s}  seq_eq={r['seq_equal']}  cls_diff={r['diff_cls']:.2e}  box_diff={r['diff_box']:.2e}  total_size={sum(r['sizes_mb'].values()):.1f} MB  total_time={r['timings']['total']:.2f}s")


if __name__ == "__main__":
    main()
