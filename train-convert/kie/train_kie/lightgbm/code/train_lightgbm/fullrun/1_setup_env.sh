#!/usr/bin/env bash
set -euo pipefail

ROOT="/workspace/ocrtool"
VENV="$ROOT/.venv_lightgbm"

python -m venv "$VENV"
source "$VENV/bin/activate"
python -m pip install --upgrade pip wheel setuptools
python -m pip install -r /workspace/ocrtool/train_lightgbm/requirements-train-lightgbm.txt
