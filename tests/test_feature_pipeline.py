from datetime import UTC, datetime, timedelta
from decimal import Decimal

from candlepilot.market.features import FeaturePipeline


def _rows(count: int = 60) -> list[list[object]]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows = []
    for index in range(count):
        price = Decimal("100") + Decimal(index) / 10
        open_ms = int((start + timedelta(minutes=index)).timestamp() * 1000)
        close_ms = open_ms + 59_999
        rows.append(
            [
                open_ms,
                str(price),
                str(price + 1),
                str(price - 1),
                str(price + Decimal("0.2")),
                "10",
                close_ms,
                str(1000 + index),
            ]
        )
    return rows


def test_feature_pipeline_produces_finite_multiscale_features() -> None:
    features = FeaturePipeline().calculate(_rows())
    assert set(features) == {
        "return_1",
        "return_5",
        "ema_20",
        "ema_50",
        "ema_spread",
        "rsi_14",
        "atr_14",
        "atr_fraction",
        "quote_volume_ratio",
    }
    assert features["ema_20"] > features["ema_50"]
    assert 50 < features["rsi_14"] <= 100
    assert features["atr_fraction"] > 0


def test_snapshot_contains_exchange_and_derived_data() -> None:
    snapshot = FeaturePipeline().snapshot(
        symbol="BTCUSDT",
        cadence="5m",
        rows=_rows(),
        mark_price=Decimal("106"),
        bid=Decimal("105.9"),
        ask=Decimal("106.1"),
        quote_volume_24h=Decimal("1000000"),
        funding_rate=Decimal("0.0001"),
    )
    assert snapshot.features["rsi_14"] > 50
    assert snapshot.funding_rate == Decimal("0.0001")


def test_microstructure_features_capture_direction_and_basis() -> None:
    features = FeaturePipeline.microstructure(
        mark_price=Decimal("101"),
        index_price=Decimal("100"),
        open_interest=Decimal("1234.5"),
        bids=[["100", "8"], ["99", "2"]],
        asks=[["101", "4"], ["102", "1"]],
        trades=[
            {"p": "100", "q": "2", "m": False},
            {"p": "100", "q": "1", "m": True},
        ],
    )

    assert features["basis_bps"] == 100.0
    assert features["open_interest"] == 1234.5
    assert features["book_imbalance"] == 1 / 3
    assert features["recent_trade_imbalance"] == 1 / 3


def test_multitimeframe_features_are_namespaced() -> None:
    features = FeaturePipeline().multitimeframe(
        {"1m": _rows(), "5m": _rows(), "15m": _rows(), "30m": _rows()}
    )

    assert len(features) == 36
    assert features["1m_ema_spread"] == features["5m_ema_spread"]
    assert "15m_rsi_14" in features
    assert "30m_rsi_14" in features
