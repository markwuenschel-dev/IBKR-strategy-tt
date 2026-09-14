"""Phase A: what happens to a spread after it is filled.

Every test drives the production :class:`~ibkr_trader.manager.Manager` against
the production SQLite store, with the same doubles at the edges as the mission
test. The store is seeded with a spread in a given state rather than replayed
through the passes that would have produced it, because the branches under
test are defined by that state: which status, which reference still working,
which legs still held, how many days to expiration.

The invariant the whole module rests on -- no management order ever adds
contracts or width -- is pinned three ways at the end: the pure check, the
order chokepoint, and the roll path that would have tripped it.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from ibkr_trader.clock import FixedClock
from ibkr_trader.errors import BrokerNotConnected, MarketDataError, ReviewTimeout
from ibkr_trader.manager import Manager, RiskIncreaseRefused, added_risk, round_up_to_tick
from ibkr_trader.models import (
    Action,
    ComboLeg,
    ComboOrder,
    ExecutionResult,
    Fill,
    ManagementKind,
    OptionLeg,
    OptionPosition,
    Outcome,
    ProposalLeg,
    Right,
    Spread,
    SpreadStatus,
    SymbolResult,
    Tif,
    TradeProposal,
)
from ibkr_trader.store import SqliteStore

from .fakes import (
    ACCOUNT,
    GOOD_EXPIRY,
    SCAN_TIME,
    FakeBroker,
    StubMarketData,
    StubReviewer,
    quote,
    tradable_snapshot,
)
from .harness import build_manager, build_runner

# --- the spread under management --------------------------------------------
#
# The mission test's own spread: 3x 185/180 puts expiring 2026-03-01 for a 1.75
# credit, opened at SCAN_TIME (45 DTE). MANAGE_TIME is 21 days before expiry,
# the default manage_dte, so the same spread is "held" on one clock and
# "managed" on the other.

SPREAD_ID = "sp-1"
SHORT_LEG = OptionLeg("AAPL", GOOD_EXPIRY, Decimal(185), Right.PUT)
LONG_LEG = OptionLeg("AAPL", GOOD_EXPIRY, Decimal(180), Right.PUT)
MANAGE_TIME = datetime(2026, 2, 8, 14, 30, tzinfo=UTC)
ROLL_EXPIRY = date(2026, 3, 27)  # 47 DTE from MANAGE_TIME: inside the entry band
RUN = "run-1"


def spread(**overrides) -> Spread:
    base = Spread(
        spread_id=SPREAD_ID,
        symbol="AAPL",
        expiry=GOOD_EXPIRY,
        short_strike=Decimal(185),
        long_strike=Decimal(180),
        quantity=3,
        open_credit=Decimal("1.75"),
        opened_at=SCAN_TIME,
        status=SpreadStatus.OPEN,
    )
    return replace(base, **overrides)


def held(
    short: int = -3, long: int = 3, s: Spread | None = None
) -> tuple[OptionPosition, ...]:
    """The two legs as the venue would report them; zero means not held."""
    s = s or spread()
    legs = []
    if short:
        legs.append(OptionPosition(s.short_leg, short))
    if long:
        legs.append(OptionPosition(s.long_leg, long))
    return tuple(legs)


def closing_quotes(
    short: tuple[str, str] = ("0.80", "0.90"), long: tuple[str, str] = ("0.30", "0.40")
) -> dict:
    """Legs quoted at MANAGE_TIME. Defaults net to a 0.50 debit to close."""
    return {
        SHORT_LEG: quote("AAPL", GOOD_EXPIRY, "185", Right.PUT, *short, -0.20),
        LONG_LEG: quote("AAPL", GOOD_EXPIRY, "180", Right.PUT, *long, -0.12),
    }


def roll_snapshot() -> object:
    """The next cycle: the fixture ladder, at ROLL_EXPIRY, quoted at MANAGE_TIME.

    Produces the fixture's 3x 185/180 @ 1.75 again, one cycle out -- a 5-wide
    spread for 1.75 against a 0.50 close is a 1.25 net credit to roll.
    """
    base = tradable_snapshot("AAPL")
    chain = tuple(
        replace(q, expiry=ROLL_EXPIRY) for q in base.chain if q.expiry == GOOD_EXPIRY
    )
    return replace(base, as_of=MANAGE_TIME, chain=chain)


def kinds(summary) -> list[ManagementKind]:
    return [a.kind for a in summary.actions]


def by_purpose(broker: FakeBroker) -> list[str]:
    return [o.purpose for o in broker.placed]


def _leg(action: Action, strike: int) -> ProposalLeg:
    return ProposalLeg(
        action=action,
        right=Right.PUT,
        strike=Decimal(strike),
        expiry=GOOD_EXPIRY,
        ratio=1,
        bid=Decimal("1.00"),
        ask=Decimal("1.10"),
        delta=-0.25,
        open_interest=500,
        volume=100,
    )


def opening_proposal(quantity: int = 3) -> TradeProposal:
    return TradeProposal(
        symbol="AAPL",
        strategy="short_put_vertical",
        expiry=GOOD_EXPIRY,
        dte=45,
        legs=(_leg(Action.SELL, 185), _leg(Action.BUY, 180)),
        quantity=quantity,
        limit_price=Decimal("1.75"),
        max_profit=Decimal(525),
        max_loss=Decimal(975),
        underlying_price=Decimal(195),
        iv_rank=45.0,
        short_delta=-0.30,
        buying_power_effect=Decimal(975),
        criteria={},
        created_at=SCAN_TIME,
        proposal_id="open-1",
    )


# --- 1. reconciliation --------------------------------------------------------


def test_a_filled_opening_order_becomes_a_spread_at_the_fill_price(tmp_path):
    """Both legs held: a spread row at the fill's price, sized to the smaller leg."""
    manager, market, _, _, store = build_manager(
        tmp_path,
        market=StubMarketData(option_positions=held(short=-2, long=3)),
    )
    execution = ExecutionResult(
        Outcome.FILLED,
        "open-1",
        fills=(Fill(3, Decimal("1.80"), SCAN_TIME),),
    )
    store.record(
        SymbolResult("AAPL", Outcome.FILLED, "", opening_proposal(), execution=execution), RUN
    )

    summary = manager.run(RUN)

    # Reconciled, and then managed in the same run: at 45 DTE that is a target.
    assert kinds(summary) == [
        ManagementKind.RECONCILED_FILL,
        ManagementKind.PROFIT_TARGET_PLACED,
    ]
    row = store.spread("open-1")
    assert row is not None
    assert row.status is SpreadStatus.PROFIT_ORDER_RESTING
    assert row.open_credit == Decimal("1.80"), "the fill, not the limit"
    assert row.quantity == 2, "min(|short|, long, order quantity)"
    assert row.opened_at == SCAN_TIME  # the injected clock
    assert store.unreconciled_openings() == (), "reconciled once, not every pass"
    assert store.management_actions(RUN)[0]["kind"] == "RECONCILED_FILL"
    assert summary.actions[1].order.quantity == 2


