# Audit Guide

This file is the fastest source of truth for any AI or human that audits or labels
`json_output_labeled/`.

## Mandatory Reading Order

Read these files in this exact order before auditing or labeling:

1. `D:\App\ocrtool\train_kie\AUDIT_GUIDE.md`
2. `D:\App\ocrtool\train_kie\README.md`
3. `D:\App\ocrtool\train_kie\ontology.py`
4. `D:\App\ocrtool\train_kie\labeling_workspace.py`

Read these only if you need to understand `DOC_TYPE` postprocess or rule-based marks:

5. `D:\App\ocrtool\train_kie\semantic_fields.py`
6. `D:\App\ocrtool\train_kie\inference_pipeline.py`

## Source Of Truth Priority

Use this order when something conflicts:

1. PDF visible by eye
2. Rules in `ontology.py` and `labeling_workspace.py`
3. OCR lines in `json_input/`
4. Existing labeled JSON

Old heuristics or ad-hoc scripts are not a higher-priority source of truth than the PDF.

## Allowed labels

The only labels allowed in `field_instances` of a labeled JSON are:

- `REGIME_HEADER`
- `ISSUE_ORG_SUPERIOR`
- `ISSUE_ORG_NAME`
- `DOC_NUMBER_SYMBOL`
- `PLACE_DATE`
- `DOC_SUBJECT`
- `ADDRESSEE`
- `RECIPIENTS`
- `SIGNER_ROLE`
- `SIGNER_NAME`

Rule-based marks (`URGENCY_MARK`, `SECRECY_MARK`, `CIRCULATION_MARK`) and the
deterministic post-processed `DOC_TYPE` must never be labeled by hand.

## Hard Rules

**Anchored span rule (applies to all label fields):**

- Label the full anchored OCR span. If the OCR line has a prefix/anchor (e.g. `Số:`,
  `Kính gửi:`, `Nơi nhận:`, `TM.`, `KT.`), keep that prefix in the label text.
- If the OCR line does not have the prefix, do not invent one.
- Never strip prefixes at the labeling step. Post-processing will strip cleanly
  after inference.
- Never split a field into sub-parts at the labeling step. E.g. do not split
  `"Số: 139/QĐ-ĐTTH"` into number and symbol.

**Per-label rules:**

- `REGIME_HEADER` is the whole top-right regime header block. It can include the
  state header (`CỘNG HÒA XÃ HỘI CHỦ NGHĨA VIỆT NAM\nĐộc lập - Tự do - Hạnh phúc`)
  and/or the party header (`ĐẢNG CỘNG SẢN VIỆT NAM`) on the same document. Keep
  whatever the OCR surface shows; do not split into two fields.
- `ISSUE_ORG_SUPERIOR` is the direct superior issuing authority block (e.g.
  `ĐẢNG BỘ HUYỆN ...`, `ỦY BAN NHÂN DÂN TỈNH ...`).
- `ISSUE_ORG_NAME` is the issuing authority block (e.g. `ĐẢNG ỦY THỊ TRẤN ...`).
- When a header has a parent organization line above and a sub-unit / operating
  body line below, keep the upper line in `ISSUE_ORG_SUPERIOR` and the lower
  line in `ISSUE_ORG_NAME`. Do not collapse them just because both belong to
  the same umbrella organization.
- Example: if the visible header is conceptually `HỘI LIÊN HIỆP PHỤ NỮ` on the
  upper line and `BAN THƯỜNG VỤ` on the lower line, then `ISSUE_ORG_SUPERIOR`
  is `HỘI LIÊN HIỆP PHỤ NỮ` and `ISSUE_ORG_NAME` is `BAN THƯỜNG VỤ` (using the
  exact OCR surface text actually printed on each line). By contrast, if the
  visible issuing line itself is `HỘI LIÊN HIỆP PHỤ NỮ THỊ TRẤN ...` and there
  is no separate lower issuing-body line, that whole line is `ISSUE_ORG_NAME`.
