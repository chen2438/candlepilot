import asyncio
import json
from datetime import UTC, datetime

from candlepilot.market.stream import BinanceMarketStream, MarketEventSequencer


class FakeSocket:
    def __init__(self, messages):
        self.messages = iter(messages)
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            item = next(self.messages)
        except StopIteration as exc:
            raise StopAsyncIteration from exc
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, socket=None, error=None):
        self.socket = socket
        self.error = error

    async def __aenter__(self):
        if self.error is not None:
            raise self.error
        return self.socket

    async def __aexit__(self, *_):
        return None


def _message() -> str:
    return json.dumps(
        {
            "stream": "btcusdt@markPrice@1s",
            "data": {
                "e": "markPriceUpdate",
                "E": 1767225600000,
                "s": "BTCUSDT",
                "p": "50000.1",
            },
        }
    )


def test_builds_combined_public_stream_url() -> None:
    stream = BinanceMarketStream(["btcusdt"], intervals=("1m", "5m"))

    assert stream.streams == (
        "btcusdt@kline_1m",
        "btcusdt@kline_5m",
        "btcusdt@markPrice@1s",
        "btcusdt@bookTicker",
        "btcusdt@ticker",
    )
    assert stream.url.startswith("wss://fstream.binance.com/stream?streams=")


def test_parses_combined_stream_envelope() -> None:
    event = BinanceMarketStream.parse_message(_message())

    assert event.symbol == "BTCUSDT"
    assert event.event_type == "markPriceUpdate"
    assert event.event_time == datetime(2026, 1, 1, tzinfo=UTC)
    assert event.payload["p"] == "50000.1"


def test_reconnects_after_transport_failure() -> None:
    connections = [
        FakeConnection(error=OSError("disconnected")),
        FakeConnection(socket=FakeSocket([_message()])),
    ]

    def factory(_):
        return connections.pop(0)

    async def scenario():
        stream = BinanceMarketStream(
            ["BTCUSDT"],
            connection_factory=factory,
            reconnect_initial=0,
            reconnect_maximum=0,
        )
        async for event in stream.events():
            await stream.stop()
            return stream, event
        raise AssertionError("expected a market event")

    stream, event = asyncio.run(scenario())
    assert event.symbol == "BTCUSDT"
    assert stream.reconnect_count == 1
    assert stream.last_error is None


def test_drops_duplicate_and_out_of_order_book_updates() -> None:
    sequencer = MarketEventSequencer()
    first = BinanceMarketStream.parse_message(
        json.dumps(
            {
                "stream": "btcusdt@bookTicker",
                "data": {"e": "bookTicker", "E": 10, "s": "BTCUSDT", "u": 100},
            }
        )
    )
    duplicate = BinanceMarketStream.parse_message(
        json.dumps(
            {
                "stream": "btcusdt@bookTicker",
                "data": {"e": "bookTicker", "E": 11, "s": "BTCUSDT", "u": 100},
            }
        )
    )
    stale = BinanceMarketStream.parse_message(
        json.dumps(
            {
                "stream": "btcusdt@bookTicker",
                "data": {"e": "bookTicker", "E": 12, "s": "BTCUSDT", "u": 99},
            }
        )
    )

    assert sequencer.accept(first)
    assert not sequencer.accept(duplicate)
    assert not sequencer.accept(stale)
    assert sequencer.dropped == 2


def test_kline_revisions_require_increasing_event_time() -> None:
    sequencer = MarketEventSequencer()

    def kline(event_time):
        return BinanceMarketStream.parse_message(
            json.dumps(
                {
                    "stream": "btcusdt@kline_1m",
                    "data": {
                        "e": "kline",
                        "E": event_time,
                        "s": "BTCUSDT",
                        "k": {"t": 1000},
                    },
                }
            )
        )

    assert sequencer.accept(kline(10))
    assert sequencer.accept(kline(11))
    assert not sequencer.accept(kline(9))
