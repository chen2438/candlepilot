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
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from fastapi import (
    FastAPI,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from candlepilot.application.engine import MAX_RESCUES_PER_RUN, TradingEngine
from candlepilot.auth import AuthManager, SESSION_COOKIE
from candlepilot.application.scheduler import (
    CADENCE_SECONDS,
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
    BacktestDecision,
    BacktestEstimate,
    BacktestRunner,
    BacktestSpec,
    ModelRun,
    ReplayInput,
    compare,
    decision_times,
    estimate,
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
    AccountReconciliationError,
    BinanceTestnetBroker,
    BinanceTestnetCredentials,
    ManualCloseError,
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
    ExecutionReport,
    MarketSnapshot,
    PortfolioState,
)
from candlepilot.market.binance import BinancePublicClient
from candlepilot.market.cache import HistoricalMarketCache
from candlepilot.market.collector import (
    MAX_COLLECTED_SYMBOLS,
    BookCollector,
)
from candlepilot.market.history import build_backtest_candles
from candlepilot.observability import AlertNotifier, OperationalMetrics, evaluate_alerts
from candlepilot.providers.pricing import (
    CACHE_FILENAME as PRICING_CACHE_FILENAME,
)
from candlepilot.providers.pricing import PROVIDER_IDS, ModelPricingCatalog
from candlepilot.providers.pricing import load_catalog as load_pricing_catalog
from candlepilot.providers.base import DecisionProvider, ProviderResult
from candlepilot.providers.openai_compatible import validate_base_url
from candlepilot.providers.registry import ProviderRegistry
from candlepilot.runtime_lock import ServiceInstanceLock
from candlepilot.providers.retry import DECISION_PROVIDER_MAX_ATTEMPTS
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


WEB_UPDATE_HELPER = Path("/usr/local/sbin/candlepilot-web-update")
WEB_UPDATE_STATUS_FILE = Path("/var/lib/candlepilot/update-status.json")
WEB_UPDATE_PHASES = {"idle", "running", "completed", "failed"}


def read_web_update_status(
    *,
    helper_path: Path | None = None,
    status_path: Path | None = None,
    platform: str | None = None,
) -> dict[str, Any]:
    """Read the root updater's deliberately small, world-readable status file."""

    helper_path = helper_path or WEB_UPDATE_HELPER
    status_path = status_path or WEB_UPDATE_STATUS_FILE
    platform = platform or sys.platform
    supported = (
        platform.startswith("linux")
        and helper_path.is_file()
        and os.access(helper_path, os.X_OK)
    )
    payload: dict[str, Any] = {
        "supported": supported,
        "phase": "idle",
        "message": (
            "尚未执行网页更新"
            if supported
            else "网页更新仅在通过 VPS 安装器部署更新助手后可用"
        ),
        "started_at": None,
        "finished_at": None,
        "from_commit": None,
        "current_commit": None,
        "backup": None,
    }
    if not supported or not status_path.is_file():
        return payload
    try:
        stored = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {**payload, "phase": "failed", "message": "无法读取更新状态"}
    if not isinstance(stored, dict) or stored.get("phase") not in WEB_UPDATE_PHASES:
        return {**payload, "phase": "failed", "message": "更新状态格式无效"}
    for key in (
        "message",
        "started_at",
        "finished_at",
        "from_commit",
        "current_commit",
        "backup",
    ):
        value = stored.get(key)
        if value is not None and not isinstance(value, str):
            return {**payload, "phase": "failed", "message": "更新状态格式无效"}
        if isinstance(value, str) and len(value) > 1000:
            return {**payload, "phase": "failed", "message": "更新状态字段过长"}
    return {
        **payload,
        **{key: stored.get(key) for key in payload if key != "supported"},
    }


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginRequest(ApiModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class ProviderSelection(ApiModel):
    providers: list[str] = Field(min_length=1, max_length=16)


class ProviderConfig(ApiModel):
    name: str
    model: str | None = None
    reasoning_effort: str | None = None
    pricing: str | None = None
    auth_source: str | None = None


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
    # frontend never receives the current key, so it cannot send it back.
    api_key: str | None = None
    extra_headers: dict[str, str] | None = None


class CustomProvidersUpdate(ApiModel):
    providers: list[CustomProviderInput] = Field(max_length=MAX_CUSTOM_LLM_PROVIDERS)


class SettingsUpdate(ApiModel):
    # Only the keys the frontend actually changed are sent, so an untouched
    # secret is never echoed back as its own mask.
    values: dict[str, str] = Field(max_length=64)


class RunLimits(ApiModel):
    max_run_seconds: int | None = Field(default=None, gt=0, le=7 * 24 * 3600)
    max_run_cost_usd: float | None = Field(default=None, gt=0, le=10_000)


class EngineStartRequest(ApiModel):
    timeout_seconds: float | None = Field(default=None, gt=0, le=MAX_SUGGESTED_TIMEOUT)


class ClosePositionRequest(ApiModel):
    symbol: str = Field(pattern=r"^[A-Z0-9]+USDT$")


class HistoryClearRequest(ApiModel):
    categories: list[str] = Field(min_length=1, max_length=16)


class CadenceSelection(ApiModel):
    cadences: list[str] = Field(min_length=1, max_length=1)


class CandidatesPerCycleSelection(ApiModel):
    candidates_per_cycle: int = Field(ge=1, le=MAX_CANDIDATES_PER_CYCLE)


class BacktestConfigInput(ApiModel):
    initial_equity: Annotated[Decimal, Field(gt=0)] = Decimal("10000")
    fee_rate: Annotated[Decimal, Field(ge=0, le=1)] = Decimal("0.0005")
    slippage_fraction: Annotated[Decimal, Field(ge=0, le=1)] = Decimal("0.0005")


class BacktestRequest(ApiModel):
    symbols: list[str] = Field(min_length=1, max_length=MAX_BACKTEST_SYMBOLS)
    cadences: list[str] = Field(default=["5m"], min_length=1, max_length=5)
    start: datetime
    end: datetime
    providers: list[str] = Field(min_length=1, max_length=MAX_BACKTEST_MODELS)
    config: BacktestConfigInput = Field(default_factory=BacktestConfigInput)
    # Only possible over a window the collector covered; the coverage is
    # checked up front rather than degrading decision by decision.
    use_recorded_book: bool = False
    replay_live_run_id: int | None = Field(default=None, gt=0)
    # Set from a probe of these providers. None inherits the providers'
    # configured timeout when the run is created; that effective value is
    # then frozen on the run for reproducibility.
    timeout_seconds: float | None = Field(default=None, gt=0, le=MAX_SUGGESTED_TIMEOUT)


class CollectorStart(ApiModel):
    symbols: list[str] = Field(min_length=1, max_length=MAX_COLLECTED_SYMBOLS)


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


def _provider_result_cost_usd(
    result: ProviderResult,
    catalog: ModelPricingCatalog | None,
    provider_ids: Mapping[str, str],
) -> float | None:
    """Price a completed call by its recorded usage and selected billing vendor."""

    usage = result.usage
    cost = usage.get("cost_usd")
    provider_id = provider_ids.get(result.provider)
    if cost is None and catalog is not None and provider_id is not None:
        cost = catalog.cost_usd(
            provider_id,
            result.model,
            input_tokens=int(usage.get("input_tokens") or 0),
            cached_input_tokens=int(usage.get("cached_input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            cache_write_tokens=int(usage.get("cache_creation_input_tokens") or 0),
        )
    return float(cost) if cost is not None else None


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
    """Reject startup constraints that are not scalar environment parsing."""

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


def _status(
    engine: TradingEngine, scheduler: TradingScheduler | None = None
) -> dict[str, Any]:
    testnet_feed = scheduler.testnet_feed if scheduler is not None else None
    return {
        "running": engine.running,
        "emergency_locked": engine.emergency_locked,
        "emergency_locked_until": engine.emergency_locked_until.isoformat()
        if engine.emergency_locked_until
        else None,
        "provider_chain": list(engine.provider_chain),
        "active_provider": engine.active_provider,
        "live_run_id": engine.live_run_id,
        "provider_routes": engine.provider_route_status(),
        "active_cadences": list(engine.active_cadences),
        "run_limits": {
            "max_run_seconds": engine.max_run_seconds,
            "max_run_cost_usd": engine.max_run_cost_usd,
        },
        "risk_limits": {
            "daily_loss_fraction": str(engine.risk.daily_loss_fraction),
        },
        "decision_timeout_seconds": engine.decision_timeout_seconds,
        "startup_probe": engine.startup_probe,
        "auto_stop_reason": engine.auto_stop_reason,
        "route_failure_count": engine.route_failure_count,
        "route_failure_limit": DECISION_PROVIDER_MAX_ATTEMPTS,
        "rescue_count": engine.rescue_count,
        "rescue_limit": MAX_RESCUES_PER_RUN,
        "supported_cadences": list(SUPPORTED_CADENCES),
        "candidates_per_cycle": scheduler.candidates_per_cycle
        if scheduler is not None
        else None,
        "max_candidates_per_cycle": MAX_CANDIDATES_PER_CYCLE,
        "candidate_count": len(engine.candidates),
        "venue_excluded_symbols": list(engine.venue_excluded_symbols),
        "universe_refreshed_at": engine.universe_refreshed_at.isoformat()
        if engine.universe_refreshed_at
        else None,
        "scheduler": {
            "current_cycle": scheduler.current_cycle if scheduler is not None else None,
            "current_cycles": list(scheduler.current_cycles.values())
            if scheduler is not None
            else [],
            "last_cycle": scheduler.last_cycle if scheduler is not None else None,
            "last_error": scheduler.last_error if scheduler is not None else None,
            "universe_last_error": scheduler.universe_last_error
            if scheduler is not None
            else None,
            "guard_last_error": scheduler.guard_last_error
            if scheduler is not None
            else None,
            "trailing_stop": scheduler.trailing_stops.status
            if scheduler is not None
            else None,
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
    pricing_loader: Callable[[Path], Awaitable[ModelPricingCatalog | None]]
    | None = None,
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
            max_portfolio_risk_fraction=settings.max_portfolio_risk_fraction,
            max_margin_fraction=settings.max_margin_fraction,
            max_symbol_margin_fraction=settings.max_symbol_margin_fraction,
            daily_loss_fraction=settings.daily_loss_fraction,
            minimum_reward_risk_ratio=settings.minimum_reward_risk_ratio,
            max_snapshot_age_seconds=settings.max_snapshot_age_seconds,
            require_take_profit=True,
            structure_gate_mode=settings.structure_gate_mode,
        ),
        testnet_broker=testnet_broker,
        cadences=settings.cadences,
    )
    validate_provider_references(settings, engine.providers.names)
    if settings.provider_chain and not engine.provider_chain:
        engine.select_provider_chain(settings.provider_chain)
    if settings.max_run_seconds is not None or settings.max_run_cost_usd is not None:
        engine.select_run_limits(
            max_run_seconds=settings.max_run_seconds,
            max_run_cost_usd=settings.max_run_cost_usd,
        )

    testnet_feed = (
        TestnetUserFeed(
            testnet_stream,
            engine.audit,
            event_handler=testnet_broker.handle_user_event
            if testnet_broker is not None
            else None,
        )
        if testnet_stream is not None
        else None
    )
    instance_lock = ServiceInstanceLock(database.url)

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
        trailing_stop_mode=settings.trailing_stop_mode,
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
    engine_start_lock = asyncio.Lock()
    manual_close_lock = asyncio.Lock()
    trade_fill_reconciliation_lock = asyncio.Lock()
    trade_fill_retry_after: dict[str, float] = {}
    trade_fill_failure_count: dict[str, int] = {}
    restart_pending = False
    update_pending = False
    update_baseline_finished_at: str | None = None
    testnet_account_memo: dict[str, Any] = {"account": None, "expires_at": 0.0}
    testnet_levels_lock = asyncio.Lock()
    testnet_levels_memo: dict[str, Any] = {"levels": None, "expires_at": 0.0}
    auth = AuthManager(
        enabled=settings.auth_enabled,
        username=settings.auth_username,
        password_hash=settings.auth_password_hash,
        session_secret=settings.auth_session_secret,
        session_ttl_seconds=settings.auth_session_ttl_seconds,
        cookie_secure=settings.auth_cookie_secure,
    )

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
            snapshot_loader = getattr(broker, "account_snapshot", None)
            if callable(snapshot_loader):
                account = await snapshot_loader()
            else:
                account = await broker.account()
                position_risk = getattr(broker, "position_risk", None)
                if callable(position_risk):
                    risk_rows = await position_risk()
                    risk_by_symbol = {
                        str(item.get("symbol", "")): {
                            **item,
                            "unrealizedProfit": item.get(
                                "unRealizedProfit", item.get("unrealizedProfit", "0")
                            ),
                        }
                        for item in risk_rows
                    }
                    account = {
                        **account,
                        "positions": [
                            {
                                **item,
                                **risk_by_symbol.get(str(item.get("symbol", "")), {}),
                            }
                            for item in account.get("positions", [])
                        ],
                    }
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

    async def testnet_income_24h() -> Decimal:
        broker = engine.testnet_broker
        if broker is None:
            raise RuntimeError("testnet broker is not configured")
        loader = getattr(broker, "income_24h", None)
        return await loader() if callable(loader) else Decimal("0")

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Acquire before touching live-run rows. A second local service using the
        # same SQLite file must not mark the first service's active run interrupted.
        instance_lock.acquire()
        try:
            await database.initialize()
            # With exclusive ownership established, a live run left open can only
            # mean the previous owner did not execute its graceful shutdown path.
            await engine.audit.interrupt_open_live_runs()
            await engine.audit.fail_open_backtest_runs()
            await engine.cancel_pending_entries(
                "pending limit intent cancelled because the process restarted"
            )
            await engine.restore_runtime_state()
            # Warm the models.dev pricing cache without blocking startup.
            warm_pricing = asyncio.create_task(pricing_catalog())
            try:
                yield
            finally:
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
        finally:
            instance_lock.release()

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
        description="Loopback API for the authenticated Binance testnet control console",
        lifespan=lifespan,
    )
    app.state.engine = engine
    app.state.database = database
    app.state.collector = collector
    app.state.scheduler = scheduler
    app.state.history_cache = history_cache
    app.state.testnet_feed = testnet_feed
    app.state.operational_metrics = operational_metrics
    app.state.auth = auth

    public_api_paths = {
        "/api/auth/status",
        "/api/auth/login",
        "/api/health/live",
        "/api/health/ready",
    }

    @app.middleware("http")
    async def authenticate_request(request: Request, call_next: Any) -> Any:
        path = request.url.path
        if (restart_pending or update_pending) and request.method not in {
            "GET",
            "HEAD",
            "OPTIONS",
        }:
            return JSONResponse(
                status_code=503,
                content={"detail": "backend maintenance is already in progress"},
                headers={"Cache-Control": "no-store"},
            )
        if not auth.enabled or not path.startswith("/api/") or path in public_api_paths:
            return await call_next(request)
        identity = auth.validate_session(request.cookies.get(SESSION_COOKIE))
        if identity is None:
            return JSONResponse(
                status_code=401,
                content={"detail": "authentication required"},
                headers={"Cache-Control": "no-store"},
            )
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != request.headers.get("host"):
                return JSONResponse(
                    status_code=403, content={"detail": "cross-site request denied"}
                )
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse(
                    status_code=403, content={"detail": "cross-site request denied"}
                )
        request.state.auth_identity = identity
        response = await call_next(request)
        response.headers.setdefault("Cache-Control", "no-store")
        return response

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
        cached = await asyncio.to_thread(
            history_cache.load, symbol, cadence, start, end, limit
        )
        if cached is not None:
            return cached
        rows, events = await asyncio.gather(
            market.historical_klines(symbol, cadence, start, end, max_candles=limit),
            market.historical_funding_rates(symbol, start, end),
        )
        candles = build_backtest_candles(rows, events, cadence)
        await asyncio.to_thread(
            history_cache.store, symbol, cadence, start, end, limit, candles
        )
        return candles

    @app.get("/api/auth/status")
    async def auth_status(request: Request) -> JSONResponse:
        identity = auth.validate_session(request.cookies.get(SESSION_COOKIE))
        return JSONResponse(
            content={
                "enabled": auth.enabled,
                "authenticated": identity is not None,
                "username": identity.username
                if identity is not None and auth.enabled
                else None,
                "expires_at": identity.expires_at
                if identity is not None and auth.enabled
                else None,
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/auth/login")
    async def auth_login(request: Request, credentials: LoginRequest) -> JSONResponse:
        if not auth.enabled:
            return JSONResponse(
                content={"enabled": False, "authenticated": True, "username": None},
                headers={"Cache-Control": "no-store"},
            )
        client = request.client.host if request.client is not None else "unknown"
        retry_after = auth.blocked_for(client)
        if retry_after:
            return JSONResponse(
                status_code=429,
                content={"detail": "too many login attempts; try again later"},
                headers={"Retry-After": str(retry_after), "Cache-Control": "no-store"},
            )
        if not auth.authenticate(credentials.username, credentials.password, client):
            return JSONResponse(
                status_code=401,
                content={"detail": "invalid username or password"},
                headers={"Cache-Control": "no-store"},
            )
        response = JSONResponse(
            content={"enabled": True, "authenticated": True, "username": auth.username},
            headers={"Cache-Control": "no-store"},
        )
        response.set_cookie(
            SESSION_COOKIE,
            auth.issue_session(),
            max_age=auth.session_ttl_seconds,
            httponly=True,
            secure=auth.cookie_secure,
            samesite="strict",
            path="/",
        )
        return response

    @app.post("/api/auth/logout")
    async def auth_logout() -> JSONResponse:
        response = JSONResponse(
            content={"authenticated": False}, headers={"Cache-Control": "no-store"}
        )
        response.delete_cookie(
            SESSION_COOKIE,
            httponly=True,
            secure=auth.cookie_secure,
            samesite="strict",
            path="/",
        )
        return response

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
                    "timeout_seconds": provider.timeout,
                    "reasoning_effort_options": list(provider.reasoning_effort_options),
                    "auth_source_options": list(
                        getattr(provider, "auth_source_options", ())
                    ),
                    "pricing": provider_pricing_ids.get(item.provider)
                    if custom
                    else None,
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
                status_code=409,
                detail="cannot change provider settings while the engine runs",
            )
        if background_model_work():
            raise HTTPException(
                status_code=409,
                detail="cannot change provider settings while a probe or backtest runs",
            )
        if not provider.capabilities.configurable_model:
            raise HTTPException(
                status_code=422,
                detail=f"{config.name} has a fixed local strategy version",
            )
        model = (config.model or "").strip() or None
        effort = (config.reasoning_effort or "").strip() or None
        pricing_supplied = "pricing" in config.model_fields_set
        pricing = (config.pricing or "").strip() or None
        auth_source_supplied = "auth_source" in config.model_fields_set
        auth_source = (config.auth_source or "").strip() or None
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
        if auth_source_supplied:
            setter = getattr(provider, "set_auth_source", None)
            if config.name != "codex-auth" or not callable(setter):
                raise HTTPException(
                    status_code=422,
                    detail="auth source can only be changed for Codex Auth",
                )
            if auth_source is None:
                raise HTTPException(
                    status_code=422, detail="Codex auth source is required"
                )
            try:
                setter(auth_source)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        provider.model = model
        provider.reasoning_effort = effort
        if pricing_supplied:
            if pricing is None:
                provider_pricing_ids.pop(config.name, None)
            else:
                provider_pricing_ids[config.name] = pricing
        engine.invalidate_startup_probe("provider settings changed")
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
                status_code=409,
                detail="cannot test a provider while a probe or backtest runs",
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
        duration_ms = int((time.perf_counter() - started) * 1000)
        usage = result.usage
        tokens_reported = any(
            key in usage
            for key in (
                "input_tokens",
                "cached_input_tokens",
                "cache_creation_input_tokens",
                "output_tokens",
                "total_tokens",
            )
        )
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or input_tokens + output_tokens)
        equivalent_cost_usd = _provider_result_cost_usd(
            result, None, provider_pricing_ids
        )
        if (
            equivalent_cost_usd is None
            and tokens_reported
            and result.model
            and request.name in provider_pricing_ids
        ):
            try:
                catalog = await pricing_catalog()
            except Exception:  # noqa: BLE001 - pricing is optional test metadata
                catalog = None
            equivalent_cost_usd = _provider_result_cost_usd(
                result, catalog, provider_pricing_ids
            )
        return {
            "ok": True,
            "provider": request.name,
            "model": result.model,
            "action": result.intent.action.value,
            "duration_ms": duration_ms,
            "usage": {
                "tokens_reported": tokens_reported,
                "input_tokens": input_tokens,
                "cached_input_tokens": int(usage.get("cached_input_tokens") or 0),
                "cache_creation_input_tokens": int(
                    usage.get("cache_creation_input_tokens") or 0
                ),
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
                "equivalent_cost_usd": equivalent_cost_usd,
            },
        }

    @app.get("/api/metrics/providers")
    async def get_provider_metrics(hours: int = 24) -> dict[str, Any]:
        if not 1 <= hours <= 720:
            raise HTTPException(
                status_code=422, detail="hours must be between 1 and 720"
            )
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
            "duration_seconds": max(
                0, int((measured_at - engine.run_started_at).total_seconds())
            ),
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
            testnet_broker_missing=(engine.testnet_broker is None),
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
            raise HTTPException(
                status_code=422, detail="limit must be between 1 and 500"
            )
        return {"events": await engine.audit.recent_alert_events(limit)}

    @app.get("/api/trailing-stops/history")
    async def get_trailing_stop_history(limit: int = 100) -> dict[str, Any]:
        if not 1 <= limit <= 500:
            raise HTTPException(
                status_code=422, detail="limit must be between 1 and 500"
            )
        return {"events": await engine.audit.recent_trailing_stop_events(limit)}

    @app.post("/api/history/clear")
    async def clear_history(request: HistoryClearRequest) -> dict[str, Any]:
        db_categories = set(AuditRepository.HISTORY_TABLES)
        valid = db_categories | {"market_cache", "pricing_cache"}
        unknown = sorted(set(request.categories) - valid)
        if unknown:
            raise HTTPException(
                status_code=422, detail=f"unknown categories: {', '.join(unknown)}"
            )
        active: list[str] = []
        if engine.running:
            active.append("the formal decision engine")
        if background_model_work():
            active.append("a backtest or probe")
        if collector.running:
            active.append("the market collector")
        if active:
            raise HTTPException(
                status_code=409,
                detail=(
                    "stop active work before clearing history: " + ", ".join(active)
                ),
            )
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
            engine.select_provider_chain(selection.providers)
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
            previous = scheduler.candidates_per_cycle
            scheduler.select_candidates_per_cycle(selection.candidates_per_cycle)
            if scheduler.candidates_per_cycle != previous:
                engine.invalidate_startup_probe("candidates per cycle changed")
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
                    "extra_header_names": sorted(headers)
                    if isinstance(headers, dict)
                    else [],
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
            raise HTTPException(
                status_code=404, detail="custom provider has no API key"
            )
        return {"api_key": key}

    @app.post("/api/custom-providers")
    async def save_custom_providers(update: CustomProvidersUpdate) -> dict[str, Any]:
        async with settings_file_lock:
            stored = {entry.get("id"): entry for entry in _stored_custom_providers()}
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
                key = (
                    provider.api_key
                    if provider.api_key is not None
                    else previous.get("api_key")
                )
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
                # Reuse the startup parser so the frontend cannot save a list the
                # next start would reject.
                Settings.from_mapping(candidate)
            except (ValueError, TypeError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            write_env_file(env_path, {CUSTOM_PROVIDERS_ENV: serialized})
        return await get_custom_providers()

    @app.get("/api/update/status")
    async def web_update_status() -> dict[str, Any]:
        nonlocal update_pending
        status = read_web_update_status()
        if update_pending and status["phase"] in {"completed", "failed"}:
            if status["finished_at"] == update_baseline_finished_at:
                # systemd may not have written "running" yet. Do not expose the
                # previous terminal result as if this new request had finished.
                return {
                    **status,
                    "phase": "running",
                    "message": "更新已排队，等待更新服务启动",
                    "finished_at": None,
                }
            update_pending = False
        return status

    @app.post("/api/update", status_code=202)
    async def start_web_update() -> dict[str, Any]:
        nonlocal update_baseline_finished_at, update_pending
        active: list[str] = []
        if engine.running:
            active.append("the formal decision engine")
        elif scheduler.running:
            active.append("the trading scheduler")
        if background_model_work():
            active.append("a provider probe or backtest")
        if collector.running:
            active.append("the market collector")
        if active:
            raise HTTPException(
                status_code=409,
                detail="stop active work before updating CandlePilot: "
                + ", ".join(active),
            )
        if restart_pending:
            raise HTTPException(status_code=409, detail="backend restart is in progress")
        status = read_web_update_status()
        if not status["supported"]:
            raise HTTPException(status_code=409, detail=status["message"])
        if update_pending or status["phase"] == "running":
            raise HTTPException(status_code=409, detail="an update is already running")

        try:
            process = await asyncio.create_subprocess_exec(
                str(WEB_UPDATE_HELPER),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise HTTPException(
                status_code=409,
                detail="the update helper did not acknowledge the request in time",
            ) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=409,
                detail=f"could not start the update helper: {type(exc).__name__}",
            ) from exc
        if process.returncode != 0:
            detail = (stderr or stdout).decode("utf-8", errors="replace").strip()
            raise HTTPException(
                status_code=409,
                detail=(detail or "the update helper refused to start")[-500:],
            )
        update_baseline_finished_at = status["finished_at"]
        update_pending = True
        return {
            "started": True,
            "message": stdout.decode("utf-8", errors="replace").strip(),
        }

    @app.post("/api/restart")
    async def restart_backend() -> dict[str, Any]:
        nonlocal restart_pending
        active: list[str] = []
        if engine.running:
            active.append("the formal decision engine")
        elif scheduler.running:
            active.append("the trading scheduler")
        if background_model_work():
            active.append("a provider probe or backtest")
        if collector.running:
            active.append("the market collector")
        if active:
            raise HTTPException(
                status_code=409,
                detail="stop active work before restarting the backend: "
                + ", ".join(active),
            )
        if restart_pending:
            raise HTTPException(
                status_code=409, detail="backend restart is already in progress"
            )
        if update_pending or read_web_update_status()["phase"] == "running":
            raise HTTPException(status_code=409, detail="a software update is in progress")
        restart_pending = True

        async def _reexec() -> None:
            # Reply first: exec replaces this process, so nothing can be sent after.
            await asyncio.sleep(0.25)
            await scheduler.stop()
            await engine.stop()
            await collector.stop()
            if owns_market:
                await market.close()
            if testnet_broker is not None:
                await testnet_broker.close()
            if testnet_feed is not None:
                await testnet_feed.close()
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
                status_code=422,
                detail=f"unknown settings: {', '.join(sorted(unknown))}",
            )
        for key, value in update.values.items():
            if "\n" in value or "\r" in value:
                raise HTTPException(
                    status_code=422, detail=f"{key} must be a single line"
                )
        # Secret inputs are intentionally write-only in the frontend. Treat a
        # submitted blank as "keep the configured value" so editing and then
        # clearing the masked field cannot erase credentials accidentally.
        effective_updates = {
            key: value
            for key, value in update.values.items()
            if value != "" or not ENV_FIELDS[key].secret
        }
        async with settings_file_lock:
            current = read_env_file(env_path)
            candidate = {
                **current,
                **{k: v for k, v in effective_updates.items() if v != ""},
            }
            for key, value in effective_updates.items():
                if value == "":
                    candidate.pop(key, None)
            # Validate the whole candidate with the startup parsers before the
            # file is touched, so a bad value can never brick the next start.
            try:
                candidate_settings = Settings.from_mapping(candidate)
                _validate_startup_settings(candidate_settings)
            except (ValueError, TypeError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            write_env_file(env_path, effective_updates)
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

    async def live_startup_probe(*, timeout_seconds: float | None) -> dict[str, Any]:
        """Measure one real, non-trading cadence batch without starting a run."""

        if not engine.candidates:
            await engine.refresh_universe()
        if not engine.candidates:
            raise RuntimeError("the live universe has no eligible symbols to probe")

        cadence = min(engine.active_cadences, key=CADENCE_SECONDS.__getitem__)
        candidate_symbols = [
            item.symbol for item in engine.candidates[: scheduler.candidates_per_cycle]
        ]
        candidate_symbols = list(dict.fromkeys(candidate_symbols))
        portfolio = await engine.current_portfolio()
        candidate_symbol_set = set(candidate_symbols)
        extra_position_symbols = [
            symbol
            for symbol in portfolio.positions
            if symbol not in candidate_symbol_set
        ]
        analysis_symbols = [*candidate_symbols, *extra_position_symbols]
        if not analysis_symbols:
            raise RuntimeError("the live run has no symbols to analyze")
        progress: dict[str, Any] = {
            "running": True,
            "ready": False,
            "consumed": False,
            "timeout_seconds": timeout_seconds,
            "provider_count": len(engine.provider_chain),
            "completed_providers": 0,
            "probe_symbols": analysis_symbols,
            "candidate_symbol_count": len(candidate_symbols),
            "extra_position_symbol_count": len(extra_position_symbols),
            "analysis_symbol_count": len(analysis_symbols),
            "probe_cadence": cadence,
            "provider_results": {
                name: {"status": "pending"} for name in engine.provider_chain
            },
            "started_at": datetime.now(UTC).isoformat(),
        }
        # Publish the mutable progress object before the first provider call.
        # GET /api/status and the event websocket can then show each concurrently
        # probed Provider as soon as its one real batch completes.
        engine.startup_probe = progress
        sample_started = time.perf_counter()
        snapshots = [
            await market.market_snapshot(symbol, cadence) for symbol in analysis_symbols
        ]
        portfolio = await engine.current_portfolio()
        shared_seconds = time.perf_counter() - sample_started
        catalog = await pricing_catalog()

        async def invoke(name: str) -> tuple[str, dict[str, Any]]:
            provider = engine.providers.get(name)
            started = time.perf_counter()
            try:
                async with asyncio.timeout(provider.timeout):
                    results = await provider.generate_trade_intents(
                        snapshots, portfolio
                    )
            except TimeoutError as exc:
                raise RuntimeError(
                    f"{name} exceeded the absolute {provider.timeout:g}s startup timeout"
                ) from exc
            expected = [(item.symbol, item.cadence) for item in snapshots]
            actual = [(item.intent.symbol, item.intent.cadence) for item in results]
            if actual != expected:
                raise RuntimeError(
                    f"{name} returned batch intents that do not match the probe inputs"
                )
            duration_seconds = shared_seconds + time.perf_counter() - started
            token_reported = any(
                any(
                    key in result.usage
                    for key in ("input_tokens", "output_tokens", "total_tokens")
                )
                for result in results
            )
            actions: dict[str, int] = {}
            for result in results:
                action = result.intent.action.value
                actions[action] = actions.get(action, 0) + 1
            costs = [
                _provider_result_cost_usd(result, catalog, provider_pricing_ids)
                for result in results
            ]
            equivalent_cost_usd = (
                sum(cost for cost in costs if cost is not None)
                if all(cost is not None for cost in costs)
                else None
            )
            summary = {
                "status": "completed",
                "model": results[0].model if results else provider.model,
                "reasoning_effort": results[0].reasoning_effort
                if results
                else provider.reasoning_effort,
                "duration_seconds": round(duration_seconds, 3),
                "actions": actions,
                "input_tokens": sum(
                    int(result.usage.get("input_tokens") or 0) for result in results
                )
                if token_reported
                else None,
                "cached_input_tokens": sum(
                    int(
                        result.usage.get("cached_input_tokens")
                        or result.usage.get("cache_read_input_tokens")
                        or 0
                    )
                    for result in results
                )
                if token_reported
                else None,
                "output_tokens": sum(
                    int(result.usage.get("output_tokens") or 0) for result in results
                )
                if token_reported
                else None,
                "total_tokens": sum(
                    int(
                        result.usage.get("total_tokens")
                        or int(result.usage.get("input_tokens") or 0)
                        + int(result.usage.get("output_tokens") or 0)
                    )
                    for result in results
                )
                if token_reported
                else None,
                "equivalent_cost_usd": equivalent_cost_usd,
                "intents": [
                    {
                        "symbol": result.intent.symbol,
                        "action": result.intent.action.value,
                        "confidence": result.intent.confidence,
                    }
                    for result in results
                ],
            }
            progress["provider_results"][name] = summary
            progress["completed_providers"] = int(progress["completed_providers"]) + 1
            return name, summary

        tasks = [
            asyncio.create_task(invoke(name), name=f"candlepilot-startup-probe-{name}")
            for name in engine.provider_chain
        ]
        try:
            measured = await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

        provider_results = {name: summary for name, summary in measured}
        slowest_seconds = max(
            float(summary["duration_seconds"]) for summary in provider_results.values()
        )
        symbol_count = len(analysis_symbols)
        projected_cycle_seconds = slowest_seconds
        cadence_seconds = {
            cadence_name: CADENCE_SECONDS[cadence_name]
            for cadence_name in engine.active_cadences
        }
        overloaded = [
            cadence_name
            for cadence_name, seconds in cadence_seconds.items()
            if projected_cycle_seconds > seconds
        ]
        utilization = projected_cycle_seconds * sum(
            1 / seconds for seconds in cadence_seconds.values()
        )
        if overloaded or utilization > 1:
            cadence_detail = ", ".join(
                f"{name}={seconds}s" for name, seconds in cadence_seconds.items()
            )
            raise ValueError(
                "live startup probe rejected this capacity: the real batch "
                f"for one {symbol_count}-symbol batch was {slowest_seconds:.2f}s; selected cadence is "
                f"{cadence_detail} and provider utilization is "
                f"{utilization * 100:.1f}%. Reduce analysis symbols or select a longer cadence."
            )
        return {
            "running": False,
            "ready": True,
            "consumed": False,
            "timeout_seconds": timeout_seconds,
            "provider_count": len(engine.provider_chain),
            "completed_providers": len(engine.provider_chain),
            "probe_symbols": analysis_symbols,
            "candidate_symbol_count": len(candidate_symbols),
            "extra_position_symbol_count": len(extra_position_symbols),
            "probe_cadence": cadence,
            "provider_results": provider_results,
            "slowest_seconds": round(slowest_seconds, 3),
            "analysis_symbol_count": symbol_count,
            "projected_cycle_seconds": round(projected_cycle_seconds, 3),
            "aggregate_utilization": round(utilization, 4),
            "max_safe_symbols": None,
            "started_at": progress["started_at"],
            "checked_at": datetime.now(UTC).isoformat(),
        }

    def resolve_live_timeout(
        request: EngineStartRequest,
    ) -> tuple[list[Any], float | None]:
        external = [
            engine.providers.get(name)
            for name in engine.provider_chain
            if engine.providers.get(name).capabilities.external_inference
        ]
        timeout_seconds = request.timeout_seconds
        if external and timeout_seconds is None:
            configured = {provider.timeout for provider in external}
            if len(configured) != 1:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "selected external providers have different timeouts; "
                        "choose one decision timeout for this live run"
                    ),
                )
            timeout_seconds = configured.pop()
        return external, timeout_seconds

    async def perform_startup_probe(request: EngineStartRequest) -> None:
        """Run and store one startup probe while the startup lock is held."""

        if engine.running:
            raise HTTPException(status_code=409, detail="engine is already running")
        if not engine.provider_chain:
            raise HTTPException(
                status_code=409,
                detail="at least one ready decision provider must be selected",
            )
        external, timeout_seconds = resolve_live_timeout(request)
        try:
            engine.startup_probe = None
            engine.configure_decision_timeout(timeout_seconds if external else None)
            engine.startup_probe = await live_startup_probe(
                timeout_seconds=timeout_seconds if external else None
            )
        except ValueError as exc:
            if engine.startup_probe is not None:
                engine.startup_probe.update(
                    running=False,
                    ready=False,
                    error=str(exc),
                )
            engine.restore_provider_timeouts()
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            if engine.startup_probe is not None:
                engine.startup_probe.update(
                    running=False,
                    ready=False,
                    error=str(exc),
                )
            engine.restore_provider_timeouts()
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/engine/probe")
    async def probe_engine(request: EngineStartRequest | None = None) -> dict[str, Any]:
        if background_model_work():
            raise HTTPException(
                status_code=409,
                detail="cannot probe the engine while another probe or backtest runs",
            )
        request = request or EngineStartRequest()
        async with engine_start_lock:
            await perform_startup_probe(request)
        return _status(engine, scheduler)

    async def begin_live_run(
        request: EngineStartRequest,
        *,
        require_probe: bool = True,
        single_cycle: bool = False,
    ) -> dict[str, object] | None:
        """Apply shared startup checks, with a probe gate only for scheduling."""

        if engine.running:
            raise HTTPException(status_code=409, detail="engine is already running")
        if not engine.provider_chain:
            raise HTTPException(
                status_code=409,
                detail="at least one ready decision provider must be selected",
            )
        external, timeout_seconds = resolve_live_timeout(request)
        probe = engine.startup_probe
        expected_timeout = timeout_seconds if external else None
        if require_probe:
            if (
                probe is None
                or not probe.get("ready")
                or probe.get("consumed")
                or probe.get("timeout_seconds") != expected_timeout
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "a successful startup probe for the current parameters is required "
                        "before starting the engine"
                    ),
                )
        else:
            try:
                engine.configure_decision_timeout(expected_timeout)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        run_config: dict[str, object] = {
            "candidates_per_cycle": scheduler.candidates_per_cycle,
            "trailing_stop_mode": scheduler.trailing_stops.mode,
        }
        if single_cycle:
            run_config["single_cycle"] = True
        try:
            await engine.start(run_config=run_config)
            if require_probe:
                assert probe is not None
                probe["ready"] = False
                probe["consumed"] = True
            else:
                engine.invalidate_startup_probe("single analysis started")
        except ValueError as exc:
            if require_probe:
                assert probe is not None
                probe["ready"] = False
                probe["error"] = str(exc)
            engine.restore_provider_timeouts()
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            if require_probe:
                assert probe is not None
                probe["ready"] = False
                probe["error"] = str(exc)
            engine.restore_provider_timeouts()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return probe

    @app.post("/api/engine/start")
    async def start_engine(request: EngineStartRequest | None = None) -> dict[str, Any]:
        if background_model_work():
            raise HTTPException(
                status_code=409,
                detail="cannot start the engine while a probe or backtest runs",
            )
        request = request or EngineStartRequest()
        async with engine_start_lock:
            await begin_live_run(request)
            scheduler.start()
        return _status(engine, scheduler)

    @app.post("/api/engine/probe-and-start")
    async def probe_and_start_engine(
        request: EngineStartRequest | None = None,
    ) -> dict[str, Any]:
        """Probe current live parameters and start only when that probe succeeds."""

        if background_model_work():
            raise HTTPException(
                status_code=409,
                detail="cannot probe and start while another probe or backtest runs",
            )
        request = request or EngineStartRequest()
        async with engine_start_lock:
            await perform_startup_probe(request)
            await begin_live_run(request)
            scheduler.start()
        return _status(engine, scheduler)

    @app.post("/api/engine/run-once")
    async def run_engine_once(
        request: EngineStartRequest | None = None,
    ) -> dict[str, Any]:
        """Analyze and trade one immediate batch, then stop without scheduling another."""

        if background_model_work():
            raise HTTPException(
                status_code=409,
                detail="cannot start the engine while a probe or backtest runs",
            )
        request = request or EngineStartRequest()
        async with engine_start_lock:
            await begin_live_run(request, require_probe=False, single_cycle=True)
        failure: BaseException | None = None
        try:
            await scheduler.run_once(engine.active_cadences[0])
        except BaseException as exc:
            failure = exc
        finally:
            emergency_is_cancelling = (
                isinstance(failure, asyncio.CancelledError) and scheduler.stop_requested
            )
            if engine.running and not emergency_is_cancelling:
                await engine.stop(
                    reason=(
                        "single analysis completed"
                        if failure is None
                        else f"single analysis failed: {type(failure).__name__}: {failure}"
                    )
                )
        if failure is not None:
            if isinstance(failure, asyncio.CancelledError):
                if emergency_is_cancelling:
                    return _status(engine, scheduler)
                raise failure
            raise HTTPException(status_code=409, detail=str(failure)) from failure
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
        cadence: Literal["1m", "5m", "15m", "30m", "1h", "4h"],
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
            raise HTTPException(
                status_code=502, detail=f"market history failed: {exc}"
            ) from exc
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
            raise HTTPException(
                status_code=502, detail=f"funding history failed: {exc}"
            ) from exc
        return [
            {
                "timestamp": event.timestamp,
                "rate": str(event.rate),
                "mark_price": str(event.mark_price)
                if event.mark_price is not None
                else None,
            }
            for event in events
        ]

    @app.get("/api/market/backtest-candles")
    async def get_backtest_candles(
        symbol: str,
        cadence: Literal["1m", "5m", "15m", "30m", "1h", "4h"],
        start: datetime,
        end: datetime,
        limit: int = 10_000,
    ) -> list[dict[str, Any]]:
        try:
            return await load_backtest_candles(
                symbol.upper(), cadence, start, end, limit
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"backtest history failed: {exc}"
            ) from exc

    @app.post("/api/universe/refresh")
    async def refresh_universe() -> list[dict[str, Any]]:
        try:
            await engine.refresh_universe()
            engine.invalidate_startup_probe("candidate universe refreshed")
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"market refresh failed: {exc}"
            ) from exc
        return await get_universe()

    @app.get("/api/signals")
    async def get_signals(limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise HTTPException(
                status_code=422, detail="limit must be between 1 and 500"
            )
        return await engine.audit.recent_intents(limit)

    @app.get("/api/decision-events")
    async def get_decision_events(
        limit: int = 100,
        before_id: int | None = None,
        run_limit: int | None = None,
        before_run_id: int | None = None,
        symbol: str | None = None,
        cadence: str | None = None,
        provider: str | None = None,
        outcome: str | None = None,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise HTTPException(
                status_code=422, detail="limit must be between 1 and 500"
            )
        if before_id is not None and before_id < 1:
            raise HTTPException(status_code=422, detail="before_id must be positive")
        if run_limit is not None and not 1 <= run_limit <= 100:
            raise HTTPException(
                status_code=422, detail="run_limit must be between 1 and 100"
            )
        if before_run_id is not None and before_run_id < 1:
            raise HTTPException(
                status_code=422, detail="before_run_id must be positive"
            )
        if before_run_id is not None and run_limit is None:
            raise HTTPException(
                status_code=422, detail="before_run_id requires run_limit"
            )
        if run_limit is not None and before_id is not None:
            raise HTTPException(
                status_code=422, detail="run paging cannot use before_id"
            )
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
            run_limit=run_limit,
            before_run_id=before_run_id,
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

    @app.get("/api/live-runs/performance")
    async def get_live_run_performance(limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise HTTPException(
                status_code=422, detail="limit must be between 1 and 500"
            )
        try:
            account = await testnet_account()
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"live run performance account query failed: {exc}",
            ) from exc
        current_positions = {
            str(item.get("symbol", "")): {
                "mark_price": str(item.get("markPrice", item.get("entryPrice", "0"))),
                "unrealized_pnl": str(item.get("unrealizedProfit", "0")),
            }
            for item in account.get("positions", [])
            if Decimal(str(item.get("positionAmt", "0"))) != 0
        }
        return await engine.audit.recent_live_run_performance(
            limit,
            current_positions=current_positions,
        )

    @app.get("/api/testnet/events")
    async def get_testnet_events(limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise HTTPException(
                status_code=422, detail="limit must be between 1 and 500"
            )
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
                "total_unrealized_profit": str(
                    account.get("totalUnrealizedProfit", "0")
                ),
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
            account, realized_24h = await asyncio.gather(
                testnet_account(), testnet_income_24h()
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
                "pnl_24h": str(
                    Decimal(str(realized_24h))
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
            set(reconciliation.unprotected_symbols)
            if reconciliation is not None
            else None
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

    @app.post("/api/account/positions/close")
    async def close_account_position(request: ClosePositionRequest) -> dict[str, Any]:
        broker = engine.testnet_broker
        if broker is None:
            raise HTTPException(
                status_code=409, detail="testnet broker is not configured"
            )
        async with engine_start_lock, manual_close_lock:
            if engine.running:
                raise HTTPException(
                    status_code=409,
                    detail="stop the trading engine before manually closing a position",
                )
            report: ExecutionReport | None = None
            try:
                report = await broker.close_position_market(request.symbol)
            except AccountReconciliationError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except ManualCloseError as exc:
                report = exc.report
                await engine.audit.record_execution(request.symbol, report)
                raise HTTPException(
                    status_code=502,
                    detail=f"manual close failed during {exc.stage.lower()}: {exc}",
                ) from exc
            except Exception as exc:
                raise HTTPException(
                    status_code=502,
                    detail=f"manual market close failed: {exc}",
                ) from exc
            finally:
                testnet_account_memo["account"] = None
                testnet_account_memo["expires_at"] = 0.0
                testnet_levels_memo["levels"] = None
                testnet_levels_memo["expires_at"] = 0.0
                engine.testnet_reconciliation = None
            await engine.audit.record_execution(request.symbol, report)
            return _json_value(
                {
                    "symbol": request.symbol,
                    "client_order_id": report.client_order_id,
                    "status": report.status,
                    "filled_quantity": report.filled_quantity,
                    "average_price": report.average_price,
                    "timestamp": report.timestamp,
                }
            )

    @app.get("/api/orders")
    async def get_orders(
        limit: int = 100, status: str | None = None
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise HTTPException(
                status_code=422, detail="limit must be between 1 and 500"
            )
        return await engine.audit.recent_executions(limit, status=status)

    @app.get("/api/fills")
    async def get_fills(limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise HTTPException(
                status_code=422, detail="limit must be between 1 and 500"
            )
        broker = engine.testnet_broker
        manual_resolver = getattr(broker, "completed_order_fill_event", None)
        exit_resolver = getattr(broker, "completed_exit_fill_event", None)
        async with trade_fill_reconciliation_lock:
            fills = await engine.audit.recent_trade_fills(limit)
            requests: list[tuple[str, Any, str, str]] = []
            if manual_resolver is not None:
                requests.extend(
                    (
                        f"manual:{fill['client_order_id']}",
                        manual_resolver,
                        fill["symbol"],
                        fill["client_order_id"],
                    )
                    for fill in fills
                    if fill["source"] == "execution_audit"
                    and fill["purpose"] == "manual_close"
                )
            if exit_resolver is not None:
                try:
                    account = await testnet_account()
                except Exception as exc:
                    account = None
                    logging.getLogger("candlepilot").warning(
                        "offline exit fill reconciliation skipped: account query failed (%s)",
                        type(exc).__name__,
                    )
                if account is not None:
                    open_symbols = {
                        str(position.get("symbol", ""))
                        for position in account.get("positions", [])
                        if Decimal(str(position.get("positionAmt", "0"))) != 0
                    }
                    related_entries = {
                        fill["related_client_order_id"]
                        for fill in fills
                        if fill["related_client_order_id"] is not None
                    }
                    requests.extend(
                        (
                            f"exit:{fill['client_order_id']}",
                            exit_resolver,
                            fill["symbol"],
                            fill["client_order_id"],
                        )
                        for fill in fills
                        if fill["purpose"] == "entry"
                        and fill["symbol"] not in open_symbols
                        and fill["client_order_id"] not in related_entries
                    )
            for retry_key, resolver, symbol, client_order_id in requests:
                if trade_fill_retry_after.get(retry_key, 0) > time.monotonic():
                    continue
                try:
                    event = await resolver(symbol, client_order_id)
                except Exception as exc:
                    event = None
                    logging.getLogger("candlepilot").warning(
                        "trade fill reconciliation failed for %s: %s",
                        client_order_id,
                        type(exc).__name__,
                    )
                if event is not None:
                    await engine.audit.record_user_event(event)
                    trade_fill_retry_after.pop(retry_key, None)
                    trade_fill_failure_count.pop(retry_key, None)
                    continue
                failures = trade_fill_failure_count.get(retry_key, 0) + 1
                trade_fill_failure_count[retry_key] = failures
                delays = (5, 15, 60, 300)
                trade_fill_retry_after[retry_key] = (
                    time.monotonic() + delays[min(failures - 1, len(delays) - 1)]
                )
            return await engine.audit.recent_trade_fills(limit)

    @app.get("/api/risk-events")
    async def get_risk_events(
        limit: int = 100, accepted: bool | None = None
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise HTTPException(
                status_code=422, detail="limit must be between 1 and 500"
            )
        return await engine.audit.recent_risk_decisions(limit, accepted=accepted)

    @app.get("/api/structure-gate/summary")
    async def get_structure_gate_summary(limit: int = 500) -> dict[str, Any]:
        if not 1 <= limit <= 500:
            raise HTTPException(
                status_code=422, detail="limit must be between 1 and 500"
            )
        summary = await engine.audit.structure_gate_summary(limit)
        summary["mode"] = engine.risk.structure_gate_mode
        return summary

    backtest_tasks: dict[int, asyncio.Task[None]] = {}
    # Probes are pre-flight, not history: they describe the endpoint as it is
    # right now, so they live with the process rather than in the database.
    probes: dict[str, ProviderProbe] = {}
    probe_keys: dict[str, tuple[object, ...]] = {}
    probe_task: asyncio.Task[None] | None = None

    def active_backtest_tasks() -> list[asyncio.Task[None]]:
        return [task for task in backtest_tasks.values() if not task.done()]

    def background_model_work() -> bool:
        return bool(
            engine_start_lock.locked()
            or active_backtest_tasks()
            or (probe_task and not probe_task.done())
        )

    def _set_probe_task(task: asyncio.Task[None]) -> None:
        nonlocal probe_task
        probe_task = task

    def _spec_from(request: BacktestRequest) -> BacktestSpec:
        return BacktestSpec(
            symbols=tuple(symbol.upper() for symbol in request.symbols),
            cadences=tuple(request.cadences),
            start=request.start,
            end=request.end,
            providers=tuple(request.providers),
            config=BacktestConfig(**request.config.model_dump()),
            use_recorded_book=request.use_recorded_book,
            replay_live_run_id=request.replay_live_run_id,
            timeout_seconds=request.timeout_seconds,
        )

    async def _checked_spec(request: BacktestRequest) -> BacktestSpec:
        spec = _spec_from(request)
        if spec.replay_live_run_id is not None:
            if spec.use_recorded_book:
                raise HTTPException(
                    status_code=422,
                    detail="formal-run replay and recorded-book mode are mutually exclusive",
                )
            live_run = await engine.audit.live_run(spec.replay_live_run_id)
            rows = await engine.audit.live_decision_snapshots(spec.replay_live_run_id)
            if live_run is None:
                raise HTTPException(status_code=404, detail="formal run not found")
            if not rows:
                raise HTTPException(
                    status_code=409,
                    detail="this formal run has no exact decision snapshots",
                )
            initial_payload = live_run["config"].get("initial_portfolio")
            if initial_payload is None:
                raise HTTPException(
                    status_code=409,
                    detail="this formal run predates account-aware replay data",
                )
            initial_portfolio = PortfolioState.model_validate(initial_payload)
            missing_protection = [
                symbol
                for symbol, position in initial_portfolio.positions.items()
                if position.stop_loss is None
            ]
            if missing_protection:
                raise HTTPException(
                    status_code=409,
                    detail="recorded starting positions lack protective stops: "
                    + ", ".join(sorted(missing_protection)),
                )
            symbols = tuple(dict.fromkeys(str(row["symbol"]) for row in rows))
            cadences = tuple(dict.fromkeys(str(row["cadence"]) for row in rows))
            missing_positions = set(initial_portfolio.positions) - set(symbols)
            if missing_positions:
                raise HTTPException(
                    status_code=409,
                    detail="recorded starting positions are missing market replay data: "
                    + ", ".join(sorted(missing_positions)),
                )
            start = live_run["started_at"]
            last_snapshot = max(row["captured_at"] for row in rows)
            end = live_run["ended_at"] or (last_snapshot + timedelta(minutes=5))
            if end <= last_snapshot:
                end = last_snapshot + timedelta(minutes=5)
            spec = replace(
                spec,
                symbols=symbols,
                cadences=cadences,
                start=start,
                end=end,
                config=replace(spec.config, initial_equity=initial_portfolio.equity),
                replay_decision_count=len(rows),
                replay_call_count=len({str(row["batch_id"]) for row in rows}),
            )
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
        selected_providers: list[DecisionProvider] = []
        for provider in spec.providers:
            try:
                selected_providers.append(engine.providers.get(provider))
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
        if spec.timeout_seconds is None:
            timed_providers = [
                provider
                for provider in selected_providers
                if provider.capabilities.external_inference
            ]
            configured_timeouts = {provider.timeout for provider in timed_providers}
            if len(configured_timeouts) != 1:
                if configured_timeouts:
                    raise HTTPException(
                        status_code=422,
                        detail="selected external providers have different configured timeouts; "
                        "set an explicit timeout for this backtest",
                    )
            elif configured_timeouts:
                spec = replace(spec, timeout_seconds=configured_timeouts.pop())
        return spec

    async def _live_replay_inputs(
        spec: BacktestSpec,
    ) -> tuple[
        dict[tuple[str, str, datetime], ReplayInput],
        PortfolioState | None,
    ]:
        if spec.replay_live_run_id is None:
            return {}, None
        live_run = await engine.audit.live_run(spec.replay_live_run_id)
        rows = await engine.audit.live_decision_snapshots(spec.replay_live_run_id)
        if live_run is None or not rows:
            raise RuntimeError(
                "formal replay data disappeared before the backtest started"
            )
        initial = PortfolioState.model_validate(live_run["config"]["initial_portfolio"])
        snapshots: dict[tuple[str, str, datetime], ReplayInput] = {}
        for row in rows:
            snapshot = MarketSnapshot.model_validate(row["market"])
            rules = SymbolRules(
                **{
                    key: Decimal(value) if value is not None else None
                    for key, value in row["rules"].items()
                }
            )
            snapshots[(snapshot.symbol, snapshot.cadence, snapshot.timestamp)] = ReplayInput(
                batch_id=str(row["batch_id"]),
                snapshot=snapshot,
                rules=rules,
            )
        return snapshots, initial

    def _providers_requiring_probe(spec: BacktestSpec) -> tuple[str, ...]:
        return tuple(
            name
            for name in spec.providers
            if engine.providers.get(name).capabilities.requires_backtest_probe
        )

    def _probe_key(spec: BacktestSpec, provider_name: str) -> tuple[object, ...]:
        """Everything that can change the payload or selected model.

        Timeout is deliberately absent: applying the timeout suggested by a
        probe must not invalidate the measurements that produced it.
        """

        provider = engine.providers.get(provider_name)
        return (
            spec.symbols,
            spec.cadences,
            spec.start,
            spec.end,
            spec.config.initial_equity,
            spec.config.fee_rate,
            spec.config.slippage_fraction,
            spec.use_recorded_book,
            spec.replay_live_run_id,
            provider.model,
            provider.reasoning_effort,
        )

    def _probe_estimate(spec: BacktestSpec) -> tuple[BacktestEstimate, dict[str, Any]]:
        """Estimate from five clean calls against every participating model."""

        invalid: list[str] = []
        latencies: dict[str, float] = {}
        required = _providers_requiring_probe(spec)
        for name in required:
            probe = probes.get(name)
            if probe is None or probe_keys.get(name) != _probe_key(spec, name):
                invalid.append(f"{name} has no matching probe")
                continue
            if (
                not probe.done
                or probe.error is not None
                or len(probe.calls) != PROBE_DECISIONS
                or probe.failures
            ):
                invalid.append(
                    f"{name} does not have {PROBE_DECISIONS} successful calls"
                )
                continue
            assert probe.average_ok_seconds is not None
            latencies[name] = probe.average_ok_seconds
        if invalid:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"run a fresh {PROBE_DECISIONS}-decision probe for the current "
                    f"backtest settings: {'; '.join(invalid)}"
                ),
            )

        for name in spec.providers:
            if name in latencies:
                continue
            latency = engine.providers.get(
                name
            ).capabilities.estimated_seconds_per_decision
            latencies[name] = latency if latency is not None else 0.001
        slowest_provider = max(spec.providers, key=latencies.__getitem__)
        seconds_per_call = latencies[slowest_provider]
        projected = estimate(spec, seconds_per_call=seconds_per_call)
        payload = {
            **projected.as_dict(),
            "seconds_per_call": round(seconds_per_call, 3),
            "slowest_provider": slowest_provider,
            "latency_source": (
                "probe_slowest_average" if required else "local_deterministic"
            ),
            "max_hours": MAX_ESTIMATED_HOURS,
            "within_limit": projected.estimated_seconds <= MAX_ESTIMATED_HOURS * 3600,
        }
        return projected, payload

    @app.post("/api/backtests/estimate")
    async def estimate_backtest(request: BacktestRequest) -> dict[str, Any]:
        spec = await _checked_spec(request)
        _projected, payload = _probe_estimate(spec)
        return payload

    @app.get("/api/backtests/formal-runs")
    async def replayable_formal_runs(limit: int = 50) -> list[dict[str, Any]]:
        return await engine.audit.replayable_live_runs(max(1, min(limit, 100)))

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
        restore: list[tuple[DecisionProvider, float]] = []
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
            replay_snapshots, initial_portfolio = await _live_replay_inputs(spec)
        except asyncio.CancelledError:
            try:
                await engine.audit.finish_backtest_run(run_id, status="cancelled")
            finally:
                backtest_tasks.pop(run_id, None)
            raise
        except Exception as exc:  # noqa: BLE001 - surface the reason on the run
            await engine.audit.finish_backtest_run(
                run_id, status="failed", error=f"history load failed: {exc}"[:500]
            )
            return

        async def flush(run: ModelRun, decision: BacktestDecision | None) -> None:
            decision_row = None
            if decision is not None:
                decision_row = decision.as_row()
                fill = decision_row.pop("fill")
                attempts = decision_row.pop("attempt_started_at")
                decision_row["fill_json"] = json.dumps(fill) if fill else None
                decision_row["attempts_json"] = json.dumps(
                    [started.isoformat() for started in attempts]
                )
            live_result = None
            if run.live_result is not None:
                live_result = _json_value(asdict(run.live_result))
            await engine.audit.update_backtest_progress(
                run_id,
                run.provider,
                decisions_done=run.decisions_done,
                decisions_total=run.decisions_total,
                calls_failed=run.calls_failed,
                usage=run.usage_dict(),
                progress={
                    "elapsed_seconds": run.elapsed_seconds,
                    "remaining_seconds": run.remaining_seconds,
                    "live_result": live_result,
                },
                result=_json_value(asdict(run.result))
                if run.result is not None
                else None,
                error=run.error,
                decision=decision_row,
            )

        try:
            catalog = await pricing_catalog()
            with _timeouts(spec):
                runs = await compare(
                    spec=spec,
                    runner_for=lambda _: BacktestRunner(
                        spec=spec,
                        series=series,
                        rules=rules,
                        risk=engine.risk,
                        captures=captures,
                        replay_snapshots=replay_snapshots,
                        initial_portfolio=initial_portfolio,
                        cost_for_result=lambda result: _provider_result_cost_usd(
                            result, catalog, provider_pricing_ids
                        ),
                    ),
                    provider_for=engine.providers.get,
                    on_progress=flush,
                )
            failed_runs = [run for run in runs if run.error is not None]
            if failed_runs:
                effective_end = min(
                    run.last_successful_at or spec.start for run in failed_runs
                )
                unavailable = [run for run in failed_runs if run.provider_failed]
                if unavailable:
                    error = "; ".join(
                        f"{run.provider} became unavailable after "
                        f"{DECISION_PROVIDER_MAX_ATTEMPTS if engine.providers.get(run.provider).capabilities.retryable else 1} attempts"
                        for run in unavailable
                    )
                else:
                    error = "; ".join(
                        f"{run.provider}: {run.error}" for run in failed_runs
                    )
                await engine.audit.finish_backtest_run(
                    run_id,
                    status="failed",
                    error=error[:500],
                    effective_end=effective_end,
                )
                return
            await engine.audit.finish_backtest_run(run_id, status="completed")
        except asyncio.CancelledError:
            await engine.audit.finish_backtest_run(run_id, status="cancelled")
            raise
        except Exception as exc:  # noqa: BLE001
            await engine.audit.finish_backtest_run(
                run_id, status="failed", error=str(exc)[:500]
            )
        finally:
            backtest_tasks.pop(run_id, None)

    async def _run_probe(spec: BacktestSpec) -> None:
        # Every provider is published before anything is awaited, and each is
        # filled in as its calls land. A probe that only appears once it has
        # finished is indistinguishable from a hung one for as long as it takes
        # -- and at the ceiling, five calls is fifteen minutes.
        required = _providers_requiring_probe(spec)
        for name in required:
            probes[name] = ProviderProbe(provider=name)

        def fail_all(reason: str) -> None:
            for name in required:
                probes[name].error = reason[:200]
                probes[name].done = True

        try:
            series, _rules = await _load_series(spec)
            captures = await _recorded_book(spec) if spec.use_recorded_book else {}
            replay_snapshots, initial_portfolio = await _live_replay_inputs(spec)
        except asyncio.CancelledError:
            fail_all("cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 - surface it on every provider
            fail_all(f"history load failed: {exc}")
            return

        symbol = spec.symbols[0]
        builder = HistoricalSnapshotBuilder(series[symbol], captures.get(symbol))
        portfolio = initial_portfolio or SimulatedExchange(spec.config).portfolio_state(
            {}
        )
        replay_probe_batches: dict[str, list[MarketSnapshot]] = {}
        for item in replay_snapshots.values():
            replay_probe_batches.setdefault(item.batch_id, []).append(item.snapshot)

        async def one(name: str) -> None:
            probe = probes[name]
            try:
                await probe_provider(
                    engine.providers.get(name),
                    spec=spec,
                    builder=builder,
                    symbol=symbol,
                    portfolio=portfolio,
                    into=probe,
                    snapshot_batches=list(replay_probe_batches.values()) or None,
                )
            except asyncio.CancelledError:
                # probe_provider's finally already marks it done, but only the
                # error distinguishes a cancellation from a short clean run.
                probe.error = "cancelled"
                probe.done = True
                raise
            except Exception as exc:  # noqa: BLE001 - one endpoint, not the set
                probe.error = str(exc)[:200]
                probe.done = True

        # Providers are also parallel during the real comparison. Probing them
        # serially would turn five 180-second ceilings into an hour-long gate
        # with four models, while teaching nothing about their shared wall time.
        await asyncio.gather(*(one(name) for name in required))

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
        required = _providers_requiring_probe(spec)
        if not required:
            raise HTTPException(
                status_code=422,
                detail="the selected local decision providers do not require a probe",
            )
        probes.clear()
        probe_keys.clear()
        for name in required:
            probe_keys[name] = _probe_key(spec, name)
        _set_probe_task(
            asyncio.create_task(_run_probe(spec), name="candlepilot-backtest-probe")
        )
        return {"providers": list(required), "decisions": PROBE_DECISIONS}

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
                        {
                            "seconds": round(call.seconds, 1),
                            "ok": call.ok,
                            "error": call.error,
                        }
                        for call in item.calls
                    ],
                    "slowest_ok_seconds": (
                        round(item.slowest_ok_seconds, 1)
                        if item.slowest_ok_seconds is not None
                        else None
                    ),
                    "average_ok_seconds": (
                        round(item.average_ok_seconds, 1)
                        if item.average_ok_seconds is not None
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

        Five calls at the ceiling is fifteen minutes; without this the only way
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
            raise HTTPException(
                status_code=409, detail="cancel the running probe first"
            )
        if active_backtest_tasks():
            raise HTTPException(status_code=409, detail="a backtest is already running")
        spec = await _checked_spec(request)
        # Coverage errors are facts about the requested historical window, so
        # report them before asking for a probe that could never reproduce the
        # requested real payload.
        captures = await _recorded_book(spec) if spec.use_recorded_book else {}
        projected, estimate_payload = _probe_estimate(spec)
        if projected.estimated_seconds > MAX_ESTIMATED_HOURS * 3600:
            raise HTTPException(
                status_code=422,
                detail=f"this window needs {projected.decisions_per_model} calls per model, "
                f"about {projected.estimated_seconds / 3600:.1f}h at the slowest "
                f"participating model's probed latency; the limit is "
                f"{MAX_ESTIMATED_HOURS:g}h. Shorten the window, drop a symbol, or use "
                "one cadence.",
            )
        # Coverage is checked before the run is created: a real backtest that
        # cannot be real should fail the request, not fail an hour in.
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
                "replay_live_run_id": spec.replay_live_run_id,
                # Recorded because the failure count is meaningless without it:
                # otherwise nothing says whether a run that lost decisions ran
                # with the probe's number or the global default.
                "timeout_seconds": spec.timeout_seconds,
                "timeout_source": (
                    "explicit"
                    if request.timeout_seconds is not None
                    else "provider_config"
                    if spec.timeout_seconds is not None
                    else "not_applicable"
                ),
                "estimate": estimate_payload,
            },
            list(spec.providers),
        )
        backtest_tasks[run_id] = asyncio.create_task(
            _run_backtest(run_id, spec, captures), name=f"candlepilot-backtest-{run_id}"
        )
        return {"id": run_id, "status": "running", "estimate": estimate_payload}

    @app.get("/api/backtests")
    async def list_backtests(limit: int = 20) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise HTTPException(
                status_code=422, detail="limit must be between 1 and 100"
            )
        return [
            _json_value(item) for item in await engine.audit.recent_backtest_runs(limit)
        ]

    @app.get("/api/backtests/{run_id}")
    async def get_backtest(run_id: int) -> dict[str, Any]:
        run = await engine.audit.backtest_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="backtest not found")
        return _json_value(run)

    @app.get("/api/backtests/{run_id}/decisions")
    async def get_backtest_decisions(
        run_id: int,
        provider: str | None = None,
        after_id: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Every decision one model made, in order.

        The run's totals cannot say why a model made no trades; these can.
        """

        if await engine.audit.backtest_run(run_id) is None:
            raise HTTPException(status_code=404, detail="backtest not found")
        if after_id < 0:
            raise HTTPException(status_code=422, detail="after_id must not be negative")
        if not 1 <= limit <= 500:
            raise HTTPException(
                status_code=422, detail="limit must be between 1 and 500"
            )
        rows, total = await engine.audit.backtest_decisions(
            run_id,
            provider=provider,
            after_id=after_id,
            limit=limit,
        )
        has_more = len(rows) > limit
        items = rows[:limit]
        return _json_value(
            {
                "items": items,
                "total": total,
                "has_more": has_more,
                "next_after_id": items[-1]["id"] if has_more and items else None,
            }
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

        required = sorted(
            {
                when
                for cadence in spec.cadences
                for when in decision_times(spec, cadence)
            }
        )
        required_set = set(required)
        captures: dict[str, dict[datetime, dict[str, Any]]] = {}
        for symbol in spec.symbols:
            rows = await engine.audit.book_captures(symbol, spec.start, spec.end)
            by_time = {
                row["captured_at"]: row
                for row in rows
                if row["captured_at"] in required_set
            }
            stale = {
                row["schema_version"]
                for row in by_time.values()
                if row["schema_version"] != MICROSTRUCTURE_SCHEMA_VERSION
            }
            if stale:
                raise HTTPException(
                    status_code=409,
                    detail=f"{symbol} has captures recorded as {', '.join(sorted(stale))} "
                    f"but this build derives {MICROSTRUCTURE_SCHEMA_VERSION}; those numbers "
                    "no longer mean the same thing and cannot be replayed",
                )
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
        origin = websocket.headers.get("origin")
        if origin and urlsplit(origin).netloc != websocket.headers.get("host"):
            await websocket.close(code=4403, reason="cross-site websocket denied")
            return
        if (
            auth.enabled
            and auth.validate_session(websocket.cookies.get(SESSION_COOKIE)) is None
        ):
            await websocket.close(code=4401, reason="authentication required")
            return
        await websocket.accept()
        last_decisions: list[dict[str, Any]] | None = None
        try:
            while True:
                await websocket.send_json(
                    {"type": "status", "data": _status(engine, scheduler)}
                )
                decisions = await engine.audit.recent_decision_events(run_limit=10)
                if decisions != last_decisions:
                    await websocket.send_json(
                        {"type": "decisions", "data": _json_value(decisions)}
                    )
                    last_decisions = decisions
                await asyncio.sleep(2)
        except (WebSocketDisconnect, RuntimeError):
            return

    frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if frontend_dist.is_dir():
        app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="console")

    return app
