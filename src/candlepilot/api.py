from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from uuid import uuid4
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from candlepilot.application.engine import TradingEngine
from candlepilot.application.scheduler import (
    MAX_CANDIDATES_PER_CYCLE,
    TradingScheduler,
)
from candlepilot.application.testnet_feed import TestnetUserFeed
from candlepilot.backtest.engine import BacktestConfig, Candle, SimulatedExchange
from candlepilot.backtest.probe import (
    MAX_SUGGESTED_TIMEOUT,
    PROBE_CEILING_SECONDS,
    PROBE_DECISIONS,
    ProviderProbe,
    probe_provider,
)
from candlepilot.backtest.runner import (
    MAX_BACKTEST_MODELS,
    MAX_BACKTEST_SYMBOLS,
    MAX_ESTIMATED_HOURS,
    MAX_FAILURE_RATE,
    BacktestDecision,
    BacktestRunner,
    BacktestSpec,
    ModelRun,
    compare,
    estimate,
    unreliable_models,
    validate,
)
from candlepilot.backtest.snapshots import (
    INTERVAL_MILLISECONDS as BACKTEST_INTERVALS,
)
from candlepilot.backtest.snapshots import HistoricalSnapshotBuilder
from candlepilot.backtest.snapshots import (
    coverage,
    required_history_start,
)
from candlepilot.broker.binance_testnet import (
    BinanceTestnetBroker,
    BinanceTestnetCredentials,
    ProtectiveLevels,
)
from candlepilot.broker.user_stream import BinanceTestnetUserStream
from candlepilot.config import (
    CUSTOM_PROVIDER_PREFIX,
    CUSTOM_LLM_WIRE_APIS,
    DOTENV_INJECTED_KEYS,
    ENV_FILE_VARIABLE,
    MAX_CUSTOM_LLM_PROVIDERS,
    Settings,
    validate_provider_references,
)
from candlepilot.domain.models import (
    SUPPORTED_CADENCES,
    MarketSnapshot,
    PortfolioState,
)
from candlepilot.market.binance import BinancePublicClient
from candlepilot.market.cache import HistoricalMarketCache
from candlepilot.market.collector import (
    MAX_COLLECTED_SYMBOLS,
    BookCollector,
    aligned_capture_times,
)
from candlepilot.market.history import build_backtest_candles
from candlepilot.observability import AlertNotifier, OperationalMetrics, evaluate_alerts
from candlepilot.providers.pricing import (
    CACHE_FILENAME as PRICING_CACHE_FILENAME,
)
from candlepilot.providers.pricing import PROVIDER_IDS, ModelPricingCatalog
from candlepilot.providers.pricing import load_catalog as load_pricing_catalog
from candlepilot.providers.base import LLMProvider
from candlepilot.providers.openai_compatible import validate_base_url
from candlepilot.providers.registry import ProviderRegistry
from candlepilot.provenance import MICROSTRUCTURE_SCHEMA_VERSION
from candlepilot.risk.engine import AggressiveRiskPolicy, SymbolRules
from candlepilot.settings_file import (
    CUSTOM_PROVIDERS_ENV,
    ENV_FIELDS,
    describe_settings,
    mask_secret,
    read_env_file,
    write_env_file,
)
from candlepilot.storage.database import (
    AuditRepository,
    CURRENT_SCHEMA_VERSION,
    DECISION_OUTCOMES,
    Database,
)


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
    pricing: str | None = None


class ProviderTestRequest(ApiModel):
    name: str


class CustomProviderInput(ApiModel):
    id: str
    base_url: str
    model: str | None = None
    reasoning_effort: str | None = None
    wire_api: str = "chat-completions"
    require_api_key: bool = True
    pricing: str | None = None
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


class BacktestConfigInput(ApiModel):
    initial_equity: Annotated[Decimal, Field(gt=0)] = Decimal("10000")
    fee_rate: Annotated[Decimal, Field(ge=0, le=1)] = Decimal("0.0005")
    slippage_fraction: Annotated[Decimal, Field(ge=0, le=1)] = Decimal("0.0005")


class BacktestRequest(ApiModel):
    symbols: list[str] = Field(min_length=1, max_length=MAX_BACKTEST_SYMBOLS)
    cadences: list[str] = Field(default=["5m"], min_length=1, max_length=3)
    start: datetime
    end: datetime
    providers: list[str] = Field(min_length=1, max_length=MAX_BACKTEST_MODELS)
    config: BacktestConfigInput = Field(default_factory=BacktestConfigInput)
    # Only possible over a window the collector covered; the coverage is
    # checked up front rather than degrading decision by decision.
    use_recorded_book: bool = False
    # Set from a probe of these providers. None keeps the configured default,
    # which is one number for endpoints that differ by minutes.
    timeout_seconds: float | None = Field(default=None, gt=0, le=MAX_SUGGESTED_TIMEOUT)


