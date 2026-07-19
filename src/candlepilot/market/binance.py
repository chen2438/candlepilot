from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from candlepilot.domain.models import MarketSnapshot
from candlepilot.market.features import (
    DAILY_STRUCTURE_INTERVAL,
    DECISION_FEATURE_INTERVALS,
    FeaturePipeline,
)
from candlepilot.market.scanner import MarketCandidateInput
from candlepilot.risk.engine import SymbolRules


BINANCE_FUTURES_PRODUCTION = "https://fapi.binance.com"
BINANCE_FUTURES_TESTNET = "https://demo-fapi.binance.com"


class BinanceError(RuntimeError):
    pass


class BinanceRateLimit(BinanceError):
    def __init__(self, retry_after: float | None) -> None:
        super().__init__("Binance request rate limit exceeded")
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class ContractInfo:
    symbol: str
    onboard_date: datetime
    rules: SymbolRules


@dataclass(frozen=True, slots=True)
class FundingRate:
    timestamp: datetime
    rate: Decimal
    mark_price: Decimal | None = None


class BinancePublicClient:
    """Read-only USD-M futures client used for discovery, features and backtests."""

    def __init__(
        self,
        *,
        base_url: str = BINANCE_FUTURES_PRODUCTION,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(10),
            headers={"User-Agent": "CandlePilot/0.1"},
        )

    async def __aenter__(self) -> BinancePublicClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _get(self, path: str, **params: str | int) -> Any:
        try:
            response = await self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise BinanceError(f"Binance transport failure: {exc}") from exc
        if response.status_code in {418, 429}:
            retry = response.headers.get("Retry-After")
            raise BinanceRateLimit(float(retry) if retry else None)
        if response.is_error:
            try:
                detail = response.json().get("msg", response.text)
            except (ValueError, AttributeError):
                detail = response.text
            raise BinanceError(f"Binance HTTP {response.status_code}: {detail}")
        return response.json()

    async def server_time(self) -> datetime:
        payload = await self._get("/fapi/v1/time")
        return datetime.fromtimestamp(payload["serverTime"] / 1000, tz=UTC)

    async def exchange_info(self) -> dict[str, ContractInfo]:
        payload = await self._get("/fapi/v1/exchangeInfo")
        contracts: dict[str, ContractInfo] = {}
        for item in payload.get("symbols", []):
            if (
                item.get("contractType") != "PERPETUAL"
                or item.get("quoteAsset") != "USDT"
                or item.get("status") != "TRADING"
            ):
                continue
            filters = {entry["filterType"]: entry for entry in item.get("filters", [])}
            lot = filters.get("LOT_SIZE", {})
            market_lot = filters.get("MARKET_LOT_SIZE", {})
            notional = filters.get("MIN_NOTIONAL", {})
            price = filters.get("PRICE_FILTER", {})
            contracts[item["symbol"]] = ContractInfo(
                symbol=item["symbol"],
                onboard_date=datetime.fromtimestamp(item["onboardDate"] / 1000, tz=UTC),
                rules=SymbolRules(
                    quantity_step=Decimal(lot.get("stepSize", "1")),
                    min_quantity=Decimal(lot.get("minQty", "1")),
                    min_notional=Decimal(notional.get("notional", "5")),
                    tick_size=Decimal(price.get("tickSize", "0.01")),
                    max_quantity=(
                        Decimal(lot["maxQty"])
                        if lot.get("maxQty") is not None
                        else None
                    ),
                    market_quantity_step=(
                        Decimal(market_lot["stepSize"])
                        if market_lot.get("stepSize") is not None
                        else None
                    ),
                    market_min_quantity=(
                        Decimal(market_lot["minQty"])
                        if market_lot.get("minQty") is not None
                        else None
                    ),
                    market_max_quantity=(
                        Decimal(market_lot["maxQty"])
                        if market_lot.get("maxQty") is not None
                        else None
                    ),
                ),
            )
        return contracts

    async def candidate_inputs(self, *, now: datetime | None = None) -> list[MarketCandidateInput]:
        now = now or datetime.now(UTC)
        contracts = await self.exchange_info()
        tickers = await self._get("/fapi/v1/ticker/24hr")
        books = await self._get("/fapi/v1/ticker/bookTicker")
        book_by_symbol = {item["symbol"]: item for item in books}
        inputs: list[MarketCandidateInput] = []
        for ticker in tickers:
            contract = contracts.get(ticker.get("symbol", ""))
            book = book_by_symbol.get(ticker.get("symbol", ""))
            if contract is None or book is None:
                continue
            bid = Decimal(book["bidPrice"])
            ask = Decimal(book["askPrice"])
            high = Decimal(ticker["highPrice"])
            low = Decimal(ticker["lowPrice"])
            last = Decimal(ticker["lastPrice"])
            volatility = (high - low) / last if last > 0 else Decimal("0")
            trend = Decimal(ticker["priceChangePercent"]) / Decimal("100")
            inputs.append(
                MarketCandidateInput(
                    symbol=contract.symbol,
                    quote_volume_24h=Decimal(ticker["quoteVolume"]),
                    bid=bid,
                    ask=ask,
                    volatility=volatility,
                    trend_strength=trend,
                    listing_age_days=max(0, (now - contract.onboard_date).days),
                )
            )
        return inputs

    async def klines(self, symbol: str, interval: str, limit: int = 200) -> list[list[Any]]:
        if interval not in {"1m", "5m", "15m", "30m", "1h", "4h", "1d"}:
            raise ValueError("unsupported kline interval")
        if not 1 <= limit <= 1500:
            raise ValueError("kline limit must be between 1 and 1500")
        return await self._get(
            "/fapi/v1/klines", symbol=symbol, interval=interval, limit=limit
        )

    async def historical_klines(
        self,
        symbol: str,
        interval: str,
        start: datetime,
        end: datetime,
        *,
        max_candles: int = 10_000,
    ) -> list[list[Any]]:
        if interval not in {"1m", "5m", "15m", "30m", "1h", "4h", "1d"}:
            raise ValueError("unsupported kline interval")
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("historical range must be timezone-aware")
        if end <= start:
            raise ValueError("historical range end must be after start")
        if not 1 <= max_candles <= 100_000:
            raise ValueError("max_candles must be between 1 and 100000")

        interval_ms = {
            "1m": 60_000,
            "5m": 300_000,
            "15m": 900_000,
            "30m": 1_800_000,
            "1h": 3_600_000,
            "4h": 14_400_000,
            "1d": 86_400_000,
        }[interval]
        cursor = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        rows: list[list[Any]] = []
        while cursor < end_ms and len(rows) < max_candles:
            page_limit = min(1500, max_candles - len(rows))
            page = await self._get(
                "/fapi/v1/klines",
                symbol=symbol,
                interval=interval,
                startTime=cursor,
                endTime=end_ms - 1,
                limit=page_limit,
            )
            if not page:
                break
            rows.extend(row for row in page if cursor <= int(row[0]) < end_ms)
            next_cursor = int(page[-1][0]) + interval_ms
            if next_cursor <= cursor or len(page) < page_limit:
                break
            cursor = next_cursor
        return rows[:max_candles]

    async def historical_funding_rates(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        *,
        max_events: int = 10_000,
    ) -> list[FundingRate]:
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("funding range must be timezone-aware")
        if end <= start:
            raise ValueError("funding range end must be after start")
        if not 1 <= max_events <= 100_000:
            raise ValueError("max_events must be between 1 and 100000")

        cursor = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        events: list[FundingRate] = []
        while cursor < end_ms and len(events) < max_events:
            page_limit = min(1000, max_events - len(events))
            page = await self._get(
                "/fapi/v1/fundingRate",
                symbol=symbol,
                startTime=cursor,
                endTime=end_ms - 1,
                limit=page_limit,
            )
            if not page:
                break
            for item in page:
                timestamp_ms = int(item["fundingTime"])
                if cursor <= timestamp_ms < end_ms:
                    mark_price = item.get("markPrice")
                    events.append(
                        FundingRate(
                            timestamp=datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC),
                            rate=Decimal(item["fundingRate"]),
                            mark_price=Decimal(mark_price) if mark_price is not None else None,
                        )
                    )
            next_cursor = int(page[-1]["fundingTime"]) + 1
            if next_cursor <= cursor or len(page) < page_limit:
                break
            cursor = next_cursor
        return events[:max_events]

    # The microstructure endpoints, named. market_snapshot and the book
    # collector both need exactly these, and a live capture and a recorded one
    # have to come from the same place or the recording is of something else.
    async def book_ticker(self, symbol: str) -> dict[str, Any]:
        return await self._get("/fapi/v1/ticker/bookTicker", symbol=symbol)

    async def premium_index(self, symbol: str) -> dict[str, Any]:
        return await self._get("/fapi/v1/premiumIndex", symbol=symbol)

    async def depth(self, symbol: str, limit: int = 20) -> dict[str, Any]:
        return await self._get("/fapi/v1/depth", symbol=symbol, limit=limit)

    async def open_interest(self, symbol: str) -> dict[str, Any]:
        return await self._get("/fapi/v1/openInterest", symbol=symbol)

    async def agg_trades(self, symbol: str, limit: int = 1000) -> list[dict[str, Any]]:
        return await self._get("/fapi/v1/aggTrades", symbol=symbol, limit=limit)

    async def market_snapshot(self, symbol: str, cadence: str) -> MarketSnapshot:
        if cadence not in set(DECISION_FEATURE_INTERVALS):
            raise ValueError("unsupported decision cadence")
        # `cadence` only labels which decision the snapshot feeds; the feature
        # ladder below is the same either way, so the two stay separate concepts
        # even though they currently list the same intervals.
        feature_intervals = (*DECISION_FEATURE_INTERVALS, DAILY_STRUCTURE_INTERVAL)
        results = await asyncio.gather(
            *(self.klines(symbol, interval, 200) for interval in feature_intervals),
            self.book_ticker(symbol),
            self._get("/fapi/v1/ticker/24hr", symbol=symbol),
            self.premium_index(symbol),
            self.depth(symbol),
            self.open_interest(symbol),
            self.agg_trades(symbol),
        )
        rows_by_interval = dict(
            zip(feature_intervals, results[: len(feature_intervals)], strict=True)
        )
        book, ticker, premium, depth, interest, trades = results[len(feature_intervals) :]
        pipeline = FeaturePipeline()
        mark_price = Decimal(premium["markPrice"])
        features = pipeline.microstructure(
            mark_price=mark_price,
            index_price=Decimal(premium["indexPrice"]),
            open_interest=Decimal(interest["openInterest"]),
            bids=depth["bids"],
            asks=depth["asks"],
            trades=trades,
        )
        daily_rows = rows_by_interval.pop(DAILY_STRUCTURE_INTERVAL)
        features.update(pipeline.multitimeframe(rows_by_interval))
        features.update(pipeline.daily_structure(daily_rows, mark_price=mark_price))
        return pipeline.snapshot(
            symbol=symbol,
            cadence=cadence,  # type: ignore[arg-type]
            features=features,
            mark_price=mark_price,
            bid=Decimal(book["bidPrice"]),
            ask=Decimal(book["askPrice"]),
            quote_volume_24h=Decimal(ticker["quoteVolume"]),
            funding_rate=Decimal(premium["lastFundingRate"]),
        )
