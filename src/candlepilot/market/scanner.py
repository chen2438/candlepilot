from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class MarketCandidateInput:
    symbol: str
    quote_volume_24h: Decimal
    bid: Decimal
    ask: Decimal
    volatility: Decimal
    trend_strength: Decimal
    listing_age_days: int
    data_completeness: Decimal = Decimal("1")

    @property
    def spread_bps(self) -> Decimal:
        midpoint = (self.bid + self.ask) / 2
        if midpoint <= 0:
            return Decimal("Infinity")
        return ((self.ask - self.bid) / midpoint) * Decimal("10000")


@dataclass(frozen=True, slots=True)
class Candidate:
    symbol: str
    score: Decimal
    volume_rank: int
    spread_bps: Decimal
    volatility: Decimal
    trend_strength: Decimal


class MarketScanner:
    def __init__(
        self,
        *,
        minimum_listing_days: int = 30,
        maximum_spread_bps: Decimal = Decimal("20"),
        minimum_data_completeness: Decimal = Decimal("0.99"),
        volume_pool_size: int = 50,
        candidate_count: int = 20,
    ) -> None:
        self.minimum_listing_days = minimum_listing_days
        self.maximum_spread_bps = maximum_spread_bps
        self.minimum_data_completeness = minimum_data_completeness
        self.volume_pool_size = volume_pool_size
        self.candidate_count = candidate_count

    def scan(self, instruments: list[MarketCandidateInput]) -> list[Candidate]:
        eligible = [
            item
            for item in instruments
            if item.symbol.endswith("USDT")
            and item.listing_age_days >= self.minimum_listing_days
            and item.data_completeness >= self.minimum_data_completeness
            and item.bid > 0
            and item.ask >= item.bid
            and item.spread_bps <= self.maximum_spread_bps
            and item.quote_volume_24h > 0
        ]
        volume_pool = sorted(
            eligible, key=lambda item: (-item.quote_volume_24h, item.symbol)
        )[: self.volume_pool_size]
        if not volume_pool:
            return []

        volume_rank = {
            item.symbol: index + 1 for index, item in enumerate(volume_pool)
        }
        volume_max = max(item.quote_volume_24h for item in volume_pool)
        volatility_max = max(item.volatility for item in volume_pool) or Decimal("1")
        trend_max = max(abs(item.trend_strength) for item in volume_pool) or Decimal("1")

        candidates = []
        for item in volume_pool:
            volume_score = item.quote_volume_24h / volume_max
            liquidity_score = Decimal("1") - (item.spread_bps / self.maximum_spread_bps)
            volatility_score = max(Decimal("0"), item.volatility / volatility_max)
            trend_score = abs(item.trend_strength) / trend_max
            score = (
                volume_score * Decimal("0.35")
                + liquidity_score * Decimal("0.30")
                + volatility_score * Decimal("0.20")
                + trend_score * Decimal("0.15")
            )
            candidates.append(
                Candidate(
                    symbol=item.symbol,
                    score=score.quantize(Decimal("0.000001")),
                    volume_rank=volume_rank[item.symbol],
                    spread_bps=item.spread_bps,
                    volatility=item.volatility,
                    trend_strength=item.trend_strength,
                )
            )
        return sorted(candidates, key=lambda item: (-item.score, item.symbol))[
            : self.candidate_count
        ]

