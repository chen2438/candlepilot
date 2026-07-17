from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

import uvicorn

from candlepilot.application.acceptance import (
    REQUIRED_RUNTIME_HOURS,
    evaluate_acceptance,
)
from candlepilot.broker.binance_testnet import (
    BinanceTestnetBroker,
    BinanceTestnetCredentials,
)
from candlepilot.config import Settings, load_dotenv
from candlepilot.market.binance import BinanceError, BinancePublicClient
from candlepilot.observability import configure_structured_logging
from candlepilot.providers.registry import ProviderRegistry
from candlepilot.storage.database import AuditRepository, Database


async def _doctor(settings: Settings) -> int:
    providers = await ProviderRegistry.from_settings(settings).health()
    market: dict[str, Any]
    try:
        async with BinancePublicClient() as client:
            server_time = await client.server_time()
            contracts = await client.exchange_info()
        market = {
            "available": True,
            "server_time": server_time.isoformat(),
            "usdt_perpetual_contracts": len(contracts),
        }
    except BinanceError as exc:
        market = {"available": False, "error": str(exc)}
    report = {
        "bind": f"http://{settings.bind_host}:{settings.bind_port}",
        "providers": [provider.model_dump(mode="json") for provider in providers],
        "binance_market": market,
        "testnet_credentials_configured": bool(
            settings.binance_testnet_api_key and settings.binance_testnet_api_secret
        ),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    provider_ready = any(item.available and item.authenticated for item in providers)
    return 0 if provider_ready and market.get("available") else 1


async def _testnet_reconciliation(settings: Settings) -> dict[str, Any] | None:
    if not (settings.binance_testnet_api_key and settings.binance_testnet_api_secret):
        return None
    broker = BinanceTestnetBroker(
        BinanceTestnetCredentials(
            settings.binance_testnet_api_key,
            settings.binance_testnet_api_secret,
        )
    )
    try:
        report = await broker.reconcile_account()
    except Exception:
        return None
    finally:
        await broker.close()
    return {
        "position_symbols": list(report.position_symbols),
        "open_order_count": report.open_order_count,
        "unprotected_symbols": list(report.unprotected_symbols),
        "pending_entry_symbols": list(report.pending_entry_symbols),
    }


async def _acceptance(
    settings: Settings, required_hours: float, lookback_hours: float
) -> int:
    database = Database(settings.database_url)
    await database.initialize()
    audit = AuditRepository(database.sessions)
    now = datetime.now(UTC)
    window_start = now - timedelta(hours=lookback_hours)
    try:
        executions = await audit.executions_between(window_start, now)
        risk_decisions = await audit.risk_decisions_between(window_start, now)
        inference_ids = await audit.inference_ids_between(window_start, now)
        reconciliation = await _testnet_reconciliation(settings)
    finally:
        await database.close()
    report = evaluate_acceptance(
        executions=executions,
        risk_decisions=risk_decisions,
        inference_ids=inference_ids,
        reconciliation=reconciliation,
        required_hours=required_hours,
    )
    payload = asdict(report)
    payload["lookback_hours"] = lookback_hours
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0 if report.passed else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="candlepilot", description="Local CandlePilot trading control service"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="check LLM auth and Binance public connectivity")
    commands.add_parser("serve", help="start the local API and built web console")
    acceptance = commands.add_parser(
        "acceptance",
        help="evaluate the audited testnet soak run against release invariants",
    )
    acceptance.add_argument(
        "--required-hours",
        type=float,
        default=REQUIRED_RUNTIME_HOURS,
        help="minimum continuous audited runtime required to pass (default: 24)",
    )
    acceptance.add_argument(
        "--lookback-hours",
        type=float,
        default=168.0,
        help="how far back to gather audited activity (default: 168)",
    )
    return parser


def main() -> None:
    load_dotenv()
    args = _parser().parse_args()
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        # A rejected .env is a user-fixable mistake, not a crash. Print what is
        # wrong instead of a traceback through the config parser.
        raise SystemExit(str(exc)) from exc
    if args.command == "doctor":
        raise SystemExit(asyncio.run(_doctor(settings)))
    if args.command == "acceptance":
        raise SystemExit(
            asyncio.run(
                _acceptance(settings, args.required_hours, args.lookback_hours)
            )
        )
    if settings.bind_host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("CandlePilot v0.1 only permits a localhost bind address")
    # Binance testnet is the only account this system trades, so a missing key is
    # a dead start. Say so here rather than letting it surface as a traceback out
    # of uvicorn's import of the app factory.
    if not (settings.binance_testnet_api_key and settings.binance_testnet_api_secret):
        raise SystemExit(
            "BINANCE_TESTNET_API_KEY and BINANCE_TESTNET_API_SECRET are required.\n"
            "Create a key at https://testnet.binancefuture.com and add both to .env."
        )
    configure_structured_logging()
    uvicorn.run(
        "candlepilot.api:create_app",
        factory=True,
        host=settings.bind_host,
        port=settings.bind_port,
        reload=False,
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
