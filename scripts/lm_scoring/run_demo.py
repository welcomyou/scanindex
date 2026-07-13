"""Demo: score a Vietnamese paragraph for spelling/grammar errors with
PhoBERT, BamiBERT (MLM) and Google round-trip (NMT).

Prints a per-word table so we can eyeball whether each method flags the
planted errors.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from underthesea import word_tokenize

from mlm_scorer import score_text, TokenScore
from roundtrip_scorer import roundtrip, score_roundtrip

TEXT = """Thực hiện ý kiến chỉ đạo của Thường trực Thành ủy về đẩy mạnh công tác chuyển đổi số, đổi mới phương thức làm việc trên môi trường số, đặc biệt là thay đổi chế độ thông tin, báo cáo, từ báo cáo giấy sang báo cáo dữ liệu theo thời gian thực, đảm bảo "đúng, đủ, sạch sống, thống nhất, dùng chung", giảm áp lực cáo cho cấp dưới, phục vụ xây dựng Hệ thống thông lãnh đạo, chỉ đạo của cấp ủy, cơ quan đảng, Văn phòng Thành ủy kính mời các đồng chí tham dự họp cuộc, cụ thể:
- Hiện trạng triển khai tổng hợp dữ trên Hệ thống quản trị thực thi của Thành phố.
- Chia sẻ liệu dữ cho Văn phòng Thành ủy."""

# Known planted errors -> for sanity-checking recall
KNOWN_ERRORS = {
    "sống": "sai_dính_chữ (sạch, sống)",
    "cáo": "thiếu_từ (báo cáo)",
    "thông": "thiếu_từ (thông tin)",
    "họp": "đảo (cuộc họp)",
    "cuộc": "đảo (cuộc họp)",
    "dữ": "thiếu_từ (dữ liệu)",
    "lieu": "đảo (dữ liệu)",
    "liệu": "đảo/chia sẻ (dữ liệu)",
}


def fmt(x: float, w: int = 7) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "  n/a".rjust(w)
    return f"{x:>{w}.2f}"


def run_mlm(model_name: str, label: str, words: list[str]) -> list[TokenScore]:
    print(f"\n=== {label} ({model_name}) ===")
    scores = score_text(model_name, words, top_k=6, device="cpu")
    # Print header
    print(f"{'#':>3}  {'word':<14} {'surp':>7} {'logp':>7} {'rank':>5}  top-k")
    print("-" * 90)
    for i, s in enumerate(scores):
        top = ", ".join(f"{t}({p:.2f})" for t, p in s.top_k[:4])
        flag = ""
        if s.word.lower() in KNOWN_ERRORS:
            flag = f"  <-- {KNOWN_ERRORS[s.word.lower()]}"
        print(f"{i:>3}  {s.word:<14} {fmt(s.surprisal)} {fmt(s.log_prob)} {s.rank:>5}  {top}{flag}")
    return scores


def main():
    words = word_tokenize(TEXT)
    print(f"Word-segmented into {len(words)} tokens:")
    print(" | ".join(words))
    print()

    # --- MLM models ---
    run_mlm("vinai/phobert-base-v2", "PhoBERT v2", words)
    run_mlm("Qualcomm-AI-Research/BamiBERT", "BamiBERT", words)

    # --- Google round-trip ---
    print("\n=== Google round-trip (Vi->En->Vi) ===")
    try:
        back = roundtrip(TEXT)
        print("ROUND-TRIP RESULT:")
        print(back)
        print("\nPer-word alignment (status):")
        rt_scores = score_roundtrip(TEXT, back)
        print(f"{'#':>3}  {'word':<14} {'status':<10} note")
        print("-" * 70)
        for i, s in enumerate(rt_scores):
            flag = ""
            if s.word.lower() in KNOWN_ERRORS:
                flag = f"  <-- {KNOWN_ERRORS[s.word.lower()]}"
            print(f"{i:>3}  {s.word:<14} {s.status:<10} {s.note}{flag}")
    except Exception as e:
        print(f"round-trip failed: {e}")


if __name__ == "__main__":
    main()
