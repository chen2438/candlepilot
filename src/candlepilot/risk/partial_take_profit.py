from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Any, Literal

from candlepilot.broker.binance_testnet import TrailingPosition
from candlepilot.risk.engine import SymbolRules
from candlepilot.storage.database import AuditRepository


PARTIAL_TAKE_PROFIT_STATE_KEY = "partial_take_profit_states_v1"


@dataclass(frozen=True, slots=True)
class PartialTakeProfitProfile:
    profile_id: str
    target_r: Decimal
    fraction: Decimal


PARTIAL_TAKE_PROFIT_PROFILES = (
    PartialTakeProfitProfile("1R / 25% + BE", Decimal("1"), Decimal("0.25")),
    PartialTakeProfitProfile("1R / 50% + BE", Decimal("1"), Decimal("0.50")),
)


@dataclass(slots=True)
class PartialTakeProfitProgress:
    partial_triggered: bool = False
    breakeven_triggered: bool = False
    unviable: bool = False
    partial_quantity: Decimal | None = None
    partial_gross_pnl: Decimal = Decimal("0")


@dataclass(slots=True)
class PartialTakeProfitState:
    side: Literal["LONG", "SHORT"]
    quantity: Decimal
    entry_price: Decimal
    original_stop: Decimal
    risk_distance: Decimal
    last_mark: Decimal
    profiles: dict[str, PartialTakeProfitProgress]


