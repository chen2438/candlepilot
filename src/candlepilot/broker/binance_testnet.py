from __future__ import annotations

import hashlib
import hmac
import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlencode
from uuid import uuid4

import httpx
from pydantic import SecretStr

from candlepilot.domain.models import ExecutionReport, OrderPlan, OrderType
from candlepilot.market.binance import BINANCE_FUTURES_TESTNET
from candlepilot.risk.engine import SymbolRules

if TYPE_CHECKING:
    from candlepilot.broker.user_stream import UserStreamEvent


class BinanceApiError(RuntimeError):
    def __init__(
        self,
        code: int,
        message: str,
        status_code: int,
        *,
        method: str | None = None,
        path: str | None = None,
    ) -> None:
        request = f" {method} {path}" if method is not None and path is not None else ""
        super().__init__(f"Binance{request} error {code}: {message}")
        self.code = code
        self.status_code = status_code
        self.method = method
        self.path = path


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


class ManualCloseError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        report: ExecutionReport,
        stage: Literal["FILL", "VERIFY", "CLEANUP"],
    ) -> None:
        super().__init__(message)
        self.report = report
        self.stage = stage


@dataclass(frozen=True, slots=True)
class BinanceTestnetCredentials:
    api_key: SecretStr
    api_secret: SecretStr


