import os
from decimal import Decimal

import pytest


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    """Detach every test from the developer's own environment and ``.env``.

    ``cli.serve`` calls ``load_dotenv()`` with no argument, which reads the repo
    root ``.env`` into ``os.environ`` — so without this the suite silently
    depends on whatever the machine happens to have configured.
    """

    for key in list(os.environ):
        if key.startswith(("CANDLEPILOT_", "BINANCE_")):
            monkeypatch.delenv(key, raising=False)
    # load_dotenv() would otherwise fall back to the repo root .env.
    monkeypatch.setenv("CANDLEPILOT_ENV_FILE", str(tmp_path / "absent.env"))


@pytest.fixture(autouse=True)
def offline_pricing(monkeypatch):
    """Keep the test suite offline: the models.dev pricing loader never fetches.

    Tests that need a real catalog pass an explicit ``pricing_loader`` to
    ``create_app`` instead.
    """

    async def _no_catalog(_cache_dir):
        return None

    monkeypatch.setattr("candlepilot.api.load_pricing_catalog", _no_catalog)


class FakeTestnetBroker:
    """A stand-in exchange for tests that need the engine to be constructible.

    Binance testnet is the only account the engine trades, so the broker is no
    longer optional and every engine under test needs one. This fills at the
    snapshot's mark price and brackets the entry, which is what the real broker
    reports back; tests that care about a specific exchange behaviour subclass
    it rather than reaching for a mode switch that no longer exists.
    """

    def __init__(self) -> None:
        self.orders: list[object] = []
        self.flattened = False

    async def reconcile_account(self):
        from candlepilot.broker.binance_testnet import ReconciliationReport

        return ReconciliationReport((), 0, ())

    async def account(self):
        return {
            "totalMarginBalance": "10000",
            "availableBalance": "8000",
            "totalInitialMargin": "0",
            "positions": [],
        }

    async def protective_levels(self):
        return {}

    async def pending_entry_symbols(self):
        return ()

    async def trailing_positions(self):
        return {}

    async def configure_symbol(self, symbol, leverage):
        return None

    async def execute_with_stop(self, order, *, leverage, replace_existing_protection=False):
        from candlepilot.domain.models import ExecutionReport

        self.orders.append(order)
        return ExecutionReport(
            client_order_id=order.client_order_id,
            status="FILLED",
            filled_quantity=order.quantity,
            average_price=order.price or Decimal("100"),
            message="fake fill",
        )

    async def emergency_flatten(self):
        self.flattened = True


class StatefulTestnetBroker(FakeTestnetBroker):
    """A fake exchange that remembers what it filled.

    The risk policy reads open positions back out of the account, so any test
    about position state -- a held symbol, an opposing entry -- needs fills to
    show up in the next account() rather than vanishing.
    """

    def __init__(self, positions: dict[str, tuple[str, Decimal, Decimal]] | None = None) -> None:
        super().__init__()
        # symbol -> (side, quantity, entry price)
        self.positions: dict[str, tuple[str, Decimal, Decimal]] = dict(positions or {})

    async def account(self):
        return {
            "totalMarginBalance": "10000",
            "availableBalance": "8000",
            "totalInitialMargin": "0",
            "positions": [
                {
                    "symbol": symbol,
                    "positionAmt": str(quantity if side == "LONG" else -quantity),
                    "entryPrice": str(entry),
                    "unrealizedProfit": "0",
                    "leverage": "3",
                }
                for symbol, (side, quantity, entry) in self.positions.items()
            ],
        }

    async def execute_with_stop(self, order, *, leverage, replace_existing_protection=False):
        report = await super().execute_with_stop(
            order, leverage=leverage, replace_existing_protection=replace_existing_protection
        )
        self.positions[order.symbol] = (
            "LONG" if order.side == "BUY" else "SHORT",
            order.quantity,
            report.average_price or Decimal("100"),
        )
        return report
