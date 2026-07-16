from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

from candlepilot.broker.binance_testnet import (
    AccountReconciliationError,
    BinanceApiError,
    BinanceTestnetBroker,
    OrderStatusUnknown,
    ProtectiveStopError,
    ReconciliationReport,
)
from candlepilot.domain.models import (
    ExecutionAttempt,
    ExecutionReport,
    MarketSnapshot,
    PortfolioState,
    ProviderHealth,
    RiskDecision,
    TradeAction,
    TradeIntent,
    TradingMode,
)
from candlepilot.execution.paper import PaperExecutor
from candlepilot.market.binance import BinancePublicClient
from candlepilot.market.scanner import Candidate, MarketScanner
from candlepilot.providers.base import ProviderResult
from candlepilot.providers.cli import ProviderError, ProviderInvocationError
from candlepilot.providers.registry import ProviderRegistry
from candlepilot.risk.engine import AggressiveRiskPolicy, SymbolRules
from candlepilot.storage.database import AuditRepository


SUPPORTED_CADENCES: tuple[str, ...] = ("5m", "15m", "30m")
PROVIDER_FAILURE_COOLDOWN = timedelta(seconds=60)


@dataclass(frozen=True, slots=True)
class DecisionOutcome:
    intent: TradeIntent
    risk: RiskDecision
    execution: ExecutionReport | None
    provider: str


@dataclass(slots=True)
class ProviderRouteState:
    consecutive_failures: int = 0
    cooldown_until: datetime | None = None
    last_error: str | None = None
    last_failed_at: datetime | None = None
    last_success_at: datetime | None = None


