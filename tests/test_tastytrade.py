"""Trade-qualification tests for the pure algorithm.

The first three groups pin the Wave 3 / U10 repairs; the rest pin the entry
rule as it stands: the long leg is chosen by delta, so the spread width is an
output of selection, and every risk figure and refusal quotes the width the
chain actually produced. Each test states the concrete number that makes it
fail, so the assertion is checkable by hand.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from ibkr_trader.config import RiskConfig, build_config
from ibkr_trader.models import (
    MarketSnapshot,
    NoTrade,
    Portfolio,
    Position,
    Right,
    TradeProposal,
)
from ibkr_trader.tastytrade import evaluate

from .fakes import ACCOUNT, GOOD_EXPIRY, NEAR_EXPIRY, SCAN_TIME, quote

NOW = datetime(2026, 1, 15, 14, 31, tzinfo=UTC)


@pytest.fixture
def config():
    return build_config({"universe": ["AAPL"], "ibkr": {"account": ACCOUNT}})


def snapshot_from(*quotes, as_of: datetime = SCAN_TIME) -> MarketSnapshot:
    """Wrap explicit quotes in a snapshot whose IV rank always clears the gate."""
    return MarketSnapshot(
        symbol="AAPL",
        underlying_price=Decimal("195.00"),
        iv_rank=55.0,
        as_of=as_of,
        chain=tuple(quotes),
    )


def put(
    strike: str,
    bid: str,
    ask: str,
    delta: float,
    open_interest: int = 500,
    expiry: date = GOOD_EXPIRY,
):
    return quote("AAPL", expiry, strike, Right.PUT, bid, ask, delta, open_interest)


#: The third Friday of February 2026: 36 DTE from ``SCAN_TIME``, inside the
#: 30-60 band, and 9 days further from the 45-day target than ``GOOD_EXPIRY``.
#: This is the real shape of the problem -- on any given day the monthly is
#: rarely the expiry closest to 45 days.
MONTHLY_EXPIRY = date(2026, 2, 20)
#: A Friday that is not a third Friday: 43 DTE, nearer the target than the
#: monthly and still not what Tastytrade mechanics would trade.
WEEKLY_EXPIRY = date(2026, 2, 27)


def ladder(expiry: date):
    """A tradable 185/180 pair on ``expiry``: 1.70 credit on a 5-wide spread."""
    return (
        put("185", "3.35", "3.45", -0.30, expiry=expiry),
        put("180", "1.65", "1.75", -0.20, expiry=expiry),
    )


def test_the_monthly_expiry_is_preferred_over_an_expiry_nearer_the_target(config):
    """Tastytrade trades monthlies; the weekly nearer 45 days is not the answer.

    ``GOOD_EXPIRY`` is 45 DTE -- an exact hit on ``target_dte`` -- while the
    February monthly is 36. Sorting on distance alone therefore picks the
    weekly every time the monthly is not sitting on the target, which is most
    days. Weeklies carry materially wider markets on single names, so this one
    ordering choice was feeding the liquidity problem it then screened on.
    """
    snapshot = snapshot_from(*ladder(MONTHLY_EXPIRY), *ladder(GOOD_EXPIRY))

    decision = evaluate("AAPL", snapshot, ample(), config.strategy, config.risk, NOW)

    assert isinstance(decision, TradeProposal), getattr(decision, "reason", decision)
    assert decision.expiry == MONTHLY_EXPIRY, "the monthly, not the closer weekly"


def test_a_weekly_is_used_when_no_monthly_falls_inside_the_band(config):
    """Preference, not requirement: an empty monthly pool must not refuse.

    Neither 2026-02-27 nor ``GOOD_EXPIRY`` is a third Friday, so the original
    nearest-to-target rule applies unchanged and picks the 45-DTE one.
    """
    snapshot = snapshot_from(*ladder(WEEKLY_EXPIRY), *ladder(GOOD_EXPIRY))

    decision = evaluate("AAPL", snapshot, ample(), config.strategy, config.risk, NOW)

    assert isinstance(decision, TradeProposal), getattr(decision, "reason", decision)
    assert decision.expiry == GOOD_EXPIRY


def test_a_wide_book_is_ranked_and_reviewed_rather_than_refused(config):
    """``max_spread_pct`` is gone; liquidity is a preference, not a gate.

    Short 185 is 3.00/3.80 -- a 23.5% spread, formerly refused outright by the
    10% limit. It has a real bid, 500 open interest, and pays 1.70 on a 5-wide
    spread, so nothing about it is untradable. Width still reaches the ranker
    (``ranking.py`` orders on it) and the reviewer (it is in the leg payload),
    which is where a preference belongs.
    """
    snapshot = snapshot_from(
        put("185", "3.00", "3.80", -0.30),
        put("180", "1.30", "2.10", -0.20),
    )

    decision = evaluate("AAPL", snapshot, ample(), config.strategy, config.risk, NOW)

    assert isinstance(decision, TradeProposal), getattr(decision, "reason", decision)
    assert [leg.strike for leg in decision.legs] == [Decimal(185), Decimal(180)]
    assert decision.limit_price == Decimal("1.70")


def test_a_leg_with_no_bid_is_still_refused(config):
    """Removing the width gate must not admit a book nobody is quoting.

    ``bid <= 0`` stays: it is the check that makes a mid meaningful at all.
    """
    snapshot = snapshot_from(
        put("185", "0.00", "3.80", -0.30),
        put("180", "1.30", "2.10", -0.20),
    )

    decision = evaluate("AAPL", snapshot, ample(), config.strategy, config.risk, NOW)

    assert isinstance(decision, NoTrade)
    assert "no bid" in decision.reason


def ample() -> Portfolio:
    """An account no ceiling binds, so a refusal is about the chain, not the size."""
    return Portfolio(net_liquidation=Decimal(100_000), buying_power=Decimal(100_000))


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

    The 185/180 pair makes a 5-wide spread, so the configured minimum is 1/3
    of 5, i.e. 1.666666... A raw credit of 1.6667 clears it (0.33334 of
    width). Rounding down to the tick first yields 1.66, which is 0.332 of
    width and fails - so the screen would reject a spread the configuration
    accepts. The order's limit price should still be the rounded 1.66, because
    that is what can actually be collected.
    """
    snapshot = snapshot_from(
        put("185", "3.4167", "3.4167", -0.30),
        put("180", "1.7500", "1.7500", -0.20),
    )

    decision = evaluate("AAPL", snapshot, ample(), config.strategy, config.risk, NOW)

    assert isinstance(decision, TradeProposal), getattr(decision, "reason", decision)
    assert decision.limit_price == Decimal("1.66")


