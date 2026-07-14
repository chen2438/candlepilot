from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from candlepilot.domain.models import MarketSnapshot
from candlepilot.market.features import FeaturePipeline
from candlepilot.market.scanner import MarketCandidateInput
from candlepilot.risk.engine import SymbolRules


BINANCE_FUTURES_PRODUCTION = "https://fapi.binance.com"
BINANCE_FUTURES_TESTNET = "https://testnet.binancefuture.com"


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


class BinancePublicClient:
    """Read-only USD-M futures client used for discovery and paper trading."""

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
            notional = filters.get("MIN_NOTIONAL", {})
            contracts[item["symbol"]] = ContractInfo(
                symbol=item["symbol"],
                onboard_date=datetime.fromtimestamp(item["onboardDate"] / 1000, tz=UTC),
                rules=SymbolRules(
                    quantity_step=Decimal(lot.get("stepSize", "1")),
                    min_quantity=Decimal(lot.get("minQty", "1")),
                    min_notional=Decimal(notional.get("notional", "5")),
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
        if interval not in {"1m", "5m", "15m", "1h"}:
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
        if interval not in {"1m", "5m", "15m", "1h"}:
            raise ValueError("unsupported kline interval")
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("historical range must be timezone-aware")
        if end <= start:
            raise ValueError("historical range end must be after start")
        if not 1 <= max_candles <= 100_000:
            raise ValueError("max_candles must be between 1 and 100000")

        interval_ms = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000}[
            interval
        ]
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

    async def market_snapshot(self, symbol: str, cadence: str) -> MarketSnapshot:
        if cadence not in {"1m", "5m", "15m"}:
            raise ValueError("unsupported decision cadence")
        rows = await self.klines(symbol, cadence, 200)
        book = await self._get("/fapi/v1/ticker/bookTicker", symbol=symbol)
        ticker = await self._get("/fapi/v1/ticker/24hr", symbol=symbol)
        premium = await self._get("/fapi/v1/premiumIndex", symbol=symbol)
        return FeaturePipeline().snapshot(
            symbol=symbol,
            cadence=cadence,  # type: ignore[arg-type]
            rows=rows,
            mark_price=Decimal(premium["markPrice"]),
            bid=Decimal(book["bidPrice"]),
            ask=Decimal(book["askPrice"]),
            quote_volume_24h=Decimal(ticker["quoteVolume"]),
            funding_rate=Decimal(premium["lastFundingRate"]),
        )
