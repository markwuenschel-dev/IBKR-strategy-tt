"""What survives concurrency and interruption.

Two confirmed defects about losing writes: a second writer whose outcomes are
silently dropped, and an interrupt that takes a possibly-live order with it.
Both tests fail against the pre-fix code.
"""

from __future__ import annotations

import sqlite3

import pytest

from ibkr_trader.models import Outcome
from ibkr_trader.store import SqliteStore

from .fakes import FakeBroker, StubMarketData, tradable_snapshot
from .harness import build_runner

# --- INT-035 -------------------------------------------------------------


def test_the_store_opens_in_wal_with_a_busy_timeout(tmp_path):
    """Default journal mode plus a zero busy timeout drops a second writer.

    On the rollback journal a second connection that finds the database locked
    raises `database is locked` immediately, because sqlite3's default
    busy_timeout is 0. The runner logs that failure and continues, so the
    outcome is lost with only a log line to show for it. WAL lets a reader and
    a writer coexist, and a non-zero busy timeout makes a competing writer wait
    for the lock instead of failing instantly.
    """
    store = SqliteStore(tmp_path / "trader.sqlite3")
    try:
        journal = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
        timeout = store._conn.execute("PRAGMA busy_timeout").fetchone()[0]
    finally:
        store.close()

    assert journal.lower() == "wal"
    assert timeout > 0


def test_a_second_connection_can_read_while_the_first_holds_a_write(tmp_path):
    """The property WAL actually buys: a reader is not blocked by a writer."""
    path = tmp_path / "trader.sqlite3"
    store = SqliteStore(path)
    try:
        store._conn.execute("BEGIN IMMEDIATE")
        store._conn.execute(
            "INSERT INTO symbol_attempts (run_id, symbol, outcome, detail, recorded_at)"
            " VALUES ('r', 'AAPL', 'ERROR', 'held open', '2026-01-15T00:00:00Z')"
        )

        reader = sqlite3.connect(path, timeout=1.0)
        try:
            rows = reader.execute("SELECT count(*) FROM symbol_attempts").fetchone()[0]
        finally:
            reader.close()

        assert rows == 0, "the reader should see the pre-transaction state, not block"
        store._conn.rollback()
    finally:
        store.close()


# --- INT-010 -------------------------------------------------------------


class InterruptingBroker(FakeBroker):
    """Raises KeyboardInterrupt the way a Ctrl-C landing mid-submission would."""

    def submit(self, proposal):
        raise KeyboardInterrupt


def test_an_interrupt_during_submission_still_records_the_attempt(tmp_path):
    """KeyboardInterrupt is not an Exception, so the isolation boundary missed it.

    `except Exception` at the boundary cannot catch a BaseException, and neither
    run_once nor the CLI catches one either. The interrupt therefore escaped
    before `_record` ran for that symbol, and the pass left no trace of an
    attempt that may have already put an order on the wire.
    """
    runner, _, _, _, store = build_runner(tmp_path, broker=InterruptingBroker())

    with pytest.raises(KeyboardInterrupt):
        runner.run_once()

    rows = store._conn.execute("SELECT symbol, outcome FROM symbol_attempts").fetchall()
    store.close()

    assert rows, "the interrupted attempt was never recorded"
    assert rows[0]["symbol"] == "AAPL"
    assert rows[0]["outcome"] == Outcome.EXECUTION_AMBIGUOUS.value


class InterruptingMarketData(StubMarketData):
    """Raises KeyboardInterrupt on one symbol's quote, as a Ctrl-C mid-scan would."""

    def __init__(self, snapshots, interrupt_on: str) -> None:
        super().__init__(snapshots)
        self._interrupt_on = interrupt_on

    def snapshot(self, symbol: str):
        if symbol == self._interrupt_on:
            raise KeyboardInterrupt
        return super().snapshot(symbol)


def test_an_interrupt_during_the_scan_records_nothing_for_that_symbol(tmp_path):
    """Nothing has been sent to the venue while the universe is being scanned.

    An ``EXECUTION_AMBIGUOUS`` row there would tell the operator to reconcile an
    order that cannot exist. The symbols concluded before the interrupt keep
    their rows; the one it landed on has none, which is the truth about it.
    """
    market = InterruptingMarketData(
        {"AAPL": tradable_snapshot("AAPL", iv_rank=5.0), "MSFT": tradable_snapshot("MSFT")},
        interrupt_on="MSFT",
    )
    runner, _, reviewer, broker, store = build_runner(
        tmp_path, universe=("AAPL", "MSFT"), market=market
    )

    with pytest.raises(KeyboardInterrupt):
        runner.run_once()

    rows = store._conn.execute("SELECT symbol, outcome FROM symbol_attempts").fetchall()
    store.close()

    assert [(r["symbol"], r["outcome"]) for r in rows] == [("AAPL", Outcome.NO_TRADE.value)]
    assert reviewer.call_count == 0
    assert broker.call_count == 0
