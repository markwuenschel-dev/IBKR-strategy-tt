"""Tests for the pure ranking of a pass's proposals.

Every test states the concrete number that makes it fail. The weights are read
from the module rather than restated here, so a deliberate reweighting does not
have to be mirrored in a dozen literals -- but the *shape* of the arithmetic
(percentile per figure, weighted sum, tie-break chain) is pinned exactly.

Fixtures are built locally. ``tests/fakes.py`` is being edited by another lane,
and these tests need nothing from it beyond a proposal with two legs.
"""

from __future__ import annotations

import math
import random
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from ibkr_trader.models import Action, ProposalLeg, Right, TradeProposal
from ibkr_trader.ranking import (
    WEIGHT_CREDIT_RATIO,
    WEIGHT_IV_RANK,
    WEIGHT_LIQUIDITY,
    RankedProposal,
    _percentiles,
    credit_ratio,
    leg_liquidity,
    rank_proposals,
    spread_width,
)

EXPIRY = date(2026, 2, 20)
CREATED = datetime(2026, 1, 15, 14, 31, tzinfo=UTC)

#: Books with a 10% relative spread on each leg: spread 0.20 on mid 2.00 and
#: spread 0.10 on mid 1.00. Mean 0.10.
TIGHT_SHORT = ("1.90", "2.10")
TIGHT_LONG = ("0.95", "1.05")

#: Books with 50% and 100% relative spreads: 1.00 on mid 2.00 and 1.00 on
#: mid 1.00. Mean 0.75.
WIDE_SHORT = ("1.50", "2.50")
WIDE_LONG = ("0.50", "1.50")

#: A dead book: bid and ask both zero, so mid is zero and spread_pct infinite.
DEAD = ("0", "0")


def leg(action: Action, strike: str, book: tuple[str, str]) -> ProposalLeg:
    bid, ask = book
    return ProposalLeg(
        action=action,
        right=Right.PUT,
        strike=Decimal(strike),
        expiry=EXPIRY,
        ratio=1,
        bid=Decimal(bid),
        ask=Decimal(ask),
        delta=-0.30 if action is Action.SELL else -0.20,
        open_interest=500,
        volume=100,
    )


def vertical(
    symbol: str,
    short_strike: str = "185",
    long_strike: str = "180",
    credit: str = "1.75",
    iv_rank: float = 50.0,
    short_book: tuple[str, str] = TIGHT_SHORT,
    long_book: tuple[str, str] = TIGHT_LONG,
    legs: tuple[ProposalLeg, ...] | None = None,
) -> TradeProposal:
    """A put credit spread: sell ``short_strike``, buy ``long_strike``.

    ``legs`` overrides the two-leg default for the malformed cases -- a
    proposal missing one side -- which the builder would otherwise refuse to
    express.
    """
    if legs is None:
        legs = (
            leg(Action.SELL, short_strike, short_book),
            leg(Action.BUY, long_strike, long_book),
        )
    width = Decimal(short_strike) - Decimal(long_strike)
    limit = Decimal(credit)
    return TradeProposal(
        symbol=symbol,
        strategy="put_credit_spread",
        expiry=EXPIRY,
        dte=36,
        legs=legs,
        quantity=1,
        limit_price=limit,
        max_profit=limit * 100,
        max_loss=(width - limit) * 100,
        underlying_price=Decimal("195.00"),
        iv_rank=iv_rank,
        short_delta=-0.30,
        buying_power_effect=(width - limit) * 100,
        criteria={},
        created_at=CREATED,
    )


def composite(ratio_pct: float, iv_pct: float, liquidity_pct: float) -> float:
    """The module's weighted sum, in the module's evaluation order.

    Same operand order as :func:`rank_proposals`, so the float result is
    bit-identical and the assertions below can use ``==`` rather than approx.
    """
    return (
        WEIGHT_CREDIT_RATIO * ratio_pct
        + WEIGHT_IV_RANK * iv_pct
        + WEIGHT_LIQUIDITY * liquidity_pct
    )


def order(ranked: tuple[RankedProposal, ...]) -> list[str]:
    return [r.symbol for r in ranked]


# --- the three figures ----------------------------------------------------


def test_spread_width_is_sold_strike_minus_bought_strike():
    assert spread_width(vertical("AAPL", "185", "180")) == Decimal(5)
    assert spread_width(vertical("AAPL", "452.5", "440")) == Decimal("12.5")


def test_credit_ratio_is_limit_price_over_width():
    assert credit_ratio(vertical("AAPL", "185", "180", credit="1.75")) == 0.35
    assert credit_ratio(vertical("AAPL", "100", "90", credit="2.50")) == 0.25


def test_leg_liquidity_is_the_mean_of_the_two_legs_spread_pct():
    # short 1.00/2.00 = 0.5, long 1.00/1.00 = 1.0; mean 0.75 -- neither leg alone.
    proposal = vertical("AAPL", short_book=WIDE_SHORT, long_book=WIDE_LONG)
    assert proposal.legs[0].spread_pct == 0.5
    assert proposal.legs[1].spread_pct == 1.0
    assert leg_liquidity(proposal) == 0.75


