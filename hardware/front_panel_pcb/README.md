# FF Getränkeautomat – Frontplatine (0,96"-I2C-OLED + 3× 6-mm-Taster)

Eigenständige 2-lagige Frontplatine (`60,0 mm × 56,0 mm`), entworfen in **KiCad 9.0** für die Raspberry-Pi-Steuerung des **FF Getränkeautomaten**.

- **Saubere Vorderseite (`F.Cu`):** Auf der für Benutzer sichtbaren Seite sitzen nur das **0,96"-I2C-OLED (`128×64`, `VCC – GND – SCL – SDA`)**, direkt in `J3` eingelötet (abgestützt mit `2,5 mm` M2-Abstandshaltern auf `H5`–`H8`), und die **3× `6×6 mm` THT-Taster (`Zurück`, `OK`, `Weiter`)**.
- **Verdeckte Verkabelung auf der Rückseite (`B.Cu`):** Der **7-polige JST-XH-Stecker (`2,50 mm`, `J1`)**, die **7-polige `2,54 mm` Stiftleiste (`J2`)** und der **`100 nF` Abblockkondensator (`C1`)** sind auf der **Rückseite (`B.Cu`)** bestückt. Dadurch laufen alle Kabel hinter der Platine in den Automaten – von vorne ist kein Kabel sichtbar.

---

## 📐 Technische Daten

| Parameter | Wert |
| :--- | :--- |
| **Abmessungen** | `60,0 mm × 56,0 mm` (Ecken mit `R3,0 mm` abgerundet) |
| **Lagen** | 2 Lagen FR-4 (`F.Cu` & `B.Cu`), vollflächige `GND`-Kupferflächen auf beiden Lagen |
| **Dicke / Kupfer** | `1,6 mm` Standard-FR-4, `1 oz` (`35 µm`) Kupfer |
| **Befestigungslöcher (Ecken)** | 4× `M3` (`Ø 3,2 mm`, nicht durchkontaktiert, `H1`–`H4`) im Raster `52,0 mm × 48,0 mm` |
| **Löcher für OLED-Abstandshalter** | 4× `M2` (`Ø 2,2 mm`, nicht durchkontaktiert, `H5`–`H8`) im Raster `23,0 mm × 23,5 mm` (`2,5 mm` Abstandshalter) |
| **Tasterabstand** | `16,0 mm` Mittenabstand horizontal (`X = 14,0 / 30,0 / 46,0 mm` vom linken Rand) |
| **Leiterbahnen** | `0,50 mm` Versorgung (`+3V3`), `0,35 mm` Signale (`I2C`, Taster), `0,30 mm` Abstand zu Kupferflächen |
| **ERC / DRC** | **0 ERC-Fehler / 0 DRC-Fehler / 0 offene Verbindungen** (geprüft mit `kicad-cli 9.0.7`) |

---

## 🔌 Belegung der Steckverbinder (`J1` & `J2` auf `B.Cu`) & Anschluss an den Raspberry Pi

`J1` (7-polig JST-XH, Rückseite unten) und `J2` (7-polige `2,54 mm` Stiftleiste, Rückseite seitlich) sitzen beide auf der **Rückseite (`B.Cu`)** und sind parallel verdrahtet – es muss nur einer von beiden bestückt werden.

Alle 7 Signale liegen auf der **linken Reihe (ungerade Pins)** der 40-poligen GPIO-Leiste des Raspberry Pi:

| Pin `J1` / `J2` | Netz | Funktion | Physischer Pin am Pi | BCM-GPIO |
| :---: | :--- | :--- | :---: | :--- |
| **Pin 1** *(eckiges Pad)* | `+3V3` | `3,3 V` Versorgung für das OLED | **Pin 1** | `3,3 V` |
| **Pin 2** | `I2C_SDA` | OLED I2C-Daten | **Pin 3** | `GPIO 2 (SDA1)` |
| **Pin 3** | `I2C_SCL` | OLED I2C-Takt | **Pin 5** | `GPIO 3 (SCL1)` |
| **Pin 4** | `GND` | Gemeinsame Masse (OLED & Taster) | **Pin 9** *(oder 14 / 20)* | `GND` |
| **Pin 5** | `BTN1_BACK` | Taste 1 links (`◀ Zurück / Abbruch`) | **Pin 11** | `GPIO 17` *(Active-Low)* |
| **Pin 6** | `BTN2_OK` | Taste 2 Mitte (`● Auswahl / OK`) | **Pin 13** | `GPIO 27` *(Active-Low)* |
| **Pin 7** | `BTN3_NEXT` | Taste 3 rechts (`Weiter ▶`) | **Pin 15** | `GPIO 22` *(Active-Low)* |

