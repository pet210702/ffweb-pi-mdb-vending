from __future__ import annotations

from typing import Any

from app.db import VendingDatabase
from app.ffweb.base import (
    FFWebClient,
    FFWebPermanentError,
    FFWebProduct,
    FFWebTemporaryError,
    MemberInfo,
    VendBookingRequest,
)


class MockFFWebClient(FFWebClient):
    """Local SQLite-backed mock implementation of FFWEB for station hardware testing."""

    def __init__(self, db: VendingDatabase) -> None:
        self._db = db
        self.simulate_outage: bool = False

    async def get_member_by_chip(self, chip_uid: str) -> MemberInfo | None:
        if self.simulate_outage:
            raise FFWebTemporaryError("Simulated FFWEB network outage")
        row = self._db.get_mock_member_by_chip(chip_uid)
        if not row:
            return None
        return MemberInfo(
            member_id=row["member_id"],
            chip_uid=row["chip_uid"],
            name=row["name"],
            balance_cents=row["balance_cents"],
            active=row["active"],
        )

    async def list_products(self) -> list[FFWebProduct]:
        if self.simulate_outage:
            raise FFWebTemporaryError("Simulated FFWEB network outage")
        return [
            FFWebProduct(
                product_id=p["product_id"],
                name=p["name"],
                price_cents=p["price_cents"],
            )
            for p in self._db.list_mock_products()
        ]

    async def record_vend(self, booking: VendBookingRequest) -> int | None:
        if self.simulate_outage:
            raise FFWebTemporaryError("Simulated FFWEB network outage during vend booking")
        try:
            return self._db.book_mock_consumption(
                idempotency_key=booking.idempotency_key,
                member_id=booking.member_id,
                product_id=booking.product_id,
                price_cents=booking.price_cents,
                billing_mode=booking.billing_mode,
                event_list_id=booking.event_list_id,
            )
        except KeyError as exc:
            raise FFWebPermanentError(str(exc)) from exc

    async def close(self) -> None:
        return None

    def status_snapshot(self) -> dict[str, Any]:
        return {
            "mode": "mock",
            "simulate_outage": self.simulate_outage,
            "healthy": not self.simulate_outage,
        }
