from __future__ import annotations

import abc
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.db import cents_to_eur_str, eur_to_cents


@dataclass(slots=True)
class MdbEvent:
    raw_line: str


@dataclass(slots=True)
class MdbStatusEvent(MdbEvent):
    status: str  # ENABLED, IDLE, DISABLED, INACTIVE, INIT


@dataclass(slots=True)
class MdbVendRequestEvent(MdbEvent):
    price_cents: int
    slot_number: int | None


@dataclass(slots=True)
class MdbVendSuccessEvent(MdbEvent):
    pass


@dataclass(slots=True)
class MdbVendErrorEvent(MdbEvent):
    reason: str


MdbEventCallback = Callable[[MdbEvent], Awaitable[None]]


def parse_qibixx_line(raw_line: str) -> MdbEvent | None:
    """Parse an asynchronous line emitted by Qibixx Cashless Peripheral ('c,...')."""
    line = raw_line.strip()
    if not line:
        return None

    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 2 or parts[0] != "c":
        return None

    category = parts[1].upper()
    if category == "STATUS" and len(parts) >= 3:
        sub = parts[2].upper()
        if sub == "VEND":
            price_cents = eur_to_cents(parts[3]) if len(parts) >= 4 and parts[3] else 0
            slot_num: int | None = None
            if len(parts) >= 5 and parts[4]:
                try:
                    slot_num = int(parts[4])
                except ValueError:
                    slot_num = None
            return MdbVendRequestEvent(
                raw_line=line, price_cents=price_cents, slot_number=slot_num
            )
        return MdbStatusEvent(raw_line=line, status=sub)

    if category == "VEND" and len(parts) >= 3 and parts[2].upper() == "SUCCESS":
        return MdbVendSuccessEvent(raw_line=line)

    if category == "ERR":
        reason = ",".join(parts[2:]) if len(parts) >= 3 else "UNKNOWN_MDB_ERR"
        return MdbVendErrorEvent(raw_line=line, reason=reason)

    return None


class MdbDriver(abc.ABC):
    """Abstract interface for MDB Cashless Peripheral hardware or simulator."""

    @abc.abstractmethod
    async def start(self, on_event: MdbEventCallback) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    async def stop(self) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    async def send_command(self, command: str) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    def status_snapshot(self) -> dict[str, Any]:
        raise NotImplementedError

    async def enable_cashless(self) -> None:
        await self.send_command("C,1")

    async def disable_cashless(self) -> None:
        await self.send_command("C,0")

    async def start_session(self, balance_cents: int) -> None:
        await self.send_command(f"C,START,{cents_to_eur_str(balance_cents)}")

    async def approve_vend(self, price_cents: int) -> None:
        await self.send_command(f"C,VEND,{cents_to_eur_str(price_cents)}")

    async def stop_or_deny(self) -> None:
        await self.send_command("C,STOP")
