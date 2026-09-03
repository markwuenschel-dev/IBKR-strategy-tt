"""Symbol-local failures must stay symbol-local.

Every test here asserts two things: the failure was recorded accurately, and the
scan kept going. The second half is the point. The previous architecture failed
because ordinary per-symbol failures were promoted into day-wide modes.

The negative assertions matter as much as the positive ones: when the algorithm
declines, the reviewer must never be called; when the reviewer declines, the
broker must never be called.
"""

from __future__ import annotations

from decimal import Decimal

from ibkr_trader.errors import (
    ExecutionAmbiguous,
    MarketDataError,
    ReviewError,
    ReviewTimeout,
)
from ibkr_trader.models import Outcome, Portfolio, Position

from .fakes import (
    FakeBroker,
    StubMarketData,
    StubReviewer,
    illiquid_snapshot,
    tradable_snapshot,
)
from .harness import build_runner

# --- no setup -------------------------------------------------------------


def test_no_qualifying_setup_never_consults_reviewer_or_broker(tmp_path):
    """A symbol with no valid trade stops at the algorithm.

    The reviewer exists to judge trades. If it is consulted when no trade was
    proposed, the "no LLM call before a proposal exists" rule is broken.
    """
    market = StubMarketData({"AAPL": tradable_snapshot("AAPL", iv_rank=5.0)})
    runner, _, reviewer, broker, store = build_runner(tmp_path, market=market)

    summary = runner.run_once()

    assert summary.results[0].outcome is Outcome.NO_TRADE
    assert "IV rank" in summary.results[0].detail
    assert reviewer.call_count == 0, "reviewer must not see a symbol with no proposal"
    assert broker.call_count == 0
    assert summary.no_trade == 1 and summary.proposals == 0

    recorded = store.attempts()
    assert len(recorded) == 1
    assert recorded[0]["outcome"] == Outcome.NO_TRADE.value
    assert recorded[0]["proposal_id"] is None


def test_illiquid_chain_is_a_no_trade_not_an_error(tmp_path):
    """Untradable markets are a trading decision, not a failure."""
    market = StubMarketData({"AAPL": illiquid_snapshot("AAPL")})
    runner, _, reviewer, broker, _ = build_runner(tmp_path, market=market)

    summary = runner.run_once()

    assert summary.results[0].outcome is Outcome.NO_TRADE
    assert "spread" in summary.results[0].detail
    assert reviewer.call_count == 0
    assert broker.call_count == 0
    assert summary.errors == 0


# --- reviewer declines ----------------------------------------------------


def test_reviewer_rejection_blocks_submission_and_is_recorded(tmp_path):
    """A rejected proposal never reaches the broker."""
    reviewer = StubReviewer(approved=False, reason="spread width exceeds preference")
    runner, _, _, broker, store = build_runner(tmp_path, reviewer=reviewer)

    summary = runner.run_once()
    result = summary.results[0]

    assert result.outcome is Outcome.REVIEW_REJECTED
    assert result.detail == "spread width exceeds preference"
    assert reviewer.call_count == 1
    assert broker.call_count == 0, "a rejected proposal must never be submitted"

    # The proposal and the rejection are both durable, so the decision is auditable.
    assert len(store.proposals()) == 1
    reviews = store.reviews()
    assert len(reviews) == 1
    assert reviews[0]["approved"] == 0
    assert reviews[0]["reason"] == "spread width exceeds preference"
    assert store.orders() == []


def test_reviewer_timeout_blocks_submission_and_scan_continues(tmp_path):
    """A silent reviewer means no trade, not an approved trade."""
    reviewer = StubReviewer(error=ReviewTimeout("reviewer did not answer within 90s"))
    market = StubMarketData(
        {"AAPL": tradable_snapshot("AAPL"), "MSFT": tradable_snapshot("MSFT")}
    )
    runner, _, _, broker, store = build_runner(
        tmp_path, universe=("AAPL", "MSFT"), market=market, reviewer=reviewer
    )

    summary = runner.run_once()

    assert [r.outcome for r in summary.results] == [
        Outcome.REVIEW_TIMEOUT,
        Outcome.REVIEW_TIMEOUT,
    ]
    assert broker.call_count == 0
    assert summary.review_failed == 2
    # The second symbol was still processed: a reviewer failure is not a day-wide mode.
    assert len(store.attempts()) == 2


def test_reviewer_malformed_answer_is_review_error_not_approval(tmp_path):
    """An unparsable answer is conservatively treated as no trade."""
    reviewer = StubReviewer(error=ReviewError("unparsable reviewer response"))
    runner, _, _, broker, store = build_runner(tmp_path, reviewer=reviewer)

    summary = runner.run_once()

    assert summary.results[0].outcome is Outcome.REVIEW_ERROR
    assert broker.call_count == 0
    assert store.orders() == []


# --- broker declines ------------------------------------------------------