def test_a_working_order_reconciles_at_its_limit_once_the_legs_appear(tmp_path):
    """Through the runner: pass 1 leaves the order WORKING, pass 2 finds it filled."""
    market = StubMarketData(
        {"AAPL": tradable_snapshot("AAPL")},
        option_positions_per_call=[(), held()],
    )
    runner, _, _, broker, store = build_runner(
        tmp_path, market=market, broker=FakeBroker(outcome=Outcome.WORKING)
    )

    first = runner.run_once()
    assert first.management.actions == ()
    (opening,) = store.unreconciled_openings()
    assert opening.fill_price is None

    # Nothing may be proposed again for a name whose order is now a position.
    market._working_orders["AAPL"] = 3
    second = runner.run_once()

    assert kinds(second.management)[0] is ManagementKind.RECONCILED_FILL
    row = store.spread(broker.submitted[0].proposal_id)
    assert row is not None and row.open_credit == Decimal("1.75")


def _unfilled_opening(tmp_path, clock):
    """An opening order that reached the venue and whose legs are not held."""
    manager, _, _, broker, store = build_manager(
        tmp_path, market=StubMarketData(), clock=clock
    )
    store.record(
        SymbolResult(
            "AAPL",
            Outcome.WORKING,
            "",
            opening_proposal(),
            execution=ExecutionResult(Outcome.WORKING, "open-1"),
        ),
        RUN,
    )
    return manager, broker, store


def test_an_order_whose_legs_are_not_held_is_reported_not_silently_skipped(tmp_path):
    """The order is still live, so nothing is decided -- but the operator is told.

    Previously this emitted nothing at all, which made a resting order that
    never fills indistinguishable from a pass that proposed nothing.
    """
    clock = FixedClock(SCAN_TIME)
    manager, broker, store = _unfilled_opening(tmp_path, clock)
    broker.working_refs = {"open-1"}

    summary = manager.run(RUN)

    assert kinds(summary) == [ManagementKind.OPENING_WORKING]
    assert store.spreads() == []
    assert broker.placed == [], "reporting is not acting"
    assert len(store.unreconciled_openings()) == 1, "still live; keep watching it"


def test_an_opening_order_that_never_filled_is_reported_once_its_session_is_over(tmp_path):
    """A DAY order absent from the book on a later session date is dead.

    This is the signal that tells the operator a screen change produced orders
    that could not fill, rather than no orders at all.
    """
    clock = FixedClock(SCAN_TIME)
    manager, broker, store = _unfilled_opening(tmp_path, clock)
    broker.working_refs = set()
    clock.advance(24 * 3600)

    summary = manager.run(RUN)

    assert kinds(summary) == [ManagementKind.OPENING_UNFILLED]
    assert store.spreads() == []
    assert broker.placed == []
    assert store.unreconciled_openings() == (), "marked, so it is not re-read forever"


