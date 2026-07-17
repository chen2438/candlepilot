from __future__ import annotations

import hashlib
import hmac
import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlencode

import httpx
from pydantic import SecretStr

from candlepilot.domain.models import ExecutionReport, OrderPlan, OrderType
from candlepilot.market.binance import BINANCE_FUTURES_TESTNET

if TYPE_CHECKING:
    from candlepilot.broker.user_stream import UserStreamEvent


class BinanceApiError(RuntimeError):
    def __init__(self, code: int, message: str, status_code: int) -> None:
        super().__init__(f"Binance error {code}: {message}")
        self.code = code
        self.status_code = status_code


class ProtectiveStopError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        entry: ExecutionReport,
        rescue: ExecutionReport | None,
        exchange_error_code: int | None,
        estimated_loss_usdt: Decimal | None,
        failed_stage: Literal["PROTECTION", "RESCUE"],
        requires_emergency_lock: bool,
    ) -> None:
        super().__init__(message)
        self.entry = entry
        self.rescue = rescue
        self.exchange_error_code = exchange_error_code
        self.estimated_loss_usdt = estimated_loss_usdt
        self.failed_stage = failed_stage
        self.requires_emergency_lock = requires_emergency_lock


class AccountReconciliationError(RuntimeError):
    pass


class OrderStatusUnknown(RuntimeError):
    def __init__(self, client_order_id: str) -> None:
        super().__init__(f"order status remains unknown: {client_order_id}")
        self.client_order_id = client_order_id


@dataclass(frozen=True, slots=True)
class BinanceTestnetCredentials:
    api_key: SecretStr
    api_secret: SecretStr


@dataclass(frozen=True, slots=True)
class ProtectiveLevels:
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    position_symbols: tuple[str, ...]
    open_order_count: int
    unprotected_symbols: tuple[str, ...]
    pending_entry_symbols: tuple[str, ...] = ()


