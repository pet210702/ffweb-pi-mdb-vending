from __future__ import annotations

import time
import urllib.request
from pathlib import Path

from app.oled.sysinfo import (
    HANDOFF_FLAG_PATH,
    check_qibixx_serial_present,
    check_usb_rfid_present,
    get_cpu_temp_str,
    get_lan_ip,
    get_uptime_seconds,
)


def _api_already_listening() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/api/status", timeout=0.4) as resp:
            return resp.status == 200
    except Exception:
        return False


def _load_small_font(bold: bool, size: int):
    from PIL import ImageFont  # type: ignore[import-not-found]

    path = (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    )
    if Path(path).is_file():
        try:
            return ImageFont.truetype(path, size=size)
        except Exception:
            pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def main() -> int:
    # If the main FastAPI daemon is already up, do not touch the OLED
    if _api_already_listening():
        return 0

    try:
        HANDOFF_FLAG_PATH.unlink(missing_ok=True)
    except OSError:
        pass

    # Wait up to 15s for /dev/i2c-1 device node during early kernel udev init
    i2c_node = Path("/dev/i2c-1")
    for _ in range(30):
        if i2c_node.exists():
            break
        time.sleep(0.5)
    if not i2c_node.exists():
        return 0

    try:
        from luma.core.interface.serial import i2c as luma_i2c  # type: ignore[import-not-found]
        from luma.oled.device import ssd1306  # type: ignore[import-not-found]
        from PIL import Image, ImageDraw  # type: ignore[import-not-found]
    except ImportError:
        return 0

    device = None
    for addr in (0x3C, 0x3D):
        try:
            serial = luma_i2c(port=1, address=addr)
            device = ssd1306(serial, width=128, height=64)
            # Prevent luma.oled from turning off the screen when this splash process exits!
            device.cleanup = lambda: None
            device.contrast(255)
            break
        except Exception:
            device = None

    if device is None:
        return 0

    font_hdr = _load_small_font(bold=True, size=9)
    font_row = _load_small_font(bold=False, size=9)
    spinner = ("|", "/", "-", "\\")
    start_ts = time.monotonic()

    # Update live boot progress until main service signals handoff (max 150s safety bound)
    step = 0
    while (time.monotonic() - start_ts) < 150.0:
        if HANDOFF_FLAG_PATH.exists():
            break

        up = get_uptime_seconds()
        ip = get_lan_ip() or "DHCP warte..."
        rfid_txt = "OK" if check_usb_rfid_present() else "fehlt"
        mdb_txt = "USB" if check_qibixx_serial_present() else "SIM"
        temp = get_cpu_temp_str()
        spin = spinner[step % len(spinner)]
        dots = "." * ((step % 3) + 1)

        img = Image.new("1", (128, 64), 0)
        draw = ImageDraw.Draw(img)

        draw.rectangle((0, 0, 127, 13), outline=1, fill=1)
        hdr = f"SYSTEM BOOT {spin} {up}s"
        draw.text((4, 1), hdr, font=font_hdr, fill=0)

        draw.text((2, 16), f"IP: {ip}"[:22], font=font_row, fill=1)
        draw.text((2, 30), f"RFID: {rfid_txt}  |  MDB: {mdb_txt}", font=font_row, fill=1)
        temp_suffix = f" ({temp})" if temp else ""
        draw.text((2, 44), f"API startet{dots}{temp_suffix}", font=font_row, fill=1)

        try:
            device.display(img)
        except Exception:
            break

        step += 1
        for _ in range(8):
            if HANDOFF_FLAG_PATH.exists():
                return 0
            time.sleep(0.1)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