def test_leg_liquidity_is_infinite_when_any_leg_has_a_dead_book():
    proposal = vertical("AAPL", short_book=TIGHT_SHORT, long_book=DEAD)
    assert math.isinf(leg_liquidity(proposal))


@pytest.mark.parametrize(
    "legs",
    [
        pytest.param((leg(Action.SELL, "185", TIGHT_SHORT),), id="sell-only"),
        pytest.param((leg(Action.BUY, "180", TIGHT_LONG),), id="buy-only"),
        pytest.param((), id="no-legs"),
    ],
)
def test_width_and_ratio_reject_a_proposal_missing_a_side(legs):
    proposal = vertical("AAPL", legs=legs)
    with pytest.raises(ValueError, match="both a sold and a bought leg"):
        spread_width(proposal)
    with pytest.raises(ValueError, match="both a sold and a bought leg"):
        credit_ratio(proposal)


def test_credit_ratio_rejects_zero_width():
    proposal = vertical("AAPL", "185", "185")
    assert spread_width(proposal) == Decimal(0)
    with pytest.raises(ValueError, match="width 0"):
        credit_ratio(proposal)


def test_credit_ratio_rejects_inverted_legs():
    # Bought strike above sold: a debit structure, width -5.
    proposal = vertical("AAPL", "180", "185")
    with pytest.raises(ValueError, match="width -5"):
        credit_ratio(proposal)


# --- percentiles ----------------------------------------------------------


def test_percentiles_of_three_distinct_values_are_1_half_0():
    assert _percentiles([0.40, 0.35, 0.30], higher_is_better=True) == [1.0, 0.5, 0.0]
    # Lower-is-better inverts which end is 1.0.
    assert _percentiles([0.40, 0.35, 0.30], higher_is_better=False) == [0.0, 0.5, 1.0]


def test_percentiles_of_a_tie_share_the_mean_position():
    # Two equal-best of three: each has 0 better and 1 tie -> position 0.5 of 2.
    assert _percentiles([0.40, 0.40, 0.30], higher_is_better=True) == [0.75, 0.75, 0.0]
    # All equal: every position is 1 of 2.
    assert _percentiles([50.0, 50.0, 50.0], higher_is_better=True) == [0.5, 0.5, 0.5]


def test_a_single_value_is_the_best_of_its_field():
    assert _percentiles([0.0], higher_is_better=True) == [1.0]


# --- composite score ------------------------------------------------------


def test_empty_input_ranks_nothing():
    assert rank_proposals(()) == ()
    assert rank_proposals([]) == ()


def test_a_single_candidate_scores_one_and_ranks_first():
    (only,) = rank_proposals([vertical("AAPL", credit="1.75", iv_rank=42.0)])
    assert only.rank == 1
    assert only.score == 1.0
    assert only.symbol == "AAPL"
    assert only.credit_ratio == 0.35
    assert only.iv_rank == 42.0
    assert only.liquidity == 0.1


def test_best_middle_worst_on_credit_ratio_with_the_rest_tied():
    # Credit 2.00 / 1.75 / 1.50 on a 5-wide: ratios 0.40 / 0.35 / 0.30.
    # IV rank and books identical, so those percentiles are 0.5 for everyone.
    ranked = rank_proposals(
        [
            vertical("MID", credit="1.75"),
            vertical("BEST", credit="2.00"),
            vertical("WORST", credit="1.50"),
        ]
    )
    assert order(ranked) == ["BEST", "MID", "WORST"]
    assert [r.rank for r in ranked] == [1, 2, 3]
    assert [r.score for r in ranked] == [
        composite(1.0, 0.5, 0.5),  # 0.75
        composite(0.5, 0.5, 0.5),  # 0.50
        composite(0.0, 0.5, 0.5),  # 0.25
    ]
    assert [r.score for r in ranked] == pytest.approx([0.75, 0.50, 0.25])


def test_tied_best_on_credit_ratio_both_get_three_quarters():
    ranked = rank_proposals(
        [
            vertical("WORST", credit="1.50"),
            vertical("TIE_B", credit="2.00"),
            vertical("TIE_A", credit="2.00"),
        ]
    )
    # Equal scores, equal ratios, equal IV rank: the symbol decides.
    assert order(ranked) == ["TIE_A", "TIE_B", "WORST"]
    assert ranked[0].score == ranked[1].score == composite(0.75, 0.5, 0.5)  # 0.625
    assert ranked[2].score == composite(0.0, 0.5, 0.5)  # 0.25
    assert ranked[0].score == pytest.approx(0.625)


def test_best_credit_ratio_beats_best_iv_rank_when_liquidity_is_tied():
    # Two candidates: percentiles are 1.0 / 0.0 on each contested figure and
    # 0.5 on the tied one. 0.5 + 0.1 = 0.6 beats 0.3 + 0.1 = 0.4.
    ranked = rank_proposals(
        [
            vertical("RICH_IV", credit="1.50", iv_rank=60.0),
            vertical("RICH_CREDIT", credit="2.00", iv_rank=40.0),
        ]
    )
    assert order(ranked) == ["RICH_CREDIT", "RICH_IV"]
    assert ranked[0].score == composite(1.0, 0.0, 0.5)
    assert ranked[1].score == composite(0.0, 1.0, 0.5)
    assert ranked[0].score == pytest.approx(0.6)
    assert ranked[1].score == pytest.approx(0.4)


