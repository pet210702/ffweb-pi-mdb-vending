from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any

from app.db import cents_to_eur_str


class FFWebError(Exception):
    """Base error for FFWEB communication."""


class FFWebTemporaryError(FFWebError):
    """Transient network/timeout/5xx error — safe to retry with idempotency_key."""


class FFWebPermanentError(FFWebError):
    """Permanent 4xx validation error — should be flagged rather than retried forever."""


@dataclass(slots=True)
class MemberInfo:
    member_id: str
    chip_uid: str
    name: str
    balance_cents: int
    active: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "member_id": self.member_id,
            "chip_uid": self.chip_uid,
            "name": self.name,
            "balance_cents": self.balance_cents,
            "balance_eur": cents_to_eur_str(self.balance_cents),
            "active": self.active,
        }


@dataclass(slots=True)
class FFWebProduct:
    product_id: str
    name: str
    price_cents: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "product_id": self.product_id,
            "name": self.name,
            "price_cents": self.price_cents,
            "price_eur": cents_to_eur_str(self.price_cents),
        }


@dataclass(slots=True)
class VendBookingRequest:
    idempotency_key: str
    member_id: str
    chip_uid: str
    product_id: str
    drink_name: str
    mdb_slot: int | None
    price_cents: int
    created_at: str
    billing_mode: str = "private"
    event_list_id: str | None = None


class FFWebClient(abc.ABC):
    """Adapter interface for FFWEB (ffweb.at)."""

    @abc.abstractmethod
    async def get_member_by_chip(self, chip_uid: str) -> MemberInfo | None:
        raise NotImplementedError

    @abc.abstractmethod
    async def list_products(self) -> list[FFWebProduct]:
        raise NotImplementedError

    @abc.abstractmethod
    async def record_vend(self, booking: VendBookingRequest) -> int | None:
        """Book consumption & deduct balance in FFWEB; return updated balance_cents if known."""
        raise NotImplementedError

    @abc.abstractmethod
    async def close(self) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    def status_snapshot(self) -> dict[str, Any]:
        raise NotImplementedError
