from __future__ import annotations

from enum import Enum


class ButtonAction(str, Enum):
    BACK = "back"           # Left button (GPIO 17 / Pin 11): Zurück / Abbrechen
    SELECT = "select"       # Middle button (GPIO 27 / Pin 13): Auswahl / OK
    NEXT = "next"           # Right button (GPIO 22 / Pin 15): Weiter / Weiterblättern
    COMBO_1_3 = "combo_1_3" # Buttons 1 + 3 pressed together: Debug / Diagnose-Menü (10s)
