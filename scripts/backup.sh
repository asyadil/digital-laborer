#!/usr/bin/env bash
set -euo pipefail

APP_DIR=${APP_DIR:-/opt/referral-bot}
BACKUP_DIR=${BACKUP_DIR:-/var/backups/referral-bot}
TS=$(date +%Y%m%d-%H%M%S)
DB_PATH=${DB_PATH:-$APP_DIR/data/referral.db}
CONFIG_PATH=${CONFIG_PATH:-$APP_DIR/config/config.yaml}

mkdir -p "$BACKUP_DIR"

if [ -f "$DB_PATH" ]; then
  cp "$DB_PATH" "$BACKUP_DIR/referral-$TS.db"
fi

if [ -f "$CONFIG_PATH" ]; then
  cp "$CONFIG_PATH" "$BACKUP_DIR/config-$TS.yaml"
fi

echo "Backup complete at $BACKUP_DIR"
