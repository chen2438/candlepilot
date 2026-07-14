from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta

from candlepilot.broker.binance_testnet import (
    AccountReconciliationError,
    BinanceTestnetBroker,
    ReconciliationReport,
)
from candlepilot.domain.models import (
    ExecutionReport,
    MarketSnapshot,
    PortfolioState,
    ProviderHealth,
    RiskDecision,
    TradeIntent,
    TradingMode,
)
from candlepilot.execution.paper import PaperExecutor
from candlepilot.market.binance import BinancePublicClient
from candlepilot.market.scanner import Candidate, MarketScanner
from candlepilot.providers.base import ProviderResult
from candlepilot.providers.cli import ProviderError
from candlepilot.providers.registry import ProviderRegistry
from candlepilot.risk.engine import AggressiveRiskPolicy, SymbolRules
from candlepilot.storage.database import AuditRepository


@dataclass(frozen=True, slots=True)
class DecisionOutcome:
    intent: TradeIntent
    risk: RiskDecision
    execution: ExecutionReport | None
    provider: str


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
    ) -> None:
        self.mode = mode
        self.providers = providers
        self.audit = audit
        self.market = market
        self.scanner = scanner or MarketScanner()
        self.risk = risk or AggressiveRiskPolicy()
        self.paper_executor = paper_executor or PaperExecutor(state_store=audit)
        self.testnet_broker = testnet_broker
        self.selected_provider: str | None = None
        self.running = False
        self.emergency_locked = False
        self.emergency_locked_until: datetime | None = None
        self.testnet_reconciliation: ReconciliationReport | None = None
        self.candidates: list[Candidate] = []
        self.universe_refreshed_at: datetime | None = None

    async def provider_health(self) -> list[ProviderHealth]:
        return await self.providers.health()

    def select_provider(self, name: str) -> None:
        self.providers.get(name)
        self.selected_provider = name

    async def start(self) -> None:
        await self.restore_runtime_state()
        if self.emergency_locked:
            raise RuntimeError("engine is emergency locked")
        if self.selected_provider is None:
            raise RuntimeError("an authenticated LLM provider must be selected")
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
        health = await self.providers.get(self.selected_provider).health_check()
        if not health.available or not health.authenticated:
            raise RuntimeError(f"provider is unavailable: {health.detail}")
        self.running = True

    def stop(self) -> None:
        self.running = False

    async def emergency_stop(self, *, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        if now.tzinfo is None:
            raise ValueError("emergency stop time must be timezone-aware")
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

    async def evaluate(
        self,
        snapshot: MarketSnapshot,
        portfolio: PortfolioState,
        rules: SymbolRules,
    ) -> DecisionOutcome:
        if not self.running or self.selected_provider is None:
            raise RuntimeError("engine is not running")
        provider = self.providers.get(self.selected_provider)
        try:
            result = await provider.generate_trade_intent(snapshot, portfolio)
        except ProviderError as exc:
            intent = TradeIntent.hold(snapshot.symbol, snapshot.cadence, f"provider error: {exc}")
            result = ProviderResult(
                intent=intent,
                provider=self.selected_provider,
                model=None,
                duration=timedelta(0),
                raw_output=str(exc),
                usage={"error": type(exc).__name__},
            )
        inference_id = await self.audit.record_inference(result)
        evaluation = self.risk.evaluate(result.intent, snapshot, portfolio, rules)
        await self.audit.record_risk(
            snapshot.symbol, evaluation.decision, inference_id=inference_id
        )
        execution = None
        if evaluation.order is not None and evaluation.decision.accepted:
            if self.mode == TradingMode.TESTNET:
                if self.testnet_broker is None:
                    raise RuntimeError("Binance testnet broker is unavailable")
                execution = await self.testnet_broker.execute_with_stop(
                    evaluation.order, leverage=result.intent.leverage
                )
            else:
                execution = await self.paper_executor.execute(
                    evaluation.order, snapshot, leverage=result.intent.leverage
                )
            await self.audit.record_execution(snapshot.symbol, execution)
        return DecisionOutcome(
            intent=result.intent,
            risk=evaluation.decision,
            execution=execution,
            provider=result.provider,
        )
