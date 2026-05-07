#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="${1:?lightgbm project root required}"

source /workspace/ocrtool/.venv_lightgbm/bin/activate
python /workspace/ocrtool/train_lightgbm/3-train_field_models.py \
  --project-root "$PROJECT_ROOT"
