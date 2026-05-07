#!/usr/bin/env bash
set -euo pipefail

SOURCE_PROJECT_ROOT="${1:?source kie project root required}"
PROJECT_ROOT="${2:?lightgbm project root required}"

source /workspace/ocrtool/.venv_lightgbm/bin/activate
MAX_WORKERS="${LIGHTGBM_MAX_WORKERS:-0}"
python /workspace/ocrtool/train_lightgbm/1-setup_project.py \
  --source-project-root "$SOURCE_PROJECT_ROOT" \
  --project-root "$PROJECT_ROOT"
python /workspace/ocrtool/train_lightgbm/2-build_fieldwise_dataset.py \
  --project-root "$PROJECT_ROOT" \
  --max-workers "$MAX_WORKERS"
