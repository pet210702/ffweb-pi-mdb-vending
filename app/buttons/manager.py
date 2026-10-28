from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
import threading
import time
from typing import Any, Awaitable, Callable

from app.buttons import ButtonAction
from app.events import EventBus

logger = logging.getLogger(__name__)

try:
    import gpiod  # type: ignore[import-not-found]
    from gpiod.line import Bias, Direction, Edge, Value  # type: ignore[import-not-found]
except ImportError:
    gpiod = None
    Bias = None
    Direction = None
    Edge = None
    Value = None

ButtonCallback = Callable[[ButtonAction], Awaitable[None]]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ButtonManager:
    """
    Manages the 3 physical hardware push-buttons hooked up to the Raspberry Pi:
      - Button 1 (Left):   GPIO 17 (Pin 11) -> BACK / CANCEL
      - Button 2 (Middle): GPIO 27 (Pin 13) -> SELECT / OK
      - Button 3 (Right):  GPIO 22 (Pin 15) -> NEXT / CYCLE

    Configured active-low with internal pull-up (PULL_UP). Pressing shorts to GND (FALLING edge).
    Provides automatic gpiod hardware monitoring with threadsafe dispatch to asyncio,
    plus API simulation support for development/testing without hardware.
    """

    def __init__(
        self,
        *,
        events: EventBus,
        mode: str = "auto",  # "auto" | "gpiod" | "simulator" | "disabled"
        chip_path: str = "/dev/gpiochip0",
        pin_back: int = 17,
        pin_select: int = 27,
        pin_next: int = 22,
        debounce_ms: int = 50,
    ) -> None:
        self._events = events
        self.mode = mode.strip().lower()
        self.chip_path = chip_path
        self.pin_back = pin_back
        self.pin_select = pin_select
        self.pin_next = pin_next
        self.debounce_ms = debounce_ms

        self._connected = False
        self._driver_name: str | None = None
        self._last_error: str | None = None
        self._last_pressed: dict[str, Any] | None = None

        self._line_request: Any = None
        self._stop_event = threading.Event()
        self._worker_thread: threading.Thread | None = None
        self._callback: ButtonCallback | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

        # Software debounce cache: pin -> last monotonic timestamp
        self._last_press_ts: dict[int, float] = {}
        self._last_combo_ts: float = 0.0

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def start(self, on_button: ButtonCallback) -> None:
        self._callback = on_button
        self._loop = asyncio.get_running_loop()

        if self.mode == "disabled":
            self._driver_name = "disabled"
            logger.info("ButtonManager disabled by configuration")
            return

        if self.mode in {"auto", "gpiod"}:
            if self._try_init_hardware():
                return

        # Fallback to simulator
        self._connected = False
        self._driver_name = "simulator"
        logger.info("ButtonManager running in simulator mode (hardware not connected)")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=1.5)
        self._worker_thread = None

        if self._line_request is not None:
            try:
                self._line_request.release()
            except Exception as exc:
                logger.debug("Error releasing GPIO line request: %s", exc)
            self._line_request = None

        self._connected = False

    def _try_init_hardware(self) -> bool:
        if gpiod is None or Direction is None or Bias is None or Edge is None:
            self._last_error = "Python gpiod bindings not installed"
            return False

        if not Path(self.chip_path).exists():
            self._last_error = f"{self.chip_path} does not exist"
            return False

        try:
            line_settings = gpiod.LineSettings(
                direction=Direction.INPUT,
                bias=Bias.PULL_UP,
                edge_detection=Edge.FALLING,
            )
            pins = (self.pin_back, self.pin_select, self.pin_next)
            request = gpiod.request_lines(
                self.chip_path,
                consumer="getraenkeautomat-buttons",
                config={pins: line_settings},
            )
            self._line_request = request
            self._connected = True
            self._driver_name = f"gpiod@{self.chip_path}"
            self._last_error = None

            self._stop_event.clear()
            self._worker_thread = threading.Thread(
                target=self._monitor_lines,
                name="gpio-button-monitor",
                daemon=True,
            )
            self._worker_thread.start()

            logger.info(
                "GPIO buttons initialized on %s: Back=GPIO%d, Select=GPIO%d, Next=GPIO%d",
                self.chip_path,
                self.pin_back,
                self.pin_select,
                self.pin_next,
            )
            return True
        except Exception as exc:
            self._last_error = str(exc)
            logger.warning("Failed to initialize hardware GPIO buttons (%s)", exc)
            return False

    def _monitor_lines(self) -> None:
        """Background thread polling wait_edge_events."""
        req = self._line_request
        poll_interval = timedelta(milliseconds=200)
        debounce_window_sec = max(0.28, (self.debounce_ms / 1000.0) * 2)

        def _is_grounded(p: int) -> bool:
            try:
                v = req.get_value(p)
                return (
                    v == 0
                    or (Value is not None and v == Value.INACTIVE)
                    or str(v) in ("Value.INACTIVE", "0")
                )
            except Exception as exc:
                logger.debug("Error reading pin %d value: %s", p, exc)
                return False

        while not self._stop_event.is_set() and req is not None:
            try:
                has_event = req.wait_edge_events(poll_interval)
                if not has_event or self._stop_event.is_set():
                    continue

                events = req.read_edge_events()

                for ev in events:
                    offset = ev.line_offset
                    now_mono = time.monotonic()

                    # 1. Ignore if within software debounce cooldown for this specific pin
                    last_ts = self._last_press_ts.get(offset, 0.0)
                    if (now_mono - last_ts) < debounce_window_sec:
                        continue

                    # 2. Ignore if a combo was triggered very recently (within 700ms)
                    if (now_mono - self._last_combo_ts) < 0.7:
                        continue

                    # 3. Reject release bounces or noise: with PULL_UP, active press MUST be grounded (LOW)
                    if not _is_grounded(offset):
                        continue

                    # Start debounce cooldown window
                    self._last_press_ts[offset] = now_mono

                    # Check for combo: 1st button (pin_back) and 3rd button (pin_next) pressed together
                    if offset in (self.pin_back, self.pin_next):
                        other_pin = self.pin_next if offset == self.pin_back else self.pin_back
                        is_combo = False

                        if _is_grounded(other_pin):
                            is_combo = True
                        else:
                            # Brief 60ms window to catch near-simultaneous physical press
                            time.sleep(0.06)
                            if _is_grounded(other_pin):
                                is_combo = True

                        if is_combo:
                            combo_ts = time.monotonic()
                            self._last_combo_ts = combo_ts
                            # Lock out both pins for 700ms so releasing the combo cannot fire single presses
                            self._last_press_ts[self.pin_back] = combo_ts + 0.4
                            self._last_press_ts[self.pin_next] = combo_ts + 0.4

                            self._record_press(ButtonAction.COMBO_1_3, None, source="hardware")
                            if self._loop and self._callback:
                                self._loop.call_soon_threadsafe(
                                    asyncio.create_task,
                                    self._dispatch_action(ButtonAction.COMBO_1_3),
                                )
                            continue

                    action = self._offset_to_action(offset)
                    if action is None:
                        continue

                    self._record_press(action, offset, source="hardware")
                    if self._loop and self._callback:
                        self._loop.call_soon_threadsafe(
                            asyncio.create_task,
                            self._dispatch_action(action),
                        )
            except Exception as exc:
                if not self._stop_event.is_set():
                    logger.warning("Error reading GPIO edge events: %s", exc)
                    time.sleep(0.1)

    def _offset_to_action(self, offset: int) -> ButtonAction | None:
        if offset == self.pin_back:
            return ButtonAction.BACK
        if offset == self.pin_select:
            return ButtonAction.SELECT
        if offset == self.pin_next:
            return ButtonAction.NEXT
        return None

    def _record_press(self, action: ButtonAction, pin: int | None, source: str) -> None:
        self._last_pressed = {
            "button": action.value,
            "pin": pin,
            "source": source,
            "pressed_at": _utc_now_iso(),
        }
        self._events.publish(
            "button_pressed",
            {
                "button": action.value,
                "pin": pin,
                "source": source,
                "timestamp": self._last_pressed["pressed_at"],
            },
        )

    async def _dispatch_action(self, action: ButtonAction) -> None:
        if self._callback:
            try:
                await self._callback(action)
            except Exception as exc:
                logger.error("Error executing button callback for %s: %s", action.value, exc)

    async def trigger_button(self, action: ButtonAction | str) -> dict[str, Any]:
        """Programmatically trigger a button action (e.g. from Web Dashboard simulator)."""
        act = ButtonAction(action) if isinstance(action, str) else action
        pin_map = {
            ButtonAction.BACK: self.pin_back,
            ButtonAction.SELECT: self.pin_select,
            ButtonAction.NEXT: self.pin_next,
            ButtonAction.COMBO_1_3: None,
        }
        pin = pin_map.get(act)
        self._record_press(act, pin, source="simulated")
        await self._dispatch_action(act)
        return {
            "button": act.value,
            "pin": pin,
            "source": "simulated",
            "last_pressed": self._last_pressed,
        }

    def status_snapshot(self) -> dict[str, Any]:
        live_values: dict[str, Any] = {}
        if self._line_request is not None:
            try:
                for name, pin in [("back", self.pin_back), ("select", self.pin_select), ("next", self.pin_next)]:
                    v = self._line_request.get_value(pin)
                    is_low = (v == 0 or (Value is not None and v == Value.INACTIVE) or str(v) in ("Value.INACTIVE", "0"))
                    live_values[name] = {
                        "pin": pin,
                        "raw": str(v),
                        "state": "PRESSED/LOW" if is_low else "RELEASED/HIGH",
                    }
            except Exception as e:
                live_values["error"] = str(e)

        return {
            "mode": self.mode,
            "driver": self._driver_name,
            "connected": self._connected,
            "worker_alive": self._worker_thread.is_alive() if self._worker_thread else False,
            "chip_path": self.chip_path,
            "pins": {
                "back": self.pin_back,
                "select": self.pin_select,
                "next": self.pin_next,
            },
            "pin_values": live_values,
            "debounce_ms": self.debounce_ms,
            "last_pressed": self._last_pressed,
            "last_error": self._last_error,
        }