def test_a_credit_genuinely_below_the_ratio_is_still_refused(config):
    """The screen must still reject a spread that fails the ratio unrounded."""
    snapshot = snapshot_from(
        put("185", "3.0000", "3.0000", -0.30),
        put("180", "1.7500", "1.7500", -0.20),
    )

    decision = evaluate("AAPL", snapshot, ample(), config.strategy, config.risk, NOW)

    assert isinstance(decision, NoTrade)
    assert "minimum" in decision.reason


# --- INT-019 -------------------------------------------------------------


def test_selection_backtracks_when_the_best_delta_candidate_is_illiquid(config):
    """A failed candidate must not end the search while others remain.

    Deltas put 190, 185 and 195 in the 0.20-0.40 band. 190 is closest to the
    0.30 target and is selected first, but its open interest of 5 fails the
    100 minimum. 185 partners with 180 (delta 0.16, nearest the 0.20 long
    target below it) for a perfectly tradable 5-wide spread at a 1.70 credit,
    and committing to the first candidate throws it away.
    """
    snapshot = snapshot_from(
        put("200", "7.30", "7.50", -0.45),
        put("195", "5.40", "5.55", -0.38),
        put("190", "4.30", "4.40", -0.30, open_interest=5),
        put("185", "3.30", "3.40", -0.22),
        put("180", "1.60", "1.70", -0.16),
        put("175", "0.90", "1.00", -0.11),
    )

    decision = evaluate("AAPL", snapshot, ample(), config.strategy, config.risk, NOW)

    assert isinstance(decision, TradeProposal), getattr(decision, "reason", decision)
    assert [leg.strike for leg in decision.legs] == [Decimal(185), Decimal(180)]