def test_an_order_missing_from_the_book_inside_its_own_session_is_not_declared_dead(tmp_path):
    """``working_order_refs()`` sees only this client's orders.

    ``_pending_positions`` reads ``ib.openTrades()``, not ``reqAllOpenOrders``
    (scanner.py:784-791), so a TWS restart or a changed client id empties the
    set while the order is still live at the venue. Declaring it dead on that
    basis would stop its eventual fill from ever being reconciled, so the
    session date has to agree before anything is marked.
    """
    clock = FixedClock(SCAN_TIME)
    manager, broker, store = _unfilled_opening(tmp_path, clock)
    broker.working_refs = set()

    summary = manager.run(RUN)

    assert kinds(summary) == [ManagementKind.OPENING_WORKING]
    assert len(store.unreconciled_openings()) == 1, "never marked on one weak signal"


# --- 2. the profit target -----------------------------------------------------


def test_an_open_spread_gets_a_gtc_buy_back_at_half_the_credit(tmp_path):
    manager, _, _, broker, store = build_manager(
        tmp_path, market=StubMarketData(option_positions=held()), spreads=[spread()]
    )

    summary = manager.run(RUN)

    (order,) = broker.placed
    assert order.purpose == "PROFIT_TARGET"
    assert order.tif is Tif.GTC
    assert order.quantity == 3
    # 1.75 x 0.5 = 0.875, rounded *up* to the tick: a debit of 0.88.
    assert order.limit_price == Decimal("-0.88")
    assert [(leg.leg.strike, leg.action) for leg in order.legs] == [
        (Decimal(185), Action.BUY),
        (Decimal(180), Action.SELL),
    ]
    assert kinds(summary) == [ManagementKind.PROFIT_TARGET_PLACED]
    assert summary.actions[0].order is order
    assert summary.actions[0].execution is not None

    row = store.spread(SPREAD_ID)
    assert row is not None
    assert row.status is SpreadStatus.PROFIT_ORDER_RESTING
    assert row.profit_order_ref == order.order_ref
    recorded = store.management_actions(RUN)
    assert recorded[0]["purpose"] == "PROFIT_TARGET"
    assert recorded[0]["limit_price"] == "-0.88"


def test_a_resting_profit_order_is_not_placed_twice(tmp_path):
    manager, _, _, broker, _ = build_manager(
        tmp_path,
        market=StubMarketData(option_positions=held()),
        broker=FakeBroker(working_refs={"pt-1"}),
        spreads=[spread(status=SpreadStatus.PROFIT_ORDER_RESTING, profit_order_ref="pt-1")],
    )

    summary = manager.run(RUN)

    assert broker.placed == []
    assert summary.actions == ()


def test_a_vanished_profit_order_is_placed_again_while_the_legs_are_held(tmp_path):
    """The GTC is gone from the venue but the spread is not: rest a new one."""
    manager, _, _, broker, store = build_manager(
        tmp_path,
        market=StubMarketData(option_positions=held()),
        broker=FakeBroker(working_refs=set()),
        spreads=[spread(status=SpreadStatus.PROFIT_ORDER_RESTING, profit_order_ref="pt-1")],
    )

    summary = manager.run(RUN)

    (order,) = broker.placed
    assert order.purpose == "PROFIT_TARGET"
    assert order.order_ref != "pt-1"
    assert kinds(summary) == [ManagementKind.PROFIT_TARGET_PLACED]
    assert store.spread(SPREAD_ID).profit_order_ref == order.order_ref


def test_a_refused_profit_order_leaves_the_spread_open_for_the_next_pass(tmp_path):
    manager, _, _, _, store = build_manager(
        tmp_path,
        market=StubMarketData(option_positions=held()),
        broker=FakeBroker(place_outcomes={"PROFIT_TARGET": Outcome.BROKER_REJECTED}),
        spreads=[spread()],
    )

    summary = manager.run(RUN)

    assert kinds(summary) == [ManagementKind.PROFIT_TARGET_PLACED]
    assert summary.actions[0].execution.outcome is Outcome.BROKER_REJECTED
    assert store.spread(SPREAD_ID).status is SpreadStatus.OPEN
    assert store.spread(SPREAD_ID).profit_order_ref is None


def test_legs_gone_with_the_profit_order_no_longer_working_is_a_profit_target_fill(tmp_path):
    manager, _, _, broker, store = build_manager(
        tmp_path,
        market=StubMarketData(option_positions=()),
        broker=FakeBroker(working_refs=set()),
        spreads=[spread(status=SpreadStatus.PROFIT_ORDER_RESTING, profit_order_ref="pt-1")],
    )

    summary = manager.run(RUN)

    assert kinds(summary) == [ManagementKind.CLOSED, ManagementKind.PROFIT_TARGET_FILLED]
    assert broker.placed == [] and broker.cancelled == []
    row = store.spread(SPREAD_ID)
    assert row.status is SpreadStatus.CLOSED
    assert row.close_reason == "profit target filled"
    assert row.closed_at == SCAN_TIME
    assert store.live_spreads() == ()


