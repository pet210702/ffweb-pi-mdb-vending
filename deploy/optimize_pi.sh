#!/usr/bin/env bash
set -euo pipefail

echo "=== Applying Raspberry Pi 1 Performance & Fast-Boot Tweaks ==="

# 1. Disable cloud-init (only needed on initial image bootstrap)
echo "[1/5] Disabling cloud-init..."
sudo touch /etc/cloud/cloud-init.disabled 2>/dev/null || true
sudo systemctl disable --now cloud-init.service cloud-init-local.service cloud-config.service cloud-final.service 2>/dev/null || true

# 2. Disable NetworkManager-wait-online (prevents 2-minute DHCP block on boot)
echo "[2/5] Disabling network-wait blocking..."
sudo systemctl disable --now NetworkManager-wait-online.service 2>/dev/null || true

# 3. Disable services without hardware on Pi 1 B+
echo "[3/5] Disabling unused Bluetooth, Wi-Fi, and swap resize services..."
sudo systemctl disable --now hciuart.service bluetooth.service wpa_supplicant.service 2>/dev/null || true
sudo systemctl disable --now rpi-resize-swap-file.service 2>/dev/null || true

# 4. Configure safe Turbo overclock for BCM2835 (active cooling verified)
echo "[4/5] Configuring Turbo Overclock (1000 MHz CPU, 500 MHz RAM)..."
CONFIG="/boot/firmware/config.txt"
if ! grep -q "arm_freq=1000" "$CONFIG"; then
  sudo tee -a "$CONFIG" >/dev/null << 'EOF'

# --- Fast-Boot & Overclock for Raspberry Pi 1 B+ (Active Cooling) ---
arm_freq=1000
core_freq=500
sdram_freq=500
over_voltage=6
EOF
  echo "Added overclock settings to $CONFIG"
else
  echo "Overclock already present in $CONFIG"
fi

# 5. Optimize getraenkeautomat.service dependencies (don't wait for network-online)
echo "[5/5] Updating getraenkeautomat.service dependencies..."
SERVICE_FILE="/etc/systemd/system/getraenkeautomat.service"
if [ -f "$SERVICE_FILE" ]; then
  sudo sed -i 's/After=network-online.target/After=network.target/g' "$SERVICE_FILE"
  sudo sed -i '/Wants=network-online.target/d' "$SERVICE_FILE"
  sudo systemctl daemon-reload
fi

echo ""
echo "=== Tweaks applied successfully! ==="
echo "Run 'sudo reboot' to boot with the 1000 MHz profile and trimmed services."
