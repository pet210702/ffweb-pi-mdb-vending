# FF Getränkeautomat — SandenVendo G-Drink DM9 & FFWEB MDB Controller

Open-source Raspberry Pi vending machine controller that bridges a **SandenVendo G-Drink DM9** (via the **MDB Cashless** bus), a **13.56 MHz USB RFID reader**, a **0.96" I2C OLED display**, **3 physical navigation buttons**, and **FFWEB (`https://www.ffweb.at`)** for fire station beverage management.

Designed to run reliably even on a single-core **Raspberry Pi 1 Model B+ (ARMv6, 512 MB RAM)** as well as newer Raspberry Pi models (Pi Zero 2 W, Pi 3, Pi 4, Pi 5).

---

## ✨ Key Features

- **Card-First & Selection-First ("Always Idle") MDB Flows:**
  - *Card-First:* Scan your firefighter RFID chip first → select between private balance or an open station event list (*Verrechnungsliste*) → credit is authorized on the G-Drink DM9 → select your drink on the machine keypad.
  - *Selection-First:* Select a drink slot on the G-Drink DM9 first → the OLED prompts `Chip vorhalten` with the drink name & price → scan your chip to authorize and dispense immediately.
- **Station Event Lists (*Verrechnungslisten / Anlässe*):**
  - Create open lists for alarms (*Einsatz*), drills (*Übung*), or station events (*Kameradschaft / Sitzung*) via the Web Dashboard.
  - When one or more lists are open, members use the **3 physical buttons** (`Zurück`, `OK`, `Weiter`) to choose between **Privat (Guthaben)** (deducted from personal balance) or an active **Verrechnungsliste** (`0.00 EUR` personal charge, booked to the event list).
- **Safe Dispense Accounting (Zero Charge on Jam/Cancel):**
  - Uses integer-cent arithmetic throughout the entire stack.
  - Deducts balance **only** after the G-Drink DM9's optical elevator delivery sensor confirms `c,VEND,SUCCESS`. Jammed bottles (`c,ERR,VEND 3`) or cancelled selections (`c,ERR,VEND 1`) charge `0.00 EUR`.
- **Offline-First SQLite WAL Queue & Idempotent FFWEB Sync:**
  - Completed vends are persisted immediately to a local SQLite database (`WAL` mode) with a unique UUID `idempotency_key`.
  - If station Wi-Fi/LAN or FFWEB is temporarily unreachable, a background worker retries pending transactions automatically without double-charging.
- **0.96" I2C OLED Status Display & Real-Time Hardware Monitoring:**
  - Crisp 4-line status display (`SSD1306` / `SH1106` `128x64`) with automatic screensaver, early-boot splash service, and FreeType text-fitting cache.
  - **Live USB RFID Watchdog:** Immediately displays `FEHLER: RFID / Lesegerät getrennt / USB prüfen!` on the OLED if the USB RFID reader is unplugged, and recovers within ~1.5 seconds when reconnected.
  - **1+3 Button Diagnostic Combo:** Pressing **Button 1 (Left)** and **Button 3 (Right)** simultaneously opens a 10-second live diagnostic screen showing LAN IP, MDB status, RFID status, and pending sync queue count.
- **Built-in Web Dashboard & Virtual G-Drink DM9 Simulator (`/`):**
  - Manage slot-to-drink mappings, test chips, event lists, and inspect live hardware states via Server-Sent Events (SSE).
  - Full browser-based simulator for the G-Drink DM9, RFID scanner, 3 buttons, and 128x64 OLED mirror — develop and test everything before connecting physical MDB hardware.

---

## 🛒 Bill of Materials (Parts List)