def test_exhausting_every_candidate_reports_the_first_failure(config):
    """When no candidate clears, the reason describes the preferred one."""
    snapshot = snapshot_from(
        put("190", "4.30", "4.40", -0.30, open_interest=5),
        put("185", "3.30", "3.40", -0.22, open_interest=5),
        put("180", "1.60", "1.70", -0.16),
    )

    decision = evaluate("AAPL", snapshot, ample(), config.strategy, config.risk, NOW)

    assert isinstance(decision, NoTrade)
    assert "190" in decision.reason
    assert "open interest" in decision.reason


# --- long leg by delta, not by fixed width ----------------------------------


def test_long_leg_is_the_put_nearest_the_long_delta_target(config):
    """The long put is chosen by delta; the width falls out of that choice.

    Short 185 is the 0.30 target. Below it, 180 has delta 0.25 and 175 has
    delta 0.20 -- exactly the long target -- so 175 wins even though it makes
    the spread 10 wide rather than 5. Credit 3.50 is 35.0% of 10.00, clearing
    the 33.3% minimum, so the width is visible in every derived figure.
    """
    snapshot = snapshot_from(
        put("185", "4.95", "5.05", -0.30),
        put("180", "3.00", "3.10", -0.25),
        put("175", "1.45", "1.55", -0.20),
        put("170", "0.70", "0.80", -0.12),
    )

    decision = evaluate("AAPL", snapshot, ample(), config.strategy, config.risk, NOW)

    assert isinstance(decision, TradeProposal), getattr(decision, "reason", decision)
    assert [leg.strike for leg in decision.legs] == [Decimal(185), Decimal(175)]
    assert decision.criteria["spread_width"] == "10.00"
    assert decision.criteria["long_delta"] == "0.20 within [0.10, 0.25], target 0.20"


def test_equal_delta_distance_breaks_toward_the_narrower_spread(config):
    """At equal distance from the target, the higher long strike wins.

    180 (delta 0.22) and 175 (delta 0.18) are both 0.02 from the 0.20 target.
    The narrower 185/180 spread risks 325 per contract against 675 for
    185/175, for the same short leg, so the tie goes to 180.
    """
    snapshot = snapshot_from(
        put("185", "3.35", "3.45", -0.30),
        put("180", "1.60", "1.70", -0.22),
        put("175", "1.15", "1.25", -0.18),
        put("170", "0.55", "0.65", -0.12),
    )

    decision = evaluate("AAPL", snapshot, ample(), config.strategy, config.risk, NOW)

    assert isinstance(decision, TradeProposal), getattr(decision, "reason", decision)
    assert [leg.strike for leg in decision.legs] == [Decimal(185), Decimal(180)]
    assert decision.criteria["spread_width"] == "5.00"
    assert decision.max_loss == Decimal(325) * decision.quantity


def test_no_put_in_the_long_delta_band_is_refused_naming_the_band(config):
    """A short strike with no partner in the long band is not tradable.

    180 has delta 0.28, above the 0.25 ceiling; 175 has delta 0.08, below the
    0.10 floor. 180 is itself a short candidate (0.02 from target) but has the
    same problem, so the refusal describes the preferred 185 strike.
    """
    snapshot = snapshot_from(
        put("185", "3.35", "3.45", -0.30),
        put("180", "1.60", "1.70", -0.28),
        put("175", "0.40", "0.50", -0.08),
    )

    decision = evaluate("AAPL", snapshot, ample(), config.strategy, config.risk, NOW)

    assert isinstance(decision, NoTrade)
    assert decision.reason == (
        "no put below the 185 short strike between 0.10 and 0.25 delta to define the spread"
    )


