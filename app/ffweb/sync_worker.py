from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.db import VendingDatabase
from app.events import EventBus
from app.ffweb.base import (
    FFWebClient,
    FFWebPermanentError,
    FFWebTemporaryError,
    VendBookingRequest,
)

logger = logging.getLogger(__name__)


class FFWebSyncWorker:
    """Background worker that retries PENDING_SYNC vend transactions until FFWEB acknowledges them."""

    def __init__(
        self,
        *,
        db: VendingDatabase,
        ffweb: FFWebClient,
        events: EventBus,
        interval_sec: float = 10.0,
    ) -> None:
        self._db = db
        self._ffweb = ffweb
        self._events = events
        self._interval_sec = interval_sec
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._sync_lock = asyncio.Lock()

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="ffweb-sync-worker")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def sync_single_transaction(self, tx: dict[str, Any]) -> bool:
        """Attempt to sync a single transaction record to FFWEB immediately."""
        async with self._sync_lock:
            return await self._sync_tx_unlocked(tx)

    async def flush_due(self, force_all: bool = False) -> dict[str, int]:
        """Process due PENDING_SYNC transactions; returns summary counts."""
        async with self._sync_lock:
            if force_all:
                self._db.reset_pending_retry_timers()
            due = self._db.get_due_pending_sync_transactions(limit=50)
            synced = 0
            failed = 0
            for tx in due:
                ok = await self._sync_tx_unlocked(tx)
                if ok:
                    synced += 1
                else:
                    failed += 1
            return {
                "processed": len(due),
                "synced": synced,
                "failed": failed,
                "remaining_pending": self._db.count_pending_sync(),
            }

    async def _sync_tx_unlocked(self, tx: dict[str, Any]) -> bool:
        tx_id = int(tx["id"])
        booking = VendBookingRequest(
            idempotency_key=tx["idempotency_key"],
            member_id=str(tx["member_id"] or ""),
            chip_uid=tx["chip_uid"],
            product_id=str(tx["ffweb_product_id"] or ""),
            drink_name=str(tx["drink_name"] or ""),
            mdb_slot=tx["mdb_slot"],
            price_cents=int(tx["price_cents"]),
            created_at=tx["created_at"],
            billing_mode=str(tx.get("billing_mode") or "private"),
            event_list_id=tx.get("event_list_id"),
        )
        try:
            new_balance = await self._ffweb.record_vend(booking)
            self._db.mark_transaction_synced(tx_id)
            self._events.publish(
                "tx_synced",
                {
                    "tx_id": tx_id,
                    "member_name": tx["member_name"],
                    "drink_name": tx["drink_name"],
                    "price_eur": tx["price_eur"],
                    "new_balance_cents": new_balance,
                    "pending_sync_count": self._db.count_pending_sync(),
                },
            )
            logger.info("Synced transaction #%d (%s) to FFWEB", tx_id, tx["idempotency_key"])
            return True
        except FFWebTemporaryError as exc:
            self._db.mark_transaction_sync_error(tx_id, str(exc), permanent=False)
            self._events.publish(
                "tx_sync_deferred",
                {
                    "tx_id": tx_id,
                    "error": str(exc),
                    "pending_sync_count": self._db.count_pending_sync(),
                },
            )
            logger.warning("Deferred FFWEB sync for tx #%d: %s", tx_id, exc)
            return False
        except FFWebPermanentError as exc:
            self._db.mark_transaction_sync_error(tx_id, str(exc), permanent=True)
            self._events.publish(
                "tx_sync_failed_permanent",
                {
                    "tx_id": tx_id,
                    "error": str(exc),
                    "pending_sync_count": self._db.count_pending_sync(),
                },
            )
            logger.error("Permanent FFWEB sync error for tx #%d: %s", tx_id, exc)
            return False

    async def _loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._interval_sec)
                if self._db.count_pending_sync() > 0:
                    await self.flush_due(force_all=False)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Unexpected error in FFWebSyncWorker: %s", exc)
