from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.buttons import ButtonAction
from app.db import eur_to_cents
from app.ffweb.mock_client import MockFFWebClient
from app.mdb.simulator import SimulatorMdbDriver
from app.rfid.evdev_reader import list_linux_input_devices

router = APIRouter(prefix="/api")


def verify_admin_token(
    request: Request,
    x_admin_token: str | None = Header(default=None),
) -> None:
    expected = request.app.state.settings.admin_api_token
    if expected and x_admin_token != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid X-Admin-Token header",
        )


# -----------------------------
# Request Schemas
# -----------------------------
class SlotUpsertBody(BaseModel):
    ffweb_product_id: str = Field(..., min_length=1)
    drink_name: str = Field(..., min_length=1)
    price_eur: str | float | None = None
    price_cents: int | None = None
    active: bool = True


class MockMemberUpsertBody(BaseModel):
    member_id: str = Field(..., min_length=1)
    chip_uid: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    balance_eur: str | float | None = None
    balance_cents: int | None = None
    active: bool = True


class MockTopupBody(BaseModel):
    delta_eur: str | float | None = None
    delta_cents: int | None = None


class MockOutageBody(BaseModel):
    simulate_outage: bool


class SimRfidScanBody(BaseModel):
    chip_uid: str = Field(..., min_length=1)


class SimMdbSelectBody(BaseModel):
    slot_number: int = Field(..., ge=1)
    price_cents: int | None = None
    price_eur: str | float | None = None


class SimMdbDispenseBody(BaseModel):
    success: bool = True


class EventListUpsertBody(BaseModel):
    list_id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    active: bool = True
    notes: str = ""


class EventListToggleBody(BaseModel):
    active: bool | None = None


class ButtonPressBody(BaseModel):
    button: str = Field(..., pattern="^(back|select|next|combo_1_3)$")


# -----------------------------
# System Status & Live SSE
# -----------------------------
@router.get("/status")
async def get_status(request: Request) -> dict[str, Any]:
    app_state = request.app.state
    return {
        "controller": app_state.controller.status_snapshot(),
        "mdb": app_state.mdb.status_snapshot(),
        "rfid": app_state.rfid.status_snapshot(),
        "oled": app_state.oled.status_snapshot(),
        "buttons": app_state.buttons.status_snapshot() if hasattr(app_state, "buttons") else None,
        "ffweb": app_state.ffweb.status_snapshot(),
        "recent_events": app_state.events.recent_events(limit=25),
    }


