from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Any, Literal

from candlepilot.broker.binance_testnet import (
    TrailingPosition,
    TrailingStopReplacementError,
)
from candlepilot.risk.engine import SymbolRules
from candlepilot.storage.database import AuditRepository


TRAILING_STATE_KEY = "trailing_stop_states_v1"


class TrailingStopCriticalError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TrailingStopProfile:
    profile_id: str
    activation_r: Decimal
    distance_r: Decimal


LIVE_PROFILE = TrailingStopProfile("2R / 1R", Decimal("2"), Decimal("1"))
SHADOW_PROFILES = (
    TrailingStopProfile("0.5R / 0.5R", Decimal("0.5"), Decimal("0.5")),
    TrailingStopProfile("0.5R / 0.75R", Decimal("0.5"), Decimal("0.75")),
    TrailingStopProfile("1R / 1R", Decimal("1"), Decimal("1")),
    TrailingStopProfile("1.5R / 0.5R", Decimal("1.5"), Decimal("0.5")),
    LIVE_PROFILE,
)


@dataclass(slots=True)
class TrailingProfileState:
    active: bool = False
    last_candidate: Decimal | None = None


@dataclass(slots=True)
class TrailingStopState:
    side: Literal["LONG", "SHORT"]
    quantity: Decimal
    entry_price: Decimal
    original_stop: Decimal
    risk_distance: Decimal
    best_mark: Decimal
    profiles: dict[str, TrailingProfileState]


