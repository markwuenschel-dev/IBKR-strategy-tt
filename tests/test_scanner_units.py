"""Line-local defects in the market-data adapter.

These exercise the adapter's own helpers directly with injected doubles, so
they run with `ib_async` absent. Each fails against the pre-fix scanner.
"""

from __future__ import annotations

import contextlib
from decimal import Decimal
from types import SimpleNamespace

import pytest

from ibkr_trader.clock import FixedClock
from ibkr_trader.config import build_config
from ibkr_trader.errors import MarketDataError
from ibkr_trader.scanner import IBKRMarketData, _whole_contracts

from .fakes import SCAN_TIME


def adapter(ib=None):
    config = build_config({"universe": ["AAPL"]})
    return IBKRMarketData(
        ibkr_config=config.ibkr,
        strategy_config=config.strategy,
        clock=FixedClock(SCAN_TIME),
        ib=ib,
    )


# --- INT-004 -------------------------------------------------------------


class LineCountingIB:
    """Counts market-data lines, and can fail on a chosen request or cancel."""

    def __init__(self, fail_request_on: int | None = None, fail_cancel_on: int | None = None):
        self.open_lines: set[int] = set()
        self.requests = 0
        self.cancels = 0
        self._fail_request_on = fail_request_on
        self._fail_cancel_on = fail_cancel_on

    def reqMktData(self, contract, generic_ticks, snapshot, regulatory):
        self.requests += 1
        if self.requests == self._fail_request_on:
            raise RuntimeError("market data request refused")
        self.open_lines.add(id(contract))
        return SimpleNamespace(contract=contract, bid=1.0, ask=1.1, last=1.05, close=1.0)

    def cancelMktData(self, contract):
        self.cancels += 1
        if self.cancels == self._fail_cancel_on:
            raise RuntimeError("cancel failed")
        self.open_lines.discard(id(contract))

    def waitOnUpdate(self, timeout: float = 0) -> bool:
        return True

    def sleep(self, seconds: float) -> None:
        return None


def test_a_failed_request_does_not_leak_the_lines_already_opened():
    """reqMktData ran outside the try, so a mid-batch failure stranded lines.

    Every line already opened in the batch stayed open, and the next symbol in
    the pass inherited a smaller budget than the configuration promised.
    """
    ib = LineCountingIB(fail_request_on=3)
    contracts = [SimpleNamespace(strike=i) for i in range(5)]

    with pytest.raises(MarketDataError):
        adapter(ib)._quote_batches(ib, contracts, "")

    assert ib.open_lines == set(), f"{len(ib.open_lines)} market-data lines leaked"


def test_a_failed_cancel_does_not_skip_the_remaining_cancels():
    """One raising cancel aborted the finally loop, leaking every later line."""
    ib = LineCountingIB(fail_cancel_on=2)
    contracts = [SimpleNamespace(strike=i) for i in range(5)]

    with contextlib.suppress(MarketDataError):
        adapter(ib)._quote_batches(ib, contracts, "")

    # Every line is attempted. The one whose cancel raised may or may not have
    # been released by the venue -- that is not ours to know -- but the four
    # after it must not be skipped because of it.
    assert ib.cancels == 5, f"only {ib.cancels} of 5 cancels were attempted"
    assert len(ib.open_lines) <= 1, f"{len(ib.open_lines)} market-data lines leaked"


# --- INT-013 -------------------------------------------------------------


def test_the_live_book_is_preferred_over_the_previous_session_close():
    """The docstring says the close is the last resort; the code ranked it second.

    With no last trade but a live two-sided book, the midpoint is the current
    price and the close is yesterday's. Selecting strikes against yesterday's
    price is what the docstring says the ordering exists to avoid.
    """
    ticker = SimpleNamespace(last=None, close=100.0, bid=119.0, ask=121.0)

    price = adapter()._underlying_price("AAPL", ticker)

    assert price == Decimal("120.00")


def test_the_close_is_still_used_when_the_book_is_empty():
    """The close remains the fallback, not a value that was removed."""
    ticker = SimpleNamespace(last=None, close=100.0, bid=None, ask=None)

    assert adapter()._underlying_price("AAPL", ticker) == Decimal("100.00")


# --- INT-022 -------------------------------------------------------------


def test_no_strike_above_spot_is_quoted():
    """Puts above spot are in the money and can never reach the 0.20-0.40 band.

    Quoting them spends the line budget the surrounding comment argues must be
    conserved, to produce rows the algorithm always discards.
    """
    chain = SimpleNamespace(strikes=[170, 180, 190, 195, 200, 210, 220])

    strikes = adapter()._strikes_near(chain, Decimal("195.00"))

    assert strikes, "the window collapsed to nothing"
    assert max(strikes) <= 195.0, f"quoted in-the-money puts: {strikes}"
    assert 180 in strikes, "the useful strikes below spot were dropped"


# --- INT-032 -------------------------------------------------------------


@pytest.mark.parametrize(
    ("reported", "expected"),
    [
        (0.5, 1),
        (-0.5, -1),
        (0.0, 0),
        (3.0, 3),
        (-3.0, -3),
        (2.4, 3),
    ],
)
def test_a_fractional_position_is_not_truncated_out_of_existence(reported, expected):
    """int() truncation dropped a fractional holding from the concentration check.

    A 0.5-share position is still exposure. Truncating it to 0 removed it from
    the portfolio entirely, so the duplicate-symbol guard never saw it. Rounding
    away from zero keeps "is there exposure here", which is the only question
    this number is read to answer, while leaving whole sizes untouched.
    """
    assert _whole_contracts(reported) == expected
