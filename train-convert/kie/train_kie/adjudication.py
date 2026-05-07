from __future__ import annotations

import copy
import unicodedata
from collections import defaultdict

from kie_json_utils import upgrade_ocr_data_in_place
from train_kie.common import read_json, utc_now_iso
from train_kie.ontology import ONTOLOGY_ID, normalize_value


def _strip_accents(text: str) -> str:
    normalized = unicodedata.normalize("NFD", (text or "").replace("đ", "d").replace("Đ", "D"))
    return "".join(char for char in normalized if unicodedata.category(char) != "Mn")


def _normalize_text(text: str | None) -> str:
    return " ".join(_strip_accents(text or "").lower().split())


def field_vote_key(field: dict) -> tuple:
    label = (field.get("label") or "").strip().upper()
    page_index = field.get("page_index")
    norm_value = field.get("normalized_value") or normalize_value(label, field.get("text"))
    if norm_value:
        return label, page_index, _normalize_text(norm_value)
    return label, page_index, _normalize_text(field.get("text"))


def choose_best_candidate(candidates: list[dict]) -> dict:
    def score(item: dict) -> tuple:
        confidence = item.get("confidence")
        if confidence is None:
            confidence = 0.0
        return (
            float(confidence),
            len(item.get("word_ids") or []),
            len(item.get("line_ids") or []),
            len(item.get("text") or ""),
        )

    ranked = sorted(candidates, key=score, reverse=True)
    return copy.deepcopy(ranked[0])


def build_consensus(raw_runs: dict[str, dict], min_votes: int | None = None) -> dict:
    model_names = sorted(raw_runs.keys())
    if not model_names:
        raise ValueError("No raw annotations were provided for adjudication.")

    min_votes = min_votes or (2 if len(model_names) >= 2 else 1)
    field_buckets = defaultdict(list)
    field_key_by_run = {}

    for model_name, payload in raw_runs.items():
        for field in payload.get("annotation", {}).get("field_instances", []):
            key = field_vote_key(field)
            field_buckets[key].append((model_name, field))
            field_key_by_run[(model_name, field.get("field_id"))] = key

    consensus_fields = []
    field_id_map = {}
    conflicts = []
    next_field_index = 1

    for key in sorted(field_buckets.keys()):
        votes = field_buckets[key]
        if len(votes) < min_votes:
            conflicts.append({
                "kind": "field",
                "key": list(key),
                "votes": len(votes),
                "supporting_models": [model_name for model_name, _ in votes],
            })
            continue

        best = choose_best_candidate([field for _, field in votes])
        new_field_id = f"f{next_field_index}"
        next_field_index += 1
        best["field_id"] = new_field_id
        best["normalized_value"] = best.get("normalized_value") or normalize_value(best.get("label", ""), best.get("text"))
        best["confidence"] = round(len(votes) / len(model_names), 4)
        best["source"] = "consensus_vote"
        best["review_status"] = "auto_consensus" if len(votes) == len(model_names) else "needs_review"
        best["supporting_models"] = [model_name for model_name, _ in votes]
        best["vote_count"] = len(votes)
        consensus_fields.append(best)
        field_id_map[key] = new_field_id

    relation_buckets = defaultdict(list)
    for model_name, payload in raw_runs.items():
        for relation in payload.get("annotation", {}).get("relations", []):
            from_key = field_key_by_run.get((model_name, relation.get("from_field_id")))
            to_key = field_key_by_run.get((model_name, relation.get("to_field_id")))
            if not from_key or not to_key:
                continue
            vote_key = (
                relation.get("type"),
                from_key,
                to_key,
            )
            relation_buckets[vote_key].append((model_name, relation))

    consensus_relations = []
    next_relation_index = 1
    for vote_key in sorted(relation_buckets.keys(), key=lambda item: (item[0], item[1], item[2])):
        votes = relation_buckets[vote_key]
        if len(votes) < min_votes:
            conflicts.append({
                "kind": "relation",
                "key": [vote_key[0], list(vote_key[1]), list(vote_key[2])],
                "votes": len(votes),
                "supporting_models": [model_name for model_name, _ in votes],
            })
            continue
        relation_type, from_field_key, to_field_key = vote_key
        from_consensus_id = field_id_map.get(from_field_key)
        to_consensus_id = field_id_map.get(to_field_key)
        if not from_consensus_id or not to_consensus_id:
            continue
        consensus_relations.append({
            "relation_id": f"r{next_relation_index}",
            "type": relation_type,
            "from_field_id": from_consensus_id,
            "to_field_id": to_consensus_id,
            "confidence": round(len(votes) / len(model_names), 4),
            "supporting_models": [model_name for model_name, _ in votes],
        })
        next_relation_index += 1

    return {
        "created_at": utc_now_iso(),
        "model_votes": model_names,
        "annotation": {
            "field_instances": consensus_fields,
            "relations": consensus_relations,
        },
        "conflicts": conflicts,
    }


def inject_consensus_into_canonical(canonical_json_path: str, consensus_payload: dict) -> dict:
    doc = read_json(canonical_json_path)
    if not doc:
        raise FileNotFoundError(f"Canonical JSON not found: {canonical_json_path}")
    doc = upgrade_ocr_data_in_place(doc)
    annotations = doc.setdefault("annotations", {})
    annotations["schema"] = ONTOLOGY_ID
    annotations["status"] = "labeled"
    annotations["source"] = "consensus_vote"
    annotations["field_instances"] = consensus_payload.get("annotation", {}).get("field_instances", [])
    annotations["relations"] = consensus_payload.get("annotation", {}).get("relations", [])
    annotations["conflicts"] = consensus_payload.get("conflicts", [])
    return doc