class TrailingStopManager:
    """Audit several shadow profiles or maintain one live trailing stop.

    The manager deliberately does not ask a Provider for updates.  It observes
    the testnet mark and exchange stop, persists the high-water mark, and either
    audits deterministic candidates (shadow) or asks the broker for one
    fail-closed 2R / 1R stop replacement (live).
    """

    minimum_step_r = Decimal("0.1")

    def __init__(
        self,
        broker: Any,
        audit: AuditRepository,
        *,
        mode: Literal["off", "shadow", "live"] = "shadow",
    ) -> None:
        if mode not in {"off", "shadow", "live"}:
            raise ValueError("trailing stop mode must be off, shadow, or live")
        self.broker = broker
        self.audit = audit
        self.mode = mode
        self._states: dict[str, TrailingStopState] = {}
        self._loaded = False
        self.last_event: dict[str, Any] | None = None

    @property
    def status(self) -> dict[str, Any]:
        profiles = self._profiles
        return {
            "mode": self.mode,
            "strategies": [
                {
                    "profile_id": profile.profile_id,
                    "activation_r": str(profile.activation_r),
                    "distance_r": str(profile.distance_r),
                }
                for profile in profiles
            ],
            "managed_positions": len(self._states),
            "active_positions": sum(
                any(
                    state.profiles.get(profile.profile_id, TrailingProfileState()).active
                    for profile in profiles
                )
                for state in self._states.values()
            ),
            "active_strategies": sum(
                state.profiles.get(profile.profile_id, TrailingProfileState()).active
                for state in self._states.values()
                for profile in profiles
            ),
            "last_event": self.last_event,
        }

    @property
    def _profiles(self) -> tuple[TrailingStopProfile, ...]:
        return SHADOW_PROFILES if self.mode == "shadow" else (LIVE_PROFILE,)

    async def maintain(
        self,
        positions: dict[str, TrailingPosition],
        rules: dict[str, SymbolRules],
        *,
        open_symbols: set[str] | None = None,
    ) -> list[str]:
        if self.mode == "off":
            return []
        await self._load()
        errors: list[str] = []
        changed = False
        observed = open_symbols if open_symbols is not None else set(positions)
        for symbol in set(self._states) - observed:
            self._states.pop(symbol, None)
            changed = True
        for symbol, position in sorted(positions.items()):
            rule = rules.get(symbol)
            if rule is None:
                errors.append(f"{symbol}: trailing stop has no venue tick rule")
                continue
            try:
                state_changed = await self._maintain_position(position, rule)
                changed = changed or state_changed
            except TrailingStopCriticalError:
                raise
            except Exception as exc:  # one symbol must not hide upkeep for the rest
                detail = f"{symbol}: {type(exc).__name__}: {exc}"
                errors.append(detail)
                await self._record(position, "failed", detail=detail)
                changed = True
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
                raise ValueError(
                    "cannot infer original R from a stop that already locks profit"
                )
            state = TrailingStopState(
                side=position.side,
                quantity=position.quantity,
                entry_price=position.entry_price,
                original_stop=position.stop_loss,
                risk_distance=abs(position.entry_price - position.stop_loss),
                best_mark=position.mark_price,
                profiles={
                    profile.profile_id: TrailingProfileState()
                    for profile in self._profiles
                },
            )
            self._states[position.symbol] = state
            changed = True

        if position.entry_price != state.entry_price:
            if not self._is_loss_side(position.side, position.entry_price, position.stop_loss):
                raise ValueError(
                    "cannot reset R after an add because the current stop is beyond breakeven"
                )
            state.entry_price = position.entry_price
            state.original_stop = position.stop_loss
            state.risk_distance = abs(position.entry_price - position.stop_loss)
            state.best_mark = position.mark_price
            state.profiles = {
                profile.profile_id: TrailingProfileState()
                for profile in self._profiles
            }
            changed = True
        for profile in self._profiles:
            if profile.profile_id not in state.profiles:
                state.profiles[profile.profile_id] = TrailingProfileState()
                changed = True
        state.quantity = position.quantity
        favorable = (
            position.mark_price > state.best_mark
            if position.side == "LONG"
            else position.mark_price < state.best_mark
        )
        if favorable:
            state.best_mark = position.mark_price
            changed = True
        for profile in self._profiles:
            changed = await self._maintain_profile(
                position, rules, state, profile
            ) or changed
        return changed

    async def _maintain_profile(
        self,
        position: TrailingPosition,
        rules: SymbolRules,
        state: TrailingStopState,
        profile: TrailingStopProfile,
    ) -> bool:
        progress = state.profiles[profile.profile_id]
        excursion = (
            state.best_mark - state.entry_price
            if position.side == "LONG"
            else state.entry_price - state.best_mark
        )
        changed = False
        if not progress.active and excursion >= profile.activation_r * state.risk_distance:
            progress.active = True
            changed = True
        if not progress.active:
            return changed

        raw_candidate = (
            state.best_mark - profile.distance_r * state.risk_distance
            if position.side == "LONG"
            else state.best_mark + profile.distance_r * state.risk_distance
        )
        candidate = self._to_tick(raw_candidate, rules.tick_size, position.side)
        minimum_step = max(rules.tick_size, self.minimum_step_r * state.risk_distance)
        reference_stop = progress.last_candidate or position.stop_loss
        improvement = (
            candidate - reference_stop
            if position.side == "LONG"
            else reference_stop - candidate
        )
        if improvement < minimum_step:
            return changed
        valid_side = (
            candidate <= position.mark_price - rules.tick_size
            if position.side == "LONG"
            else candidate >= position.mark_price + rules.tick_size
        )
        if not valid_side:
            if progress.last_candidate != candidate:
                progress.last_candidate = candidate
                await self._record(
                    position,
                    "missed",
                    profile=profile,
                    candidate=candidate,
                    detail="mark already crossed the next trailing trigger",
                )
                return True
            return changed
        if progress.last_candidate == candidate:
            return changed
        if self.mode == "shadow":
            progress.last_candidate = candidate
            await self._record(
                position, "shadow", profile=profile, candidate=candidate
            )
            return True

        try:
            replacement = await self.broker.replace_stop_loss(
                position.symbol, position.side, candidate
            )
        except TrailingStopReplacementError as exc:
            if exc.requires_emergency_lock:
                await self._record(
                    position,
                    "failed",
                    profile=profile,
                    candidate=candidate,
                    detail=str(exc),
                )
                raise TrailingStopCriticalError(str(exc)) from exc
            raise
        progress.last_candidate = replacement.current_stop
        await self._record(
            position,
            "applied",
            profile=profile,
            candidate=replacement.current_stop,
            previous_stop=replacement.previous_stop,
        )
        return True

    async def _record(
        self,
        position: TrailingPosition,
        status: str,
        *,
        profile: TrailingStopProfile | None = None,
        candidate: Decimal | None = None,
        previous_stop: Decimal | None = None,
        detail: str = "",
    ) -> None:
        state = self._states.get(position.symbol)
        event = {
            "side": position.side,
            "quantity": str(position.quantity),
            "entry_price": str(position.entry_price),
            "mark_price": str(position.mark_price),
            "original_stop": str(state.original_stop) if state is not None else None,
            "best_mark": str(state.best_mark) if state is not None else None,
            "previous_stop": str(previous_stop or position.stop_loss)
            if (previous_stop or position.stop_loss) is not None
            else None,
            "candidate_stop": str(candidate) if candidate is not None else None,
            "profile_id": profile.profile_id if profile is not None else None,
            "activation_r": str(profile.activation_r) if profile is not None else None,
            "distance_r": str(profile.distance_r) if profile is not None else None,
            "detail": detail,
        }
        await self.audit.record_trailing_stop_event(
            position.symbol, self.mode, status, event
        )
        self.last_event = {"symbol": position.symbol, "status": status, **event}

    async def _load(self) -> None:
        if self._loaded:
            return
        raw = await self.audit.get_runtime_state(TRAILING_STATE_KEY)
        if raw:
            parsed = json.loads(raw)
            stored_mode = parsed.get("mode") if "states" in parsed else None
            stored_states = parsed.get("states", parsed)
            for symbol, item in stored_states.items():
                raw_profiles = item.get("profiles")
                if raw_profiles is None:
                    raw_profiles = {
                        LIVE_PROFILE.profile_id: {
                            "active": item.get("active", False),
                            "last_candidate": item.get("last_candidate"),
                        }
                    }
                self._states[symbol] = TrailingStopState(
                    side=item["side"],
                    quantity=Decimal(item["quantity"]),
                    entry_price=Decimal(item["entry_price"]),
                    original_stop=Decimal(item["original_stop"]),
                    risk_distance=Decimal(item["risk_distance"]),
                    best_mark=Decimal(item["best_mark"]),
                    profiles={
                        profile_id: TrailingProfileState(
                            active=bool(progress["active"]),
                            last_candidate=Decimal(progress["last_candidate"])
                            if progress.get("last_candidate") is not None
                            else None,
                        )
                        for profile_id, progress in raw_profiles.items()
                    },
                )
            if stored_mode != self.mode:
                for state in self._states.values():
                    for progress in state.profiles.values():
                        progress.last_candidate = None
        self._loaded = True

    async def _save(self) -> None:
        payload = {
            "mode": self.mode,
            "states": {
                symbol: {
                    "side": state.side,
                    "quantity": str(state.quantity),
                    "entry_price": str(state.entry_price),
                    "original_stop": str(state.original_stop),
                    "risk_distance": str(state.risk_distance),
                    "best_mark": str(state.best_mark),
                    "profiles": {
                        profile_id: {
                            "active": progress.active,
                            "last_candidate": str(progress.last_candidate)
                            if progress.last_candidate is not None
                            else None,
                        }
                        for profile_id, progress in state.profiles.items()
                    },
                }
                for symbol, state in self._states.items()
            }
        }
        await self.audit.set_runtime_state(
            TRAILING_STATE_KEY, json.dumps(payload, separators=(",", ":"))
        )

    @staticmethod
    def _is_loss_side(side: str, entry: Decimal, stop: Decimal) -> bool:
        return stop < entry if side == "LONG" else stop > entry

    @staticmethod
    def _to_tick(value: Decimal, tick: Decimal, side: str) -> Decimal:
        if tick <= 0:
            raise ValueError("tick size must be positive")
        rounding = ROUND_DOWN if side == "LONG" else ROUND_UP
        return (value / tick).to_integral_value(rounding=rounding) * tick
