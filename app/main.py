from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import router as api_router
from app.buttons.manager import ButtonManager
from app.config import Settings
from app.db import VendingDatabase
from app.events import EventBus
from app.ffweb.base import FFWebClient
from app.ffweb.http_client import HttpFFWebClient
from app.ffweb.mock_client import MockFFWebClient
from app.ffweb.sync_worker import FFWebSyncWorker
from app.mdb.base import MdbDriver
from app.mdb.qibixx_serial import QibixxSerialMdbDriver
from app.mdb.simulator import SimulatorMdbDriver
from app.oled.display import OledDisplayManager
from app.rfid.evdev_reader import RfidReaderManager
from app.state_machine import VendingController

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

STATIC_DIR = Path(__file__).resolve().parent / "static"


def _build_mdb_driver(settings: Settings) -> MdbDriver:
    if settings.mdb_mode == "qibixx":
        return QibixxSerialMdbDriver(
            port=settings.mdb_serial_port,
            baudrate=settings.mdb_baudrate,
        )
    return SimulatorMdbDriver()


def _build_ffweb_client(settings: Settings, db: VendingDatabase) -> FFWebClient:
    if settings.ffweb_mode == "http":
        return HttpFFWebClient(
            base_url=settings.ffweb_base_url,
            api_key=settings.ffweb_api_key,
            timeout_sec=settings.ffweb_timeout_sec,
            member_lookup_path=settings.ffweb_member_lookup_path,
            products_path=settings.ffweb_products_path,
            vend_path=settings.ffweb_vend_path,
        )
    return MockFFWebClient(db)


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = VendingDatabase(cfg.db_path)
        events = EventBus()
        mdb = _build_mdb_driver(cfg)
        ffweb = _build_ffweb_client(cfg, db)
        sync_worker = FFWebSyncWorker(
            db=db,
            ffweb=ffweb,
            events=events,
            interval_sec=cfg.sync_retry_interval_sec,
        )
        controller = VendingController(
            settings=cfg,
            db=db,
            mdb=mdb,
            ffweb=ffweb,
            sync_worker=sync_worker,
            events=events,
        )
        rfid = RfidReaderManager(
            mode=cfg.rfid_mode,
            configured_device_path=cfg.rfid_device_path,
            events=events,
        )
        oled = OledDisplayManager(
            events=events,
            mode=cfg.oled_mode,
            i2c_port=cfg.oled_i2c_port,
            i2c_address=cfg.oled_i2c_address,
            screensaver_sec=cfg.oled_screensaver_sec,
            rfid_status_fn=rfid.status_snapshot,
        )
        buttons = ButtonManager(
            events=events,
            mode=cfg.buttons_mode,
            chip_path=cfg.buttons_chip_path,
            pin_back=cfg.button_pin_back,
            pin_select=cfg.button_pin_select,
            pin_next=cfg.button_pin_next,
            debounce_ms=cfg.button_debounce_ms,
        )

        app.state.settings = cfg
        app.state.db = db
        app.state.events = events
        app.state.mdb = mdb
        app.state.ffweb = ffweb
        app.state.sync_worker = sync_worker
        app.state.controller = controller
        app.state.rfid = rfid
        app.state.oled = oled
        app.state.buttons = buttons
        app.state.shutting_down = False

        async def _on_rfid(uid: str, src: str) -> None:
            await controller.on_rfid_scan(uid, source=src)

        async def _on_button(action) -> None:
            await controller.on_button_press(action)

        async def _trigger_diagnostics(hold_sec: float = 10.0) -> dict[str, Any]:
            return await oled.show_diagnostics(
                rfid_snapshot=rfid.status_snapshot(),
                mdb_snapshot=mdb.status_snapshot(),
                ffweb_snapshot=ffweb.status_snapshot(),
                pending_sync_count=db.count_pending_sync(),
                hold_sec=hold_sec,
            )

        controller.set_diagnostics_callback(_trigger_diagnostics)

        await rfid.start(_on_rfid)
        await oled.start()
        await mdb.start(controller.on_mdb_event)
        await sync_worker.start()
        await buttons.start(_on_button)

        if cfg.oled_mode != "disabled" and cfg.oled_startup_diag_sec > 0:
            if Path(f"/dev/i2c-{cfg.oled_i2c_port}").exists():
                await asyncio.sleep(0.2)
            await oled.show_diagnostics(
                rfid_snapshot=rfid.status_snapshot(),
                mdb_snapshot=mdb.status_snapshot(),
                ffweb_snapshot=ffweb.status_snapshot(),
                pending_sync_count=db.count_pending_sync(),
                hold_sec=cfg.oled_startup_diag_sec,
            )

        try:
            yield
        finally:
            app.state.shutting_down = True
            events.publish("shutdown", {})
            await buttons.stop()
            await rfid.stop()
            await sync_worker.stop()
            await controller.shutdown()
            await mdb.stop()
            await oled.stop()
            await ffweb.close()
            db.close()

    app = FastAPI(
        title="FF Getränkeautomat — G-Drink DM9 & FFWEB Controller",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.include_router(api_router)

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    async def root_dashboard() -> FileResponse:
        return FileResponse(str(STATIC_DIR / "index.html"))

    return app


app = create_app()