def test_legs_gone_with_the_profit_order_still_working_cancels_it(tmp_path):
    """Closed some other way. A GTC buy-back left resting would *open* a spread."""
    manager, _, _, broker, store = build_manager(
        tmp_path,
        market=StubMarketData(option_positions=()),
        broker=FakeBroker(working_refs={"pt-1"}),
        spreads=[spread(status=SpreadStatus.PROFIT_ORDER_RESTING, profit_order_ref="pt-1")],
    )

    summary = manager.run(RUN)

    assert broker.cancelled == ["pt-1"]
    assert kinds(summary) == [ManagementKind.PROFIT_TARGET_CANCELLED, ManagementKind.CLOSED]
    assert store.spread(SPREAD_ID).close_reason == "legs no longer held"


# --- 3. at manage_dte ---------------------------------------------------------


def _at_dte(
    tmp_path, *, spreads, quotes=None, broker=None, reviewer=None, snapshots=None, **kw
):
    """A manager at MANAGE_TIME (21 DTE) with the legs held and quoted."""
    market = StubMarketData(
        snapshots or {},
        option_positions=held(),
        quotes=closing_quotes() if quotes is None else quotes,
    )
    return build_manager(
        tmp_path,
        market=market,
        broker=broker,
        reviewer=reviewer,
        clock=FixedClock(MANAGE_TIME),
        spreads=spreads,
        **kw,
    )


def test_at_manage_dte_a_roll_for_a_net_credit_is_taken(tmp_path):
    """Pull the profit order, buy the spread back, sell the next cycle."""
    manager, market, reviewer, broker, store = _at_dte(
        tmp_path,
        spreads=[spread(status=SpreadStatus.PROFIT_ORDER_RESTING, profit_order_ref="pt-1")],
        broker=FakeBroker(working_refs={"pt-1"}),
        snapshots={"AAPL": roll_snapshot()},
    )

    summary = manager.run(RUN)

    assert broker.cancelled == ["pt-1"]
    assert by_purpose(broker) == ["ROLL_CLOSE", "ROLL_OPEN"]
    close, opening = broker.placed

    assert close.tif is Tif.DAY
    assert close.quantity == 3
    assert close.limit_price == Decimal("-0.50")  # (0.85 - 0.35) debit
    assert [(leg.leg.strike, leg.action) for leg in close.legs] == [
        (Decimal(185), Action.BUY),
        (Decimal(180), Action.SELL),
    ]

    # Exactly one review, of the roll that was sent, carrying why it is a roll.
    assert reviewer.call_count == 1
    proposal = reviewer.reviewed[0]
    assert proposal.expiry == ROLL_EXPIRY
    assert "roll" in proposal.criteria
    assert SPREAD_ID in proposal.criteria["roll"]
    assert opening.order_ref == proposal.proposal_id
    assert opening.limit_price == Decimal("1.75")
    assert opening.tif is Tif.DAY
    assert opening.quantity == 3
    assert [(leg.leg.expiry, leg.leg.strike, leg.action) for leg in opening.legs] == [
        (ROLL_EXPIRY, Decimal(185), Action.SELL),
        (ROLL_EXPIRY, Decimal(180), Action.BUY),
    ]

    assert kinds(summary) == [
        ManagementKind.PROFIT_TARGET_CANCELLED,
        ManagementKind.ROLL_CLOSE,
        ManagementKind.CLOSED,
        ManagementKind.ROLL_OPEN,
    ]
    old = store.spread(SPREAD_ID)
    assert old.status is SpreadStatus.CLOSED
    assert old.close_reason == "rolled"
    assert old.close_price == Decimal("0.50")

    # The opening half is recorded like any opening order, so it reconciles
    # the normal way -- and, having filled here, already has its spread row.
    (attempt,) = store.attempts(RUN)
    assert attempt["proposal_id"] == proposal.proposal_id
    assert attempt["outcome"] == Outcome.FILLED.value
    assert store.orders()[0]["order_ref"] == proposal.proposal_id
    assert store.reviews()[0]["approved"] == 1
    new = store.spread(proposal.proposal_id)
    assert new is not None
    assert new.status is SpreadStatus.OPEN
    assert new.expiry == ROLL_EXPIRY
    assert new.open_credit == Decimal("1.75")
    assert new.quantity == 3
    assert store.unreconciled_openings() == ()