class TradingEngine:
    def __init__(
        self,
        *,
        mode: TradingMode,
        providers: ProviderRegistry,
        audit: AuditRepository,
        market: BinancePublicClient,
        scanner: MarketScanner | None = None,
        risk: AggressiveRiskPolicy | None = None,
        paper_executor: PaperExecutor | None = None,
        testnet_broker: BinanceTestnetBroker | None = None,
        cadences: tuple[str, ...] | None = None,
    ) -> None:
        self.mode = mode
        self.providers = providers
        self.audit = audit
        self.market = market
        self.scanner = scanner or MarketScanner()
        self.risk = risk or AggressiveRiskPolicy(require_take_profit=mode == TradingMode.TESTNET)
        self.paper_executor = paper_executor or PaperExecutor(state_store=audit)
        self.testnet_broker = testnet_broker
        self.selected_provider: str | None = None
        self.backup_provider: str | None = None
        self.provider_chain: tuple[str, ...] = ()
        self.active_provider: str | None = None
        self._provider_route_states: dict[str, ProviderRouteState] = {}
        self.active_cadences: tuple[str, ...] = self._normalize_cadences(
            cadences if cadences is not None else SUPPORTED_CADENCES
        )
        self.running = False
        self.emergency_locked = False
        self.emergency_locked_until: datetime | None = None
        self.testnet_reconciliation: ReconciliationReport | None = None
        self.candidates: list[Candidate] = []
        self.universe_refreshed_at: datetime | None = None
        self.run_started_at: datetime | None = None
        self.run_ended_at: datetime | None = None
        self.run_start_inference_id: int | None = None
        self.run_end_inference_id: int | None = None

    async def provider_health(self) -> list[ProviderHealth]:
        return await self.providers.health()

    def select_provider(self, name: str, backup: str | None = None) -> None:
        self.select_provider_chain([name, *([backup] if backup is not None else [])])

    def select_provider_chain(self, providers: tuple[str, ...] | list[str]) -> None:
        if self.running:
            raise RuntimeError("cannot change provider route while running")
        if not providers:
            raise ValueError("provider route must contain at least one provider")
        ordered = tuple(providers)
        if len(set(ordered)) != len(ordered):
            raise ValueError("provider route cannot contain duplicates")
        for name in ordered:
            self.providers.get(name)
        self.provider_chain = ordered
        self.selected_provider = ordered[0]
        self.backup_provider = ordered[1] if len(ordered) > 1 else None
        self.active_provider = None
        self._provider_route_states = {
            name: self._provider_route_states.get(name, ProviderRouteState())
            for name in ordered
        }

    def provider_route_status(self, *, now: datetime | None = None) -> list[dict[str, object]]:
        now = now or datetime.now(UTC)
        statuses: list[dict[str, object]] = []
        for priority, name in enumerate(self.provider_chain, start=1):
            route = self._provider_route_states.setdefault(name, ProviderRouteState())
            cooling = route.cooldown_until is not None and route.cooldown_until > now
            state = "active" if name == self.active_provider else "cooldown" if cooling else "standby"
            statuses.append(
                {
                    "provider": name,
                    "priority": priority,
                    "state": state,
                    "consecutive_failures": route.consecutive_failures,
                    "cooldown_until": route.cooldown_until.isoformat()
                    if route.cooldown_until
                    else None,
                    "last_error": route.last_error,
                    "last_failed_at": route.last_failed_at.isoformat()
                    if route.last_failed_at
                    else None,
                    "last_success_at": route.last_success_at.isoformat()
                    if route.last_success_at
                    else None,
                }
            )
        return statuses

    @staticmethod
    def _normalize_cadences(cadences: tuple[str, ...] | list[str]) -> tuple[str, ...]:
        requested = set(cadences)
        invalid = requested - set(SUPPORTED_CADENCES)
        if invalid:
            raise ValueError(f"unsupported cadences: {', '.join(sorted(invalid))}")
        chosen = tuple(cadence for cadence in SUPPORTED_CADENCES if cadence in requested)
        if not chosen:
            raise ValueError("at least one cadence must be selected")
        return chosen

    def select_cadences(self, cadences: tuple[str, ...] | list[str]) -> None:
        if self.running:
            raise RuntimeError("cannot change cadences while running")
        self.active_cadences = self._normalize_cadences(cadences)

    async def start(self) -> None:
        if self.running:
            raise RuntimeError("engine is already running")
        await self.restore_runtime_state()
        if self.emergency_locked:
            raise RuntimeError("engine is emergency locked")
        if not self.provider_chain:
            raise RuntimeError("at least one authenticated LLM provider must be selected")
        if self.mode == TradingMode.TESTNET and self.testnet_broker is None:
            raise RuntimeError("Binance testnet credentials are not configured")
        if self.mode == TradingMode.TESTNET and self.testnet_broker is not None:
            report = await self.testnet_broker.reconcile_account()
            self.testnet_reconciliation = report
            if report.unprotected_symbols:
                symbols = ", ".join(report.unprotected_symbols)
                raise AccountReconciliationError(
                    f"testnet positions lack protective stops: {symbols}"
                )
        health_results = await asyncio.gather(
            *(self.providers.get(name).health_check() for name in self.provider_chain),
            return_exceptions=True,
        )
        ready: list[str] = []
        failures: list[str] = []
        checked_at = datetime.now(UTC)
        for name, health in zip(self.provider_chain, health_results, strict=True):
            state = self._provider_route_states[name]
            if isinstance(health, BaseException):
                detail = type(health).__name__
                failures.append(f"{name}: {detail}")
                state.last_error = detail
                state.last_failed_at = checked_at
                state.cooldown_until = checked_at + PROVIDER_FAILURE_COOLDOWN
            elif health.available and health.authenticated:
                ready.append(name)
                state.consecutive_failures = 0
                state.cooldown_until = None
                state.last_error = None
            else:
                failures.append(f"{name}: {health.detail}")
                state.last_error = health.detail
                state.last_failed_at = checked_at
                state.cooldown_until = checked_at + PROVIDER_FAILURE_COOLDOWN
        if not ready:
            raise RuntimeError(f"no provider in route is ready: {'; '.join(failures)}")
        self.active_provider = ready[0]
        self.run_start_inference_id = await self.audit.latest_inference_id()
        self.run_end_inference_id = None
        self.run_started_at = datetime.now(UTC)
        self.run_ended_at = None
        self.running = True

    async def stop(self) -> None:
        if self.running:
            self.run_end_inference_id = await self.audit.latest_inference_id()
            self.run_ended_at = datetime.now(UTC)
        self.running = False

    async def emergency_stop(self, *, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        if now.tzinfo is None:
            raise ValueError("emergency stop time must be timezone-aware")
        if self.running:
            self.run_end_inference_id = await self.audit.latest_inference_id()
            self.run_ended_at = now
        self.running = False
        self.emergency_locked = True
        tomorrow = now.astimezone(UTC).date() + timedelta(days=1)
        self.emergency_locked_until = datetime.combine(tomorrow, time.min, tzinfo=UTC)
        await self.audit.set_runtime_state(
            "emergency_locked_until", self.emergency_locked_until.isoformat()
        )
        if self.mode == TradingMode.TESTNET and self.testnet_broker is not None:
            await self.testnet_broker.emergency_flatten()
        else:
            await self.paper_executor.emergency_flatten()

    async def clear_emergency_lock(self) -> None:
        if self.running:
            raise RuntimeError("cannot clear emergency lock while running")
        self.emergency_locked = False
        self.emergency_locked_until = None
        await self.audit.delete_runtime_state("emergency_locked_until")

    async def restore_runtime_state(self, *, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        await self.paper_executor.restore()
        stored = await self.audit.get_runtime_state("emergency_locked_until")
        if stored is None:
            self.emergency_locked = False
            self.emergency_locked_until = None
            return
        locked_until = datetime.fromisoformat(stored)
        if locked_until.tzinfo is None:
            locked_until = locked_until.replace(tzinfo=UTC)
        if now >= locked_until:
            await self.audit.delete_runtime_state("emergency_locked_until")
            self.emergency_locked = False
            self.emergency_locked_until = None
            return
        self.emergency_locked = True
        self.emergency_locked_until = locked_until

    async def refresh_universe(self) -> list[Candidate]:
        inputs = await self.market.candidate_inputs()
        self.candidates = self.scanner.scan(inputs)
        self.universe_refreshed_at = datetime.now(UTC)
        return self.candidates

    async def current_portfolio(self) -> PortfolioState:
        if self.mode in {TradingMode.PAPER, TradingMode.BACKTEST}:
            return self.paper_executor.portfolio_state()
        broker = self.testnet_broker
        if broker is None:
            raise RuntimeError("testnet broker is unavailable")
        account = await broker.account()
        positions = {
            str(item["symbol"]): item
            for item in account.get("positions", [])
            if Decimal(str(item.get("positionAmt", "0"))) != 0
        }
        return PortfolioState(
            equity=account.get("totalMarginBalance", account.get("totalWalletBalance", "0")),
            available_balance=account.get("availableBalance", "0"),
            open_positions=len(positions),
            margin_used=account.get("totalInitialMargin", "0"),
            symbol_sides={
                symbol: "LONG" if Decimal(str(item["positionAmt"])) > 0 else "SHORT"
                for symbol, item in positions.items()
            },
            symbol_quantities={
                symbol: abs(Decimal(str(item["positionAmt"]))) for symbol, item in positions.items()
            },
        )

    async def evaluate(
        self,
        snapshot: MarketSnapshot,
        portfolio: PortfolioState,
        rules: SymbolRules,
    ) -> DecisionOutcome:
        if not self.running or not self.provider_chain:
            raise RuntimeError("engine is not running")
        now = datetime.now(UTC)
        candidates = [
            name
            for name in self.provider_chain
            if (state := self._provider_route_states.setdefault(name, ProviderRouteState())).cooldown_until is None
            or state.cooldown_until <= now
        ]
        if not candidates:
            # Avoid dropping an entire decision cycle when every route is cooling down.
            candidates = [
                min(
                    self.provider_chain,
                    key=lambda name: self._provider_route_states[name].cooldown_until
                    or datetime.min.replace(tzinfo=UTC),
                )
            ]

        failed_results: list[ProviderResult] = []
        result: ProviderResult | None = None
        for position, name in enumerate(candidates, start=1):
            provider = self.providers.get(name)
            try:
                result = await provider.generate_trade_intent(snapshot, portfolio)
            except ProviderError as exc:
                failed_at = datetime.now(UTC)
                state = self._provider_route_states[name]
                state.consecutive_failures += 1
                state.cooldown_until = failed_at + PROVIDER_FAILURE_COOLDOWN
                state.last_error = str(exc)
                state.last_failed_at = failed_at
                failed_results.append(
                    self._provider_failure_result(
                        provider_name=name,
                        provider=provider,
                        error=exc,
                        snapshot=snapshot,
                        portfolio=portfolio,
                        route_position=self.provider_chain.index(name) + 1,
                        failover_continues=position < len(candidates),
                    )
                )
                continue
            state = self._provider_route_states[name]
            state.consecutive_failures = 0
            state.cooldown_until = None
            state.last_error = None
            state.last_success_at = datetime.now(UTC)
            self.active_provider = name
            break

        if result is not None:
            for failed_result in failed_results:
                await self.audit.record_inference(failed_result)
        else:
            self.active_provider = None
            if not failed_results:
                raise RuntimeError("no provider route was attempted")
            for failed_result in failed_results[:-1]:
                await self.audit.record_inference(failed_result)
            result = failed_results[-1]
        inference_id = await self.audit.record_inference(result)
        evaluation_snapshot = snapshot
        evaluation_portfolio = portfolio
        intent_matches_snapshot = (
            result.intent.symbol == snapshot.symbol and result.intent.cadence == snapshot.cadence
        )
        if intent_matches_snapshot and result.intent.action != TradeAction.HOLD:
            analysis_age = (datetime.now(UTC) - snapshot.timestamp).total_seconds()
            if analysis_age < -2 or analysis_age > self.risk.max_snapshot_age_seconds:
                rejection = RiskDecision(
                    accepted=False,
                    reason="analysis snapshot expired before pre-trade refresh",
                )
                await self.audit.record_risk(snapshot.symbol, rejection, inference_id=inference_id)
                return DecisionOutcome(
                    intent=result.intent,
                    risk=rejection,
                    execution=None,
                    provider=result.provider,
                )
            try:
                evaluation_snapshot = await self.market.market_snapshot(
                    snapshot.symbol, snapshot.cadence
                )
                if self.mode in {TradingMode.PAPER, TradingMode.BACKTEST}:
                    protective_reports = await self.paper_executor.mark_to_market(
                        evaluation_snapshot
                    )
                    for report in protective_reports:
                        await self.audit.record_execution(snapshot.symbol, report)
                evaluation_portfolio = await self.current_portfolio()
            except Exception as exc:
                rejection = RiskDecision(
                    accepted=False,
                    reason=f"pre-trade refresh failed: {type(exc).__name__}",
                )
                await self.audit.record_risk(snapshot.symbol, rejection, inference_id=inference_id)
                return DecisionOutcome(
                    intent=result.intent,
                    risk=rejection,
                    execution=None,
                    provider=result.provider,
                )

        evaluation = self.risk.evaluate(
            result.intent,
            evaluation_snapshot,
            evaluation_portfolio,
            rules,
        )
        await self.audit.record_risk(
            snapshot.symbol, evaluation.decision, inference_id=inference_id
        )
        execution = None
        if evaluation.order is not None and evaluation.decision.accepted:
            try:
                if self.mode == TradingMode.TESTNET:
                    if self.testnet_broker is None:
                        raise RuntimeError("Binance testnet broker is unavailable")
                    execution = await self.testnet_broker.execute_with_stop(
                        evaluation.order,
                        leverage=result.intent.leverage,
                        replace_existing_protection=result.intent.action == TradeAction.ADD,
                    )
                else:
                    execution = await self.paper_executor.execute(
                        evaluation.order,
                        evaluation_snapshot,
                        leverage=result.intent.leverage,
                    )
            except ProtectiveStopError as exc:
                await self.audit.record_execution(snapshot.symbol, exc.entry)
                if exc.rescue is not None:
                    await self.audit.record_execution(snapshot.symbol, exc.rescue)
                await self.audit.record_execution_attempt(
                    snapshot.symbol,
                    ExecutionAttempt(
                        inference_id=inference_id,
                        client_order_id=evaluation.order.client_order_id,
                        status="RESCUED" if exc.rescue is not None else "FAILED",
                        stage=exc.failed_stage,
                        message=str(exc),
                        exchange_error_code=exc.exchange_error_code,
                        entry_report=exc.entry,
                        rescue_report=exc.rescue,
                        estimated_loss_usdt=exc.estimated_loss_usdt,
                    ),
                )
                if exc.requires_emergency_lock:
                    await self.emergency_stop()
            except Exception as exc:
                execution_status = (
                    "UNKNOWN"
                    if isinstance(exc, (TimeoutError, OrderStatusUnknown))
                    else "FAILED"
                )
                await self.audit.record_execution_attempt(
                    snapshot.symbol,
                    ExecutionAttempt(
                        inference_id=inference_id,
                        client_order_id=evaluation.order.client_order_id,
                        status=execution_status,
                        stage="ENTRY",
                        message=f"{type(exc).__name__}: {exc}",
                        exchange_error_code=exc.code
                        if isinstance(exc, BinanceApiError)
                        else None,
                    ),
                )
                if execution_status == "UNKNOWN":
                    await self.emergency_stop()
            else:
                await self.audit.record_execution(snapshot.symbol, execution)
                await self.audit.record_execution_attempt(
                    snapshot.symbol,
                    ExecutionAttempt(
                        inference_id=inference_id,
                        client_order_id=evaluation.order.client_order_id,
                        status="SUCCEEDED",
                        stage="COMPLETE",
                        message="entry accepted and required execution checks completed",
                        entry_report=execution,
                    ),
                )
        return DecisionOutcome(
            intent=result.intent,
            risk=evaluation.decision,
            execution=execution,
            provider=result.provider,
        )

    @staticmethod
    def _provider_failure_result(
        *,
        provider_name: str,
        provider: object,
        error: ProviderError,
        snapshot: MarketSnapshot,
        portfolio: PortfolioState,
        route_position: int,
        failover_continues: bool,
    ) -> ProviderResult:
        diagnostics = error if isinstance(error, ProviderInvocationError) else None
        usage = dict(diagnostics.usage) if diagnostics else {}
        usage.update(
            {
                "error": type(error).__name__,
                "error_message": str(error),
                "failover_attempt": True,
                "route_position": route_position,
                "failover_continues": failover_continues,
            }
        )
        rationale = (
            f"provider attempt failed at route position {route_position}; "
            f"{'continuing failover' if failover_continues else 'no provider succeeded'}: {error}"
        )
        return ProviderResult(
            intent=TradeIntent.hold(snapshot.symbol, snapshot.cadence, rationale[:2000]),
            provider=provider_name,
            model=diagnostics.model if diagnostics else getattr(provider, "model", None),
            duration=diagnostics.duration if diagnostics else timedelta(0),
            raw_output=diagnostics.raw_output if diagnostics else str(error),
            usage=usage,
            prompt_version=diagnostics.prompt_version if diagnostics else None,
            data_version=diagnostics.data_version if diagnostics else None,
            provider_version=diagnostics.provider_version if diagnostics else None,
            input_payload=diagnostics.input_payload
            if diagnostics
            else {
                "market": snapshot.model_dump(mode="json"),
                "portfolio": portfolio.model_dump(mode="json"),
            },
            prompt=diagnostics.prompt if diagnostics else None,
            reasoning_effort=getattr(provider, "reasoning_effort", None),
        )
