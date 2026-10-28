from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.ffweb.http_client import HttpFFWebClient
from app.ffweb.base import VendBookingRequest
from app.main import create_app
from app.mdb.base import (
    MdbStatusEvent,
    MdbVendErrorEvent,
    MdbVendRequestEvent,
    MdbVendSuccessEvent,
    parse_qibixx_line,
)


def make_test_settings(tmp_path: Path, **overrides) -> Settings:
    defaults = dict(
        db_path=str(tmp_path / "test_vending.db"),
        mdb_mode="simulator",
        rfid_mode="simulator",
        ffweb_mode="mock",
        session_timeout_sec=5.0,
        awaiting_chip_timeout_sec=5.0,
        dispense_timeout_sec=5.0,
        sync_retry_interval_sec=60.0,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def test_qibixx_parser_variants() -> None:
    ev1 = parse_qibixx_line("c,STATUS,ENABLED\r\n")
    assert isinstance(ev1, MdbStatusEvent)
    assert ev1.status == "ENABLED"

    ev2 = parse_qibixx_line("c,STATUS,VEND,1.50,11")
    assert isinstance(ev2, MdbVendRequestEvent)
    assert ev2.price_cents == 150
    assert ev2.slot_number == 11

    ev3 = parse_qibixx_line("c,VEND,SUCCESS")
    assert isinstance(ev3, MdbVendSuccessEvent)

    ev4 = parse_qibixx_line("c,ERR,VEND 3")
    assert isinstance(ev4, MdbVendErrorEvent)
    assert ev4.reason == "VEND 3"


def test_happy_path_scan_first_vend_and_balance_deduction(tmp_path: Path) -> None:
    settings = make_test_settings(tmp_path)
    app = create_app(settings)

    with TestClient(app) as client:
        # 1. Scan Kdt. Florian Huber's chip (seeded with 25.00 EUR = 2500 cents)
        scan_res = client.post("/api/sim/rfid-scan", json={"chip_uid": "0001234567"})
        assert scan_res.status_code == 200
        body = scan_res.json()
        assert body["result"]["action"] == "session_started"
        assert body["controller"]["state"] == "SESSION_ACTIVE"

        status_res = client.get("/api/status").json()
        assert status_res["mdb"]["last_tx"] == "C,START,25.00"

        # 2. Select Slot 11 (Coca-Cola 0,5L @ 1.50 EUR)
        sel_res = client.post("/api/sim/mdb-select", json={"slot_number": 11})
        assert sel_res.status_code == 200
        assert sel_res.json()["controller"]["state"] == "DISPENSING"
        assert sel_res.json()["mdb"]["last_tx"] == "C,VEND,1.50"

        # Balance MUST NOT be deducted yet before optical drop sensor confirms delivery!
        members_mid = client.get("/api/mock-ffweb/members").json()["members"]
        florian_mid = next(m for m in members_mid if m["chip_uid"] == "0001234567")
        assert florian_mid["balance_cents"] == 2500

        # 3. Elevator optical sensor confirms bottle dispensed (c,VEND,SUCCESS)
        disp_res = client.post("/api/sim/mdb-dispense", json={"success": True})
        assert disp_res.status_code == 200
        assert disp_res.json()["controller"]["state"] == "IDLE"

        # Verify balance deducted to 23.50 EUR (2350 cents) and transaction synced
        members_after = client.get("/api/mock-ffweb/members").json()["members"]
        florian_after = next(m for m in members_after if m["chip_uid"] == "0001234567")
        assert florian_after["balance_cents"] == 2350

        tx_list = client.get("/api/transactions").json()["transactions"]
        assert len(tx_list) == 1
        assert tx_list[0]["vend_status"] == "DISPENSED"
        assert tx_list[0]["sync_status"] == "SYNCED"
        assert tx_list[0]["drink_name"] == "Coca-Cola 0,5L"
        assert tx_list[0]["mdb_slot"] == 11
        assert tx_list[0]["price_cents"] == 150


def test_bottle_jam_charges_zero(tmp_path: Path) -> None:
    settings = make_test_settings(tmp_path)
    app = create_app(settings)

    with TestClient(app) as client:
        client.post("/api/sim/rfid-scan", json={"chip_uid": "0001234567"})
        client.post("/api/sim/mdb-select", json={"slot_number": 12})  # Almdudler 1.50 EUR

        # Simulate jammed bottle (c,ERR,VEND 3)
        disp_res = client.post("/api/sim/mdb-dispense", json={"success": False})
        assert disp_res.status_code == 200
        assert disp_res.json()["controller"]["state"] == "IDLE"

        # Balance must remain untouched at 25.00 EUR
        members = client.get("/api/mock-ffweb/members").json()["members"]
        florian = next(m for m in members if m["chip_uid"] == "0001234567")
        assert florian["balance_cents"] == 2500

        tx = client.get("/api/transactions").json()["transactions"][0]
        assert tx["vend_status"] == "FAILED_DISPENSE"
        assert tx["sync_status"] == "NOT_APPLICABLE"


def test_insufficient_balance_and_select_first_mode(tmp_path: Path) -> None:
    settings = make_test_settings(tmp_path)
    app = create_app(settings)

    with TestClient(app) as client:
        # Select Slot 11 (1.50 EUR) BEFORE scanning chip ("Always Idle" mode)
        sel_res = client.post("/api/sim/mdb-select", json={"slot_number": 11})
        assert sel_res.json()["controller"]["state"] == "AWAITING_CHIP"

        # Scan Lukas Steiner's chip (seeded with only 1.00 EUR) -> must deny!
        scan_low = client.post("/api/sim/rfid-scan", json={"chip_uid": "0000001111"})
        assert scan_low.json()["result"]["action"] == "denied_balance"
        assert scan_low.json()["controller"]["state"] == "IDLE"

        # Now select Slot 13 (Römerquelle 1.00 EUR) first, then scan Lukas's 1.00 EUR chip -> approves!
        client.post("/api/sim/mdb-select", json={"slot_number": 13})
        scan_ok = client.post("/api/sim/rfid-scan", json={"chip_uid": "0000001111"})
        assert scan_ok.json()["result"]["action"] == "vend_approved"
        assert scan_ok.json()["controller"]["state"] == "DISPENSING"

        client.post("/api/sim/mdb-dispense", json={"success": True})
        members = client.get("/api/mock-ffweb/members").json()["members"]
        lukas = next(m for m in members if m["chip_uid"] == "0000001111")
        assert lukas["balance_cents"] == 0


def test_mid_vend_ffweb_outage_queues_and_recovers_idempotently(tmp_path: Path) -> None:
    settings = make_test_settings(tmp_path)
    app = create_app(settings)

    with TestClient(app) as client:
        # Scan & select while FFWEB is online
        client.post("/api/sim/rfid-scan", json={"chip_uid": "0009876543"})  # 12.50 EUR
        client.post("/api/sim/mdb-select", json={"slot_number": 15})  # Obi G'spritzt 1.50 EUR

        # FFWEB network drops right while elevator is dispensing!
        client.post("/api/mock-ffweb/outage", json={"simulate_outage": True})

        # Bottle drops -> vend succeeds locally and queues PENDING_SYNC
        client.post("/api/sim/mdb-dispense", json={"success": True})
        tx_data = client.get("/api/transactions").json()
        assert tx_data["pending_sync_count"] == 1
        assert tx_data["transactions"][0]["sync_status"] == "PENDING_SYNC"

        # Bring FFWEB back online -> auto-flushes queue once without double-charging
        client.post("/api/mock-ffweb/outage", json={"simulate_outage": False})
        tx_after = client.get("/api/transactions").json()
        assert tx_after["pending_sync_count"] == 0
        assert tx_after["transactions"][0]["sync_status"] == "SYNCED"

        members = client.get("/api/mock-ffweb/members").json()["members"]
        markus = next(m for m in members if m["chip_uid"] == "0009876543")
        assert markus["balance_cents"] == 1100


@pytest.mark.asyncio
async def test_http_ffweb_client_contract() -> None:
    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        if request.method == "GET" and "/members/04AABBCC" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "id": "M-42",
                    "chip_uid": "04AABBCC",
                    "name": "OBI Stefan Berger",
                    "balance": "14.50",
                    "active": True,
                },
            )
        if request.method == "POST" and "/consumptions" in str(request.url):
            return httpx.Response(201, json={"balance": "13.00"})
        return httpx.Response(404)

    client = HttpFFWebClient(
        base_url="https://api.ffweb.at",
        api_key="secret-station-token",
    )
    # Replace internal transport with MockTransport
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="https://api.ffweb.at",
        headers={"Authorization": "Bearer secret-station-token"},
        transport=httpx.MockTransport(handler),
    )

    try:
        member = await client.get_member_by_chip("04AABBCC")
        assert member is not None
        assert member.member_id == "M-42"
        assert member.balance_cents == 1450

        new_bal = await client.record_vend(
            VendBookingRequest(
                idempotency_key="idem-123",
                member_id="M-42",
                chip_uid="04AABBCC",
                product_id="FF-COLA-05",
                drink_name="Coca-Cola 0,5L",
                mdb_slot=11,
                price_cents=150,
                created_at="2026-10-27T17:00:00+00:00",
            )
        )
        assert new_bal == 1300
        post_req = captured_requests[-1]
        assert post_req.headers["X-Idempotency-Key"] == "idem-123"
        payload = json.loads(post_req.content.decode())
        assert payload["price"] == 1.5
        assert payload["mdb_slot"] == 11
    finally:
        await client.close()


