"""What happens when the system cannot rule on a trade safely by itself.

The duplicate-order guard and the ``max_positions`` limit both key on rows the
market-data adapter synthesizes from still-working orders. When that read fails,
the adapter used to return ``[]`` -- producing a portfolio byte-identical to one
with genuinely no working orders. Both guards were then *skipped* rather than
failed, and the docstring's claim that the resulting duplicate is "bounded by
``max_positions``" is circular: that bound is computed from the very rows the
failure discarded.

The ruling on this is neither "proceed anyway" nor "halt the pass". It is:

    do not submit the affected trade blindly; record it as awaiting a decision;
    keep scanning other symbols; revisit once a human has ruled.

These tests cover the part of that which lives in this repository: the adapter
reports the ambiguity instead of destroying it, the algorithm refuses to submit
under it, the outcome is durably distinguishable from both "no trade" and
"error", and one ambiguous symbol does not stop the ones after it.

The escalation *transport* -- delivering the decision request and ingesting the
answer -- is deliberately not here. It is a separate authorized subsystem. What
is here is the signal it will carry, without which no transport could exist.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from ibkr_trader.models import NeedsDecision, Outcome, Portfolio, TradeProposal
from ibkr_trader.tastytrade import evaluate

from .fakes import (
    SCAN_TIME,
    FakeBroker,
    StubMarketData,
    illiquid_snapshot,
    tradable_snapshot,
)
from .harness import build_runner

# --- the signal exists at all -------------------------------------------


def test_a_portfolio_knows_whether_its_working_orders_are_trustworthy():
    """Without this flag there is nothing to escalate about.

    A portfolio built from a failed order-stream read is otherwise identical to
    one built from a genuinely empty one, so no caller can tell them apart.
    """
    healthy = Portfolio(net_liquidation=Decimal(50_000), buying_power=Decimal(25_000))
    assert healthy.pending_orders_known is True

    unknown = Portfolio(
        net_liquidation=Decimal(50_000),
        buying_power=Decimal(25_000),
        pending_orders_known=False,
    )
    assert unknown.pending_orders_known is False


def test_the_adapter_reports_the_ambiguity_instead_of_discarding_it():
    """`return []` destroyed the only evidence that anything went wrong."""
    market = StubMarketData({"AAPL": tradable_snapshot("AAPL")}, pending_orders_known=False)

    portfolio = market.portfolio()

    assert portfolio.pending_orders_known is False


# --- the algorithm refuses to submit under it ---------------------------


def config_pair():
    from ibkr_trader.config import build_config

    config = build_config({"universe": ["AAPL"]})
    return config.strategy, config.risk


def test_a_tradable_symbol_becomes_a_decision_request_when_pending_state_is_unknown():
    """The trade that would have been submitted is exactly the one at risk.

    This snapshot yields a real proposal under a healthy portfolio. Under an
    ambiguous one it must not: neither guard that would have refused it can be
    trusted to have run.
    """
    strategy, risk = config_pair()
    snapshot = tradable_snapshot("AAPL")

    healthy = Portfolio(net_liquidation=Decimal(50_000), buying_power=Decimal(25_000))
    ambiguous = Portfolio(
        net_liquidation=Decimal(50_000),
        buying_power=Decimal(25_000),
        pending_orders_known=False,
    )

    assert isinstance(
        evaluate("AAPL", snapshot, healthy, strategy, risk, SCAN_TIME), TradeProposal
    )

    decision = evaluate("AAPL", snapshot, ambiguous, strategy, risk, SCAN_TIME)

    assert isinstance(decision, NeedsDecision)
    assert decision.symbol == "AAPL"
    assert "unknown" in decision.reason
    assert "duplicate-symbol" in decision.reason and "position limit" in decision.reason
    assert decision.proposal is not None, "the ruling needs the trade as context"
    assert decision.proposal.symbol == "AAPL"


def test_a_symbol_that_would_not_have_traded_anyway_is_not_escalated():
    """Ambiguity only matters where it changes the answer.

    Escalating a symbol the algorithm was going to decline regardless would
    spend a human decision on nothing, and would bury the requests that matter.
    """
    strategy, risk = config_pair()
    ambiguous = Portfolio(
        net_liquidation=Decimal(50_000),
        buying_power=Decimal(25_000),
        pending_orders_known=False,
    )

    decision = evaluate("XYZ", illiquid_snapshot("XYZ"), ambiguous, strategy, risk, SCAN_TIME)

    assert not isinstance(decision, NeedsDecision)


def test_an_already_refused_symbol_is_not_escalated_either():
    """A held position refuses on evidence that is present, not evidence missing."""
    from ibkr_trader.models import Position

    strategy, risk = config_pair()
    held = Portfolio(
        net_liquidation=Decimal(50_000),
        buying_power=Decimal(25_000),
        positions=(Position(symbol="AAPL", quantity=1, description="filled"),),
        pending_orders_known=False,
    )

    decision = evaluate("AAPL", tradable_snapshot("AAPL"), held, strategy, risk, SCAN_TIME)

    assert not isinstance(decision, NeedsDecision)
    assert "already holding" in decision.reason


# --- it is durable, and it does not stop the pass -----------------------


def test_the_outcome_is_distinguishable_from_both_no_trade_and_error():
    """Three different things a human responds to three different ways."""
    assert Outcome.AWAITING_DECISION not in {
        Outcome.NO_TRADE,
        Outcome.ERROR,
        Outcome.DATA_ERROR,
    }
    from ibkr_trader.models import SUBMITTED_OUTCOMES

    assert Outcome.AWAITING_DECISION not in SUBMITTED_OUTCOMES, (
        "an escalated trade has not reached the venue"
    )


def test_an_escalated_symbol_is_recorded_and_never_submitted(tmp_path):
    """Durable, because the decision outlives the pass that raised it."""
    market = StubMarketData({"AAPL": tradable_snapshot("AAPL")}, pending_orders_known=False)
    broker = FakeBroker()
    runner, _, reviewer, _, store = build_runner(
        tmp_path, universe=("AAPL",), market=market, broker=broker
    )

    summary = runner.run_once()

    rows = store._conn.execute("SELECT symbol, outcome FROM symbol_attempts").fetchall()
    store.close()

    assert [(r["symbol"], r["outcome"]) for r in rows] == [
        ("AAPL", Outcome.AWAITING_DECISION.value)
    ]
    assert broker.call_count == 0, "nothing may reach the venue under an unknown state"
    assert reviewer.call_count == 0, "no point spending a review on a blocked trade"
    assert summary.awaiting_decision == 1


def test_one_ambiguous_symbol_does_not_stop_the_symbols_after_it(tmp_path):
    """The explicit ruling: no unresolved escalation becomes a day-wide stop.

    Both symbols are tradable and the portfolio is ambiguous for the whole pass,
    so both escalate -- what matters is that the second one is still *reached*
    and recorded rather than the pass ending at the first.
    """
    market = StubMarketData(
        {"AAPL": tradable_snapshot("AAPL"), "MSFT": tradable_snapshot("MSFT")},
        pending_orders_known=False,
    )
    runner, _, _, broker, store = build_runner(
        tmp_path, universe=("AAPL", "MSFT"), market=market
    )

    summary = runner.run_once()

    rows = store._conn.execute(
        "SELECT symbol, outcome FROM symbol_attempts ORDER BY id"
    ).fetchall()
    store.close()

    assert [r["symbol"] for r in rows] == ["AAPL", "MSFT"]
    assert {r["outcome"] for r in rows} == {Outcome.AWAITING_DECISION.value}
    assert summary.scanned == 2
    assert summary.awaiting_decision == 2
    assert broker.call_count == 0


def test_the_operator_summary_names_the_pending_decisions(tmp_path):
    """An operator must not have to read the database to learn a ruling is owed."""
    market = StubMarketData({"AAPL": tradable_snapshot("AAPL")}, pending_orders_known=False)
    runner, *_ = build_runner(tmp_path, universe=("AAPL",), market=market)

    rendered = runner.run_once().render()

    assert "Awaiting decision: 1" in rendered


def test_a_healthy_pass_reports_no_pending_decisions(tmp_path):
    """The line must not be noise on the ordinary path."""
    market = StubMarketData({"AAPL": tradable_snapshot("AAPL")})
    runner, *_ = build_runner(tmp_path, universe=("AAPL",), market=market)

    summary = runner.run_once()

    assert summary.awaiting_decision == 0
    assert "Awaiting decision" not in summary.render()


# --- the docstrings that were false -------------------------------------


def test_the_adapter_no_longer_claims_a_bound_it_cannot_provide():
    """`scanner.py` justified proceeding by a bound computed from the discarded rows.

    The claim was: "the duplicate this could admit is bounded by max_positions".
    max_positions is enforced against open_symbol_count, which is derived from
    the same positions tuple the failure emptied. Circular, and it was the
    stated reason for the design choice.
    """
    import inspect

    from ibkr_trader.scanner import IBKRMarketData

    source = " ".join(inspect.getsource(IBKRMarketData._pending_positions).split())

    # The docstring still quotes the old claim -- in order to refute it -- so a
    # bare substring check cannot tell assertion from correction. What must be
    # true is that it is named as the discarded rationale, not offered as one.
    assert "circular" in source, "the bound is still offered as a justification"
    assert "previous rationale" in source


def test_the_adapter_no_longer_claims_to_see_other_clients_orders():
    """Nothing in this repository requests orders from other clients.

    `client_id` defaults to 1, not 0, and there is no reqAllOpenOrders or
    master-client call anywhere, so a manually entered spread is not visible.
    The docstring said the opposite, permanently and without any failure.
    """
    import inspect

    from ibkr_trader.scanner import IBKRMarketData

    # Collapsed, because the phrases under test wrap across lines.
    source = " ".join(inspect.getsource(IBKRMarketData._pending_positions).split())

    assert "not only orders this process placed" not in source
    assert "orders of *this client*" in source, "the real scope is not stated"
    assert "client_id" in source, "the reason for the scope is not given"


@pytest.mark.parametrize("call", ["reqAllOpenOrders", "reqAutoOpenOrders", "masterClient"])
def test_the_repository_still_does_not_request_other_clients_orders(call):
    """Pins the fact the docstring now states, so the two cannot drift apart."""
    from pathlib import Path

    package = Path(__file__).resolve().parent.parent / "ibkr_trader"
    # Look for a call, not a mention: the scanner docstring now names these
    # precisely to record that they are absent.
    hits = [
        p.name for p in package.glob("*.py") if f"{call}(" in p.read_text(encoding="utf-8")
    ]

    assert hits == [], f"{call} is now called in {hits}; the docstring needs updating"


# --- INT-023 fallout: instrument identity ------------------------------


def snapshot_on_class(symbol, trading_class):
    from dataclasses import replace

    return replace(tradable_snapshot(symbol), trading_class=trading_class)


def test_a_proposal_on_a_non_standard_trading_class_is_escalated_not_submitted():
    """The broker would trade a different instrument than the one reviewed.

    `broker._build_option` omits tradingClass entirely, so a proposal quoted and
    reviewed against an adjusted class -- AAPL1, left behind by a split or a
    special dividend -- would be resolved by the broker as standard AAPL at the
    same strike and expiry. Not a rejected order: a *filled* one, on a different
    deliverable, that nothing in the record would distinguish from the intended
    trade.

    Until that is fixed at the broker, the algorithm refuses to hand it over.
    """
    strategy, risk = config_pair()
    healthy = Portfolio(net_liquidation=Decimal(50_000), buying_power=Decimal(25_000))

    decision = evaluate(
        "AAPL", snapshot_on_class("AAPL", "AAPL1"), healthy, strategy, risk, SCAN_TIME
    )

    assert isinstance(decision, NeedsDecision)
    assert "AAPL1" in decision.reason
    assert "trading class" in decision.reason
    assert decision.proposal is not None


def test_the_standard_trading_class_proposes_normally():
    """The ordinary path must not be disturbed by the guard."""
    strategy, risk = config_pair()
    healthy = Portfolio(net_liquidation=Decimal(50_000), buying_power=Decimal(25_000))

    decision = evaluate(
        "AAPL", snapshot_on_class("AAPL", "AAPL"), healthy, strategy, risk, SCAN_TIME
    )

    assert isinstance(decision, TradeProposal)


def test_an_unknown_trading_class_proposes_normally():
    """An empty class is 'not reported', not 'reported as wrong'.

    Snapshots built by doubles that predate this field must keep working, and a
    missing value is not evidence of a non-standard instrument.
    """
    strategy, risk = config_pair()
    healthy = Portfolio(net_liquidation=Decimal(50_000), buying_power=Decimal(25_000))

    decision = evaluate(
        "AAPL", snapshot_on_class("AAPL", ""), healthy, strategy, risk, SCAN_TIME
    )

    assert isinstance(decision, TradeProposal)


def test_a_non_standard_class_still_does_not_stop_the_pass(tmp_path):
    """Other eligible trades keep processing, per the same ruling."""
    market = StubMarketData(
        {
            "AAPL": snapshot_on_class("AAPL", "AAPL1"),
            "MSFT": snapshot_on_class("MSFT", "MSFT"),
        }
    )
    runner, _, _, broker, store = build_runner(
        tmp_path, universe=("AAPL", "MSFT"), market=market
    )

    summary = runner.run_once()

    rows = store._conn.execute(
        "SELECT symbol, outcome FROM symbol_attempts ORDER BY id"
    ).fetchall()
    store.close()

    outcomes = {r["symbol"]: r["outcome"] for r in rows}
    assert outcomes["AAPL"] == Outcome.AWAITING_DECISION.value
    assert outcomes["MSFT"] != Outcome.AWAITING_DECISION.value, (
        "the eligible symbol must still be processed"
    )
    assert summary.awaiting_decision == 1
    assert broker.call_count == 1, "exactly the eligible trade reached the venue"
