"""One owner for derived quote arithmetic, and one owner for the leg's wire shape.

Two numbers describe the same thing and were computed by different code. The
figure that *selects* a contract is ``OptionQuote.spread_pct`` (models.py), read
by the liquidity screen in ``tastytrade``. The figure *shown to the reviewer*
justifying that selection was recomputed inline in ``reviewer.py`` from a
``ProposalLeg``, which carries the same bid and ask but had none of the derived
properties. The two disagreed on the degenerate case -- ``inf`` against ``None``
-- and both disagreements were deliberate, documented, and in different files.

The leg's wire shape had the same problem in a smaller way: two hand-maintained
serializers, ``reviewer._leg_payload`` and ``store._legs_as_json``, listing the
same ten fields, already drifted by the three derived ones.

These tests pin the resolution: the arithmetic has one definition, the wire
shape has one definition, and the ``inf``/``None`` split survives as a single
JSON-safety rule at the serialization boundary rather than as a second
arithmetic.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import json
import math
import textwrap
from datetime import date
from decimal import Decimal

import pytest

from ibkr_trader import models
from ibkr_trader.models import (
    Action,
    OptionQuote,
    Outcome,
    ProposalLeg,
    Right,
    leg_payload,
)
from ibkr_trader.reviewer import _leg_payload
from ibkr_trader.scanner import IBKRMarketData
from ibkr_trader.store import _legs_as_json

# --- fixtures ------------------------------------------------------------

EXPIRY = date(2026, 2, 20)


def option_quote(bid: str, ask: str) -> OptionQuote:
    return OptionQuote(
        symbol="AAPL",
        expiry=EXPIRY,
        strike=Decimal("185"),
        right=Right.PUT,
        bid=Decimal(bid),
        ask=Decimal(ask),
        delta=-0.30,
        open_interest=500,
        volume=100,
    )


def proposal_leg(bid: str, ask: str) -> ProposalLeg:
    return ProposalLeg(
        action=Action.SELL,
        right=Right.PUT,
        strike=Decimal("185"),
        expiry=EXPIRY,
        ratio=1,
        bid=Decimal(bid),
        ask=Decimal(ask),
        delta=-0.30,
        open_interest=500,
        volume=100,
    )


# --- ARCH-C7: one arithmetic ---------------------------------------------

BOOKS = [
    ("3.35", "3.45"),
    ("1.60", "1.70"),
    ("0.05", "0.95"),
    ("10.00", "10.00"),
    ("0.00", "0.00"),
]


@pytest.mark.parametrize(("bid", "ask"), BOOKS)
def test_a_leg_and_a_quote_derive_the_same_numbers_from_the_same_book(bid, ask):
    """The two types carry the same market; they must not describe it differently.

    Includes the zero book, which is where the old two implementations diverged.
    """
    quote = option_quote(bid, ask)
    leg = proposal_leg(bid, ask)

    assert leg.mid == quote.mid
    assert leg.spread == quote.spread
    assert leg.spread_pct == quote.spread_pct or (
        math.isinf(leg.spread_pct) and math.isinf(quote.spread_pct)
    )


def test_the_selection_figure_stays_infinite_on_a_dead_book():
    """`inf` is not an accident -- it is what makes the screen reject.

    ``tastytrade`` tests ``spread_pct > max_spread_pct``. A ``None`` here would
    raise a TypeError inside the pure algorithm; a zero would let an unquotable
    contract through the liquidity screen as if it were perfectly tight.
    """
    assert math.isinf(option_quote("0.00", "0.00").spread_pct)
    assert math.isinf(proposal_leg("0.00", "0.00").spread_pct)


# --- ARCH-C7: one wire shape ---------------------------------------------


@pytest.mark.parametrize(("bid", "ask"), BOOKS)
def test_both_serializers_render_the_leg_through_the_same_owner(bid, ask):
    """Neither serializer may hand-maintain the shape any more.

    Parametrized over every book, the dead one included. A single healthy book
    would not notice a ``_leg_payload`` that diverged only when mid is
    non-positive -- and that is the exact case this change exists to eliminate,
    since it is where the two old implementations disagreed.
    """
    leg = proposal_leg(bid, ask)

    assert _leg_payload(leg) == leg_payload(leg, derived=True)

    proposal = _proposal_with(leg)
    assert _legs_as_json(proposal) == [leg_payload(leg)]


def test_the_two_renderings_differ_only_in_which_fields_they_carry():
    """They may differ in content; they may not differ in how a field is computed."""
    leg = proposal_leg("3.35", "3.45")

    stored = leg_payload(leg)
    reviewed = leg_payload(leg, derived=True)

    assert set(reviewed) - set(stored) == {"mid", "spread", "spread_pct"}
    assert set(stored) - set(reviewed) == set()
    for key in stored:
        assert stored[key] == reviewed[key], f"{key} rendered two different ways"


# --- ARCH-C7: the JSON-safety rule, once, at the boundary ----------------


def test_a_dead_book_serializes_as_null_not_infinity():
    """`inf` is not valid JSON, and the payload must stay parseable.

    This was the only real semantic divergence between the two implementations,
    and it survives as a rule about *serialization* rather than as a second
    arithmetic: the domain keeps infinity, the wire gets null.
    """
    leg = proposal_leg("0.00", "0.00")

    payload = leg_payload(leg, derived=True)

    assert payload["spread_pct"] is None
    assert math.isinf(leg.spread_pct), "the domain value is unchanged"
    assert json.loads(json.dumps(payload))["spread_pct"] is None


def test_a_normal_book_carries_a_real_ratio():
    leg = proposal_leg("3.35", "3.45")

    payload = leg_payload(leg, derived=True)

    assert payload["spread_pct"] == pytest.approx(float(leg.spread / leg.mid))


# --- INT-036: the property gains a reader --------------------------------


def test_the_payload_reads_the_spread_property_rather_than_recomputing_it(monkeypatch):
    """`OptionQuote.spread` had exactly one reader: its own sibling property.

    Asserting ``payload["spread"] == str(leg.spread)`` is not enough to show
    that. A serializer writing ``str(leg.ask - leg.bid)`` inline satisfies that
    equality while reading nothing -- the property would still be dead, and this
    test would still be green.

    So the property itself is replaced. A payload that reports the substitute is
    reading it; a payload that reports 0.10 is recomputing behind its back.
    """
    leg = proposal_leg("3.35", "3.45")
    assert leg.spread == Decimal("0.10")

    monkeypatch.setattr(models.Quoted, "spread", property(lambda _self: Decimal("999")))

    assert leg.spread == Decimal("999"), "the substitution itself must take effect"
    assert leg_payload(leg, derived=True)["spread"] == "999"


def test_the_payload_reads_the_mid_property_too(monkeypatch):
    """The same claim for the other derived figure the reviewer is shown.

    Written the same way as the ``spread`` case above, and for the same reason:
    ``payload["mid"] == str(leg.mid)`` is satisfied by an inline recomputation
    and proves nothing about who reads what.
    """
    leg = proposal_leg("3.35", "3.45")
    assert leg.mid == Decimal("3.40")

    monkeypatch.setattr(models.Quoted, "mid", property(lambda _self: Decimal("777")))

    assert leg.mid == Decimal("777"), "the substitution itself must take effect"
    assert leg_payload(leg, derived=True)["mid"] == "777"


# --- INT-029: a field whose name was true only half the time -------------


def test_the_snapshot_carries_no_unread_volatility_figure():
    """``implied_volatility`` had zero readers and a name that lied on fallback.

    Nothing in production or in the tests ever read it: not the reviewer
    payload, not the store schema, not any serializer. It was written at three
    places and read at none. And when the implied-volatility history was
    unusable, the fallback filled it with an annualized *realized* volatility of
    the underlying's closes -- a different quantity under the same name, which
    the method's own docstring admits and the field name does not.

    ``iv_rank``, which *is* read, is unaffected: it is a percentile within
    whichever series was used, and it stays honest either way.
    """
    fields = {f.name for f in dataclasses.fields(models.MarketSnapshot)}

    assert "implied_volatility" not in fields
    assert "iv_rank" in fields, "the figure that is actually read must survive"


def test_the_raw_volatility_figure_is_still_reported_to_the_operator():
    """Removing the field must not remove the number from the record.

    The adapter still computes it and still logs it every pass, so an operator
    reconstructing why a rank looked wrong has the input available -- it just is
    not carried around the system as data nobody consumes.

    Checked against the parsed call, not against substrings of the source. Two
    substring assertions here were tautological: `"implied_volatility,"` is
    contained in `"implied_volatility, iv_rank = self._iv_rank"`, so the second
    could not fail while the first passed, and neither noticed the argument
    being replaced with a constant or `iv=` leaving the format string.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(IBKRMarketData.snapshot)))

    logged = [
        call
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr in {"info", "warning"}
        and call.args
        and isinstance(call.args[0], ast.Constant)
        and "iv=" in call.args[0].value
    ]

    assert logged, "no log line in snapshot() reports a volatility figure"
    names = {arg.id for call in logged for arg in call.args if isinstance(arg, ast.Name)}
    assert "implied_volatility" in names, (
        "the volatility figure is no longer passed to the operator log line"
    )


