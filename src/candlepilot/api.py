from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from uuid import uuid4
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from candlepilot.application.engine import SUPPORTED_CADENCES, TradingEngine
from candlepilot.application.paper_feed import PaperMarketFeed
from candlepilot.application.scheduler import (
    MAX_CANDIDATES_PER_CYCLE,
    TradingScheduler,
)
from candlepilot.application.testnet_feed import TestnetUserFeed
from candlepilot.backtest.engine import BacktestConfig, BacktestEngine, Candle, ReplayIntent
from candlepilot.backtest.portfolio import PortfolioBacktestEngine
from candlepilot.backtest.replay import align_cached_intents, generate_fresh_intents
from candlepilot.broker.binance_testnet import BinanceTestnetBroker, BinanceTestnetCredentials
from candlepilot.broker.user_stream import BinanceTestnetUserStream
from candlepilot.config import (
    CUSTOM_LLM_WIRE_APIS,
    DOTENV_INJECTED_KEYS,
    ENV_FILE_VARIABLE,
    MAX_CUSTOM_LLM_PROVIDERS,
    Settings,
)
from candlepilot.domain.models import MarketSnapshot, PortfolioState, TradeIntent, TradingMode
from candlepilot.market.binance import BinancePublicClient
from candlepilot.market.cache import HistoricalMarketCache
from candlepilot.market.history import build_backtest_candles
from candlepilot.observability import AlertNotifier, OperationalMetrics, evaluate_alerts
from candlepilot.providers.pricing import (
    CACHE_FILENAME as PRICING_CACHE_FILENAME,
)
from candlepilot.providers.pricing import PROVIDER_IDS, ModelPricingCatalog
from candlepilot.providers.pricing import load_catalog as load_pricing_catalog
from candlepilot.providers.openai_compatible import validate_base_url
from candlepilot.providers.registry import ProviderRegistry
from candlepilot.provenance import BACKTEST_DATA_SCHEMA_VERSION, content_fingerprint
from candlepilot.risk.engine import AggressiveRiskPolicy, SymbolRules
from candlepilot.settings_file import (
    CUSTOM_PROVIDERS_ENV,
    ENV_FIELDS,
    describe_settings,
    mask_secret,
    read_env_file,
    write_env_file,
)
from candlepilot.storage.database import AuditRepository, CURRENT_SCHEMA_VERSION, Database


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProviderSelection(ApiModel):
    providers: list[str] | None = Field(default=None, min_length=1, max_length=16)
    name: str | None = None
    backup: str | None = None


class ProviderConfig(ApiModel):
    name: str
    model: str | None = None
    reasoning_effort: str | None = None


class ProviderTestRequest(ApiModel):
    name: str


class CustomProviderInput(ApiModel):
    id: str
    base_url: str
    model: str | None = None
    reasoning_effort: str | None = None
    wire_api: str = "chat-completions"
    require_api_key: bool = True
    # None keeps the stored key, "" clears it, any other value replaces it — the
    # console never receives the current key, so it cannot send it back.
    api_key: str | None = None
    extra_headers: dict[str, str] | None = None


class CustomProvidersUpdate(ApiModel):
    providers: list[CustomProviderInput] = Field(max_length=MAX_CUSTOM_LLM_PROVIDERS)


class SettingsUpdate(ApiModel):
    # Only the keys the console actually changed are sent, so an untouched
    # secret is never echoed back as its own mask.
    values: dict[str, str] = Field(max_length=64)


class RunLimits(ApiModel):
    max_run_seconds: int | None = Field(default=None, gt=0, le=7 * 24 * 3600)
    max_run_cost_usd: float | None = Field(default=None, gt=0, le=10_000)


class HistoryClearRequest(ApiModel):
    categories: list[str] = Field(min_length=1, max_length=16)


class CadenceSelection(ApiModel):
    cadences: list[str] = Field(min_length=1, max_length=8)


class CandidatesPerCycleSelection(ApiModel):
    candidates_per_cycle: int = Field(ge=1, le=MAX_CANDIDATES_PER_CYCLE)


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
    cadence: Literal["1m", "5m", "15m", "30m"]
    candles: list[BacktestCandleInput] = Field(min_length=1, max_length=100_000)
    decisions: list[ReplayIntentInput] = Field(default_factory=list, max_length=100_000)
    config: BacktestConfigInput = Field(default_factory=BacktestConfigInput)


class BacktestReplayRequest(ApiModel):
    symbol: str = Field(pattern=r"^[A-Z0-9]+USDT$")
    cadence: Literal["1m", "5m", "15m", "30m"]
    start: datetime
    end: datetime
    limit: int = Field(default=10_000, ge=1, le=100_000)
    config: BacktestConfigInput = Field(default_factory=BacktestConfigInput)


class BacktestLLMRequest(BacktestReplayRequest):
    provider: str
    max_calls: int = Field(default=100, ge=1, le=500)


class PortfolioBacktestLeg(ApiModel):
    symbol: str = Field(pattern=r"^[A-Z0-9]+USDT$")
    cadence: Literal["1m", "5m", "15m", "30m"]
    candles: list[BacktestCandleInput] = Field(min_length=1, max_length=100_000)
    decisions: list[ReplayIntentInput] = Field(default_factory=list, max_length=100_000)


class PortfolioBacktestRequest(ApiModel):
    legs: list[PortfolioBacktestLeg] = Field(min_length=2, max_length=20)
    config: BacktestConfigInput = Field(default_factory=BacktestConfigInput)


# CLI-accepted aliases that are not published as models.dev ids.
_CURATED_MODEL_ALIASES: dict[str, tuple[str, ...]] = {
    "claude-code-auth": ("sonnet", "opus", "haiku", "fable"),
}
_MODEL_ID_PREFIX: dict[str, str] = {"openai": "gpt-5", "anthropic": "claude-"}


def _model_options(
    provider_name: str, catalog: ModelPricingCatalog | None, current: str | None
) -> list[str]:
    options: list[str] = list(_CURATED_MODEL_ALIASES.get(provider_name, ()))
    provider_id = PROVIDER_IDS.get(provider_name)
    if catalog is not None and provider_id is not None:
        prefix = _MODEL_ID_PREFIX.get(provider_id, "")
        catalog_ids = sorted(
            {
                model
                for (pid, model) in catalog.prices
                if pid == provider_id and model.startswith(prefix)
            }
        )
        options.extend(model for model in catalog_ids if model not in options)
    if current and current not in options:
        options.append(current)
    return options


