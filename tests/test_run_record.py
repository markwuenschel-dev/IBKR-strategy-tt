"""A pass cannot trade until its account provenance is durable."""

from __future__ import annotations

import sqlite3

import pytest

from ibkr_trader.clock import FixedClock
from ibkr_trader.config import build_config
from ibkr_trader.runner import Runner
from ibkr_trader.store import SqliteStore

from .fakes import ACCOUNT, SCAN_TIME, FakeBroker, StubMarketData, StubReviewer


def test_each_pass_records_the_verified_book_before_scanning(tmp_path):
    events: list[str] = []

    class ObservedMarket(StubMarketData):
        def snapshot(self, symbol):
            events.append("scan")
            return super().snapshot(symbol)

    class ObservedStore(SqliteStore):
        def start_run(self, *args, **kwargs):
            events.append("run-record")
            return super().start_run(*args, **kwargs)

    config = build_config(
        {
            "universe": ["AAPL"],
            "ibkr": {"account": ACCOUNT, "host": "paper-host", "port": 4002},
            "database_path": str(tmp_path / "trader.sqlite3"),
        }
    )
    store = ObservedStore(config.database_path, clock=FixedClock(SCAN_TIME))
    runner = Runner(
        config=config,
        market_data=ObservedMarket(failures={"AAPL": RuntimeError("stop after seam")}),
        reviewer=StubReviewer(),
        broker=FakeBroker(verified_account=ACCOUNT),
        store=store,
        clock=FixedClock(SCAN_TIME),
    )

    summary = runner.run_once()
    rows = store.runs()
    store.close()

    assert events[:2] == ["run-record", "scan"]
    assert rows == [
        {
            "run_id": summary.run_id,
            "declared_mode": "paper",
            "verified_account": ACCOUNT,
            "host": "paper-host",
            "port": 4002,
            "started_at": SCAN_TIME.isoformat(),
        }
    ]


def test_a_failed_run_record_refuses_the_pass_before_any_external_action():
    class RefusingStore:
        def start_run(self, *args, **kwargs):  # noqa: ARG002 - deliberate refusal
            raise sqlite3.OperationalError("database is full")

        def record(self, *args, **kwargs):  # noqa: ARG002 - must never be called
            raise AssertionError("no symbol can be recorded before a run starts")

        def close(self):
            pass

    market = StubMarketData()
    reviewer = StubReviewer()
    broker = FakeBroker(verified_account=ACCOUNT)
    config = build_config({"universe": ["AAPL"], "ibkr": {"account": ACCOUNT}})
    runner = Runner(
        config=config,
        market_data=market,
        reviewer=reviewer,
        broker=broker,
        store=RefusingStore(),
        clock=FixedClock(SCAN_TIME),
    )

    with pytest.raises(sqlite3.OperationalError, match="database is full"):
        runner.run_once()

    assert market.requested == []
    assert reviewer.reviewed == []
    assert broker.submitted == []


def test_loop_records_one_identity_row_per_pass(tmp_path):
    from .harness import build_runner

    runner, _, _, _, store = build_runner(tmp_path)

    summaries = runner.run_while(lambda: True, max_passes=2)

    assert [row["run_id"] for row in store.runs()] == [s.run_id for s in summaries]
    assert {row["verified_account"] for row in store.runs()} == {ACCOUNT}
    store.close()