def test_the_volatility_figure_is_still_computed_where_the_log_line_can_see_it():
    """The name the log line reports must be bound from the real computation.

    Separated from the assertion above because they fail for different reasons:
    this one goes red if the call disappears, that one if the argument does.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(IBKRMarketData.snapshot)))

    bound = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "_iv_rank"
        for element in node.targets
        for target in (element.elts if isinstance(element, ast.Tuple) else [element])
        if isinstance(target, ast.Name)
    }

    assert {"implied_volatility", "iv_rank"} <= bound


# --- INT-016: a venue rejection reached the venue ------------------------


def test_an_order_the_venue_rejected_counts_as_submitted():
    """It reached IBKR. Reporting 'Orders submitted: 0' for it is false.

    ``BROKER_REJECTED`` means the venue saw the order and refused it, which is
    exactly what ``SUBMISSION_FAILED`` does *not* mean -- that one never left.
    """
    assert Outcome.BROKER_REJECTED in models.SUBMITTED_OUTCOMES
    assert Outcome.SUBMISSION_FAILED not in models.SUBMITTED_OUTCOMES


def test_the_operator_line_separates_a_venue_rejection_from_a_failure_to_send(tmp_path):
    """The second half of the same defect: one line counted two different things.

    A venue rejection is a question about the order -- wrong price, wrong
    permissions, a halted underlying. A failure to send is a question about this
    process. Reading a single "Broker rejected: 1" an operator cannot tell which
    happened, and the two need different responses.
    """
    from ibkr_trader.errors import SubmissionFailed

    from .fakes import FakeBroker, StubMarketData, tradable_snapshot
    from .harness import build_runner

    rejected_summary = build_runner(
        tmp_path / "a",
        market=StubMarketData({"AAPL": tradable_snapshot("AAPL")}),
        broker=FakeBroker(outcome=Outcome.BROKER_REJECTED, message="rejected at venue"),
    )[0].run_once()

    unsent_summary = build_runner(
        tmp_path / "b",
        market=StubMarketData({"AAPL": tradable_snapshot("AAPL")}),
        broker=FakeBroker(error=SubmissionFailed("never left the process")),
    )[0].run_once()

    rejected = rejected_summary.render()
    unsent = unsent_summary.render()

    assert "Rejected by venue: 1" in rejected
    assert "Never sent: 0" in rejected
    assert "Orders submitted: 1" in rejected, "it reached the venue"

    assert "Rejected by venue: 0" in unsent
    assert "Never sent: 1" in unsent
    assert "Orders submitted: 0" in unsent, "it never reached the venue"


def _proposal_with(leg: ProposalLeg):
    from ibkr_trader.models import TradeProposal

    from .fakes import SCAN_TIME

    return TradeProposal(
        symbol="AAPL",
        strategy="put_credit_spread",
        expiry=EXPIRY,
        dte=36,
        quantity=1,
        limit_price=Decimal("1.75"),
        max_profit=Decimal("175"),
        max_loss=Decimal("325"),
        underlying_price=Decimal("195.00"),
        iv_rank=45.0,
        short_delta=-0.30,
        buying_power_effect=Decimal("325"),
        legs=(leg,),
        criteria={},
        created_at=SCAN_TIME,
    )
