from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.db import cents_to_eur_str
from app.events import EventBus
from app.oled.sysinfo import HANDOFF_FLAG_PATH, get_cpu_temp_str, get_lan_ip

logger = logging.getLogger(__name__)

try:
    from luma.core.interface.serial import i2c as luma_i2c  # type: ignore[import-not-found]
    from luma.oled.device import sh1106, ssd1306  # type: ignore[import-not-found]
except ImportError:
    luma_i2c = None
    ssd1306 = None
    sh1106 = None

try:
    from PIL import Image, ImageDraw, ImageFont  # type: ignore[import-not-found]
except ImportError:
    Image = None
    ImageDraw = None
    ImageFont = None

_FONT_CANDIDATES_REGULAR = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
)
_FONT_CANDIDATES_BOLD = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
)


class OledDisplayManager:
    """
    Drives a 4-pin 0.96" 128x64 I2C OLED display (VCC, GND, SCL, SDA).
    Coordinates handoff from the early-boot splash script, shows a startup
    diagnostic summary before entering the user UI, auto-scales text to fit
    128px width without clipping, and sleeps after inactivity to prevent burn-in.
    """

    def __init__(
        self,
        *,
        events: EventBus,
        mode: str = "auto",  # "auto" | "ssd1306" | "sh1106" | "disabled"
        i2c_port: int = 1,
        i2c_address: int = 0x3C,
        screensaver_sec: float = 90.0,
        rfid_status_fn: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self._events = events
        self.mode = mode.strip().lower()
        self.i2c_port = i2c_port
        self.i2c_address = i2c_address
        self.screensaver_sec = screensaver_sec
        self._rfid_status_fn = rfid_status_fn

        self._device: Any = None
        self._connected = False
        self._driver_name: str | None = None
        self._last_error: str | None = None
        self._running = False
        self._task: asyncio.Task[None] | None = None

        self._header: str = "FF GETRÄNKEAUTOMAT"
        self._line1: str = "Bereit"
        self._line2: str = "Chip vorhalten"
        self._debug_lines: list[str] | None = None
        self._menu_state: dict[str, Any] | None = None
        self._hold_until: float = 0.0
        self._last_activity_ts: float = time.monotonic()
        self._last_refresh_ts: float = 0.0
        self._sleeping: bool = False
        self._font_cache: dict[tuple[bool, int], Any] = {}
        self._fit_cache: dict[tuple[str, bool, int, int, int], tuple[str, Any, int]] = {}

    def _get_font(self, bold: bool, size: int) -> Any:
        key = (bold, size)
        if key in self._font_cache:
            return self._font_cache[key]
        if ImageFont is None:
            return None

        candidates = _FONT_CANDIDATES_BOLD if bold else _FONT_CANDIDATES_REGULAR
        font = None
        for path in candidates:
            if Path(path).is_file():
                try:
                    font = ImageFont.truetype(path, size=size)
                    break
                except Exception:
                    continue
        if font is None:
            try:
                font = ImageFont.load_default(size=size)
            except TypeError:
                font = ImageFont.load_default()

        self._font_cache[key] = font
        return font

    @staticmethod
    def _measure_width(draw: Any, text: str, font: Any) -> int:
        try:
            bbox = draw.textbbox((0, 0), text, font=font)
            return int(bbox[2] - bbox[0])
        except Exception:
            return int(draw.textlength(text, font=font))

    def _fit_text(
        self,
        draw: Any,
        text: str,
        *,
        bold: bool,
        max_size: int,
        min_size: int,
        max_width: int,
    ) -> tuple[str, Any, int]:
        """Pick the largest font size in [min_size..max_size] that fits max_width; trim cleanly if still wider."""
        clean = text.strip()
        cache_key = (clean, bold, max_size, min_size, max_width)
        cached = self._fit_cache.get(cache_key)
        if cached is not None:
            return cached

        for size in range(max_size, min_size - 1, -1):
            font = self._get_font(bold, size)
            width = self._measure_width(draw, clean, font)
            if width <= max_width:
                res = (clean, font, width)
                if len(self._fit_cache) < 256:
                    self._fit_cache[cache_key] = res
                return res

        font = self._get_font(bold, min_size)
        trimmed = clean
        while len(trimmed) > 1 and self._measure_width(draw, trimmed + "…", font) > max_width:
            trimmed = trimmed[:-1].rstrip()
        final_text = (trimmed + "…") if trimmed != clean else clean
        res = (final_text, font, self._measure_width(draw, final_text, font))
        if len(self._fit_cache) < 256:
            self._fit_cache[cache_key] = res
        return res

    async def start(self) -> None:
        # Signal early-boot splash script (if running) to release /dev/i2c-1
        try:
            HANDOFF_FLAG_PATH.write_text("ready\n", encoding="utf-8")
        except OSError:
            pass

        if self.mode == "disabled":
            return

        if Path(f"/dev/i2c-{self.i2c_port}").exists():
            await asyncio.sleep(0.25)
        self._running = True
        await asyncio.to_thread(self._try_init_hardware)
        await self._render_idle()
        self._task = asyncio.create_task(self._event_loop(), name="oled-display-loop")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await asyncio.to_thread(self._clear_hardware)

    async def show_diagnostics(
        self,
        *,
        rfid_snapshot: dict[str, Any],
        mdb_snapshot: dict[str, Any],
        ffweb_snapshot: dict[str, Any],
        pending_sync_count: int = 0,
        hold_sec: float = 6.0,
    ) -> dict[str, Any]:
        """Render a 4-line system debug/diagnostic summary on the OLED before showing the user UI."""
        ip = get_lan_ip() or "Kein LAN"
        rfid_ok = bool(rfid_snapshot.get("hardware_connected"))
        rfid_label = "USB OK" if rfid_ok else ("SIM" if rfid_snapshot.get("mode") == "simulator" else "FEHLT")

        mdb_mode = str(mdb_snapshot.get("mode") or "")
        if mdb_mode == "simulator":
            mdb_label = "SIM"
        else:
            mdb_label = "OK" if mdb_snapshot.get("connected") else "GETRENNT"

        ff_mode = "MOCK" if ffweb_snapshot.get("mode") == "mock" else "LIVE"
        if ffweb_snapshot.get("simulate_outage"):
            ff_mode = "OFFLINE"

        temp = get_cpu_temp_str()
        row1 = f"IP: {ip}"
        row2 = f"RFID: {rfid_label} | MDB: {mdb_label}"
        row3 = f"FFWEB: {ff_mode} | Q:{pending_sync_count}" + (f" | {temp}" if temp else "")

        header = "STATUS / DIAGNOSE"
        self._header = header
        self._line1 = row1
        self._line2 = f"{row2} / {row3}"
        self._debug_lines = [row1, row2, row3]
        self._last_activity_ts = time.monotonic()
        self._hold_until = self._last_activity_ts + hold_sec

        await asyncio.to_thread(self._draw_debug_frame, header, [row1, row2, row3])
        return {"header": header, "lines": [row1, row2, row3], "hold_sec": hold_sec}

    def _try_init_hardware(self) -> bool:
        if self.mode == "disabled":
            return False
        if luma_i2c is None or Image is None:
            if self.mode in {"ssd1306", "sh1106"}:
                self._last_error = "luma.oled / Pillow not installed"
            return False

        dev_node = Path(f"/dev/i2c-{self.i2c_port}")
        if not dev_node.exists():
            if self.mode in {"ssd1306", "sh1106"}:
                self._last_error = f"{dev_node} not found (enable I2C with raspi-config)"
            return False

        for addr in (self.i2c_address, 0x3D if self.i2c_address == 0x3C else 0x3C):
            try:
                self._close_device()
                serial = luma_i2c(port=self.i2c_port, address=addr)
                driver_cls = sh1106 if self.mode == "sh1106" else ssd1306
                dev = driver_cls(serial, width=128, height=64)
                dev.contrast(255)
                self._device = dev
                self._connected = True
                self._sleeping = False
                self._driver_name = f"{driver_cls.__name__}@0x{addr:02X}"
                self._last_error = None
                logger.info(
                    "Connected 0.96\" I2C OLED display: %s on /dev/i2c-%d",
                    self._driver_name,
                    self.i2c_port,
                )
                return True
            except Exception as exc:
                self._last_error = str(exc)
        self._connected = False
        return False

    def _close_device(self) -> None:
        dev = self._device
        self._device = None
        if dev is not None and hasattr(dev, "cleanup"):
            try:
                dev.cleanup()
            except Exception:
                pass

    def _handle_i2c_error(self, context: str, exc: Exception) -> None:
        was_connected = self._connected
        self._connected = False
        self._last_error = str(exc)
        self._close_device()
        if was_connected:
            logger.warning("OLED I2C %s failed (%s); will auto-reconnect when plugged back in", context, exc)

    def _ensure_display_configured(self) -> None:
        """
        Re-send non-blanking controller setup registers (charge pump ON, addressing mode,
        orientation, contrast, and Display ON 0xAF) without sending 0xAE (Display OFF).
        If the OLED briefly lost power or was unplugged and plugged back in (Power-On Reset),
        this wakes the internal charge pump and display controller with zero flicker.
        """
        dev = self._device
        if dev is None or not hasattr(dev, "command"):
            return
        if self.mode == "sh1106" or (self._driver_name and self._driver_name.startswith("sh1106")):
            dev.command(
                0xD5, 0x50,  # SETDISPLAYCLOCKDIV
                0xA8, 0x3F,  # SETMULTIPLEX (64-1)
                0xD3, 0x00,  # SETDISPLAYOFFSET
                0x40,        # SETSTARTLINE (0)
                0xAD, 0x8B,  # SH1106 DC-DC charge pump ON
                0xA1,        # SEGREMAP
                0xC8,        # COMSCANDEC
                0xDA, 0x12,  # SETCOMPINS
                0xD9, 0x22,  # SETPRECHARGE
                0xDB, 0x35,  # SETVCOMDETECT
                0x81, 0xFF,  # SETCONTRAST (255)
                0xA4,        # DISPLAYALLON_RESUME
                0xA6,        # NORMALDISPLAY
                0xAF,        # DISPLAYON
            )
        else:
            dev.command(
                0xD5, 0x80,  # SETDISPLAYCLOCKDIV
                0xA8, 0x3F,  # SETMULTIPLEX (64-1)
                0xD3, 0x00,  # SETDISPLAYOFFSET
                0x40,        # SETSTARTLINE (0)
                0x8D, 0x14,  # CHARGEPUMP (Enable charge pump after POR!)
                0x20, 0x00,  # MEMORYMODE (Horizontal addressing mode)
                0xA1,        # SEGREMAP
                0xC8,        # COMSCANDEC
                0xDA, 0x12,  # SETCOMPINS
                0xD9, 0xF1,  # SETPRECHARGE
                0xDB, 0x40,  # SETVCOMDETECT
                0x81, 0xFF,  # SETCONTRAST (255)
                0xA4,        # DISPLAYALLON_RESUME
                0xA6,        # NORMALDISPLAY
                0xAF,        # DISPLAYON
            )

    def _redraw_current_screen_sync(self) -> None:
        """Re-render whichever screen state is currently active (used on hot-plug recovery and periodic refresh)."""
        if self._menu_state is not None:
            self._draw_menu_frame(
                self._menu_state["header"],
                self._menu_state["option"],
                self._menu_state["subtitle"],
                self._menu_state["footer"],
            )
        elif self._debug_lines is not None:
            self._draw_debug_frame(self._header, self._debug_lines)
        else:
            self._draw_frame(self._header, self._line1, self._line2)

    def _periodic_hardware_check_sync(self) -> None:
        """
        Periodically verifies the OLED is reachable on I2C and self-heals after any
        accidental disconnect/reconnect or brief power brownout (POR).
        """
        if self.mode == "disabled":
            return
        if not self._connected or self._device is None:
            if self._try_init_hardware():
                self._redraw_current_screen_sync()
            return

        if self._sleeping:
            try:
                if hasattr(self._device, "command"):
                    self._device.command(0xAE)
            except Exception as exc:
                self._handle_i2c_error("sleep check", exc)
            return

        try:
            self._ensure_display_configured()
        except Exception as exc:
            self._handle_i2c_error("periodic check", exc)
            return

        self._redraw_current_screen_sync()

    def _clear_hardware(self) -> None:
        if self._device is not None:
            try:
                self._device.clear()
            except Exception:
                pass

    async def _show(
        self, header: str, line1: str, line2: str, hold_sec: float = 0.0
    ) -> None:
        self._header = header
        self._line1 = line1
        self._line2 = line2
        self._debug_lines = None
        self._menu_state = None
        self._last_activity_ts = time.monotonic()
        if hold_sec > 0:
            self._hold_until = self._last_activity_ts + hold_sec
        else:
            self._hold_until = 0.0

        await asyncio.to_thread(self._draw_frame, header, line1, line2)

    async def _show_menu(
        self,
        header: str,
        option_title: str,
        subtitle: str,
        footer: str = "[<Abbr]   [OK]   [Weiter>]",
    ) -> None:
        self._header = header
        self._line1 = option_title
        self._line2 = subtitle
        self._debug_lines = None
        self._menu_state = {
            "header": header,
            "option": option_title,
            "subtitle": subtitle,
            "footer": footer,
        }
        self._last_activity_ts = time.monotonic()
        self._hold_until = 0.0

        await asyncio.to_thread(
            self._draw_menu_frame, header, option_title, subtitle, footer
        )

    def _is_rfid_missing(self) -> bool:
        if self._rfid_status_fn is not None:
            snap = self._rfid_status_fn()
            if snap.get("simulate_disconnected"):
                return True
            mode = str(snap.get("mode") or "")
            if mode == "simulator":
                return False
            if mode == "auto" and not snap.get("evdev_supported", False):
                return False
            return not bool(snap.get("hardware_connected"))
        return False

    async def _render_idle(self) -> None:
        if self._is_rfid_missing():
            await self._show(
                "FEHLER: RFID",
                "Lesegerät getrennt",
                "USB prüfen!",
                hold_sec=0.0,
            )
        else:
            await self._show(
                "FF GETRÄNKEAUTOMAT",
                "Bereit",
                "Chip vorhalten",
                hold_sec=0.0,
            )

    def _wake_if_sleeping(self) -> None:
        if self._device is not None and self._sleeping:
            self._device.show()
            self._device.contrast(255)
            self._sleeping = False

    def _draw_frame(self, header: str, line1: str, line2: str) -> None:
        if not self._connected and self.mode != "disabled":
            if not self._try_init_hardware():
                return

        if self._device is None or Image is None or ImageDraw is None:
            return

        try:
            self._wake_if_sleeping()
            self._ensure_display_configured()

            img = Image.new("1", (128, 64), 0)
            draw = ImageDraw.Draw(img)

            # 1. Top inverted banner (128x15), auto-sized & horizontally centered
            draw.rectangle((0, 0, 127, 14), outline=1, fill=1)
            hdr_txt, hdr_font, hdr_w = self._fit_text(
                draw, header, bold=True, max_size=10, min_size=8, max_width=122
            )
            hdr_x = max(2, (128 - hdr_w) // 2)
            draw.text((hdr_x, 2), hdr_txt, font=hdr_font, fill=0)

            # 2. Subtitle / member or drink name (fits within 122px)
            l1_txt, l1_font, _ = self._fit_text(
                draw, line1, bold=False, max_size=11, min_size=9, max_width=122
            )
            draw.text((3, 19), l1_txt, font=l1_font, fill=1)

            # 3. Main prominent bottom line (auto-scales 16px -> 10px so nothing ever clips)
            l2_txt, l2_font, _ = self._fit_text(
                draw, line2, bold=True, max_size=16, min_size=10, max_width=122
            )
            draw.text((3, 39), l2_txt, font=l2_font, fill=1)

            self._device.display(img)
            self._last_refresh_ts = time.monotonic()
        except Exception as exc:
            self._handle_i2c_error("frame write", exc)

    def _draw_debug_frame(self, header: str, lines: list[str]) -> None:
        if not self._connected and self.mode != "disabled":
            if not self._try_init_hardware():
                return

        if self._device is None or Image is None or ImageDraw is None:
            return

        try:
            self._wake_if_sleeping()
            self._ensure_display_configured()

            img = Image.new("1", (128, 64), 0)
            draw = ImageDraw.Draw(img)

            draw.rectangle((0, 0, 127, 13), outline=1, fill=1)
            hdr_txt, hdr_font, hdr_w = self._fit_text(
                draw, header, bold=True, max_size=9, min_size=8, max_width=122
            )
            hdr_x = max(2, (128 - hdr_w) // 2)
            draw.text((hdr_x, 1), hdr_txt, font=hdr_font, fill=0)

            y_positions = (16, 31, 46)
            for idx, row_text in enumerate(lines[:3]):
                txt, fnt, _ = self._fit_text(
                    draw, row_text, bold=False, max_size=10, min_size=8, max_width=122
                )
                draw.text((3, y_positions[idx]), txt, font=fnt, fill=1)

            self._device.display(img)
            self._last_refresh_ts = time.monotonic()
        except Exception as exc:
            self._handle_i2c_error("debug frame write", exc)

    def _draw_menu_frame(
        self,
        header: str,
        option_title: str,
        subtitle: str,
        footer: str = "[<Abbr]   [OK]   [Weiter>]",
    ) -> None:
        if not self._connected and self.mode != "disabled":
            if not self._try_init_hardware():
                return

        if self._device is None or Image is None or ImageDraw is None:
            return

        try:
            self._wake_if_sleeping()
            self._ensure_display_configured()

            img = Image.new("1", (128, 64), 0)
            draw = ImageDraw.Draw(img)

            # 1. Top inverted banner (128x14)
            draw.rectangle((0, 0, 127, 13), outline=1, fill=1)
            hdr_txt, hdr_font, hdr_w = self._fit_text(
                draw, header, bold=True, max_size=9, min_size=8, max_width=122
            )
            hdr_x = max(2, (128 - hdr_w) // 2)
            draw.text((hdr_x, 1), hdr_txt, font=hdr_font, fill=0)

            # 2. Main selected option (with indicator >)
            opt_txt, opt_font, _ = self._fit_text(
                draw, f"> {option_title}", bold=True, max_size=12, min_size=9, max_width=122
            )
            draw.text((3, 17), opt_txt, font=opt_font, fill=1)

            # 3. Subtitle / note
            sub_txt, sub_font, _ = self._fit_text(
                draw, subtitle, bold=False, max_size=9, min_size=8, max_width=122
            )
            draw.text((12, 33), sub_txt, font=sub_font, fill=1)

            # 4. Bottom footer with physical 3-button navigation cues
            draw.line((0, 48, 127, 48), fill=1)
            ftr_txt, ftr_font, ftr_w = self._fit_text(
                draw, footer, bold=False, max_size=9, min_size=8, max_width=122
            )
            ftr_x = max(1, (128 - ftr_w) // 2)
            draw.text((ftr_x, 51), ftr_txt, font=ftr_font, fill=1)

            self._device.display(img)
            self._last_refresh_ts = time.monotonic()
        except Exception as exc:
            self._handle_i2c_error("menu frame write", exc)

    def _enter_screensaver(self) -> None:
        if self._device is None or self._sleeping:
            return
        try:
            self._device.clear()
            self._device.hide()
            self._sleeping = True
        except Exception as exc:
            self._handle_i2c_error("screensaver", exc)

    async def _event_loop(self) -> None:
        queue = self._events.subscribe()
        try:
            while self._running:
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=1.0)
                    await self._handle_event(ev)
                except asyncio.TimeoutError:
                    now = time.monotonic()
                    if self._hold_until > 0 and now >= self._hold_until:
                        self._hold_until = 0.0
                        await self._render_idle()
                    elif (
                        not self._sleeping
                        and self._header == "FF GETRÄNKEAUTOMAT"
                        and (now - self._last_activity_ts) >= self.screensaver_sec
                    ):
                        await asyncio.to_thread(self._enter_screensaver)
                    elif not self._connected or (now - self._last_refresh_ts) >= 1.5:
                        await asyncio.to_thread(self._periodic_hardware_check_sync)
        finally:
            self._events.unsubscribe(queue)

    async def _handle_event(self, ev: dict[str, Any]) -> None:
        etype = ev.get("type", "")
        p = ev.get("payload") or {}

        if etype in {"billing_selection_started", "billing_selection_changed"}:
            options = p.get("options") or []
            selected_idx = int(p.get("selected_index", 0))
            total = len(options)
            opt = options[selected_idx] if 0 <= selected_idx < len(options) else {}
            header = f"ABRECHNUNG ({selected_idx + 1}/{total})" if total > 1 else "ABRECHNUNG WÄHLEN"
            opt_name = str(opt.get("name") or "Privat")
            opt_sub = str(opt.get("subtext") or "")
            await self._show_menu(header, opt_name, opt_sub)

        elif etype == "session_started":
            billing_mode = str(p.get("billing_mode") or "private")
            if billing_mode == "event_list":
                el = p.get("event_list") or {}
                list_name = str(el.get("name") or "Freigetränk")
                await self._show("FREIGETRÄNK (FF)", list_name, "Getränk wählen")
            else:
                member = p.get("member") or {}
                name = str(member.get("name") or "Mitglied")
                bal = str(member.get("balance_eur") or "0.00")
                await self._show("GUTHABEN AKTIV", name, f"{bal} EUR")

        elif etype == "awaiting_chip_for_selection":
            if self._is_rfid_missing():
                await self._render_idle()
            else:
                drink = str(p.get("drink_name") or "Getränk")
                price = str(p.get("price_eur") or "0.00")
                await self._show("CHIP SCANNEN", drink, f"{price} EUR")

        elif etype == "dispensing_started":
            drink = str(p.get("drink_name") or "Getränk")
            await self._show("AUSGABE LÄUFT...", drink, "Bitte warten")

        elif etype == "vend_dispensed":
            drink = str(p.get("drink_name") or "Getränk")
            billing_mode = str(p.get("billing_mode") or "private")
            if billing_mode == "event_list":
                el_name = str(p.get("event_list_name") or "Freigetränk")
                await self._show("PROST! FREIGETRÄNK", drink, el_name, hold_sec=6.0)
            else:
                price = str(p.get("price_eur") or "0.00")
                await self._show("PROST! DANKE!", drink, f"-{price} EUR", hold_sec=6.0)

        elif etype == "tx_synced":
            new_bal_cents = p.get("new_balance_cents")
            drink = str(p.get("drink_name") or "Gebucht")
            if new_bal_cents is not None:
                await self._show(
                    "PROST! GEBUCHT",
                    drink,
                    f"Rest: {cents_to_eur_str(int(new_bal_cents))} €",
                    hold_sec=6.0,
                )

        elif etype in {"scan_denied_balance", "vend_denied_balance"}:
            bal = str(p.get("balance_eur") or "0.00")
            await self._show("KEIN GUTHABEN", "Bitte aufladen!", f"{bal} EUR", hold_sec=5.0)

        elif etype == "unknown_chip_scanned":
            uid = str(p.get("chip_uid") or "?")
            await self._show("UNBEKANNTER CHIP", "Nicht registriert:", uid, hold_sec=6.0)

        elif etype == "vend_failed_no_charge":
            await self._show("AUSGABEFEHLER", "Keine Abbuchung", "0.00 EUR", hold_sec=6.0)

        elif etype == "session_cancelled":
            await self._show("ABGEBROCHEN", "Sitzung beendet", "0.00 EUR", hold_sec=3.0)

        elif etype == "ffweb_lookup_failed":
            await self._show("NETZWERKFEHLER", "FFWEB offline", "Bitte später!", hold_sec=5.0)

        elif etype == "oled_wake_requested":
            self._last_activity_ts = time.monotonic()
            await asyncio.to_thread(self._redraw_current_screen_sync)

        elif etype == "rfid_status_changed":
            self._wake_if_sleeping()
            self._last_activity_ts = time.monotonic()
            connected = bool(p.get("connected"))
            if not connected:
                if self._header != "STATUS / DIAGNOSE" or time.monotonic() >= self._hold_until:
                    self._hold_until = 0.0
                    await self._render_idle()
            else:
                if self._header == "FEHLER: RFID" or time.monotonic() >= self._hold_until:
                    self._hold_until = 0.0
                    await self._render_idle()

        elif etype == "state_changed" and p.get("state") == "IDLE":
            if time.monotonic() >= self._hold_until:
                await self._render_idle()

    def status_snapshot(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "connected": self._connected,
            "driver": self._driver_name,
            "i2c_bus": f"/dev/i2c-{self.i2c_port}",
            "sleeping": self._sleeping,
            "screen": {
                "header": self._header,
                "line1": self._line1,
                "line2": self._line2,
                "debug_lines": self._debug_lines,
                "menu": self._menu_state,
            },
            "last_error": self._last_error,
        }
