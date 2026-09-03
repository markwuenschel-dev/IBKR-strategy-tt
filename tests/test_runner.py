"""Loop behaviour and operator output.

Two things are pinned here: repeating the scan needs no scheduler and no second
process, and the end-of-pass summary tells an operator why nothing traded
without them opening a database.
"""

from __future__ import annotations

import logging

from ibkr_trader.clock import FixedClock

from .fakes import (
    SCAN_TIME,
    StubMarketData,
    StubReviewer,
    tradable_snapshot,
)
from .harness import build_runner


def test_repeat_scanning_needs_no_scheduler(tmp_path):
    """``run_while`` is the entire scheduling story.

    The same process loops, sleeps on the injected clock, and stops when the
    market closes. No controller, no worker, no tick receipts.
    """
    clock = FixedClock(SCAN_TIME)
    market = StubMarketData({"AAPL": tradable_snapshot("AAPL")})
    # Duplicate positions are disallowed by default, so let the same fixture
    # trade on every pass rather than modelling fills back into the portfolio.
    runner, _, _, broker, store = build_runner(
        tmp_path, market=market, clock=clock, overrides={"scan_interval_seconds": 60.0}
    )

    summaries = runner.run_while(lambda: True, max_passes=3)

    assert len(summaries) == 3
    assert broker.call_count == 3
    assert len(store.attempts()) == 3
    # It slept between passes, on the injected clock, costing no wall-clock time.
    assert clock.slept == [60.0, 60.0, 60.0]


def test_loop_stops_when_the_market_closes(tmp_path):
    """A closed market ends the loop; nothing needs to be torn down."""
    clock = FixedClock(SCAN_TIME)
    calls = {"n": 0}

    def market_is_open() -> bool:
        calls["n"] += 1
        return calls["n"] <= 2

    runner, _, _, broker, _ = build_runner(tmp_path, clock=clock)
    summaries = runner.run_while(market_is_open)

    assert len(summaries) == 1
    assert broker.call_count == 1


def test_closed_market_runs_no_passes_at_all(tmp_path):
    runner, _, _, broker, store = build_runner(tmp_path)
    assert runner.run_while(lambda: False) == []
    assert broker.call_count == 0
    assert store.attempts() == []


# --- operator output ------------------------------------------------------


def test_summary_reports_every_category(tmp_path):
    """The §12 summary, over a universe that exercises several outcomes."""
    market = StubMarketData(
        snapshots={
            "AAPL": tradable_snapshot("AAPL"),
            "QQQ": tradable_snapshot("QQQ", iv_rank=5.0),
        },
        failures={
            "NVDA": __import__(
                "ibkr_trader.errors", fromlist=["MarketDataError"]
            ).MarketDataError("option chain unavailable")
        },
    )
    runner, _, _, _, _ = build_runner(
        tmp_path, universe=("AAPL", "QQQ", "NVDA"), market=market
    )

    summary = runner.run_once()
    rendered = summary.render()

    assert "Scanned: 3" in rendered
    assert "No trade: 1" in rendered
    assert "Proposals: 1" in rendered
    assert "Reviewer approved: 1" in rendered
    assert "Orders submitted: 1" in rendered
    assert "Filled: 1" in rendered
    assert "Errors: 1" in rendered


def test_each_symbol_logs_one_outcome_line(tmp_path, caplog):
    """One line per symbol, naming the outcome and the reason."""
    market = StubMarketData(
        {"AAPL": tradable_snapshot("AAPL"), "QQQ": tradable_snapshot("QQQ", iv_rank=5.0)}
    )
    runner, _, _, _, _ = build_runner(tmp_path, universe=("AAPL", "QQQ"), market=market)

    with caplog.at_level(logging.INFO, logger="ibkr_trader.runner"):
        runner.run_once()

    lines = [r.getMessage() for r in caplog.records]
    aapl = next(line for line in lines if line.startswith("AAPL"))
    qqq = next(line for line in lines if line.startswith("QQQ"))

    assert "FILLED" in aapl
    assert "185/180 put credit spread @ 1.75" in aapl
    assert "NO_TRADE" in qqq
    assert "IV rank" in qqq


def test_rejected_review_reports_the_reviewers_reason(tmp_path, caplog):
    """The operator sees why the reviewer said no, not merely that it did."""
    reviewer = StubReviewer(approved=False, reason="spread width exceeds preference")
    runner, _, _, _, _ = build_runner(tmp_path, reviewer=reviewer)

    with caplog.at_level(logging.INFO, logger="ibkr_trader.runner"):
        runner.run_once()

    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("AAPL"))
    assert "REVIEW_REJECTED" in line
    assert "spread width exceeds preference" in line