def test_a_put_above_the_short_strike_is_never_the_long_leg(config):
    """Strictly below: a put at or above the short strike defines no risk.

    190 has delta 0.20, the exact long target, but sits above the 185 short
    strike. Only 175 (delta 0.13) is admissible, so the spread is 185/175: a
    4.00 credit on 10.00 wide, 40.0%.
    """
    snapshot = snapshot_from(
        put("190", "6.95", "7.05", -0.20),
        put("185", "4.95", "5.05", -0.30),
        put("175", "0.95", "1.05", -0.13),
    )

    decision = evaluate("AAPL", snapshot, ample(), config.strategy, config.risk, NOW)

    assert isinstance(decision, TradeProposal), getattr(decision, "reason", decision)
    assert [leg.strike for leg in decision.legs] == [Decimal(185), Decimal(175)]


def test_an_illiquid_best_long_put_yields_to_the_next_one_in_the_band(config):
    """Backtracking applies to the long leg too, before the short is abandoned.

    180 is the exact 0.20 long target but has open interest 5. 175 (delta
    0.15) is the next-best partner and is liquid, so the spread is 185/175:
    a 3.50 credit on 10.00 wide, 35.0%.
    """
    snapshot = snapshot_from(
        put("185", "4.95", "5.05", -0.30),
        put("180", "3.00", "3.10", -0.20, open_interest=5),
        put("175", "1.45", "1.55", -0.15),
    )

    decision = evaluate("AAPL", snapshot, ample(), config.strategy, config.risk, NOW)

    assert isinstance(decision, TradeProposal), getattr(decision, "reason", decision)
    assert [leg.strike for leg in decision.legs] == [Decimal(185), Decimal(175)]


# --- the derived width drives every risk figure -----------------------------


def test_credit_ratio_refusal_names_the_derived_width(config):
    """The rejection quotes the width this chain produced, not a configured one.

    Short 185 mid 3.40 against long 180 mid 2.30 is a 1.10 credit on a 5-wide
    spread: 22.0%, below the 33.3% minimum.
    """
    snapshot = snapshot_from(
        put("185", "3.35", "3.45", -0.30),
        put("180", "2.25", "2.35", -0.20),
    )

    decision = evaluate("AAPL", snapshot, ample(), config.strategy, config.risk, NOW)

    assert isinstance(decision, NoTrade)
    assert decision.reason == (
        "credit 1.10 is 22.0% of the 5.00-wide spread, below the 33.3% minimum"
    )


def test_max_loss_and_sizing_use_the_derived_width(config):
    """A 10-wide spread risks (10.00 - credit) x 100, not (5 - credit) x 100.

    Short 185 mid 5.00, long 175 mid 1.50: credit 3.50, defined risk 650 per
    contract. The 2% budget on 100,000 is 2,000, affording three contracts
    (1,950 max loss). A fixed 5-wide assumption would have said 150 per
    contract and hit the 10-contract cap -- four times the real exposure.
    """
    snapshot = snapshot_from(
        put("185", "4.95", "5.05", -0.30),
        put("175", "1.45", "1.55", -0.20),
    )

    decision = evaluate("AAPL", snapshot, ample(), config.strategy, config.risk, NOW)

    assert isinstance(decision, TradeProposal), getattr(decision, "reason", decision)
    assert decision.limit_price == Decimal("3.50")
    assert decision.quantity == 3
    assert decision.max_loss == Decimal(1_950)
    assert decision.max_profit == Decimal(1_050)
    assert decision.buying_power_effect == Decimal(1_950)


