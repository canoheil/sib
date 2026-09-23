#!/usr/bin/env bash
set -euo pipefail

export PYTHONPATH="${PYTHONPATH:-/home/user}:$(pwd)/..:."

# Goje Production Deployment Script
APP_DIR="${APP_DIR:-/opt/goje}"
SERVICE_NAME="goje.service"

echo "🍅 [1/5] Starting Goje production deployment..."

cd "$APP_DIR"

echo "🍅 [2/5] Installing/Updating python dependencies..."
if [ -d "venv" ]; then
    source venv/bin/activate
fi
pip install --upgrade pip
pip install -r requirements.txt

echo "🍅 [3/5] Applying safe database migrations (Alembic)..."
alembic upgrade head

echo "🍅 [4/5] Running automated verification test suite..."
python3 -m pytest -q

echo "🍅 [5/5] Restarting systemd service..."
if command -v systemctl >/dev/null 2>&1; then
    sudo systemctl restart "$SERVICE_NAME"
    echo "✅ Goje service successfully restarted!"
    sudo systemctl status "$SERVICE_NAME" --no-pager
else
    echo "⚠️ systemctl not found. Please restart bot process manually."
fi

echo "🍅 Deployment completed successfully!"