class CollectorStart(ApiModel):
    symbols: list[str] = Field(min_length=1, max_length=MAX_COLLECTED_SYMBOLS)


class SymbolRulesInput(ApiModel):
    quantity_step: Annotated[Decimal, Field(gt=0)]
    min_quantity: Annotated[Decimal, Field(gt=0)]
    min_notional: Annotated[Decimal, Field(gt=0)]
    tick_size: Annotated[Decimal, Field(gt=0)]


class DecisionRequest(ApiModel):
    snapshot: MarketSnapshot
    portfolio: PortfolioState
    rules: SymbolRulesInput










# CLI-accepted aliases that are not published as models.dev ids.
_CURATED_MODEL_ALIASES: dict[str, tuple[str, ...]] = {
    "claude-code-auth": ("sonnet", "opus", "haiku", "fable"),
}
_MODEL_ID_PREFIX: dict[str, str] = {"openai": "gpt-5", "anthropic": "claude-"}

def _capture_features(row: dict[str, Any]) -> dict[str, float]:
    """Rebuild the microstructure block from a stored capture.

    The book is recomputed from the raw depth that was kept, so a change to that
    formula is picked up here for free. The tape summary can only be restored,
    which is what MICROSTRUCTURE_SCHEMA_VERSION guards.
    """

    from candlepilot.market.features import FeaturePipeline

    derived = FeaturePipeline.microstructure(
        mark_price=Decimal(row["mark_price"]),
        index_price=Decimal(row["index_price"]),
        open_interest=Decimal(row["open_interest"]),
        bids=row["depth"]["bids"],
        asks=row["depth"]["asks"],
        trades=[],
    )
    derived["recent_trade_imbalance"] = float(row["trade_imbalance"])
    derived["recent_trade_seconds"] = float(row["trade_seconds"])
    return derived


def pricing_provider_ids(settings: Settings) -> dict[str, str]:
    """Map each configured provider to the models.dev provider it bills as.

    The CLIs are fixed; a custom endpoint is only in here if its config says so.
    It cannot be derived: the same model is resold by a dozen models.dev
    providers at rates that genuinely differ, and an OpenAI-compatible endpoint
    is exactly the aggregator case, so neither the model name nor the base URL
    settles whose price applies. Absent from this map means cost stays unknown,
    which is the honest answer rather than a plausible wrong number.
    """

    identifiers = dict(PROVIDER_IDS)
    for provider in settings.custom_llm_providers:
        if provider.pricing:
            identifiers[provider.provider_name] = provider.pricing
    return identifiers


def _model_options(
    provider_name: str,
    catalog: ModelPricingCatalog | None,
    current: str | None,
    provider_ids: Mapping[str, str] | None = None,
) -> list[str]:
    options: list[str] = list(_CURATED_MODEL_ALIASES.get(provider_name, ()))
    provider_id = (provider_ids or PROVIDER_IDS).get(provider_name)
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