def restart_command() -> tuple[list[str], dict[str, str]]:
    """Build the argv and environment for re-executing this backend.

    Values this process took from .env are dropped so the rewritten file is read
    again — load_dotenv never overrides a real variable, so inheriting them would
    silently keep the old configuration. Anything genuinely exported in the shell
    is preserved (it legitimately outranks .env). Re-exec goes through the module
    so `candlepilot serve` and `python -m candlepilot.cli serve` both come back.
    """

    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in DOTENV_INJECTED_KEYS
    }
    argv = [sys.executable, "-m", "candlepilot.cli", *sys.argv[1:]]
    return argv, environment


def _validate_startup_settings(settings: Settings) -> None:
    """Reject values the parsers accept but startup would later choke on.

    ``Settings`` parsing is deliberately lenient for some keys — the engine and
    scheduler do the range checks at construction. Saving such a value would
    brick the next start, so mirror those checks here.
    """

    unsupported = set(settings.cadences) - set(SUPPORTED_CADENCES)
    if unsupported:
        raise ValueError(
            f"unsupported cadences: {', '.join(sorted(unsupported))}; "
            f"choose from {', '.join(SUPPORTED_CADENCES)}"
        )
    if not 1 <= settings.candidates_per_cycle <= MAX_CANDIDATES_PER_CYCLE:
        raise ValueError(
            f"candidates_per_cycle must be between 1 and {MAX_CANDIDATES_PER_CYCLE}"
        )
    if settings.bind_host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("bind host must be localhost")


def _delete_pricing_cache(cache_dir: Path) -> int:
    try:
        (cache_dir / PRICING_CACHE_FILENAME).unlink()
        return 1
    except OSError:
        return 0


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


def _candle_from_payload(payload: dict[str, Any]) -> Candle:
    return Candle(
        timestamp=payload["timestamp"],
        open=Decimal(str(payload["open"])),
        high=Decimal(str(payload["high"])),
        low=Decimal(str(payload["low"])),
        close=Decimal(str(payload["close"])),
        volume=Decimal(str(payload["volume"])),
        funding_rate=Decimal(str(payload.get("funding_rate", "0"))),
    )


def _backtest_provenance(
    candles: list[Candle],
    *,
    prompt_versions: list[str | None] | None = None,
    models: list[str | None] | None = None,
    provider_versions: list[str | None] | None = None,
) -> dict[str, Any]:
    return {
        "data_version": content_fingerprint(
            candles,
            schema_version=BACKTEST_DATA_SCHEMA_VERSION,
        ),
        "prompt_versions": sorted({item for item in prompt_versions or [] if item}),
        "models": sorted({item for item in models or [] if item}),
        "provider_versions": sorted({item for item in provider_versions or [] if item}),
    }


def _status(engine: TradingEngine, scheduler: TradingScheduler | None = None) -> dict[str, Any]:
    paper_feed = scheduler.paper_feed if scheduler is not None else None
    testnet_feed = scheduler.testnet_feed if scheduler is not None else None
    return {
        "mode": engine.mode.value,
        "running": engine.running,
        "emergency_locked": engine.emergency_locked,
        "emergency_locked_until": engine.emergency_locked_until.isoformat()
        if engine.emergency_locked_until
        else None,
        "selected_provider": engine.selected_provider,
        "backup_provider": engine.backup_provider,
        "provider_chain": list(engine.provider_chain),
        "active_provider": engine.active_provider,
        "provider_routes": engine.provider_route_status(),
        "active_cadences": list(engine.active_cadences),
        "run_limits": {
            "max_run_seconds": engine.max_run_seconds,
            "max_run_cost_usd": engine.max_run_cost_usd,
        },
        "auto_stop_reason": engine.auto_stop_reason,
        "route_exhausted_since": engine.route_exhausted_since.isoformat()
        if engine.route_exhausted_since
        else None,
        "supported_cadences": list(SUPPORTED_CADENCES),
        "candidates_per_cycle": scheduler.candidates_per_cycle if scheduler is not None else None,
        "max_candidates_per_cycle": MAX_CANDIDATES_PER_CYCLE,
        "candidate_count": len(engine.candidates),
        "universe_refreshed_at": engine.universe_refreshed_at.isoformat()
        if engine.universe_refreshed_at
        else None,
        "market_stream": {
            "enabled": paper_feed is not None,
            "running": paper_feed.running if paper_feed is not None else False,
            "symbol_count": len(paper_feed.symbols) if paper_feed is not None else 0,
            "event_count": paper_feed.event_count if paper_feed is not None else 0,
            "backfill_count": paper_feed.backfill_count if paper_feed is not None else 0,
            "last_backfill_at": paper_feed.last_backfill_at.isoformat()
            if paper_feed is not None and paper_feed.last_backfill_at
            else None,
            "last_error": paper_feed.last_error if paper_feed is not None else None,
        },
        "user_stream": {
            "enabled": testnet_feed is not None,
            "running": testnet_feed.running if testnet_feed is not None else False,
            "event_count": testnet_feed.event_count if testnet_feed is not None else 0,
            "last_event_at": testnet_feed.last_event_at.isoformat()
            if testnet_feed is not None and testnet_feed.last_event_at
            else None,
            "reconnect_count": testnet_feed.stream.reconnect_count
            if testnet_feed is not None
            else 0,
            "dropped_event_count": testnet_feed.stream.dropped_event_count
            if testnet_feed is not None
            else 0,
            "last_error": (
                testnet_feed.last_error or testnet_feed.stream.last_error
                if testnet_feed is not None
                else None
            ),
        },
    }