def test_a_roll_close_that_rests_is_finished_by_the_next_pass(tmp_path):
    """Close WORKING -> ROLLING; legs gone next pass -> the open half runs fresh."""
    manager, _, reviewer, broker, store = _at_dte(
        tmp_path,
        spreads=[spread()],
        broker=FakeBroker(place_outcomes={"ROLL_CLOSE": Outcome.WORKING}),
        snapshots={"AAPL": roll_snapshot()},
    )

    first = manager.run(RUN)

    assert by_purpose(broker) == ["ROLL_CLOSE"]
    assert kinds(first) == [ManagementKind.ROLL_CLOSE]
    row = store.spread(SPREAD_ID)
    assert row.status is SpreadStatus.ROLLING
    assert row.closing_order_ref == broker.placed[0].order_ref
    assert row.close_price == Decimal("0.50"), "kept so the open half can price the net"
    assert reviewer.call_count == 1

    # Next pass: the legs are gone and the close is no longer working.
    manager2, _, reviewer2, broker2, store2 = build_manager(
        tmp_path / "pass2",
        market=StubMarketData({"AAPL": roll_snapshot()}, option_positions=()),
        clock=FixedClock(MANAGE_TIME),
        spreads=[row],
    )
    second = manager2.run("run-2")

    assert kinds(second) == [ManagementKind.CLOSED, ManagementKind.ROLL_OPEN]
    assert store2.spread(SPREAD_ID).close_reason == "roll close filled"
    assert by_purpose(broker2) == ["ROLL_OPEN"]
    # The old proposal is stale: it was evaluated and reviewed again.
    assert reviewer2.call_count == 1
    assert broker2.placed[0].order_ref == reviewer2.reviewed[0].proposal_id


def test_a_roll_that_nets_no_credit_is_declined_and_the_spread_is_closed(tmp_path):
    """Close at 1.80 (rounded up from 1.795) against a 1.75 next cycle: net -0.05."""
    manager, _, reviewer, broker, store = _at_dte(
        tmp_path,
        spreads=[spread()],
        quotes=closing_quotes(short=("2.39", "2.50"), long=("0.60", "0.70")),
        broker=FakeBroker(place_outcomes={"CLOSE": Outcome.WORKING}),
        snapshots={"AAPL": roll_snapshot()},
    )

    summary = manager.run(RUN)

    assert kinds(summary) == [ManagementKind.ROLL_DECLINED, ManagementKind.CLOSE]
    declined = summary.actions[0].detail
    assert "net -0.05" in declined
    assert reviewer.call_count == 0, "a roll that cannot pay is never reviewed"

    (close,) = broker.placed
    assert close.purpose == "CLOSE"
    assert close.tif is Tif.DAY
    assert close.quantity == 3
    assert close.limit_price == Decimal("-1.80")  # 2.445 - 0.65 = 1.795, rounded up
    row = store.spread(SPREAD_ID)
    assert row.status is SpreadStatus.CLOSING
    assert row.closing_order_ref == close.order_ref


def test_a_roll_the_reviewer_rejects_is_declined_and_the_spread_is_closed(tmp_path):
    manager, _, reviewer, broker, store = _at_dte(
        tmp_path,
        spreads=[spread()],
        reviewer=StubReviewer(approved=False, reason="next cycle too rich"),
        snapshots={"AAPL": roll_snapshot()},
    )

    summary = manager.run(RUN)

    assert kinds(summary) == [
        ManagementKind.ROLL_DECLINED,
        ManagementKind.CLOSE,
        ManagementKind.CLOSED,
    ]
    assert "next cycle too rich" in summary.actions[0].detail
    assert reviewer.call_count == 1
    assert by_purpose(broker) == ["CLOSE"]
    assert store.spread(SPREAD_ID).close_reason == "closed"
    assert store.spread(SPREAD_ID).close_price == Decimal("0.50")


def test_a_review_that_fails_is_a_declined_roll_never_an_approval(tmp_path):
    manager, _, _, broker, _ = _at_dte(
        tmp_path,
        spreads=[spread()],
        reviewer=StubReviewer(error=ReviewTimeout("no answer in 90s")),
        snapshots={"AAPL": roll_snapshot()},
    )

    summary = manager.run(RUN)

    assert kinds(summary)[0] is ManagementKind.ROLL_DECLINED
    assert "ReviewTimeout" in summary.actions[0].detail
    assert by_purpose(broker) == ["CLOSE"]


def test_rolling_is_not_attempted_when_disabled(tmp_path):
    manager, market, reviewer, broker, _ = _at_dte(
        tmp_path,
        spreads=[spread()],
        snapshots={"AAPL": roll_snapshot()},
        overrides={"management": {"roll": False}},
    )

    summary = manager.run(RUN)

    assert market.requested == [], "no chain is scanned for a roll nobody asked for"
    assert reviewer.call_count == 0
    assert kinds(summary)[:2] == [ManagementKind.ROLL_DECLINED, ManagementKind.CLOSE]
    assert "disabled" in summary.actions[0].detail
    assert by_purpose(broker) == ["CLOSE"]


