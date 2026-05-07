"""
Audit script: scan 200 labeled KIE JSON files (batch_0001 + batch_0002) against
their canonical input JSONs, to surface patterns that could be fixed by
improving the KIE Viewer.

- Does NOT modify any file.
- Prints a markdown report to stdout.
- Run:
    python kie_viewer/audit_viewer_issues.py
"""

from __future__ import annotations

import glob
import io
import json
import os
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

# Force UTF-8 stdout on Windows
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

INPUT_ROOT = r"D:/tmp/Train_20260413_143844_kie/json_input"
LABELED_ROOT = r"D:/tmp/Train_20260413_143844_kie/json_output_labeled"
BATCHES = ["batch_0001", "batch_0002"]

V3_TRAIN_LABELS = {
    "REGIME_HEADER", "ISSUE_ORG_SUPERIOR", "ISSUE_ORG_NAME", "DOC_NUMBER_SYMBOL",
    "PLACE_DATE", "DOC_SUBJECT", "ADDRESSEE", "RECIPIENTS",
    "SIGNER_ROLE", "SIGNER_NAME",
}
V3_READONLY_LABELS = {"URGENCY_MARK", "SECRECY_MARK", "CIRCULATION_MARK", "DOC_TYPE"}
V3_ALL_LABELS = V3_TRAIN_LABELS | V3_READONLY_LABELS


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_input_index(inp):
    """Build lookup maps from a canonical input JSON."""
    pages = inp.get("pages", [])
    line_by_id = {}
    word_by_id = {}
    line_to_page = {}
    word_to_page = {}
    line_to_wordids = {}
    word_to_line = {}
    for page in pages:
        pidx = page.get("page_index")
        for line in page.get("lines", []):
            lid = line.get("line_id")
            line_by_id[lid] = line
            line_to_page[lid] = pidx
            wids = []
            for w in line.get("words", []):
                wid = w.get("word_id")
                word_by_id[wid] = w
                word_to_page[wid] = pidx
                word_to_line[wid] = lid
                wids.append(wid)
            # Prefer explicit word_ids list if present; else derived
            line_to_wordids[lid] = line.get("word_ids") or wids
    return {
        "line_by_id": line_by_id,
        "word_by_id": word_by_id,
        "line_to_page": line_to_page,
        "word_to_page": word_to_page,
        "line_to_wordids": line_to_wordids,
        "word_to_line": word_to_line,
    }


def parse_line_num(lid):
    # "p0_l3" -> (0, 3)
    try:
        p, l = lid.split("_")
        return int(p[1:]), int(l[1:])
    except Exception:
        return None


def truncate(s, n=80):
    if s is None:
        return ""
    s = str(s)
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"


