from datetime import UTC, datetime, timedelta
from decimal import Decimal


from candlepilot.broker.binance_testnet import ProtectiveLevels
from candlepilot.domain.models import (
    MarketSnapshot,
    ProviderHealth,
    TradeIntent,
)
from candlepilot.market.scanner import MarketCandidateInput
from candlepilot.providers.base import DecisionProvider, ProviderResult


class ApiProvider(DecisionProvider):
    name = "api-fixture"

    async def health_check(self):
        return ProviderHealth(provider=self.name, available=True, authenticated=True)

    async def generate_trade_intent(self, snapshot, portfolio):
        intent = TradeIntent.hold(snapshot.symbol, snapshot.cadence, "fixture")
        return ProviderResult(intent, self.name, None, timedelta(0), intent.model_dump_json(), {})


class ConfigurableProvider(ApiProvider):
    name = "api-fixture"
    reasoning_effort_options = ("low", "medium", "high")


class CustomApiProvider(ApiProvider):
    name = "openai-compatible:main"


class BrokenProvider(DecisionProvider):
    name = "broken-fixture"

    async def health_check(self):
        return ProviderHealth(provider=self.name, available=True, authenticated=True)

    async def generate_trade_intent(self, snapshot, portfolio):
        raise RuntimeError("model 'bogus' is not available")


class ApiMarket:
    async def candidate_inputs(self):
        return [
            MarketCandidateInput(
                symbol="BTCUSDT",
                quote_volume_24h=Decimal("1000000"),
                bid=Decimal("99.9"),
                ask=Decimal("100.1"),
                volatility=Decimal("0.1"),
                trend_strength=Decimal("0.03"),
                listing_age_days=1000,
            )
        ]

    async def close(self):
        return None

    async def market_snapshot(self, symbol, cadence):
        return MarketSnapshot(
            symbol=symbol,
            cadence=cadence,
            timestamp=datetime.now(UTC),
            mark_price="100",
            bid="99.9",
            ask="100.1",
            quote_volume_24h="1000000",
        )

    async def historical_klines(self, symbol, interval, start, end, *, max_candles=10_000):
        step = {
            "1m": 60_000,
            "5m": 300_000,
            "15m": 900_000,
            "30m": 1_800_000,
            "1h": 3_600_000,
            "4h": 14_400_000,
        }[interval]
        start_ms = int(start.timestamp() * 1000)
        return [
            [
                start_ms + offset * step,
                "100",
                "101",
                "99",
                "100",
                "10",
                start_ms + (offset + 1) * step - 1,
                "1000",
            ]
            for offset in range(min(2, max_candles))
        ]

    async def historical_mark_price_klines(
        self, symbol, interval, start, end, *, max_candles=10_000
    ):
        return await self.historical_klines(
            symbol, interval, start, end, max_candles=max_candles
        )

    async def historical_funding_rates(self, symbol, start, end, *, max_events=10_000):
        return []


class LLMReplayMarket(ApiMarket):
    async def historical_klines(self, symbol, interval, start, end, *, max_candles=10_000):
        step = {
            "1m": 60_000,
            "5m": 300_000,
            "15m": 900_000,
            "30m": 1_800_000,
            "1h": 3_600_000,
            "4h": 14_400_000,
        }[interval]
        start_ms = int(start.timestamp() * 1000)
        return [
            [
                start_ms + index * step,
                str(100 + index),
                str(102 + index),
                str(99 + index),
                str(101 + index),
                "10",
                start_ms + (index + 1) * step - 1,
                str((101 + index) * 10),
            ]
            for index in range(21)
        ]


class ApiTestnetBroker:
    def __init__(self) -> None:
        self.account_calls = 0
        self.position_risk_calls = 0
        self.level_calls = 0

    async def protective_levels(self):
        self.level_calls += 1
        return {
            "BTCUSDT": ProtectiveLevels(
                stop_loss=Decimal("58000"), take_profit=Decimal("63000")
            )
        }

    async def account(self):
        self.account_calls += 1
        # Mirrors the real /fapi/v3/account futures response: it has neither
        # canTrade nor a usable mark/entry price for the position table.
        return {
            "totalWalletBalance": "10000.5",
            "totalMarginBalance": "10025.5",
            "availableBalance": "9000",
            "totalUnrealizedProfit": "25",
            "totalInitialMargin": "1000",
            "positions": [
                {
                    "symbol": "BTCUSDT",
                    "positionAmt": "0.25",
                    "unrealizedProfit": "25",
                    "leverage": "3",
                    "isolated": True,
                    "positionInitialMargin": "1000",
                },
                {"symbol": "ETHUSDT", "positionAmt": "0"},
            ],
        }

    async def position_risk(self):
        self.position_risk_calls += 1
        return [
            {
                "symbol": "BTCUSDT",
                "positionAmt": "0.25",
                "entryPrice": "60000",
                "markPrice": "60100",
                "unRealizedProfit": "25",
                "leverage": "3",
            }
        ]
