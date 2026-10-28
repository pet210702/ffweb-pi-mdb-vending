from __future__ import annotations

import socket
from pathlib import Path


HANDOFF_FLAG_PATH = Path("/tmp/getraenkeautomat-oled.ready")


def get_lan_ip() -> str:
    """Return primary LAN IPv4 address without sending network traffic."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 1))
        ip = sock.getsockname()[0]
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass
    finally:
        sock.close()
    return ""


def get_cpu_temp_str() -> str:
    """Read Raspberry Pi SoC temperature from sysfs if available."""
    thermal = Path("/sys/class/thermal/thermal_zone0/temp")
    try:
        if thermal.is_file():
            milli_c = int(thermal.read_text(encoding="utf-8").strip())
            return f"{milli_c / 1000.0:.0f}C"
    except Exception:
        pass
    return ""


def get_uptime_seconds() -> int:
    uptime_file = Path("/proc/uptime")
    try:
        if uptime_file.is_file():
            first = uptime_file.read_text(encoding="utf-8").split()[0]
            return int(float(first))
    except Exception:
        pass
    return 0


def check_usb_rfid_present() -> bool:
    by_id = Path("/dev/input/by-id")
    if not by_id.is_dir():
        return False
    keywords = ("rfid", "ic_reader", "syc", "card", "08ff", "0035", "nfc")
    for entry in by_id.iterdir():
        if any(k in entry.name.lower() for k in keywords):
            return True
    return False


def check_qibixx_serial_present(port: str = "/dev/ttyACM0") -> bool:
    return Path(port).exists()
