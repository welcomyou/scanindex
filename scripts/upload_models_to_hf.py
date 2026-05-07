"""
Upload ScanIndex models to Hugging Face Hub.

Architecture:
  - 7 standalone repos for individual models (max discoverability)
  - 1 bundle repo `welcomyou/scanindex-models` (orientation + manifest)
  - 1 collection `welcomyou/scanindex` grouping all 8 + upstream lineage

Each HF repo mirrors the local `models/<dirname>/` layout so a plain
`snapshot_download(repo_id, local_dir="models")` reconstructs the tree.

Usage:
  pip install -U huggingface_hub
  huggingface-cli login        # token saved to ~/.cache/huggingface/token

  # Dry run (no network):
  python scripts/upload_models_to_hf.py --dry-run

  # Real upload:
  python scripts/upload_models_to_hf.py

  # Upload only one repo:
  python scripts/upload_models_to_hf.py --only welcomyou/layoutlmv3-vn-admin-kie

  # Skip collection step (e.g. if you'll create it manually):
  python scripts/upload_models_to_hf.py --skip-collection
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = ROOT / "models"

USER = "welcomyou"
COLLECTION_TITLE = "ScanIndex"
# HF caps collection description at 150 chars
COLLECTION_DESCRIPTION = (
    "Models loaded by https://github.com/welcomyou/scanindex — "
    "OCR, KIE, layout, tables, embedder for Vietnamese admin docs."
)


# ────────────────────────────────────────────────────────────────────
# Per-repo definitions
# ────────────────────────────────────────────────────────────────────
@dataclass
class RepoSpec:
    repo_id: str
    sources: List[str]          # paths under models/, uploaded preserving structure
    readme: str
    pipeline_tag: Optional[str] = None
    license: str = "other"
    base_models: List[str] = field(default_factory=list)
    extra_tags: List[str] = field(default_factory=list)


def _readme(frontmatter: dict, body: str) -> str:
    head = "---\n"
    for k, v in frontmatter.items():
        if v is None:
            continue
        if isinstance(v, list):
            head += f"{k}:\n"
            for item in v:
                head += f"  - {item}\n"
        else:
            head += f"{k}: {v}\n"
    head += "---\n\n"
    return head + body.strip() + "\n"


# ── 1. LayoutLMv3 fine-tune for Vietnamese admin KIE ────────────────
LAYOUTLMV3_README = _readme(
    {
        "library_name": "transformers",
        "pipeline_tag": "token-classification",
        "license": "cc-by-nc-sa-4.0",
        "base_model": "microsoft/layoutlmv3-base",
        "language": ["vi"],
        "tags": [
            "layoutlmv3", "kie", "key-information-extraction",
            "document-understanding", "vietnamese", "onnx", "int8",
        ],
    },
    """
# LayoutLMv3 — Vietnamese administrative document KIE