| Component | Recommended Model / Specification | Interface | Notes |
| :--- | :--- | :--- | :--- |
| **Single-Board Computer** | Raspberry Pi 1 Model B+ Rev 1.2 (or Pi 3B+ / 4B / Zero 2 W) | Ethernet / USB / 40-pin GPIO | Runs Debian Trixie / Raspberry Pi OS (32-bit or 64-bit) |
| **Cooling (Recommended for Pi 1)** | 5V Aluminum Heatsink + Fan Cooler + Copper Shim (`15x15mm`) | 5V & GND GPIO | Allows stable 1000 MHz Turbo overclock at ~30 °C |
| **MDB Cashless Interface** | **Qibixx MDB-USB Interface** (or Qibixx MDB Pi HAT) | USB CDC Serial (`/dev/ttyACM0`) | Connects Raspberry Pi USB to the vending machine's 6-pin Molex MDB bus |
| **RFID Chip Reader** | **13.56 MHz USB HID RFID Reader** (e.g., `Sycreader` / `IC_Reader 08ff:0009`) | USB HID (`evdev`) | Reads 10-digit decimal UID from MIFARE Classic / NFC keyfobs |
| **OLED Display** | **0.96" I2C OLED Display (`128x64`)** (`SSD1306` or `SH1106`) | I2C (`/dev/i2c-1`, `0x3C`) | 4-pin module (`VCC`, `GND`, `SCL`, `SDA`) |
| **Navigation Buttons (3x)** | Momentary Push Buttons (Tactile or 12mm/16mm Panel-Mount Arcade Buttons) | GPIO + GND (`Active-Low`) | Uses internal Raspberry Pi `PULL_UP` resistors — no external resistors required |
| **Power Supply & Storage** | Official 5V Micro-USB / USB-C PSU + 16 GB Industrial microSD Card | Power / SD | High-endurance microSD recommended for continuous operation |
| **Vending Machine** | **SandenVendo G-Drink DM9** (with SF01 Controller Board) | MDB Slave | Elevator bottle/can vending machine |

---

## 🔌 Wiring & Pinout Guide (Raspberry Pi 40-Pin GPIO Header)

All components connect directly to the Raspberry Pi's 40-pin header without requiring an intermediate HAT:

```text
                 Raspberry Pi 40-Pin GPIO Header (Top Pins 1–20)
                 ┌──────────────────────┬──────────────────────┐
   OLED VCC  <── │ (01) 3.3V Power      │ (02) 5V Power        │──> 5V Fan VCC (Red)
   OLED SDA  <── │ (03) GPIO 2 (SDA1)   │ (04) 5V Power        │
   OLED SCL  <── │ (05) GPIO 3 (SCL1)   │ (06) GND             │──> 5V Fan GND (Black)
                 │ (07) GPIO 4          │ (08) GPIO 14 (TXD)   │
   OLED GND  <── │ (09) GND             │ (10) GPIO 15 (RXD)   │
   Button 1  <── │ (11) GPIO 17         │ (12) GPIO 18         │
   Button 2  <── │ (13) GPIO 27         │ (14) GND             │──> Button 1 & 2 GND
   Button 3  <── │ (15) GPIO 22         │ (16) GPIO 23         │
                 │ (17) 3.3V Power      │ (18) GPIO 24         │
                 │ (19) GPIO 10 (MOSI)  │ (20) GND             │──> Button 3 GND
                 └──────────────────────┴──────────────────────┘
```

### 1. 0.96" I2C OLED Display (`SSD1306` @ `0x3C`)
| OLED Pin | Raspberry Pi Physical Pin | GPIO / Function |
| :--- | :--- | :--- |
| **VCC** | **Pin 1** | `3.3V Power` |
| **GND** | **Pin 9** | `GND` |
| **SDA** | **Pin 3** | `GPIO 2 (I2C1 SDA)` |
| **SCL** | **Pin 5** | `GPIO 3 (I2C1 SCL)` |

### 2. Three Physical Push Buttons (Active-Low with Internal Pull-Up)
Each button has 2 terminals: connect one terminal to the **GPIO pin** and the other terminal to any **GND pin** (Pins `14` and `20` sit right across from the button GPIOs, or daisy-chain a common ground wire across all three buttons).

| Button Position | Function | GPIO (BCM) | Physical Pin | Ground Pin |
| :--- | :--- | :--- | :--- | :--- |
| **Button 1 (Left)** | **Back / Cancel (`Zurück / Abbruch`)** | `GPIO 17` | **Pin 11** | **Pin 14** (`GND`) |
| **Button 2 (Middle)** | **Select / Confirm (`Auswahl / OK`)** | `GPIO 27` | **Pin 13** | **Pin 14** (`GND`) |
| **Button 3 (Right)** | **Next / Cycle (`Weiter ▶`)** | `GPIO 22` | **Pin 15** | **Pin 20** (`GND`) |

> **Tip:** Press **Button 1 (Left) + Button 3 (Right)** simultaneously at any time in `IDLE` mode to display the **10-second Diagnostic Menu** on the OLED screen.

### 3. Active 5V Fan Cooler
| Fan Wire | Raspberry Pi Physical Pin | Function |
| :--- | :--- | :--- |
| **Red (+5V)** | **Pin 2** or **Pin 4** | `5V Power` |
| **Black (GND)** | **Pin 6** | `GND` |

