#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME=${SERVICE_NAME:-referral-bot}
APP_DIR=${APP_DIR:-/opt/referral-bot}
VENV_DIR=${VENV_DIR:-$APP_DIR/.venv}
PYTHON_BIN=${PYTHON_BIN:-python3}
CONFIG_PATH=${CONFIG_PATH:-$APP_DIR/config/config.yaml}
LOG_FILE=${LOG_FILE:-$APP_DIR/logs/referral.log}

mkdir -p "$APP_DIR" "$APP_DIR/logs"

# Sync code (assumes rsync/scp done externally)
# This script focuses on service wiring

# Create virtualenv
if [ ! -d "$VENV_DIR" ]; then
  $PYTHON_BIN -m venv "$VENV_DIR"
fi
source "$VENV_DIR/bin/activate"

# Install dependencies
if [ -f "$APP_DIR/requirements.txt" ]; then
  pip install --upgrade pip
  pip install -r "$APP_DIR/requirements.txt"
fi

# Validate config
if [ ! -f "$CONFIG_PATH" ]; then
  echo "Missing config at $CONFIG_PATH" >&2
  exit 1
fi

# Create systemd unit
cat <<'EOF' | sudo tee /etc/systemd/system/$SERVICE_NAME.service >/dev/null
[Unit]
Description=Referral Automation Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=%h/%HOLDER%
ExecStart=%h/%HOLDER%/.venv/bin/python -m src.main
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

# Replace placeholders with actual app dir
sudo sed -i "s|%h/%HOLDER%|$APP_DIR|g" /etc/systemd/system/$SERVICE_NAME.service

sudo systemctl daemon-reload
sudo systemctl enable $SERVICE_NAME
sudo systemctl restart $SERVICE_NAME

echo "Deployment completed. Logs: $LOG_FILE (systemd journal also available)."