def test_broker_rejection_records_exact_message_and_continues(tmp_path):
    """The broker's own rejection text is preserved verbatim.

    Paraphrasing a venue rejection is how operators lose the ability to diagnose
    why an order failed.
    """
    rejection = "201: Order rejected - insufficient margin for this order"
    broker = FakeBroker(outcome=Outcome.BROKER_REJECTED, message=rejection)
    market = StubMarketData(
        {"AAPL": tradable_snapshot("AAPL"), "MSFT": tradable_snapshot("MSFT")}
    )
    runner, _, reviewer, _, store = build_runner(
        tmp_path, universe=("AAPL", "MSFT"), market=market, broker=broker
    )

    summary = runner.run_once()

    assert [r.outcome for r in summary.results] == [
        Outcome.BROKER_REJECTED,
        Outcome.BROKER_REJECTED,
    ]
    assert summary.results[0].detail == rejection

    orders = store.orders()
    assert len(orders) == 2
    assert orders[0]["message"] == rejection
    assert orders[0]["outcome"] == Outcome.BROKER_REJECTED.value
    # The second symbol was still attempted.
    assert reviewer.call_count == 2
    assert broker.call_count == 2


def test_ambiguous_execution_is_scoped_to_one_order(tmp_path):
    """An ambiguous submission does not latch anything globally.

    This is the one failure that legitimately needs follow-up, so the test pins
    the boundary: the affected order is flagged for reconciliation, and the next
    symbol is still submitted normally.
    """

    class AmbiguousOnceBroker(FakeBroker):
        """Loses the connection on the first order only."""

        def submit(self, proposal):
            if not self.submitted:
                self.submitted.append(proposal)
                raise ExecutionAmbiguous(
                    "connection lost during transmission",
                    order_ref=proposal.proposal_id,
                )
            return super().submit(proposal)

    broker = AmbiguousOnceBroker(outcome=Outcome.FILLED)
    market = StubMarketData(
        {"AAPL": tradable_snapshot("AAPL"), "MSFT": tradable_snapshot("MSFT")}
    )
    runner, _, _, _, store = build_runner(
        tmp_path, universe=("AAPL", "MSFT"), market=market, broker=broker
    )

    summary = runner.run_once()

    assert summary.results[0].outcome is Outcome.EXECUTION_AMBIGUOUS
    assert summary.results[1].outcome is Outcome.FILLED, (
        "an ambiguous order must not stop the next symbol from trading"
    )
    assert summary.ambiguous == 1
    assert summary.filled == 1

    # The ambiguous order is recorded under its durable reference, so it can be
    # reconciled by id rather than guessed at.
    orders = {o["outcome"]: o for o in store.orders()}
    assert orders[Outcome.EXECUTION_AMBIGUOUS.value]["order_ref"] == (
        summary.results[0].proposal.proposal_id
    )


# --- symbol isolation -----------------------------------------------------


def test_data_error_on_one_symbol_does_not_block_another(tmp_path):
    """The headline isolation guarantee, with a typed data failure."""
    market = StubMarketData(
        snapshots={"AAPL": tradable_snapshot("AAPL")},
        failures={"SPY": MarketDataError("option chain unavailable")},
    )
    runner, _, reviewer, broker, store = build_runner(
        tmp_path, universe=("SPY", "AAPL"), market=market
    )

    summary = runner.run_once()

    assert summary.results[0].symbol == "SPY"
    assert summary.results[0].outcome is Outcome.DATA_ERROR
    assert summary.results[1].symbol == "AAPL"
    assert summary.results[1].outcome is Outcome.FILLED

    # The healthy symbol traded normally.
    assert reviewer.call_count == 1
    assert broker.call_count == 1
    assert broker.submitted[0].symbol == "AAPL"
    assert len(store.attempts()) == 2


def test_unexpected_exception_on_one_symbol_does_not_block_another(tmp_path):
    """An untyped bug is contained by the isolation boundary.

    ``MarketDataError`` is handled precisely; a bare ``RuntimeError`` is not, so
    this exercises the deliberate catch-all in ``_process_symbol_safely`` rather
    than the typed path.
    """
    market = StubMarketData(
        snapshots={"AAPL": tradable_snapshot("AAPL")},
        failures={"SPY": RuntimeError("unexpected parser bug")},
    )
    runner, _, _, broker, store = build_runner(
        tmp_path, universe=("SPY", "AAPL"), market=market
    )

    summary = runner.run_once()

    assert summary.results[0].outcome is Outcome.ERROR
    assert "RuntimeError" in summary.results[0].detail
    assert summary.results[1].outcome is Outcome.FILLED
    assert broker.call_count == 1
    assert len(store.attempts()) == 2


def test_portfolio_limits_stop_new_trades_without_erroring(tmp_path):
    """A concentration limit is a no-trade, not a failure."""
    held = Portfolio(
        net_liquidation=Decimal(50_000),
        buying_power=Decimal(25_000),
        positions=(Position(symbol="AAPL", quantity=-3, description="185/180 put"),),
    )
    market = StubMarketData({"AAPL": tradable_snapshot("AAPL")}, portfolio=held)
    runner, _, reviewer, broker, _ = build_runner(tmp_path, market=market)

    summary = runner.run_once()

    assert summary.results[0].outcome is Outcome.NO_TRADE
    assert "already holding" in summary.results[0].detail
    assert reviewer.call_count == 0
    assert broker.call_count == 0


def test_disconnected_broker_does_not_lose_the_proposal(tmp_path):
    """When the broker is down, the proposal and its approval are still recorded."""
    broker = FakeBroker(connected=False)
    runner, _, reviewer, _, store = build_runner(tmp_path, broker=broker)

    summary = runner.run_once()

    assert summary.results[0].outcome is Outcome.SUBMISSION_FAILED
    assert broker.call_count == 0, "nothing may be transmitted while disconnected"
    assert reviewer.call_count == 1
    assert len(store.proposals()) == 1
    assert len(store.reviews()) == 1
    assert store.orders() == []
