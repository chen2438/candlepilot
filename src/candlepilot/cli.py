from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

import uvicorn

from candlepilot.config import Settings
from candlepilot.market.binance import BinanceError, BinancePublicClient
from candlepilot.providers.registry import ProviderRegistry


async def _doctor() -> int:
    settings = Settings.from_env()
    providers = await ProviderRegistry().health()
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
        "mode": settings.mode.value,
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="candlepilot", description="Local CandlePilot trading control service"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="check LLM auth and Binance public connectivity")
    commands.add_parser("serve", help="start the local API and built web console")
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.command == "doctor":
        raise SystemExit(asyncio.run(_doctor()))
    settings = Settings.from_env()
    if settings.bind_host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("CandlePilot v0.1 only permits a localhost bind address")
    uvicorn.run(
        "candlepilot.api:app",
        host=settings.bind_host,
        port=settings.bind_port,
        reload=False,
    )


if __name__ == "__main__":
    main()

