"""Assembly of the real runner for tests.

Every test in this suite goes through here, so no test can accidentally verify a
different wiring than the mission test does. Only the external boundaries
(market data, reviewer, broker, clock) are substitutable; the runner, the
algorithm and the SQLite store are always the production implementations.
"""

from __future__ import annotations

from ibkr_trader.clock import FixedClock
from ibkr_trader.config import build_config
from ibkr_trader.models import Outcome
from ibkr_trader.runner import Runner
from ibkr_trader.store import SqliteStore

from .fakes import (
    ACCOUNT,
    SCAN_TIME,
    FakeBroker,
    StubMarketData,
    StubReviewer,
    tradable_snapshot,
)


def build_runner(
    tmp_path,
    *,
    universe=("AAPL",),
    market=None,
    reviewer=None,
    broker=None,
    clock=None,
    overrides=None,
):
    """Build the production runner with test doubles at its edges.

    Returns:
        ``(runner, market, reviewer, broker, store)`` so a test can assert on
        what each boundary was asked to do — including that it was never asked.
    """
    settings = {
        "universe": list(universe),
        "database_path": str(tmp_path / "trader.sqlite3"),
        "ibkr": {"account": ACCOUNT},
    }
    if overrides:
        settings.update(overrides)
    config = build_config(settings)

    clock = clock or FixedClock(SCAN_TIME)
    market = market or StubMarketData({"AAPL": tradable_snapshot("AAPL")})
    reviewer = reviewer if reviewer is not None else StubReviewer(approved=True)
    broker = broker if broker is not None else FakeBroker(outcome=Outcome.FILLED)
    store = SqliteStore(config.database_path, clock=clock)

    runner = Runner(
        config=config,
        market_data=market,
        reviewer=reviewer,
        broker=broker,
        store=store,
        clock=clock,
    )
    return runner, market, reviewer, broker, store
