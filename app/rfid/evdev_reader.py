from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from app.events import EventBus

logger = logging.getLogger(__name__)

try:
    import evdev  # type: ignore[import-not-found]
    from evdev import ecodes  # type: ignore[import-not-found]
except ImportError:
    evdev = None
    ecodes = None

RfidScanCallback = Callable[[str, str], Awaitable[None]]

# Linux evdev keycode name -> character mapping for HID keyboard-wedge RFID readers
_KEY_CHAR_MAP: dict[str, str] = {
    "KEY_0": "0",
    "KEY_1": "1",
    "KEY_2": "2",
    "KEY_3": "3",
    "KEY_4": "4",
    "KEY_5": "5",
    "KEY_6": "6",
    "KEY_7": "7",
    "KEY_8": "8",
    "KEY_9": "9",
    "KEY_KP0": "0",
    "KEY_KP1": "1",
    "KEY_KP2": "2",
    "KEY_KP3": "3",
    "KEY_KP4": "4",
    "KEY_KP5": "5",
    "KEY_KP6": "6",
    "KEY_KP7": "7",
    "KEY_KP8": "8",
    "KEY_KP9": "9",
    "KEY_A": "A",
    "KEY_B": "B",
    "KEY_C": "C",
    "KEY_D": "D",
    "KEY_E": "E",
    "KEY_F": "F",
}

_RFID_KEYWORDS = (
    "rfid",
    "sycreader",
    "syc",
    "ic reader",
    "id&ic",
    "card reader",
    "smart card",
    "mifare",
    "nfc",
    "08ff",
    "ffff:0035",
)


def list_linux_input_devices() -> list[dict[str, Any]]:
    """Return available Linux /dev/input devices (empty list on macOS or if inaccessible)."""
    if evdev is None:
        return []

    by_id_map: dict[str, str] = {}
    by_id_dir = Path("/dev/input/by-id")
    if by_id_dir.is_dir():
        for link in by_id_dir.iterdir():
            try:
                target = str(link.resolve())
                by_id_map[target] = str(link)
            except OSError:
                continue

    results: list[dict[str, Any]] = []
    for path in evdev.list_devices():
        try:
            dev = evdev.InputDevice(path)
            resolved = str(Path(path).resolve())
            results.append(
                {
                    "path": path,
                    "by_id": by_id_map.get(resolved),
                    "name": dev.name,
                    "phys": dev.phys,
                    "vendor_id": f"{dev.info.vendor:04x}",
                    "product_id": f"{dev.info.product:04x}",
                }
            )
            dev.close()
        except Exception:
            continue
    return results


def auto_detect_rfid_device() -> str | None:
    """Best-effort auto-discovery of a USB HID RFID chip reader on Linux."""
    for info in list_linux_input_devices():
        haystack = " ".join(
            [
                str(info.get("name") or ""),
                str(info.get("by_id") or ""),
                f"{info.get('vendor_id')}:{info.get('product_id')}",
            ]
        ).lower()
        if any(k in haystack for k in _RFID_KEYWORDS):
            return str(info.get("by_id") or info["path"])
    return None