def create_app(
    *,
    settings: Settings | None = None,
    database: Database | None = None,
    market: BinancePublicClient | None = None,
    engine: TradingEngine | None = None,
    pricing_loader: Callable[[Path], Awaitable[ModelPricingCatalog | None]] | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    pricing_loader = pricing_loader or load_pricing_catalog
    owns_database = database is None
    owns_market = market is None
    database = database or Database(settings.database_url)
    market = market or BinancePublicClient()
    testnet_broker = None
    testnet_stream = None
    if settings.binance_testnet_api_key and settings.binance_testnet_api_secret:
        credentials = BinanceTestnetCredentials(
            settings.binance_testnet_api_key,
            settings.binance_testnet_api_secret,
        )
        testnet_broker = BinanceTestnetBroker(credentials)
        if settings.mode == TradingMode.TESTNET:
            testnet_stream = BinanceTestnetUserStream(credentials)
    engine = engine or TradingEngine(
        mode=settings.mode,
        providers=ProviderRegistry.from_settings(settings),
        audit=AuditRepository(database.sessions),
        market=market,
        risk=AggressiveRiskPolicy(
            max_leverage=settings.max_leverage,
            max_risk_fraction=settings.max_risk_fraction,
            max_positions=settings.max_positions,
            max_margin_fraction=settings.max_margin_fraction,
            daily_loss_fraction=settings.daily_loss_fraction,
            max_snapshot_age_seconds=settings.max_snapshot_age_seconds,
            require_take_profit=settings.mode == TradingMode.TESTNET,
        ),
        testnet_broker=testnet_broker,
        cadences=settings.cadences,
    )
    if settings.provider_chain and not engine.provider_chain:
        engine.select_provider_chain(settings.provider_chain)
    elif settings.default_provider is not None and engine.selected_provider is None:
        engine.select_provider(settings.default_provider)
    if settings.max_run_seconds is not None or settings.max_run_cost_usd is not None:
        engine.select_run_limits(
            max_run_seconds=settings.max_run_seconds,
            max_run_cost_usd=settings.max_run_cost_usd,
        )

    async def load_paper_backfill(symbols: list[str]) -> list[MarketSnapshot]:
        results = await asyncio.gather(
            *(market.market_snapshot(symbol, "1m") for symbol in symbols),
            return_exceptions=True,
        )
        return [result for result in results if isinstance(result, MarketSnapshot)]

    paper_feed = (
        PaperMarketFeed(
            engine.paper_executor,
            engine.audit,
            backfill_loader=load_paper_backfill,
        )
        if engine.mode == TradingMode.PAPER and owns_market
        else None
    )
    testnet_feed = (
        TestnetUserFeed(
            testnet_stream,
            engine.audit,
            event_handler=testnet_broker.handle_user_event if testnet_broker is not None else None,
        )
        if testnet_stream is not None
        else None
    )
    async def current_run_cost_usd() -> float | None:
        if engine.run_start_inference_id is None:
            return None
        metrics = await engine.audit.run_session_metrics(
            engine.run_start_inference_id,
            end_at_id=None,
            catalog=await pricing_catalog(),
        )
        return metrics.get("equivalent_cost_usd")

    scheduler = TradingScheduler(
        engine,
        market,
        candidates_per_cycle=settings.candidates_per_cycle,
        run_cost_loader=current_run_cost_usd,
        paper_feed=paper_feed,
        testnet_feed=testnet_feed,
    )
    history_cache = HistoricalMarketCache(settings.data_dir / "market")
    operational_metrics = OperationalMetrics()
    alert_notifier = AlertNotifier()
    alert_lock = asyncio.Lock()
    request_logger = logging.getLogger("candlepilot.http")
    pricing_cache_dir = settings.data_dir / "pricing"
    pricing_lock = asyncio.Lock()
    pricing_memo: dict[str, Any] = {"catalog": None, "expires_at": None}
    testnet_account_lock = asyncio.Lock()
    env_path = Path(os.environ.get(ENV_FILE_VARIABLE, ".env")).resolve()
    settings_file_lock = asyncio.Lock()
    testnet_account_memo: dict[str, Any] = {"account": None, "expires_at": 0.0}

    async def pricing_catalog() -> ModelPricingCatalog | None:
        now = datetime.now(UTC)
        async with pricing_lock:
            expires_at = pricing_memo["expires_at"]
            if expires_at is not None and now < expires_at:
                return pricing_memo["catalog"]
            catalog = await pricing_loader(pricing_cache_dir)
            pricing_memo["catalog"] = catalog
            pricing_memo["expires_at"] = now + timedelta(hours=1)
            return catalog

    async def testnet_account() -> dict[str, Any]:
        broker = engine.testnet_broker
        if broker is None:
            raise RuntimeError("testnet broker is not configured")
        now = time.monotonic()
        if testnet_account_memo["account"] is not None:
            if now < testnet_account_memo["expires_at"]:
                return testnet_account_memo["account"]
        async with testnet_account_lock:
            now = time.monotonic()
            if testnet_account_memo["account"] is not None:
                if now < testnet_account_memo["expires_at"]:
                    return testnet_account_memo["account"]
            account = await broker.account()
            testnet_account_memo["account"] = account
            testnet_account_memo["expires_at"] = time.monotonic() + 1.0
            return account

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await database.initialize()
        await engine.restore_runtime_state()
        # Warm the models.dev pricing cache without blocking startup.
        warm_pricing = asyncio.create_task(pricing_catalog())
        yield
        warm_pricing.cancel()
        await asyncio.gather(warm_pricing, return_exceptions=True)
        await scheduler.stop()
        if owns_market:
            await market.close()
        if testnet_broker is not None:
            await testnet_broker.close()
        if testnet_feed is not None:
            await testnet_feed.close()
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
    app.state.paper_feed = paper_feed
    app.state.testnet_feed = testnet_feed
    app.state.operational_metrics = operational_metrics

    @app.middleware("http")
    async def observe_request(request: Request, call_next: Any) -> Any:
        request_id = uuid4().hex
        started = time.perf_counter()
        operational_metrics.request_started()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = (time.perf_counter() - started) * 1_000
            operational_metrics.request_finished(500, duration_ms)
            request_logger.exception(
                "request_failed",
                extra={
                    "structured": {
                        "request_id": request_id,
                        "method": request.method,
                        "path": request.url.path,
                        "status_code": 500,
                        "duration_ms": round(duration_ms, 3),
                    }
                },
            )
            raise
        duration_ms = (time.perf_counter() - started) * 1_000
        operational_metrics.request_finished(response.status_code, duration_ms)
        response.headers["X-Request-ID"] = request_id
        request_logger.info(
            "request_completed",
            extra={
                "structured": {
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "duration_ms": round(duration_ms, 3),
                }
            },
        )
        return response

    async def load_backtest_candles(
        symbol: str,
        cadence: str,
        start: datetime,
        end: datetime,
        limit: int,
    ) -> list[dict[str, Any]]:
        cached = await asyncio.to_thread(history_cache.load, symbol, cadence, start, end, limit)
        if cached is not None:
            return cached
        rows, events = await asyncio.gather(
            market.historical_klines(symbol, cadence, start, end, max_candles=limit),
            market.historical_funding_rates(symbol, start, end),
        )
        candles = build_backtest_candles(rows, events, cadence)
        await asyncio.to_thread(history_cache.store, symbol, cadence, start, end, limit, candles)
        return candles

    @app.get("/api/status")
    async def get_status() -> dict[str, Any]:
        return _status(engine, scheduler)

    @app.get("/api/health/live")
    async def health_live() -> dict[str, Any]:
        return {
            "status": "alive",
            "service": "candlepilot",
            "checked_at": datetime.now(UTC),
        }

    @app.get("/api/health/ready")
    async def health_ready() -> JSONResponse:
        checks: dict[str, dict[str, Any]] = {}
        try:
            schema_version = await database.schema_version()
            checks["database"] = {
                "ready": schema_version == CURRENT_SCHEMA_VERSION,
                "schema_version": schema_version,
                "expected_schema_version": CURRENT_SCHEMA_VERSION,
            }
        except Exception as exc:
            checks["database"] = {"ready": False, "error": str(exc)}
        broker_required = engine.mode == TradingMode.TESTNET
        checks["testnet_broker"] = {
            "ready": not broker_required or engine.testnet_broker is not None,
            "required": broker_required,
            "configured": engine.testnet_broker is not None,
        }
        ready = all(check["ready"] for check in checks.values())
        return JSONResponse(
            status_code=200 if ready else 503,
            content={
                "status": "ready" if ready else "not_ready",
                "checks": checks,
                "checked_at": datetime.now(UTC).isoformat(),
            },
        )

    @app.get("/api/providers")
    async def get_providers() -> list[dict[str, Any]]:
        health = await engine.provider_health()
        catalog = await pricing_catalog()
        result = []
        for item in health:
            provider = engine.providers.get(item.provider)
            result.append(
                {
                    **item.model_dump(mode="json"),
                    "capabilities": asdict(provider.capabilities),
                    "model": provider.model,
                    "reasoning_effort": provider.reasoning_effort,
                    "reasoning_effort_options": list(provider.reasoning_effort_options),
                    "model_options": _model_options(item.provider, catalog, provider.model),
                }
            )
        return result

    @app.post("/api/providers/config")
    async def set_provider_config(config: ProviderConfig) -> list[dict[str, Any]]:
        try:
            provider = engine.providers.get(config.name)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if engine.running:
            raise HTTPException(
                status_code=409, detail="cannot change model settings while the engine runs"
            )
        model = (config.model or "").strip() or None
        effort = (config.reasoning_effort or "").strip() or None
        if effort is not None and effort not in provider.reasoning_effort_options:
            raise HTTPException(
                status_code=422,
                detail=f"unsupported reasoning effort for {config.name}: {effort}",
            )
        provider.model = model
        provider.reasoning_effort = effort
        return await get_providers()

    @app.post("/api/providers/test")
    async def test_provider(request: ProviderTestRequest) -> dict[str, Any]:
        try:
            provider = engine.providers.get(request.name)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if engine.running:
            raise HTTPException(
                status_code=409, detail="cannot test a provider while the engine runs"
            )
        # A one-off call with a synthetic snapshot proves the applied model and
        # reasoning effort authenticate and return a schema-valid TradeIntent.
        # It is intentionally not audited so it never pollutes decisions/metrics.
        snapshot = MarketSnapshot(
            symbol="BTCUSDT",
            cadence="5m",
            timestamp=datetime.now(UTC),
            mark_price="100",
            bid="99.9",
            ask="100.1",
            quote_volume_24h="1000000",
        )
        portfolio = PortfolioState(equity="10000", available_balance="10000")
        started = time.perf_counter()
        try:
            result = await provider.generate_trade_intent(snapshot, portfolio)
        except Exception as exc:  # provider/auth/model failures surface as ok=false
            return {
                "ok": False,
                "provider": request.name,
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "detail": str(exc),
            }
        return {
            "ok": True,
            "provider": request.name,
            "model": result.model,
            "action": result.intent.action.value,
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }

    @app.get("/api/metrics/providers")
    async def get_provider_metrics(hours: int = 24) -> dict[str, Any]:
        if not 1 <= hours <= 720:
            raise HTTPException(status_code=422, detail="hours must be between 1 and 720")
        catalog = await pricing_catalog()
        return {
            "window_hours": hours,
            "pricing_source": "models.dev" if catalog is not None else None,
            "providers": await engine.audit.provider_metrics(hours, catalog=catalog),
        }

    @app.get("/api/metrics/run-session")
    async def get_run_session_metrics() -> dict[str, Any]:
        if engine.run_started_at is None or engine.run_start_inference_id is None:
            return {
                "state": "none",
                "started_at": None,
                "ended_at": None,
                "duration_seconds": 0,
                "call_count": 0,
                "error_count": 0,
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "priced_call_count": 0,
                "cost_complete": True,
                "equivalent_cost_usd": 0.0,
                "average_duration_ms": 0.0,
                "average_tokens": 0.0,
                "average_cost_usd": 0.0,
            }
        ended_at = None if engine.running else engine.run_ended_at
        measured_at = ended_at or datetime.now(UTC)
        metrics = await engine.audit.run_session_metrics(
            engine.run_start_inference_id,
            end_at_id=None if engine.running else engine.run_end_inference_id,
            catalog=await pricing_catalog(),
        )
        return {
            "state": "running" if engine.running else "completed",
            "started_at": engine.run_started_at,
            "ended_at": ended_at,
            "duration_seconds": max(0, int((measured_at - engine.run_started_at).total_seconds())),
            **metrics,
        }

    @app.get("/api/metrics/runtime")
    async def get_runtime_metrics() -> dict[str, Any]:
        return operational_metrics.snapshot()

    @app.get("/api/alerts")
    async def get_alerts() -> dict[str, Any]:
        status = _status(engine, scheduler)
        reconciliation = engine.testnet_reconciliation
        alerts = evaluate_alerts(
            operational_metrics.snapshot(),
            await engine.audit.provider_metrics(24),
            emergency_locked=engine.emergency_locked,
            testnet_unprotected=reconciliation.unprotected_symbols
            if reconciliation is not None
            else (),
            user_stream_error=status["user_stream"]["last_error"],
            testnet_broker_missing=(
                engine.mode == TradingMode.TESTNET and engine.testnet_broker is None
            ),
        )
        async with alert_lock:
            transitions = alert_notifier.diff(alerts)
            alert_notifier.emit(transitions)
            for event in transitions:
                await engine.audit.record_alert_event(event)
        return {
            "active_count": len(alerts),
            "alerts": alerts,
            "transitions": transitions,
            "evaluated_at": datetime.now(UTC),
        }

    @app.get("/api/alerts/history")
    async def get_alert_history(limit: int = 100) -> dict[str, Any]:
        if not 1 <= limit <= 500:
            raise HTTPException(status_code=422, detail="limit must be between 1 and 500")
        return {"events": await engine.audit.recent_alert_events(limit)}

    @app.post("/api/history/clear")
    async def clear_history(request: HistoryClearRequest) -> dict[str, Any]:
        db_categories = set(AuditRepository.HISTORY_TABLES)
        valid = db_categories | {"market_cache", "pricing_cache"}
        unknown = sorted(set(request.categories) - valid)
        if unknown:
            raise HTTPException(status_code=422, detail=f"unknown categories: {', '.join(unknown)}")
        selected = set(request.categories)
        cleared: dict[str, int] = {}
        db_selected = selected & db_categories
        if db_selected:
            cleared.update(await engine.audit.clear_history(db_selected))
        if "market_cache" in selected:
            cleared["market_cache"] = await asyncio.to_thread(history_cache.clear)
        if "pricing_cache" in selected:
            cleared["pricing_cache"] = await asyncio.to_thread(
                _delete_pricing_cache, pricing_cache_dir
            )
            async with pricing_lock:
                pricing_memo["catalog"] = None
                pricing_memo["expires_at"] = None
        request_logger.info(
            "history_cleared",
            extra={"structured": {"categories": sorted(selected), "counts": cleared}},
        )
        return {"cleared": cleared}

    @app.post("/api/providers/select")
    async def select_provider(selection: ProviderSelection) -> dict[str, Any]:
        try:
            if selection.providers is not None:
                engine.select_provider_chain(selection.providers)
            elif selection.name is not None:
                engine.select_provider(selection.name, selection.backup)
            else:
                raise ValueError("providers or name is required")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _status(engine, scheduler)

    @app.post("/api/cadences")
    async def select_cadences(selection: CadenceSelection) -> dict[str, Any]:
        try:
            engine.select_cadences(selection.cadences)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _status(engine, scheduler)

    @app.post("/api/candidates-per-cycle")
    async def select_candidates_per_cycle(
        selection: CandidatesPerCycleSelection,
    ) -> dict[str, Any]:
        try:
            scheduler.select_candidates_per_cycle(selection.candidates_per_cycle)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _status(engine, scheduler)

    @app.get("/api/settings")
    async def get_settings() -> dict[str, Any]:
        async with settings_file_lock:
            return describe_settings(env_path, read_env_file(env_path))

    def _stored_custom_providers() -> list[dict[str, Any]]:
        raw = read_env_file(env_path).get(CUSTOM_PROVIDERS_ENV, "").strip()
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []

    @app.get("/api/custom-providers")
    async def get_custom_providers() -> dict[str, Any]:
        async with settings_file_lock:
            stored = _stored_custom_providers()
        providers = []
        for entry in stored:
            key = entry.get("api_key") or ""
            headers = entry.get("extra_headers") or {}
            providers.append(
                {
                    "id": entry.get("id", ""),
                    "base_url": entry.get("base_url", ""),
                    "model": entry.get("model") or "",
                    "reasoning_effort": entry.get("reasoning_effort") or "",
                    "wire_api": entry.get("wire_api") or "chat-completions",
                    "require_api_key": entry.get("require_api_key", True),
                    # Header values are secrets too: expose only their names.
                    "extra_header_names": sorted(headers) if isinstance(headers, dict) else [],
                    "api_key_configured": bool(key),
                    "api_key_masked": mask_secret(key) if key else "",
                }
            )
        return {
            "providers": providers,
            "max_providers": MAX_CUSTOM_LLM_PROVIDERS,
            "wire_apis": sorted(CUSTOM_LLM_WIRE_APIS),
        }

    @app.post("/api/custom-providers")
    async def save_custom_providers(update: CustomProvidersUpdate) -> dict[str, Any]:
        async with settings_file_lock:
            stored = {
                entry.get("id"): entry for entry in _stored_custom_providers()
            }
            entries: list[dict[str, Any]] = []
            for provider in update.providers:
                previous = stored.get(provider.id, {})
                # The provider constructor only records a bad URL as a config
                # error, so validate here to fail the form instead of saving an
                # endpoint that silently reports itself unavailable later.
                try:
                    base_url = validate_base_url(provider.base_url)
                except ValueError as exc:
                    raise HTTPException(
                        status_code=422, detail=f"{provider.id}: {exc}"
                    ) from exc
                entry: dict[str, Any] = {"id": provider.id, "base_url": base_url}
                # Omitted key means "unchanged", so carry the stored one over.
                key = provider.api_key if provider.api_key is not None else previous.get("api_key")
                if key:
                    entry["api_key"] = key
                for field, value in (
                    ("model", provider.model),
                    ("reasoning_effort", provider.reasoning_effort),
                ):
                    if value and value.strip():
                        entry[field] = value.strip()
                entry["wire_api"] = provider.wire_api
                if not provider.require_api_key:
                    entry["require_api_key"] = False
                headers = (
                    provider.extra_headers
                    if provider.extra_headers is not None
                    else previous.get("extra_headers")
                )
                if headers:
                    entry["extra_headers"] = headers
                entries.append(entry)

            serialized = json.dumps(entries, separators=(",", ":")) if entries else ""
            candidate = {**read_env_file(env_path), CUSTOM_PROVIDERS_ENV: serialized}
            try:
                # Reuse the startup parser so the console cannot save a list the
                # next start would reject.
                Settings.from_mapping(candidate)
            except (ValueError, TypeError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            write_env_file(env_path, {CUSTOM_PROVIDERS_ENV: serialized})
        return await get_custom_providers()

    @app.post("/api/restart")
    async def restart_backend() -> dict[str, Any]:
        if engine.running:
            raise HTTPException(
                status_code=409,
                detail="stop the engine before restarting the backend",
            )

        async def _reexec() -> None:
            # Reply first: exec replaces this process, so nothing can be sent after.
            await asyncio.sleep(0.25)
            await database.close()
            argv, environment = restart_command()
            logging.getLogger("candlepilot").info(
                "restarting backend", extra={"argv": argv}
            )
            os.execve(sys.executable, argv, environment)

        asyncio.get_running_loop().create_task(_reexec())
        return {"restarting": True, "env_file": str(env_path)}

    @app.post("/api/settings")
    async def save_settings(update: SettingsUpdate) -> dict[str, Any]:
        unknown = set(update.values) - set(ENV_FIELDS)
        if unknown:
            raise HTTPException(
                status_code=422, detail=f"unknown settings: {', '.join(sorted(unknown))}"
            )
        for key, value in update.values.items():
            if "\n" in value or "\r" in value:
                raise HTTPException(
                    status_code=422, detail=f"{key} must be a single line"
                )
        async with settings_file_lock:
            current = read_env_file(env_path)
            candidate = {**current, **{k: v for k, v in update.values.items() if v != ""}}
            for key, value in update.values.items():
                if value == "":
                    candidate.pop(key, None)
            # Validate the whole candidate with the startup parsers before the
            # file is touched, so a bad value can never brick the next start.
            try:
                candidate_settings = Settings.from_mapping(candidate)
                _validate_startup_settings(candidate_settings)
            except (ValueError, TypeError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            write_env_file(env_path, update.values)
            return describe_settings(env_path, read_env_file(env_path))

    @app.post("/api/run-limits")
    async def select_run_limits(limits: RunLimits) -> dict[str, Any]:
        try:
            engine.select_run_limits(
                max_run_seconds=limits.max_run_seconds,
                max_run_cost_usd=limits.max_run_cost_usd,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _status(engine, scheduler)

    @app.post("/api/engine/start")
    async def start_engine() -> dict[str, Any]:
        try:
            await engine.start()
            scheduler.start()
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _status(engine, scheduler)

    @app.post("/api/engine/stop")
    async def stop_engine() -> dict[str, Any]:
        await scheduler.stop()
        await engine.stop()
        return _status(engine, scheduler)

    @app.post("/api/engine/emergency-stop")
    async def emergency_stop() -> dict[str, Any]:
        await scheduler.stop()
        await engine.emergency_stop()
        return _status(engine, scheduler)

    @app.post("/api/engine/clear-emergency-lock")
    async def clear_emergency_lock() -> dict[str, Any]:
        try:
            await engine.clear_emergency_lock()
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _status(engine, scheduler)

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
        cadence: Literal["1m", "5m", "15m", "30m"],
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
        cadence: Literal["1m", "5m", "15m", "30m"],
        start: datetime,
        end: datetime,
        limit: int = 10_000,
    ) -> list[dict[str, Any]]:
        try:
            return await load_backtest_candles(symbol.upper(), cadence, start, end, limit)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"backtest history failed: {exc}") from exc

    @app.post("/api/universe/refresh")
    async def refresh_universe() -> list[dict[str, Any]]:
        try:
            await engine.refresh_universe()
            await scheduler.sync_market_feed()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"market refresh failed: {exc}") from exc
        return await get_universe()

    @app.get("/api/signals")
    async def get_signals(limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise HTTPException(status_code=422, detail="limit must be between 1 and 500")
        return await engine.audit.recent_intents(limit)

    @app.get("/api/decision-events")
    async def get_decision_events(limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise HTTPException(status_code=422, detail="limit must be between 1 and 500")
        return await engine.audit.recent_decision_events(limit)

    @app.get("/api/decision-events/{inference_id}")
    async def get_decision_detail(inference_id: int) -> dict[str, Any]:
        if inference_id < 1:
            raise HTTPException(status_code=422, detail="inference id must be positive")
        detail = await engine.audit.decision_detail(
            inference_id,
            catalog=await pricing_catalog(),
        )
        if detail is None:
            raise HTTPException(status_code=404, detail="inference not found")
        return detail

    @app.get("/api/testnet/events")
    async def get_testnet_events(limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise HTTPException(status_code=422, detail="limit must be between 1 and 500")
        return await engine.audit.recent_user_events(limit)

    @app.get("/api/testnet/account-status")
    async def get_testnet_account_status() -> dict[str, Any]:
        broker = engine.testnet_broker
        stream_status = _status(engine, scheduler)["user_stream"]
        reconciliation = (
            _json_value(asdict(engine.testnet_reconciliation))
            if engine.testnet_reconciliation is not None
            else None
        )
        if broker is None:
            return {
                "enabled": False,
                "active": False,
                "mode": engine.mode.value,
                "account": None,
                "positions": [],
                "reconciliation": reconciliation,
                "user_stream": stream_status,
                "fetched_at": None,
            }
        try:
            account = await testnet_account()
            positions = [
                {
                    "symbol": item.get("symbol"),
                    "position_amount": str(item.get("positionAmt", "0")),
                    "entry_price": str(item.get("entryPrice", "0")),
                    "mark_price": str(item.get("markPrice", "0")),
                    "unrealized_profit": str(item.get("unrealizedProfit", "0")),
                    "leverage": int(item.get("leverage", 0)),
                    "isolated": item.get("isolated") in {True, "true", "TRUE"},
                }
                for item in account.get("positions", [])
                if Decimal(str(item.get("positionAmt", "0"))) != 0
            ]
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"testnet account query failed: {exc}"
            ) from exc
        return {
            "enabled": True,
            "active": engine.mode == TradingMode.TESTNET,
            "mode": engine.mode.value,
            "account": {
                # The USD-M futures /fapi/v3/account response has no canTrade
                # field (futures permission lives on the API key). Report margin
                # readiness — funds available to open a position — instead.
                "can_trade": Decimal(str(account.get("availableBalance", "0"))) > 0,
                "total_wallet_balance": str(account.get("totalWalletBalance", "0")),
                "total_margin_balance": str(account.get("totalMarginBalance", "0")),
                "available_balance": str(account.get("availableBalance", "0")),
                "total_unrealized_profit": str(account.get("totalUnrealizedProfit", "0")),
                "total_initial_margin": str(account.get("totalInitialMargin", "0")),
            },
            "positions": positions,
            "reconciliation": reconciliation,
            "user_stream": stream_status,
            "fetched_at": datetime.now(UTC),
        }

    @app.get("/api/account/portfolio")
    async def get_account_portfolio() -> dict[str, Any]:
        if engine.mode == TradingMode.TESTNET:
            broker = engine.testnet_broker
            if broker is None:
                raise HTTPException(status_code=503, detail="testnet broker is not configured")
            try:
                account = await testnet_account()
            except Exception as exc:
                raise HTTPException(
                    status_code=502, detail=f"testnet account query failed: {exc}"
                ) from exc
            positions = [
                item
                for item in account.get("positions", [])
                if Decimal(str(item.get("positionAmt", "0"))) != 0
            ]
            return _json_value(
                {
                    "mode": engine.mode.value,
                    "source": "binance-testnet",
                    "initial_equity": None,
                    "cash": str(account.get("totalWalletBalance", "0")),
                    "equity": str(
                        account.get(
                            "totalMarginBalance",
                            account.get("totalWalletBalance", "0"),
                        )
                    ),
                    "available_balance": str(account.get("availableBalance", "0")),
                    "daily_pnl": None,
                    "unrealized_pnl": str(account.get("totalUnrealizedProfit", "0")),
                    "open_positions": len(positions),
                    "margin_used": str(account.get("totalInitialMargin", "0")),
                }
            )

        executor = engine.paper_executor
        state = executor.portfolio_state()
        return _json_value(
            {
                "mode": engine.mode.value,
                "source": "paper",
                "initial_equity": executor.initial_equity,
                "cash": executor.cash,
                "equity": state.equity,
                "available_balance": state.available_balance,
                "daily_pnl": state.daily_pnl,
                "unrealized_pnl": state.equity - executor.cash,
                "open_positions": state.open_positions,
                "margin_used": state.margin_used,
            }
        )

    @app.get("/api/account/positions")
    async def get_account_positions() -> list[dict[str, Any]]:
        if engine.mode == TradingMode.TESTNET:
            broker = engine.testnet_broker
            if broker is None:
                raise HTTPException(status_code=503, detail="testnet broker is not configured")
            try:
                account = await testnet_account()
            except Exception as exc:
                raise HTTPException(
                    status_code=502, detail=f"testnet account query failed: {exc}"
                ) from exc

            positions: list[dict[str, Any]] = []
            reconciliation = engine.testnet_reconciliation
            unprotected = (
                set(reconciliation.unprotected_symbols) if reconciliation is not None else None
            )
            for item in account.get("positions", []):
                amount = Decimal(str(item.get("positionAmt", "0")))
                if amount == 0:
                    continue
                quantity = abs(amount)
                mark_price = Decimal(str(item.get("markPrice", "0")))
                leverage = int(item.get("leverage", 1))
                notional = quantity * mark_price
                margin = item.get("positionInitialMargin", item.get("initialMargin"))
                if margin is None:
                    margin = notional / leverage if leverage > 0 else Decimal("0")
                symbol = str(item.get("symbol", ""))
                protection_status = (
                    "unknown"
                    if unprotected is None
                    else "missing"
                    if symbol in unprotected
                    else "exchange"
                )
                positions.append(
                    {
                        "symbol": symbol,
                        "side": "LONG" if amount > 0 else "SHORT",
                        "quantity": str(quantity),
                        "average_price": str(item.get("entryPrice", "0")),
                        "mark_price": str(mark_price),
                        "leverage": leverage,
                        "unrealized_pnl": str(item.get("unrealizedProfit", "0")),
                        "notional": str(notional),
                        "margin_used": str(margin),
                        "stop_loss": None,
                        "take_profit": None,
                        "protection_source": protection_status,
                    }
                )
            return [_json_value(item) for item in positions]

        return [_json_value(item) for item in engine.paper_executor.position_snapshots()]

    @app.get("/api/orders")
    async def get_orders(limit: int = 100, status: str | None = None) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise HTTPException(status_code=422, detail="limit must be between 1 and 500")
        return await engine.audit.recent_executions(limit, status=status)

    @app.get("/api/fills")
    async def get_fills(limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise HTTPException(status_code=422, detail="limit must be between 1 and 500")
        return await engine.audit.recent_executions(limit, status="FILLED")

    @app.get("/api/risk-events")
    async def get_risk_events(
        limit: int = 100, accepted: bool | None = None
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise HTTPException(status_code=422, detail="limit must be between 1 and 500")
        return await engine.audit.recent_risk_decisions(limit, accepted=accepted)

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
            "execution": outcome.execution.model_dump(mode="json") if outcome.execution else None,
        }

    @app.get("/api/backtests")
    async def get_backtests(limit: int = 20) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise HTTPException(status_code=422, detail="limit must be between 1 and 100")
        return await engine.audit.recent_backtests(limit)

    @app.get("/api/backtests/{backtest_id}")
    async def get_backtest(backtest_id: int) -> dict[str, Any]:
        if backtest_id < 1:
            raise HTTPException(status_code=422, detail="backtest id must be positive")
        result = await engine.audit.backtest(backtest_id)
        if result is None:
            raise HTTPException(status_code=404, detail="backtest not found")
        return result

    @app.post("/api/backtests", status_code=201)
    async def run_backtest(request: BacktestRunRequest) -> dict[str, Any]:
        mismatched = [
            replay
            for replay in request.decisions
            if replay.intent.symbol != request.symbol or replay.intent.cadence != request.cadence
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
        serialized = _json_value(asdict(result))
        serialized["provenance"] = _backtest_provenance(candles)
        return await engine.audit.record_backtest(
            request.symbol,
            request.cadence,
            serialized,
        )

    @app.post("/api/backtests/replay", status_code=201)
    async def replay_cached_backtest(request: BacktestReplayRequest) -> dict[str, Any]:
        symbol = request.symbol.upper()
        try:
            candle_payloads, records = await asyncio.gather(
                load_backtest_candles(
                    symbol, request.cadence, request.start, request.end, request.limit
                ),
                engine.audit.intents_between(symbol, request.cadence, request.start, request.end),
            )
            candles = [_candle_from_payload(payload) for payload in candle_payloads]
            decisions = align_cached_intents(
                records,
                request.cadence,
                {candle.timestamp for candle in candles},
            )
            if not decisions:
                raise HTTPException(
                    status_code=409,
                    detail="no cached LLM decisions match this symbol, cadence, and range",
                )
            result = BacktestEngine(BacktestConfig(**request.config.model_dump())).run(
                candles, decisions
            )
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"cached replay failed: {exc}") from exc
        serialized = _json_value(asdict(result))
        serialized["provenance"] = _backtest_provenance(
            candles,
            prompt_versions=[item["provenance"].get("prompt_version") for item in records],
            models=[item.get("model") for item in records],
            provider_versions=[item["provenance"].get("provider_version") for item in records],
        )
        serialized["replay"] = {
            "source": "cached_llm_decisions",
            "decision_count": len(decisions),
            "start": request.start.isoformat(),
            "end": request.end.isoformat(),
        }
        return await engine.audit.record_backtest(symbol, request.cadence, serialized)

    @app.post("/api/backtests/llm", status_code=201)
    async def run_fresh_llm_backtest(request: BacktestLLMRequest) -> dict[str, Any]:
        symbol = request.symbol.upper()
        try:
            provider = engine.providers.get(request.provider)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        health = await provider.health_check()
        if not health.available or not health.authenticated:
            raise HTTPException(status_code=409, detail=f"provider is unavailable: {health.detail}")
        config = BacktestConfig(**request.config.model_dump())
        try:
            candle_payloads = await load_backtest_candles(
                symbol, request.cadence, request.start, request.end, request.limit
            )
            candles = [_candle_from_payload(payload) for payload in candle_payloads]
            decisions, provider_results = await generate_fresh_intents(
                provider,
                candles,
                symbol=symbol,
                cadence=request.cadence,
                config=config,
                max_calls=request.max_calls,
            )
            if not decisions:
                raise HTTPException(
                    status_code=409,
                    detail="fresh LLM replay requires at least 20 historical candles",
                )
            inference_ids = [
                await engine.audit.record_inference(result) for result in provider_results
            ]
            result = BacktestEngine(config).run(candles, decisions)
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"fresh LLM replay failed: {exc}") from exc
        serialized = _json_value(asdict(result))
        serialized["provenance"] = _backtest_provenance(
            candles,
            prompt_versions=[item.prompt_version for item in provider_results],
            models=[item.model for item in provider_results],
            provider_versions=[item.provider_version for item in provider_results],
        )
        serialized["replay"] = {
            "source": "fresh_llm_calls",
            "provider": request.provider,
            "models": sorted({item.model for item in provider_results if item.model}),
            "decision_count": len(decisions),
            "inference_ids": inference_ids,
            "portfolio_context": "fixed_initial_equity",
            "start": request.start.isoformat(),
            "end": request.end.isoformat(),
        }
        return await engine.audit.record_backtest(symbol, request.cadence, serialized)

    @app.post("/api/backtests/portfolio", status_code=201)
    async def run_portfolio_backtest(request: PortfolioBacktestRequest) -> dict[str, Any]:
        symbols = [leg.symbol for leg in request.legs]
        if len(set(symbols)) != len(symbols):
            raise HTTPException(status_code=422, detail="portfolio symbols must be unique")
        mismatched = [
            decision
            for leg in request.legs
            for decision in leg.decisions
            if decision.intent.symbol != leg.symbol or decision.intent.cadence != leg.cadence
        ]
        if mismatched:
            raise HTTPException(
                status_code=422,
                detail="all portfolio intents must match their leg symbol and cadence",
            )
        legs = {
            leg.symbol: (
                [Candle(**item.model_dump()) for item in leg.candles],
                [ReplayIntent(item.decided_at, item.intent) for item in leg.decisions],
            )
            for leg in request.legs
        }
        try:
            result = PortfolioBacktestEngine(BacktestConfig(**request.config.model_dump())).run(
                legs
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        serialized = _json_value(asdict(result))
        serialized["provenance"] = {
            "data_version": content_fingerprint(
                legs,
                schema_version=BACKTEST_DATA_SCHEMA_VERSION,
            ),
            "per_symbol_data_versions": {
                symbol: _backtest_provenance(candles)["data_version"]
                for symbol, (candles, _) in legs.items()
            },
            "prompt_versions": [],
            "models": [],
            "provider_versions": [],
        }
        serialized["symbols"] = symbols
        serialized["cadences"] = {leg.symbol: leg.cadence for leg in request.legs}
        return await engine.audit.record_backtest("PORTFOLIO", "mixed", serialized)

    @app.websocket("/ws/events")
    async def event_stream(websocket: WebSocket) -> None:
        await websocket.accept()
        last_decisions: list[dict[str, Any]] | None = None
        try:
            while True:
                await websocket.send_json({"type": "status", "data": _status(engine, scheduler)})
                decisions = await engine.audit.recent_decision_events(50)
                if decisions != last_decisions:
                    await websocket.send_json({"type": "decisions", "data": _json_value(decisions)})
                    last_decisions = decisions
                await asyncio.sleep(2)
        except (WebSocketDisconnect, RuntimeError):
            return

    frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if frontend_dist.is_dir():
        app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="console")

    return app


app = create_app()
