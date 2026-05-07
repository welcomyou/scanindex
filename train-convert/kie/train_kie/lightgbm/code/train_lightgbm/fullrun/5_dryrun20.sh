#!/usr/bin/env bash
set -euo pipefail

SOURCE_PROJECT_ROOT="${1:?dryrun20 kie subset root required}"
PROJECT_ROOT="${2:?dryrun20 lightgbm project root required}"
MAX_WORKERS="${LIGHTGBM_MAX_WORKERS:-0}"

bash /workspace/ocrtool/train_lightgbm/fullrun/1_setup_env.sh

source /workspace/ocrtool/.venv_lightgbm/bin/activate
python /workspace/ocrtool/train_lightgbm/1-setup_project.py \
  --source-project-root "$SOURCE_PROJECT_ROOT" \
  --project-root "$PROJECT_ROOT"
python /workspace/ocrtool/train_lightgbm/2-build_fieldwise_dataset.py \
  --project-root "$PROJECT_ROOT" \
  --max-workers "$MAX_WORKERS"
python /workspace/ocrtool/train_lightgbm/3-train_field_models.py \
  --project-root "$PROJECT_ROOT"
python /workspace/ocrtool/train_lightgbm/4-evaluate_models.py \
  --project-root "$PROJECT_ROOT"