### 4. USB Devices
- **USB Port 1:** 13.56 MHz USB HID RFID Reader (`/dev/input/by-id/usb-...-event-kbd`).
- **USB Port 2:** Qibixx MDB-USB Interface (`/dev/ttyACM0`) connected via MDB harness to the SandenVendo G-Drink DM9.

---

## ⚙️ Note on RFID UID Format (USB Reader vs. Smartphone NFC Tools)

Standard 13.56 MHz MIFARE Classic chips store a 4-byte hexadecimal serial number.
- **Smartphone Apps (e.g., NFC Tools):** Display the raw bytes in MSB order (`04:A2:F3:9B`).
- **USB 13.56 MHz Readers (10-digit Decimal Output):** Reverse the 4 bytes (LSB first: `9B F3 A2 04`) and convert the 32-bit integer to a zero-padded 10-digit decimal string (`2616435204`).
- Use the **Live Chip Scanner** in the Web Dashboard (`http://<pi-ip>:8000`) to see the exact 10-digit UID emitted by the USB reader and copy it with one click.

---

## 🚀 Quick Start (Local Development & Simulator on macOS / Linux)

You can run the full stack locally on macOS or Linux without any hardware attached using the built-in virtual simulator:

```bash
git clone https://github.com/pet210702/ffweb-pi-mdb-vending.git
cd ffweb-pi-mdb-vending

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Run automated test suite
pytest -v

# Start the FastAPI server + Virtual G-Drink DM9 Simulator
uvicorn app.main:app --reload --port 8000
```

- **Web Dashboard & Simulator:** `http://localhost:8000`
- **Interactive OpenAPI Docs:** `http://localhost:8000/docs`

---

## 🍓 Raspberry Pi Installation Guide

### Step 1: Copy Project & Run Automated Installer
From your workstation, copy the repository to the Raspberry Pi and run [`deploy/install_pi.sh`](deploy/install_pi.sh):

```bash
scp -r . ffadmin@<pi-ip>:~/getraenkeautomat
ssh ffadmin@<pi-ip> "bash ~/getraenkeautomat/deploy/install_pi.sh"
```

What [`deploy/install_pi.sh`](deploy/install_pi.sh) does automatically:
1. Installs precompiled Debian/Raspberry Pi OS ARM packages (`python3-fastapi`, `python3-uvicorn`, `python3-httpx`, `python3-serial`, `python3-evdev`, `python3-luma.oled`, `python3-gpiod`, `i2c-tools`, `fonts-dejavu-core`) so installation finishes in seconds even on 32-bit ARMv6 hardware.
2. Enables the hardware I2C bus (`/dev/i2c-1`) and adds your user to the `input`, `dialout`, `i2c`, and `gpio` groups.
3. Deploys the application to `/opt/getraenkeautomat` and creates `/opt/getraenkeautomat/.env` from `.env.example`.
4. Installs and enables two `systemd` services:
   - `getraenkeautomat-boot-oled.service`: Renders an immediate boot splash screen on the OLED as soon as `/dev/i2c-1` is ready.
   - `getraenkeautomat.service`: Runs the FastAPI/Uvicorn controller service with automatic restart.

### Step 2: (Optional) Apply Raspberry Pi 1 Fast-Boot & Overclock Tweaks
If running on a **Raspberry Pi 1 Model B+** with active cooling, run [`deploy/optimize_pi.sh`](deploy/optimize_pi.sh) to disable unused background services (`cloud-init`, `bluetooth`, `wpa_supplicant`, `NetworkManager-wait-online`) and enable the official 1000 MHz Turbo profile:

```bash
ssh ffadmin@<pi-ip> "bash /opt/getraenkeautomat/deploy/optimize_pi.sh && sudo reboot"
```

---

## 🔄 Switching from Simulator to Live MDB Hardware & FFWEB

Edit `/opt/getraenkeautomat/.env` on the Raspberry Pi and restart the service (`sudo systemctl restart getraenkeautomat`):

### 1. Enable Qibixx MDB-USB Interface
```ini
MDB_MODE=qibixx
MDB_SERIAL_PORT=/dev/ttyACM0
MDB_BAUDRATE=115200
```
*On the SandenVendo G-Drink DM9 SF01 controller board, ensure the protocol DIP/menu setting is set to **MDB** and Cashless payment is enabled under `PAYMENT SETTING`.*

### 2. Enable Live FFWEB API Integration
```ini
FFWEB_MODE=http
FFWEB_BASE_URL=https://api.ffweb.at
FFWEB_API_KEY=your_station_api_token
FFWEB_MEMBER_LOOKUP_PATH=/api/v1/vending/members/{chip_uid}
FFWEB_PRODUCTS_PATH=/api/v1/vending/products
FFWEB_VEND_PATH=/api/v1/vending/consumptions
```

