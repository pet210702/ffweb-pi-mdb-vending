from __future__ import annotations

import logging
from typing import Any

from app.db import cents_to_eur_str
from app.mdb.base import MdbDriver, MdbEventCallback, parse_qibixx_line

logger = logging.getLogger(__name__)


class SimulatorMdbDriver(MdbDriver):
    """
    Virtual SandenVendo G-Drink DM9 + Qibixx MDB-USB Simulator.
    Emits real Qibixx ASCII frames ('c,STATUS,...', 'c,VEND,SUCCESS', etc.)
    so all parser and state-machine code runs identically to physical hardware.
    """

    def __init__(self) -> None:
        self._on_event: MdbEventCallback | None = None
        self._enabled = False
        self._display_text = "G-DRINK DM9 BEREIT"
        self._authorized_credit_cents = 0
        self._pending_vend_slot: int | None = None
        self._pending_vend_price_cents: int = 0
        self._vend_approved = False
        self._last_rx: str | None = None
        self._last_tx: str | None = None

    async def start(self, on_event: MdbEventCallback) -> None:
        self._on_event = on_event
        await self.enable_cashless()

    async def stop(self) -> None:
        self._enabled = False
        self._display_text = "OFFLINE"

    async def _emit_raw(self, raw_line: str) -> None:
        self._last_rx = raw_line
        logger.info("SIM MDB RX <- %s", raw_line)
        event = parse_qibixx_line(raw_line)
        if event and self._on_event:
            await self._on_event(event)

    async def send_command(self, command: str) -> None:
        cmd = command.strip()
        self._last_tx = cmd
        logger.info("SIM MDB TX -> %s", cmd)
        parts = [p.strip() for p in cmd.split(",")]
        if len(parts) >= 2 and parts[0].upper() == "C":
            action = parts[1].upper()
            if action == "1":
                self._enabled = True
                self._display_text = "CHIP SCANNEN ODER WÄHLEN"
                await self._emit_raw("c,STATUS,ENABLED")
            elif action == "0":
                self._enabled = False
                self._authorized_credit_cents = 0
                self._vend_approved = False
                self._display_text = "KARTENLESER DEAKTIVIERT"
                await self._emit_raw("c,STATUS,DISABLED")
            elif action == "START" and len(parts) >= 3:
                eur_text = parts[2]
                self._display_text = f"GUTHABEN: {eur_text} EUR"
                self._vend_approved = False
            elif action == "VEND" and len(parts) >= 3:
                if parts[2] == "-1":
                    self._vend_approved = False
                    self._pending_vend_slot = None
                    self._display_text = "ABGELEHNT"
                    await self._emit_raw("c,STATUS,IDLE")
                else:
                    self._vend_approved = True
                    slot_label = (
                        f"SLOT {self._pending_vend_slot}"
                        if self._pending_vend_slot is not None
                        else "GETRÄNK"
                    )
                    self._display_text = f"LIFT FÄHRT — {slot_label}..."
            elif action == "STOP":
                self._authorized_credit_cents = 0
                self._pending_vend_slot = None
                self._pending_vend_price_cents = 0
                self._vend_approved = False
                self._display_text = "CHIP SCANNEN ODER WÄHLEN"
                await self._emit_raw("c,STATUS,IDLE")

    # -------------------------------------------------
    # Actions triggered on the Virtual G-Drink DM9 UI
    # -------------------------------------------------
    async def simulate_keypad_selection(self, slot_number: int, price_cents: int) -> None:
        if not self._enabled:
            raise RuntimeError("MDB cashless peripheral is currently disabled")
        self._pending_vend_slot = slot_number
        self._pending_vend_price_cents = price_cents
        self._vend_approved = False
        eur = cents_to_eur_str(price_cents)
        self._display_text = f"SLOT {slot_number} — {eur} EUR"
        await self._emit_raw(f"c,STATUS,VEND,{eur},{slot_number}")

    async def simulate_elevator_dispense(self, success: bool = True) -> None:
        if not self._vend_approved:
            raise RuntimeError("No approved vend is currently waiting for dispense")
        slot = self._pending_vend_slot
        self._pending_vend_slot = None
        self._pending_vend_price_cents = 0
        self._vend_approved = False
        if success:
            self._display_text = f"ENTNEHMEN (SLOT {slot}) — DANKE!"
            await self._emit_raw("c,VEND,SUCCESS")
            await self._emit_raw("c,STATUS,IDLE")
        else:
            self._display_text = f"AUSGABEFEHLER SLOT {slot} (KEINE ABBUCHUNG)"
            await self._emit_raw("c,ERR,VEND 3")
            await self._emit_raw("c,STATUS,IDLE")

    async def simulate_machine_cancel(self) -> None:
        self._pending_vend_slot = None
        self._pending_vend_price_cents = 0
        self._vend_approved = False
        self._display_text = "ABGEBROCHEN — BEREIT"
        await self._emit_raw("c,ERR,VEND 1")
        await self._emit_raw("c,STATUS,IDLE")

    def status_snapshot(self) -> dict[str, Any]:
        return {
            "mode": "simulator",
            "connected": self._enabled,
            "dm9_display": self._display_text,
            "pending_slot": self._pending_vend_slot,
            "vend_approved_waiting_sensor": self._vend_approved,
            "last_rx": self._last_rx,
            "last_tx": self._last_tx,
        }
