#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/getraenkeautomat"
SERVICE_USER="${SUDO_USER:-$(whoami)}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "=== Installing FF Getraenkeautomat on Raspberry Pi (user: ${SERVICE_USER}) ==="

echo "[1/5] Installing precompiled Debian/Raspberry Pi OS Python & OLED packages..."
sudo apt-get update
sudo apt-get install -y \
  python3-venv \
  python3-fastapi \
  python3-uvicorn \
  python3-httpx \
  python3-serial \
  python3-evdev \
  python3-pydantic \
  i2c-tools \
  python3-pil \
  python3-smbus2 \
  python3-luma.oled \
  python3-gpiod \
  gpiod \
  fonts-dejavu-core

echo "[2/5] Enabling I2C bus and granting '${SERVICE_USER}' access (input, dialout, i2c, gpio)..."
sudo raspi-config nonint do_i2c 0
sudo usermod -aG input,dialout,i2c,gpio "${SERVICE_USER}"

echo "[3/5] Copying application to ${APP_DIR}..."
sudo mkdir -p "${APP_DIR}/data"
sudo rsync -a --delete \
  --exclude '.venv' \
  --exclude 'data' \
  --exclude '.env' \
  --exclude '__pycache__' \
  "${SRC_DIR}/" "${APP_DIR}/" 2>/dev/null || {
    sudo cp -r "${SRC_DIR}/app" "${SRC_DIR}/deploy" "${SRC_DIR}/requirements.txt" "${SRC_DIR}/pyproject.toml" "${SRC_DIR}/.env.example" "${APP_DIR}/"
}
sudo chown -R "${SERVICE_USER}:${SERVICE_USER}" "${APP_DIR}"

if [ ! -f "${APP_DIR}/.env" ]; then
  cp "${APP_DIR}/.env.example" "${APP_DIR}/.env"
  chown "${SERVICE_USER}:${SERVICE_USER}" "${APP_DIR}/.env"
  echo "Created ${APP_DIR}/.env from .env.example"
fi

echo "[4/5] Creating virtualenv (--system-site-packages for fast 32-bit ARM startup)..."
sudo -u "${SERVICE_USER}" python3 -m venv --system-site-packages "${APP_DIR}/.venv"

echo "[5/5] Installing and starting early-boot OLED splash + main systemd services..."
sed "s/__SERVICE_USER__/${SERVICE_USER}/g" "${APP_DIR}/deploy/getraenkeautomat-boot-oled.service" \
  | sudo tee /etc/systemd/system/getraenkeautomat-boot-oled.service >/dev/null
sed "s/__SERVICE_USER__/${SERVICE_USER}/g" "${APP_DIR}/deploy/getraenkeautomat.service" \
  | sudo tee /etc/systemd/system/getraenkeautomat.service >/dev/null

sudo systemctl daemon-reload
sudo systemctl enable getraenkeautomat-boot-oled.service
sudo systemctl enable getraenkeautomat.service
sudo systemctl restart getraenkeautomat.service

echo ""
echo "=== I2C Bus Scan (/dev/i2c-1 — OLED usually shows '3c'): ==="
sudo i2cdetect -y 1 || true
echo ""
echo "=== Done! Service status: ==="
sudo systemctl --no-pager --full status getraenkeautomat.service || true
echo ""
echo "Dashboard & Simulator UI: http://$(hostname -I | awk '{print $1}'):8000"
