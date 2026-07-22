from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from statistics import fmean
from typing import Any

from candlepilot.analysis.indicators import closed_klines, raw_bar, summarize
from candlepilot.market.binance import BinancePublicClient
from candlepilot.market.features import Kline


TIMEFRAMES = ("5m", "15m", "1h")
RAW_BAR_LIMIT = 60
INDICATOR_BAR_LIMIT = 150
DATA_VERSION = "kansoku-compatible-crypto-v1"


def _relative_volume(rows: list[Kline]) -> dict[str, Any] | None:
    """Compare today's UTC cumulative volume with prior days at the same time.

    Kansoku aligns US equity sessions.  Crypto has no exchange close, so UTC is
    the reproducible session boundary and quote volume is the comparable unit.
    """

    if not rows:
        return None
    by_day: dict[str, list[Kline]] = {}
    for row in rows:
        by_day.setdefault(row.open_time.astimezone(UTC).date().isoformat(), []).append(row)
    today = max(by_day)
    today_rows = by_day[today]
    cutoff = max(item.open_time.hour * 60 + item.open_time.minute for item in today_rows)
    today_cum = sum(float(item.quote_volume) for item in today_rows)
    prior = sorted(day for day in by_day if day < today)[-5:]
    if not prior:
        return None
    baselines = [
        sum(
            float(item.quote_volume)
            for item in by_day[day]
            if item.open_time.hour * 60 + item.open_time.minute <= cutoff
        )
        for day in prior
    ]
    average = fmean(baselines)
    if average <= 0:
        return None
    return {
        "ratio": today_cum / average,
        "today_cumulative_quote_volume": today_cum,
        "baseline_average": average,
        "days_used": len(baselines),
        "cutoff_utc_minute": cutoff,
    }


def _depth_flow(depth: Mapping[str, Any], trades: list[Mapping[str, Any]]) -> dict[str, Any]:
    bid_notional = sum(float(price) * float(quantity) for price, quantity in depth["bids"])
    ask_notional = sum(float(price) * float(quantity) for price, quantity in depth["asks"])
    total_depth = bid_notional + ask_notional
    buyer_notional = 0.0
    seller_notional = 0.0
    for trade in trades:
        notional = float(trade["p"]) * float(trade["q"])
        if bool(trade.get("m")):
            seller_notional += notional
        else:
            buyer_notional += notional
    total_trades = buyer_notional + seller_notional
    return {
        "depth_bid_notional": bid_notional,
        "depth_ask_notional": ask_notional,
        "depth_imbalance": (bid_notional - ask_notional) / total_depth if total_depth else 0,
        "recent_taker_buy_notional": buyer_notional,
        "recent_taker_sell_notional": seller_notional,
        "recent_taker_imbalance": (
            (buyer_notional - seller_notional) / total_trades if total_trades else 0
        ),
        "recent_trade_count": len(trades),
    }


class AnalysisDataPackBuilder:
    def __init__(self, market: BinancePublicClient) -> None:
        self.market = market

    async def build(
        self,
        symbol: str,
        *,
        account: Mapping[str, Any] | None,
        previous_analysis: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        symbol = symbol.upper()
        (
            rows_5m,
            rows_15m,
            rows_1h,
            book,
            premium,
            depth,
            interest,
            trades,
            ticker,
            btc,
            eth,
        ) = await asyncio.gather(
            self.market.klines(symbol, "5m", INDICATOR_BAR_LIMIT),
            self.market.klines(symbol, "15m", 500),
            self.market.klines(symbol, "1h", INDICATOR_BAR_LIMIT),
            self.market.book_ticker(symbol),
            self.market.premium_index(symbol),
            self.market.depth(symbol, 20),
            self.market.open_interest(symbol),
            self.market.agg_trades(symbol, 1000),
            self.market.ticker_24h(symbol),
            self.market.premium_index("BTCUSDT"),
            self.market.premium_index("ETHUSDT"),
        )
        parsed = {
            "5m": closed_klines(rows_5m),
            "15m": closed_klines(rows_15m),
            "1h": closed_klines(rows_1h),
        }
        for timeframe, items in parsed.items():
            if len(items) < 60:
                raise ValueError(f"{timeframe} requires at least 60 closed bars")
        frames = {
            timeframe: {
                "bars": [raw_bar(item) for item in items[-RAW_BAR_LIMIT:]],
                "summary": summarize(
                    items[-INDICATOR_BAR_LIMIT:], include_vwap=timeframe in {"5m", "15m"}
                ),
            }
            for timeframe, items in parsed.items()
        }
        mark = float(premium["markPrice"])
        index = float(premium["indexPrice"])
        position = None
        if account is not None:
            position = next(
                (
                    row
                    for row in account.get("positions", [])
                    if row.get("symbol") == symbol and float(row.get("positionAmt", 0)) != 0
                ),
                None,
            )
        return {
            "data_version": DATA_VERSION,
            "as_of": datetime.now(UTC).isoformat(),
            "symbol": symbol,
            "timeframes": frames,
            "quote": {
                "mark_price": mark,
                "index_price": index,
                "basis_fraction": (mark - index) / index if index else 0,
                "bid": float(book["bidPrice"]),
                "ask": float(book["askPrice"]),
                "price_change_percent_24h": float(ticker["priceChangePercent"]),
                "quote_volume_24h": float(ticker["quoteVolume"]),
            },
            "relative_volume": _relative_volume(parsed["15m"]),
            "derivatives": {
                "funding_rate": float(premium["lastFundingRate"]),
                "next_funding_time": datetime.fromtimestamp(
                    int(premium["nextFundingTime"]) / 1000, tz=UTC
                ).isoformat(),
                "open_interest_contracts": float(interest["openInterest"]),
                **_depth_flow(depth, trades),
            },
            "market_benchmarks": {
                "adaptation": "BTCUSDT and ETHUSDT replace Kansoku's SPY and QQQ for crypto",
                "btc": {"mark_price": float(btc["markPrice"]), "funding_rate": float(btc["lastFundingRate"])},
                "eth": {"mark_price": float(eth["markPrice"]), "funding_rate": float(eth["lastFundingRate"])},
            },
            "account": {
                "available": account is not None,
                "total_wallet_balance": account.get("totalWalletBalance") if account else None,
                "available_balance": account.get("availableBalance") if account else None,
                "position": position,
            },
            "previous_analysis": previous_analysis,
            "lessons": [],
            "unavailable_inputs": {
                "news": "no news source is configured",
                "event_calendar": "no crypto event calendar is configured",
                "options_levels": "no options open-interest source is configured",
                "pro_pattern_detectors": "not present in Kansoku's public implementation",
            },
        }