def test_unknown_chip_unmapped_slot_and_second_tap_cancel(tmp_path: Path) -> None:
    settings = make_test_settings(tmp_path)
    app = create_app(settings)

    with TestClient(app) as client:
        # 1. Scan unknown firefighter chip -> sets last_unknown_chip banner
        unk = client.post("/api/sim/rfid-scan", json={"chip_uid": "deadbeef99"}).json()
        assert unk["result"]["action"] == "denied_unknown_chip"
        assert unk["controller"]["last_unknown_chip"]["chip_uid"] == "DEADBEEF99"

        # Registering that chip clears the unknown chip banner automatically
        reg = client.post(
            "/api/mock-ffweb/members",
            json={
                "member_id": "FF-999",
                "chip_uid": "DEADBEEF99",
                "name": "FM Max Mustermann",
                "balance_eur": "10.00",
            },
        )
        assert reg.status_code == 200
        assert client.get("/api/status").json()["controller"]["last_unknown_chip"] is None

        # 2. Scan registered chip -> starts session, then tap SAME chip again -> cancels session
        s1 = client.post("/api/sim/rfid-scan", json={"chip_uid": "DEADBEEF99"}).json()
        assert s1["controller"]["state"] == "SESSION_ACTIVE"
        s2 = client.post("/api/sim/rfid-scan", json={"chip_uid": "DEADBEEF99"}).json()
        assert s2["result"]["action"] == "session_cancelled"
        assert s2["controller"]["state"] == "IDLE"

        # 3. Unmapped slot 99 -> denied safely
        client.post("/api/sim/rfid-scan", json={"chip_uid": "DEADBEEF99"})
        unmapped = client.post("/api/sim/mdb-select", json={"slot_number": 99}).json()
        assert unmapped["controller"]["state"] == "IDLE"
        txs = client.get("/api/transactions").json()["transactions"]
        assert txs[0]["vend_status"] == "DENIED_UNMAPPED_SLOT"