class BinanceTestnetBroker:
    """Signed broker that refuses to connect to Binance production trading."""

    def __init__(
        self,
        credentials: BinanceTestnetCredentials,
        *,
        base_url: str = BINANCE_FUTURES_TESTNET,
        client: httpx.AsyncClient | None = None,
        recovery_attempts: int = 4,
        recovery_delay: float = 0.25,
        rate_limit_attempts: int = 3,
    ) -> None:
        normalized = base_url.rstrip("/")
        if normalized != BINANCE_FUTURES_TESTNET:
            raise ValueError("BinanceTestnetBroker only permits the official futures testnet")
        self.credentials = credentials
        if recovery_attempts < 1 or recovery_delay < 0 or rate_limit_attempts < 1:
            raise ValueError("invalid order recovery settings")
        self.recovery_attempts = recovery_attempts
        self.recovery_delay = recovery_delay
        self.rate_limit_attempts = rate_limit_attempts
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=normalized,
            timeout=httpx.Timeout(10),
            headers={"User-Agent": "CandlePilot/0.1"},
        )
        self._time_offset_ms = 0

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def sync_time(self) -> None:
        response = await self._client.get("/fapi/v1/time")
        response.raise_for_status()
        server_ms = int(response.json()["serverTime"])
        self._time_offset_ms = server_ms - int(time.time() * 1000)

    def _signed_params(self, params: dict[str, Any]) -> str:
        values = {
            key: str(value).lower() if isinstance(value, bool) else str(value)
            for key, value in params.items()
            if value is not None
        }
        values["timestamp"] = str(int(time.time() * 1000) + self._time_offset_ms)
        values.setdefault("recvWindow", "5000")
        query = urlencode(values)
        signature = hmac.new(
            self.credentials.api_secret.get_secret_value().encode(),
            query.encode(),
            hashlib.sha256,
        ).hexdigest()
        return f"{query}&signature={signature}"

    async def _signed_request(
        self, method: str, path: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        time_resynced = False
        for attempt in range(self.rate_limit_attempts):
            query = self._signed_params(params)
            try:
                response = await self._client.request(
                    method,
                    f"{path}?{query}",
                    headers={"X-MBX-APIKEY": self.credentials.api_key.get_secret_value()},
                )
            except httpx.TimeoutException as exc:
                raise TimeoutError("Binance testnet request status is unknown") from exc
            try:
                payload = response.json()
            except ValueError as exc:
                raise BinanceApiError(0, response.text, response.status_code) from exc
            if response.status_code in {418, 429} and attempt + 1 < self.rate_limit_attempts:
                retry_after = float(response.headers.get("Retry-After", self.recovery_delay))
                if retry_after:
                    await asyncio.sleep(retry_after)
                continue
            code = int(payload.get("code", 0)) if isinstance(payload, dict) else 0
            if response.is_error and code == -1021 and not time_resynced:
                await self.sync_time()
                time_resynced = True
                continue
            if response.is_error:
                raise BinanceApiError(
                    code,
                    str(payload.get("msg", response.text)),
                    response.status_code,
                )
            return payload
        raise BinanceApiError(0, "rate limit retry budget exhausted", 429)

    async def account(self) -> dict[str, Any]:
        return await self._signed_request("GET", "/fapi/v3/account", {})

    async def reconcile_account(self) -> ReconciliationReport:
        await self.sync_time()
        account, open_orders, open_algo_orders = await asyncio.gather(
            self.account(),
            self._signed_request("GET", "/fapi/v1/openOrders", {}),
            self._signed_request("GET", "/fapi/v1/openAlgoOrders", {}),
        )
        positions = {
            item["symbol"]
            for item in account.get("positions", [])
            if Decimal(str(item.get("positionAmt", "0"))) != 0
        }
        protected = {
            item["symbol"]
            for item in open_algo_orders
            if item.get("orderType") in {"STOP", "STOP_MARKET"}
            and (
                item.get("closePosition") in {True, "true", "TRUE"}
                or item.get("reduceOnly") in {True, "true", "TRUE"}
            )
        }
        unprotected = tuple(sorted(positions - protected))
        pending_entries = tuple(
            sorted(
                {
                    str(item["symbol"])
                    for item in open_orders
                    if item.get("reduceOnly") not in {True, "true", "TRUE"}
                }
            )
        )
        return ReconciliationReport(
            position_symbols=tuple(sorted(positions)),
            open_order_count=len(open_orders) + len(open_algo_orders),
            unprotected_symbols=unprotected,
            pending_entry_symbols=pending_entries,
        )

    async def protective_levels(self) -> dict[str, ProtectiveLevels]:
        """Read the live stop/take-profit triggers guarding each position.

        The exchange is the only authority here: a bracket can be filled,
        cancelled, or amended outside this process, so the levels are read back
        rather than remembered from whatever was placed at entry.
        """

        orders = await self._signed_request("GET", "/fapi/v1/openAlgoOrders", {})
        stops: dict[str, Decimal] = {}
        targets: dict[str, Decimal] = {}
        for item in orders:
            if item.get("closePosition") not in {True, "true", "TRUE"}:
                continue
            raw_price = item.get("triggerPrice", item.get("stopPrice"))
            if raw_price is None:
                continue
            price = Decimal(str(raw_price))
            if price <= 0:
                continue
            symbol = str(item.get("symbol", ""))
            order_type = item.get("orderType")
            if order_type in {"STOP", "STOP_MARKET"}:
                stops[symbol] = price
            elif order_type in {"TAKE_PROFIT", "TAKE_PROFIT_MARKET"}:
                targets[symbol] = price
        return {
            symbol: ProtectiveLevels(stop_loss=stops.get(symbol), take_profit=targets.get(symbol))
            for symbol in stops.keys() | targets.keys()
        }

    async def configure_symbol(self, symbol: str, leverage: int) -> None:
        if not 1 <= leverage <= 10:
            raise ValueError("testnet leverage must be between 1 and 10")
        try:
            await self._signed_request(
                "POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "ISOLATED"}
            )
        except BinanceApiError as exc:
            if exc.code != -4046:  # No need to change margin type.
                raise
        await self._signed_request(
            "POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage}
        )

    async def execute_with_stop(
        self,
        order: OrderPlan,
        *,
        leverage: int,
        replace_existing_protection: bool = False,
    ) -> ExecutionReport:
        if order.reduce_only:
            return await self._place_order(order)
        if order.stop_price is None:
            raise ValueError("testnet opening orders require a protective stop")
        if order.take_profit_price is None:
            raise ValueError("testnet opening orders require a take profit")
        await self.sync_time()
        stale_protection = (
            await self._candlepilot_protective_orders(order.symbol)
            if replace_existing_protection
            else ()
        )
        await self.configure_symbol(order.symbol, leverage)
        entry = await self._place_order(order)
        if entry.status not in {"NEW", "PARTIALLY_FILLED", "FILLED"}:
            return entry
        exit_side = "SELL" if order.side == "BUY" else "BUY"
        fresh_protection: list[str] = []
        try:
            fresh_protection.append(
                await self._place_protective(
                    order, exit_side, "STOP_MARKET", order.stop_price, "sl"
                )
            )
            fresh_protection.append(
                await self._place_protective(
                    order,
                    exit_side,
                    "TAKE_PROFIT_MARKET",
                    order.take_profit_price,
                    "tp",
                )
            )
        except Exception as exc:
            for client_order_id in fresh_protection:
                try:
                    await self._cancel_algo_order(client_order_id)
                except Exception:
                    pass
            rescue: ExecutionReport | None = None
            rescue_error: Exception | None = None
            try:
                rescue = await self._emergency_reduce(order, exit_side)
            except Exception as emergency_exc:
                rescue_error = emergency_exc
            error_code = exc.code if isinstance(exc, BinanceApiError) else None
            message = (
                "entry succeeded but protective bracket failed; "
                "emergency reduce-only order submitted"
                if rescue is not None
                else "entry succeeded but protective bracket and emergency reduce failed"
            )
            if rescue_error is not None:
                message = f"{message}: {type(rescue_error).__name__}"
            raise ProtectiveStopError(
                message,
                entry=entry,
                rescue=rescue,
                exchange_error_code=error_code,
                estimated_loss_usdt=self._estimated_rescue_loss(order, entry, rescue),
                failed_stage="PROTECTION" if rescue is not None else "RESCUE",
                requires_emergency_lock=rescue is None,
            ) from exc
        try:
            for client_order_id in stale_protection:
                await self._cancel_algo_order(client_order_id)
        except Exception as exc:
            raise ProtectiveStopError(
                "replacement bracket is active but stale protection could not be removed",
                entry=entry,
                rescue=None,
                exchange_error_code=exc.code if isinstance(exc, BinanceApiError) else None,
                estimated_loss_usdt=None,
                failed_stage="PROTECTION",
                requires_emergency_lock=False,
            ) from exc
        return entry

    async def _candlepilot_protective_orders(self, symbol: str) -> tuple[str, ...]:
        orders = await self._signed_request(
            "GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol}
        )
        return tuple(
            str(item["clientAlgoId"])
            for item in orders
            if item.get("orderType") in {"STOP", "STOP_MARKET", "TAKE_PROFIT_MARKET"}
            and item.get("closePosition") in {True, "true", "TRUE"}
            and str(item.get("clientAlgoId", "")).startswith("cp-")
            and str(item["clientAlgoId"]).endswith(("-sl", "-tp"))
        )

    async def _cancel_algo_order(self, client_order_id: str) -> None:
        try:
            await self._signed_request(
                "DELETE",
                "/fapi/v1/algoOrder",
                {"clientAlgoId": client_order_id},
            )
        except BinanceApiError as exc:
            if exc.code != -2011:
                raise

    async def _place_protective(
        self,
        order: OrderPlan,
        side: str,
        order_type: str,
        trigger_price: Decimal,
        suffix: str,
    ) -> str:
        """Place a close-position protective trigger (stop loss or take profit).

        Both legs use ``closePosition`` with mark-price triggering so a filled
        entry is bracketed on the exchange itself; ``STOP_MARKET`` and
        ``TAKE_PROFIT_MARKET`` both read ``stopPrice`` as the trigger.
        """

        client_order_id = f"{order.client_order_id}-{suffix}"
        await self._signed_request(
            "POST",
            "/fapi/v1/algoOrder",
            {
                "algoType": "CONDITIONAL",
                "symbol": order.symbol,
                "side": side,
                "type": order_type,
                "triggerPrice": trigger_price,
                "closePosition": True,
                "workingType": "MARK_PRICE",
                "clientAlgoId": client_order_id,
            },
        )
        return client_order_id

    async def _place_order(self, order: OrderPlan) -> ExecutionReport:
        params: dict[str, Any] = {
            "symbol": order.symbol,
            "side": order.side,
            "type": order.order_type.value,
            "quantity": order.quantity,
            "newClientOrderId": order.client_order_id,
            "reduceOnly": order.reduce_only,
        }
        if order.order_type == OrderType.LIMIT:
            params.update({"price": order.price, "timeInForce": "GTC"})
        else:
            params["newOrderRespType"] = "RESULT"
        try:
            payload = await self._signed_request("POST", "/fapi/v1/order", params)
        except TimeoutError:
            payload = await self._recover_unknown_order(order)
        executed = Decimal(str(payload.get("executedQty", "0")))
        average = Decimal(str(payload.get("avgPrice", "0")))
        return ExecutionReport(
            client_order_id=payload.get("clientOrderId", order.client_order_id),
            status=payload.get("status", "NEW"),
            filled_quantity=executed,
            average_price=average if average > 0 else None,
            message="Binance USD-M futures testnet",
            timestamp=datetime.now(UTC),
        )

    async def _recover_unknown_order(self, order: OrderPlan) -> dict[str, Any]:
        for attempt in range(self.recovery_attempts):
            if attempt and self.recovery_delay:
                await asyncio.sleep(self.recovery_delay * (2 ** (attempt - 1)))
            try:
                return await self._signed_request(
                    "GET",
                    "/fapi/v1/order",
                    {
                        "symbol": order.symbol,
                        "origClientOrderId": order.client_order_id,
                    },
                )
            except BinanceApiError as exc:
                if exc.code != -2013:
                    raise
        raise OrderStatusUnknown(order.client_order_id)

    async def _emergency_reduce(self, order: OrderPlan, side: str) -> ExecutionReport:
        return await self._place_order(
            OrderPlan(
                client_order_id=f"{order.client_order_id}-rescue",
                symbol=order.symbol,
                side=side,
                quantity=order.quantity,
                order_type=OrderType.MARKET,
                reduce_only=True,
            )
        )

    @staticmethod
    def _estimated_rescue_loss(
        order: OrderPlan,
        entry: ExecutionReport,
        rescue: ExecutionReport | None,
    ) -> Decimal | None:
        if (
            rescue is None
            or entry.average_price is None
            or rescue.average_price is None
            or entry.filled_quantity <= 0
            or rescue.filled_quantity <= 0
        ):
            return None
        quantity = min(entry.filled_quantity, rescue.filled_quantity)
        pnl = (
            (rescue.average_price - entry.average_price) * quantity
            if order.side == "BUY"
            else (entry.average_price - rescue.average_price) * quantity
        )
        return max(Decimal("0"), -pnl)

    async def cancel_all(self, symbol: str) -> None:
        await asyncio.gather(
            self._signed_request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol}),
            self._signed_request("DELETE", "/fapi/v1/algoOpenOrders", {"symbol": symbol}),
        )

    async def handle_user_event(self, event: UserStreamEvent) -> None:
        """Freeze a partially-filled CandlePilot entry so it cannot reopen after its stop."""

        if event.event_type != "ORDER_TRADE_UPDATE":
            return
        order = event.payload.get("o", {})
        client_order_id = str(order.get("c", ""))
        if (
            order.get("X") != "PARTIALLY_FILLED"
            or not client_order_id.startswith("cp-")
            or client_order_id.endswith(("-sl", "-tp", "-rescue"))
            or order.get("R") in {True, "true", "TRUE"}
        ):
            return
        try:
            await self._signed_request(
                "DELETE",
                "/fapi/v1/order",
                {
                    "symbol": str(order["s"]),
                    "origClientOrderId": client_order_id,
                },
            )
        except BinanceApiError as exc:
            if exc.code != -2011:  # Already completed or cancelled before this event was handled.
                raise

    async def emergency_flatten(self) -> None:
        await self.sync_time()
        account, open_orders, open_algo_orders = await asyncio.gather(
            self.account(),
            self._signed_request("GET", "/fapi/v1/openOrders", {}),
            self._signed_request("GET", "/fapi/v1/openAlgoOrders", {}),
        )
        positions = {
            str(position["symbol"]): Decimal(str(position.get("positionAmt", "0")))
            for position in account.get("positions", [])
            if Decimal(str(position.get("positionAmt", "0"))) != 0
        }
        order_symbols = {
            str(order["symbol"])
            for order in [*open_orders, *open_algo_orders]
            if order.get("symbol")
        }

        failures: list[str] = []
        for symbol in sorted(order_symbols | positions.keys()):
            try:
                await self.cancel_all(symbol)
            except Exception as exc:
                failures.append(f"cancel {symbol}: {type(exc).__name__}")

        # Cancellation failure must not prevent the more important attempt to
        # flatten known exposure. The lock remains active and the aggregate error
        # is surfaced after every position has had its close attempted.
        for symbol, quantity in positions.items():
            try:
                await self._signed_request(
                    "POST",
                    "/fapi/v1/order",
                    {
                        "symbol": symbol,
                        "side": "SELL" if quantity > 0 else "BUY",
                        "type": "MARKET",
                        "quantity": abs(quantity),
                        "reduceOnly": True,
                        "newClientOrderId": f"cp-kill-{int(time.time() * 1000)}",
                    },
                )
            except Exception as exc:
                failures.append(f"flatten {symbol}: {type(exc).__name__}")
        if failures:
            raise RuntimeError("emergency account cleanup incomplete: " + "; ".join(failures))