def test_a_dead_book_on_either_leg_is_a_decision_for_a_human(tmp_path):
    manager, _, reviewer, broker, store = _at_dte(
        tmp_path,
        spreads=[spread()],
        quotes=closing_quotes(long=("0", "0")),
        snapshots={"AAPL": roll_snapshot()},
    )

    summary = manager.run(RUN)

    assert kinds(summary) == [ManagementKind.NEEDS_DECISION]
    assert "180P" in summary.actions[0].detail
    assert broker.placed == []
    assert reviewer.call_count == 0
    assert store.spread(SPREAD_ID).status is SpreadStatus.NEEDS_DECISION


def test_a_spread_awaiting_a_decision_is_not_touched_again(tmp_path):
    manager, market, _, broker, _ = _at_dte(
        tmp_path, spreads=[spread(status=SpreadStatus.NEEDS_DECISION)]
    )

    summary = manager.run(RUN)

    assert summary.actions == ()
    assert broker.placed == []
    assert market.quoted == []


def test_a_lapsed_day_close_is_placed_again(tmp_path):
    """CLOSING, legs held, reference no longer working: the branch runs again."""
    manager, _, _, broker, store = _at_dte(
        tmp_path,
        spreads=[spread(status=SpreadStatus.CLOSING, closing_order_ref="old-close")],
        broker=FakeBroker(working_refs=set(), place_outcomes={"CLOSE": Outcome.WORKING}),
        overrides={"management": {"roll": False}},
    )

    summary = manager.run(RUN)

    assert by_purpose(broker) == ["CLOSE"]
    assert kinds(summary)[-1] is ManagementKind.CLOSE
    row = store.spread(SPREAD_ID)
    assert row.status is SpreadStatus.CLOSING
    assert row.closing_order_ref == broker.placed[0].order_ref != "old-close"


def test_a_close_still_working_is_left_to_work(tmp_path):
    manager, market, _, broker, _ = _at_dte(
        tmp_path,
        spreads=[spread(status=SpreadStatus.CLOSING, closing_order_ref="c-1")],
        broker=FakeBroker(working_refs={"c-1"}),
    )

    summary = manager.run(RUN)

    assert summary.actions == ()
    assert broker.placed == []
    assert market.quoted == []


def test_a_profit_order_that_cannot_be_cancelled_stops_the_spread_for_this_pass(tmp_path):
    """It was working a moment ago: it has most likely just filled. Do not close on top."""
    manager, _, _, broker, store = _at_dte(
        tmp_path,
        spreads=[spread(status=SpreadStatus.PROFIT_ORDER_RESTING, profit_order_ref="pt-1")],
        broker=FakeBroker(working_refs={"pt-1"}, cancel_result=False),
        snapshots={"AAPL": roll_snapshot()},
    )

    summary = manager.run(RUN)

    assert kinds(summary) == [ManagementKind.PROFIT_TARGET_CANCELLED]
    assert "cancelled=False" in summary.actions[0].detail
    assert broker.placed == []
    assert store.spread(SPREAD_ID).status is SpreadStatus.PROFIT_ORDER_RESTING


# --- 4. strays, isolation, the run -------------------------------------------


def test_a_held_leg_belonging_to_no_spread_is_reported_and_left_alone(tmp_path):
    stray = OptionLeg("MSFT", GOOD_EXPIRY, Decimal(400), Right.PUT)
    manager, _, _, broker, store = build_manager(
        tmp_path,
        market=StubMarketData(option_positions=held() + (OptionPosition(stray, -2),)),
        spreads=[spread(status=SpreadStatus.PROFIT_ORDER_RESTING, profit_order_ref="pt-1")],
        broker=FakeBroker(working_refs={"pt-1"}),
    )

    summary = manager.run(RUN)

    assert kinds(summary) == [ManagementKind.UNMANAGED_POSITION]
    action = summary.actions[0]
    assert action.symbol == "MSFT"
    assert action.spread_id is None
    assert "400P x-2" in action.detail
    assert broker.placed == []
    assert store.management_actions(RUN)[0]["symbol"] == "MSFT"


def test_a_failure_on_one_spread_does_not_stop_the_next(tmp_path):
    """MSFT (opened first, so processed first) cannot be quoted; AAPL is still closed."""
    msft = spread(
        opened_at=SCAN_TIME - timedelta(days=1),
        spread_id="sp-msft",
        symbol="MSFT",
        short_strike=Decimal(400),
        long_strike=Decimal(395),
    )
    market = StubMarketData(
        option_positions=held() + held(s=msft),
        quotes=closing_quotes(),  # nothing for MSFT
    )
    manager, _, _, broker, store = build_manager(
        tmp_path,
        market=market,
        clock=FixedClock(MANAGE_TIME),
        spreads=[msft, spread()],
        overrides={"management": {"roll": False}},
    )

    summary = manager.run(RUN)

    assert kinds(summary)[0] is ManagementKind.ERROR
    assert summary.actions[0].symbol == "MSFT"
    assert summary.actions[0].spread_id == "sp-msft"
    assert "MarketDataError" in summary.actions[0].detail
    assert [a.symbol for a in summary.actions[1:]] == ["AAPL", "AAPL", "AAPL"]
    assert by_purpose(broker) == ["CLOSE"]
    assert broker.placed[0].symbol == "AAPL"
    assert summary.errors == 1
    assert store.management_actions(RUN)[0]["kind"] == "ERROR"


