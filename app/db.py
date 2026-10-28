from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat(timespec="seconds")


def eur_to_cents(value: str | float | int | Decimal) -> int:
    dec = Decimal(str(value).strip().replace(",", "."))
    return int((dec * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def cents_to_eur_str(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    abs_cents = abs(cents)
    return f"{sign}{abs_cents // 100}.{abs_cents % 100:02d}"


def cents_to_eur_float(cents: int) -> float:
    return round(cents / 100.0, 2)


@dataclass(slots=True)
class SlotMapping:
    slot_number: int
    ffweb_product_id: str
    drink_name: str
    price_cents: int
    active: bool
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot_number": self.slot_number,
            "ffweb_product_id": self.ffweb_product_id,
            "drink_name": self.drink_name,
            "price_cents": self.price_cents,
            "price_eur": cents_to_eur_str(self.price_cents),
            "active": self.active,
            "updated_at": self.updated_at,
        }


class VendingDatabase:
    """SQLite persistence for slot mappings, immutable vend audit log, sync queue, and mock FFWEB data."""

    def __init__(self, db_path: str | Path, seed_defaults: bool = True) -> None:
        self.db_path = Path(db_path)
        if str(self.db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
            self._conn.execute("PRAGMA busy_timeout=5000;")
            self._conn.execute("PRAGMA temp_store=MEMORY;")
            self._conn.execute("PRAGMA foreign_keys=ON;")
            self._init_schema()
            if seed_defaults:
                self._seed_defaults_if_empty()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS slot_mappings (
                slot_number INTEGER PRIMARY KEY,
                ffweb_product_id TEXT NOT NULL,
                drink_name TEXT NOT NULL,
                price_cents INTEGER NOT NULL CHECK(price_cents >= 0),
                active INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS vend_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                idempotency_key TEXT UNIQUE NOT NULL,
                chip_uid TEXT NOT NULL,
                member_id TEXT,
                member_name TEXT,
                mdb_slot INTEGER,
                ffweb_product_id TEXT,
                drink_name TEXT,
                price_cents INTEGER NOT NULL DEFAULT 0,
                billing_mode TEXT NOT NULL DEFAULT 'private',
                event_list_id TEXT,
                event_list_name TEXT,
                member_balance_deducted INTEGER NOT NULL DEFAULT 0,
                vend_status TEXT NOT NULL,
                sync_status TEXT NOT NULL,
                sync_attempts INTEGER NOT NULL DEFAULT 0,
                next_retry_at TEXT,
                last_sync_error TEXT,
                created_at TEXT NOT NULL,
                synced_at TEXT
            );

            CREATE TABLE IF NOT EXISTS event_lists (
                list_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                notes TEXT,
                created_at TEXT NOT NULL,
                closed_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_vend_tx_sync_status
                ON vend_transactions(sync_status, next_retry_at);

            CREATE INDEX IF NOT EXISTS idx_vend_tx_created_at
                ON vend_transactions(created_at DESC);

            CREATE INDEX IF NOT EXISTS idx_vend_tx_event_list
                ON vend_transactions(event_list_id, vend_status);

            -- Mock FFWEB tables (used when FFWEB_MODE=mock)
            CREATE TABLE IF NOT EXISTS mock_ffweb_members (
                member_id TEXT PRIMARY KEY,
                chip_uid TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                balance_cents INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS mock_ffweb_products (
                product_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                price_cents INTEGER NOT NULL CHECK(price_cents >= 0)
            );

            CREATE TABLE IF NOT EXISTS mock_ffweb_bookings (
                idempotency_key TEXT PRIMARY KEY,
                member_id TEXT NOT NULL,
                product_id TEXT NOT NULL,
                price_cents INTEGER NOT NULL,
                billing_mode TEXT NOT NULL DEFAULT 'private',
                event_list_id TEXT,
                booked_at TEXT NOT NULL
            );
            """
        )

        # Non-destructive migrations for existing SQLite databases
        for col, col_def in (
            ("billing_mode", "TEXT NOT NULL DEFAULT 'private'"),
            ("event_list_id", "TEXT"),
            ("event_list_name", "TEXT"),
            ("member_balance_deducted", "INTEGER NOT NULL DEFAULT 0"),
        ):
            try:
                self._conn.execute(f"ALTER TABLE vend_transactions ADD COLUMN {col} {col_def}")
            except sqlite3.OperationalError:
                pass

        for col, col_def in (
            ("billing_mode", "TEXT NOT NULL DEFAULT 'private'"),
            ("event_list_id", "TEXT"),
        ):
            try:
                self._conn.execute(f"ALTER TABLE mock_ffweb_bookings ADD COLUMN {col} {col_def}")
            except sqlite3.OperationalError:
                pass

        self._conn.commit()

    def _seed_defaults_if_empty(self) -> None:
        now = utc_now_iso()
        slot_count = self._conn.execute("SELECT COUNT(*) FROM slot_mappings").fetchone()[0]
        if slot_count == 0:
            default_slots = [
                (11, "FF-COLA-05", "Coca-Cola 0,5L", 150),
                (12, "FF-ALMD-05", "Almdudler 0,5L", 150),
                (13, "FF-ROEM-PR", "Römerquelle prickelnd 0,5L", 100),
                (14, "FF-ROEM-ST", "Römerquelle still 0,5L", 100),
                (15, "FF-OBI-05", "Obi G'spritzt 0,5L", 150),
                (16, "FF-EISTEE-05", "Eistee Pfirsich 0,5L", 150),
            ]
            self._conn.executemany(
                """
                INSERT INTO slot_mappings (slot_number, ffweb_product_id, drink_name, price_cents, active, updated_at)
                VALUES (?, ?, ?, ?, 1, ?)
                """,
                [(s, pid, name, price, now) for s, pid, name, price in default_slots],
            )
            self._conn.executemany(
                """
                INSERT OR IGNORE INTO mock_ffweb_products (product_id, name, price_cents)
                VALUES (?, ?, ?)
                """,
                [(pid, name, price) for _, pid, name, price in default_slots],
            )

        member_count = self._conn.execute("SELECT COUNT(*) FROM mock_ffweb_members").fetchone()[0]
        if member_count == 0:
            default_members = [
                ("FF-101", "0001234567", "Kdt. Florian Huber", 2500, 1),
                ("FF-102", "0009876543", "LM Markus Gruber", 1250, 1),
                ("FF-103", "0000001111", "FM Lukas Steiner (Wenig Guthaben)", 100, 1),
            ]
            self._conn.executemany(
                """
                INSERT INTO mock_ffweb_members (member_id, chip_uid, name, balance_cents, active, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [(mid, uid, name, bal, act, now) for mid, uid, name, bal, act in default_members],
            )

        list_count = self._conn.execute("SELECT COUNT(*) FROM event_lists").fetchone()[0]
        if list_count == 0:
            default_lists = [
                ("LIST-NACHBESPR", "Nachbesprechung", 0, "Nachbesprechung nach Einsatz oder Übung", now, now),
                ("LIST-UEBUNG", "Monatsübung", 0, "Gemeinsame Übung (inaktiv)", now, now),
            ]
            self._conn.executemany(
                """
                INSERT INTO event_lists (list_id, name, active, notes, created_at, closed_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                default_lists,
            )
        self._conn.commit()

    # -----------------------------
    # Slot Mappings
    # -----------------------------
    def list_slots(self, active_only: bool = False) -> list[SlotMapping]:
        with self._lock:
            sql = "SELECT * FROM slot_mappings"
            if active_only:
                sql += " WHERE active = 1"
            sql += " ORDER BY slot_number ASC"
            rows = self._conn.execute(sql).fetchall()
            return [self._row_to_slot(r) for r in rows]

    def get_slot(self, slot_number: int) -> SlotMapping | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM slot_mappings WHERE slot_number = ?", (slot_number,)
            ).fetchone()
            return self._row_to_slot(row) if row else None

    def upsert_slot(
        self,
        slot_number: int,
        ffweb_product_id: str,
        drink_name: str,
        price_cents: int,
        active: bool = True,
    ) -> SlotMapping:
        now = utc_now_iso()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO slot_mappings (slot_number, ffweb_product_id, drink_name, price_cents, active, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(slot_number) DO UPDATE SET
                    ffweb_product_id = excluded.ffweb_product_id,
                    drink_name = excluded.drink_name,
                    price_cents = excluded.price_cents,
                    active = excluded.active,
                    updated_at = excluded.updated_at
                """,
                (slot_number, ffweb_product_id, drink_name, price_cents, 1 if active else 0, now),
            )
            self._conn.execute(
                """
                INSERT INTO mock_ffweb_products (product_id, name, price_cents)
                VALUES (?, ?, ?)
                ON CONFLICT(product_id) DO UPDATE SET
                    name = excluded.name,
                    price_cents = excluded.price_cents
                """,
                (ffweb_product_id, drink_name, price_cents),
            )
            self._conn.commit()
            slot = self.get_slot(slot_number)
            assert slot is not None
            return slot

    def delete_slot(self, slot_number: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM slot_mappings WHERE slot_number = ?", (slot_number,))
            self._conn.commit()
            return cur.rowcount > 0

    @staticmethod
    def _row_to_slot(row: sqlite3.Row) -> SlotMapping:
        return SlotMapping(
            slot_number=int(row["slot_number"]),
            ffweb_product_id=str(row["ffweb_product_id"]),
            drink_name=str(row["drink_name"]),
            price_cents=int(row["price_cents"]),
            active=bool(row["active"]),
            updated_at=str(row["updated_at"]),
        )

    # -----------------------------
    # Event Lists (Verrechnungslisten & Anlässe)
    # -----------------------------
    def list_event_lists(self, active_only: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            sql = """
                SELECT el.*,
                       COUNT(vt.id) as consumption_count,
                       COALESCE(SUM(vt.price_cents), 0) as total_cost_cents
                FROM event_lists el
                LEFT JOIN vend_transactions vt
                  ON vt.event_list_id = el.list_id AND vt.vend_status = 'DISPENSED'
            """
            params: list[Any] = []
            if active_only:
                sql += " WHERE el.active = 1"
            sql += " GROUP BY el.list_id ORDER BY el.active DESC, el.created_at DESC"
            rows = self._conn.execute(sql, params).fetchall()
            return [self._row_to_event_list(r) for r in rows]

    def get_event_list(self, list_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT el.*,
                       COUNT(vt.id) as consumption_count,
                       COALESCE(SUM(vt.price_cents), 0) as total_cost_cents
                FROM event_lists el
                LEFT JOIN vend_transactions vt
                  ON vt.event_list_id = el.list_id AND vt.vend_status = 'DISPENSED'
                WHERE el.list_id = ?
                GROUP BY el.list_id
                """,
                (list_id,),
            ).fetchone()
            return self._row_to_event_list(row) if row else None

    def upsert_event_list(
        self,
        list_id: str,
        name: str,
        active: bool = True,
        notes: str = "",
    ) -> dict[str, Any]:
        now = utc_now_iso()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO event_lists (list_id, name, active, notes, created_at, closed_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(list_id) DO UPDATE SET
                    name = excluded.name,
                    active = excluded.active,
                    notes = excluded.notes,
                    closed_at = CASE
                        WHEN excluded.active = 0 AND closed_at IS NULL THEN excluded.closed_at
                        WHEN excluded.active = 1 THEN NULL
                        ELSE closed_at
                    END
                """,
                (
                    list_id.strip(),
                    name.strip(),
                    1 if active else 0,
                    notes.strip(),
                    now,
                    None if active else now,
                ),
            )
            self._conn.commit()
            el = self.get_event_list(list_id.strip())
            assert el is not None
            return el

    def toggle_event_list(self, list_id: str, active: bool | None = None) -> dict[str, Any] | None:
        now = utc_now_iso()
        with self._lock:
            current = self._conn.execute(
                "SELECT active FROM event_lists WHERE list_id = ?", (list_id,)
            ).fetchone()
            if not current:
                return None
            new_active = (not bool(current["active"])) if active is None else bool(active)
            closed_at = None if new_active else now
            self._conn.execute(
                "UPDATE event_lists SET active = ?, closed_at = ? WHERE list_id = ?",
                (1 if new_active else 0, closed_at, list_id),
            )
            self._conn.commit()
            return self.get_event_list(list_id)

    def delete_event_list(self, list_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM event_lists WHERE list_id = ?", (list_id,))
            self._conn.commit()
            return cur.rowcount > 0

    @staticmethod
    def _row_to_event_list(row: sqlite3.Row) -> dict[str, Any]:
        cost_cents = int(row["total_cost_cents"]) if "total_cost_cents" in row.keys() else 0
        return {
            "list_id": str(row["list_id"]),
            "name": str(row["name"]),
            "active": bool(row["active"]),
            "notes": str(row["notes"] or ""),
            "created_at": str(row["created_at"]),
            "closed_at": row["closed_at"],
            "consumption_count": int(row["consumption_count"]) if "consumption_count" in row.keys() else 0,
            "total_cost_cents": cost_cents,
            "total_cost_eur": cents_to_eur_str(cost_cents),
        }

    # -----------------------------
    # Transactions & Sync Queue
    # -----------------------------
    def create_transaction(
        self,
        *,
        idempotency_key: str,
        chip_uid: str,
        member_id: str | None,
        member_name: str | None,
        mdb_slot: int | None,
        ffweb_product_id: str | None,
        drink_name: str | None,
        price_cents: int,
        vend_status: str,
        sync_status: str,
        billing_mode: str = "private",
        event_list_id: str | None = None,
        event_list_name: str | None = None,
        member_balance_deducted: int | None = None,
    ) -> dict[str, Any]:
        now = utc_now_iso()
        actual_deduction = (
            member_balance_deducted
            if member_balance_deducted is not None
            else (price_cents if billing_mode == "private" and vend_status == "DISPENSED" else 0)
        )
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO vend_transactions (
                    idempotency_key, chip_uid, member_id, member_name,
                    mdb_slot, ffweb_product_id, drink_name, price_cents,
                    billing_mode, event_list_id, event_list_name, member_balance_deducted,
                    vend_status, sync_status, sync_attempts, next_retry_at, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    idempotency_key,
                    chip_uid,
                    member_id,
                    member_name,
                    mdb_slot,
                    ffweb_product_id,
                    drink_name,
                    price_cents,
                    billing_mode,
                    event_list_id,
                    event_list_name,
                    actual_deduction,
                    vend_status,
                    sync_status,
                    now if sync_status == "PENDING_SYNC" else None,
                    now,
                ),
            )
            self._conn.commit()
            tx = self.get_transaction(int(cur.lastrowid))
            assert tx is not None
            return tx

    def get_transaction(self, tx_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM vend_transactions WHERE id = ?", (tx_id,)
            ).fetchone()
            return self._row_to_tx_dict(row) if row else None

    def list_transactions(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM vend_transactions ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [self._row_to_tx_dict(r) for r in rows]

    def count_pending_sync(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM vend_transactions WHERE sync_status = 'PENDING_SYNC'"
            ).fetchone()
            return int(row[0])

    def get_due_pending_sync_transactions(self, limit: int = 25) -> list[dict[str, Any]]:
        now = utc_now_iso()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM vend_transactions
                WHERE sync_status = 'PENDING_SYNC'
                  AND (next_retry_at IS NULL OR next_retry_at <= ?)
                ORDER BY id ASC
                LIMIT ?
                """,
                (now, limit),
            ).fetchall()
            return [self._row_to_tx_dict(r) for r in rows]

    def reset_pending_retry_timers(self) -> int:
        now = utc_now_iso()
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE vend_transactions
                SET next_retry_at = ?
                WHERE sync_status = 'PENDING_SYNC'
                """,
                (now,),
            )
            self._conn.commit()
            return cur.rowcount

    def mark_transaction_synced(self, tx_id: int) -> None:
        now = utc_now_iso()
        with self._lock:
            self._conn.execute(
                """
                UPDATE vend_transactions
                SET sync_status = 'SYNCED',
                    sync_attempts = sync_attempts + 1,
                    next_retry_at = NULL,
                    last_sync_error = NULL,
                    synced_at = ?
                WHERE id = ?
                """,
                (now, tx_id),
            )
            self._conn.commit()

    def mark_transaction_sync_error(
        self, tx_id: int, error_message: str, permanent: bool = False
    ) -> None:
        with self._lock:
            row = self._conn.execute(
                "SELECT sync_attempts FROM vend_transactions WHERE id = ?", (tx_id,)
            ).fetchone()
            attempts = (int(row["sync_attempts"]) if row else 0) + 1
            if permanent:
                self._conn.execute(
                    """
                    UPDATE vend_transactions
                    SET sync_status = 'FAILED_PERMANENT',
                        sync_attempts = ?,
                        next_retry_at = NULL,
                        last_sync_error = ?
                    WHERE id = ?
                    """,
                    (attempts, error_message[:500], tx_id),
                )
            else:
                backoff_sec = min(300, 5 * (3 ** min(attempts - 1, 4)))
                next_retry = (utc_now() + timedelta(seconds=backoff_sec)).isoformat(
                    timespec="seconds"
                )
                self._conn.execute(
                    """
                    UPDATE vend_transactions
                    SET sync_status = 'PENDING_SYNC',
                        sync_attempts = ?,
                        next_retry_at = ?,
                        last_sync_error = ?
                    WHERE id = ?
                    """,
                    (attempts, next_retry, error_message[:500], tx_id),
                )
            self._conn.commit()

    @staticmethod
    def _row_to_tx_dict(row: sqlite3.Row) -> dict[str, Any]:
        price_cents = int(row["price_cents"])
        row_keys = row.keys()
        billing_mode = str(row["billing_mode"]) if "billing_mode" in row_keys else "private"
        event_list_id = row["event_list_id"] if "event_list_id" in row_keys else None
        event_list_name = row["event_list_name"] if "event_list_name" in row_keys else None
        deducted = (
            int(row["member_balance_deducted"])
            if "member_balance_deducted" in row_keys and row["member_balance_deducted"] is not None
            else (price_cents if billing_mode == "private" and str(row["vend_status"]) == "DISPENSED" else 0)
        )
        return {
            "id": int(row["id"]),
            "idempotency_key": str(row["idempotency_key"]),
            "chip_uid": str(row["chip_uid"]),
            "member_id": row["member_id"],
            "member_name": row["member_name"],
            "mdb_slot": row["mdb_slot"],
            "ffweb_product_id": row["ffweb_product_id"],
            "drink_name": row["drink_name"],
            "price_cents": price_cents,
            "price_eur": cents_to_eur_str(price_cents),
            "billing_mode": billing_mode,
            "event_list_id": event_list_id,
            "event_list_name": event_list_name,
            "member_balance_deducted": deducted,
            "member_balance_deducted_eur": cents_to_eur_str(deducted),
            "vend_status": str(row["vend_status"]),
            "sync_status": str(row["sync_status"]),
            "sync_attempts": int(row["sync_attempts"]),
            "next_retry_at": row["next_retry_at"],
            "last_sync_error": row["last_sync_error"],
            "created_at": str(row["created_at"]),
            "synced_at": row["synced_at"],
        }

    # -----------------------------
    # Mock FFWEB Store Operations
    # -----------------------------
    def list_mock_members(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM mock_ffweb_members ORDER BY member_id ASC"
            ).fetchall()
            return [
                {
                    "member_id": str(r["member_id"]),
                    "chip_uid": str(r["chip_uid"]),
                    "name": str(r["name"]),
                    "balance_cents": int(r["balance_cents"]),
                    "balance_eur": cents_to_eur_str(int(r["balance_cents"])),
                    "active": bool(r["active"]),
                    "updated_at": str(r["updated_at"]),
                }
                for r in rows
            ]

    def get_mock_member_by_chip(self, chip_uid: str) -> dict[str, Any] | None:
        normalized = chip_uid.strip().upper()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM mock_ffweb_members WHERE chip_uid = ? OR UPPER(chip_uid) = ? LIMIT 1",
                (normalized, normalized),
            ).fetchone()
            if not row:
                return None
            return {
                "member_id": str(row["member_id"]),
                "chip_uid": str(row["chip_uid"]),
                "name": str(row["name"]),
                "balance_cents": int(row["balance_cents"]),
                "balance_eur": cents_to_eur_str(int(row["balance_cents"])),
                "active": bool(row["active"]),
                "updated_at": str(row["updated_at"]),
            }

    def upsert_mock_member(
        self,
        *,
        member_id: str,
        chip_uid: str,
        name: str,
        balance_cents: int,
        active: bool = True,
    ) -> dict[str, Any]:
        now = utc_now_iso()
        normalized_uid = chip_uid.strip().upper()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO mock_ffweb_members (member_id, chip_uid, name, balance_cents, active, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(member_id) DO UPDATE SET
                    chip_uid = excluded.chip_uid,
                    name = excluded.name,
                    balance_cents = excluded.balance_cents,
                    active = excluded.active,
                    updated_at = excluded.updated_at
                """,
                (member_id.strip(), normalized_uid, name.strip(), balance_cents, 1 if active else 0, now),
            )
            self._conn.commit()
            member = self.get_mock_member_by_chip(normalized_uid)
            assert member is not None
            return member

    def adjust_mock_member_balance(self, member_id: str, delta_cents: int) -> dict[str, Any] | None:
        now = utc_now_iso()
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE mock_ffweb_members
                SET balance_cents = balance_cents + ?, updated_at = ?
                WHERE member_id = ?
                """,
                (delta_cents, now, member_id),
            )
            self._conn.commit()
            if cur.rowcount == 0:
                return None
            row = self._conn.execute(
                "SELECT * FROM mock_ffweb_members WHERE member_id = ?", (member_id,)
            ).fetchone()
            if not row:
                return None
            return self.get_mock_member_by_chip(str(row["chip_uid"]))

    def list_mock_products(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM mock_ffweb_products ORDER BY product_id ASC"
            ).fetchall()
            return [
                {
                    "product_id": str(r["product_id"]),
                    "name": str(r["name"]),
                    "price_cents": int(r["price_cents"]),
                    "price_eur": cents_to_eur_str(int(r["price_cents"])),
                }
                for r in rows
            ]

    def book_mock_consumption(
        self,
        *,
        idempotency_key: str,
        member_id: str,
        product_id: str,
        price_cents: int,
        billing_mode: str = "private",
        event_list_id: str | None = None,
    ) -> int:
        """
        Record consumption once per idempotency_key.
        If billing_mode == 'event_list', station covers the cost: member balance is NOT deducted!
        If billing_mode == 'private', member balance is deducted.
        Returns the member's resulting balance_cents.
        """
        now = utc_now_iso()
        with self._lock:
            existing = self._conn.execute(
                "SELECT 1 FROM mock_ffweb_bookings WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            row = self._conn.execute(
                "SELECT balance_cents FROM mock_ffweb_members WHERE member_id = ?",
                (member_id,),
            ).fetchone()
            if not row:
                raise KeyError(f"Unknown member_id: {member_id}")
            if existing:
                return int(row["balance_cents"])

            current_balance = int(row["balance_cents"])
            # If billed to event list, member pays 0.00 € (balance remains intact!)
            if billing_mode == "event_list":
                new_balance = current_balance
            else:
                new_balance = current_balance - price_cents
                self._conn.execute(
                    "UPDATE mock_ffweb_members SET balance_cents = ?, updated_at = ? WHERE member_id = ?",
                    (new_balance, now, member_id),
                )

            self._conn.execute(
                """
                INSERT INTO mock_ffweb_bookings (idempotency_key, member_id, product_id, price_cents, billing_mode, event_list_id, booked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (idempotency_key, member_id, product_id, price_cents, billing_mode, event_list_id, now),
            )
            self._conn.commit()
            return new_balance
