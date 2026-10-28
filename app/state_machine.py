from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any

from app.buttons import ButtonAction
from app.config import Settings
from app.db import SlotMapping, VendingDatabase, cents_to_eur_str
from app.events import EventBus, utc_now_iso
from app.ffweb.base import FFWebClient, FFWebError, MemberInfo
from app.ffweb.sync_worker import FFWebSyncWorker
from app.mdb.base import (
    MdbDriver,
    MdbEvent,
    MdbStatusEvent,
    MdbVendErrorEvent,
    MdbVendRequestEvent,
    MdbVendSuccessEvent,
)

logger = logging.getLogger(__name__)


class ControllerState(str, Enum):
    IDLE = "IDLE"
    AUTHORIZING_CHIP = "AUTHORIZING_CHIP"
    SELECTING_BILLING = "SELECTING_BILLING"
    SESSION_ACTIVE = "SESSION_ACTIVE"
    AWAITING_CHIP = "AWAITING_CHIP"
    DISPENSING = "DISPENSING"


@dataclass(slots=True)
class PendingSelection:
    slot_number: int | None
    ffweb_product_id: str
    drink_name: str
    price_cents: int


@dataclass(slots=True)
class ActiveDispense:
    member: MemberInfo
    selection: PendingSelection


class VendingController:
    """
    Coordinates RFID chip scans, physical 3-button navigation, MDB Cashless peripheral
    commands/events, local SQLite slot mapping + audit logging, and FFWEB balance/consumption sync.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        db: VendingDatabase,
        mdb: MdbDriver,
        ffweb: FFWebClient,
        sync_worker: FFWebSyncWorker,
        events: EventBus,
    ) -> None:
        self._settings = settings
        self._db = db
        self._mdb = mdb
        self._ffweb = ffweb
        self._sync_worker = sync_worker
        self._events = events

        self.state: ControllerState = ControllerState.IDLE
        self.active_member: MemberInfo | None = None
        self.pending_selection: PendingSelection | None = None
        self.active_dispense: ActiveDispense | None = None
        self.last_unknown_chip: dict[str, str] | None = None

        # Billing mode & event lists (Verrechnungslisten / Anlässe)
        self.current_billing_mode: str = "private"
        self.current_event_list: dict[str, Any] | None = None
        self.available_billing_options: list[dict[str, Any]] = []
        self.selected_billing_index: int = 0
        self._diagnostics_callback: Any = None

        self._timer_task: asyncio.Task[None] | None = None
        self._scan_lock = asyncio.Lock()

    def set_diagnostics_callback(self, cb: Any) -> None:
        self._diagnostics_callback = cb

    async def shutdown(self) -> None:
        self._cancel_timer()

    def _cancel_timer(self) -> None:
        if self._timer_task and not self._timer_task.done():
            self._timer_task.cancel()
        self._timer_task = None

    def _arm_timer(self, delay_sec: float, callback) -> None:
        self._cancel_timer()

        async def _runner() -> None:
            try:
                await asyncio.sleep(delay_sec)
                await callback()
            except asyncio.CancelledError:
                pass

        self._timer_task = asyncio.create_task(_runner())

    def _transition_to_idle(self, reason: str = "") -> None:
        self._cancel_timer()
        prev = self.state
        self.state = ControllerState.IDLE
        self.active_member = None
        self.pending_selection = None
        self.active_dispense = None
        self.current_billing_mode = "private"
        self.current_event_list = None
        self.available_billing_options = []
        self.selected_billing_index = 0
        if prev != ControllerState.IDLE or reason:
            self._events.publish(
                "state_changed",
                {"state": self.state.value, "previous_state": prev.value, "reason": reason},
            )

    def _resolve_slot(self, slot_number: int | None, mdb_price_cents: int) -> PendingSelection | None:
        slot: SlotMapping | None = (
            self._db.get_slot(slot_number) if slot_number is not None else None
        )
        if slot and slot.active:
            effective_price = mdb_price_cents if mdb_price_cents > 0 else slot.price_cents
            return PendingSelection(
                slot_number=slot_number,
                ffweb_product_id=slot.ffweb_product_id,
                drink_name=slot.drink_name,
                price_cents=effective_price,
            )

        if self._settings.allow_unmapped_slots:
            label = f"Slot {slot_number}" if slot_number is not None else "Unbekannter Slot"
            pid = f"SLOT-{slot_number}" if slot_number is not None else "SLOT-UNKNOWN"
            return PendingSelection(
                slot_number=slot_number,
                ffweb_product_id=pid,
                drink_name=label,
                price_cents=mdb_price_cents,
            )
        return None

    # ---------------------------------------------------------
    # RFID Input Handler
    # ---------------------------------------------------------
    async def on_rfid_scan(self, chip_uid: str, source: str = "usb") -> dict[str, Any]:
        normalized_uid = chip_uid.strip().upper()
        async with self._scan_lock:
            self._events.publish(
                "rfid_scanned",
                {"chip_uid": normalized_uid, "source": source, "state": self.state.value},
            )

            # Never interrupt a physical bottle dispense in progress
            if self.state == ControllerState.DISPENSING:
                logger.info("Ignoring RFID scan %s while elevator is dispensing", normalized_uid)
                return {"action": "ignored_dispensing", "chip_uid": normalized_uid}

            # Tapping the same chip again during SESSION_ACTIVE cancels the session cleanly
            if (
                self.state == ControllerState.SESSION_ACTIVE
                and self.active_member is not None
                and self.active_member.chip_uid.upper() == normalized_uid
            ):
                member_name = self.active_member.name
                self._transition_to_idle(reason="cancelled_by_second_tap")
                await self._mdb.stop_or_deny()
                self._events.publish(
                    "session_cancelled",
                    {"chip_uid": normalized_uid, "member_name": member_name},
                )
                return {"action": "session_cancelled", "chip_uid": normalized_uid}

            # If a previous user's session was still open, close it before authorizing the new chip
            if self.state == ControllerState.SESSION_ACTIVE:
                self._transition_to_idle(reason="superseded_by_new_chip")
                await self._mdb.stop_or_deny()

            saved_selection = (
                self.pending_selection if self.state == ControllerState.AWAITING_CHIP else None
            )
            prev_state = self.state
            self.state = ControllerState.AUTHORIZING_CHIP

            try:
                member = await self._ffweb.get_member_by_chip(normalized_uid)
            except FFWebError as exc:
                logger.error("FFWEB lookup failed for chip %s: %s", normalized_uid, exc)
                self._transition_to_idle(reason="ffweb_unreachable")
                if prev_state == ControllerState.AWAITING_CHIP:
                    await self._mdb.stop_or_deny()
                self._events.publish(
                    "ffweb_lookup_failed",
                    {"chip_uid": normalized_uid, "error": str(exc)},
                )
                return {"action": "ffweb_error", "error": str(exc), "chip_uid": normalized_uid}

            if member is None or not member.active:
                self.last_unknown_chip = {
                    "chip_uid": normalized_uid,
                    "scanned_at": utc_now_iso(),
                    "source": source,
                }
                self._db.create_transaction(
                    idempotency_key=str(uuid.uuid4()),
                    chip_uid=normalized_uid,
                    member_id=member.member_id if member else None,
                    member_name=member.name if member else None,
                    mdb_slot=saved_selection.slot_number if saved_selection else None,
                    ffweb_product_id=saved_selection.ffweb_product_id if saved_selection else None,
                    drink_name=saved_selection.drink_name if saved_selection else None,
                    price_cents=saved_selection.price_cents if saved_selection else 0,
                    vend_status="DENIED_UNKNOWN_CHIP",
                    sync_status="NOT_APPLICABLE",
                )
                self._transition_to_idle(reason="unknown_or_inactive_chip")
                if prev_state == ControllerState.AWAITING_CHIP:
                    await self._mdb.stop_or_deny()
                self._events.publish(
                    "unknown_chip_scanned",
                    {"chip_uid": normalized_uid, "source": source},
                )
                return {"action": "denied_unknown_chip", "chip_uid": normalized_uid}

            # Flow B: Drink was already selected on the DM9 ("Always Idle" mode)
            self.last_unknown_chip = None
            if saved_selection is not None:
                active_lists = self._db.list_event_lists(active_only=True)
                if active_lists:
                    options = [
                        {
                            "mode": "private",
                            "id": None,
                            "name": f"Privat ({cents_to_eur_str(member.balance_cents)} €)",
                            "subtext": "Privates Guthaben",
                            "balance_cents": member.balance_cents,
                        }
                    ]
                    for el in active_lists:
                        options.append(
                            {
                                "mode": "event_list",
                                "id": el["list_id"],
                                "name": el["name"],
                                "subtext": "Freigetränk (FF)",
                                "balance_cents": 0,
                            }
                        )
                    self.active_member = member
                    self.available_billing_options = options
                    self.selected_billing_index = 0
                    self.pending_selection = saved_selection
                    self.state = ControllerState.SELECTING_BILLING
                    self._arm_timer(
                        self._settings.billing_selection_timeout_sec,
                        self._on_billing_selection_timeout,
                    )
                    self._events.publish(
                        "billing_selection_started",
                        {
                            "member": member.to_dict(),
                            "options": options,
                            "selected_index": 0,
                            "pending_selection": {
                                "drink_name": saved_selection.drink_name,
                                "price_eur": cents_to_eur_str(saved_selection.price_cents),
                            },
                            "timeout_sec": self._settings.billing_selection_timeout_sec,
                        },
                    )
                    return {
                        "action": "selecting_billing",
                        "member": member.to_dict(),
                        "options": options,
                    }

                if member.balance_cents < saved_selection.price_cents:
                    self._record_insufficient_balance(member, saved_selection)
                    self._transition_to_idle(reason="insufficient_balance")
                    await self._mdb.stop_or_deny()
                    return {
                        "action": "denied_balance",
                        "member": member.to_dict(),
                        "required_cents": saved_selection.price_cents,
                    }

                self.current_billing_mode = "private"
                self.current_event_list = None
                await self._begin_dispense(member, saved_selection)
                return {
                    "action": "vend_approved",
                    "member": member.to_dict(),
                    "drink_name": saved_selection.drink_name,
                }

            # Flow A: Scan chip first -> check active event lists
            active_lists = self._db.list_event_lists(active_only=True)
            if active_lists:
                options = [
                    {
                        "mode": "private",
                        "id": None,
                        "name": f"Privat ({cents_to_eur_str(member.balance_cents)} €)",
                        "subtext": "Privates Guthaben",
                        "balance_cents": member.balance_cents,
                    }
                ]
                for el in active_lists:
                    options.append(
                        {
                            "mode": "event_list",
                            "id": el["list_id"],
                            "name": el["name"],
                            "subtext": "Freigetränk (FF)",
                            "balance_cents": 0,
                        }
                    )
                self.active_member = member
                self.available_billing_options = options
                self.selected_billing_index = 0
                self.state = ControllerState.SELECTING_BILLING
                self._arm_timer(
                    self._settings.billing_selection_timeout_sec,
                    self._on_billing_selection_timeout,
                )
                self._events.publish(
                    "billing_selection_started",
                    {
                        "member": member.to_dict(),
                        "options": options,
                        "selected_index": 0,
                        "timeout_sec": self._settings.billing_selection_timeout_sec,
                    },
                )
                return {
                    "action": "selecting_billing",
                    "member": member.to_dict(),
                    "options": options,
                    "selected_index": 0,
                }

            if member.balance_cents <= 0:
                self._record_zero_balance(member)
                self._transition_to_idle(reason="zero_balance")
                return {"action": "denied_balance", "member": member.to_dict()}

            self.active_member = member
            self.current_billing_mode = "private"
            self.current_event_list = None
            self.state = ControllerState.SESSION_ACTIVE
            self._arm_timer(self._settings.session_timeout_sec, self._on_session_timeout)
            await self._mdb.start_session(member.balance_cents)
            self._events.publish(
                "session_started",
                {
                    "member": member.to_dict(),
                    "billing_mode": "private",
                    "timeout_sec": self._settings.session_timeout_sec,
                },
            )
            return {"action": "session_started", "member": member.to_dict()}

    def _record_zero_balance(self, member: MemberInfo) -> None:
        self._db.create_transaction(
            idempotency_key=str(uuid.uuid4()),
            chip_uid=member.chip_uid,
            member_id=member.member_id,
            member_name=member.name,
            mdb_slot=None,
            ffweb_product_id=None,
            drink_name=None,
            price_cents=0,
            billing_mode="private",
            vend_status="DENIED_BALANCE",
            sync_status="NOT_APPLICABLE",
        )
        self._events.publish(
            "scan_denied_balance",
            {
                "member_name": member.name,
                "balance_eur": cents_to_eur_str(member.balance_cents),
            },
        )

    # ---------------------------------------------------------
    # Physical Button Input Handler
    # ---------------------------------------------------------
    async def on_button_press(self, action: ButtonAction | str) -> dict[str, Any]:
        btn = ButtonAction(action) if isinstance(action, str) else action

        # 0. Combo 1+3: Show debug / diagnostic menu for 10 seconds
        if btn == ButtonAction.COMBO_1_3:
            logger.info("Button combo 1+3 pressed: invoking debug menu for 10s")
            if self.state in {
                ControllerState.SELECTING_BILLING,
                ControllerState.SESSION_ACTIVE,
                ControllerState.AWAITING_CHIP,
            }:
                self._transition_to_idle(reason="debug_menu_invoked")
                await self._mdb.stop_or_deny()

            self._events.publish(
                "debug_menu_requested",
                {"hold_sec": 10.0, "source": "buttons_1_3"},
            )
            diag_res = None
            if self._diagnostics_callback:
                diag_res = await self._diagnostics_callback(hold_sec=10.0)
            return {"action": "debug_menu", "hold_sec": 10.0, "diagnostics": diag_res}

        # 1. During billing selection menu
        if self.state == ControllerState.SELECTING_BILLING and self.active_member is not None:
            if btn == ButtonAction.NEXT:
                if self.available_billing_options:
                    self.selected_billing_index = (
                        self.selected_billing_index + 1
                    ) % len(self.available_billing_options)
                    self._arm_timer(
                        self._settings.billing_selection_timeout_sec,
                        self._on_billing_selection_timeout,
                    )
                    current_opt = self.available_billing_options[self.selected_billing_index]
                    self._events.publish(
                        "billing_selection_changed",
                        {
                            "options": self.available_billing_options,
                            "selected_index": self.selected_billing_index,
                            "current": current_opt,
                        },
                    )
                    return {
                        "action": "billing_selection_changed",
                        "selected_index": self.selected_billing_index,
                        "current": current_opt,
                    }

            elif btn == ButtonAction.SELECT:
                choice = (
                    self.available_billing_options[self.selected_billing_index]
                    if self.available_billing_options
                    else {"mode": "private"}
                )
                member = self.active_member
                saved_sel = self.pending_selection

                if choice["mode"] == "private":
                    needed_cents = saved_sel.price_cents if saved_sel else 0
                    if member.balance_cents <= 0 or (saved_sel and member.balance_cents < needed_cents):
                        if saved_sel:
                            self._record_insufficient_balance(member, saved_sel)
                        else:
                            self._record_zero_balance(member)
                        self._transition_to_idle(reason="insufficient_balance")
                        await self._mdb.stop_or_deny()
                        return {"action": "denied_balance", "member": member.to_dict()}

                    self.current_billing_mode = "private"
                    self.current_event_list = None
                else:
                    self.current_billing_mode = "event_list"
                    self.current_event_list = choice

                if saved_sel is not None:
                    await self._begin_dispense(member, saved_sel)
                    return {
                        "action": "vend_approved",
                        "billing_mode": self.current_billing_mode,
                        "event_list": self.current_event_list,
                    }

                self.state = ControllerState.SESSION_ACTIVE
                self._arm_timer(self._settings.session_timeout_sec, self._on_session_timeout)
                session_credit = (
                    member.balance_cents if self.current_billing_mode == "private" else 1000
                )
                await self._mdb.start_session(session_credit)
                self._events.publish(
                    "session_started",
                    {
                        "member": member.to_dict(),
                        "billing_mode": self.current_billing_mode,
                        "event_list": self.current_event_list,
                        "timeout_sec": self._settings.session_timeout_sec,
                    },
                )
                return {
                    "action": "session_started",
                    "billing_mode": self.current_billing_mode,
                    "event_list": self.current_event_list,
                }

            elif btn == ButtonAction.BACK:
                member_name = self.active_member.name if self.active_member else None
                self._transition_to_idle(reason="cancelled_by_back_button")
                await self._mdb.stop_or_deny()
                self._events.publish(
                    "session_cancelled",
                    {"reason": "back_button", "member_name": member_name},
                )
                return {"action": "session_cancelled", "reason": "back_button"}

        # 2. During active session: BACK button cancels session
        if self.state == ControllerState.SESSION_ACTIVE:
            if btn == ButtonAction.BACK:
                member_name = self.active_member.name if self.active_member else None
                self._transition_to_idle(reason="cancelled_by_back_button")
                await self._mdb.stop_or_deny()
                self._events.publish(
                    "session_cancelled",
                    {"reason": "back_button", "member_name": member_name},
                )
                return {"action": "session_cancelled", "reason": "back_button"}

        # 3. During awaiting chip: BACK cancels pending drink selection
        if self.state == ControllerState.AWAITING_CHIP:
            if btn == ButtonAction.BACK:
                self._transition_to_idle(reason="cancelled_by_back_button")
                await self._mdb.stop_or_deny()
                self._events.publish("session_cancelled", {"reason": "back_button"})
                return {"action": "session_cancelled", "reason": "back_button"}

        # 4. In IDLE: any button wakes up OLED display if screensaver active
        if self.state == ControllerState.IDLE:
            self._events.publish("oled_wake_requested", {"button": btn.value})
            return {"action": "woke_display", "button": btn.value}

        return {"action": "ignored", "state": self.state.value, "button": btn.value}

    async def _on_billing_selection_timeout(self) -> None:
        if self.state == ControllerState.SELECTING_BILLING and self.active_member is not None:
            logger.info("Billing selection timed out")
            member = self.active_member
            if (
                self.selected_billing_index == 0
                and self.available_billing_options
                and self.available_billing_options[0].get("mode") == "private"
                and member.balance_cents > 0
            ):
                logger.info("Defaulting to private billing for %s", member.name)
                saved_sel = self.pending_selection
                self.current_billing_mode = "private"
                self.current_event_list = None

                if saved_sel is not None:
                    if member.balance_cents >= saved_sel.price_cents:
                        await self._begin_dispense(member, saved_sel)
                        return
                    self._record_insufficient_balance(member, saved_sel)
                    self._transition_to_idle(reason="insufficient_balance")
                    await self._mdb.stop_or_deny()
                    return

                self.state = ControllerState.SESSION_ACTIVE
                self._arm_timer(self._settings.session_timeout_sec, self._on_session_timeout)
                await self._mdb.start_session(member.balance_cents)
                self._events.publish(
                    "session_started",
                    {
                        "member": member.to_dict(),
                        "billing_mode": "private",
                        "timeout_sec": self._settings.session_timeout_sec,
                    },
                )
                return

            self._transition_to_idle(reason="billing_selection_timeout")
            await self._mdb.stop_or_deny()
            self._events.publish("session_cancelled", {"reason": "selection_timeout"})

    # ---------------------------------------------------------
    # MDB Event Handler
    # ---------------------------------------------------------
    async def on_mdb_event(self, event: MdbEvent) -> None:
        if isinstance(event, MdbVendRequestEvent):
            await self._handle_vend_request(event)
        elif isinstance(event, MdbVendSuccessEvent):
            await self._handle_vend_success(event)
        elif isinstance(event, MdbVendErrorEvent):
            await self._handle_vend_error(event)
        elif isinstance(event, MdbStatusEvent):
            await self._handle_status_event(event)

    async def _handle_vend_request(self, event: MdbVendRequestEvent) -> None:
        selection = self._resolve_slot(event.slot_number, event.price_cents)
        if selection is None:
            member = self.active_member
            self._db.create_transaction(
                idempotency_key=str(uuid.uuid4()),
                chip_uid=member.chip_uid if member else "UNSCANNED",
                member_id=member.member_id if member else None,
                member_name=member.name if member else None,
                mdb_slot=event.slot_number,
                ffweb_product_id=None,
                drink_name=f"Unkonfigurierter Slot {event.slot_number}",
                price_cents=event.price_cents,
                vend_status="DENIED_UNMAPPED_SLOT",
                sync_status="NOT_APPLICABLE",
            )
            self._transition_to_idle(reason=f"unmapped_slot_{event.slot_number}")
            await self._mdb.stop_or_deny()
            self._events.publish(
                "vend_denied_unmapped_slot",
                {"slot_number": event.slot_number, "price_cents": event.price_cents},
            )
            return

        # Case 1: Firefighter already scanned their chip (SESSION_ACTIVE)
        if self.state == ControllerState.SESSION_ACTIVE and self.active_member is not None:
            member = self.active_member
            if self.current_billing_mode == "event_list":
                # Freigetränk covered by station list!
                await self._begin_dispense(member, selection)
                return

            if member.balance_cents < selection.price_cents:
                self._record_insufficient_balance(member, selection)
                self._transition_to_idle(reason="insufficient_balance")
                await self._mdb.stop_or_deny()
                return
            await self._begin_dispense(member, selection)
            return

        # Case 2: Selection-First ("Always Idle") — user pressed slot button before scanning chip
        if self.state == ControllerState.IDLE:
            self.pending_selection = selection
            self.state = ControllerState.AWAITING_CHIP
            self._arm_timer(
                self._settings.awaiting_chip_timeout_sec, self._on_awaiting_chip_timeout
            )
            self._events.publish(
                "awaiting_chip_for_selection",
                {
                    "slot_number": selection.slot_number,
                    "drink_name": selection.drink_name,
                    "price_eur": cents_to_eur_str(selection.price_cents),
                    "timeout_sec": self._settings.awaiting_chip_timeout_sec,
                },
            )
            return

        logger.warning(
            "Unexpected MDB VEND request in state %s (denying safely)", self.state.value
        )
        self._transition_to_idle(reason="unexpected_vend_request")
        await self._mdb.stop_or_deny()

    async def _begin_dispense(self, member: MemberInfo, selection: PendingSelection) -> None:
        self._cancel_timer()
        self.active_member = member
        self.pending_selection = None
        self.active_dispense = ActiveDispense(member=member, selection=selection)
        self.state = ControllerState.DISPENSING
        self._arm_timer(self._settings.dispense_timeout_sec, self._on_dispense_timeout)
        self._events.publish(
            "dispensing_started",
            {
                "member_name": member.name,
                "slot_number": selection.slot_number,
                "drink_name": selection.drink_name,
                "price_eur": cents_to_eur_str(selection.price_cents),
            },
        )
        await self._mdb.approve_vend(selection.price_cents)

    async def _handle_vend_success(self, event: MdbVendSuccessEvent) -> None:
        if self.state != ControllerState.DISPENSING or self.active_dispense is None:
            logger.warning("Received c,VEND,SUCCESS outside DISPENSING state (%s)", event.raw_line)
            return

        dispense = self.active_dispense
        billing_mode = self.current_billing_mode
        event_list = self.current_event_list
        self._transition_to_idle(reason="vend_success")

        tx = self._db.create_transaction(
            idempotency_key=str(uuid.uuid4()),
            chip_uid=dispense.member.chip_uid,
            member_id=dispense.member.member_id,
            member_name=dispense.member.name,
            mdb_slot=dispense.selection.slot_number,
            ffweb_product_id=dispense.selection.ffweb_product_id,
            drink_name=dispense.selection.drink_name,
            price_cents=dispense.selection.price_cents,
            billing_mode=billing_mode,
            event_list_id=event_list["id"] if event_list else None,
            event_list_name=event_list["name"] if event_list else None,
            member_balance_deducted=0 if billing_mode == "event_list" else dispense.selection.price_cents,
            vend_status="DISPENSED",
            sync_status="PENDING_SYNC",
        )
        self._events.publish(
            "vend_dispensed",
            {
                "tx_id": tx["id"],
                "member_name": dispense.member.name,
                "drink_name": dispense.selection.drink_name,
                "slot_number": dispense.selection.slot_number,
                "price_eur": "0.00" if billing_mode == "event_list" else tx["price_eur"],
                "billing_mode": billing_mode,
                "event_list_name": event_list["name"] if event_list else None,
            },
        )
        # Immediately attempt live sync to FFWEB (if offline, background worker retries automatically)
        await self._sync_worker.sync_single_transaction(tx)

    async def _handle_vend_error(self, event: MdbVendErrorEvent) -> None:
        reason_upper = event.reason.upper()
        is_user_cancel = "VEND 1" in reason_upper
        status_label = "CANCELLED" if is_user_cancel else "FAILED_DISPENSE"

        if self.active_dispense is not None:
            dispense = self.active_dispense
            self._transition_to_idle(reason=f"mdb_err_{event.reason}")
            tx = self._db.create_transaction(
                idempotency_key=str(uuid.uuid4()),
                chip_uid=dispense.member.chip_uid,
                member_id=dispense.member.member_id,
                member_name=dispense.member.name,
                mdb_slot=dispense.selection.slot_number,
                ffweb_product_id=dispense.selection.ffweb_product_id,
                drink_name=dispense.selection.drink_name,
                price_cents=dispense.selection.price_cents,
                vend_status=status_label,
                sync_status="NOT_APPLICABLE",
            )
            self._events.publish(
                "vend_failed_no_charge",
                {
                    "tx_id": tx["id"],
                    "member_name": dispense.member.name,
                    "drink_name": dispense.selection.drink_name,
                    "reason": event.reason,
                    "status": status_label,
                },
            )
            return

        if self.state in {ControllerState.SESSION_ACTIVE, ControllerState.AWAITING_CHIP}:
            self._transition_to_idle(reason=f"mdb_err_{event.reason}")

    async def _handle_status_event(self, event: MdbStatusEvent) -> None:
        if event.status in {"IDLE", "DISABLED", "INACTIVE"} and self.state in {
            ControllerState.SESSION_ACTIVE,
            ControllerState.AWAITING_CHIP,
        }:
            self._transition_to_idle(reason=f"vmc_status_{event.status.lower()}")

    def _record_insufficient_balance(
        self, member: MemberInfo, selection: PendingSelection
    ) -> None:
        self._db.create_transaction(
            idempotency_key=str(uuid.uuid4()),
            chip_uid=member.chip_uid,
            member_id=member.member_id,
            member_name=member.name,
            mdb_slot=selection.slot_number,
            ffweb_product_id=selection.ffweb_product_id,
            drink_name=selection.drink_name,
            price_cents=selection.price_cents,
            vend_status="DENIED_BALANCE",
            sync_status="NOT_APPLICABLE",
        )
        self._events.publish(
            "vend_denied_balance",
            {
                "member_name": member.name,
                "balance_eur": cents_to_eur_str(member.balance_cents),
                "drink_name": selection.drink_name,
                "price_eur": cents_to_eur_str(selection.price_cents),
            },
        )

    # ---------------------------------------------------------
    # Timeouts & Manual Cancel
    # ---------------------------------------------------------
    async def cancel_current_session(self, reason: str = "manual_cancel") -> None:
        if self.state in {
            ControllerState.SELECTING_BILLING,
            ControllerState.SESSION_ACTIVE,
            ControllerState.AWAITING_CHIP,
        }:
            self._transition_to_idle(reason=reason)
            await self._mdb.stop_or_deny()

    async def _on_session_timeout(self) -> None:
        if self.state == ControllerState.SESSION_ACTIVE:
            logger.info("Session timed out without drink selection")
            self._transition_to_idle(reason="session_timeout")
            await self._mdb.stop_or_deny()

    async def _on_awaiting_chip_timeout(self) -> None:
        if self.state == ControllerState.AWAITING_CHIP:
            logger.info("Selection timed out waiting for RFID chip")
            self._transition_to_idle(reason="awaiting_chip_timeout")
            await self._mdb.stop_or_deny()

    async def _on_dispense_timeout(self) -> None:
        if self.state == ControllerState.DISPENSING and self.active_dispense is not None:
            dispense = self.active_dispense
            logger.error("Dispense timed out waiting for VMC optical drop confirmation")
            self._transition_to_idle(reason="dispense_timeout")
            self._db.create_transaction(
                idempotency_key=str(uuid.uuid4()),
                chip_uid=dispense.member.chip_uid,
                member_id=dispense.member.member_id,
                member_name=dispense.member.name,
                mdb_slot=dispense.selection.slot_number,
                ffweb_product_id=dispense.selection.ffweb_product_id,
                drink_name=dispense.selection.drink_name,
                price_cents=dispense.selection.price_cents,
                vend_status="FAILED_DISPENSE",
                sync_status="NOT_APPLICABLE",
            )
            await self._mdb.stop_or_deny()

    def status_snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "active_member": self.active_member.to_dict() if self.active_member else None,
            "billing_mode": self.current_billing_mode,
            "selected_event_list": self.current_event_list,
            "available_billing_options": self.available_billing_options,
            "selected_billing_index": self.selected_billing_index,
            "pending_selection": (
                {
                    "slot_number": self.pending_selection.slot_number,
                    "ffweb_product_id": self.pending_selection.ffweb_product_id,
                    "drink_name": self.pending_selection.drink_name,
                    "price_cents": self.pending_selection.price_cents,
                    "price_eur": cents_to_eur_str(self.pending_selection.price_cents),
                }
                if self.pending_selection
                else None
            ),
            "active_dispense": (
                {
                    "member_name": self.active_dispense.member.name,
                    "slot_number": self.active_dispense.selection.slot_number,
                    "drink_name": self.active_dispense.selection.drink_name,
                    "price_eur": cents_to_eur_str(self.active_dispense.selection.price_cents),
                }
                if self.active_dispense
                else None
            ),
            "last_unknown_chip": self.last_unknown_chip,
            "pending_sync_count": self._db.count_pending_sync(),
        }