def test_best_iv_and_liquidity_ties_best_credit_and_the_tie_goes_to_credit():
    # RICH_CREDIT: 0.5*1 + 0.3*0 + 0.2*0 = 0.5.
    # RICH_REST:   0.5*0 + 0.3*1 + 0.2*1 = 0.5.
    # Equal composites break toward the higher credit ratio, 0.40 over 0.30.
    ranked = rank_proposals(
        [
            vertical("RICH_REST", credit="1.50", iv_rank=60.0),
            vertical(
                "RICH_CREDIT",
                credit="2.00",
                iv_rank=40.0,
                short_book=WIDE_SHORT,
                long_book=WIDE_LONG,
            ),
        ]
    )
    assert ranked[0].score == ranked[1].score == 0.5
    assert order(ranked) == ["RICH_CREDIT", "RICH_REST"]
    assert ranked[0].credit_ratio == 0.4
    assert ranked[1].credit_ratio == 0.3


def test_tighter_legs_rank_higher():
    ranked = rank_proposals(
        [
            vertical("WIDE", short_book=WIDE_SHORT, long_book=WIDE_LONG),
            vertical("TIGHT"),
        ]
    )
    assert order(ranked) == ["TIGHT", "WIDE"]
    assert ranked[0].liquidity == 0.1
    assert ranked[1].liquidity == 0.75
    assert ranked[0].score == composite(0.5, 0.5, 1.0)  # 0.6
    assert ranked[1].score == composite(0.5, 0.5, 0.0)  # 0.4


def test_a_dead_book_ranks_last_on_liquidity_without_poisoning_the_score():
    ranked = rank_proposals(
        [
            vertical("DEAD", long_book=DEAD),
            vertical("LIVE"),
        ]
    )
    assert order(ranked) == ["LIVE", "DEAD"]
    assert math.isinf(ranked[1].liquidity)
    assert ranked[1].score == composite(0.5, 0.5, 0.0)
    assert math.isfinite(ranked[1].score)


# --- determinism ----------------------------------------------------------


FIELD = [
    vertical("AAPL", credit="1.75", iv_rank=42.0),
    vertical("MSFT", credit="2.00", iv_rank=35.0, short_book=WIDE_SHORT),
    vertical("NVDA", credit="1.50", iv_rank=70.0),
    vertical("AMZN", credit="1.75", iv_rank=42.0),  # identical to AAPL
    vertical("GOOG", credit="1.60", iv_rank=55.0, long_book=WIDE_LONG),
]


def test_shuffled_input_yields_the_same_order_and_ranks():
    reference = rank_proposals(FIELD)
    rng = random.Random(20260907)
    for _ in range(25):
        shuffled = list(FIELD)
        rng.shuffle(shuffled)
        ranked = rank_proposals(shuffled)
        assert order(ranked) == order(reference)
        assert [r.rank for r in ranked] == [r.rank for r in reference]
        assert [r.score for r in ranked] == [r.score for r in reference]


def test_ranks_are_one_based_and_contiguous():
    ranked = rank_proposals(FIELD)
    assert [r.rank for r in ranked] == [1, 2, 3, 4, 5]


def test_final_tiebreak_is_the_symbol():
    # AAPL and AMZN are identical on every figure; AAPL sorts first however
    # they arrive. A two-way tie is the mean position, 0.5, on every figure.
    for candidates in ([FIELD[0], FIELD[3]], [FIELD[3], FIELD[0]]):
        ranked = rank_proposals(candidates)
        assert order(ranked) == ["AAPL", "AMZN"]
        assert ranked[0].score == ranked[1].score == composite(0.5, 0.5, 0.5)
        assert ranked[0].score == pytest.approx(0.5)


def test_identical_field_falls_through_to_alphabetical():
    ranked = rank_proposals([vertical("ZZZ"), vertical("MMM"), vertical("AAA")])
    assert order(ranked) == ["AAA", "MMM", "ZZZ"]
    assert {r.score for r in ranked} == {composite(0.5, 0.5, 0.5)}
    assert ranked[0].score == pytest.approx(0.5)


# --- describe -------------------------------------------------------------


def test_describe_renders_place_score_and_the_three_figures():
    ranked = RankedProposal(
        proposal=vertical("AAPL"),
        rank=2,
        score=0.71,
        credit_ratio=0.36,
        iv_rank=42.0,
        liquidity=0.041,
    )
    assert ranked.describe(5) == (
        "rank 2/5 score 0.71 (credit/width 0.36, IV rank 42, leg spread 4.1%)"
    )


def test_describe_of_a_ranked_field_uses_the_computed_figures():
    ranked = rank_proposals([vertical("AAPL", credit="1.75", iv_rank=42.0)])
    assert ranked[0].describe(len(ranked)) == (
        "rank 1/1 score 1.00 (credit/width 0.35, IV rank 42, leg spread 10.0%)"
    )
