from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from statistics import fmean
from typing import Any, Protocol

import httpx


DERIBIT_PUBLIC_API = "https://www.deribit.com/api/v2"
INSTRUMENT_CACHE_SECONDS = 60 * 60
SNAPSHOT_CACHE_SECONDS = 5 * 60
FAILURE_CACHE_SECONDS = 60
BENCHMARK_UNDERLYINGS = ("BTC", "ETH")


class DeribitOptionsError(RuntimeError):
    pass


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed else None


def _ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator > 0 else None


class OptionsContextSource(Protocol):
    async def context(self, symbol: str) -> dict[str, Any]: ...


class DeribitPublicOptionsClient:
    """Read-only client for Deribit's public option metadata and summaries."""

    def __init__(
        self,
        *,
        base_url: str = DERIBIT_PUBLIC_API,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(8),
            headers={"User-Agent": "CandlePilot/0.1"},
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _get(self, method: str, **params: str | bool) -> list[dict[str, Any]]:
        try:
            response = await self._client.get(f"/public/{method}", params=params)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise DeribitOptionsError("Deribit options transport failure") from exc
        if not isinstance(payload, Mapping):
            raise DeribitOptionsError("Deribit options response is not an object")
        error = payload.get("error")
        if error is not None:
            raise DeribitOptionsError("Deribit options API rejected the request")
        result = payload.get("result")
        if not isinstance(result, list):
            raise DeribitOptionsError("Deribit options response has no result list")
        return [dict(item) for item in result if isinstance(item, Mapping)]

    async def instruments(self) -> list[dict[str, Any]]:
        return await self._get(
            "get_instruments",
            currency="any",
            kind="option",
            expired=False,
        )

    async def summaries(self, currency: str) -> list[dict[str, Any]]:
        return await self._get(
            "get_book_summary_by_currency",
            currency=currency,
            kind="option",
        )


def _aggregate_expiries(
    rows: Sequence[dict[str, Any]],
    *,
    underlying_price: float,
    now: datetime,
) -> list[dict[str, Any]]:
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[int(row["expiration_timestamp"])].append(row)
    result: list[dict[str, Any]] = []
    for expiration, expiry_rows in sorted(grouped.items())[:3]:
        call_oi = sum(row["open_interest"] for row in expiry_rows if row["option_type"] == "call")
        put_oi = sum(row["open_interest"] for row in expiry_rows if row["option_type"] == "put")
        volume = sum(row["volume_24h"] for row in expiry_rows)
        expiry_prices = [
            row["underlying_price"] for row in expiry_rows if row["underlying_price"] is not None
        ]
        expiry_underlying_price = fmean(expiry_prices) if expiry_prices else underlying_price
        closest_distance = min(abs(row["strike"] - expiry_underlying_price) for row in expiry_rows)
        atm_ivs = [
            row["mark_iv"]
            for row in expiry_rows
            if abs(row["strike"] - expiry_underlying_price) == closest_distance
            and row["mark_iv"] is not None
        ]
        expiry_time = datetime.fromtimestamp(expiration / 1000, tz=UTC)
        result.append(
            {
                "expiration": expiry_time.isoformat(),
                "days_to_expiry": max((expiry_time - now).total_seconds() / 86400, 0),
                "call_open_interest": call_oi,
                "put_open_interest": put_oi,
                "put_call_open_interest_ratio": _ratio(put_oi, call_oi),
                "volume_24h": volume,
                "atm_mark_iv": fmean(atm_ivs) if atm_ivs else None,
            }
        )
    return result


def _largest_strikes(
    rows: Sequence[dict[str, Any]],
    *,
    option_type: str,
    underlying_price: float,
) -> list[dict[str, float]]:
    totals: dict[float, float] = defaultdict(float)
    for row in rows:
        if row["option_type"] == option_type:
            totals[row["strike"]] += row["open_interest"]
    ranked = sorted(totals.items(), key=lambda item: (-item[1], abs(item[0] - underlying_price)))
    return [
        {
            "strike": strike,
            "open_interest": open_interest,
            "distance_percent": (strike - underlying_price) / underlying_price * 100,
        }
        for strike, open_interest in ranked[:3]
        if open_interest > 0
    ]


def aggregate_option_snapshot(
    underlying: str,
    instruments: Sequence[Mapping[str, Any]],
    summaries: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
) -> dict[str, Any]:
    metadata = {
        str(item.get("instrument_name")): item
        for item in instruments
        if item.get("base_currency") == underlying
        and item.get("kind") == "option"
        and item.get("is_active") is True
    }
    rows: list[dict[str, Any]] = []
    for summary in summaries:
        instrument = metadata.get(str(summary.get("instrument_name")))
        if instrument is None:
            continue
        strike = _number(instrument.get("strike"))
        open_interest = _number(summary.get("open_interest"))
        expiration = instrument.get("expiration_timestamp")
        option_type = instrument.get("option_type")
        if (
            strike is None
            or strike <= 0
            or open_interest is None
            or open_interest < 0
            or not isinstance(expiration, int)
            or option_type not in {"call", "put"}
        ):
            continue
        underlying_price = _number(summary.get("underlying_price"))
        rows.append(
            {
                "instrument_name": instrument["instrument_name"],
                "expiration_timestamp": expiration,
                "strike": strike,
                "option_type": option_type,
                "open_interest": open_interest,
                "volume_24h": max(_number(summary.get("volume")) or 0, 0),
                "mark_iv": _number(summary.get("mark_iv")),
                "underlying_price": underlying_price,
                "quoted": summary.get("bid_price") is not None
                or summary.get("ask_price") is not None,
            }
        )

    prices = [row["underlying_price"] for row in rows if row["underlying_price"] is not None]
    if not rows or not prices:
        return {
            "status": "unavailable",
            "underlying": underlying,
            "reason": "no active option chain with an underlying price",
        }
    nearest_expiration = min(row["expiration_timestamp"] for row in rows)
    nearest_prices = [
        row["underlying_price"]
        for row in rows
        if row["expiration_timestamp"] == nearest_expiration and row["underlying_price"] is not None
    ]
    underlying_price = fmean(nearest_prices or prices)
    expiries = {row["expiration_timestamp"] for row in rows}
    positive_oi = [row for row in rows if row["open_interest"] > 0]
    total_call_oi = sum(row["open_interest"] for row in rows if row["option_type"] == "call")
    total_put_oi = sum(row["open_interest"] for row in rows if row["option_type"] == "put")
    total_volume = sum(row["volume_24h"] for row in rows)
    near_spot = [
        row
        for row in positive_oi
        if abs(row["strike"] - underlying_price) / underlying_price <= 0.30
    ]
    near_calls = sum(row["option_type"] == "call" for row in near_spot)
    near_puts = sum(row["option_type"] == "put" for row in near_spot)
    iv_count = sum(row["mark_iv"] is not None and row["mark_iv"] > 0 for row in rows)
    quoted_count = sum(row["quoted"] for row in rows)
    failures = []
    if len(expiries) < 2:
        failures.append("fewer than two active expiries")
    if len(positive_oi) < 6:
        failures.append("fewer than six contracts have open interest")
    if total_call_oi <= 0 or total_put_oi <= 0:
        failures.append("call or put open interest is absent")
    if total_volume <= 0:
        failures.append("24h option volume is zero")
    if near_calls < 2 or near_puts < 2:
        failures.append("near-spot call or put coverage is too sparse")
    if iv_count < 4:
        failures.append("fewer than four contracts have mark IV")
    if quoted_count < 4:
        failures.append("fewer than four contracts have a live quote")
    quality = {
        "eligible": not failures,
        "failures": failures,
        "active_contracts": len(rows),
        "active_expiries": len(expiries),
        "contracts_with_open_interest": len(positive_oi),
        "contracts_with_mark_iv": iv_count,
        "contracts_with_quote": quoted_count,
        "near_spot_call_contracts": near_calls,
        "near_spot_put_contracts": near_puts,
    }
    if failures:
        return {
            "status": "unavailable",
            "underlying": underlying,
            "reason": "option chain did not pass the deterministic quality gate",
            "quality": quality,
        }

    expiry_rows = _aggregate_expiries(rows, underlying_price=underlying_price, now=now)
    total_oi = total_call_oi + total_put_oi
    expiry_oi = [row["call_open_interest"] + row["put_open_interest"] for row in expiry_rows]
    atm_ivs = [row["atm_mark_iv"] for row in expiry_rows if row["atm_mark_iv"] is not None]
    return {
        "status": "available",
        "underlying": underlying,
        "underlying_price": underlying_price,
        "open_interest_unit": f"{underlying} underlying units",
        "call_open_interest": total_call_oi,
        "put_open_interest": total_put_oi,
        "put_call_open_interest_ratio": _ratio(total_put_oi, total_call_oi),
        "option_volume_24h": total_volume,
        "largest_call_open_interest_strikes": _largest_strikes(
            rows, option_type="call", underlying_price=underlying_price
        ),
        "largest_put_open_interest_strikes": _largest_strikes(
            rows, option_type="put", underlying_price=underlying_price
        ),
        "nearest_expiries": expiry_rows,
        "near_to_next_atm_iv_change": (atm_ivs[1] - atm_ivs[0] if len(atm_ivs) >= 2 else None),
        "largest_near_expiry_open_interest_fraction": (
            max(expiry_oi) / total_oi if expiry_oi and total_oi > 0 else None
        ),
        "quality": quality,
    }


class DeribitOptionsContextProvider:
    """Build compact, cached option context for analysis data packs."""

    def __init__(self, client: DeribitPublicOptionsClient | None = None) -> None:
        self.client = client or DeribitPublicOptionsClient()
        self._owns_client = client is None
        self._instruments: list[dict[str, Any]] | None = None
        self._instruments_expires_at = 0.0
        self._snapshots: dict[str, dict[str, Any]] = {}
        self._snapshot_as_of: datetime | None = None
        self._snapshots_expires_at = 0.0
        self._failure_expires_at = 0.0
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        if self._owns_client:
            await self.client.close()

    async def context(self, symbol: str) -> dict[str, Any]:
        requested = symbol.upper().removesuffix("USDT")
        try:
            snapshots, as_of = await self._current_snapshots()
        except Exception:
            return {
                "source": "deribit_public",
                "available": False,
                "as_of": None,
                "requested_underlying": requested,
                "direct": {
                    "underlying": requested,
                    "available": False,
                    "reason": "options source temporarily unavailable",
                },
                "benchmark_underlyings": [],
                "snapshots": {},
            }
        selected = set(BENCHMARK_UNDERLYINGS)
        selected.add(requested)
        compact = {
            underlying: snapshots[underlying]
            for underlying in sorted(selected)
            if snapshots.get(underlying, {}).get("status") == "available"
        }
        direct_snapshot = snapshots.get(requested)
        direct_available = (
            direct_snapshot is not None and direct_snapshot.get("status") == "available"
        )
        direct = {
            "underlying": requested,
            "available": direct_available,
            "snapshot_key": requested if direct_available else None,
            "reason": None,
        }
        if not direct_available:
            direct["reason"] = (
                direct_snapshot.get("reason")
                if direct_snapshot is not None
                else "no active option chain was discovered for this underlying"
            )
            if direct_snapshot is not None and direct_snapshot.get("quality") is not None:
                direct["quality"] = direct_snapshot["quality"]
        benchmarks = [underlying for underlying in BENCHMARK_UNDERLYINGS if underlying in compact]
        return {
            "source": "deribit_public",
            "available": bool(compact),
            "as_of": as_of.isoformat(),
            "data_age_seconds": max((datetime.now(UTC) - as_of).total_seconds(), 0),
            "requested_underlying": requested,
            "direct": direct,
            "benchmark_underlyings": benchmarks,
            "snapshots": compact,
            "interpretation_limits": [
                "open interest is not signed trader or dealer positioning",
                "large open-interest strikes are context, not proven support or resistance",
                "put/call ratios and IV structure are not standalone directional signals",
                "benchmark options must not be represented as the requested symbol's own options",
            ],
        }

    async def _current_snapshots(self) -> tuple[dict[str, dict[str, Any]], datetime]:
        now_monotonic = time.monotonic()
        if now_monotonic < self._failure_expires_at:
            raise DeribitOptionsError("Deribit options refresh is cooling down")
        if self._snapshot_as_of is not None and now_monotonic < self._snapshots_expires_at:
            return self._snapshots, self._snapshot_as_of
        async with self._lock:
            now_monotonic = time.monotonic()
            if now_monotonic < self._failure_expires_at:
                raise DeribitOptionsError("Deribit options refresh is cooling down")
            if self._snapshot_as_of is not None and now_monotonic < self._snapshots_expires_at:
                return self._snapshots, self._snapshot_as_of
            try:
                instruments = await self._current_instruments()
                settlement_currencies = sorted(
                    {
                        str(item["settlement_currency"])
                        for item in instruments
                        if item.get("settlement_currency")
                    }
                )
                responses = await asyncio.gather(
                    *(self.client.summaries(currency) for currency in settlement_currencies),
                    return_exceptions=True,
                )
                summaries = [
                    row for response in responses if isinstance(response, list) for row in response
                ]
                if not summaries:
                    raise DeribitOptionsError("Deribit returned no option summaries")
                as_of = datetime.now(UTC)
                underlyings = sorted(
                    {
                        str(item["base_currency"])
                        for item in instruments
                        if item.get("base_currency")
                    }
                )
                snapshots = {
                    underlying: aggregate_option_snapshot(
                        underlying,
                        instruments,
                        summaries,
                        now=as_of,
                    )
                    for underlying in underlyings
                }
            except Exception:
                self._failure_expires_at = time.monotonic() + FAILURE_CACHE_SECONDS
                raise
            self._snapshots = snapshots
            self._snapshot_as_of = as_of
            self._snapshots_expires_at = time.monotonic() + SNAPSHOT_CACHE_SECONDS
            self._failure_expires_at = 0.0
            return snapshots, as_of

    async def _current_instruments(self) -> list[dict[str, Any]]:
        now_monotonic = time.monotonic()
        if self._instruments is not None and now_monotonic < self._instruments_expires_at:
            return self._instruments
        instruments = await self.client.instruments()
        self._instruments = instruments
        self._instruments_expires_at = time.monotonic() + INSTRUMENT_CACHE_SECONDS
        return instruments
