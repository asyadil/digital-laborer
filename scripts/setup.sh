#!/usr/bin/env bash
set -euo pipefail

# Basic setup for Linux deployment
PYTHON_BIN=${PYTHON_BIN:-python3}
VENV_DIR=${VENV_DIR:-.venv}
CONFIG_PATH=${CONFIG_PATH:-config/config.yaml}

# Create virtualenv
if [ ! -d "$VENV_DIR" ]; then
  $PYTHON_BIN -m venv "$VENV_DIR"
fi
source "$VENV_DIR/bin/activate"

# Upgrade pip
pip install --upgrade pip

# Install dependencies
if [ -f requirements.txt ]; then
  pip install -r requirements.txt
fi

# Ensure config exists
if [ ! -f "$CONFIG_PATH" ]; then
  echo "Missing $CONFIG_PATH. Copy sample and edit secrets." >&2
  exit 1
fi

echo "Setup complete. Activate with: source $VENV_DIR/bin/activate"
