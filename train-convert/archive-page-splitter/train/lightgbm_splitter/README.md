# LightGBM Splitter Dataset

This branch builds page-level datasets for splitting a bulk-scanned PDF into
documents and selecting the first signer page for KIE.

It does not modify the production KIE LightGBM or LayoutLMv3 models.

## Build Dataset

```powershell
python D:\App\ocrtool\train-convert\archive-page-splitter\train\lightgbm_splitter\1-build_dataset_splitter.py `
  --label-root D:\tmp\Train_20260413_143844_kie\json_output_labeled `
  --ocr-root D:\tmp\Train_20260413_143844_kie\ocr `
  --output-root D:\tmp\Train_20260413_143844_LGBM_SPLITTER
```

Outputs:

```text
D:\tmp\Train_20260413_143844_LGBM_SPLITTER\dataset\doc_start_pages.csv
D:\tmp\Train_20260413_143844_LGBM_SPLITTER\dataset\signer_pages.csv
D:\tmp\Train_20260413_143844_LGBM_SPLITTER\dataset\manifest.json
D:\tmp\Train_20260413_143844_LGBM_SPLITTER\dataset\feature_definitions.json
D:\tmp\Train_20260413_143844_LGBM_SPLITTER\reports\dataset_summary.json
```

## Labels

`doc_start_pages.csv`

- target: `target_doc_start`
- positive: page containing labeled start fields
- negative: other pages in the same labeled document

`signer_pages.csv`

- target: `target_signer_page`
- positive: first page containing labeled `SIGNER_NAME` or `SIGNER_ROLE`
- negative: pages before that first labeled signer page
- pages after the first labeled signer page are outside scope and are not rows

## Features

Doc start features:

- `regime_score`
- `issue_org_score`
- `doc_number_score`
- `place_date_score`
- `subject_score`

Signer page features:

- `recipients_score`
- `signer_role_score`
- `signer_name_score`
- `has_noi_nhan_regex`
- `has_tm_kt_tl_tuq_regex`
- `relative_page_position`
