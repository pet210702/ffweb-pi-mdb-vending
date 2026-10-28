from __future__ import annotations

from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from app.buttons import ButtonAction
from app.config import Settings
from app.main import create_app


def make_test_settings(tmp_path: Path, **overrides) -> Settings:
    defaults = dict(
        db_path=str(tmp_path / "test_events_vending.db"),
        mdb_mode="simulator",
        rfid_mode="simulator",
        ffweb_mode="mock",
        buttons_mode="simulator",
        billing_selection_timeout_sec=5.0,
        session_timeout_sec=5.0,
        awaiting_chip_timeout_sec=5.0,
        dispense_timeout_sec=5.0,
        sync_retry_interval_sec=60.0,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def test_event_list_crud_and_toggle(tmp_path: Path) -> None:
    settings = make_test_settings(tmp_path)
    app = create_app(settings)

    with TestClient(app) as client:
        # Check initial seeded lists
        res = client.get("/api/event-lists")
        assert res.status_code == 200
        lists = res.json()["event_lists"]
        assert len(lists) >= 2
        nachbespr = next(l for l in lists if l["list_id"] == "LIST-NACHBESPR")
        assert nachbespr["name"] == "Nachbesprechung"
        assert nachbespr["active"] is False

        # Toggle Nachbesprechung to active
        toggle_res = client.post("/api/event-lists/LIST-NACHBESPR/toggle", json={"active": True})
        assert toggle_res.status_code == 200
        assert toggle_res.json()["event_list"]["active"] is True

        # Query active-only
        active_res = client.get("/api/event-lists?active_only=true")
        assert active_res.status_code == 200
        active_lists = active_res.json()["event_lists"]
        assert len(active_lists) == 1
        assert active_lists[0]["list_id"] == "LIST-NACHBESPR"

        # Create a new event list
        new_res = client.post(
            "/api/event-lists",
            json={
                "list_id": "LIST-BRAND-2026",
                "name": "Großbrand Industriegebiet",
                "active": True,
                "notes": "Einsatzverpflegung Station",
            },
        )
        assert new_res.status_code == 200
        assert new_res.json()["event_list"]["name"] == "Großbrand Industriegebiet"

        active_res2 = client.get("/api/event-lists?active_only=true")
        assert len(active_res2.json()["event_lists"]) == 2

        # Delete the created list
        del_res = client.delete("/api/event-lists/LIST-BRAND-2026")
        assert del_res.status_code == 200
        assert del_res.json()["deleted"] is True


def test_billing_selection_navigation_and_cancel_with_back(tmp_path: Path) -> None:
    settings = make_test_settings(tmp_path)
    app = create_app(settings)

    with TestClient(app) as client:
        # Activate Nachbesprechung
        client.post("/api/event-lists/LIST-NACHBESPR/toggle", json={"active": True})

        # Scan Florian's chip (balance 25.00 EUR)
        scan_res = client.post("/api/sim/rfid-scan", json={"chip_uid": "0001234567"})
        assert scan_res.status_code == 200
        res_data = scan_res.json()
        assert res_data["result"]["action"] == "selecting_billing"
        assert res_data["controller"]["state"] == "SELECTING_BILLING"
        assert res_data["controller"]["selected_billing_index"] == 0

        # Press NEXT button -> index becomes 1 (Nachbesprechung)
        next_res = client.post("/api/buttons/press", json={"button": "next"})
        assert next_res.status_code == 200
        assert next_res.json()["controller"]["selected_billing_index"] == 1
        current_opt = next_res.json()["controller"]["available_billing_options"][1]
        assert current_opt["name"] == "Nachbesprechung"
        assert current_opt["mode"] == "event_list"

        # Press NEXT button again -> wraps back to 0 (Privat)
        next_res2 = client.post("/api/buttons/press", json={"button": "next"})
        assert next_res2.json()["controller"]["selected_billing_index"] == 0

        # Press BACK button -> cancels session cleanly back to IDLE
        back_res = client.post("/api/buttons/press", json={"button": "back"})
        assert back_res.status_code == 200
        assert back_res.json()["controller"]["state"] == "IDLE"


def test_station_covered_event_drink_zero_member_charge(tmp_path: Path) -> None:
    settings = make_test_settings(tmp_path)
    app = create_app(settings)

    with TestClient(app) as client:
        # Activate Nachbesprechung
        client.post("/api/event-lists/LIST-NACHBESPR/toggle", json={"active": True})

        # Scan LM Markus Gruber (chip 0009876543, initial balance 12.50 EUR = 1250 cents)
        scan_res = client.post("/api/sim/rfid-scan", json={"chip_uid": "0009876543"})
        assert scan_res.status_code == 200
        assert scan_res.json()["controller"]["state"] == "SELECTING_BILLING"

        # Press NEXT to select Nachbesprechung (index 1)
        client.post("/api/buttons/press", json={"button": "next"})

        # Press SELECT to confirm Nachbesprechung
        sel_res = client.post("/api/buttons/press", json={"button": "select"})
        assert sel_res.status_code == 200
        ctrl = sel_res.json()["controller"]
        assert ctrl["state"] == "SESSION_ACTIVE"
        assert ctrl["billing_mode"] == "event_list"
        assert ctrl["selected_event_list"]["id"] == "LIST-NACHBESPR"

        # Select Slot 12 (Almdudler 0,5L @ 1.50 EUR) on machine keypad
        select_slot_res = client.post("/api/sim/mdb-select", json={"slot_number": 12})
        assert select_slot_res.status_code == 200
        assert select_slot_res.json()["controller"]["state"] == "DISPENSING"

        # Elevator drops drink
        dispense_res = client.post("/api/sim/mdb-dispense", json={"success": True})
        assert dispense_res.status_code == 200
        assert dispense_res.json()["controller"]["state"] == "IDLE"

        # Verify: Markus Gruber's personal balance MUST BE 100% UNCHANGED (1250 cents = 12.50 EUR)!
        members = client.get("/api/mock-ffweb/members").json()["members"]
        gruber = next(m for m in members if m["chip_uid"] == "0009876543")
        assert gruber["balance_cents"] == 1250

        # Verify transaction logged with billing_mode='event_list' and member_balance_deducted=0
        tx_list = client.get("/api/transactions").json()["transactions"]
        assert len(tx_list) == 1
        tx = tx_list[0]
        assert tx["billing_mode"] == "event_list"
        assert tx["event_list_id"] == "LIST-NACHBESPR"
        assert tx["event_list_name"] == "Nachbesprechung"
        assert tx["price_cents"] == 150
        assert tx["member_balance_deducted"] == 0
        assert tx["vend_status"] == "DISPENSED"
        assert tx["sync_status"] == "SYNCED"

        # Verify event list stats: 1 drink, 1.50 EUR station cost
        lists = client.get("/api/event-lists").json()["event_lists"]
        nachbespr = next(l for l in lists if l["list_id"] == "LIST-NACHBESPR")
        assert nachbespr["consumption_count"] == 1
        assert nachbespr["total_cost_cents"] == 150
        assert nachbespr["total_cost_eur"] == "1.50"


def test_zero_balance_member_can_drink_on_event_list_but_denied_private(tmp_path: Path) -> None:
    settings = make_test_settings(tmp_path)
    app = create_app(settings)

    with TestClient(app) as client:
        # Create firefighter with 0.00 EUR balance
        client.post(
            "/api/mock-ffweb/members",
            json={
                "member_id": "FF-BROKE",
                "chip_uid": "0099999999",
                "name": "Probefeuerwehrmann Null",
                "balance_cents": 0,
                "active": True,
            },
        )
        client.post("/api/event-lists/LIST-NACHBESPR/toggle", json={"active": True})

        # Case A: Scans chip, tries to select Privat (Option 0) -> Denied balance!
        client.post("/api/sim/rfid-scan", json={"chip_uid": "0099999999"})
        denied_res = client.post("/api/buttons/press", json={"button": "select"})
        assert denied_res.status_code == 200
        assert denied_res.json()["controller"]["state"] == "IDLE"

        # Case B: Scans chip, selects Nachbesprechung (Option 1) -> Allowed! Station covers it!
        client.post("/api/sim/rfid-scan", json={"chip_uid": "0099999999"})
        client.post("/api/buttons/press", json={"button": "next"})  # move to Nachbesprechung
        approved_res = client.post("/api/buttons/press", json={"button": "select"})
        assert approved_res.status_code == 200
        assert approved_res.json()["controller"]["state"] == "SESSION_ACTIVE"
        assert approved_res.json()["controller"]["billing_mode"] == "event_list"


def test_physical_back_button_cancels_active_session(tmp_path: Path) -> None:
    settings = make_test_settings(tmp_path)
    app = create_app(settings)

    with TestClient(app) as client:
        # Start normal session
        client.post("/api/sim/rfid-scan", json={"chip_uid": "0001234567"})
        assert client.get("/api/status").json()["controller"]["state"] == "SESSION_ACTIVE"

        # Press physical Back button (GPIO 17 / Pin 11)
        res = client.post("/api/buttons/press", json={"button": "back"})
        assert res.status_code == 200
        assert res.json()["controller"]["state"] == "IDLE"
        assert client.get("/api/status").json()["mdb"]["last_tx"] == "C,STOP"


def test_combo_1_3_calls_debug_menu_for_10s(tmp_path: Path) -> None:
    settings = make_test_settings(tmp_path)
    app = create_app(settings)

    with TestClient(app) as client:
        # Press Button 1 and Button 3 together (combo_1_3)
        res = client.post("/api/buttons/press", json={"button": "combo_1_3"})
        assert res.status_code == 200
        data = res.json()
        assert data["action"] == "combo_1_3"

        # Verify OLED debug frame was invoked with 10 seconds hold
        oled_screen = data["oled"]["screen"]
        assert oled_screen["header"] == "STATUS / DIAGNOSE"
        assert len(oled_screen["debug_lines"]) == 3
        assert "IP:" in oled_screen["debug_lines"][0]
        assert "RFID:" in oled_screen["debug_lines"][1]
        assert "FFWEB:" in oled_screen["debug_lines"][2]

        # Verify controller remains cleanly in IDLE
        assert data["controller"]["state"] == "IDLE"


def test_oled_shows_error_when_rfid_disconnected(tmp_path: Path) -> None:
    import time

    settings = make_test_settings(tmp_path, oled_startup_diag_sec=0.0)
    app = create_app(settings)

    with TestClient(app) as client:
        # Initially in simulator mode -> Bereit / Chip vorhalten
        initial = client.get("/api/status").json()["oled"]["screen"]
        assert initial["header"] == "FF GETRÄNKEAUTOMAT"
        assert initial["line1"] == "Bereit"
        assert initial["line2"] == "Chip vorhalten"

        # Simulate RFID sensor disconnection
        app.state.rfid.set_simulated_disconnect(True)
        time.sleep(0.05)

        disconnected = client.get("/api/status").json()["oled"]["screen"]
        assert disconnected["header"] == "FEHLER: RFID"
        assert disconnected["line1"] == "Lesegerät getrennt"
        assert disconnected["line2"] == "USB prüfen!"

        # Reconnect RFID sensor -> immediately returns to Bereit / Chip vorhalten
        app.state.rfid.set_simulated_disconnect(False)
        time.sleep(0.05)

        reconnected = client.get("/api/status").json()["oled"]["screen"]
        assert reconnected["header"] == "FF GETRÄNKEAUTOMAT"
        assert reconnected["line1"] == "Bereit"
        assert reconnected["line2"] == "Chip vorhalten"