def main():
    files = []
    for b in BATCHES:
        files.extend(sorted(glob.glob(os.path.join(LABELED_ROOT, b, "*.json"))))

    total = len(files)

    # Per-pattern collectors
    p1 = {"file_count": set(), "field_count": 0, "samples": []}
    p2 = {"file_count": set(), "field_count": 0, "samples": []}
    p3 = {"file_count": set(), "field_count": 0, "samples": []}
    p4 = {"file_count": set(), "field_count": 0, "samples": []}
    p5 = {"file_count": set(), "field_count": 0, "samples": [], "labels": Counter()}
    p6 = {"file_count": set(), "field_count": 0, "samples": []}
    p7 = {"file_count": set(), "field_count": 0, "samples": []}
    p8 = {"file_count": set(), "field_count": 0, "samples": []}
    p9 = {"file_count": set(), "field_count": 0, "samples": []}  # SIGNER w/o relation
    p10 = {"file_count": set(), "rel_count": 0, "samples": []}   # dangling relation
    p11 = {"file_count": set(), "rel_count": 0, "samples": []}   # relation_type key
    p12 = {"file_count": set(), "field_count": 0, "samples": []} # empty field
    p13 = {"file_count": set(), "field_count": 0, "samples": []} # ADDRESSEE/RECIPIENTS anchor missing
    p14 = {"file_count": set(), "field_count": 0, "samples": []} # normalized != text
    p15 = {"missing_count": 0, "values": []}                      # confidence distribution

    # Free-form extras
    extras = defaultdict(lambda: {"file_count": set(), "count": 0, "samples": []})

    files_with_any_issue = set()

    for lp in files:
        stem = Path(lp).stem
        # Figure out input batch
        batch = Path(lp).parent.name
        ip = os.path.join(INPUT_ROOT, batch, stem + ".json")
        if not os.path.isfile(ip):
            extras["input_missing"]["count"] += 1
            extras["input_missing"]["file_count"].add(stem)
            if len(extras["input_missing"]["samples"]) < 3:
                extras["input_missing"]["samples"].append(stem)
            continue
        try:
            lab = load_json(lp)
            inp = load_json(ip)
        except Exception as exc:
            extras["parse_error"]["count"] += 1
            extras["parse_error"]["file_count"].add(stem)
            if len(extras["parse_error"]["samples"]) < 3:
                extras["parse_error"]["samples"].append(f"{stem}: {exc}")
            continue

        idx = build_input_index(inp)
        line_by_id = idx["line_by_id"]
        word_by_id = idx["word_by_id"]
        line_to_page = idx["line_to_page"]
        word_to_page = idx["word_to_page"]
        line_to_wordids = idx["line_to_wordids"]
        word_to_line = idx["word_to_line"]

        fields = lab.get("field_instances", []) or []
        relations = lab.get("relations", []) or []

        # Build fast maps for the labeled file
        fid_to_field = {fi.get("field_id"): fi for fi in fields}
        signer_role_ids = {fi["field_id"] for fi in fields if fi.get("label") == "SIGNER_ROLE"}
        signer_name_ids = {fi["field_id"] for fi in fields if fi.get("label") == "SIGNER_NAME"}
        rel_from_ids = set()
        rel_to_ids = set()

        issue_this_file = False

        # Relations first (cheap)
        for r in relations:
            # Pattern 11: 'relation_type' key
            if "relation_type" in r and "type" not in r:
                p11["file_count"].add(stem)
                p11["rel_count"] += 1
                if len(p11["samples"]) < 3:
                    p11["samples"].append((stem, r))
                issue_this_file = True
            frm = r.get("from_field_id")
            to = r.get("to_field_id")
            if frm:
                rel_from_ids.add(frm)
            if to:
                rel_to_ids.add(to)
            # Pattern 10: dangling
            if (frm and frm not in fid_to_field) or (to and to not in fid_to_field):
                p10["file_count"].add(stem)
                p10["rel_count"] += 1
                if len(p10["samples"]) < 5:
                    p10["samples"].append((stem, r.get("relation_id"), frm, to))
                issue_this_file = True

        for fi in fields:
            fid = fi.get("field_id")
            label = fi.get("label")
            page_idx = fi.get("page_index")
            line_ids = fi.get("line_ids") or []
            word_ids = fi.get("word_ids") or []
            text = fi.get("text") or ""
            nv = fi.get("normalized_value")

            # Pattern 5: unknown label
            if label not in V3_ALL_LABELS:
                p5["file_count"].add(stem)
                p5["field_count"] += 1
                p5["labels"][label] += 1
                if len(p5["samples"]) < 3:
                    p5["samples"].append((stem, fid, label))
                issue_this_file = True

            # Pattern 12: empty field
            if (not line_ids and not word_ids) or not text.strip():
                p12["file_count"].add(stem)
                p12["field_count"] += 1
                if len(p12["samples"]) < 3:
                    p12["samples"].append((stem, fid, label, truncate(text)))
                issue_this_file = True

            # Pattern 6: multi-page word_ids
            word_pages = {word_to_page.get(wid) for wid in word_ids if wid in word_to_page}
            word_pages.discard(None)
            if len(word_pages) > 1:
                p6["file_count"].add(stem)
                p6["field_count"] += 1
                if len(p6["samples"]) < 3:
                    p6["samples"].append((stem, fid, label, sorted(word_pages)))
                issue_this_file = True

            # Pattern 7: page_index mismatch
            expected_pages = set()
            for wid in word_ids:
                if wid in word_to_page:
                    expected_pages.add(word_to_page[wid])
            for lid in line_ids:
                if lid in line_to_page:
                    expected_pages.add(line_to_page[lid])
            if expected_pages and page_idx not in expected_pages:
                p7["file_count"].add(stem)
                p7["field_count"] += 1
                if len(p7["samples"]) < 3:
                    p7["samples"].append((stem, fid, label, page_idx, sorted(expected_pages)))
                issue_this_file = True

            # Pattern 3: word_ids not in any field.line_ids
            line_id_set = set(line_ids)
            bad_word_ids = []
            for wid in word_ids:
                lid_owner = word_to_line.get(wid)
                if lid_owner is None:
                    bad_word_ids.append((wid, None))
                elif lid_owner not in line_id_set:
                    bad_word_ids.append((wid, lid_owner))
            if bad_word_ids:
                p3["file_count"].add(stem)
                p3["field_count"] += 1
                if len(p3["samples"]) < 3:
                    p3["samples"].append((stem, fid, label, bad_word_ids[:3]))
                issue_this_file = True

            # Pattern 4: line_ids with no words in word_ids
            wid_set = set(word_ids)
            dangling_lines = []
            for lid in line_ids:
                wids_in_line = line_to_wordids.get(lid, [])
                if not any(w in wid_set for w in wids_in_line):
                    dangling_lines.append(lid)
            if dangling_lines:
                p4["file_count"].add(stem)
                p4["field_count"] += 1
                if len(p4["samples"]) < 3:
                    p4["samples"].append((stem, fid, label, dangling_lines[:3]))
                issue_this_file = True

            # Pattern 1: full-line field, text != line.text
            # Condition: exactly 1 line_id, word_ids covers the entire line.
            if len(line_ids) == 1:
                lid = line_ids[0]
                full_line_wids = line_to_wordids.get(lid, [])
                if full_line_wids and set(word_ids) == set(full_line_wids) and lid in line_by_id:
                    line_text = line_by_id[lid].get("text", "")
                    if text != line_text:
                        p1["file_count"].add(stem)
                        p1["field_count"] += 1
                        if len(p1["samples"]) < 5:
                            p1["samples"].append((stem, label, line_text, text))
                        issue_this_file = True

            # Pattern 2: multi-line text not split by \n
            if len(line_ids) > 1:
                # Check if all lines are fully covered
                if "\n" not in text:
                    # Multi-line field collapsed to single-line text
                    p2["file_count"].add(stem)
                    p2["field_count"] += 1
                    if len(p2["samples"]) < 5:
                        lines_text = [line_by_id.get(lid, {}).get("text", "") for lid in line_ids]
                        p2["samples"].append((stem, label, lines_text, truncate(text, 120)))
                    issue_this_file = True
                else:
                    # Has newlines: verify split matches the line.text sequence
                    parts = text.split("\n")
                    expected_parts = [line_by_id.get(lid, {}).get("text", "") for lid in line_ids]
                    if len(parts) != len(line_ids):
                        p2["file_count"].add(stem)
                        p2["field_count"] += 1
                        if len(p2["samples"]) < 5:
                            p2["samples"].append((stem, label, f"nlines={len(line_ids)}", f"nparts={len(parts)}"))
                        issue_this_file = True

            # Pattern 8: DOC_NUMBER_SYMBOL non-contiguous lines
            if label == "DOC_NUMBER_SYMBOL" and len(line_ids) > 1:
                parsed = [parse_line_num(l) for l in line_ids]
                if all(parsed):
                    pages_set = {x[0] for x in parsed}
                    if len(pages_set) == 1:
                        nums = sorted(x[1] for x in parsed)
                        if nums != list(range(nums[0], nums[-1] + 1)):
                            p8["file_count"].add(stem)
                            p8["field_count"] += 1
                            if len(p8["samples"]) < 3:
                                p8["samples"].append((stem, fid, line_ids))
                            issue_this_file = True

            # Pattern 13: ADDRESSEE/RECIPIENTS anchor missing
            if label in ("ADDRESSEE", "RECIPIENTS") and line_ids:
                lid0 = line_ids[0]
                line_text = line_by_id.get(lid0, {}).get("text", "")
                anchor = "kính gửi" if label == "ADDRESSEE" else "nơi nhận"
                if anchor in line_text.lower() and anchor not in text.lower():
                    p13["file_count"].add(stem)
                    p13["field_count"] += 1
                    if len(p13["samples"]) < 3:
                        p13["samples"].append((stem, label, truncate(line_text), truncate(text)))
                    issue_this_file = True

            # Pattern 14: normalized_value != text
            if nv is not None and nv != text:
                p14["file_count"].add(stem)
                p14["field_count"] += 1
                if len(p14["samples"]) < 5:
                    p14["samples"].append((stem, label, truncate(text), truncate(nv)))

            # Pattern 15: confidence
            conf = fi.get("confidence")
            if conf is None:
                p15["missing_count"] += 1
            else:
                try:
                    p15["values"].append(float(conf))
                except Exception:
                    p15["missing_count"] += 1

        # Pattern 9: SIGNER_ROLE or SIGNER_NAME without relation
        for sid in signer_role_ids | signer_name_ids:
            if sid not in rel_from_ids and sid not in rel_to_ids:
                p9["file_count"].add(stem)
                p9["field_count"] += 1
                lbl = fid_to_field[sid].get("label")
                if len(p9["samples"]) < 5:
                    p9["samples"].append((stem, sid, lbl))
                issue_this_file = True

        if issue_this_file:
            files_with_any_issue.add(stem)

    # ═════════════════════════════════════════════════════
    # Build report
    # ═════════════════════════════════════════════════════
    out = []
    out.append("## Summary")
    out.append(f"- Total files scanned: {total}")
    out.append(f"- Files with any issue: {len(files_with_any_issue)}")
    out.append("")

    def emit(idx, title, bucket, field_mode=True, sample_formatter=None):
        fc = len(bucket["file_count"])
        if field_mode:
            mc = bucket.get("field_count", 0)
        else:
            mc = bucket.get("rel_count", 0)
        if fc == 0 and mc == 0:
            out.append(f"## Pattern {idx}: {title}")
            out.append("- Không có.")
            out.append("")
            return
        unit = "fields" if field_mode else "relations"
        out.append(f"## Pattern {idx}: {title}")
        out.append(f"- Count: {fc} files, {mc} {unit}")
        if bucket.get("samples"):
            out.append("- Samples:")
            for s in bucket["samples"]:
                if sample_formatter:
                    out.append(f"  - {sample_formatter(s)}")
                else:
                    out.append(f"  - {s}")
        out.append("")

    emit(1, "Full-line field nhưng text ≠ line.text", p1,
         sample_formatter=lambda s: f"{s[0]} | {s[1]} | expected={s[2]!r} | actual={s[3]!r}")
    emit(2, "Multi-line field không đúng split \\n", p2,
         sample_formatter=lambda s: f"{s[0]} | {s[1]} | lines={s[2]} | text={s[3]!r}")
    emit(3, "word_ids có word mà line chủ không trong line_ids", p3,
         sample_formatter=lambda s: f"{s[0]} | {s[1]} | {s[2]} | bad={s[3]}")
    emit(4, "line_ids thừa (không word nào trong word_ids)", p4,
         sample_formatter=lambda s: f"{s[0]} | {s[1]} | {s[2]} | dangling_lines={s[3]}")
    emit(5, "Label không nằm trong ontology v3", p5,
         sample_formatter=lambda s: f"{s[0]} | {s[1]} | {s[2]}")
    if p5["labels"]:
        out.append(f"  - Unknown labels: {dict(p5['labels'])}")
        out.append("")
    emit(6, "Field span nhiều page", p6,
         sample_formatter=lambda s: f"{s[0]} | {s[1]} | {s[2]} | pages={s[3]}")
    emit(7, "page_index mismatch", p7,
         sample_formatter=lambda s: f"{s[0]} | {s[1]} | {s[2]} | field.page={s[3]} expected={s[4]}")
    emit(8, "DOC_NUMBER_SYMBOL non-contiguous lines", p8,
         sample_formatter=lambda s: f"{s[0]} | {s[1]} | lines={s[2]}")
    emit(9, "SIGNER_ROLE/SIGNER_NAME không trong relation signed_by", p9,
         sample_formatter=lambda s: f"{s[0]} | {s[1]} | {s[2]}")
    emit(10, "Relation dangling (from/to không tồn tại)", p10, field_mode=False,
         sample_formatter=lambda s: f"{s[0]} | rid={s[1]} from={s[2]} to={s[3]}")
    emit(11, "Relation key dùng 'relation_type' thay vì 'type'", p11, field_mode=False)
    emit(12, "Field rỗng (line_ids=word_ids=[] hoặc text='')", p12,
         sample_formatter=lambda s: f"{s[0]} | {s[1]} | {s[2]} | text={s[3]!r}")
    emit(13, "ADDRESSEE/RECIPIENTS mất anchor khi line gốc có", p13,
         sample_formatter=lambda s: f"{s[0]} | {s[1]} | line_text={s[2]!r} | field_text={s[3]!r}")
    emit(14, "normalized_value ≠ text", p14,
         sample_formatter=lambda s: f"{s[0]} | {s[1]} | text={s[2]!r} | normalized={s[3]!r}")

    # Pattern 15 - confidence
    out.append("## Pattern 15: Confidence distribution")
    if p15["values"]:
        vals = p15["values"]
        out.append(f"- Count with confidence: {len(vals)}; missing: {p15['missing_count']}")
        out.append(
            f"- min={min(vals):.3f}, max={max(vals):.3f}, mean={statistics.mean(vals):.3f}, "
            f"median={statistics.median(vals):.3f}"
        )
        bins = Counter()
        for v in vals:
            if v < 0.5:
                bins["<0.5"] += 1
            elif v < 0.8:
                bins["0.5-0.8"] += 1
            elif v < 0.95:
                bins["0.8-0.95"] += 1
            elif v < 1.0:
                bins["0.95-0.99"] += 1
            else:
                bins["=1.0"] += 1
        out.append(f"- Bins: {dict(bins)}")
    else:
        out.append(f"- Không có confidence nào; missing={p15['missing_count']}")
    out.append("")

    # Extras (free-form)
    if extras:
        out.append("## Extra observations")
        for k, v in extras.items():
            out.append(f"- {k}: files={len(v['file_count'])}, count={v['count']}, samples={v['samples'][:3]}")
        out.append("")

    print("\n".join(out))


if __name__ == "__main__":
    main()