Fine-tuned [`microsoft/layoutlmv3-base`](https://huggingface.co/microsoft/layoutlmv3-base) for key-information extraction on Vietnamese administrative documents (Quyết định, Công văn, Tờ trình, Báo cáo, ...).

The variant is the **fontgray-norm** flavour: `token_type_ids` encode three style buckets derived from per-word font size, foreground gray level, and word height (see `style_emphasis_ids` in the training pipeline).

## Files

- `layoutlmv3_fontgray_norm_final_epoch25/layoutlmv3_fontgray_norm_final_epoch25.int8.onnx` — quantized INT8 ONNX model
- `layoutlmv3_fontgray_norm_final_epoch25/label_list.json` — label vocabulary (BIO tags)
- `layoutlmv3_fontgray_norm_final_epoch25/layoutlmv3_fontgray_config.json` — runtime config (style buckets, line position buckets)
- Tokenizer files (`tokenizer.json`, `tokenizer_config.json`, `special_tokens_map.json`, `vocab.txt`, …)

## Intended use

This model is designed for the [ScanIndex](https://github.com/welcomyou/scanindex) pipeline. It expects the canonical OCR JSON profile produced by ScreenAI + the project's preprocessing (`layoutlmv3_runtime_v1`). Using it standalone requires reproducing that input format.

## Loading

```python
from huggingface_hub import snapshot_download
local = snapshot_download("welcomyou/layoutlmv3-vn-admin-kie", local_dir="models")
# Then point ScanIndex at <repo>/models/layoutlmv3_fontgray_norm_final_epoch25/
```

## Training & data

See [train-convert/kie/train_kie/layoutlmv3_fontgray_norm/](https://github.com/welcomyou/scanindex/tree/main/train-convert/kie/train_kie/layoutlmv3_fontgray_norm) for the training scripts and decision records.

Trained on internal annotated Vietnamese admin documents (not redistributed).

## License

Inherits LayoutLMv3 base license: **CC-BY-NC-SA-4.0** (research / non-commercial). Commercial use requires a separate agreement with Microsoft for the base model.
""",
)

# ── 2. E5-small mix50 v2 ONNX fp32 ──────────────────────────────────
E5_README = _readme(
    {
        "library_name": "sentence-transformers",
        "pipeline_tag": "sentence-similarity",
        "license": "mit",
        "base_model": "intfloat/multilingual-e5-small",
        "language": ["vi", "en"],
        "tags": [
            "sentence-similarity", "sentence-transformers", "e5",
            "vietnamese", "onnx", "fp32", "retrieval", "document-search",
        ],
    },
    """
# E5-small mix50 v2 — Vietnamese archive embedder

Fine-tuned [`intfloat/multilingual-e5-small`](https://huggingface.co/intfloat/multilingual-e5-small) for retrieval on Vietnamese archived administrative documents. Trained on a 50/50 mix of (a) in-domain Vietnamese corpus and (b) general retrieval pairs, exported to ONNX fp32.

Used as the dense passage encoder in the [ScanIndex](https://github.com/welcomyou/scanindex) hybrid search (Tantivy BM25 + FAISS HNSW + RRF fusion).

## Files

- `archive_models/e5-small-mix50-v2-onnx-fp32/model.onnx` (+ `model.onnx_data`)
- Tokenizer + sentence-transformers metadata (`config.json`, `tokenizer.json`, `sentencepiece.bpe.model`, `1_Pooling/`, `modules.json`, …)

## Asymmetric input

E5 requires query/passage prefixes:

```python
queries  = [f"query: {q}" for q in raw_queries]
passages = [f"passage: {p}" for p in raw_passages]
```

## Loading (ONNX)

```python
import onnxruntime as ort
from transformers import AutoTokenizer
from huggingface_hub import snapshot_download

local = snapshot_download("welcomyou/e5-small-vn-archive-mix50", local_dir="models")
sub = f"{local}/archive_models/e5-small-mix50-v2-onnx-fp32"
tok = AutoTokenizer.from_pretrained(sub)
sess = ort.InferenceSession(f"{sub}/model.onnx")
```

## Training

See [train-convert/archive-embedder/train/mix50_v2/](https://github.com/welcomyou/scanindex/tree/main/train-convert/archive-embedder/train/mix50_v2).

## License

MIT, inherited from `intfloat/multilingual-e5-small`.
""",
)

# ── 3. distilled-protonx CT2 (Vietnamese OCR text correction) ───────
PROTONX_CT2_README = _readme(
    {
        "library_name": "ctranslate2",
        "pipeline_tag": "translation",
        "license": "apache-2.0",
        "base_model": "protonx-models/distilled-protonx-legal-tc",
        "language": ["vi"],
        "tags": [
            "ctranslate2", "ct2", "text2text-generation",
            "vietnamese", "ocr-correction", "post-ocr",
        ],
    },
    """
# distilled-protonx-legal-tc — CTranslate2 build (Vietnamese OCR text correction)

CTranslate2-converted version of [`protonx-models/distilled-protonx-legal-tc`](https://huggingface.co/protonx-models/distilled-protonx-legal-tc), optimised for fast CPU OCR text correction on Vietnamese administrative documents.

The upstream `distilled-protonx-legal-tc` is a smaller student distilled from `protonx-legal-tc`. This repo only does the CT2 conversion + INT8 quantization — no further training. Used by the [ScanIndex](https://github.com/welcomyou/scanindex) pipeline as the correction stage between OCR and PDF/DOCX export.

## Performance (ScanIndex internal benchmark, 13-page Vietnamese admin doc)

| Variant | Time | Accuracy |
|---|---|---|
| `protonx-legal-tc` — CT2 OPTIMIZE, beam=1 | 14.5s | 99.561% |
| **This repo** (`distilled-protonx-legal-tc` CT2 INT8, beam=1) | **8.3s** | **99.550%** |

42% faster, 0.011 pp accuracy drop — recommended trade-off for CPU.

## Files

- `distilled_ct2/model.bin` — CTranslate2 model
- `distilled_ct2/tokenizer.json`, `tokenizer_config.json`, `special_tokens_map.json`, `shared_vocabulary.json`, `config.json`

## Loading

```python
import ctranslate2
from transformers import AutoTokenizer
from huggingface_hub import snapshot_download

local = snapshot_download("welcomyou/distilled-protonx-vn-correction-ct2", local_dir="models")
sub = f"{local}/distilled_ct2"
translator = ctranslate2.Translator(sub, device="cpu")
tok = AutoTokenizer.from_pretrained(sub)
```

## License

Apache-2.0, inheriting from the protonx-legal-tc base.
""",
)

# ── 4. LightGBM page splitter (doc_start + signer_page) ─────────────
LIGHTGBM_README = _readme(
    {
        "library_name": "lightgbm",
        "pipeline_tag": "tabular-classification",
        "license": "mit",
        "language": ["vi"],
        "tags": [
            "lightgbm", "classification", "vietnamese",
            "document-segmentation", "page-splitting",
        ],
    },
    """
# LightGBM page splitter — Vietnamese admin batches

Two LightGBM Booster models used by [ScanIndex](https://github.com/welcomyou/scanindex) to split a multi-document scan batch into individual document boundaries:

| Model | Task |
|---|---|
| `lightgbm_splitter/doc_start/model.txt` | Binary: is this page the **start** of a new document? |
| `lightgbm_splitter/signer_page/model.txt` | Per-document: which page contains the signer block? |

## Files

- `lightgbm_splitter/doc_start/model.txt` — LightGBM Booster (text format, portable)
- `lightgbm_splitter/doc_start/model.joblib` — sklearn wrapper (optional)
- `lightgbm_splitter/signer_page/model.txt`
- `lightgbm_splitter/signer_page/model.joblib`

## Features

Page-level features extracted from canonical OCR JSON: header/footer signals, regime presence, signer-block markers, page index, relative position, etc. See `build_doc_start_features` and the `predict_*` helpers in [scanindex/core/digitization/page_splitter.py](https://github.com/welcomyou/scanindex/blob/main/scanindex/core/digitization/page_splitter.py).

## Loading

```python
import lightgbm as lgb
from huggingface_hub import snapshot_download
local = snapshot_download("welcomyou/lightgbm-vn-page-splitter", local_dir="models")
booster = lgb.Booster(model_file=f"{local}/lightgbm_splitter/doc_start/model.txt")
```

## License

MIT.
""",
)

# ── 5. DocLayout-YOLO ONNX dynamic-axes re-export ───────────────────
DOCLAYOUT_README = _readme(
    {
        "library_name": "onnxruntime",
        "pipeline_tag": "object-detection",
        "license": "agpl-3.0",
        "base_model": "juliozhao/DocLayout-YOLO-DocStructBench",
        "tags": [
            "yolo", "doclayout-yolo", "object-detection",
            "document-layout-analysis", "onnx", "dynamic-axes",
        ],
    },
    """
# DocLayout-YOLO — ONNX with dynamic axes

Re-export of [`juliozhao/DocLayout-YOLO-DocStructBench`](https://huggingface.co/juliozhao/DocLayout-YOLO-DocStructBench) and the DocLayNet-pretrained variant to **ONNX with dynamic batch + spatial dimensions**, which the upstream releases do not provide.

## Why dynamic axes?

The official DocLayout-YOLO ONNX exports use fixed input shapes (e.g. 1024×1024). Dynamic axes let downstream tools batch arbitrary page sizes without re-exporting per resolution — convenient for desktop OCR pipelines that hit pages of mixed DPI.

## Variants

| Subdir | Source | Use case |
|---|---|---|
| `doclayout_yolo_onnx_dynamic/` | DocStructBench (academic + business mix) | Primary — used by ScanIndex `layout_analyzer` |
| `doclayout_yolo_doclaynet_onnx_dynamic/` | DocLayNet (annotated diverse docs) | Auxiliary for non-table region routing |

Each subdir contains the `.onnx` + `.onnx.data` (external weights) + a `.names.json` for class id → label mapping.

## Loading

```python
import onnxruntime as ort
from huggingface_hub import snapshot_download
local = snapshot_download("welcomyou/doclayout-yolo-onnx-dynamic", local_dir="models")
sess = ort.InferenceSession(f"{local}/doclayout_yolo_onnx_dynamic/doclayout_yolo_docstructbench_imgsz1024_dynamic.onnx")
# input "images": (N, 3, H, W) where H, W must be multiples of 32
```

## Re-export reproduction

See [train-convert/doclayoutyolo/convert/export_doclayout_yolo_to_onnx.py](https://github.com/welcomyou/scanindex/blob/main/train-convert/doclayoutyolo/convert/export_doclayout_yolo_to_onnx.py).

## License

**AGPL-3.0**, inherited from upstream DocLayout-YOLO. Commercial use requires complying with AGPL terms or obtaining an alternative license from the authors.
""",
)

# ── 6. GMFT TATR ONNX ───────────────────────────────────────────────
GMFT_README = _readme(
    {
        "library_name": "onnxruntime",
        "pipeline_tag": "object-detection",
        "license": "mit",
        "base_model": "microsoft/table-transformer-detection",
        "tags": [
            "table-detection", "table-structure-recognition",
            "tatr", "table-transformer", "onnx", "gmft",
        ],
    },
    """
# Microsoft Table Transformer (TATR) — ONNX re-export

Re-export of Microsoft's Table Transformer to ONNX, packaged the way [GMFT](https://github.com/conjuncts/gmft) consumes it. Used by [ScanIndex](https://github.com/welcomyou/scanindex) for table detection + structure recognition during DOCX export.

## Variants

| Subdir | Upstream | Task |
|---|---|---|
| `gmft_onnx/detection/model.onnx` | [`microsoft/table-transformer-detection`](https://huggingface.co/microsoft/table-transformer-detection) | Detect table bounding boxes on a page |
| `gmft_onnx/structure/model.onnx` | [`microsoft/table-transformer-structure-recognition-v1.1-all`](https://huggingface.co/microsoft/table-transformer-structure-recognition-v1.1-all) | Detect rows / columns / cells inside a cropped table |

Each subdir also contains the HF `config.json` + preprocessor metadata so `transformers` / `optimum` can wrap the ONNX directly.

## Loading

```python
from huggingface_hub import snapshot_download
local = snapshot_download("welcomyou/gmft-tatr-onnx", local_dir="models")
# Detection:  f"{local}/gmft_onnx/detection/model.onnx"
# Structure:  f"{local}/gmft_onnx/structure/model.onnx"
```

## Re-export reproduction

See [train-convert/gmft/convert/export_gmft_tatr_to_onnx.py](https://github.com/welcomyou/scanindex/blob/main/train-convert/gmft/convert/export_gmft_tatr_to_onnx.py).

## License

MIT, inherited from Microsoft Table Transformer.
""",
)

# ── 7. Docling TableFormer v1 stepcache ONNX ────────────────────────
DOCLING_README = _readme(
    {
        "library_name": "onnxruntime",
        "pipeline_tag": "object-detection",
        "license": "mit",
        "base_model": "ds4sd/docling-models",
        "tags": [
            "table-structure-recognition", "tableformer", "docling",
            "onnx", "stepcache", "kv-cache",
        ],
    },
    """
# Docling TableFormer v1 — ONNX stepcache export

ONNX export of [Docling](https://github.com/DS4SD/docling)'s TableFormer v1 structure recognizer, **split into encoder + step-cached decoder + bbox-head sub-graphs** so the autoregressive decoder can be run one step at a time with a KV-cache from Python — without pulling in the Docling runtime.

## Why stepcache?

Docling's stock decoder runs the full sequence per call. For desktop CPU inference you want to cache K/V across decoder steps to amortize cost. This export materializes that pattern at the ONNX level so onnxruntime (or any ONNX runtime) handles it without custom Docling code.

## Files (`docling_tableformer_v1_stepcache_onnx/`)

| File | Role |
|---|---|
| `docling_v1_encoder.onnx` | Encodes the cropped table image once |
| `docling_v1_decoder_step.onnx` | One decoder step; consumes encoder features + previous KV |
| `docling_v1_bbox_head.onnx` | Maps decoder hidden states to per-cell bboxes |
| `vocab.json`, `tableformer_config.json` | Tokenizer + model config |

## Loading

```python
import onnxruntime as ort
from huggingface_hub import snapshot_download
local = snapshot_download("welcomyou/docling-tableformer-v1-onnx-stepcache", local_dir="models")
sub = f"{local}/docling_tableformer_v1_stepcache_onnx"
encoder = ort.InferenceSession(f"{sub}/docling_v1_encoder.onnx")
decoder = ort.InferenceSession(f"{sub}/docling_v1_decoder_step.onnx")
bbox    = ort.InferenceSession(f"{sub}/docling_v1_bbox_head.onnx")
```

A reference Python loop that ties these three sessions into a stepcache decoder lives at [train-convert/docling-tableformer-v1/convert/onnx_stepcache_runner_reference.py](https://github.com/welcomyou/scanindex/blob/main/train-convert/docling-tableformer-v1/convert/onnx_stepcache_runner_reference.py).

## Re-export reproduction

See [train-convert/docling-tableformer-v1/convert/export_docling_v1_tableformer_stepcache_onnx.py](https://github.com/welcomyou/scanindex/blob/main/train-convert/docling-tableformer-v1/convert/export_docling_v1_tableformer_stepcache_onnx.py).

## License

MIT, inherited from Docling.
""",
)

# ── Bundle: orientation classifier + manifest ───────────────────────
def _bundle_readme(standalone: List["RepoSpec"]) -> str:
    rows = "\n".join(
        f"| `{r.repo_id}` | [{r.repo_id.split('/')[-1]}](https://huggingface.co/{r.repo_id}) |"
        for r in standalone
    )
    return _readme(
        {
            "library_name": "onnxruntime",
            "license": "apache-2.0",
            "language": ["vi"],
            "tags": ["scanindex", "ocr", "kie", "vietnamese", "model-bundle"],
        },
        f"""
# ScanIndex — runtime model bundle

Small companion repo for [ScanIndex](https://github.com/welcomyou/scanindex). Contains:

- `orientation/PP-LCNet_x1_0_doc_ori.onnx` — PaddleOCR's 4-way page-orientation classifier (Apache-2.0; tiny, redistributed for offline-install convenience)
- `manifest.json` — list of standalone model repos that complete the runtime

The actual model weights live in the standalone repos below. Download all of them at once with `scripts/download_offline_models.py` in the GitHub repo.

## Standalone model repos

| HF repo | Link |
|---|---|
{rows}

## Not included (fetched at runtime from upstream)

- **Chrome ScreenAI OCR** — `scanindex.core.ocr.screen_ai_downloader` pulls directly from Google CDN to honor the Chrome license.
- **`BAAI/bge-reranker-v2-m3`** — `sentence_transformers` pulls upstream on first use of the Accurate search mode.

## See also

[`welcomyou/scanindex` collection](https://huggingface.co/collections/welcomyou/scanindex) groups these models with their upstream lineage.
""",
    )


STANDALONE: List[RepoSpec] = [
    RepoSpec(
        repo_id=f"{USER}/layoutlmv3-vn-admin-kie",
        sources=["layoutlmv3_fontgray_norm_final_epoch25"],
        readme=LAYOUTLMV3_README,
    ),
    RepoSpec(
        repo_id=f"{USER}/e5-small-vn-archive-mix50",
        sources=["archive_models/e5-small-mix50-v2-onnx-fp32"],
        readme=E5_README,
    ),
    RepoSpec(
        repo_id=f"{USER}/distilled-protonx-vn-correction-ct2",
        sources=["distilled_ct2"],
        readme=PROTONX_CT2_README,
    ),
    RepoSpec(
        repo_id=f"{USER}/lightgbm-vn-page-splitter",
        sources=["lightgbm_splitter"],
        readme=LIGHTGBM_README,
    ),
    RepoSpec(
        repo_id=f"{USER}/doclayout-yolo-onnx-dynamic",
        sources=["doclayout_yolo_onnx_dynamic", "doclayout_yolo_doclaynet_onnx_dynamic"],
        readme=DOCLAYOUT_README,
    ),
    RepoSpec(
        repo_id=f"{USER}/gmft-tatr-onnx",
        sources=["gmft_onnx"],
        readme=GMFT_README,
    ),
    RepoSpec(
        repo_id=f"{USER}/docling-tableformer-v1-onnx-stepcache",
        sources=["docling_tableformer_v1_stepcache_onnx"],
        readme=DOCLING_README,
    ),
]

BUNDLE_REPO = f"{USER}/scanindex-models"
BUNDLE_SOURCES = ["orientation"]


# Upstream lineage to also pin in the Collection (extra context for visitors)
UPSTREAM_LINEAGE = [
    "microsoft/layoutlmv3-base",
    "intfloat/multilingual-e5-small",
    "protonx-models/distilled-protonx-legal-tc",
    "juliozhao/DocLayout-YOLO-DocStructBench",
    "microsoft/table-transformer-detection",
    "microsoft/table-transformer-structure-recognition-v1.1-all",
    "BAAI/bge-reranker-v2-m3",
]


# ────────────────────────────────────────────────────────────────────
# Upload logic
# ────────────────────────────────────────────────────────────────────
IGNORE_PATTERNS = [
    "*.pyc", "__pycache__/*", ".cache/*", ".locks/*",
    "*.tmp", "*.partial", "*.lock",
    # Training-only artifacts — runtime uses ONNX/CT2 model.bin/LightGBM .txt
    # so these are unnecessary in published repos
    "*.safetensors", "*.pt", "*.ckpt",
    "optimizer.pt", "scheduler.pt", "training_args.bin",
    "rng_state.pth", "trainer_state.json",
]


def _check_sources(sources: List[str]) -> List[Path]:
    missing, ok = [], []
    for s in sources:
        p = MODELS_DIR / s
        (ok if p.exists() else missing).append(p)
    if missing:
        for p in missing:
            print(f"  MISSING: {p}")
    return ok


def _is_ignored(rel_path: str) -> bool:
    import fnmatch
    parts = rel_path.replace("\\", "/").split("/")
    for pat in IGNORE_PATTERNS:
        if "/" in pat:
            if fnmatch.fnmatch(rel_path.replace("\\", "/"), pat):
                return True
        else:
            if any(fnmatch.fnmatch(part, pat) for part in parts):
                return True
    return False


def _effective_size_mb(roots: List[Path]) -> float:
    total = 0
    for root in roots:
        for f in root.rglob("*"):
            if not f.is_file():
                continue
            rel = f.relative_to(root).as_posix()
            if _is_ignored(rel):
                continue
            total += f.stat().st_size
    return total / 1e6


def _push_repo(api, spec: RepoSpec, dry_run: bool, log: Callable,
               readmes_only: bool = False) -> bool:
    log(f"\n=== {spec.repo_id} ===")
    sources = _check_sources(spec.sources)
    if not sources:
        log("  (no sources on disk — skipping)")
        return False

    if not readmes_only:
        total_mb = _effective_size_mb(sources)
        for p in sources:
            log(f"  source: {p.relative_to(MODELS_DIR)}/")
        log(f"  size:   {total_mb:.1f} MB (after ignore patterns)")

    if dry_run:
        log("  (dry-run)")
        return True

    from huggingface_hub import create_repo
    create_repo(spec.repo_id, repo_type="model", exist_ok=True, private=False)

    readme_tmp = ROOT / f".hf_readme_{spec.repo_id.replace('/', '__')}.tmp.md"
    readme_tmp.write_text(spec.readme, encoding="utf-8")
    try:
        api.upload_file(
            path_or_fileobj=str(readme_tmp),
            path_in_repo="README.md",
            repo_id=spec.repo_id, repo_type="model",
            commit_message="docs: model card",
        )
    finally:
        readme_tmp.unlink(missing_ok=True)

    if readmes_only:
        log("  README updated (folder upload skipped)")
        return True

    for src in sources:
        api.upload_folder(
            folder_path=str(src),
            path_in_repo=src.name,
            repo_id=spec.repo_id, repo_type="model",
            commit_message=f"upload {src.name}",
            ignore_patterns=IGNORE_PATTERNS,
        )
    log(f"  -> https://huggingface.co/{spec.repo_id}")
    return True


def _push_bundle(api, dry_run: bool, log: Callable) -> bool:
    log(f"\n=== {BUNDLE_REPO} (bundle) ===")
    sources = _check_sources(BUNDLE_SOURCES)

    manifest = {
        "schema_version": 1,
        "github_repo": "https://github.com/welcomyou/scanindex",
        "standalone_repos": [
            {"repo_id": s.repo_id, "sources": s.sources}
            for s in STANDALONE
        ],
        "bundle_paths": [str(p.name) for p in sources],
        "external_models": {
            "screen_ai":  "Google CDN — fetched by scanindex.core.ocr.screen_ai_downloader",
            "reranker":   "BAAI/bge-reranker-v2-m3 — fetched by sentence_transformers",
        },
    }
    manifest_path = ROOT / ".hf_manifest.tmp.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    readme_path = ROOT / ".hf_readme_bundle.tmp.md"
    readme_path.write_text(_bundle_readme(STANDALONE), encoding="utf-8")

    try:
        if dry_run:
            log("  (dry-run)")
            log(f"  manifest preview: {json.dumps(manifest, indent=2)[:300]}…")
            return True

        from huggingface_hub import create_repo
        create_repo(BUNDLE_REPO, repo_type="model", exist_ok=True, private=False)
        api.upload_file(
            path_or_fileobj=str(readme_path), path_in_repo="README.md",
            repo_id=BUNDLE_REPO, repo_type="model",
            commit_message="docs: bundle README",
        )
        api.upload_file(
            path_or_fileobj=str(manifest_path), path_in_repo="manifest.json",
            repo_id=BUNDLE_REPO, repo_type="model",
            commit_message="manifest: standalone repo list",
        )
        for src in sources:
            api.upload_folder(
                folder_path=str(src), path_in_repo=src.name,
                repo_id=BUNDLE_REPO, repo_type="model",
                commit_message=f"upload {src.name}",
                ignore_patterns=IGNORE_PATTERNS,
            )
        log(f"  -> https://huggingface.co/{BUNDLE_REPO}")
        return True
    finally:
        manifest_path.unlink(missing_ok=True)
        readme_path.unlink(missing_ok=True)


def _build_collection(dry_run: bool, log: Callable) -> bool:
    log(f"\n=== Collection {USER}/{COLLECTION_TITLE} ===")
    if dry_run:
        log("  (dry-run)")
        return True
    try:
        from huggingface_hub import create_collection, add_collection_item
    except ImportError:
        log("  -> SKIP (need huggingface_hub>=0.21)")
        return False
    try:
        coll = create_collection(
            title=COLLECTION_TITLE, namespace=USER,
            description=COLLECTION_DESCRIPTION, exists_ok=True,
        )
        slug = coll.slug
        items = [(s.repo_id, "model") for s in STANDALONE]
        items.append((BUNDLE_REPO, "model"))
        items.extend((m, "model") for m in UPSTREAM_LINEAGE)
        for repo_id, item_type in items:
            try:
                add_collection_item(slug, repo_id, item_type, exists_ok=True)
                log(f"  + {repo_id}")
            except Exception as e:
                log(f"  ! {repo_id} — {e}")
        log(f"  -> https://huggingface.co/collections/{slug}")
        return True
    except Exception as e:
        log(f"  -> ERROR: {e}")
        return False


def _safe_print(msg: str) -> None:
    """Console print that survives non-cp1252 chars on Windows."""
    try:
        print(msg)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "utf-8"
        print(msg.encode(enc, errors="replace").decode(enc, errors="replace"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--only", default=None,
                        help="Upload only this repo_id (skips bundle + collection)")
    parser.add_argument("--skip-bundle", action="store_true")
    parser.add_argument("--skip-collection", action="store_true")
    parser.add_argument("--collection-only", action="store_true",
                        help="Only build/update the Collection (skip all uploads)")
    parser.add_argument("--readmes-only", action="store_true",
                        help="Re-upload only README.md for each repo (skip model files)")
    args = parser.parse_args()

    try:
        from huggingface_hub import HfApi
    except ImportError:
        print("ERROR: pip install -U huggingface_hub", file=sys.stderr)
        return 1

    if not MODELS_DIR.is_dir():
        print(f"ERROR: {MODELS_DIR} not found", file=sys.stderr)
        return 1

    api = HfApi() if not args.dry_run else None
    log = _safe_print

    if args.collection_only:
        _build_collection(args.dry_run, log)
        return 0

    targets = STANDALONE
    if args.only:
        targets = [s for s in STANDALONE if s.repo_id == args.only]
        if not targets:
            print(f"ERROR: --only {args.only!r} not in STANDALONE list", file=sys.stderr)
            return 1

    ok_count = 0
    for spec in targets:
        ok_count += int(_push_repo(api, spec, args.dry_run, log,
                                   readmes_only=args.readmes_only))

    if not args.only and not args.skip_bundle:
        _push_bundle(api, args.dry_run, log)

    if not args.only and not args.skip_collection:
        _build_collection(args.dry_run, log)

    log(f"\nDone. {ok_count}/{len(targets)} standalone repos pushed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