@pytest.mark.parametrize(
    ("long_bid", "long_ask"),
    [
        ("1.60", "1.70"),  # long mid 1.65 == short mid 1.65: zero credit
        ("1.75", "1.85"),  # long mid 1.80 > short mid 1.65: a debit
    ],
)
def test_zero_or_negative_net_credit_is_refused(config, long_bid, long_ask):
    """A long put quoted at or above the short put collects nothing.

    The ratio screen would divide a zero credit into 0.0% and blame the
    minimum; the honest reason is that there is no credit at all.
    """
    snapshot = snapshot_from(
        put("185", "1.60", "1.70", -0.30),
        put("180", long_bid, long_ask, -0.20),
    )

    decision = evaluate("AAPL", snapshot, ample(), config.strategy, config.risk, NOW)

    assert isinstance(decision, NoTrade)
    assert decision.reason == "185/180 put spread offers no net credit"


# --- expiry and delta bands ---------------------------------------------------


def test_empty_expiry_band_is_refused_naming_the_band(config):
    """A chain with only a 14-DTE expiry has nothing in the 30-60 band."""
    snapshot = snapshot_from(
        quote("AAPL", NEAR_EXPIRY, "185", Right.PUT, "3.35", "3.45", -0.30),
        quote("AAPL", NEAR_EXPIRY, "180", Right.PUT, "1.60", "1.70", -0.20),
    )

    decision = evaluate("AAPL", snapshot, ample(), config.strategy, config.risk, NOW)

    assert isinstance(decision, NoTrade)
    assert decision.reason == "no expiry between 30 and 60 DTE"


def test_empty_short_delta_band_is_refused_naming_the_band(config):
    """Puts at 0.45 and 0.15 delta straddle the 0.20-0.40 band without entering it."""
    snapshot = snapshot_from(
        put("200", "7.30", "7.50", -0.45),
        put("180", "1.60", "1.70", -0.15),
    )

    decision = evaluate("AAPL", snapshot, ample(), config.strategy, config.risk, NOW)

    assert isinstance(decision, NoTrade)
    assert decision.reason == "no 45-DTE short put between 0.20 and 0.40 delta"


# --- portfolio ceilings ------------------------------------------------------


def _config_with_risk(**risk):
    return build_config({"universe": ["AAPL"], "ibkr": {"account": ACCOUNT}, "risk": risk})


def test_position_limit_refuses_a_new_underlying_naming_the_count():
    """Two underlyings held against a limit of two leaves no slot for a third."""
    config = _config_with_risk(max_positions=2)
    portfolio = Portfolio(
        net_liquidation=Decimal(100_000),
        buying_power=Decimal(100_000),
        positions=(Position("MSFT", -1), Position("SPY", -2)),
    )
    snapshot = snapshot_from(
        put("185", "3.35", "3.45", -0.30),
        put("180", "1.60", "1.70", -0.20),
    )

    decision = evaluate("AAPL", snapshot, portfolio, config.strategy, config.risk, NOW)

    assert isinstance(decision, NoTrade)
    assert decision.reason == "portfolio already holds 2 underlyings (limit 2)"


def test_position_limit_ignores_closed_positions():
    """A zero-quantity row is history, not exposure, and frees its slot."""
    config = _config_with_risk(max_positions=2)
    portfolio = Portfolio(
        net_liquidation=Decimal(100_000),
        buying_power=Decimal(100_000),
        positions=(Position("MSFT", -1), Position("SPY", 0)),
    )
    snapshot = snapshot_from(
        put("185", "3.35", "3.45", -0.30),
        put("180", "1.60", "1.70", -0.20),
    )

    decision = evaluate("AAPL", snapshot, portfolio, config.strategy, config.risk, NOW)

    assert isinstance(decision, TradeProposal), getattr(decision, "reason", decision)


