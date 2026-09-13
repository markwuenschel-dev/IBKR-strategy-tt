"""The management surface of the IBKR adapter: ``place``, ``cancel``, ``working_order_refs``.

``test_broker_encoding`` pins what an *opening* proposal puts on the wire.
This module pins the other three things the ``Broker`` port now asks for: a
management ``ComboOrder`` goes through the same encoder (bought bag, sign on
the price, ``orderRef`` stamped, and now ``tif`` carried), a reference can be
cancelled wherever the venue is holding it, and the set of working references
is read from the venue rather than from memory.

Everything runs through the real adapter with the same injected ``api``/``ib``
seams the encoding tests use, so ``ib_async`` is never imported.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from ibkr_trader.broker import OPENING_PURPOSE, IBKRBroker
from ibkr_trader.clock import FixedClock
from ibkr_trader.errors import BrokerError, BrokerNotConnected
from ibkr_trader.models import Action, ComboLeg, ComboOrder, OptionLeg, Outcome, Right, Tif

from .fakes import ACCOUNT, SCAN_TIME
from .test_broker_encoding import FAKE_API, FakeIB, canonical_proposal, make_broker

EXPIRY = date(2026, 3, 20)


def put_leg(strike: str, action: Action) -> ComboLeg:
    return ComboLeg(
        leg=OptionLeg(symbol="AAPL", expiry=EXPIRY, strike=Decimal(strike), right=Right.PUT),
        action=action,
    )


def profit_target() -> ComboOrder:
    """Close a short 185/180 put vertical for a 0.88 debit, resting GTC.

    The mirror of the opening trade in ``test_broker_encoding``: the short 185
    is bought back, the long 180 is sold, and the package costs money.
    """
    return ComboOrder(
        symbol="AAPL",
        legs=(put_leg("185", Action.BUY), put_leg("180", Action.SELL)),
        quantity=3,
        limit_price=Decimal("-0.88"),
        tif=Tif.GTC,
        order_ref="spread-1:PROFIT_TARGET",
        purpose="PROFIT_TARGET",
    )


class ManagementIB(FakeIB):
    """``FakeIB`` plus the two open-order streams and ``cancelOrder``.

    ``own`` is what ``openTrades()`` reports (this client's orders); ``every``
    is what ``reqAllOpenOrders()`` reports (all clients'). They are separate so
    a test can put a reference in exactly one of them.
    """

    def __init__(
        self, own=(), every=(), connected: bool = True, status: str = "Filled"
    ) -> None:
        super().__init__(status=status, connected=connected)
        self._own = list(own)
        self._every = list(every)
        self.cancelled: list = []
        self.all_open_requests = 0
        self.stream_error: Exception | None = None

    def openTrades(self):
        if self.stream_error is not None:
            raise self.stream_error
        return list(self._own)

    def reqAllOpenOrders(self):
        if self.stream_error is not None:
            raise self.stream_error
        self.all_open_requests += 1
        return list(self._every)

    def cancelOrder(self, order):
        self.cancelled.append(order)


def working(ref: str, status: str = "Submitted", account: str = ACCOUNT):
    """A venue trade whose order carries ``ref``, as ib_async shapes it."""
    return SimpleNamespace(
        order=SimpleNamespace(orderRef=ref, account=account, orderId=len(ref)),
        orderStatus=SimpleNamespace(status=status),
    )


def management_broker(ib: ManagementIB) -> IBKRBroker:
    config, _ = canonical_proposal()
    return IBKRBroker(config.ibkr, FixedClock(SCAN_TIME), ib=ib, api=FAKE_API)


# --- place() ---------------------------------------------------------------


def test_a_profit_target_is_a_bought_bag_at_a_positive_debit_resting_gtc():
    """The same encoder as the opening trade, with the sign flipped by the price.

    A closing debit of 0.88 is a positive price to *buy* the bag; the bag side
    never changes. ``tif`` now travels from the order rather than being fixed
    at ``DAY``, because the profit target must outlive the session.
    """
    ib = ManagementIB()
    broker = management_broker(ib)

    result = broker.place(profit_target())

    contract, order = ib.placed[0]
    assert order.action == "BUY"
    assert order.lmtPrice == 0.88, "a 0.88 debit is a +0.88 price to buy the bag"
    assert order.totalQuantity == 3.0
    assert order.tif == "GTC"
    assert order.orderRef == "spread-1:PROFIT_TARGET"
    assert order.account == ACCOUNT
    assert result.order_ref == "spread-1:PROFIT_TARGET"

    assert contract.secType == "BAG"
    legs = {leg.conId: leg for leg in contract.comboLegs}
    assert legs[185].action == "BUY", "the short 185 put is bought back"
    assert legs[180].action == "SELL", "the long 180 put is sold"


def test_a_debit_fill_is_reported_negative_in_the_domain_sign():
    """``Fill.price`` follows ``ComboOrder.is_credit``: paid money is negative."""
    ib = ManagementIB(status="Filled")  # FakeIB reports avgFillPrice 1.75
    broker = management_broker(ib)

    result = broker.place(profit_target())

    assert result.outcome is Outcome.FILLED
    assert result.fills[0].price == Decimal("-1.75")


def test_submit_still_encodes_the_opening_credit_exactly_as_before():
    """``submit`` is now ``place`` of an opening order; nothing on the wire moved.

    Asserted through the encoding suite's own fake so the two suites cannot
    drift: this is the payload ``test_broker_encoding`` pins, plus the two
    fields the refactor added to the path (``tif`` and the purpose).
    """
    ib = FakeIB()
    broker, proposal = make_broker(ib)

    result = broker.submit(proposal)

    _, order = ib.placed[0]
    assert order.action == "BUY"
    assert order.lmtPrice == -1.75
    assert order.tif == "DAY", "an opening order is a day order"
    assert order.orderRef == proposal.proposal_id
    assert result.order_ref == proposal.proposal_id
    assert OPENING_PURPOSE == "OPEN"


def test_place_refuses_when_disconnected_and_sends_nothing():
    ib = ManagementIB(connected=False)
    broker = management_broker(ib)

    with pytest.raises(BrokerNotConnected):
        broker.place(profit_target())
    assert ib.placed == []


# --- cancel() --------------------------------------------------------------


def test_cancel_finds_the_reference_among_this_clients_open_trades():
    """The cheap path: our own order, no round trip to the venue needed."""
    target = working("spread-1:PROFIT_TARGET")
    ib = ManagementIB(own=[target], every=[])
    broker = management_broker(ib)

    assert broker.cancel("spread-1:PROFIT_TARGET") is True
    assert ib.cancelled == [target.order]
    assert ib.all_open_requests == 0, "openTrades answered; reqAllOpenOrders not needed"


def test_cancel_finds_a_reference_placed_by_an_earlier_process():
    """After a restart the order is not in ``openTrades``; the venue still has it."""
    target = working("spread-1:PROFIT_TARGET")
    ib = ManagementIB(own=[], every=[target])
    broker = management_broker(ib)

    assert broker.cancel("spread-1:PROFIT_TARGET") is True
    assert ib.cancelled == [target.order]
    assert ib.all_open_requests == 1


def test_cancel_reports_false_when_no_working_order_carries_the_reference():
    """Absent, already done, or another account's: none of them is ours to cancel."""
    ib = ManagementIB(
        own=[working("someone-else")],
        every=[
            working("spread-1:PROFIT_TARGET", status="Filled"),
            working("spread-2:PROFIT_TARGET", account="DU9999999"),
        ],
    )
    broker = management_broker(ib)

    assert broker.cancel("spread-1:PROFIT_TARGET") is False, "a filled order is not working"
    assert broker.cancel("spread-2:PROFIT_TARGET") is False, "another book's order"
    assert broker.cancel("never-existed") is False
    assert ib.cancelled == []


def test_cancel_refuses_when_disconnected():
    ib = ManagementIB(own=[working("spread-1:PROFIT_TARGET")], connected=False)
    broker = management_broker(ib)

    with pytest.raises(BrokerNotConnected):
        broker.cancel("spread-1:PROFIT_TARGET")
    assert ib.cancelled == []


def test_an_unreadable_order_stream_is_a_broker_error_not_a_false():
    """False means "nothing to cancel"; a failed read means "cannot tell"."""
    ib = ManagementIB()
    ib.stream_error = RuntimeError("order stream unavailable")
    broker = management_broker(ib)

    with pytest.raises(BrokerError, match="open-order stream"):
        broker.cancel("spread-1:PROFIT_TARGET")


def test_a_cancel_that_cannot_be_transmitted_is_a_broker_error():
    class RefusesCancel(ManagementIB):
        def cancelOrder(self, _order):
            raise RuntimeError("socket closed")

    ib = RefusesCancel(own=[working("spread-1:PROFIT_TARGET")])
    broker = management_broker(ib)

    with pytest.raises(BrokerError, match="could not transmit a cancel"):
        broker.cancel("spread-1:PROFIT_TARGET")


# --- working_order_refs() --------------------------------------------------


def test_working_refs_are_read_from_every_client_for_this_account_only():
    """Filters: other accounts out, done orders out, empty refs out."""
    ib = ManagementIB(
        own=[working("only-in-open-trades")],
        every=[
            working("spread-1:PROFIT_TARGET"),
            working("spread-2:PROFIT_TARGET", account=""),
            working("spread-3:PROFIT_TARGET", account="DU9999999"),
            working("spread-4:PROFIT_TARGET", status="Cancelled"),
            working(""),
            working("manual-order-from-tws", status="PreSubmitted"),
        ],
    )
    broker = management_broker(ib)

    refs = broker.working_order_refs()

    assert refs == frozenset(
        {"spread-1:PROFIT_TARGET", "spread-2:PROFIT_TARGET", "manual-order-from-tws"}
    )
    assert "only-in-open-trades" not in refs, "the venue view is authoritative, not ours"
    assert isinstance(refs, frozenset)


def test_working_refs_refuse_when_disconnected():
    broker = management_broker(ManagementIB(connected=False))

    with pytest.raises(BrokerNotConnected):
        broker.working_order_refs()


def test_working_refs_report_a_failed_read_as_a_broker_error():
    """An empty set would read as "nothing resting" and re-place every target."""
    ib = ManagementIB()
    ib.stream_error = RuntimeError("order stream unavailable")
    broker = management_broker(ib)

    with pytest.raises(BrokerError, match="reqAllOpenOrders"):
        broker.working_order_refs()