def _pricing_options(catalog: ModelPricingCatalog | None) -> list[str]:
    if catalog is None:
        return []
    return sorted({provider for provider, _ in catalog.prices})


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

    ``Settings`` parsing is lenient about the remaining keys here — the engine
    and scheduler do those range checks at construction. Saving such a value
    would brick the next start, so mirror those checks here. Cadences are not
    among them: ``_parse_cadences`` validates them, so a bad one never reaches
    this function.
    """

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


def _status(engine: TradingEngine, scheduler: TradingScheduler | None = None) -> dict[str, Any]:
    testnet_feed = scheduler.testnet_feed if scheduler is not None else None
    return {
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
    provider_pricing_ids = pricing_provider_ids(settings)
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
        testnet_stream = BinanceTestnetUserStream(credentials)
    elif engine is None:
        # Binance testnet is the only account this system trades. There is no
        # simulated fallback to quietly drop into, so say what is missing rather
        # than starting something that cannot trade.
        raise RuntimeError(
            "BINANCE_TESTNET_API_KEY and BINANCE_TESTNET_API_SECRET are required; "
            "create a key at https://testnet.binancefuture.com and put it in .env"
        )
    engine = engine or TradingEngine(
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
        ),
        testnet_broker=testnet_broker,
        cadences=settings.cadences,
    )
    validate_provider_references(settings, engine.providers.names)
    if settings.provider_chain and not engine.provider_chain:
        engine.select_provider_chain(settings.provider_chain)
    if settings.default_provider is not None and engine.selected_provider is None:
        engine.select_provider(settings.default_provider)
    if settings.max_run_seconds is not None or settings.max_run_cost_usd is not None:
        engine.select_run_limits(
            max_run_seconds=settings.max_run_seconds,
            max_run_cost_usd=settings.max_run_cost_usd,
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
            provider_ids=provider_pricing_ids,
        )
        return metrics.get("equivalent_cost_usd")

    scheduler = TradingScheduler(
        engine,
        market,
        candidates_per_cycle=settings.candidates_per_cycle,
        run_cost_loader=current_run_cost_usd,
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
    testnet_levels_lock = asyncio.Lock()
    testnet_levels_memo: dict[str, Any] = {"levels": None, "expires_at": 0.0}

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

    async def testnet_protective_levels() -> dict[str, ProtectiveLevels]:
        broker = engine.testnet_broker
        if broker is None:
            raise RuntimeError("testnet broker is not configured")
        now = time.monotonic()
        if testnet_levels_memo["levels"] is not None:
            if now < testnet_levels_memo["expires_at"]:
                return testnet_levels_memo["levels"]
        async with testnet_levels_lock:
            now = time.monotonic()
            if testnet_levels_memo["levels"] is not None:
                if now < testnet_levels_memo["expires_at"]:
                    return testnet_levels_memo["levels"]
            levels = await broker.protective_levels()
            testnet_levels_memo["levels"] = levels
            testnet_levels_memo["expires_at"] = time.monotonic() + 1.0
            return levels

    async def testnet_daily_income() -> Decimal:
        broker = engine.testnet_broker
        if broker is None:
            raise RuntimeError("testnet broker is not configured")
        loader = getattr(broker, "daily_income", None)
        return await loader() if callable(loader) else Decimal("0")

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
        await engine.stop()
        await collector.stop()
        model_tasks = active_backtest_tasks()
        if probe_task is not None and not probe_task.done():
            probe_task.cancel()
            model_tasks.append(probe_task)
        for task in model_tasks:
            task.cancel()
        if model_tasks:
            # Backtests write their cancelled terminal state while the audit
            # database is still open; Provider cancellation also gets a chance
            # to terminate its CLI process/HTTP request before resources close.
            await asyncio.gather(*model_tasks, return_exceptions=True)
        if owns_market:
            await market.close()
        if testnet_broker is not None:
            await testnet_broker.close()
        if testnet_feed is not None:
            await testnet_feed.close()
        if owns_database:
            await database.close()

    async def _store_captures(captures: list[Any]) -> None:
        await engine.audit.store_book_captures(
            [
                {
                    "symbol": item.symbol,
                    "captured_at": item.captured_at,
                    "schema_version": item.schema_version,
                    "payload": {
                        "bid": str(item.bid),
                        "ask": str(item.ask),
                        "mark_price": str(item.mark_price),
                        "index_price": str(item.index_price),
                        "funding_rate": str(item.funding_rate),
                        "depth": item.depth,
                        "trade_imbalance": item.trade_imbalance,
                        "trade_seconds": item.trade_seconds,
                        "open_interest": str(item.open_interest),
                    },
                }
                for item in captures
            ]
        )

    collector = BookCollector(market, store=_store_captures)

    app = FastAPI(
        title="CandlePilot API",
        version="0.1.0",
        description="Local-only API for Binance testnet trading and historical backtests",
        lifespan=lifespan,
    )
    app.state.engine = engine
    app.state.database = database
    app.state.collector = collector
    app.state.scheduler = scheduler
    app.state.history_cache = history_cache
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
        # No broker check: it is a required constructor argument and create_app
        # refuses to build without credentials, so an engine without one cannot
        # reach this endpoint.
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
            custom = item.provider.startswith(CUSTOM_PROVIDER_PREFIX)
            result.append(
                {
                    **item.model_dump(mode="json"),
                    "capabilities": asdict(provider.capabilities),
                    "model": provider.model,
                    "reasoning_effort": provider.reasoning_effort,
                    "reasoning_effort_options": list(provider.reasoning_effort_options),
                    "pricing": provider_pricing_ids.get(item.provider) if custom else None,
                    "pricing_options": _pricing_options(catalog) if custom else [],
                    "model_options": _model_options(
                        item.provider, catalog, provider.model, provider_pricing_ids
                    ),
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
        if background_model_work():
            raise HTTPException(
                status_code=409,
                detail="cannot change model settings while a probe or backtest runs",
            )
        model = (config.model or "").strip() or None
        effort = (config.reasoning_effort or "").strip() or None
        pricing_supplied = "pricing" in config.model_fields_set
        pricing = (config.pricing or "").strip() or None
        if pricing_supplied and not config.name.startswith(CUSTOM_PROVIDER_PREFIX):
            raise HTTPException(
                status_code=422,
                detail="pricing provider can only be changed for a Custom API",
            )
        if effort is not None and effort not in provider.reasoning_effort_options:
            raise HTTPException(
                status_code=422,
                detail=f"unsupported reasoning effort for {config.name}: {effort}",
            )
        provider.model = model
        provider.reasoning_effort = effort
        if pricing_supplied:
            if pricing is None:
                provider_pricing_ids.pop(config.name, None)
            else:
                provider_pricing_ids[config.name] = pricing
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
        if background_model_work():
            raise HTTPException(
                status_code=409, detail="cannot test a provider while a probe or backtest runs"
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
            "providers": await engine.audit.provider_metrics(
                hours, catalog=catalog, provider_ids=provider_pricing_ids
            ),
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
            provider_ids=provider_pricing_ids,
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
                engine.testnet_broker is None
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
        catalog = await pricing_catalog()
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
                    "pricing": entry.get("pricing") or "",
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
            "pricing_options": _pricing_options(catalog),
        }

    @app.get("/api/custom-providers/{provider_id}/api-key")
    async def reveal_custom_provider_api_key(
        provider_id: str, response: Response
    ) -> dict[str, str]:
        """Return one stored key only after an explicit local UI request."""

        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        async with settings_file_lock:
            stored = _stored_custom_providers()
        entry = next((item for item in stored if item.get("id") == provider_id), None)
        if entry is None:
            raise HTTPException(status_code=404, detail="custom provider not found")
        key = entry.get("api_key")
        if not isinstance(key, str) or not key:
            raise HTTPException(status_code=404, detail="custom provider has no API key")
        return {"api_key": key}

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
                    ("pricing", provider.pricing),
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
        if background_model_work():
            raise HTTPException(
                status_code=409,
                detail="cannot start the engine while a probe or backtest runs",
            )
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
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"market refresh failed: {exc}") from exc
        return await get_universe()

    @app.get("/api/signals")
    async def get_signals(limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise HTTPException(status_code=422, detail="limit must be between 1 and 500")
        return await engine.audit.recent_intents(limit)

    @app.get("/api/decision-events")
    async def get_decision_events(
        limit: int = 100,
        before_id: int | None = None,
        symbol: str | None = None,
        cadence: str | None = None,
        provider: str | None = None,
        outcome: str | None = None,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise HTTPException(status_code=422, detail="limit must be between 1 and 500")
        if before_id is not None and before_id < 1:
            raise HTTPException(status_code=422, detail="before_id must be positive")
        # An unknown filter value would otherwise return an empty page, which
        # reads as "no such decisions" rather than "you asked for nonsense".
        if outcome is not None and outcome not in DECISION_OUTCOMES:
            raise HTTPException(
                status_code=422,
                detail=f"outcome must be one of {', '.join(sorted(DECISION_OUTCOMES))}",
            )
        if cadence is not None and cadence not in SUPPORTED_CADENCES:
            raise HTTPException(
                status_code=422,
                detail=f"cadence must be one of {', '.join(SUPPORTED_CADENCES)}",
            )
        return await engine.audit.recent_decision_events(
            limit,
            before_id=before_id,
            symbol=symbol,
            cadence=cadence,
            provider=provider,
            outcome=outcome,
        )

    @app.get("/api/decision-events/{inference_id}")
    async def get_decision_detail(inference_id: int) -> dict[str, Any]:
        if inference_id < 1:
            raise HTTPException(status_code=422, detail="inference id must be positive")
        detail = await engine.audit.decision_detail(
            inference_id,
            catalog=await pricing_catalog(),
            provider_ids=provider_pricing_ids,
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
            "active": True,
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
        try:
            account, realized_today = await asyncio.gather(
                testnet_account(), testnet_daily_income()
            )
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
                "daily_pnl": str(
                    Decimal(str(realized_today))
                    + Decimal(str(account.get("totalUnrealizedProfit", "0")))
                ),
                "unrealized_pnl": str(account.get("totalUnrealizedProfit", "0")),
                "open_positions": len(positions),
                "margin_used": str(account.get("totalInitialMargin", "0")),
            }
        )

    @app.get("/api/account/positions")
    async def get_account_positions() -> list[dict[str, Any]]:
        try:
            account, levels = await asyncio.gather(
                testnet_account(), testnet_protective_levels()
            )
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
            # protection_status stays the reconciliation signal: it also
            # counts reduce-only stops, which the level read deliberately
            # ignores. The prices are the live triggers of our own brackets.
            guard = levels.get(symbol, ProtectiveLevels())
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
                    "stop_loss": guard.stop_loss,
                    "take_profit": guard.take_profit,
                    "protection_source": protection_status,
                }
            )
        return [_json_value(item) for item in positions]

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
            request.rules.tick_size,
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


    backtest_tasks: dict[int, asyncio.Task[None]] = {}
    # Probes are pre-flight, not history: they describe the endpoint as it is
    # right now, so they live with the process rather than in the database.
    probes: dict[str, ProviderProbe] = {}
    probe_task: asyncio.Task[None] | None = None

    def active_backtest_tasks() -> list[asyncio.Task[None]]:
        return [task for task in backtest_tasks.values() if not task.done()]

    def background_model_work() -> bool:
        return bool(active_backtest_tasks() or (probe_task and not probe_task.done()))

    def _set_probe_task(task: asyncio.Task[None]) -> None:
        nonlocal probe_task
        probe_task = task

    async def measured_seconds_per_call() -> float:
        """Estimate from this install's own latency, not a guess.

        A backtest's cost is dominated by how slow the configured models
        actually are here, which the audit log already knows.
        """

        metrics = await engine.audit.provider_metrics(24 * 7)
        durations = [
            item["average_duration_ms"] for item in metrics if item["average_duration_ms"] > 0
        ]
        if not durations:
            return 25.0
        return sum(durations) / len(durations) / 1000

    def _spec_from(request: BacktestRequest) -> BacktestSpec:
        return BacktestSpec(
            symbols=tuple(symbol.upper() for symbol in request.symbols),
            cadences=tuple(request.cadences),
            start=request.start,
            end=request.end,
            providers=tuple(request.providers),
            config=BacktestConfig(**request.config.model_dump()),
            use_recorded_book=request.use_recorded_book,
            timeout_seconds=request.timeout_seconds,
        )

    async def _checked_spec(request: BacktestRequest) -> BacktestSpec:
        spec = _spec_from(request)
        unsupported = set(spec.cadences) - set(SUPPORTED_CADENCES)
        if unsupported:
            raise HTTPException(
                status_code=422,
                detail=f"unsupported cadences: {', '.join(sorted(unsupported))}",
            )
        try:
            validate(spec)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        for provider in spec.providers:
            try:
                engine.providers.get(provider)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
        return spec

    @app.post("/api/backtests/estimate")
    async def estimate_backtest(request: BacktestRequest) -> dict[str, Any]:
        spec = await _checked_spec(request)
        result = estimate(spec, seconds_per_call=await measured_seconds_per_call())
        return {
            **result.as_dict(),
            "max_hours": MAX_ESTIMATED_HOURS,
            "within_limit": result.estimated_seconds <= MAX_ESTIMATED_HOURS * 3600,
        }

    async def _load_series(
        spec: BacktestSpec,
    ) -> tuple[dict[str, dict[str, list[Candle]]], dict[str, SymbolRules]]:
        contracts = await market.exchange_info()
        history_start = required_history_start(spec.start, spec.cadences[0])
        series: dict[str, dict[str, list[Candle]]] = {}
        rules: dict[str, SymbolRules] = {}
        for symbol in spec.symbols:
            contract = contracts.get(symbol)
            if contract is None:
                raise HTTPException(status_code=404, detail=f"unknown symbol: {symbol}")
            rules[symbol] = contract.rules
            by_interval: dict[str, list[Candle]] = {}
            for interval in BACKTEST_INTERVALS:
                payloads = await load_backtest_candles(
                    symbol, interval, history_start, spec.end, 100_000
                )
                by_interval[interval] = [
                    Candle(
                        timestamp=item["timestamp"],
                        open=Decimal(item["open"]),
                        high=Decimal(item["high"]),
                        low=Decimal(item["low"]),
                        close=Decimal(item["close"]),
                        volume=Decimal(item["volume"]),
                        funding_rate=Decimal(item.get("funding_rate", "0")),
                    )
                    for item in payloads
                ]
            series[symbol] = by_interval
        return series, rules

    @contextmanager
    def _timeouts(spec: BacktestSpec) -> Iterator[None]:
        """Apply the run's timeout to its providers, then put them back.

        The registry's providers are shared with the live engine, so the
        override cannot outlive the run -- including when the run raises or is
        cancelled, which is exactly when a leaked timeout would go unnoticed.
        """

        if spec.timeout_seconds is None:
            yield
            return
        restore: list[tuple[LLMProvider, float]] = []
        try:
            for name in spec.providers:
                provider = engine.providers.get(name)
                restore.append((provider, provider.timeout))
                provider.timeout = spec.timeout_seconds
            yield
        finally:
            for provider, previous in restore:
                provider.timeout = previous

    async def _run_backtest(
        run_id: int,
        spec: BacktestSpec,
        captures: dict[str, dict[datetime, dict[str, Any]]],
    ) -> None:
        try:
            series, rules = await _load_series(spec)
        except Exception as exc:  # noqa: BLE001 - surface the reason on the run
            await engine.audit.finish_backtest_run(
                run_id, status="failed", error=f"history load failed: {exc}"[:500]
            )
            return

        async def flush(run: ModelRun, decision: BacktestDecision | None) -> None:
            if decision is not None:
                row = decision.as_row()
                fill = row.pop("fill")
                row["fill_json"] = json.dumps(fill) if fill else None
                # A model call takes seconds; this local write takes
                # milliseconds. Persist the complete row now so an expanded
                # running backtest can show it on the next three-second poll.
                await engine.audit.record_backtest_decisions(
                    run_id, run.provider, [row]
                )
            await engine.audit.update_backtest_progress(
                run_id,
                run.provider,
                decisions_done=run.decisions_done,
                decisions_total=run.decisions_total,
                calls_failed=run.calls_failed,
                result=_json_value(asdict(run.result)) if run.result is not None else None,
                error=run.error,
            )
        try:
            with _timeouts(spec):
                runs = await compare(
                    spec=spec,
                    runner_for=lambda _: BacktestRunner(
                        spec=spec,
                        series=series,
                        rules=rules,
                        risk=engine.risk,
                        captures=captures,
                    ),
                    provider_for=engine.providers.get,
                    on_progress=flush,
                )
            # A run that lost decisions did not measure the window it claims to,
            # so it must not be filed next to one that did.
            degraded = unreliable_models(runs)
            if degraded:
                await engine.audit.finish_backtest_run(
                    run_id,
                    status="unreliable",
                    error="; ".join(
                        f"{run.provider} lost {run.calls_failed} of "
                        f"{run.decisions_done} decisions "
                        f"({run.failure_rate:.0%}, limit {MAX_FAILURE_RATE:.0%})"
                        for run in degraded
                    )[:500],
                )
            else:
                await engine.audit.finish_backtest_run(run_id, status="completed")
        except asyncio.CancelledError:
            await engine.audit.finish_backtest_run(run_id, status="cancelled")
            raise
        except Exception as exc:  # noqa: BLE001
            await engine.audit.finish_backtest_run(run_id, status="failed", error=str(exc)[:500])
        finally:
            backtest_tasks.pop(run_id, None)

    async def _run_probe(spec: BacktestSpec) -> None:
        # Every provider is published before anything is awaited, and each is
        # filled in as its calls land. A probe that only appears once it has
        # finished is indistinguishable from a hung one for as long as it takes
        # -- and at the ceiling, three calls is nine minutes.
        for name in spec.providers:
            probes[name] = ProviderProbe(provider=name)

        def fail_all(reason: str) -> None:
            for name in spec.providers:
                probes[name].error = reason[:200]
                probes[name].done = True

        try:
            series, _rules = await _load_series(spec)
        except asyncio.CancelledError:
            fail_all("cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 - surface it on every provider
            fail_all(f"history load failed: {exc}")
            return

        symbol = spec.symbols[0]
        builder = HistoricalSnapshotBuilder(series[symbol])
        portfolio = SimulatedExchange(spec.config).portfolio_state({})
        for name in spec.providers:
            probe = probes[name]
            try:
                await probe_provider(
                    engine.providers.get(name),
                    spec=spec,
                    builder=builder,
                    symbol=symbol,
                    portfolio=portfolio,
                    into=probe,
                )
            except asyncio.CancelledError:
                # This one was interrupted and the ones after it never started.
                # Marked unconditionally: probe_provider's `finally` has already
                # set done on the way out, so keying off that would leave a
                # cancelled probe looking like one that simply finished early.
                for pending in spec.providers[spec.providers.index(name) :]:
                    probes[pending].error = "cancelled"
                    probes[pending].done = True
                raise
            except Exception as exc:  # noqa: BLE001 - one endpoint, not the set
                probe.error = str(exc)[:200]
                probe.done = True

    @app.post("/api/backtests/probe", status_code=202)
    async def start_probe(request: BacktestRequest) -> dict[str, Any]:
        """Time PROBE_DECISIONS real calls per provider before a run is paid for.

        These are real inferences against the real endpoint on the real payload,
        so they cost what they cost; there is no cheaper way to learn what the
        timeout should be than to watch the endpoint answer.
        """

        if engine.running:
            raise HTTPException(
                status_code=409,
                detail="stop the engine before probing: it shares the same provider",
            )
        if probe_task and not probe_task.done():
            raise HTTPException(status_code=409, detail="a probe is already running")
        if active_backtest_tasks():
            raise HTTPException(status_code=409, detail="a backtest is already running")
        spec = await _checked_spec(request)
        for name in spec.providers:
            probes.pop(name, None)
        _set_probe_task(
            asyncio.create_task(_run_probe(spec), name="candlepilot-backtest-probe")
        )
        return {"providers": list(spec.providers), "decisions": PROBE_DECISIONS}

    @app.get("/api/backtests/probe")
    async def read_probe() -> dict[str, Any]:
        return {
            "running": bool(probe_task and not probe_task.done()),
            "decisions": PROBE_DECISIONS,
            "ceiling_seconds": PROBE_CEILING_SECONDS,
            "providers": [
                {
                    "provider": item.provider,
                    "error": item.error,
                    "failures": item.failures,
                    "done": item.done,
                    # How long the call in flight has been waiting. The only
                    # thing that moves while a slow endpoint thinks, and so the
                    # only thing that says the probe is alive.
                    "in_flight_seconds": (
                        round(item.in_flight_seconds, 1)
                        if item.in_flight_seconds is not None
                        else None
                    ),
                    "calls": [
                        {"seconds": round(call.seconds, 1), "ok": call.ok, "error": call.error}
                        for call in item.calls
                    ],
                    "slowest_ok_seconds": (
                        round(item.slowest_ok_seconds, 1)
                        if item.slowest_ok_seconds is not None
                        else None
                    ),
                    "suggested_timeout_seconds": item.suggested_timeout_seconds,
                }
                for item in probes.values()
            ],
        }

    @app.post("/api/backtests/probe/cancel")
    async def cancel_probe() -> dict[str, Any]:
        """Stop waiting on an endpoint that is not going to answer.

        Three calls at the ceiling is nine minutes; without this the only way
        out of a probe against a dead endpoint is to restart the backend.
        """

        if probe_task is None or probe_task.done():
            raise HTTPException(status_code=409, detail="no probe is running")
        probe_task.cancel()
        return {"cancelled": True}

    @app.post("/api/backtests", status_code=202)
    async def start_backtest(request: BacktestRequest) -> dict[str, Any]:
        # The backtest and the live loop share a provider, and each provider
        # serialises its own calls. Running both would starve each other, and
        # the queueing delay would push live snapshots past the staleness limit
        # so real trades get vetoed for a reason that is not about the market.
        if engine.running:
            raise HTTPException(
                status_code=409,
                detail="stop the engine before starting a backtest: they share the "
                "same provider and would queue behind each other",
            )
        if probe_task and not probe_task.done():
            raise HTTPException(status_code=409, detail="cancel the running probe first")
        if active_backtest_tasks():
            raise HTTPException(status_code=409, detail="a backtest is already running")
        spec = await _checked_spec(request)
        projected = estimate(spec, seconds_per_call=await measured_seconds_per_call())
        if projected.estimated_seconds > MAX_ESTIMATED_HOURS * 3600:
            raise HTTPException(
                status_code=422,
                detail=f"this window needs {projected.decisions_per_model} calls per model, "
                f"about {projected.estimated_seconds / 3600:.1f}h at the latency measured "
                f"here; the limit is {MAX_ESTIMATED_HOURS:g}h. Shorten the window, drop a "
                f"symbol, or use one cadence.",
            )
        # Coverage is checked before the run is created: a real backtest that
        # cannot be real should fail the request, not fail an hour in.
        captures = await _recorded_book(spec) if spec.use_recorded_book else {}
        run_id = await engine.audit.create_backtest_run(
            {
                "symbols": list(spec.symbols),
                "cadences": list(spec.cadences),
                "start": spec.start.isoformat(),
                "end": spec.end.isoformat(),
                "providers": list(spec.providers),
                "provider_configs": {
                    name: {
                        "model": engine.providers.get(name).model,
                        "reasoning_effort": engine.providers.get(name).reasoning_effort,
                    }
                    for name in spec.providers
                },
                "use_recorded_book": spec.use_recorded_book,
                # Recorded because the failure count is meaningless without it:
                # otherwise nothing says whether a run that lost decisions ran
                # with the probe's number or the global default.
                "timeout_seconds": spec.timeout_seconds,
                "estimate": projected.as_dict(),
            },
            list(spec.providers),
        )
        backtest_tasks[run_id] = asyncio.create_task(
            _run_backtest(run_id, spec, captures), name=f"candlepilot-backtest-{run_id}"
        )
        return {"id": run_id, "status": "running", "estimate": projected.as_dict()}

    @app.get("/api/backtests")
    async def list_backtests(limit: int = 20) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise HTTPException(status_code=422, detail="limit must be between 1 and 100")
        return [_json_value(item) for item in await engine.audit.recent_backtest_runs(limit)]

    @app.get("/api/backtests/{run_id}")
    async def get_backtest(run_id: int) -> dict[str, Any]:
        run = await engine.audit.backtest_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="backtest not found")
        return _json_value(run)

    @app.get("/api/backtests/{run_id}/decisions")
    async def get_backtest_decisions(
        run_id: int, provider: str | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        """Every decision one model made, in order.

        The run's totals cannot say why a model made no trades; these can.
        """

        if await engine.audit.backtest_run(run_id) is None:
            raise HTTPException(status_code=404, detail="backtest not found")
        return _json_value(
            await engine.audit.backtest_decisions(
                run_id, provider=provider, limit=max(1, min(limit, 2000))
            )
        )

    @app.post("/api/backtests/{run_id}/cancel")
    async def cancel_backtest(run_id: int) -> dict[str, Any]:
        task = backtest_tasks.get(run_id)
        if task is None or task.done():
            raise HTTPException(status_code=409, detail="backtest is not running")
        task.cancel()
        return {"id": run_id, "status": "cancelling"}

    @app.get("/api/collector")
    async def collector_status() -> dict[str, Any]:
        return {
            **collector.status(),
            "max_symbols": MAX_COLLECTED_SYMBOLS,
            "recorded": _json_value(await engine.audit.book_capture_summary()),
        }

    @app.post("/api/collector/start")
    async def start_collector(request: CollectorStart) -> dict[str, Any]:
        # No provider, no orders: this can run while the engine trades, and it
        # is worth running when nothing does.
        try:
            collector.start(request.symbols)
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return collector.status()

    @app.post("/api/collector/stop")
    async def stop_collector() -> dict[str, Any]:
        await collector.stop()
        return collector.status()

    async def _recorded_book(
        spec: BacktestSpec,
    ) -> dict[str, dict[datetime, dict[str, Any]]]:
        """Load the captures a real backtest needs, refusing a holed window.

        A partly covered window is the trap: some decisions would see order flow
        and some would not, averaging two different strategies into one number
        that never mentions it.
        """

        required = aligned_capture_times(spec.start, spec.end)
        captures: dict[str, dict[datetime, dict[str, Any]]] = {}
        for symbol in spec.symbols:
            rows = await engine.audit.book_captures(symbol, spec.start, spec.end)
            stale = {
                row["schema_version"]
                for row in rows
                if row["schema_version"] != MICROSTRUCTURE_SCHEMA_VERSION
            }
            if stale:
                raise HTTPException(
                    status_code=409,
                    detail=f"{symbol} has captures recorded as {', '.join(sorted(stale))} "
                    f"but this build derives {MICROSTRUCTURE_SCHEMA_VERSION}; those numbers "
                    "no longer mean the same thing and cannot be replayed",
                )
            by_time = {row["captured_at"]: row for row in rows}
            gaps = coverage(required, set(by_time))
            if not gaps.complete:
                raise HTTPException(
                    status_code=409,
                    detail=f"{symbol} has order-book captures for {gaps.recorded} of "
                    f"{gaps.required} decision instants ({gaps.fraction:.0%}). A real "
                    "backtest needs every instant, or half the decisions would see flow "
                    "and half would not. Record the window first, or run a plain backtest.",
                )
            captures[symbol] = {
                when: {
                    "mark_price": Decimal(row["mark_price"]),
                    "bid": Decimal(row["bid"]),
                    "ask": Decimal(row["ask"]),
                    "funding_rate": Decimal(row["funding_rate"]),
                    "features": _capture_features(row),
                }
                for when, row in by_time.items()
            }
        return captures

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