### 3. Complete `.env` Configuration Reference

| Variable | Default | Description |
| :--- | :--- | :--- |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | HTTP server bind address and port |
| `DB_PATH` | `data/vending.db` | Local SQLite database path (WAL mode) |
| `ADMIN_API_TOKEN` | *(empty)* | Optional `X-Admin-Token` / Bearer token for admin write routes |
| `MDB_MODE` | `simulator` | `simulator` (virtual DM9) or `qibixx` (live serial MDB hardware) |
| `MDB_SERIAL_PORT` | `/dev/ttyACM0` | Serial device path for Qibixx MDB-USB / Pi HAT |
| `RFID_MODE` | `auto` | `auto` (uses Linux `evdev` if present), `evdev`, or `simulator` |
| `RFID_DEVICE_PATH` | *(empty)* | Optional explicit `/dev/input/by-id/...` path (auto-detected if empty) |
| `OLED_MODE` | `auto` | `auto` (`ssd1306` on `/dev/i2c-1`), `ssd1306`, `sh1106`, or `disabled` |
| `OLED_I2C_ADDRESS` | `0x3C` | Hex I2C address of the 0.96" OLED display |
| `OLED_SCREENSAVER_SEC` | `90` | Seconds of idle time before dimming/blanking the OLED |
| `BUTTONS_MODE` | `auto` | `auto` (uses `/dev/gpiochip0` via `libgpiod` v2), `simulator`, or `disabled` |
| `BUTTON_PIN_BACK` | `17` | BCM GPIO pin for Button 1 (Left: Back / Cancel) |
| `BUTTON_PIN_SELECT` | `27` | BCM GPIO pin for Button 2 (Middle: Select / OK) |
| `BUTTON_PIN_NEXT` | `22` | BCM GPIO pin for Button 3 (Right: Next / Cycle) |
| `BILLING_SELECTION_TIMEOUT_SEC` | `12` | Seconds before the OLED billing/event-list chooser times out |
| `SESSION_TIMEOUT_SEC` | `30` | Seconds an authorized MDB credit session stays open on the DM9 |
| `AWAITING_CHIP_TIMEOUT_SEC` | `15` | Seconds a selection-first drink request waits for an RFID chip scan |
| `DISPENSE_TIMEOUT_SEC` | `25` | Max seconds to wait for the DM9 optical elevator sensor confirmation |
| `ALLOW_UNMAPPED_SLOTS` | `false` | Whether unmapped DM9 slots can be vended using the machine's fallback price |
| `FFWEB_MODE` | `mock` | `mock` (local SQLite members/balances) or `http` (live FFWEB REST API) |

---

## 🗂️ Project Structure

```text
.
├── app/
│   ├── api/routes.py          # FastAPI REST endpoints & SSE real-time stream
│   ├── buttons/               # Physical 3-button GPIO driver (libgpiod v2 + combo detection)
│   ├── ffweb/                 # Mock & live HTTP FFWEB client + resilient background sync worker
│   ├── mdb/                   # Qibixx MDB Cashless serial driver & Virtual G-Drink DM9 simulator
│   ├── oled/                  # SSD1306/SH1106 128x64 I2C OLED renderer & early-boot splash
│   ├── rfid/                  # Linux evdev USB HID reader with hot-plug & disconnect detection
│   ├── static/index.html      # Station Admin Dashboard & Virtual Hardware Simulator UI
│   ├── config.py              # Environment configuration loader
│   ├── db.py                  # SQLite WAL persistence (slots, members, event lists, vend log)
│   ├── events.py              # Async pub/sub event bus
│   ├── main.py                # FastAPI application lifespan & service wiring
│   └── state_machine.py       # Core vending state machine (Card-First, Selection-First, Event Lists)
├── deploy/
│   ├── getraenkeautomat-boot-oled.service  # Early-boot OLED splash systemd unit
│   ├── getraenkeautomat.service            # Main controller systemd unit
│   ├── install_pi.sh                       # One-command Raspberry Pi installer
│   └── optimize_pi.sh                      # Raspberry Pi 1 fast-boot & 1000 MHz overclock script
├── tests/                     # Automated pytest suite (vending flows, event lists, hardware edge cases)
├── .env.example               # Example environment configuration
├── pyproject.toml             # Pytest & project metadata
└── requirements.txt           # Python dependencies
```

---

## 📄 License

Released under the [MIT License](LICENSE).