def test_oled_text_fitting_never_exceeds_122px() -> None:
    pil = pytest.importorskip("PIL")
    from PIL import Image, ImageDraw
    from app.events import EventBus
    from app.oled.display import OledDisplayManager

    mgr = OledDisplayManager(events=EventBus(), mode="disabled")
    img = Image.new("1", (128, 64), 0)
    draw = ImageDraw.Draw(img)

    for sample in (
        "FF GETRÄNKEAUTOMAT",
        "Chip vorhalten",
        "Römerquelle prickelnd 0,5L",
        "FM Lukas Steiner (Wenig Guthaben)",
    ):
        txt, _font, width = mgr._fit_text(
            draw, sample, bold=True, max_size=16, min_size=8, max_width=122
        )
        assert width <= 122, f"{txt!r} exceeded 122px ({width}px)"


def test_startup_and_manual_oled_diagnostics(tmp_path: Path) -> None:
    settings = make_test_settings(tmp_path, oled_startup_diag_sec=6.0)
    app = create_app(settings)

    with TestClient(app) as client:
        # Immediately after startup, OLED holds the 4-line diagnostic summary
        status = client.get("/api/status").json()
        assert status["oled"]["screen"]["header"] == "STATUS / DIAGNOSE"
        assert len(status["oled"]["screen"]["debug_lines"]) == 3
        assert status["oled"]["screen"]["debug_lines"][0].startswith("IP:")

        # Scanning a chip immediately overrides the diagnostic hold screen
        client.post("/api/sim/rfid-scan", json={"chip_uid": "0001234567"})
        after_scan = client.get("/api/status").json()
        assert after_scan["oled"]["screen"]["header"] == "GUTHABEN AKTIV"

        # Manual diagnostic trigger via API works anytime
        diag_res = client.post("/api/oled/diagnostics?hold_sec=5").json()
        assert diag_res["oled"]["header"] == "STATUS / DIAGNOSE"
        assert "RFID:" in diag_res["oled"]["lines"][1]
        assert "FFWEB:" in diag_res["oled"]["lines"][2]



