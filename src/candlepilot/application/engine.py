from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

from candlepilot.broker.binance_testnet import (
    AccountReconciliationError,
    BinanceApiError,
    BinanceTestnetBroker,
    OrderStatusUnknown,
    ProtectiveLevels,
    ProtectiveStopError,
    ReconciliationReport,
)
from candlepilot.domain.models import (
    DEFAULT_DECISION_CADENCE,
    SUPPORTED_CADENCES,
    ExecutionAttempt,
    ExecutionReport,
    MarketSnapshot,
    OrderPlan,
    PortfolioState,
    PositionState,
    ProviderHealth,
    RiskDecision,
    TradeAction,
    TradeIntent,
)
from candlepilot.market.binance import BinancePublicClient
from candlepilot.market.scanner import Candidate, MarketScanner
from candlepilot.providers.base import ProviderResult
from candlepilot.providers.cli import ProviderError, ProviderInvocationError
from candlepilot.providers.registry import ProviderRegistry
from candlepilot.providers.retry import (
    DECISION_PROVIDER_MAX_ATTEMPTS,
    DECISION_PROVIDER_RETRY_DELAYS,
    validate_retry_delays,
)
from candlepilot.risk.engine import AggressiveRiskPolicy, RiskEvaluation, SymbolRules
from candlepilot.storage.database import AuditRepository