@router.get("/events")
async def stream_events(request: Request) -> StreamingResponse:
    event_bus = request.app.state.events
    queue = event_bus.subscribe()

    async def event_generator():
        try:
            # Send initial connected heartbeat
            yield f"data: {json.dumps({'type': 'connected'})}\n\n"
            while not getattr(request.app.state, "shutting_down", False):
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=10.0)
                    if event.get("type") == "shutdown":
                        break
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            return
        finally:
            event_bus.unsubscribe(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/rfid/devices")
async def get_rfid_devices() -> dict[str, Any]:
    return {"devices": list_linux_input_devices()}


@router.post("/oled/diagnostics")
async def show_oled_diagnostics(request: Request, hold_sec: float = 8.0) -> dict[str, Any]:
    app_state = request.app.state
    summary = await app_state.oled.show_diagnostics(
        rfid_snapshot=app_state.rfid.status_snapshot(),
        mdb_snapshot=app_state.mdb.status_snapshot(),
        ffweb_snapshot=app_state.ffweb.status_snapshot(),
        pending_sync_count=app_state.db.count_pending_sync(),
        hold_sec=max(2.0, min(hold_sec, 60.0)),
    )
    return {"oled": summary}


# -----------------------------
# Slot-to-Drink Mappings
# -----------------------------
@router.get("/slots")
async def list_slots(request: Request) -> dict[str, Any]:
    slots = request.app.state.db.list_slots(active_only=False)
    return {"slots": [s.to_dict() for s in slots]}


@router.put("/slots/{slot_number}", dependencies=[Depends(verify_admin_token)])
async def upsert_slot(
    slot_number: int, body: SlotUpsertBody, request: Request
) -> dict[str, Any]:
    if body.price_cents is not None:
        cents = body.price_cents
    elif body.price_eur is not None:
        cents = eur_to_cents(body.price_eur)
    else:
        raise HTTPException(status_code=400, detail="Provide price_eur or price_cents")

    if cents < 0:
        raise HTTPException(status_code=400, detail="Price cannot be negative")

    slot = request.app.state.db.upsert_slot(
        slot_number=slot_number,
        ffweb_product_id=body.ffweb_product_id.strip(),
        drink_name=body.drink_name.strip(),
        price_cents=cents,
        active=body.active,
    )
    request.app.state.events.publish("slot_updated", slot.to_dict())
    return {"slot": slot.to_dict()}


@router.delete("/slots/{slot_number}", dependencies=[Depends(verify_admin_token)])
async def delete_slot(slot_number: int, request: Request) -> dict[str, Any]:
    deleted = request.app.state.db.delete_slot(slot_number)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Slot {slot_number} not found")
    request.app.state.events.publish("slot_deleted", {"slot_number": slot_number})
    return {"deleted": True, "slot_number": slot_number}


# -----------------------------
# Transactions & Retry Queue
# -----------------------------
@router.get("/transactions")
async def list_transactions(request: Request, limit: int = 50) -> dict[str, Any]:
    capped_limit = max(1, min(limit, 500))
    db = request.app.state.db
    return {
        "transactions": db.list_transactions(limit=capped_limit),
        "pending_sync_count": db.count_pending_sync(),
    }


@router.post("/transactions/retry-sync", dependencies=[Depends(verify_admin_token)])
async def retry_pending_syncs(request: Request) -> dict[str, Any]:
    summary = await request.app.state.sync_worker.flush_due(force_all=True)
    return summary


# -----------------------------
# Mock FFWEB Management (for testing before live FFWEB token)
# -----------------------------
@router.get("/mock-ffweb/members")
async def list_mock_members(request: Request) -> dict[str, Any]:
    ffweb = request.app.state.ffweb
    return {
        "mock_active": isinstance(ffweb, MockFFWebClient),
        "simulate_outage": getattr(ffweb, "simulate_outage", False),
        "members": request.app.state.db.list_mock_members(),
    }


@router.post("/mock-ffweb/members", dependencies=[Depends(verify_admin_token)])
async def upsert_mock_member(
    body: MockMemberUpsertBody, request: Request
) -> dict[str, Any]:
    if body.balance_cents is not None:
        cents = body.balance_cents
    elif body.balance_eur is not None:
        cents = eur_to_cents(body.balance_eur)
    else:
        cents = 0

    try:
        member = request.app.state.db.upsert_mock_member(
            member_id=body.member_id,
            chip_uid=body.chip_uid,
            name=body.name,
            balance_cents=cents,
            active=body.active,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    ctrl = request.app.state.controller
    if ctrl.last_unknown_chip and ctrl.last_unknown_chip.get("chip_uid") == member["chip_uid"]:
        ctrl.last_unknown_chip = None
    request.app.state.events.publish("mock_member_updated", member)
    return {"member": member}


@router.post(
    "/mock-ffweb/members/{member_id}/topup", dependencies=[Depends(verify_admin_token)]
)
async def topup_mock_member(
    member_id: str, body: MockTopupBody, request: Request
) -> dict[str, Any]:
    if body.delta_cents is not None:
        delta = body.delta_cents
    elif body.delta_eur is not None:
        delta = eur_to_cents(body.delta_eur)
    else:
        raise HTTPException(status_code=400, detail="Provide delta_eur or delta_cents")

    updated = request.app.state.db.adjust_mock_member_balance(member_id, delta)
    if not updated:
        raise HTTPException(status_code=404, detail=f"Member {member_id} not found")
    request.app.state.events.publish("mock_member_updated", updated)
    return {"member": updated}


@router.post("/mock-ffweb/outage", dependencies=[Depends(verify_admin_token)])
async def set_mock_outage(body: MockOutageBody, request: Request) -> dict[str, Any]:
    ffweb = request.app.state.ffweb
    if not isinstance(ffweb, MockFFWebClient):
        raise HTTPException(
            status_code=400, detail="Outage simulation is only available when FFWEB_MODE=mock"
        )
    ffweb.simulate_outage = body.simulate_outage
    request.app.state.events.publish(
        "mock_outage_toggled", {"simulate_outage": ffweb.simulate_outage}
    )
    if not ffweb.simulate_outage:
        await request.app.state.sync_worker.flush_due(force_all=True)
    return {"simulate_outage": ffweb.simulate_outage}


# -----------------------------
# Hardware Simulator Controls
# -----------------------------
@router.post("/sim/rfid-scan")
async def sim_rfid_scan(body: SimRfidScanBody, request: Request) -> dict[str, Any]:
    uid = body.chip_uid.strip().upper()
    result = await request.app.state.controller.on_rfid_scan(uid, source="simulator_api")
    return {"result": result, "controller": request.app.state.controller.status_snapshot()}


@router.post("/sim/mdb-select")
async def sim_mdb_select(body: SimMdbSelectBody, request: Request) -> dict[str, Any]:
    mdb = request.app.state.mdb
    if not isinstance(mdb, SimulatorMdbDriver):
        raise HTTPException(
            status_code=400, detail="MDB simulator is only active when MDB_MODE=simulator"
        )

    if body.price_cents is not None:
        price_cents = body.price_cents
    elif body.price_eur is not None:
        price_cents = eur_to_cents(body.price_eur)
    else:
        slot = request.app.state.db.get_slot(body.slot_number)
        price_cents = slot.price_cents if slot else 150

    await mdb.simulate_keypad_selection(body.slot_number, price_cents)
    return {
        "controller": request.app.state.controller.status_snapshot(),
        "mdb": mdb.status_snapshot(),
    }


@router.post("/sim/mdb-dispense")
async def sim_mdb_dispense(body: SimMdbDispenseBody, request: Request) -> dict[str, Any]:
    mdb = request.app.state.mdb
    if not isinstance(mdb, SimulatorMdbDriver):
        raise HTTPException(
            status_code=400, detail="MDB simulator is only active when MDB_MODE=simulator"
        )
    try:
        await mdb.simulate_elevator_dispense(success=body.success)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "controller": request.app.state.controller.status_snapshot(),
        "mdb": mdb.status_snapshot(),
    }


@router.post("/sim/mdb-cancel")
async def sim_mdb_cancel(request: Request) -> dict[str, Any]:
    mdb = request.app.state.mdb
    if isinstance(mdb, SimulatorMdbDriver):
        await mdb.simulate_machine_cancel()
    else:
        await request.app.state.controller.cancel_current_session(reason="api_cancel")
    return {
        "controller": request.app.state.controller.status_snapshot(),
        "mdb": mdb.status_snapshot(),
    }


# -----------------------------
# Event Lists (Verrechnungslisten & Anlässe)
# -----------------------------
@router.get("/event-lists")
async def list_event_lists(request: Request, active_only: bool = False) -> dict[str, Any]:
    lists = request.app.state.db.list_event_lists(active_only=active_only)
    return {"event_lists": lists}


@router.post("/event-lists", dependencies=[Depends(verify_admin_token)])
async def create_event_list(body: EventListUpsertBody, request: Request) -> dict[str, Any]:
    created = request.app.state.db.upsert_event_list(
        list_id=body.list_id,
        name=body.name,
        active=body.active,
        notes=body.notes,
    )
    request.app.state.events.publish("event_list_updated", created)
    return {"event_list": created}


@router.put("/event-lists/{list_id}", dependencies=[Depends(verify_admin_token)])
async def update_event_list(
    list_id: str, body: EventListUpsertBody, request: Request
) -> dict[str, Any]:
    updated = request.app.state.db.upsert_event_list(
        list_id=list_id,
        name=body.name,
        active=body.active,
        notes=body.notes,
    )
    request.app.state.events.publish("event_list_updated", updated)
    return {"event_list": updated}


@router.post("/event-lists/{list_id}/toggle", dependencies=[Depends(verify_admin_token)])
async def toggle_event_list(
    list_id: str, body: EventListToggleBody, request: Request
) -> dict[str, Any]:
    updated = request.app.state.db.toggle_event_list(list_id, active=body.active)
    if not updated:
        raise HTTPException(status_code=404, detail=f"Event list {list_id} not found")
    request.app.state.events.publish("event_list_updated", updated)
    return {"event_list": updated}


@router.delete("/event-lists/{list_id}", dependencies=[Depends(verify_admin_token)])
async def delete_event_list(list_id: str, request: Request) -> dict[str, Any]:
    deleted = request.app.state.db.delete_event_list(list_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Event list {list_id} not found")
    request.app.state.events.publish("event_list_deleted", {"list_id": list_id})
    return {"deleted": True, "list_id": list_id}


# -----------------------------
# Physical / Virtual Button Controls
# -----------------------------
@router.post("/buttons/press")
async def press_button(body: ButtonPressBody, request: Request) -> dict[str, Any]:
    action = ButtonAction(body.button)
    btn_mgr = getattr(request.app.state, "buttons", None)
    if btn_mgr:
        res = await btn_mgr.trigger_button(action)
    else:
        ctrl_res = await request.app.state.controller.on_button_press(action)
        res = {"button": action.value, "controller_result": ctrl_res}

    return {
        "action": action.value,
        "result": res,
        "controller": request.app.state.controller.status_snapshot(),
        "oled": request.app.state.oled.status_snapshot(),
    }


@router.get("/buttons/status")
async def get_button_status(request: Request) -> dict[str, Any]:
    btn_mgr = getattr(request.app.state, "buttons", None)
    if not btn_mgr:
        return {"connected": False, "mode": "disabled"}
    return btn_mgr.status_snapshot()