### 4-poliger OLED-Anschluss zum Direkteinlöten (`J3`, Vorderseite `F.Cu`)
Die 4 Stiftkontakte des 0,96"-I2C-OLED (`VCC – GND – SCL – SDA`, von links nach rechts mit Blick auf die Vorderseite) von vorne direkt in `J3` stecken und auf der Rückseite verlöten. Die beiden unteren M2-Löcher (`H7` & `H8`) mit **`2,5 mm` (oder `3 mm`) M2-Nylon-Abstandshaltern** bestücken:
- **Pin 1 (eckiges Pad, links):** `VCC` (`+3V3`)
- **Pin 2:** `GND`
- **Pin 3:** `SCL` (`I2C_SCL`)
- **Pin 4 (rechts):** `SDA` (`I2C_SDA`)

---

## 🛒 Stückliste (BOM)

| Ref | Seite | Anz. | Wert / Beschreibung | Gehäuse / Footprint | Hinweise |
| :--- | :---: | :---: | :--- | :--- | :--- |
| **`J3`** | **Vorne (`F.Cu`)** | 1 | `0,96"` I2C-OLED (`128×64`, 4-polig `2,54 mm`) | `PinHeader_1x04_P2.54mm_Vertical` | Die 4 Stifte des OLED (`VCC-GND-SCL-SDA`) direkt in `J3` löten + `2,5 mm` M2-Abstandshalter |
| **`SW1`–`SW3`** | **Vorne (`F.Cu`)** | 3 | `6×6 mm` THT-Taster (`H = 5 mm` oder `7 mm`) | `SW_PUSH_6mm` | Standard-Printtaster `6×6 mm` mit 4 Beinen (passend zur Höhe des OLED) |
| **`C1`** | **Hinten (`B.Cu`)** | 1 | `100 nF` (`0,1 µF`) Keramik-Scheibenkondensator | `C_Disc_D4.7mm_W2.5mm_P5.00mm` | Auf der Rückseite, damit die Vorderseite frei bleibt |
| **`J1`** | **Hinten (`B.Cu`)** | 1 | 7-polige JST-XH-Stiftleiste (`B7B-XH-A`, `2,50 mm`) | `JST_XH_B7B-XH-A_1x07_P2.50mm_Vertical` | Auf der Rückseite für verdeckte Kabelführung |
| **`J2`** | **Hinten (`B.Cu`)** | 1 | `1×7` Stiftleiste (`2,54 mm` Raster) | `PinHeader_1x07_P2.54mm_Vertical` | Auf der Rückseite; optionale Alternative zu `J1` |
| **`H1`–`H4`** | Durchgehend | 4 | `M3` Befestigungslöcher (`Ø 3,2 mm`) | `MountingHole_3.2mm_M3` | Befestigung im Gehäuse (Abstand `52 × 48 mm`) |
| **`H5`–`H8`** | Durchgehend | 4 | `M2` Löcher für Abstandshalter (`Ø 2,2 mm`) | `MountingHole_2.2mm_M2` | Für `2,5 mm` `M2`-Abstandshalter unter dem 0,96"-OLED |

---

## 🏭 Fertigungsdaten

Alle Dateien für die Fertigung liegen in [`production/`](production/):
- **[`production/ff_front_panel_gerbers.zip`](production/ff_front_panel_gerbers.zip)**: Direkt bei **JLCPCB**, **PCBWay** oder **AISLER** hochladen (2 Lagen, `60 × 56 mm`, `1,6 mm` Dicke).
- **[`production/BOM.csv`](production/BOM.csv)** & **[`production/POS.csv`](production/POS.csv)**: Stückliste und Bestückungsdaten (Bauteilpositionen, Seite `F.Cu` / `B.Cu` jeweils korrekt angegeben).
- **[`production/JLCPCB_BOM.csv`](production/JLCPCB_BOM.csv)** & **[`production/JLCPCB_CPL.csv`](production/JLCPCB_CPL.csv)**: Stückliste und Bestückungsdaten im JLCPCB-Format.
- **[`production/ff_front_panel.step`](production/ff_front_panel.step)**: Vollständiges 3D-CAD-Modell (STEP).
- **[`docs/schematic.pdf`](docs/schematic.pdf)**: Schaltplan als druckbares A4-Vektor-PDF.
