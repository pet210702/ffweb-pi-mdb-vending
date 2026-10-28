from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv_file(dotenv_path: Path) -> None:
    """Minimal .env loader so no extra third-party package is required on 32-bit Pi."""
    if not dotenv_path.is_file():
        return
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(slots=True)
class Settings:
    host: str = "0.0.0.0"
    port: int = 8000
    db_path: str = "data/vending.db"
    admin_api_token: str = ""

    # MDB ("simulator" | "qibixx")
    mdb_mode: str = "simulator"
    mdb_serial_port: str = "/dev/ttyACM0"
    mdb_baudrate: int = 115200

    # RFID ("auto" | "evdev" | "simulator")
    rfid_mode: str = "auto"
    rfid_device_path: str = ""

    # 0.96" I2C OLED ("auto" | "ssd1306" | "sh1106" | "disabled")
    oled_mode: str = "auto"
    oled_i2c_port: int = 1
    oled_i2c_address: int = 0x3C
    oled_screensaver_sec: float = 90.0
    oled_startup_diag_sec: float = 6.0

    # Physical Buttons ("auto" | "gpiod" | "simulator" | "disabled")
    buttons_mode: str = "auto"
    buttons_chip_path: str = "/dev/gpiochip0"
    button_pin_back: int = 17   # Button 1 (Left): Zurück / Abbrechen
    button_pin_select: int = 27 # Button 2 (Middle): Auswahl / OK
    button_pin_next: int = 22   # Button 3 (Right): Weiter / Weiterblättern
    button_debounce_ms: int = 50
    billing_selection_timeout_sec: float = 12.0

    # Timeouts & rules
    session_timeout_sec: float = 30.0
    awaiting_chip_timeout_sec: float = 15.0
    dispense_timeout_sec: float = 25.0
    allow_unmapped_slots: bool = False

    # FFWEB ("mock" | "http")
    ffweb_mode: str = "mock"
    ffweb_base_url: str = "https://api.ffweb.at"
    ffweb_api_key: str = ""
    ffweb_timeout_sec: float = 4.0
    ffweb_member_lookup_path: str = "/api/v1/vending/members/{chip_uid}"
    ffweb_products_path: str = "/api/v1/vending/products"
    ffweb_vend_path: str = "/api/v1/vending/consumptions"
    sync_retry_interval_sec: float = 10.0

    @classmethod
    def from_env(cls, dotenv_path: str | Path = ".env") -> Settings:
        _load_dotenv_file(Path(dotenv_path))
        return cls(
            host=os.getenv("HOST", "0.0.0.0"),
            port=int(os.getenv("PORT", "8000")),
            db_path=os.getenv("DB_PATH", "data/vending.db"),
            admin_api_token=os.getenv("ADMIN_API_TOKEN", ""),
            mdb_mode=os.getenv("MDB_MODE", "simulator").strip().lower(),
            mdb_serial_port=os.getenv("MDB_SERIAL_PORT", "/dev/ttyACM0"),
            mdb_baudrate=int(os.getenv("MDB_BAUDRATE", "115200")),
            rfid_mode=os.getenv("RFID_MODE", "auto").strip().lower(),
            rfid_device_path=os.getenv("RFID_DEVICE_PATH", "").strip(),
            oled_mode=os.getenv("OLED_MODE", "auto").strip().lower(),
            oled_i2c_port=int(os.getenv("OLED_I2C_PORT", "1")),
            oled_i2c_address=int(os.getenv("OLED_I2C_ADDRESS", "0x3C"), 0),
            oled_screensaver_sec=float(os.getenv("OLED_SCREENSAVER_SEC", "90")),
            oled_startup_diag_sec=float(os.getenv("OLED_STARTUP_DIAG_SEC", "6")),
            buttons_mode=os.getenv("BUTTONS_MODE", "auto").strip().lower(),
            buttons_chip_path=os.getenv("BUTTONS_CHIP_PATH", "/dev/gpiochip0"),
            button_pin_back=int(os.getenv("BUTTON_PIN_BACK", "17")),
            button_pin_select=int(os.getenv("BUTTON_PIN_SELECT", "27")),
            button_pin_next=int(os.getenv("BUTTON_PIN_NEXT", "22")),
            button_debounce_ms=int(os.getenv("BUTTON_DEBOUNCE_MS", "50")),
            billing_selection_timeout_sec=float(
                os.getenv("BILLING_SELECTION_TIMEOUT_SEC", "12")
            ),
            session_timeout_sec=float(os.getenv("SESSION_TIMEOUT_SEC", "30")),
            awaiting_chip_timeout_sec=float(os.getenv("AWAITING_CHIP_TIMEOUT_SEC", "15")),
            dispense_timeout_sec=float(os.getenv("DISPENSE_TIMEOUT_SEC", "25")),
            allow_unmapped_slots=_env_bool("ALLOW_UNMAPPED_SLOTS", False),
            ffweb_mode=os.getenv("FFWEB_MODE", "mock").strip().lower(),
            ffweb_base_url=os.getenv("FFWEB_BASE_URL", "https://api.ffweb.at").rstrip("/"),
            ffweb_api_key=os.getenv("FFWEB_API_KEY", ""),
            ffweb_timeout_sec=float(os.getenv("FFWEB_TIMEOUT_SEC", "4.0")),
            ffweb_member_lookup_path=os.getenv(
                "FFWEB_MEMBER_LOOKUP_PATH", "/api/v1/vending/members/{chip_uid}"
            ),
            ffweb_products_path=os.getenv(
                "FFWEB_PRODUCTS_PATH", "/api/v1/vending/products"
            ),
            ffweb_vend_path=os.getenv(
                "FFWEB_VEND_PATH", "/api/v1/vending/consumptions"
            ),
            sync_retry_interval_sec=float(os.getenv("SYNC_RETRY_INTERVAL_SEC", "10")),
        )