def test_an_interrupt_mid_placement_is_recorded_before_it_escapes(tmp_path):
    """Ctrl-C inside a management order: record that one may be live, then unwind.

    ``KeyboardInterrupt`` is not an ``Exception``, so the per-spread boundary
    cannot absorb it -- and must not. What it must do is leave a trace: the
    order may already be at the venue, and the next pass reconciles from the
    record. Same rule as the runner's interrupted submission.
    """
    broker = FakeBroker(place_outcomes={"PROFIT_TARGET": KeyboardInterrupt()})
    manager, _, _, broker, store = build_manager(
        tmp_path,
        market=StubMarketData(option_positions=held()),
        broker=broker,
        spreads=[spread()],
    )

    with pytest.raises(KeyboardInterrupt):
        manager.run(RUN)

    (recorded,) = store.management_actions(RUN)
    assert recorded["kind"] == ManagementKind.ERROR.value
    assert recorded["spread_id"] == SPREAD_ID
    assert "KeyboardInterrupt" in recorded["detail"]
    assert "may be live at the venue" in recorded["detail"]


def test_a_failure_to_record_is_logged_and_the_action_still_happens(tmp_path, caplog):
    class DeafStore(SqliteStore):
        def record_management(self, _action, _run_id):
            raise RuntimeError("disk full")

    market = StubMarketData(option_positions=held())
    store = DeafStore(tmp_path / "deaf.sqlite3", clock=FixedClock(SCAN_TIME))
    store.record_spread(spread())
    from ibkr_trader.config import build_config

    config = build_config({"universe": ["AAPL"], "ibkr": {"account": ACCOUNT}})
    broker = FakeBroker()
    manager = Manager(config, market, StubReviewer(), broker, store, FixedClock(SCAN_TIME))

    summary = manager.run(RUN)

    assert by_purpose(broker) == ["PROFIT_TARGET"]
    assert kinds(summary) == [ManagementKind.PROFIT_TARGET_PLACED]
    assert any(
        "failed to record PROFIT_TARGET_PLACED" in r.getMessage() for r in caplog.records
    )


def test_the_summary_counts_are_derived_from_the_actions(tmp_path):
    manager, _, _, _, _ = build_manager(
        tmp_path, market=StubMarketData(option_positions=held()), spreads=[spread()]
    )

    rendered = manager.run(RUN).render()

    assert rendered.splitlines()[0] == "Managed: 1 spread(s), 1 action(s)"
    assert "Profit targets placed: 1" in rendered
    assert "Closes sent" not in rendered


# --- 5. the invariant: never more contracts, never wider ----------------------


def test_added_risk_is_the_named_check():
    s = spread()
    assert added_risk(s, 3, Decimal(5)) is None
    assert added_risk(s, 1, Decimal(2)) is None
    assert "exceeds the 3 contract(s)" in added_risk(s, 4, Decimal(5))
    assert "exceeds the 5-wide spread" in added_risk(s, 3, Decimal("7.5"))
    assert "not a positive" in added_risk(s, 0, Decimal(5))


def test_the_order_chokepoint_refuses_an_order_that_adds_risk(tmp_path):
    manager, _, _, broker, _ = build_manager(tmp_path, market=StubMarketData())
    too_many = ComboOrder(
        symbol="AAPL",
        legs=(ComboLeg(SHORT_LEG, Action.BUY), ComboLeg(LONG_LEG, Action.SELL)),
        quantity=4,
        limit_price=Decimal("-0.88"),
        tif=Tif.GTC,
        order_ref="x",
        purpose="PROFIT_TARGET",
    )

    with pytest.raises(RiskIncreaseRefused, match="quantity 4 exceeds"):
        manager._place(spread(), too_many)

    assert broker.placed == [], "nothing reached the broker"


