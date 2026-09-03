"""The mission test.

One symbol, a known-good Tastytrade setup, an approving reviewer, a broker that
fills. It drives the real production runner end to end and asserts that exactly
one expected order reaches the broker and is durably recorded.

If this fails, CI fails. It must never be made green by calling the algorithm
directly, by stubbing the runner, or by skipping on a clock condition: time here
comes from a :class:`FixedClock`, so the test behaves identically at any hour.
"""

from __future__ import annotations

from datetime import UTC
from decimal import Decimal

from ibkr_trader.clock import FixedClock
from ibkr_trader.models import Action, Outcome, Right

from .fakes import (
    EXPECTED_CREDIT,
    EXPECTED_LONG_STRIKE,
    EXPECTED_QUANTITY,
    EXPECTED_SHORT_STRIKE,
    GOOD_EXPIRY,
)
from .harness import build_runner


def test_production_runner_places_known_good_order(tmp_path):
    """The full vertical slice: scan -> algorithm -> review -> submit -> record."""
    runner, market, reviewer, broker, store = build_runner(tmp_path)

    summary = runner.run_once()

    # --- the symbol was scanned ---
    assert market.requested == ["AAPL"]

    # --- exactly one trade was proposed ---
    assert summary.scanned == 1
    assert summary.proposals == 1
    result = summary.results[0]
    proposal = result.proposal
    assert proposal is not None, "the known-good fixture must produce a proposal"

    # --- it is the specific spread we expect, not merely "some" spread ---
    assert proposal.symbol == "AAPL"
    assert proposal.expiry == GOOD_EXPIRY
    assert proposal.dte == 45
    assert proposal.quantity == EXPECTED_QUANTITY
    assert proposal.limit_price == EXPECTED_CREDIT
    assert len(proposal.legs) == 2

    short_leg = next(leg for leg in proposal.legs if leg.action is Action.SELL)
    long_leg = next(leg for leg in proposal.legs if leg.action is Action.BUY)
    assert short_leg.strike == EXPECTED_SHORT_STRIKE
    assert long_leg.strike == EXPECTED_LONG_STRIKE
    assert short_leg.right is Right.PUT and long_leg.right is Right.PUT

    # defined risk: (width - credit) x multiplier, per contract
    assert proposal.max_profit == EXPECTED_CREDIT * 100 * EXPECTED_QUANTITY
    assert proposal.max_loss == (Decimal(5) - EXPECTED_CREDIT) * 100 * EXPECTED_QUANTITY

    # --- review was requested exactly once, for this proposal, and approved ---
    assert reviewer.call_count == 1
    assert reviewer.reviewed[0].proposal_id == proposal.proposal_id
    assert result.review is not None
    assert result.review.approved is True

    # --- the order was submitted exactly once ---
    assert broker.call_count == 1
    assert broker.submitted[0].proposal_id == proposal.proposal_id

    # --- the broker acknowledged and the fill was recorded ---
    execution = result.execution
    assert execution is not None
    assert execution.outcome is Outcome.FILLED
    assert execution.broker_order_id is not None
    assert execution.filled_quantity == EXPECTED_QUANTITY
    assert result.outcome is Outcome.FILLED

    # --- one durable identity ties proposal -> review -> order together ---
    assert execution.order_ref == proposal.proposal_id

    # --- durable records exist in the real store ---
    attempts = store.attempts()
    assert len(attempts) == 1
    assert attempts[0]["symbol"] == "AAPL"
    assert attempts[0]["outcome"] == Outcome.FILLED.value
    assert attempts[0]["proposal_id"] == proposal.proposal_id

    stored_proposals = store.proposals()
    assert len(stored_proposals) == 1
    assert stored_proposals[0]["proposal_id"] == proposal.proposal_id
    assert Decimal(stored_proposals[0]["limit_price"]) == EXPECTED_CREDIT

    stored_reviews = store.reviews()
    assert len(stored_reviews) == 1
    assert stored_reviews[0]["proposal_id"] == proposal.proposal_id
    assert stored_reviews[0]["approved"] == 1

    stored_orders = store.orders()
    assert len(stored_orders) == 1
    assert stored_orders[0]["order_ref"] == proposal.proposal_id

    stored_fills = store.fills()
    assert len(stored_fills) == 1
    assert stored_fills[0]["quantity"] == EXPECTED_QUANTITY

    # --- the pass completed cleanly ---
    assert summary.filled == 1
    assert summary.errors == 0
    assert summary.orders_submitted == 1


def test_mission_test_does_not_depend_on_wall_clock(tmp_path):
    """The runner reads time only from the injected clock.

    Guards the "never skip near midnight" requirement: the identical fixture run
    at a different injected instant produces the identical order.
    """
    from datetime import datetime

    runner, _, _, broker, _ = build_runner(tmp_path)
    summary = runner.run_once()
    baseline = summary.results[0].proposal
    assert baseline is not None

    # Re-run the same fixture with the clock at a hostile instant.
    runner2, _, _, broker2, _ = build_runner(
        tmp_path / "second",
        clock=FixedClock(datetime(2026, 1, 15, 23, 59, 59, tzinfo=UTC)),
    )
    summary2 = runner2.run_once()
    repeat = summary2.results[0].proposal
    assert repeat is not None

    assert repeat.limit_price == baseline.limit_price
    assert repeat.quantity == baseline.quantity
    assert [leg.strike for leg in repeat.legs] == [leg.strike for leg in baseline.legs]
    assert broker.call_count == 1 and broker2.call_count == 1