class PartialTakeProfitManager:
    """Observe partial-profit profiles without changing exchange orders."""

    def __init__(self, audit: AuditRepository) -> None:
        self.audit = audit
        self._states: dict[str, PartialTakeProfitState] = {}
        self._loaded = False
        self.last_event: dict[str, Any] | None = None

    @property
    def status(self) -> dict[str, Any]:
        progress = [
            item
            for state in self._states.values()
            for item in state.profiles.values()
        ]
        return {
            "mode": "shadow",
            "strategies": [
                {
                    "profile_id": profile.profile_id,
                    "target_r": str(profile.target_r),
                    "fraction": str(profile.fraction),
                    "move_remainder_to_breakeven": True,
                }
                for profile in PARTIAL_TAKE_PROFIT_PROFILES
            ],
            "managed_positions": len(self._states),
            "partial_fills": sum(item.partial_triggered for item in progress),
            "breakeven_fills": sum(item.breakeven_triggered for item in progress),
            "unviable_strategies": sum(item.unviable for item in progress),
            "last_event": self.last_event,
        }

    async def maintain(
        self,
        positions: dict[str, TrailingPosition],
        rules: dict[str, SymbolRules],
        *,
        open_symbols: set[str] | None = None,
    ) -> list[str]:
        await self._load()
        errors: list[str] = []
        changed = False
        observed = open_symbols if open_symbols is not None else set(positions)
        for symbol in sorted(set(self._states) - observed):
            state = self._states.pop(symbol)
            for profile in PARTIAL_TAKE_PROFIT_PROFILES:
                progress = state.profiles.get(profile.profile_id)
                if (
                    progress is not None
                    and progress.partial_triggered
                    and not progress.breakeven_triggered
                ):
                    await self._record(
                        symbol,
                        state,
                        profile,
                        progress,
                        "position_closed",
                        detail="real position closed before another shadow fill was observed",
                    )
            changed = True
        for symbol, position in sorted(positions.items()):
            rule = rules.get(symbol)
            if rule is None:
                errors.append(f"{symbol}: partial take-profit shadow has no venue rule")
                continue
            try:
                changed = await self._maintain_position(position, rule) or changed
            except Exception as exc:
                errors.append(f"{symbol}: {type(exc).__name__}: {exc}")
        if changed:
            await self._save()
        return errors

    async def _maintain_position(
        self, position: TrailingPosition, rules: SymbolRules
    ) -> bool:
        if position.stop_loss is None:
            raise ValueError("position has no exchange stop")
        state = self._states.get(position.symbol)
        changed = False
        if state is None or state.side != position.side:
            if not self._is_loss_side(position.side, position.entry_price, position.stop_loss):
                raise ValueError("cannot infer original R from a stop beyond breakeven")
            state = self._new_state(position)
            self._states[position.symbol] = state
            changed = True
        elif position.entry_price != state.entry_price:
            if not self._is_loss_side(position.side, position.entry_price, position.stop_loss):
                raise ValueError("cannot reset R after an add with stop beyond breakeven")
            state = self._new_state(position)
            self._states[position.symbol] = state
            changed = True

        state.last_mark = position.mark_price
        for profile in PARTIAL_TAKE_PROFIT_PROFILES:
            if profile.profile_id not in state.profiles:
                state.profiles[profile.profile_id] = PartialTakeProfitProgress()
                changed = True
            changed = (
                await self._maintain_profile(position, rules, state, profile) or changed
            )
        return changed

    @staticmethod
    def _new_state(position: TrailingPosition) -> PartialTakeProfitState:
        return PartialTakeProfitState(
            side=position.side,
            quantity=position.quantity,
            entry_price=position.entry_price,
            original_stop=position.stop_loss,
            risk_distance=abs(position.entry_price - position.stop_loss),
            last_mark=position.mark_price,
            profiles={
                profile.profile_id: PartialTakeProfitProgress()
                for profile in PARTIAL_TAKE_PROFIT_PROFILES
            },
        )

    async def _maintain_profile(
        self,
        position: TrailingPosition,
        rules: SymbolRules,
        state: PartialTakeProfitState,
        profile: PartialTakeProfitProfile,
    ) -> bool:
        progress = state.profiles[profile.profile_id]
        if progress.unviable or progress.breakeven_triggered:
            return False
        if not progress.partial_triggered:
            partial_quantity = self._round_down(
                state.quantity * profile.fraction,
                rules.market_quantity_step or rules.quantity_step,
            )
            remaining_quantity = state.quantity - partial_quantity
            minimum = rules.market_min_quantity or rules.min_quantity
            if partial_quantity < minimum or remaining_quantity <= 0:
                progress.unviable = True
                await self._record(
                    position.symbol,
                    state,
                    profile,
                    progress,
                    "unviable",
                    detail="partial quantity is below the venue minimum",
                )
                return True
            target = self._target_price(state, profile, rules.tick_size)
            if not self._target_reached(position.side, position.mark_price, target):
                return False
            progress.partial_triggered = True
            progress.partial_quantity = partial_quantity
            progress.partial_gross_pnl = self._gross_pnl(
                position.side, state.entry_price, target, partial_quantity
            )
            await self._record(
                position.symbol,
                state,
                profile,
                progress,
                "partial_simulated_filled",
                target_price=target,
                simulated_fill=target,
                fill_quantity=partial_quantity,
                fill_gross_pnl=progress.partial_gross_pnl,
                detail="shadow limit fill at the deterministic 1R target",
            )
            return True

        if not self._breakeven_crossed(
            position.side, position.mark_price, state.entry_price
        ):
            return False
        remaining_quantity = state.quantity - (progress.partial_quantity or Decimal("0"))
        fill_gross_pnl = self._gross_pnl(
            position.side, state.entry_price, position.mark_price, remaining_quantity
        )
        progress.breakeven_triggered = True
        await self._record(
            position.symbol,
            state,
            profile,
            progress,
            "breakeven_simulated_filled",
            target_price=self._target_price(state, profile, rules.tick_size),
            simulated_fill=position.mark_price,
            fill_quantity=remaining_quantity,
            fill_gross_pnl=fill_gross_pnl,
            strategy_gross_pnl=progress.partial_gross_pnl + fill_gross_pnl,
            detail="first observed mark crossed the shadow breakeven stop",
        )
        return True

    async def _record(
        self,
        symbol: str,
        state: PartialTakeProfitState,
        profile: PartialTakeProfitProfile,
        progress: PartialTakeProfitProgress,
        status: str,
        *,
        target_price: Decimal | None = None,
        simulated_fill: Decimal | None = None,
        fill_quantity: Decimal | None = None,
        fill_gross_pnl: Decimal | None = None,
        strategy_gross_pnl: Decimal | None = None,
        detail: str = "",
    ) -> None:
        partial_quantity = progress.partial_quantity
        event = {
            "side": state.side,
            "original_quantity": str(state.quantity),
            "entry_price": str(state.entry_price),
            "original_stop": str(state.original_stop),
            "risk_distance": str(state.risk_distance),
            "observed_mark_price": str(state.last_mark),
            "profile_id": profile.profile_id,
            "target_r": str(profile.target_r),
            "partial_fraction": str(profile.fraction),
            "target_price": str(target_price) if target_price is not None else None,
            "breakeven_price": str(state.entry_price),
            "partial_quantity": str(partial_quantity)
            if partial_quantity is not None
            else None,
            "remaining_quantity": str(state.quantity - partial_quantity)
            if partial_quantity is not None
            else None,
            "fill_quantity": str(fill_quantity) if fill_quantity is not None else None,
            "simulated_fill_price": str(simulated_fill)
            if simulated_fill is not None
            else None,
            "fill_gross_pnl": str(fill_gross_pnl)
            if fill_gross_pnl is not None
            else None,
            "strategy_gross_pnl": str(strategy_gross_pnl)
            if strategy_gross_pnl is not None
            else None,
            "detail": detail,
        }
        await self.audit.record_partial_take_profit_event(symbol, status, event)
        self.last_event = {"symbol": symbol, "status": status, **event}

    async def _load(self) -> None:
        if self._loaded:
            return
        raw = await self.audit.get_runtime_state(PARTIAL_TAKE_PROFIT_STATE_KEY)
        if raw:
            for symbol, item in json.loads(raw).items():
                self._states[symbol] = PartialTakeProfitState(
                    side=item["side"],
                    quantity=Decimal(item["quantity"]),
                    entry_price=Decimal(item["entry_price"]),
                    original_stop=Decimal(item["original_stop"]),
                    risk_distance=Decimal(item["risk_distance"]),
                    last_mark=Decimal(item["last_mark"]),
                    profiles={
                        profile_id: PartialTakeProfitProgress(
                            partial_triggered=bool(progress["partial_triggered"]),
                            breakeven_triggered=bool(progress["breakeven_triggered"]),
                            unviable=bool(progress.get("unviable", False)),
                            partial_quantity=Decimal(progress["partial_quantity"])
                            if progress.get("partial_quantity") is not None
                            else None,
                            partial_gross_pnl=Decimal(
                                progress.get("partial_gross_pnl", "0")
                            ),
                        )
                        for profile_id, progress in item["profiles"].items()
                    },
                )
        self._loaded = True

    async def _save(self) -> None:
        payload = {
            symbol: {
                "side": state.side,
                "quantity": str(state.quantity),
                "entry_price": str(state.entry_price),
                "original_stop": str(state.original_stop),
                "risk_distance": str(state.risk_distance),
                "last_mark": str(state.last_mark),
                "profiles": {
                    profile_id: {
                        "partial_triggered": progress.partial_triggered,
                        "breakeven_triggered": progress.breakeven_triggered,
                        "unviable": progress.unviable,
                        "partial_quantity": str(progress.partial_quantity)
                        if progress.partial_quantity is not None
                        else None,
                        "partial_gross_pnl": str(progress.partial_gross_pnl),
                    }
                    for profile_id, progress in state.profiles.items()
                },
            }
            for symbol, state in self._states.items()
        }
        await self.audit.set_runtime_state(
            PARTIAL_TAKE_PROFIT_STATE_KEY,
            json.dumps(payload, separators=(",", ":")),
        )

    @staticmethod
    def _target_price(
        state: PartialTakeProfitState,
        profile: PartialTakeProfitProfile,
        tick_size: Decimal,
    ) -> Decimal:
        if tick_size <= 0:
            raise ValueError("tick size must be positive")
        raw = (
            state.entry_price + profile.target_r * state.risk_distance
            if state.side == "LONG"
            else state.entry_price - profile.target_r * state.risk_distance
        )
        rounding = ROUND_UP if state.side == "LONG" else ROUND_DOWN
        return (raw / tick_size).to_integral_value(rounding=rounding) * tick_size

    @staticmethod
    def _round_down(value: Decimal, step: Decimal) -> Decimal:
        if step <= 0:
            raise ValueError("quantity step must be positive")
        return (value / step).to_integral_value(rounding=ROUND_DOWN) * step

    @staticmethod
    def _is_loss_side(side: str, entry: Decimal, stop: Decimal) -> bool:
        return stop < entry if side == "LONG" else stop > entry

    @staticmethod
    def _target_reached(side: str, mark: Decimal, target: Decimal) -> bool:
        return mark >= target if side == "LONG" else mark <= target

    @staticmethod
    def _breakeven_crossed(side: str, mark: Decimal, breakeven: Decimal) -> bool:
        return mark <= breakeven if side == "LONG" else mark >= breakeven

    @staticmethod
    def _gross_pnl(
        side: str, entry: Decimal, fill: Decimal, quantity: Decimal
    ) -> Decimal:
        move = fill - entry if side == "LONG" else entry - fill
        return move * quantity
