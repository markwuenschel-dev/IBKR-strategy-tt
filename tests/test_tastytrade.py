"""Trade-qualification tests for the three defects Wave 3 / U10 repairs.

The algorithm had no dedicated test module, so the arithmetic that decides how
much money to put at risk was covered only incidentally through the mission
test. Each test below fails against the pre-fix implementation and states the
concrete number that makes it fail, so the assertion is checkable by hand.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from ibkr_trader.config import build_config
from ibkr_trader.models import MarketSnapshot, NoTrade, Portfolio, Right, TradeProposal
from ibkr_trader.tastytrade import evaluate

from .fakes import ACCOUNT, GOOD_EXPIRY, SCAN_TIME, quote

NOW = datetime(2026, 1, 15, 14, 31, tzinfo=UTC)


@pytest.fixture
def config():
    return build_config({"universe": ["AAPL"], "ibkr": {"account": ACCOUNT}})


def snapshot_from(*quotes) -> MarketSnapshot:
    """Wrap explicit quotes in a snapshot whose IV rank always clears the gate."""
    return MarketSnapshot(
        symbol="AAPL",
        underlying_price=Decimal("195.00"),
        iv_rank=55.0,
        as_of=SCAN_TIME,
        chain=tuple(quotes),
    )


def put(strike: str, bid: str, ask: str, delta: float, open_interest: int = 500):
    return quote("AAPL", GOOD_EXPIRY, strike, Right.PUT, bid, ask, delta, open_interest)


# --- INT-017 -------------------------------------------------------------


def test_sizing_refusal_names_buying_power_not_the_risk_budget(config):
    """A buying-power refusal must not be reported as a risk-budget breach.

    Defined risk is (5.00 - 1.75) x 100 = 325 per contract. The risk budget is
    2% of 100,000 = 2,000, which affords six contracts. Buying power is 100,
    which affords none. Zero contracts is therefore a buying-power outcome, and
    a message blaming the per-trade budget asserts something arithmetically
    false: 325 is well inside 2,000.
    """
    portfolio = Portfolio(net_liquidation=Decimal(100_000), buying_power=Decimal(100))
    snapshot = snapshot_from(
        put("185", "3.35", "3.45", -0.30),
        put("180", "1.60", "1.70", -0.20),
    )

    decision = evaluate("AAPL", snapshot, portfolio, config.strategy, config.risk, NOW)

    assert isinstance(decision, NoTrade)
    assert "buying power" in decision.reason.lower()
    assert "per-trade budget" not in decision.reason.lower()


def test_sizing_refusal_still_names_the_risk_budget_when_that_is_what_bound(config):
    """The risk budget must still be named when it is genuinely the binding cap."""
    portfolio = Portfolio(net_liquidation=Decimal(1_000), buying_power=Decimal(1_000_000))
    snapshot = snapshot_from(
        put("185", "3.35", "3.45", -0.30),
        put("180", "1.60", "1.70", -0.20),
    )

    decision = evaluate("AAPL", snapshot, portfolio, config.strategy, config.risk, NOW)

    assert isinstance(decision, NoTrade)
    assert "budget" in decision.reason.lower()


# --- INT-031 -------------------------------------------------------------


def test_credit_ratio_is_tested_before_the_price_is_rounded_down(config):
    """Rounding must not make the screen stricter than the configured ratio.

    The configured minimum is 1/3 of a 5-wide spread, i.e. 1.666666... A raw
    credit of 1.6667 clears it (0.33334 of width). Rounding down to the tick
    first yields 1.66, which is 0.332 of width and fails - so the screen would
    reject a spread the configuration accepts. The order's limit price should
    still be the rounded 1.66, because that is what can actually be collected.
    """
    portfolio = Portfolio(net_liquidation=Decimal(100_000), buying_power=Decimal(100_000))
    snapshot = snapshot_from(
        put("185", "3.4167", "3.4167", -0.30),
        put("180", "1.7500", "1.7500", -0.20),
    )

    decision = evaluate("AAPL", snapshot, portfolio, config.strategy, config.risk, NOW)

    assert isinstance(decision, TradeProposal), getattr(decision, "reason", decision)
    assert decision.limit_price == Decimal("1.66")


def test_a_credit_genuinely_below_the_ratio_is_still_refused(config):
    """The screen must still reject a spread that fails the ratio unrounded."""
    portfolio = Portfolio(net_liquidation=Decimal(100_000), buying_power=Decimal(100_000))
    snapshot = snapshot_from(
        put("185", "3.0000", "3.0000", -0.30),
        put("180", "1.7500", "1.7500", -0.20),
    )

    decision = evaluate("AAPL", snapshot, portfolio, config.strategy, config.risk, NOW)

    assert isinstance(decision, NoTrade)
    assert "minimum" in decision.reason


# --- INT-019 -------------------------------------------------------------


def test_selection_backtracks_when_the_best_delta_candidate_is_illiquid(config):
    """A failed candidate must not end the search while others remain.

    Deltas put 190, 185 and 195 in the 0.20-0.40 band. 190 is closest to the
    0.30 target and is selected first, but its open interest of 5 fails the
    100 minimum. 185/180 is a perfectly tradable 5-wide spread at a 1.70 credit,
    and committing to the first candidate throws it away.
    """
    portfolio = Portfolio(net_liquidation=Decimal(100_000), buying_power=Decimal(100_000))
    snapshot = snapshot_from(
        put("200", "7.30", "7.50", -0.45),
        put("195", "5.40", "5.55", -0.38),
        put("190", "4.30", "4.40", -0.30, open_interest=5),
        put("185", "3.30", "3.40", -0.22),
        put("180", "1.60", "1.70", -0.16),
        put("175", "0.90", "1.00", -0.11),
    )

    decision = evaluate("AAPL", snapshot, portfolio, config.strategy, config.risk, NOW)

    assert isinstance(decision, TradeProposal), getattr(decision, "reason", decision)
    assert [leg.strike for leg in decision.legs] == [Decimal(185), Decimal(180)]


def test_exhausting_every_candidate_reports_the_first_failure(config):
    """When no candidate clears, the reason describes the preferred one."""
    portfolio = Portfolio(net_liquidation=Decimal(100_000), buying_power=Decimal(100_000))
    snapshot = snapshot_from(
        put("190", "4.30", "4.40", -0.30, open_interest=5),
        put("185", "3.30", "3.40", -0.22, open_interest=5),
        put("180", "1.60", "1.70", -0.16),
    )

    decision = evaluate("AAPL", snapshot, portfolio, config.strategy, config.risk, NOW)

    assert isinstance(decision, NoTrade)
    assert "190" in decision.reason
    assert "open interest" in decision.reason