def test_a_roll_into_a_wider_spread_is_declined_before_anything_is_sent(tmp_path):
    """The next cycle selects 185/180 (5 wide); the spread held is 185/183 (2 wide)."""
    narrow = spread(long_strike=Decimal(183))
    narrow_long = OptionLeg("AAPL", GOOD_EXPIRY, Decimal(183), Right.PUT)
    quotes = {
        SHORT_LEG: quote("AAPL", GOOD_EXPIRY, "185", Right.PUT, "0.80", "0.90", -0.20),
        narrow_long: quote("AAPL", GOOD_EXPIRY, "183", Right.PUT, "0.50", "0.60", -0.15),
    }
    market = StubMarketData(
        {"AAPL": roll_snapshot()}, option_positions=held(s=narrow), quotes=quotes
    )
    manager, _, reviewer, broker, _ = build_manager(
        tmp_path, market=market, clock=FixedClock(MANAGE_TIME), spreads=[narrow]
    )

    summary = manager.run(RUN)

    assert kinds(summary)[0] is ManagementKind.ROLL_DECLINED
    assert "width 5 exceeds the 2-wide spread" in summary.actions[0].detail
    assert reviewer.call_count == 0
    assert by_purpose(broker) == ["CLOSE"]
    assert broker.placed[0].quantity == 3


def test_a_close_is_sized_to_what_the_venue_says_is_held(tmp_path):
    """The record says 3; the account holds 2. Buying back 3 would open a long spread."""
    manager, _, _, broker, _ = _at_dte(
        tmp_path,
        spreads=[spread()],
        overrides={"management": {"roll": False}},
    )
    manager._market_data._option_positions = held(short=-2, long=2)

    manager.run(RUN)

    assert by_purpose(broker) == ["CLOSE"]
    assert broker.placed[0].quantity == 2


def test_debits_round_up_to_the_tick():
    assert round_up_to_tick(Decimal("0.875")) == Decimal("0.88")
    assert round_up_to_tick(Decimal("0.501")) == Decimal("0.51")
    assert round_up_to_tick(Decimal("0.50")) == Decimal("0.50")


# --- 6. the runner: phase A before B, guarded --------------------------------


def test_the_book_is_managed_before_any_symbol_is_quoted(tmp_path):
    events: list[str] = []

    class ObservedMarket(StubMarketData):
        def option_positions(self):
            events.append("positions")
            return super().option_positions()

        def snapshot(self, symbol):
            events.append("scan")
            return super().snapshot(symbol)

    class ObservedStore(SqliteStore):
        def start_run(self, *args, **kwargs):
            events.append("run-record")
            return super().start_run(*args, **kwargs)

    market = ObservedMarket({"AAPL": tradable_snapshot("AAPL")}, option_positions=held())
    store = ObservedStore(tmp_path / "trader.sqlite3", clock=FixedClock(SCAN_TIME))
    store.record_spread(spread())
    runner, _, _, broker, _ = build_runner(tmp_path, market=market, store=store)

    summary = runner.run_once()

    assert events[:3] == ["run-record", "positions", "scan"]
    assert kinds(summary.management) == [ManagementKind.PROFIT_TARGET_PLACED]
    assert by_purpose(broker) == ["PROFIT_TARGET"]
    # Management lines come first in the operator summary.
    assert summary.render().splitlines()[0].startswith("Managed: 1 spread(s)")
    assert "Scanned: 1" in summary.render()
    assert store.management_actions(summary.run_id)[0]["kind"] == "PROFIT_TARGET_PLACED"


def test_a_management_failure_before_any_spread_does_not_stop_the_scan(tmp_path):
    market = StubMarketData(
        {"AAPL": tradable_snapshot("AAPL")},
        option_positions_error=MarketDataError("position stream unavailable"),
    )
    runner, _, _, broker, store = build_runner(tmp_path, market=market)

    summary = runner.run_once()

    (action,) = summary.management.actions
    assert action.kind is ManagementKind.ERROR
    assert action.symbol == "*"
    assert "position stream unavailable" in action.detail
    assert store.management_actions(summary.run_id)[0]["kind"] == "ERROR"
    # Phase B still ran, and traded.
    assert summary.results[0].outcome is Outcome.FILLED
    assert broker.call_count == 1
    assert "Management errors: 1" in summary.render()


def test_manage_once_records_its_own_run_and_scans_nothing(tmp_path):
    runner, market, reviewer, broker, store = build_runner(
        tmp_path, market=StubMarketData(option_positions=held()), store=None
    )
    store.record_spread(spread())

    summary = runner.manage_once()

    assert market.requested == []
    assert reviewer.call_count == 0
    assert broker.submitted == []
    assert by_purpose(broker) == ["PROFIT_TARGET"]
    (run,) = store.runs()
    assert run["run_id"] == summary.run_id
    assert run["verified_account"] == ACCOUNT
    assert store.management_actions(summary.run_id)[0]["kind"] == "PROFIT_TARGET_PLACED"


def test_manage_once_refuses_without_a_verified_account(tmp_path):
    runner, _, _, broker, store = build_runner(
        tmp_path,
        market=StubMarketData(option_positions=held()),
        broker=FakeBroker(verified_account=None),
    )

    with pytest.raises(BrokerNotConnected):
        runner.manage_once()

    assert store.runs() == []
    assert broker.placed == []
