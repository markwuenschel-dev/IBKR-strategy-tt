"""Line-local defects in the market-data adapter.

These exercise the adapter's own helpers directly with injected doubles, so
they run with `ib_async` absent. Each fails against the pre-fix scanner.
"""

from __future__ import annotations

import contextlib
import logging
import math
from decimal import Decimal
from types import SimpleNamespace

import pytest

from ibkr_trader.clock import FixedClock
from ibkr_trader.config import build_config
from ibkr_trader.errors import MarketDataError
from ibkr_trader.scanner import IBKRMarketData, _whole_contracts

from .fakes import ACCOUNT, SCAN_TIME


def adapter(ib=None, **strategy):
    config = build_config(
        {"universe": ["AAPL"], "ibkr": {"account": ACCOUNT}, "strategy": strategy}
    )
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

    strikes = adapter()._strikes_near(chain, Decimal("195.00"), window=0.15)

    assert strikes, "the window collapsed to nothing"
    assert max(strikes) <= 195.0, f"quoted in-the-money puts: {strikes}"
    assert 180 in strikes, "the useful strikes below spot were dropped"


def test_the_window_is_applied_as_a_fraction_of_spot_with_no_extra_margin():
    """``low = spot * (1 - window)`` exactly; the old fixed subtraction is gone."""
    chain = SimpleNamespace(strikes=[150, 160, 170, 180, 190, 195])

    strikes = adapter()._strikes_near(chain, Decimal("200.00"), window=0.20)

    # 200 * 0.80 = 160 sits on the boundary and is inside; 150 is not.
    assert strikes == [160, 170, 180, 190, 195]


# --- the volatility-scaled strike window ---------------------------------
#
# ``max(strike_window_pct, strike_window_iv_multiple * iv * sqrt(max_dte / 365))``
# with the defaults 0.15 / 1.2 / 60.


def test_the_floor_wins_when_the_underlying_is_calm():
    """At IV 0.15 the scaled term is 1.2 * 0.15 * sqrt(60/365) = 0.073 < 0.15."""
    assert adapter()._strike_window("AAPL", 0.15) == pytest.approx(0.15)


def test_the_scaled_window_wins_when_the_underlying_is_volatile():
    """IV 0.60 over 60 days: 1.2 * 0.60 * sqrt(60/365) = 0.2919 > 0.15.

    This is the case the fixed window got wrong: on a name this volatile the
    0.20-delta put sits well past 15% OTM, so the whole short band fell outside
    the window and the symbol reported "no listed strike".
    """
    window = adapter()._strike_window("TSLA", 0.60)

    assert window == pytest.approx(1.2 * 0.60 * math.sqrt(60 / 365))
    assert window == pytest.approx(0.292, abs=0.001)


def test_the_multiple_and_horizon_are_read_from_the_configuration():
    """Not constants: both the multiple and ``max_dte`` come from the config."""
    window = adapter(strike_window_iv_multiple=2.0, max_dte=90)._strike_window("X", 0.50)

    assert window == pytest.approx(2.0 * 0.50 * math.sqrt(90 / 365))


def test_a_multiple_of_zero_disables_the_scaling():
    """The documented off switch: IV is ignored and the floor stands alone."""
    assert adapter(strike_window_iv_multiple=0.0)._strike_window("TSLA", 0.60) == 0.15


@pytest.mark.parametrize("implied_volatility", [None, 0.0, -0.3])
def test_a_missing_or_unusable_iv_falls_back_to_the_floor(implied_volatility):
    """IBKR often has not sent tick 106 yet; that must not fail the symbol.

    NaN never reaches here -- the caller reads the ticker through ``_finite``
    -- so None, zero and a negative reading are the whole unusable set.
    """
    assert adapter()._strike_window("AAPL", implied_volatility) == pytest.approx(0.15)


def test_the_chosen_window_and_its_inputs_are_logged_for_audit(caplog):
    """A run's strike selection must be reconstructible from its log alone."""
    with caplog.at_level(logging.DEBUG, logger="ibkr_trader.scanner"):
        adapter()._strike_window("TSLA", 0.60)
        adapter()._strike_window("AAPL", None)

    messages = [record.getMessage() for record in caplog.records]
    scaled = next(m for m in messages if m.startswith("TSLA:"))
    assert "iv-scaled" in scaled
    assert "iv=0.6000" in scaled and "max_dte=60" in scaled and "floor=0.1500" in scaled
    fallback = next(m for m in messages if m.startswith("AAPL:"))
    assert "floor" in fallback and "unavailable" in fallback


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


# --- the bag's own quote -----------------------------------------------------


def test_a_credit_spread_s_negative_quote_is_read_not_discarded():
    """The trap this helper exists to avoid.

    ``_two_sided`` -- the reader for a single option -- requires a non-negative
    bid and a positive ask. A credit spread's bag is quoted in prices *paid*,
    so it is negative on both sides, and reusing that reader would have thrown
    every credit spread's quote away as malformed. Silently: the caller cannot
    tell a discarded quote from a venue that declined to quote.
    """
    ticker = SimpleNamespace(bid=-1.90, ask=-1.50)

    quote = IBKRMarketData._combo_quote(ticker)

    assert quote is not None
    assert quote.marketable_credit == Decimal("1.50"), "crossing collects the lower credit"
    assert quote.credit_mid == Decimal("1.70")
    assert quote.width == Decimal("0.40")


def test_a_crossed_or_absent_bag_book_is_no_quote_at_all():
    assert IBKRMarketData._combo_quote(SimpleNamespace(bid=-1.50, ask=-1.90)) is None
    assert IBKRMarketData._combo_quote(SimpleNamespace(bid=float("nan"), ask=-1.9)) is None
    assert IBKRMarketData._combo_quote(SimpleNamespace()) is None


def test_a_debit_spread_quote_still_reads_correctly():
    """Nothing here assumes the credit direction; a debit bag quotes positive."""
    quote = IBKRMarketData._combo_quote(SimpleNamespace(bid=1.50, ask=1.90))

    assert quote.marketable_credit == Decimal("-1.90"), "paying 1.90 is a negative credit"
    assert quote.credit_mid == Decimal("-1.70")
