from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from train_lightgbm.common import read_json, write_json
from train_lightgbm.dataset import _build_features, generate_candidates, load_ocr_document
from train_lightgbm.schema_decoder import CandidatePrediction, decode_document_predictions, link_signers
from train_lightgbm.training import load_models, score_rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run field-wise LightGBM inference on one manifest-style doc.")
    parser.add_argument("--project-root", required=True, help="LightGBM project root.")
    parser.add_argument("--doc-meta-json", required=True, help="JSON file containing doc metadata with source_canonical_json and selected_pages.")
    parser.add_argument("--output-json", required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    project_root = Path(args.project_root)
    models_dir = project_root / "models" / "fieldwise"
    thresholds = read_json(models_dir / "thresholds.json", default={})
    models = load_models(models_dir)
    doc_meta = read_json(args.doc_meta_json)
    doc = load_ocr_document(doc_meta)
    decoded_input = {}
    for field, model in models.items():
        meta = read_json(models_dir / f"{field}.meta.json")
        candidates = generate_candidates(doc, field)
        rows = []
        for cand in candidates:
            page = doc.pages[cand.page_index]
            features = _build_features(
                cand.field,
                cand.source_kind,
                page,
                cand.page_role,
                cand.line_ids,
                cand.word_ids,
                cand.bbox,
                cand.text,
                cand.normalized_text,
                doc,
            )
            rows.append({"features": features, "target": 0})
        scores = score_rows(model, rows, meta["feature_names"])
        decoded_input[field] = [
            CandidatePrediction(
                field=field,
                score=score,
                page_index=cand.page_index,
                line_ids=list(cand.line_ids),
                word_ids=list(cand.word_ids),
                bbox=list(cand.bbox),
                text=cand.text,
                candidate_id=cand.candidate_id,
            )
            for cand, score in zip(candidates, scores)
        ]
    decoded = decode_document_predictions(decoded_input, thresholds)
    payload = {
        "field_instances": [
            {
                "field_id": pred.candidate_id,
                "label": field,
                "page_index": pred.page_index,
                "line_ids": pred.line_ids,
                "word_ids": pred.word_ids,
                "bbox": pred.bbox,
                "text": pred.text,
                "confidence": pred.score,
            }
            for field, preds in decoded.items()
            for pred in preds
        ],
        "relations": link_signers(decoded),
    }
    write_json(args.output_json, payload)
    print(json.dumps({"field_count": len(payload["field_instances"]), "relation_count": len(payload["relations"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
