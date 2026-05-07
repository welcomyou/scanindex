# `train_lightgbm/`

Pipeline KIE mới theo hướng `field-wise LightGBM + schema decoder`.

## Mục tiêu

- Tận dụng trực tiếp `json_input/`, `json_output_labeled/`, `ocr/*.json` của project `train_kie`
- Không dùng token BIO
- Train scorer riêng cho từng field trên candidate vùng
- Decode cuối theo cardinality của ontology

## Entry points

0. [0-create_kie_subset.py](/D:/App/ocrtool/train_lightgbm/0-create_kie_subset.py)
1. [1-setup_project.py](/D:/App/ocrtool/train_lightgbm/1-setup_project.py)
2. [2-build_fieldwise_dataset.py](/D:/App/ocrtool/train_lightgbm/2-build_fieldwise_dataset.py)
3. [3-train_field_models.py](/D:/App/ocrtool/train_lightgbm/3-train_field_models.py)
4. [4-evaluate_models.py](/D:/App/ocrtool/train_lightgbm/4-evaluate_models.py)
5. [5-run_field_inference.py](/D:/App/ocrtool/train_lightgbm/5-run_field_inference.py)

## Dữ liệu export

- `exports/fieldwise/<FIELD>/<split>.jsonl`
- `exports/ground_truth/<split>.jsonl`
- `reports/dataset_build_report.json`

## RunPod quick start

```bash
bash /workspace/ocrtool/train_lightgbm/fullrun/1_setup_env.sh
bash /workspace/ocrtool/train_lightgbm/fullrun/2_build_dataset.sh /workspace/Train_20260413_143844_kie /workspace/Train_20260413_143844_LightGBM
bash /workspace/ocrtool/train_lightgbm/fullrun/3_train.sh /workspace/Train_20260413_143844_LightGBM
bash /workspace/ocrtool/train_lightgbm/fullrun/4_eval.sh /workspace/Train_20260413_143844_LightGBM
```

## Dryrun20 one-shot

```bash
LIGHTGBM_MAX_WORKERS=8 bash /workspace/ocrtool/train_lightgbm/fullrun/5_dryrun20.sh \
  /workspace/Train_20260413_143844_kie_dryrun20 \
  /workspace/Train_20260413_143844_LightGBM_dryrun20
```
