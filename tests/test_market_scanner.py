from decimal import Decimal

from candlepilot.market.features import DAILY_STRUCTURE_PERIOD
from candlepilot.market.scanner import MarketCandidateInput, MarketScanner


def _instrument(
    symbol: str,
    *,
    volume: str = "1000000",
    bid: str = "100",
    ask: str = "100.1",
    volatility: str = "0.02",
    trend: str = "0.01",
    age: int = 100,
    completeness: str = "1",
) -> MarketCandidateInput:
    return MarketCandidateInput(
        symbol=symbol,
        quote_volume_24h=Decimal(volume),
        bid=Decimal(bid),
        ask=Decimal(ask),
        volatility=Decimal(volatility),
        trend_strength=Decimal(trend),
        listing_age_days=age,
        data_completeness=Decimal(completeness),
    )


def test_scanner_filters_new_wide_and_incomplete_contracts() -> None:
    scanner = MarketScanner()
    results = scanner.scan(
        [
            _instrument("BTCUSDT"),
            _instrument("NEWUSDT", age=5),
            _instrument("WIDEUSDT", bid="100", ask="101"),
            _instrument("GAPUSDT", completeness="0.90"),
            _instrument("BTCUSD"),
        ]
    )
    assert [item.symbol for item in results] == ["BTCUSDT"]


def test_scanner_first_limits_by_trailing_volume() -> None:
    scanner = MarketScanner(volume_pool_size=2, candidate_count=2)
    results = scanner.scan(
        [
            _instrument("AAAUSDT", volume="300"),
            _instrument("BBBUSDT", volume="200"),
            _instrument("CCCUSDT", volume="100", volatility="1", trend="1"),
        ]
    )
    assert {item.symbol for item in results} == {"AAAUSDT", "BBBUSDT"}


def test_scanner_order_is_deterministic() -> None:
    scanner = MarketScanner(candidate_count=2)
    instruments = [_instrument("ETHUSDT"), _instrument("BTCUSDT")]
    assert [item.symbol for item in scanner.scan(instruments)] == ["BTCUSDT", "ETHUSDT"]


def test_listing_floor_guarantees_the_daily_levels_are_real() -> None:
    """The scanner's listing floor is what makes 1d features safe to send.

    Every candidate is fed to market_snapshot, which computes the 20-day range.
    A candidate younger than that window makes daily_structure raise and takes
    the whole symbol out of the cycle, so the floor must stay clear of it --
    with room for the unclosed current bar.
    """

    assert MarketScanner().minimum_listing_days > DAILY_STRUCTURE_PERIOD
