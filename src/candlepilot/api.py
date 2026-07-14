from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from candlepilot.application.engine import TradingEngine
from candlepilot.application.scheduler import TradingScheduler
from candlepilot.backtest.engine import BacktestConfig, BacktestEngine, Candle, ReplayIntent
from candlepilot.broker.binance_testnet import BinanceTestnetBroker, BinanceTestnetCredentials
from candlepilot.config import Settings
from candlepilot.domain.models import MarketSnapshot, PortfolioState, TradeIntent
from candlepilot.market.binance import BinancePublicClient
from candlepilot.market.cache import HistoricalMarketCache
from candlepilot.market.history import build_backtest_candles
from candlepilot.providers.registry import ProviderRegistry
from candlepilot.risk.engine import SymbolRules
from candlepilot.storage.database import AuditRepository, Database


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProviderSelection(ApiModel):
    name: str


class SymbolRulesInput(ApiModel):
    quantity_step: Annotated[Decimal, Field(gt=0)]
    min_quantity: Annotated[Decimal, Field(gt=0)]
    min_notional: Annotated[Decimal, Field(gt=0)]


class DecisionRequest(ApiModel):
    snapshot: MarketSnapshot
    portfolio: PortfolioState
    rules: SymbolRulesInput


class BacktestCandleInput(ApiModel):
    timestamp: datetime
    open: Annotated[Decimal, Field(gt=0)]
    high: Annotated[Decimal, Field(gt=0)]
    low: Annotated[Decimal, Field(gt=0)]
    close: Annotated[Decimal, Field(gt=0)]
    volume: Annotated[Decimal, Field(ge=0)]
    funding_rate: Decimal = Decimal("0")


class ReplayIntentInput(ApiModel):
    decided_at: datetime
    intent: TradeIntent


class BacktestConfigInput(ApiModel):
    initial_equity: Annotated[Decimal, Field(gt=0)] = Decimal("10000")
    fee_rate: Annotated[Decimal, Field(ge=0, le=1)] = Decimal("0.0005")
    slippage_fraction: Annotated[Decimal, Field(ge=0, le=1)] = Decimal("0.0005")
    max_risk_fraction: Annotated[Decimal, Field(gt=0, le=1)] = Decimal("0.02")
    max_margin_fraction: Annotated[Decimal, Field(gt=0, le=1)] = Decimal("0.60")


class BacktestRunRequest(ApiModel):
    symbol: str = Field(pattern=r"^[A-Z0-9]+USDT$")
    cadence: Literal["1m", "5m", "15m"]
    candles: list[BacktestCandleInput] = Field(min_length=1, max_length=100_000)
    decisions: list[ReplayIntentInput] = Field(default_factory=list, max_length=100_000)
    config: BacktestConfigInput = Field(default_factory=BacktestConfigInput)


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _status(engine: TradingEngine) -> dict[str, Any]:
    return {
        "mode": engine.mode.value,
        "running": engine.running,
        "emergency_locked": engine.emergency_locked,
        "selected_provider": engine.selected_provider,
        "candidate_count": len(engine.candidates),
        "universe_refreshed_at": engine.universe_refreshed_at.isoformat()
        if engine.universe_refreshed_at
        else None,
    }