def test_contract_cap_binds_the_size_and_is_never_exceeded():
    """The budget affords six 325-risk contracts; a cap of one sends one.

    Sizing shrinks to the tightest ceiling and never grows past it, whatever
    the credit looks like.
    """
    config = _config_with_risk(max_contracts=1)
    snapshot = snapshot_from(
        put("185", "3.35", "3.45", -0.30),
        put("180", "1.60", "1.70", -0.20),
    )

    decision = evaluate("AAPL", snapshot, ample(), config.strategy, config.risk, NOW)

    assert isinstance(decision, TradeProposal), getattr(decision, "reason", decision)
    assert decision.quantity == 1
    assert decision.max_loss == Decimal(325)


def test_contract_cap_refusal_names_the_cap(config):
    """A cap that affords nothing is reported as the cap, not as the budget.

    ``RiskConfig`` refuses ``max_contracts < 1`` at construction, so this
    branch is unreachable from a loaded configuration. It is pinned anyway:
    the sizing function attributes a zero result to the ceiling that produced
    it, and if that constraint ever loosens the message must already be right.
    ``model_construct`` bypasses validation to build the otherwise-impossible
    value.
    """
    risk = RiskConfig.model_construct(max_contracts=0)
    snapshot = snapshot_from(
        put("185", "3.35", "3.45", -0.30),
        put("180", "1.60", "1.70", -0.20),
    )

    decision = evaluate("AAPL", snapshot, ample(), config.strategy, risk, NOW)

    assert isinstance(decision, NoTrade)
    assert decision.reason == "the contract cap is 0, so no position can be opened"


# --- DTE is counted on the market calendar ------------------------------------


@pytest.mark.parametrize(
    "as_of",
    [
        # 02:00 UTC on the 16th is 21:00 Eastern on the 15th: the UTC date has
        # rolled over while the session date has not. A UTC count says 44.
        datetime(2026, 1, 16, 2, 0, tzinfo=UTC),
        # 23:00 UTC on the 15th is 18:00 Eastern on the same day: the two
        # calendars agree, and the count must not be shifted the other way.
        datetime(2026, 1, 15, 23, 0, tzinfo=UTC),
    ],
)
def test_dte_is_counted_from_the_eastern_date_not_the_utc_date(config, as_of):
    """DTE to the 1 March expiry from the evening of 15 January is 45.

    Both instants fall on the US session date of 15 January; only the first
    has a UTC date of the 16th, which is the one a naive ``as_of.date()``
    would count from.
    """
    snapshot = snapshot_from(
        put("185", "3.35", "3.45", -0.30),
        put("180", "1.60", "1.70", -0.20),
        as_of=as_of,
    )

    decision = evaluate("AAPL", snapshot, ample(), config.strategy, config.risk, NOW)

    assert isinstance(decision, TradeProposal), getattr(decision, "reason", decision)
    assert decision.dte == 45
    assert decision.criteria["dte"] == "45 within [30, 60], target 45"


def test_an_expiry_on_the_band_edge_is_kept_by_the_eastern_date(config):
    """The 14 February expiry is 30 DTE from 15 January and 29 from the 16th.

    At 02:00 UTC on the 16th the session date is still the 15th, so the expiry
    is inside the [30, 60] band. Counting from the UTC date would drop it and
    refuse the symbol for want of an expiry it actually has.
    """
    edge_expiry = date(2026, 2, 14)
    snapshot = snapshot_from(
        quote("AAPL", edge_expiry, "185", Right.PUT, "3.35", "3.45", -0.30),
        quote("AAPL", edge_expiry, "180", Right.PUT, "1.60", "1.70", -0.20),
        as_of=datetime(2026, 1, 16, 2, 0, tzinfo=UTC),
    )

    decision = evaluate("AAPL", snapshot, ample(), config.strategy, config.risk, NOW)

    assert isinstance(decision, TradeProposal), getattr(decision, "reason", decision)
    assert decision.dte == 30