@dataclass(frozen=True, slots=True)
class ProtectiveLevels:
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ProtectiveAlgoOrder:
    client_order_id: str
    order_type: Literal["STOP_MARKET", "TAKE_PROFIT_MARKET"]
    trigger_price: Decimal | None


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

    async def income_24h(self, *, now: datetime | None = None) -> Decimal:
        """Return trading income components from the trailing 24-hour window.

        Deposits and transfers are deliberately excluded: the 24-hour loss breaker
        measures trading performance, not account funding. Unrealized PnL comes
        from the account payload and is added by the portfolio assembler.
        """

        now = now or datetime.now(UTC)
        if now.tzinfo is None:
            raise ValueError("24-hour income time must be timezone-aware")
        now = now.astimezone(UTC)
        start = now - timedelta(hours=24)
        total = Decimal("0")
        page = 1
        while True:
            rows = await self._signed_request(
                "GET",
                "/fapi/v1/income",
                {
                    "startTime": int(start.timestamp() * 1000),
                    "endTime": int(now.timestamp() * 1000),
                    "page": page,
                    "limit": 1000,
                },
            )
            for row in rows:
                if row.get("incomeType") in {"REALIZED_PNL", "COMMISSION", "FUNDING_FEE"}:
                    total += Decimal(str(row.get("income", "0")))
            if len(rows) < 1000:
                return total
            page += 1

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
                raise BinanceApiError(
                    0,
                    response.text,
                    response.status_code,
                    method=method,
                    path=path,
                ) from exc
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
                    method=method,
                    path=path,
                )
            return payload
        raise BinanceApiError(
            0,
            "rate limit retry budget exhausted",
            429,
            method=method,
            path=path,
        )

    async def account(self) -> dict[str, Any]:
        return await self._signed_request("GET", "/fapi/v3/account", {})

    async def position_risk(self) -> list[dict[str, Any]]:
        """Return exchange-authoritative position prices and unrealized PnL.

        The v3 account summary contains position quantities and margin fields,
        but does not include a usable mark price.  Position Risk v3 is the
        signed source for both mark and entry prices.
        """

        return await self._signed_request("GET", "/fapi/v3/positionRisk", {})

    async def symbol_configuration(
        self, symbol: str | None = None
    ) -> list[dict[str, Any]]:
        """Return leverage and margin mode removed from Binance's v3 snapshots."""

        return await self._signed_request(
            "GET", "/fapi/v1/symbolConfig", {"symbol": symbol}
        )

    async def _exchange_info(self) -> dict[str, Any]:
        path = "/fapi/v1/exchangeInfo"
        try:
            response = await self._client.get(path)
        except httpx.HTTPError as exc:
            raise BinanceApiError(
                0,
                "testnet exchange-info transport failure",
                0,
                method="GET",
                path=path,
            ) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise BinanceApiError(
                0,
                response.text,
                response.status_code,
                method="GET",
                path=path,
            ) from exc
        if response.is_error:
            code = int(payload.get("code", 0)) if isinstance(payload, dict) else 0
            message = (
                str(payload.get("msg", response.text))
                if isinstance(payload, dict)
                else response.text
            )
            raise BinanceApiError(
                code,
                message,
                response.status_code,
                method="GET",
                path=path,
            )
        return payload

    async def tradable_contract_rules(self) -> dict[str, SymbolRules]:
        """Return filters from the execution venue, not Binance production."""

        payload = await self._exchange_info()
        rules: dict[str, SymbolRules] = {}
        for item in payload.get("symbols", []):
            if not (
                item.get("contractType") == "PERPETUAL"
                and item.get("quoteAsset") == "USDT"
                and item.get("status") == "TRADING"
            ):
                continue
            filters = {entry["filterType"]: entry for entry in item.get("filters", [])}
            lot = filters.get("LOT_SIZE", {})
            market_lot = filters.get("MARKET_LOT_SIZE", {})
            notional = filters.get("MIN_NOTIONAL", {})
            price = filters.get("PRICE_FILTER", {})
            rules[str(item["symbol"])] = SymbolRules(
                quantity_step=Decimal(lot.get("stepSize", "1")),
                min_quantity=Decimal(lot.get("minQty", "1")),
                min_notional=Decimal(notional.get("notional", "5")),
                tick_size=Decimal(price.get("tickSize", "0.01")),
                max_quantity=(
                    Decimal(lot["maxQty"]) if lot.get("maxQty") is not None else None
                ),
                market_quantity_step=(
                    Decimal(market_lot["stepSize"])
                    if market_lot.get("stepSize") is not None
                    else None
                ),
                market_min_quantity=(
                    Decimal(market_lot["minQty"])
                    if market_lot.get("minQty") is not None
                    else None
                ),
                market_max_quantity=(
                    Decimal(market_lot["maxQty"])
                    if market_lot.get("maxQty") is not None
                    else None
                ),
            )
        return rules

    async def tradable_symbols(self) -> frozenset[str]:
        """Return the USDT perpetual contracts the testnet can accept now."""

        return frozenset((await self.tradable_contract_rules()).keys())

    async def account_snapshot(self) -> dict[str, Any]:
        """Return balances plus exchange-authoritative live position fields."""

        account, risk_rows, configuration_rows = await asyncio.gather(
            self.account(), self.position_risk(), self.symbol_configuration()
        )
        risk_by_symbol = {
            str(item.get("symbol", "")): {
                **item,
                "unrealizedProfit": item.get(
                    "unRealizedProfit", item.get("unrealizedProfit", "0")
                ),
            }
            for item in risk_rows
        }
        configuration_by_symbol = {
            str(item.get("symbol", "")): item for item in configuration_rows
        }
        positions: list[dict[str, Any]] = []
        for item in account.get("positions", []):
            symbol = str(item.get("symbol", ""))
            risk = risk_by_symbol.get(symbol, {})
            configuration = configuration_by_symbol.get(symbol, {})
            # V3 deliberately removes symbol configuration. Keep quantities and
            # margin figures from the account response, enrich only live risk
            # fields from positionRisk, then restore leverage/margin mode from
            # the dedicated configuration endpoint.
            merged = {
                **item,
                **{
                    key: risk[key]
                    for key in (
                        "entryPrice",
                        "markPrice",
                        "unrealizedProfit",
                        "unRealizedProfit",
                        "notional",
                    )
                    if key in risk
                },
                **{
                    key: configuration[key]
                    for key in ("leverage", "marginType")
                    if key in configuration
                },
            }
            margin_type = str(merged.get("marginType", "")).upper()
            if margin_type:
                merged["isolated"] = margin_type == "ISOLATED"
            if (
                Decimal(str(merged.get("positionAmt", "0"))) != 0
                and merged.get("entryPrice") is None
            ):
                raise AccountReconciliationError(
                    f"position risk response is missing entry price for {symbol}"
                )
            positions.append(merged)
        return {
            **account,
            "positions": positions,
        }

    async def pending_entry_symbols(self) -> tuple[str, ...]:
        """Return symbols with a live non-reduce-only order on the exchange."""

        open_orders, open_algo_orders = await asyncio.gather(
            self._signed_request("GET", "/fapi/v1/openOrders", {}),
            self._signed_request("GET", "/fapi/v1/openAlgoOrders", {}),
        )
        return tuple(
            sorted(
                {
                    str(item["symbol"])
                    for item in [*open_orders, *open_algo_orders]
                    if item.get("reduceOnly") not in {True, "true", "TRUE"}
                    and item.get("closePosition") not in {True, "true", "TRUE"}
                }
            )
        )

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
                    for item in [*open_orders, *open_algo_orders]
                    if item.get("reduceOnly") not in {True, "true", "TRUE"}
                    and item.get("closePosition") not in {True, "true", "TRUE"}
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
        rows = await self.symbol_configuration(symbol)
        current = next(
            (item for item in rows if str(item.get("symbol", "")) == symbol),
            None,
        )
        if current is None:
            raise AccountReconciliationError(
                f"symbol configuration response is missing {symbol}"
            )
        margin_type = str(current.get("marginType", "")).upper()
        if margin_type != "ISOLATED":
            try:
                await self._signed_request(
                    "POST",
                    "/fapi/v1/marginType",
                    {"symbol": symbol, "marginType": "ISOLATED"},
                )
            except BinanceApiError as exc:
                if exc.code != -4046:  # No need to change margin type.
                    raise
        try:
            current_leverage = int(current.get("leverage", 0))
        except (TypeError, ValueError):
            current_leverage = 0
        if current_leverage != leverage:
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
        invalid_stale = next(
            (item.client_order_id for item in stale_protection if item.trigger_price is None),
            None,
        )
        if invalid_stale is not None:
            raise AccountReconciliationError(
                f"protective order {invalid_stale} has no valid trigger price"
            )
        await self.configure_symbol(order.symbol, leverage)
        entry = await self._place_order(order)
        if entry.status not in {"NEW", "PARTIALLY_FILLED", "FILLED"}:
            return entry
        if entry.status in {"NEW", "PARTIALLY_FILLED"}:
            try:
                cancellation = await self._signed_request(
                    "DELETE",
                    "/fapi/v1/order",
                    {
                        "symbol": order.symbol,
                        "origClientOrderId": order.client_order_id,
                    },
                )
            except Exception:
                cancellation = await self._recover_terminal_order(order)
            canceled_quantity = Decimal(
                str(cancellation.get("executedQty", entry.filled_quantity))
            )
            canceled_average = Decimal(
                str(cancellation.get("avgPrice", entry.average_price or "0"))
            )
            resolved_status = cancellation.get("status", entry.status)
            if canceled_quantity >= order.quantity:
                resolved_status = "FILLED"
            entry = entry.model_copy(
                update={
                    "status": resolved_status,
                    "filled_quantity": max(entry.filled_quantity, canceled_quantity),
                    "average_price": canceled_average
                    if canceled_average > 0
                    else entry.average_price,
                }
            )
            if entry.filled_quantity == 0:
                return entry.model_copy(
                    update={
                        "status": "CANCELED",
                        "message": "unfilled opening limit canceled before protection",
                        "timestamp": datetime.now(UTC),
                    }
                )
            if entry.status != "FILLED":
                entry = entry.model_copy(update={"status": "PARTIALLY_FILLED"})
        protected_order = (
            order.model_copy(update={"quantity": entry.filled_quantity})
            if entry.status == "PARTIALLY_FILLED" and entry.filled_quantity > 0
            else order
        )
        exit_side = "SELL" if order.side == "BUY" else "BUY"
        cancelled_stale: list[ProtectiveAlgoOrder] = []
        fresh_protection: list[str] = []
        try:
            # Binance rejects a second closePosition stop/take-profit in the same
            # direction with -4130. Preserve the old bracket through the entry,
            # then remove it before placing the replacement pair.
            for stale_order in stale_protection:
                await self._cancel_algo_order(stale_order.client_order_id)
                cancelled_stale.append(stale_order)
            fresh_protection.append(
                await self._place_protective(
                    protected_order, exit_side, "STOP_MARKET", order.stop_price, "sl"
                )
            )
            fresh_protection.append(
                await self._place_protective(
                    protected_order,
                    exit_side,
                    "TAKE_PROFIT_MARKET",
                    order.take_profit_price,
                    "tp",
                )
            )
        except Exception as exc:
            cleanup_failed = False
            for client_order_id in fresh_protection:
                try:
                    await self._cancel_algo_order(client_order_id)
                except Exception:
                    cleanup_failed = True
            rescue: ExecutionReport | None = None
            rescue_error: Exception | None = None
            try:
                rescue = await self._emergency_reduce(protected_order, exit_side)
            except Exception as emergency_exc:
                rescue_error = emergency_exc
            restoration_failed = False
            for stale_order in cancelled_stale:
                try:
                    assert stale_order.trigger_price is not None
                    await self._place_protective(
                        protected_order,
                        exit_side,
                        stale_order.order_type,
                        stale_order.trigger_price,
                        "sl" if stale_order.order_type == "STOP_MARKET" else "tp",
                        client_order_id=stale_order.client_order_id,
                    )
                except Exception:
                    restoration_failed = True
            error_code = exc.code if isinstance(exc, BinanceApiError) else None
            message = (
                "entry succeeded but protective bracket failed; "
                "emergency reduce-only order submitted"
                if rescue is not None
                else "entry succeeded but protective bracket and emergency reduce failed"
            )
            if rescue_error is not None:
                message = f"{message}: {type(rescue_error).__name__}"
            if cleanup_failed:
                message = f"{message}; protective-order cleanup failed"
            if restoration_failed:
                message = f"{message}; previous protective bracket restoration failed"
            raise ProtectiveStopError(
                message,
                entry=entry,
                rescue=rescue,
                exchange_error_code=error_code,
                estimated_loss_usdt=self._estimated_rescue_loss(
                    protected_order, entry, rescue
                ),
                failed_stage="PROTECTION" if rescue is not None else "RESCUE",
                requires_emergency_lock=(
                    rescue is None or cleanup_failed or restoration_failed
                ),
            ) from exc
        return entry

    async def _candlepilot_protective_orders(
        self, symbol: str
    ) -> tuple[ProtectiveAlgoOrder, ...]:
        orders = await self._signed_request(
            "GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol}
        )
        protective_orders: list[ProtectiveAlgoOrder] = []
        for item in orders:
            order_type = item.get("orderType")
            client_order_id = str(item.get("clientAlgoId", ""))
            if (
                order_type not in {"STOP_MARKET", "TAKE_PROFIT_MARKET"}
                or item.get("closePosition") not in {True, "true", "TRUE"}
                or not client_order_id.startswith("cp-")
                or not client_order_id.endswith(("-sl", "-tp"))
            ):
                continue
            raw_trigger = item.get("triggerPrice", item.get("stopPrice"))
            trigger_price = Decimal(str(raw_trigger)) if raw_trigger is not None else None
            if trigger_price is not None and trigger_price <= 0:
                trigger_price = None
            protective_orders.append(
                ProtectiveAlgoOrder(
                    client_order_id=client_order_id,
                    order_type=order_type,
                    trigger_price=trigger_price,
                )
            )
        return tuple(protective_orders)

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
        *,
        client_order_id: str | None = None,
    ) -> str:
        """Place a close-position protective trigger (stop loss or take profit).

        Both legs use ``closePosition`` with mark-price triggering so a filled
        entry is bracketed on the exchange itself; ``STOP_MARKET`` and
        ``TAKE_PROFIT_MARKET`` both read ``stopPrice`` as the trigger.
        """

        client_order_id = client_order_id or f"{order.client_order_id}-{suffix}"
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

    async def _recover_terminal_order(self, order: OrderPlan) -> dict[str, Any]:
        terminal_statuses = {
            "FILLED",
            "CANCELED",
            "REJECTED",
            "EXPIRED",
            "EXPIRED_IN_MATCH",
        }
        last_error: Exception | None = None
        for attempt in range(self.recovery_attempts):
            if attempt and self.recovery_delay:
                await asyncio.sleep(self.recovery_delay * (2 ** (attempt - 1)))
            try:
                payload = await self._signed_request(
                    "GET",
                    "/fapi/v1/order",
                    {
                        "symbol": order.symbol,
                        "origClientOrderId": order.client_order_id,
                    },
                )
            except Exception as exc:
                last_error = exc
                continue
            if payload.get("status") in terminal_statuses:
                return payload
        raise OrderStatusUnknown(order.client_order_id) from last_error

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

    async def close_position_market(self, symbol: str) -> ExecutionReport:
        """Close one live position without touching unrelated manual orders.

        The reduce-only market order is sent before CandlePilot's bracket is
        removed, so a rejected or partial close leaves the remaining exposure
        protected. Only after the exchange confirms the symbol is flat are this
        system's own stop/take-profit algo orders cancelled.
        """

        account, bracket_ids = await asyncio.gather(
            self.account_snapshot(), self._candlepilot_protective_orders(symbol)
        )
        position = next(
            (
                item
                for item in account.get("positions", [])
                if str(item.get("symbol", "")) == symbol
                and Decimal(str(item.get("positionAmt", "0"))) != 0
            ),
            None,
        )
        if position is None:
            raise AccountReconciliationError(f"no open position exists for {symbol}")
        amount = Decimal(str(position["positionAmt"]))
        order = OrderPlan(
            client_order_id=f"cp-manual-{uuid4().hex[:20]}",
            symbol=symbol,
            side="SELL" if amount > 0 else "BUY",
            quantity=abs(amount),
            order_type=OrderType.MARKET,
            reduce_only=True,
        )
        report = (await self._place_order(order)).model_copy(
            update={"message": "manual market close from account frontend"}
        )
        if report.status != "FILLED":
            raise ManualCloseError(
                f"manual close did not fill completely ({report.status})",
                report=report,
                stage="FILL",
            )
        try:
            refreshed = await self.account_snapshot()
        except Exception as exc:
            raise ManualCloseError(
                "manual close filled but the resulting position could not be verified",
                report=report,
                stage="VERIFY",
            ) from exc
        remaining = next(
            (
                Decimal(str(item.get("positionAmt", "0")))
                for item in refreshed.get("positions", [])
                if str(item.get("symbol", "")) == symbol
            ),
            Decimal("0"),
        )
        if remaining != 0:
            raise ManualCloseError(
                f"manual close left a remaining position of {remaining} {symbol}",
                report=report,
                stage="VERIFY",
            )
        cancellations = await asyncio.gather(
            *(
                self._cancel_algo_order(protection.client_order_id)
                for protection in bracket_ids
            ),
            return_exceptions=True,
        )
        failed_cancellations = [
            result for result in cancellations if isinstance(result, BaseException)
        ]
        if failed_cancellations:
            raise ManualCloseError(
                "position is flat but CandlePilot protective orders could not all be cancelled",
                report=report,
                stage="CLEANUP",
            )
        return report

    async def completed_order_fill_event(
        self, symbol: str, client_order_id: str
    ) -> UserStreamEvent | None:
        """Reconstruct one completed order from exchange trade history.

        Manual close is intentionally allowed only while the engine (and thus
        its user stream) is stopped.  Querying the exchange trades supplies the
        side, weighted price and realized PnL that the REST order response may
        omit, without guessing any execution data.
        """

        from candlepilot.broker.user_stream import UserStreamEvent

        order = await self._signed_request(
            "GET",
            "/fapi/v1/order",
            {"symbol": symbol, "origClientOrderId": client_order_id},
        )
        if order.get("status") != "FILLED" or order.get("orderId") is None:
            return None
        order_id = str(order["orderId"])
        rows = await self._signed_request(
            "GET", "/fapi/v1/userTrades", {"symbol": symbol, "orderId": order_id}
        )
        trades = [row for row in rows if str(row.get("orderId", "")) == order_id]
        if not trades:
            return None
        quantity = sum((Decimal(str(row.get("qty", "0"))) for row in trades), Decimal("0"))
        if quantity <= 0:
            return None
        quote_quantity = sum(
            (
                Decimal(str(row.get("quoteQty")))
                if row.get("quoteQty") is not None
                else Decimal(str(row.get("price", "0"))) * Decimal(str(row.get("qty", "0")))
                for row in trades
            ),
            Decimal("0"),
        )
        realized_pnl = sum(
            (Decimal(str(row.get("realizedPnl", "0"))) for row in trades), Decimal("0")
        )
        event_ms = max(int(row.get("time", 0)) for row in trades)
        event_time = datetime.fromtimestamp(event_ms / 1000, tz=UTC)
        side = str(order.get("side") or trades[0].get("side") or "")
        payload = {
            "e": "ORDER_TRADE_UPDATE",
            "E": event_ms,
            "T": event_ms,
            "_source": "rest_trade_reconciliation",
            "o": {
                "s": symbol,
                "c": client_order_id,
                "S": side,
                "x": "TRADE",
                "X": "FILLED",
                "z": str(quantity),
                "ap": str(quote_quantity / quantity),
                "R": bool(order.get("reduceOnly", True)),
                "rp": str(realized_pnl),
                "i": order["orderId"],
            },
        }
        return UserStreamEvent(
            event_type="ORDER_TRADE_UPDATE",
            event_time=event_time,
            transaction_time=event_time,
            symbol=symbol,
            payload=payload,
        )

    async def completed_exit_fill_event(
        self, symbol: str, entry_client_order_id: str
    ) -> UserStreamEvent | None:
        """Find the exchange-owned exit for one CandlePilot entry."""

        for suffix in ("-sl", "-tp", "-rescue"):
            try:
                event = await self.completed_order_fill_event(
                    symbol, f"{entry_client_order_id}{suffix}"
                )
            except BinanceApiError as exc:
                if exc.code == -2013:  # No triggered regular order under this bracket ID.
                    continue
                raise
            if event is not None:
                return event
        return None

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
