from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from app.db import cents_to_eur_float, eur_to_cents
from app.ffweb.base import (
    FFWebClient,
    FFWebPermanentError,
    FFWebProduct,
    FFWebTemporaryError,
    MemberInfo,
    VendBookingRequest,
)


class HttpFFWebClient(FFWebClient):
    """Production REST client for FFWEB (https://www.ffweb.at)."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_sec: float = 4.0,
        member_lookup_path: str = "/api/v1/vending/members/{chip_uid}",
        products_path: str = "/api/v1/vending/products",
        vend_path: str = "/api/v1/vending/consumptions",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.member_lookup_path = member_lookup_path
        self.products_path = products_path
        self.vend_path = vend_path
        headers: dict[str, str] = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
            headers["X-API-Key"] = api_key
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=httpx.Timeout(timeout_sec),
        )
        self._last_error: str | None = None

    async def close(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _extract_cents(data: dict[str, Any], cents_key: str, eur_key: str) -> int:
        if cents_key in data and data[cents_key] is not None:
            return int(data[cents_key])
        if eur_key in data and data[eur_key] is not None:
            return eur_to_cents(data[eur_key])
        return 0

    async def get_member_by_chip(self, chip_uid: str) -> MemberInfo | None:
        path = self.member_lookup_path.format(chip_uid=quote(chip_uid.strip(), safe=""))
        try:
            resp = await self._client.get(path)
        except httpx.HTTPError as exc:
            self._last_error = str(exc)
            raise FFWebTemporaryError(f"FFWEB unreachable: {exc}") from exc

        if resp.status_code == 404:
            self._last_error = None
            return None
        if resp.status_code >= 500 or resp.status_code == 429:
            self._last_error = f"HTTP {resp.status_code}"
            raise FFWebTemporaryError(f"FFWEB server error HTTP {resp.status_code}")
        if resp.status_code >= 400:
            self._last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
            raise FFWebPermanentError(self._last_error)

        self._last_error = None
        data = resp.json()
        balance_cents = self._extract_cents(data, "balance_cents", "balance")
        member_id = str(data.get("member_id") or data.get("id") or chip_uid)
        name = str(
            data.get("name")
            or data.get("full_name")
            or f"{data.get('first_name', '')} {data.get('last_name', '')}".strip()
            or member_id
        )
        active = bool(data.get("active", True))
        return MemberInfo(
            member_id=member_id,
            chip_uid=str(data.get("chip_uid") or chip_uid),
            name=name,
            balance_cents=balance_cents,
            active=active,
        )

    async def list_products(self) -> list[FFWebProduct]:
        try:
            resp = await self._client.get(self.products_path)
        except httpx.HTTPError as exc:
            self._last_error = str(exc)
            raise FFWebTemporaryError(f"FFWEB unreachable: {exc}") from exc

        if resp.status_code >= 500 or resp.status_code == 429:
            raise FFWebTemporaryError(f"FFWEB server error HTTP {resp.status_code}")
        resp.raise_for_status()

        items = resp.json()
        if isinstance(items, dict):
            items = items.get("items") or items.get("products") or []
        results: list[FFWebProduct] = []
        for item in items:
            price_cents = self._extract_cents(item, "price_cents", "price")
            results.append(
                FFWebProduct(
                    product_id=str(item.get("product_id") or item.get("id")),
                    name=str(item.get("name") or ""),
                    price_cents=price_cents,
                )
            )
        return results

    async def record_vend(self, booking: VendBookingRequest) -> int | None:
        payload = {
            "idempotency_key": booking.idempotency_key,
            "member_id": booking.member_id,
            "chip_uid": booking.chip_uid,
            "product_id": booking.product_id,
            "drink_name": booking.drink_name,
            "mdb_slot": booking.mdb_slot,
            "price_cents": booking.price_cents,
            "price": cents_to_eur_float(booking.price_cents),
            "billing_mode": booking.billing_mode,
            "event_list_id": booking.event_list_id,
            "quantity": 1,
            "consumed_at": booking.created_at,
        }
        try:
            resp = await self._client.post(
                self.vend_path,
                json=payload,
                headers={"X-Idempotency-Key": booking.idempotency_key},
            )
        except httpx.HTTPError as exc:
            self._last_error = str(exc)
            raise FFWebTemporaryError(f"FFWEB unreachable during vend sync: {exc}") from exc

        if resp.status_code in {200, 201, 202, 204, 409}:
            # 409 Conflict on duplicate idempotency_key means already booked safely
            self._last_error = None
            if resp.content:
                try:
                    body = resp.json()
                    if isinstance(body, dict) and ("balance_cents" in body or "balance" in body):
                        return self._extract_cents(body, "balance_cents", "balance")
                except Exception:
                    pass
            return None

        if resp.status_code >= 500 or resp.status_code == 429 or resp.status_code == 408:
            self._last_error = f"HTTP {resp.status_code}"
            raise FFWebTemporaryError(f"FFWEB temporary failure HTTP {resp.status_code}")

        self._last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
        raise FFWebPermanentError(self._last_error)

    def status_snapshot(self) -> dict[str, Any]:
        return {
            "mode": "http",
            "base_url": self.base_url,
            "api_key_configured": bool(self.api_key),
            "last_error": self._last_error,
        }