PROVIDER_FAILURE_COOLDOWN = timedelta(seconds=60)
MAX_RESCUES_PER_RUN = 3


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
        providers: ProviderRegistry,
        audit: AuditRepository,
        market: BinancePublicClient,
        testnet_broker: BinanceTestnetBroker,
        scanner: MarketScanner | None = None,
        risk: AggressiveRiskPolicy | None = None,
        cadences: tuple[str, ...] | None = None,
        provider_retry_delays: tuple[float, ...] | None = None,
        retry_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.providers = providers
        self.audit = audit
        self.market = market
        self.scanner = scanner or MarketScanner()
        # The exchange brackets every entry, so a take profit is not optional.
        self.risk = risk or AggressiveRiskPolicy(require_take_profit=True)
        self.testnet_broker = testnet_broker
        self.provider_chain: tuple[str, ...] = ()
        self.active_provider: str | None = None
        self._provider_route_states: dict[str, ProviderRouteState] = {}
        self._provider_route_lock = asyncio.Lock()
        self._provider_retry_delays = validate_retry_delays(
            DECISION_PROVIDER_RETRY_DELAYS
            if provider_retry_delays is None
            else provider_retry_delays
        )
        self._retry_sleep = retry_sleep
        self.active_cadences: tuple[str, ...] = self._normalize_cadences(
            cadences if cadences is not None else (DEFAULT_DECISION_CADENCE,)
        )
        self.running = False
        self.emergency_locked = False
        self.emergency_locked_until: datetime | None = None
        self.testnet_reconciliation: ReconciliationReport | None = None
        self.candidates: list[Candidate] = []
        self.venue_excluded_symbols: tuple[str, ...] = ()
        self.venue_contract_rules: dict[str, SymbolRules] | None = None
        self.universe_refreshed_at: datetime | None = None
        self.run_started_at: datetime | None = None
        self.run_ended_at: datetime | None = None
        self.run_start_inference_id: int | None = None
        self.run_end_inference_id: int | None = None
        self.live_run_id: int | None = None
        self.route_failure_count = 0
        self.rescue_count = 0
        self.max_run_seconds: int | None = None
        self.max_run_cost_usd: float | None = None
        self.auto_stop_reason: str | None = None
        self.decision_timeout_seconds: float | None = None
        self.startup_probe: dict[str, object] | None = None
        self._provider_timeout_restore: dict[str, float] = {}

    def invalidate_startup_probe(self, reason: str) -> None:
        """Keep the last result visible, but prevent it from starting a changed run."""

        if self.startup_probe is None or self.startup_probe.get("running"):
            return
        self.startup_probe["ready"] = False
        self.startup_probe["invalidated_reason"] = reason

    async def provider_health(self) -> list[ProviderHealth]:
        return await self.providers.health()

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
        changed = ordered != self.provider_chain
        self.provider_chain = ordered
        self.active_provider = None
        self._provider_route_states = {
            name: self._provider_route_states.get(name, ProviderRouteState())
            for name in ordered
        }
        if changed:
            self.invalidate_startup_probe("provider route changed")

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
            raise ValueError("exactly one analysis cadence must be selected")
        if len(chosen) != 1:
            raise ValueError("exactly one analysis cadence must be selected")
        return chosen

    def select_cadences(self, cadences: tuple[str, ...] | list[str]) -> None:
        if self.running:
            raise RuntimeError("cannot change cadences while running")
        selected = self._normalize_cadences(cadences)
        if selected != self.active_cadences:
            self.active_cadences = selected
            self.invalidate_startup_probe("analysis cadences changed")

    def select_run_limits(
        self,
        *,
        max_run_seconds: int | None,
        max_run_cost_usd: float | None,
    ) -> None:
        """Bound the next run by wall-clock time and/or equivalent model cost.

        Either limit may be ``None`` to leave that dimension unbounded; whichever
        limit is reached first stops the run.
        """

        if self.running:
            raise RuntimeError("cannot change run limits while running")
        if max_run_seconds is not None and max_run_seconds <= 0:
            raise ValueError("max_run_seconds must be positive")
        if max_run_cost_usd is not None and max_run_cost_usd <= 0:
            raise ValueError("max_run_cost_usd must be positive")
        if (
            max_run_seconds != self.max_run_seconds
            or max_run_cost_usd != self.max_run_cost_usd
        ):
            self.max_run_seconds = max_run_seconds
            self.max_run_cost_usd = max_run_cost_usd
            self.invalidate_startup_probe("run limits changed")

    def configure_decision_timeout(self, seconds: float | None) -> None:
        """Freeze one absolute external-provider timeout for the next live run."""

        if self.running:
            raise RuntimeError("cannot change decision timeout while running")
        self.restore_provider_timeouts()
        self.decision_timeout_seconds = seconds
        if seconds is None:
            return
        if seconds <= 0:
            raise ValueError("decision timeout must be positive")
        for name in self.provider_chain:
            provider = self.providers.get(name)
            if not provider.capabilities.external_inference:
                continue
            self._provider_timeout_restore[name] = provider.timeout
            provider.timeout = seconds

    def restore_provider_timeouts(self) -> None:
        for name, timeout in self._provider_timeout_restore.items():
            self.providers.get(name).timeout = timeout
        self._provider_timeout_restore.clear()
        self.decision_timeout_seconds = None

    def evaluate_stop_reason(
        self,
        *,
        now: datetime | None = None,
        run_cost_usd: float | None = None,
    ) -> str | None:
        """Return why the current run should stop, or ``None`` to keep running."""

        if not self.running:
            return None
        now = now or datetime.now(UTC)
        if self.rescue_count >= MAX_RESCUES_PER_RUN:
            return (
                f"本次运行累计紧急回补 {self.rescue_count} 次，"
                f"达到安全上限 {MAX_RESCUES_PER_RUN} 次"
            )
        if self.max_run_seconds is not None and self.run_started_at is not None:
            elapsed = (now - self.run_started_at).total_seconds()
            if elapsed >= self.max_run_seconds:
                return f"run duration limit reached ({self.max_run_seconds}s)"
        if self.max_run_cost_usd is not None and run_cost_usd is not None:
            if run_cost_usd >= self.max_run_cost_usd:
                return (
                    f"run cost budget reached (${run_cost_usd:.4f} of "
                    f"${self.max_run_cost_usd:.4f})"
                )
        if self.route_failure_count >= DECISION_PROVIDER_MAX_ATTEMPTS:
            return (
                "every provider in the route failed for "
                f"{self.route_failure_count} consecutive attempts"
            )
        return None

    async def start(self, *, run_config: Mapping[str, object] | None = None) -> None:
        if self.running:
            raise RuntimeError("engine is already running")
        await self.restore_runtime_state()
        if self.emergency_locked:
            raise RuntimeError("engine is emergency locked")
        if not self.provider_chain:
            raise RuntimeError("at least one ready decision provider must be selected")
        await self.cancel_pending_entries(
            "pending limit intent cancelled because its originating run is no longer active"
        )
        report = await self.testnet_broker.reconcile_account()
        self.testnet_reconciliation = report
        if report.unprotected_symbols:
            symbols = ", ".join(report.unprotected_symbols)
            raise AccountReconciliationError(
                f"testnet positions lack protective stops: {symbols}"
            )
        if report.pending_entry_symbols:
            symbols = ", ".join(report.pending_entry_symbols)
            raise AccountReconciliationError(
                f"testnet account has pending entry orders: {symbols}"
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
        config: dict[str, object] = {
            "provider_chain": list(self.provider_chain),
            "cadences": list(self.active_cadences),
            "decision_timeout_seconds": self.decision_timeout_seconds,
            "max_run_seconds": self.max_run_seconds,
            "max_run_cost_usd": self.max_run_cost_usd,
            "rescue_limit": MAX_RESCUES_PER_RUN,
        }
        if run_config is not None:
            config.update(run_config)
        self.live_run_id = await self.audit.create_live_run(config)
        self.route_failure_count = 0
        self.rescue_count = 0
        self.auto_stop_reason = None
        self.running = True

    async def stop(self, *, reason: str | None = None) -> None:
        if self.running:
            await self.cancel_pending_entries(
                "pending limit intent cancelled because the run stopped",
                live_run_id=self.live_run_id,
            )
            self.run_end_inference_id = await self.audit.latest_inference_id()
            self.run_ended_at = datetime.now(UTC)
            stop_reason = reason or self.auto_stop_reason or "stopped by user"
            status = "auto_stopped" if self.auto_stop_reason else "stopped"
            if self.live_run_id is not None:
                await self.audit.finish_live_run(
                    self.live_run_id,
                    status=status,
                    stop_reason=stop_reason,
                    ended_at=self.run_ended_at,
                )
        self.running = False
        self.restore_provider_timeouts()

    async def emergency_stop(
        self,
        *,
        now: datetime | None = None,
        reason: str | None = None,
    ) -> None:
        now = now or datetime.now(UTC)
        if now.tzinfo is None:
            raise ValueError("emergency stop time must be timezone-aware")
        if self.running:
            await self.cancel_pending_entries(
                "pending limit intent cancelled by emergency stop",
                live_run_id=self.live_run_id,
            )
            self.run_end_inference_id = await self.audit.latest_inference_id()
            self.run_ended_at = now
            if self.live_run_id is not None:
                await self.audit.finish_live_run(
                    self.live_run_id,
                    status="emergency_stopped",
                    stop_reason=reason
                    or self.auto_stop_reason
                    or "emergency stop requested",
                    ended_at=now,
                )
        self.running = False
        self.restore_provider_timeouts()
        self.emergency_locked = True
        tomorrow = now.astimezone(UTC).date() + timedelta(days=1)
        self.emergency_locked_until = datetime.combine(tomorrow, time.min, tzinfo=UTC)
        await self.audit.set_runtime_state(
            "emergency_locked_until", self.emergency_locked_until.isoformat()
        )
        await self.testnet_broker.emergency_flatten()

    async def clear_emergency_lock(self) -> None:
        if self.running:
            raise RuntimeError("无法解除紧急锁：引擎仍在运行")
        try:
            report = await self.testnet_broker.reconcile_account()
        except Exception as exc:
            raise AccountReconciliationError(
                "无法解除紧急锁：测试网账户安全检查失败"
            ) from exc
        self.testnet_reconciliation = report
        blockers: list[str] = []
        if report.position_symbols:
            blockers.append(f"仍有持仓：{', '.join(report.position_symbols)}")
        if report.open_order_count:
            blockers.append(f"仍有挂单：{report.open_order_count}")
        if blockers:
            raise AccountReconciliationError(
                f"无法解除紧急锁：{'；'.join(blockers)}"
            )
        self.emergency_locked = False
        self.emergency_locked_until = None
        await self.audit.delete_runtime_state("emergency_locked_until")

    async def restore_runtime_state(self, *, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
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
        rules_loader = getattr(self.testnet_broker, "tradable_contract_rules", None)
        venue_loader = getattr(self.testnet_broker, "tradable_symbols", None)
        if callable(rules_loader):
            self.venue_contract_rules = await rules_loader()
            venue_symbols = frozenset(self.venue_contract_rules)
        elif callable(venue_loader):
            self.venue_contract_rules = None
            venue_symbols = await venue_loader()
        else:
            self.venue_contract_rules = None
            venue_symbols = None
        if venue_symbols is not None:
            production_symbols = {item.symbol for item in inputs}
            self.venue_excluded_symbols = tuple(
                sorted(production_symbols.difference(venue_symbols))
            )
            inputs = [item for item in inputs if item.symbol in venue_symbols]
        else:
            self.venue_excluded_symbols = ()
        self.candidates = self.scanner.scan(inputs)
        self.universe_refreshed_at = datetime.now(UTC)
        return self.candidates

    async def current_portfolio(self) -> PortfolioState:
        broker = self.testnet_broker
        income_24h_loader = getattr(broker, "income_24h", None)
        income_24h = (
            income_24h_loader()
            if callable(income_24h_loader)
            else asyncio.sleep(0, result=Decimal("0"))
        )
        pending_loader = getattr(broker, "pending_entry_symbols", None)
        pending_entries = (
            pending_loader()
            if callable(pending_loader)
            else asyncio.sleep(0, result=())
        )
        snapshot_loader = getattr(broker, "account_snapshot", None)
        account_loader = snapshot_loader if callable(snapshot_loader) else broker.account
        account, levels, realized_24h, pending_entry_symbols = await asyncio.gather(
            account_loader(), broker.protective_levels(), income_24h, pending_entries
        )
        raw_positions = {
            str(item["symbol"]): item
            for item in account.get("positions", [])
            if Decimal(str(item.get("positionAmt", "0"))) != 0
        }
        positions: dict[str, PositionState] = {}
        for symbol, item in raw_positions.items():
            amount = Decimal(str(item["positionAmt"]))
            entry_price = item.get("entryPrice")
            if entry_price is None:
                raise AccountReconciliationError(
                    f"position risk response is missing entry price for {symbol}"
                )
            guard = levels.get(symbol, ProtectiveLevels())
            leverage = int(item.get("leverage", 1))
            initial_margin = item.get(
                "positionInitialMargin", item.get("initialMargin")
            )
            if initial_margin is None:
                mark_price = Decimal(str(item.get("markPrice", entry_price)))
                initial_margin = abs(amount) * mark_price / leverage
            positions[symbol] = PositionState(
                side="LONG" if amount > 0 else "SHORT",
                quantity=abs(amount),
                entry_price=Decimal(str(entry_price)),
                unrealized_pnl=Decimal(str(item.get("unrealizedProfit", "0"))),
                leverage=leverage,
                initial_margin=Decimal(str(initial_margin)),
                stop_loss=guard.stop_loss,
                take_profit=guard.take_profit,
            )
        account_unrealized = account.get("totalUnrealizedProfit")
        unrealized_pnl = (
            Decimal(str(account_unrealized))
            if account_unrealized is not None
            else sum((position.unrealized_pnl for position in positions.values()), Decimal("0"))
        )
        local_pending_symbols = {
            item["intent"].symbol
            for item in await self.audit.pending_local_entries(
                live_run_id=self.live_run_id if self.running else None
            )
        }
        return PortfolioState(
            equity=account.get("totalMarginBalance", account.get("totalWalletBalance", "0")),
            available_balance=account.get("availableBalance", "0"),
            pnl_24h=Decimal(str(realized_24h)) + unrealized_pnl,
            open_positions=len(positions),
            margin_used=account.get("totalInitialMargin", "0"),
            positions=positions,
            pending_entry_symbols=tuple(
                dict.fromkeys((*pending_entry_symbols, *sorted(local_pending_symbols)))
            ),
        )

    async def pending_entry_symbols(self) -> tuple[str, ...]:
        entries = await self.audit.pending_local_entries(live_run_id=self.live_run_id)
        return tuple(dict.fromkeys(item["intent"].symbol for item in entries))

    async def cancel_pending_entries(
        self,
        reason: str,
        *,
        live_run_id: int | None = None,
    ) -> int:
        entries = await self.audit.pending_local_entries(live_run_id=live_run_id)
        for item in entries:
            decision = item["decision"].model_copy(
                update={
                    "accepted": False,
                    "reason": reason,
                    "pending_entry": False,
                    "evaluated_at": datetime.now(UTC),
                }
            )
            await self.audit.update_risk(
                item["inference_id"], decision, completed=True
            )
        return len(entries)

    async def process_pending_entry(self, symbol: str) -> str | None:
        entries = [
            item
            for item in await self.audit.pending_local_entries(
                live_run_id=self.live_run_id
            )
            if item["intent"].symbol == symbol
        ]
        if not entries:
            return None
        if len(entries) != 1:
            raise RuntimeError(f"multiple local pending entries exist for {symbol}")
        item = entries[0]
        intent: TradeIntent = item["intent"]
        prior: RiskDecision = item["decision"]
        if reason := self._order_submission_block_reason():
            cancelled = prior.model_copy(
                update={
                    "accepted": False,
                    "reason": reason,
                    "pending_entry": False,
                    "evaluated_at": datetime.now(UTC),
                }
            )
            await self.audit.update_risk(
                item["inference_id"], cancelled, completed=True
            )
            return "cancelled"
        expires_at = prior.pending_expires_at
        now = datetime.now(UTC)
        if expires_at is None:
            expires_at = item["created_at"] + timedelta(seconds=intent.ttl_seconds)
        if now >= expires_at:
            expired = prior.model_copy(
                update={
                    "accepted": False,
                    "reason": (
                        f"pending limit intent expired after {intent.ttl_seconds}s"
                    ),
                    "pending_entry": False,
                    "pending_expires_at": expires_at,
                    "evaluated_at": now,
                }
            )
            await self.audit.update_risk(
                item["inference_id"], expired, completed=True
            )
            return "expired"

        snapshot = await self.market.market_snapshot(intent.symbol, intent.cadence)
        portfolio = await self.current_portfolio()
        portfolio = portfolio.model_copy(
            update={
                "pending_entry_symbols": tuple(
                    pending_symbol
                    for pending_symbol in portfolio.pending_entry_symbols
                    if pending_symbol != intent.symbol
                )
            }
        )
        rules = await self._rules_for_symbol(intent.symbol)
        evaluation = self.risk.evaluate(
            intent,
            snapshot,
            portfolio,
            rules,
            now=datetime.now(UTC),
        )
        decision = evaluation.decision.model_copy(
            update={"pending_expires_at": expires_at}
        )
        evaluation = RiskEvaluation(decision=decision, order=evaluation.order)
        if decision.pending_entry:
            await self.audit.update_risk(
                item["inference_id"], decision, completed=False
            )
            return "waiting"

        await self.audit.update_risk(
            item["inference_id"], decision, completed=True
        )
        if not decision.accepted or evaluation.order is None:
            return "cancelled"
        if reason := self._order_submission_block_reason():
            cancelled = decision.model_copy(
                update={"accepted": False, "reason": reason, "evaluated_at": datetime.now(UTC)}
            )
            await self.audit.update_risk(
                item["inference_id"], cancelled, completed=True
            )
            return "cancelled"
        await self._execute_order(
            intent.symbol,
            item["inference_id"],
            intent,
            evaluation.order,
        )
        return "triggered"

    async def _rules_for_symbol(self, symbol: str) -> SymbolRules:
        if self.venue_contract_rules is not None:
            rules = self.venue_contract_rules.get(symbol)
            if rules is None:
                raise RuntimeError(f"testnet contract rules are unavailable for {symbol}")
            return rules
        contract = (await self.market.exchange_info()).get(symbol)
        if contract is None:
            raise RuntimeError(f"contract rules are unavailable for {symbol}")
        return contract.rules

    async def _execute_order(
        self,
        symbol: str,
        inference_id: int,
        intent: TradeIntent,
        order: OrderPlan,
    ) -> ExecutionReport | None:
        if reason := self._order_submission_block_reason():
            await self.audit.record_execution_attempt(
                symbol,
                ExecutionAttempt(
                    inference_id=inference_id,
                    client_order_id=order.client_order_id,
                    status="FAILED",
                    stage="ENTRY",
                    message=f"order not submitted: {reason}",
                ),
            )
            return None
        try:
            execution = await self.testnet_broker.execute_with_stop(
                order,
                leverage=intent.leverage,
                replace_existing_protection=intent.action == TradeAction.ADD,
            )
        except ProtectiveStopError as exc:
            await self.audit.record_execution(symbol, exc.entry)
            if exc.rescue is not None:
                await self.audit.record_execution(symbol, exc.rescue)
            await self.audit.record_execution_attempt(
                symbol,
                ExecutionAttempt(
                    inference_id=inference_id,
                    client_order_id=order.client_order_id,
                    status="RESCUED" if exc.rescue is not None else "FAILED",
                    stage=exc.failed_stage,
                    message=str(exc),
                    exchange_error_code=exc.exchange_error_code,
                    entry_report=exc.entry,
                    rescue_report=exc.rescue,
                    estimated_loss_usdt=exc.estimated_loss_usdt,
                ),
            )
            if exc.rescue is not None:
                self.rescue_count += 1
            if exc.requires_emergency_lock:
                await self.emergency_stop()
            return None
        except Exception as exc:
            execution_status = (
                "UNKNOWN"
                if isinstance(exc, (TimeoutError, OrderStatusUnknown))
                else "FAILED"
            )
            await self.audit.record_execution_attempt(
                symbol,
                ExecutionAttempt(
                    inference_id=inference_id,
                    client_order_id=order.client_order_id,
                    status=execution_status,
                    stage="ENTRY",
                    message=f"{type(exc).__name__}: {exc}",
                    exchange_error_code=exc.code
                    if isinstance(exc, BinanceApiError)
                    else None,
                ),
            )
            if execution_status == "UNKNOWN":
                await self.emergency_stop(
                    reason=f"entry execution status unknown: {order.client_order_id}"
                )
            return None

        await self.audit.record_execution(symbol, execution)
        completed = execution.status in {"NEW", "PARTIALLY_FILLED", "FILLED"}
        await self.audit.record_execution_attempt(
            symbol,
            ExecutionAttempt(
                inference_id=inference_id,
                client_order_id=order.client_order_id,
                status="SUCCEEDED" if completed else "FAILED",
                stage="COMPLETE" if completed else "ENTRY",
                message=(
                    "order accepted and required execution checks completed"
                    if completed
                    else f"exchange returned terminal status {execution.status}"
                ),
                entry_report=execution,
            ),
        )
        return execution

    def _order_submission_block_reason(self) -> str | None:
        """Why a new exchange order may no longer leave this process."""

        if self.emergency_locked:
            return "order submission blocked by the emergency lock"
        if self.auto_stop_reason is not None:
            return f"order submission blocked because the run is stopping: {self.auto_stop_reason}"
        if not self.running:
            return "order submission blocked because the engine is stopped"
        return None

    @staticmethod
    def _prepare_pending_evaluation(
        intent: TradeIntent,
        evaluation: RiskEvaluation,
    ) -> RiskEvaluation:
        if not evaluation.decision.pending_entry:
            return evaluation
        decision = evaluation.decision.model_copy(
            update={
                "pending_expires_at": datetime.now(UTC)
                + timedelta(seconds=intent.ttl_seconds)
            }
        )
        return RiskEvaluation(decision=decision, order=evaluation.order)

    async def evaluate(
        self,
        snapshot: MarketSnapshot,
        portfolio: PortfolioState,
        rules: SymbolRules,
    ) -> DecisionOutcome:
        if not self.running or not self.provider_chain:
            raise RuntimeError("engine is not running")
        async with self._provider_route_lock:
            if self.route_failure_count >= DECISION_PROVIDER_MAX_ATTEMPTS:
                raise RuntimeError("provider route failure threshold already reached")
            now = datetime.now(UTC)
            candidates = [
                name
                for name in self.provider_chain
                if (
                    state := self._provider_route_states.setdefault(
                        name, ProviderRouteState()
                    )
                ).cooldown_until
                is None
                or state.cooldown_until <= now
            ]
            if not candidates:
                # The earliest route gets the first recovery attempt; subsequent
                # retries revisit the full chain inside this same decision.
                candidates = [
                    min(
                        self.provider_chain,
                        key=lambda name: self._provider_route_states[name].cooldown_until
                        or datetime.min.replace(tzinfo=UTC),
                    )
                ]

            failed_results: list[ProviderResult] = []
            result: ProviderResult | None = None
            analysis_snapshot = snapshot
            analysis_portfolio = portfolio
            refresh_retry_inputs = False
            while self.route_failure_count < DECISION_PROVIDER_MAX_ATTEMPTS:
                attempt_number = self.route_failure_count + 1
                if refresh_retry_inputs:
                    try:
                        analysis_snapshot = await self.market.market_snapshot(
                            snapshot.symbol, snapshot.cadence
                        )
                        analysis_portfolio = await self.current_portfolio()
                    except Exception as exc:
                        for failed_result in failed_results:
                            await self.audit.record_inference(
                                failed_result, live_run_id=self.live_run_id
                            )
                        raise RuntimeError(
                            "decision retry input refresh failed: "
                            f"{type(exc).__name__}"
                        ) from exc
                    refresh_retry_inputs = False
                round_candidates = (
                    candidates if attempt_number == 1 else list(self.provider_chain)
                )
                for position, name in enumerate(round_candidates, start=1):
                    provider = self.providers.get(name)
                    try:
                        async with asyncio.timeout(provider.timeout):
                            result = await provider.generate_trade_intent(
                                analysis_snapshot, analysis_portfolio
                            )
                    except TimeoutError:
                        timeout_error = ProviderError(
                            f"decision provider exceeded absolute {provider.timeout:g}s timeout"
                        )
                        failed_at = datetime.now(UTC)
                        state = self._provider_route_states[name]
                        state.consecutive_failures += 1
                        state.cooldown_until = failed_at + PROVIDER_FAILURE_COOLDOWN
                        state.last_error = str(timeout_error)
                        state.last_failed_at = failed_at
                        retry_continues = (
                            position < len(round_candidates)
                            or attempt_number < DECISION_PROVIDER_MAX_ATTEMPTS
                        )
                        failed_results.append(
                            self._provider_failure_result(
                                provider_name=name,
                                provider=provider,
                                error=timeout_error,
                                snapshot=analysis_snapshot,
                                portfolio=analysis_portfolio,
                                route_position=self.provider_chain.index(name) + 1,
                                failover_continues=retry_continues,
                                decision_attempt=attempt_number,
                            )
                        )
                        continue
                    except Exception as exc:
                        provider_error = (
                            exc
                            if isinstance(exc, ProviderError)
                            else ProviderError(f"{type(exc).__name__}: {exc}")
                        )
                        failed_at = datetime.now(UTC)
                        state = self._provider_route_states[name]
                        state.consecutive_failures += 1
                        state.cooldown_until = failed_at + PROVIDER_FAILURE_COOLDOWN
                        state.last_error = str(provider_error)
                        state.last_failed_at = failed_at
                        retry_continues = (
                            position < len(round_candidates)
                            or attempt_number < DECISION_PROVIDER_MAX_ATTEMPTS
                        )
                        failed_results.append(
                            self._provider_failure_result(
                                provider_name=name,
                                provider=provider,
                                error=provider_error,
                                snapshot=analysis_snapshot,
                                portfolio=analysis_portfolio,
                                route_position=self.provider_chain.index(name) + 1,
                                failover_continues=retry_continues,
                                decision_attempt=attempt_number,
                            )
                        )
                        continue
                    state = self._provider_route_states[name]
                    state.consecutive_failures = 0
                    state.cooldown_until = None
                    state.last_error = None
                    state.last_success_at = datetime.now(UTC)
                    self.active_provider = name
                    self.route_failure_count = 0
                    break

                if result is not None:
                    break
                self.active_provider = None
                self.route_failure_count += 1
                if self.route_failure_count < DECISION_PROVIDER_MAX_ATTEMPTS:
                    await self._retry_sleep(
                        self._provider_retry_delays[self.route_failure_count - 1]
                    )
                    refresh_retry_inputs = True

            if result is not None:
                for failed_result in failed_results:
                    await self.audit.record_inference(
                        failed_result, live_run_id=self.live_run_id
                    )
            else:
                if not failed_results:
                    raise RuntimeError("no provider route was attempted")
                for failed_result in failed_results[:-1]:
                    await self.audit.record_inference(
                        failed_result, live_run_id=self.live_run_id
                    )
                result = failed_results[-1]
        inference_id = await self.audit.record_inference(
            result, live_run_id=self.live_run_id
        )
        if (
            result.intent.action != TradeAction.HOLD
            and (reason := self._order_submission_block_reason()) is not None
        ):
            rejection = RiskDecision(accepted=False, reason=reason)
            await self.audit.record_risk(
                analysis_snapshot.symbol, rejection, inference_id=inference_id
            )
            return DecisionOutcome(result.intent, rejection, None, result.provider)
        evaluation_snapshot = analysis_snapshot
        evaluation_portfolio = analysis_portfolio
        intent_matches_snapshot = (
            result.intent.symbol == analysis_snapshot.symbol
            and result.intent.cadence == analysis_snapshot.cadence
        )
        if intent_matches_snapshot and result.intent.action != TradeAction.HOLD:
            analysis_age = (
                datetime.now(UTC) - analysis_snapshot.timestamp
            ).total_seconds()
            if analysis_age < -2 or analysis_age > self.risk.max_snapshot_age_seconds:
                rejection = RiskDecision(
                    accepted=False,
                    reason="analysis snapshot expired before pre-trade refresh",
                )
                await self.audit.record_risk(
                    analysis_snapshot.symbol, rejection, inference_id=inference_id
                )
                return DecisionOutcome(
                    intent=result.intent,
                    risk=rejection,
                    execution=None,
                    provider=result.provider,
                )
            try:
                evaluation_snapshot = await self.market.market_snapshot(
                    analysis_snapshot.symbol, analysis_snapshot.cadence
                )
                evaluation_portfolio = await self.current_portfolio()
            except Exception as exc:
                rejection = RiskDecision(
                    accepted=False,
                    reason=f"pre-trade refresh failed: {type(exc).__name__}",
                )
                await self.audit.record_risk(
                    analysis_snapshot.symbol, rejection, inference_id=inference_id
                )
                return DecisionOutcome(
                    intent=result.intent,
                    risk=rejection,
                    execution=None,
                    provider=result.provider,
                )

        evaluation = self._prepare_pending_evaluation(
            result.intent,
            self.risk.evaluate(
                result.intent,
                evaluation_snapshot,
                evaluation_portfolio,
                rules,
            ),
        )
        if (
            evaluation.order is not None
            and evaluation.decision.accepted
            and (reason := self._order_submission_block_reason()) is not None
        ):
            evaluation = RiskEvaluation(
                decision=RiskDecision(accepted=False, reason=reason),
                order=None,
            )
        await self.audit.record_risk(
            analysis_snapshot.symbol, evaluation.decision, inference_id=inference_id
        )
        execution = None
        if (
            evaluation.order is not None
            and evaluation.decision.accepted
            and not evaluation.decision.pending_entry
        ):
            execution = await self._execute_order(
                analysis_snapshot.symbol,
                inference_id,
                result.intent,
                evaluation.order,
            )
        return DecisionOutcome(
            intent=result.intent,
            risk=evaluation.decision,
            execution=execution,
            provider=result.provider,
        )

    async def evaluate_batch(
        self,
        snapshots: list[MarketSnapshot],
        portfolio: PortfolioState,
        rules_by_symbol: dict[str, SymbolRules],
    ) -> list[DecisionOutcome]:
        """Run one physical Provider call for every symbol in a cadence cycle."""
        if not snapshots:
            return []
        if not self.running or not self.provider_chain:
            raise RuntimeError("engine is not running")
        async with self._provider_route_lock:
            if self.route_failure_count >= DECISION_PROVIDER_MAX_ATTEMPTS:
                raise RuntimeError("provider route failure threshold already reached")
            now = datetime.now(UTC)
            candidates = [
                name for name in self.provider_chain
                if (state := self._provider_route_states.setdefault(name, ProviderRouteState())).cooldown_until is None
                or state.cooldown_until <= now
            ]
            if not candidates:
                candidates = [min(
                    self.provider_chain,
                    key=lambda name: self._provider_route_states[name].cooldown_until
                    or datetime.min.replace(tzinfo=UTC),
                )]
            analysis_snapshots = list(snapshots)
            analysis_portfolio = portfolio
            failed_batches: list[list[ProviderResult]] = []
            results: list[ProviderResult] | None = None
            refresh_retry_inputs = False
            while self.route_failure_count < DECISION_PROVIDER_MAX_ATTEMPTS:
                attempt_number = self.route_failure_count + 1
                if refresh_retry_inputs:
                    try:
                        analysis_snapshots = [
                            await self.market.market_snapshot(item.symbol, item.cadence)
                            for item in snapshots
                        ]
                        analysis_portfolio = await self.current_portfolio()
                    except Exception as exc:
                        for batch in failed_batches:
                            for failed_result in batch:
                                await self.audit.record_inference(
                                    failed_result, live_run_id=self.live_run_id
                                )
                        raise RuntimeError(
                            f"decision retry input refresh failed: {type(exc).__name__}"
                        ) from exc
                    refresh_retry_inputs = False
                round_candidates = candidates if attempt_number == 1 else list(self.provider_chain)
                for position, name in enumerate(round_candidates, start=1):
                    provider = self.providers.get(name)
                    try:
                        async with asyncio.timeout(provider.timeout):
                            results = await provider.generate_trade_intents(
                                analysis_snapshots, analysis_portfolio
                            )
                        if len(results) != len(analysis_snapshots):
                            raise ProviderError("provider returned the wrong number of batch intents")
                        expected = [(item.symbol, item.cadence) for item in analysis_snapshots]
                        actual = [(item.intent.symbol, item.intent.cadence) for item in results]
                        if actual != expected:
                            raise ProviderError("provider batch intents do not match input order")
                    except Exception as exc:
                        provider_error = (
                            exc if isinstance(exc, ProviderError)
                            else ProviderError(f"{type(exc).__name__}: {exc}")
                        )
                        failed_at = datetime.now(UTC)
                        state = self._provider_route_states[name]
                        state.consecutive_failures += 1
                        state.cooldown_until = failed_at + PROVIDER_FAILURE_COOLDOWN
                        state.last_error = str(provider_error)
                        state.last_failed_at = failed_at
                        continues = position < len(round_candidates) or attempt_number < DECISION_PROVIDER_MAX_ATTEMPTS
                        failed_batches.append(self._provider_failure_batch(
                            provider_name=name, provider=provider, error=provider_error,
                            snapshots=analysis_snapshots, portfolio=analysis_portfolio,
                            route_position=self.provider_chain.index(name) + 1,
                            failover_continues=continues, decision_attempt=attempt_number,
                        ))
                        results = None
                        continue
                    state = self._provider_route_states[name]
                    state.consecutive_failures = 0
                    state.cooldown_until = None
                    state.last_error = None
                    state.last_success_at = datetime.now(UTC)
                    self.active_provider = name
                    self.route_failure_count = 0
                    break
                if results is not None:
                    break
                self.active_provider = None
                self.route_failure_count += 1
                if self.route_failure_count < DECISION_PROVIDER_MAX_ATTEMPTS:
                    await self._retry_sleep(
                        self._provider_retry_delays[self.route_failure_count - 1]
                    )
                    refresh_retry_inputs = True
            for batch in failed_batches:
                # The last failed batch becomes the final HOLD batch only when no route succeeds.
                if results is None and batch is failed_batches[-1]:
                    continue
                for failed_result in batch:
                    await self.audit.record_inference(failed_result, live_run_id=self.live_run_id)
            if results is None:
                if not failed_batches:
                    raise RuntimeError("no provider route was attempted")
                results = failed_batches[-1]

        outcomes: list[DecisionOutcome] = []
        for snapshot, result in zip(analysis_snapshots, results, strict=True):
            rules = rules_by_symbol[snapshot.symbol]
            outcomes.append(
                await self._evaluate_batch_result(
                    snapshot, analysis_portfolio, result, rules
                )
            )
        return outcomes

    async def _evaluate_batch_result(
        self,
        analysis_snapshot: MarketSnapshot,
        analysis_portfolio: PortfolioState,
        result: ProviderResult,
        rules: SymbolRules,
    ) -> DecisionOutcome:
        inference_id = await self.audit.record_inference(result, live_run_id=self.live_run_id)
        if (
            result.intent.action != TradeAction.HOLD
            and (reason := self._order_submission_block_reason()) is not None
        ):
            rejection = RiskDecision(accepted=False, reason=reason)
            await self.audit.record_risk(
                analysis_snapshot.symbol, rejection, inference_id=inference_id
            )
            return DecisionOutcome(result.intent, rejection, None, result.provider)
        evaluation_snapshot = analysis_snapshot
        evaluation_portfolio = analysis_portfolio
        if result.intent.action != TradeAction.HOLD:
            analysis_age = (datetime.now(UTC) - analysis_snapshot.timestamp).total_seconds()
            if analysis_age < -2 or analysis_age > self.risk.max_snapshot_age_seconds:
                rejection = RiskDecision(
                    accepted=False,
                    reason="analysis snapshot expired before pre-trade refresh",
                )
                await self.audit.record_risk(analysis_snapshot.symbol, rejection, inference_id=inference_id)
                return DecisionOutcome(result.intent, rejection, None, result.provider)
            try:
                evaluation_snapshot = await self.market.market_snapshot(
                    analysis_snapshot.symbol, analysis_snapshot.cadence
                )
                evaluation_portfolio = await self.current_portfolio()
            except Exception as exc:
                rejection = RiskDecision(
                    accepted=False,
                    reason=f"pre-trade refresh failed: {type(exc).__name__}",
                )
                await self.audit.record_risk(analysis_snapshot.symbol, rejection, inference_id=inference_id)
                return DecisionOutcome(result.intent, rejection, None, result.provider)
        evaluation = self._prepare_pending_evaluation(
            result.intent,
            self.risk.evaluate(
                result.intent,
                evaluation_snapshot,
                evaluation_portfolio,
                rules,
            ),
        )
        if (
            evaluation.order is not None
            and evaluation.decision.accepted
            and (reason := self._order_submission_block_reason()) is not None
        ):
            evaluation = RiskEvaluation(
                decision=RiskDecision(accepted=False, reason=reason),
                order=None,
            )
        await self.audit.record_risk(
            analysis_snapshot.symbol, evaluation.decision, inference_id=inference_id
        )
        execution = None
        if (
            evaluation.order is not None
            and evaluation.decision.accepted
            and not evaluation.decision.pending_entry
        ):
            execution = await self._execute_order(
                analysis_snapshot.symbol,
                inference_id,
                result.intent,
                evaluation.order,
            )
        return DecisionOutcome(result.intent, evaluation.decision, execution, result.provider)

    @classmethod
    def _provider_failure_batch(
        cls, *, provider_name: str, provider: object, error: ProviderError,
        snapshots: list[MarketSnapshot], portfolio: PortfolioState, route_position: int,
        failover_continues: bool, decision_attempt: int,
    ) -> list[ProviderResult]:
        results = [
            cls._provider_failure_result(
                provider_name=provider_name, provider=provider, error=error, snapshot=snapshot,
                portfolio=portfolio, route_position=route_position,
                failover_continues=failover_continues, decision_attempt=decision_attempt,
            ) for snapshot in snapshots
        ]
        size = len(results)
        for index, result in enumerate(results):
            usage = dict(result.usage)
            for key in ("input_tokens", "cached_input_tokens", "cache_read_input_tokens",
                        "cache_creation_input_tokens", "output_tokens", "total_tokens"):
                if key in usage:
                    total = int(usage[key] or 0)
                    usage[key] = total // size + (1 if index < total % size else 0)
            if usage.get("cost_usd") is not None:
                usage["cost_usd"] = float(usage["cost_usd"]) / size
            usage.update(batch_size=size, batch_index=index + 1, batch_shared_call=True)
            object.__setattr__(result, "usage", usage)
        return results

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
        decision_attempt: int,
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
                "decision_attempt": decision_attempt,
                "decision_attempt_limit": DECISION_PROVIDER_MAX_ATTEMPTS,
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
