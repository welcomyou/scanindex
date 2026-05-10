# Sample `digital_140` (synthetic)

This sample was originally a real Vietnamese administrative document.
It has been **replaced with a fully fabricated synthetic document** so the
public training pipeline has a schema-valid example to consume without
exposing real document content.

- Synthetic doc id: `0000000000000001`
- Synthetic relative PDF path: `digitalpdf/SYNTHETIC (DEMO).pdf`
- 1 page, 11 lines, 32 words. All bboxes are placeholder values.
- Field labels covered: `REGIME_HEADER`, `ISSUE_ORG_SUPERIOR`,
  `ISSUE_ORG_NAME`, `DOC_NUMBER_SYMBOL`, `PLACE_DATE`, `DOC_SUBJECT`,
  `RECIPIENTS`, `SIGNER_ROLE`, `SIGNER_NAME`.
- Relation: one `signed_by` linking SIGNER_ROLE → SIGNER_NAME.

The directory name `digital_140/` is kept for backward compatibility with
existing path references in the training scripts. The actual content has
no relationship to any real document.

## Files

```text
canonical_json/SYNTHETIC (DEMO)_ocr.pdf.json
label_input/digitalpdf__SYNTHETIC_DEMO__0000000000000001.json
labeled_json/digitalpdf__SYNTHETIC_DEMO__0000000000000001.json
sample_manifest.json
converted/layoutlmv3_base/train_sample.jsonl
converted/layoutlmv3_fontgray_norm/train_sample.jsonl
converted/layoutlmv3_fontgray_norm/label_list.json
converted/lilt_xlmr/train_sample.jsonl
converted/lilt_xlmr/train_labels.json
converted/lilt_phobert/train_sample.jsonl
converted/lilt_phobert/train_labels.json
converted/lightgbm/ground_truth_sample.jsonl
converted/lightgbm/<field>_candidate_sample.jsonl   (4 files)
```

## Schema reference

See `kie_vi_official_v3` in `scanindex/core/kie/json_utils.py` for the
canonical/labeled schemas. The structure here is hand-written to match
that schema exactly so downstream converters (`train_layoutlmv3`,
`train_lightgbm`, `train_lilt_*`) can smoke-test without modification.

To regenerate this synthetic example, run
`d:/tmp/_gen_synthetic_sample.py` from the project's source-of-truth
location (the script lives outside the repo by design — running it on
real document data would re-leak content).