- `DOC_NUMBER_SYMBOL` is the single doc-number/symbol span. Include `Số:` / `SỐ:`
  / `Số` if OCR has it. If OCR wrapped the symbol suffix to a second line, the
  label is multi-line (contiguous) and must not include unrelated continuation.
- `PLACE_DATE` is the place/date line.
- `DOC_SUBJECT` is the full real title block, not body text. If an explicit printed
  doc-type line is inside the title block, keep that line inside `DOC_SUBJECT`.
  Later body lines such as `QUYẾT ĐỊNH:`, `Điều 1`, `Căn cứ`, `Xét` are not
  automatically part of `DOC_SUBJECT`.
- `ADDRESSEE` is the `Kính gửi` block. Keep the anchor `Kính gửi:` in the label
  text if OCR has it; preserve variants like `Kinh gửi:` / `Kính gởi:` / `-Kính gửi`
  exactly as printed. If `Kính gửi` is not visibly printed, do not invent the header.
- `RECIPIENTS` is the `Nơi nhận` block. Keep `Nơi nhận:` (or OCR variants like
  `Nơi nhân:`) in the label text. If `Nơi nhận` is not visibly printed, do not
  invent the header.
- `SIGNER_ROLE` is the merged authority+title block for one signer. Keep prefix
  anchors (`TM.`, `KT.`, `T/M`, `TL.`, `TUQ.`, `Q.`) if OCR has them. The title
  part (`CHỦ TỊCH`, `BÍ THƯ`, `PHÓ CHỦ TỊCH`, ...) belongs in the same field.
- If the signer block also prints an extra office/title line directly below the
  signer name (often in parentheses, e.g. `(Phó Chủ tịch UBND thị trấn)` or
  `(Bí thư Chi bộ)`), include that lower office/title line in the same
  `SIGNER_ROLE` field for that signer. Do not leave that office/title line
  unlabeled just because it is printed under `SIGNER_NAME`.
- `SIGNER_NAME` is the full printed name of the signer.
- Multi-signer documents: create multiple `SIGNER_ROLE` and `SIGNER_NAME`
  instances, one per signer. Do not merge across signer blocks.

**Text format rules:**

- `text` in labeled JSON must preserve real OCR line structure using newline `\n`.
- Never replace structure with `|` as a synthetic separator in labeled JSON. A
  literal `|` may still appear if it truly exists in OCR noise; that is not a
  formatting rule.
- If labeled JSON contains `?`, always compare that field against the exact OCR
  units in `json_input/` first. If the same `?` already exists in the selected
  OCR `word_ids` / `line_ids`, keep it — that is OCR source text, not a labeling
  bug. If `json_input/` has a real character but labeled JSON turned it into `?`,
  treat that as output corruption / encoding damage and restore the exact OCR
  text from `json_input/`.
- Do not manually retype a cleaner-looking character when the exact OCR text is
  available in `words[].text` or `lines[].text`.

## Common False Positives

- A body line later on the page repeats `QUYẾT ĐỊNH`, `THÔNG BÁO`, `BÁO CÁO`,
  etc. This does not mean the first title block is missing text.
- A header line like `QUYẾT ĐỊNH 191-QĐ/ĐU` above a `BÁO CÁO` can be a reference
  to the inspection/decision that created the report, not part of the report
  title.
- `RECIPIENTS` or `ADDRESSEE` may start with a bullet or OCR punctuation. That
  alone is not a label error.
- Multi-line `SIGNER_ROLE` often looks like `TM. BAN THƯỜNG VỤ\nBÍ THƯ`. That is
  one field, not two. Do not split into authority/title at the labeling step.

## Relation Rules

Only one relation type is allowed:

- `signed_by`: `SIGNER_ROLE -> SIGNER_NAME`

For multi-signer documents, create one `signed_by` relation per signer, matching
each `SIGNER_ROLE` to its corresponding `SIGNER_NAME` by position/order on the
page.

## Text Display Rule

- In reports for humans, you may show lines joined by `|` for compact display.
- In labeled JSON itself, always keep real line breaks as `\n`.