class RfidReaderManager:
    """
    Manages physical USB HID RFID reading (via Linux evdev) alongside simulated scans.
    """

    def __init__(
        self,
        mode: str = "auto",
        configured_device_path: str = "",
        events: EventBus | None = None,
    ) -> None:
        self.mode = mode  # "auto" | "evdev" | "simulator"
        self.configured_device_path = configured_device_path
        self._events = events
        self._on_scan: RfidScanCallback | None = None
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._active_device_path: str | None = None
        self._active_device_name: str | None = None
        self._connected = False
        self._simulate_disconnected = False
        self._last_notified_connected: bool | None = None
        self._last_uid: str | None = None
        self._last_error: str | None = None

    def _notify_status_change(self, connected: bool, error: str | None = None) -> None:
        if self._last_notified_connected == connected:
            return
        self._last_notified_connected = connected
        if self._events is not None:
            self._events.publish(
                "rfid_status_changed",
                {
                    "connected": connected,
                    "device_path": self._active_device_path,
                    "device_name": self._active_device_name,
                    "error": error,
                },
            )

    def set_simulated_disconnect(self, disconnected: bool) -> None:
        """Simulate USB RFID disconnect/reconnect for testing."""
        self._simulate_disconnected = disconnected
        if disconnected:
            self._last_error = "USB RFID-Leser getrennt (Simulation)"
            self._notify_status_change(False, self._last_error)
        else:
            self._last_error = None
            is_conn = True if self.mode == "simulator" else self._connected
            self._notify_status_change(is_conn, None)

    async def start(self, on_scan: RfidScanCallback) -> None:
        self._on_scan = on_scan
        self._running = True
        if self.mode in {"auto", "evdev"} and evdev is not None:
            self._task = asyncio.create_task(self._evdev_loop(), name="rfid-evdev-loop")
            await asyncio.sleep(0.05)
        elif self.mode == "evdev" and evdev is None:
            self._last_error = "python-evdev is only available on Linux"
            self._notify_status_change(False, self._last_error)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def inject_simulated_scan(self, chip_uid: str) -> str:
        normalized = chip_uid.strip().upper()
        if not normalized:
            raise ValueError("chip_uid must not be empty")
        self._last_uid = normalized
        if self._on_scan:
            await self._on_scan(normalized, "simulator")
        return normalized

    async def _evdev_loop(self) -> None:
        assert evdev is not None and ecodes is not None
        while self._running:
            dev = None
            try:
                target_path = self.configured_device_path or auto_detect_rfid_device()
                if not target_path:
                    self._connected = False
                    self._active_device_path = None
                    self._active_device_name = None
                    self._last_error = "Kein USB RFID-Leser erkannt"
                    self._notify_status_change(False, self._last_error)
                    await asyncio.sleep(1.5)
                    continue

                dev = evdev.InputDevice(target_path)
                try:
                    dev.grab()
                except OSError as grab_err:
                    logger.warning("Could not grab exclusive input on %s: %s", target_path, grab_err)

                self._connected = True
                self._active_device_path = target_path
                self._active_device_name = dev.name
                self._last_error = None
                logger.info("Connected USB RFID reader: %s (%s)", dev.name, target_path)
                self._notify_status_change(True, None)

                buffer: list[str] = []
                async for event in dev.async_read_loop():
                    if not self._running:
                        break
                    if event.type != ecodes.EV_KEY:
                        continue
                    # Value 1 == key down (ignore key up == 0 and key hold == 2)
                    if event.value != 1:
                        continue

                    key_code = ecodes.KEY.get(event.code)
                    if isinstance(key_code, list):
                        key_name = key_code[0]
                    else:
                        key_name = str(key_code or "")

                    if key_name in {"KEY_ENTER", "KEY_KPENTER"}:
                        uid = "".join(buffer).strip().upper()
                        buffer.clear()
                        if uid:
                            self._last_uid = uid
                            logger.info("RFID chip scanned via USB: %s", uid)
                            if self._on_scan:
                                await self._on_scan(uid, "usb_evdev")
                    elif key_name in _KEY_CHAR_MAP:
                        buffer.append(_KEY_CHAR_MAP[key_name])

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._connected = False
                self._active_device_path = None
                self._active_device_name = None
                self._last_error = str(exc)
                logger.warning("USB RFID reader disconnected or failed (%s); retrying in 1.5s...", exc)
                self._notify_status_change(False, self._last_error)
                await asyncio.sleep(1.5)
            finally:
                if dev is not None:
                    try:
                        dev.ungrab()
                    except Exception:
                        pass
                    try:
                        dev.close()
                    except Exception:
                        pass

    def status_snapshot(self) -> dict[str, Any]:
        hw_connected = False if self._simulate_disconnected else self._connected
        return {
            "mode": self.mode,
            "evdev_supported": evdev is not None,
            "configured_device_path": self.configured_device_path or None,
            "active_device_path": None if self._simulate_disconnected else self._active_device_path,
            "active_device_name": None if self._simulate_disconnected else self._active_device_name,
            "hardware_connected": hw_connected,
            "simulate_disconnected": self._simulate_disconnected,
            "last_scanned_uid": self._last_uid,
            "last_error": self._last_error,
        }
