"""Test whether GEC-style correction models catch GRAMMAR errors (not just
spelling) on sentences from the user's document.

For each test sentence we know the planted grammar error. We send the
sentence to each model and print original vs corrected, then a diff so we
can judge whether the grammar error was touched at all.
"""

from __future__ import annotations

import difflib
import torch
from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

# Each item: (label, sentence, planted error description)
TESTS = [
    (
        "thieu_tu",
        "Giảm áp lực cáo cho cấp dưới, phục vụ xây dựng Hệ thống thông lãnh đạo.",
        "áp lực cáo -> báo cáo ; thông lãnh đạo -> thông tin lãnh đạo",
    ),
    (
        "dao_tu",
        "Văn phòng Thành ủy kính mời các đồng chí tham dự họp cuộc.",
        "họp cuộc -> cuộc họp (đảo từ)",
    ),
    (
        "thieu_tu_2",
        "Hiện trạng triển khai tổng hợp dữ trên Hệ thống quản trị thực thi của Thành phố.",
        "tổng hợp dữ -> tổng hợp dữ liệu (thiếu từ)",
    ),
    (
        "dao_tu_2",
        "Chia sẻ liệu dữ cho Văn phòng Thành ủy tại buổi họp.",
        "liệu dữ -> dữ liệu (đảo từ)",
    ),
    (
        "dinh_chu",
        "Đảm bảo đúng, đủ, sạch sống, thống nhất, dùng chung.",
        "sạch sống -> sạch, sống (dính chữ / thiếu dấu phẩy)",
    ),
    # A correct sentence as control -> should be left mostly unchanged
    (
        "CONTROL_dung",
        "Văn phòng Thành ủy kính mời các đồng chí tham dự cuộc họp.",
        "(không có lỗi - mong đợi: giữ nguyên)",
    ),
]


def correct(model_name: str, sentences: list[str], max_length: int = 256) -> list[str]:
    tok = AutoTokenizer.from_pretrained(model_name)
    model = AutoModelForSeq2SeqLM.from_pretrained(model_name).eval()
    outs = []
    for s in sentences:
        enc = tok(s, return_tensors="pt", truncation=True, max_length=512)
        with torch.no_grad():
            gen = model.generate(
                **enc,
                max_length=max_length,
                num_beams=5,
                early_stopping=True,
            )
        outs.append(tok.decode(gen[0], skip_special_tokens=True))
    return outs


def show_diff(a: str, b: str) -> str:
    """Word-level diff, returns a readable string."""
    wa = a.split()
    wb = b.split()
    sm = difflib.SequenceMatcher(None, wa, wb, autojunk=False)
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            out.append(" ".join(wa[i1:i2]))
        elif tag == "replace":
            out.append(f"[{' '.join(wa[i1:i2])} -> {' '.join(wb[j1:j2])}]")
        elif tag == "delete":
            out.append(f"[-{' '.join(wa[i1:i2])}]")
        elif tag == "insert":
            out.append(f"[+{' '.join(wb[j1:j2])}]")
    return " ".join(out).strip()


def main():
    sentences = [t[1] for t in TESTS]
    models = [
        ("bmd1905/vietnamese-correction-v2", "bmd1905-v2"),
        ("nrl-ai/vn-spell-correction-base", "nrl-spell"),
    ]
    results = {}
    for full, short in models:
        print(f"\n>>> generating with {short} ...")
        results[short] = correct(full, sentences)

    for i, (label, sent, note) in enumerate(TESTS):
        print("\n" + "=" * 90)
        print(f"[{label}] planted: {note}")
        print(f"ORIGINAL : {sent}")
        for short in [m[1] for m in models]:
            out = results[short][i]
            changed = "CHANGED" if out.strip() != sent.strip() else "unchanged"
            print(f"{short:12}: [{changed}] {show_diff(sent, out)}")


if __name__ == "__main__":
    main()