## Prompt: AI Batch Audit + Fix

Dùng prompt sau để đưa cho AI agent (có quyền đọc/ghi file và xem PDF bằng
vision) xử lý 1 batch. Thay `XXXX` bằng số batch cần xử lý. Rule chi tiết nằm
ở các section trên của file này — prompt dưới chỉ cần reference.

```markdown
# Task: Audit + fix labeled KIE JSON cho batch_XXXX (ontology v3)

Bạn là AI agent có quyền đọc/ghi file trực tiếp và xem PDF bằng vision.

## Mục tiêu
Audit và sửa toàn bộ output KIE của batch_XXXX. Sửa trực tiếp trong OUTPUT_DIR.
Không OCR lại, không đổi tên file, không đổi batch assignment.

## Đọc trước theo đúng thứ tự
1. D:\App\ocrtool\train_kie\AUDIT_GUIDE.md (file này — rule + label + relation)
2. D:\App\ocrtool\train_kie\README.md
3. D:\App\ocrtool\train_kie\ontology.py
4. D:\App\ocrtool\train_kie\labeling_workspace.py

## Đường dẫn
- INPUT_DIR  = D:\tmp\Train_20260413_143844_kie\json_input\batch_XXXX
- OUTPUT_DIR = D:\tmp\Train_20260413_143844_kie\json_output_labeled\batch_XXXX
- Canonical OCR (đầy đủ, có line.ocr_text, word.has_space_after): đường dẫn
  chính xác nằm trong input_doc["source_canonical_json"]; thường là
  D:\tmp\Train_20260413_143844_kie\ocr\<doc_id>_ocr.pdf.json
- PDF gốc để xem vision: đường dẫn trong input_doc["relative_pdf_path"]
- Python venv: D:\App\ocrtool\.venv_build\Scripts\python.exe

## Nguồn ưu tiên khi quyết định
Theo đúng thứ tự "Source Of Truth Priority" ở AUDIT_GUIDE.md:
1. PDF nhìn bằng vision
2. Rule trong ontology.py và labeling_workspace.py
3. OCR trong json_input (và canonical khi cần line.ocr_text gốc)
4. Output cũ nếu có

## Ontology v3
10 labels + 1 relation đã định nghĩa ở AUDIT_GUIDE.md mục "Allowed labels" và
"Relation Rules". Tuyệt đối không dùng label/relation v2 (STATE_HEADER,
PARTY_TITLE, DOC_NUMBER, DOC_SYMBOL, DOC_NUMBER_SYMBOL_FULL, SIGNER_AUTHORITY,
SIGNER_TITLE, authority_for). Không gán tay URGENCY_MARK, SECRECY_MARK,
CIRCULATION_MARK, DOC_TYPE.

## Rule
Theo "Hard Rules", "Per-label rules", "Text format rules", "Common False
Positives", "Relation Rules" ở AUDIT_GUIDE.md. Không lặp lại ở đây.

Bổ sung về word_ids/line_ids:
- Nếu word_ids không map chính xác được, cho phép word_ids = [] nhưng vẫn phải
  có line_ids, text, page_index. Validator sẽ auto-fill word_ids theo policy.
- Text giữ OCR surface gốc: ưu tiên line.ocr_text / word.ocr_text (canonical),
  fallback line.text / word.text nếu không có.
- Nếu output cũ sai nhiều, viết lại toàn bộ field_instances và relations.

## Yêu cầu rà bằng vision (bắt buộc)
- Trang đầu: check kỹ ISSUE_ORG_SUPERIOR vs ISSUE_ORG_NAME bằng PDF + OCR (bbox,
  thứ tự dọc). SUPERIOR thường ở trên, NAME ở dưới.
- DOC_SUBJECT trang đầu: tránh thiếu/thừa body. Xem "Common False Positives".
- Trang có người ký: nếu file hiện tại không có signer ở các trang trong
  json_input, xem PDF xác định trang đầu tiên (từ trên xuống, không đi vào
  phụ lục/đính kèm) có người ký chính; kiểm tra SIGNER_ROLE/SIGNER_NAME không
  thiếu/thừa text, không dính con dấu đỏ.
- Nếu trang chứa signer chính không có trong json_input: regenerate json_input
  cho đúng file đó từ canonical OCR, rồi mới sửa output.

## Validator chuẩn (bắt buộc dùng sau mỗi fix)
```python
import sys; sys.path.insert(0, r"D:\App\ocrtool")
from train_kie.labeling_workspace import validate_label_output_detailed
# result = {"errors": [...], "warnings": [...], "normalized": dict or None}
result = validate_label_output_detailed(labeled_payload, canonical_doc, llm_name="audit")
```
Hoặc tối thiểu `normalize_label_output(...)` (strict, throw nếu có error).
Chuẩn pass cuối: file phải import được qua `train_kie/4-adjudicate_votes.py`
(dùng `load_external_label_output`).

### Error codes → action
- UNKNOWN_WORD_ID / UNKNOWN_LINE_ID: xóa id không tồn tại hoặc chọn lại id
  đúng từ canonical (pages[].words/lines).
- AMBIGUOUS_LINE_ONLY_FIELD: field có line_ids nhưng word_ids=[] và text không
  khớp full-line OCR. Full-line → chỉnh text khớp line.ocr_text; partial →
  ghi word_ids cụ thể.
- DOC_NUMBER_SYMBOL contiguous / forbidden continuation: line_ids phải liền
  kề, không chứa "ngày/Kính gửi/Nơi nhận".
- signed_by endpoints: relation phải từ SIGNER_ROLE → SIGNER_NAME. Đổi label
  hoặc đổi hướng relation.
- page_index mismatch / spans multiple pages: sửa page_index theo line/word
  thực; split thành field riêng theo page nếu cần.
- Empty text / missing line_ids+word_ids: bổ sung từ canonical hoặc xóa field.

### Warning codes → action
- MISSING_ANCHOR: ADDRESSEE/RECIPIENTS nên include "Kính gửi:"/"Nơi nhận:"
  nếu OCR line gốc có; mở rộng line_ids/word_ids để bao anchor.
- FIELD_OVERLAP (≥50% word_ids): 2 field khác label cùng chiếm word_ids. Thu
  hẹp 1 field hoặc xóa field sai.

## Quy trình cho mỗi file
1. Mở file input trong INPUT_DIR.
2. Mở PDF tương ứng để rà bằng vision.
3. Nếu có output cùng tên: audit kỹ và sửa; chưa có: tạo mới.
4. Chạy validate_label_output_detailed. Fix đến khi errors=[]. Warnings còn
   lại phải review từng cái (chấp nhận nếu đã đúng semantic).
5. Ghi đè file output với schema:
   `{"field_instances":[{field_id, label, page_index, line_ids, word_ids, text,
    normalized_value, confidence?}, ...], "relations":[{relation_id,
    type:"signed_by", from_field_id, to_field_id, confidence?}, ...]}`
6. Chỉ giữ file nếu pass validator.

## Tiêu chí pass
- Chỉ 10 labels v3, chỉ relation signed_by, không dấu vết v2.
- JSON hợp lệ, khớp OCR surface trong canonical.
- Không field/relation bịa.
- Pass validator chuẩn; import được qua 4-adjudicate_votes.py.
- Pass semantic review bằng PDF + OCR.

## Thực thi
- Sửa file trực tiếp trên đĩa, tuần tự toàn bộ batch.
- Không hỏi lại nếu không thật sự blocker.
- File quá mơ hồ: lưu bản tốt nhất + ghi vào danh sách review tay.

## Báo cáo cuối (in trực tiếp ra stdout/chat, KHÔNG ghi file)
- Batch đã xử lý
- Tổng số file input
- Số file tạo mới / sửa / giữ nguyên
- Danh sách file cần review tay
- File nào phải regenerate json_input
- Lỗi schema/OCR bất thường nếu có

KHÔNG in lại nội dung JSON của từng file trong báo cáo cuối.
```