def create_app(
    *,
    settings: Settings | None = None,
    database: Database | None = None,
    market: BinancePublicClient | None = None,
    engine: TradingEngine | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    owns_database = database is None
    owns_market = market is None
    database = database or Database(settings.database_url)
    market = market or BinancePublicClient()
    testnet_broker = None
    if settings.binance_testnet_api_key and settings.binance_testnet_api_secret:
        testnet_broker = BinanceTestnetBroker(
            BinanceTestnetCredentials(
                settings.binance_testnet_api_key,
                settings.binance_testnet_api_secret,
            )
        )
    engine = engine or TradingEngine(
        mode=settings.mode,
        providers=ProviderRegistry(),
        audit=AuditRepository(database.sessions),
        market=market,
        testnet_broker=testnet_broker,
    )
    scheduler = TradingScheduler(engine, market)
    history_cache = HistoricalMarketCache(settings.data_dir / "market")

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await database.initialize()
        yield
        await scheduler.stop()
        if owns_market:
            await market.close()
        if testnet_broker is not None:
            await testnet_broker.close()
        if owns_database:
            await database.close()

    app = FastAPI(
        title="CandlePilot API",
        version="0.1.0",
        description="Local-only API for paper and Binance testnet trading",
        lifespan=lifespan,
    )
    app.state.engine = engine
    app.state.database = database
    app.state.scheduler = scheduler
    app.state.history_cache = history_cache

    @app.get("/api/status")
    async def get_status() -> dict[str, Any]:
        return _status(engine)

    @app.get("/api/providers")
    async def get_providers() -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") for item in await engine.provider_health()]

    @app.post("/api/providers/select")
    async def select_provider(selection: ProviderSelection) -> dict[str, Any]:
        try:
            engine.select_provider(selection.name)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return _status(engine)

    @app.post("/api/engine/start")
    async def start_engine() -> dict[str, Any]:
        try:
            await engine.start()
            scheduler.start()
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _status(engine)

    @app.post("/api/engine/stop")
    async def stop_engine() -> dict[str, Any]:
        engine.stop()
        await scheduler.stop()
        return _status(engine)

    @app.post("/api/engine/emergency-stop")
    async def emergency_stop() -> dict[str, Any]:
        await scheduler.stop()
        await engine.emergency_stop()
        return _status(engine)

    @app.post("/api/engine/clear-emergency-lock")
    async def clear_emergency_lock() -> dict[str, Any]:
        try:
            engine.clear_emergency_lock()
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _status(engine)

    @app.get("/api/universe")
    async def get_universe() -> list[dict[str, Any]]:
        return [
            {
                "symbol": item.symbol,
                "score": str(item.score),
                "volume_rank": item.volume_rank,
                "spread_bps": str(item.spread_bps),
                "volatility": str(item.volatility),
                "trend_strength": str(item.trend_strength),
            }
            for item in engine.candidates
        ]

    @app.get("/api/market/klines")
    async def get_historical_klines(
        symbol: str,
        cadence: Literal["1m", "5m", "15m"],
        start: datetime,
        end: datetime,
        limit: int = 10_000,
    ) -> list[dict[str, Any]]:
        try:
            rows = await market.historical_klines(
                symbol.upper(), cadence, start, end, max_candles=limit
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"market history failed: {exc}") from exc
        return [
            {
                "timestamp": datetime.fromtimestamp(int(row[0]) / 1000, tz=UTC),
                "open": row[1],
                "high": row[2],
                "low": row[3],
                "close": row[4],
                "volume": row[5],
                "funding_rate": "0",
            }
            for row in rows
        ]

    @app.get("/api/market/funding-rates")
    async def get_historical_funding_rates(
        symbol: str,
        start: datetime,
        end: datetime,
        limit: int = 10_000,
    ) -> list[dict[str, Any]]:
        try:
            events = await market.historical_funding_rates(
                symbol.upper(), start, end, max_events=limit
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"funding history failed: {exc}") from exc
        return [
            {
                "timestamp": event.timestamp,
                "rate": str(event.rate),
                "mark_price": str(event.mark_price) if event.mark_price is not None else None,
            }
            for event in events
        ]

    @app.get("/api/market/backtest-candles")
    async def get_backtest_candles(
        symbol: str,
        cadence: Literal["1m", "5m", "15m"],
        start: datetime,
        end: datetime,
        limit: int = 10_000,
    ) -> list[dict[str, Any]]:
        try:
            cached = await asyncio.to_thread(
                history_cache.load, symbol.upper(), cadence, start, end, limit
            )
            if cached is not None:
                return cached
            rows, events = await asyncio.gather(
                market.historical_klines(
                    symbol.upper(), cadence, start, end, max_candles=limit
                ),
                market.historical_funding_rates(symbol.upper(), start, end),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"backtest history failed: {exc}") from exc
        candles = build_backtest_candles(rows, events, cadence)
        await asyncio.to_thread(
            history_cache.store,
            symbol.upper(),
            cadence,
            start,
            end,
            limit,
            candles,
        )
        return candles

    @app.post("/api/universe/refresh")
    async def refresh_universe() -> list[dict[str, Any]]:
        try:
            await engine.refresh_universe()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"market refresh failed: {exc}") from exc
        return await get_universe()

    @app.get("/api/signals")
    async def get_signals(limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise HTTPException(status_code=422, detail="limit must be between 1 and 500")
        return await engine.audit.recent_intents(limit)

    @app.post("/api/decisions/evaluate")
    async def evaluate_decision(request: DecisionRequest) -> dict[str, Any]:
        rules = SymbolRules(
            request.rules.quantity_step,
            request.rules.min_quantity,
            request.rules.min_notional,
        )
        try:
            outcome = await engine.evaluate(request.snapshot, request.portfolio, rules)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "provider": outcome.provider,
            "intent": outcome.intent.model_dump(mode="json"),
            "risk": outcome.risk.model_dump(mode="json"),
            "execution": outcome.execution.model_dump(mode="json")
            if outcome.execution
            else None,
        }

    @app.get("/api/backtests")
    async def get_backtests(limit: int = 20) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise HTTPException(status_code=422, detail="limit must be between 1 and 100")
        return await engine.audit.recent_backtests(limit)

    @app.post("/api/backtests", status_code=201)
    async def run_backtest(request: BacktestRunRequest) -> dict[str, Any]:
        mismatched = [
            replay
            for replay in request.decisions
            if replay.intent.symbol != request.symbol
            or replay.intent.cadence != request.cadence
        ]
        if mismatched:
            raise HTTPException(
                status_code=422,
                detail="all replay intents must match the requested symbol and cadence",
            )
        config = BacktestConfig(**request.config.model_dump())
        candles = [Candle(**item.model_dump()) for item in request.candles]
        decisions = [ReplayIntent(item.decided_at, item.intent) for item in request.decisions]
        try:
            result = BacktestEngine(config).run(candles, decisions)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return await engine.audit.record_backtest(
            request.symbol,
            request.cadence,
            _json_value(asdict(result)),
        )

    @app.websocket("/ws/events")
    async def event_stream(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            while True:
                await websocket.send_json({"type": "status", "data": _status(engine)})
                await asyncio.sleep(2)
        except (WebSocketDisconnect, RuntimeError):
            return

    frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if frontend_dist.is_dir():
        app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="console")

    return app


app = create_app()
