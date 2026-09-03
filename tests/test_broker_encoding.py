"""What the IBKR adapter actually puts on the wire.

The leg/side/price encoding of a combo order is the one place in this system
where a plausible-looking bug silently inverts the position: a put *credit*
spread and a put *debit* spread differ only by which leg is sold. These tests
pin the exact payload so that changing the convention requires deliberately
rewriting an assertion that spells out the intended trade in full.

They exercise the real adapter through its injected ``api``/``ib`` seams, so
they run with ``ib_async`` absent. They cannot prove TWS *accepts* the encoding
— only a live paper session can do that — but they do prove the adapter emits
the encoding it claims to.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from ibkr_trader.broker import IBKRBroker
from ibkr_trader.clock import FixedClock
from ibkr_trader.config import build_config
from ibkr_trader.errors import ExecutionAmbiguous, SubmissionFailed
from ibkr_trader.models import NoTrade, Outcome, Portfolio
from ibkr_trader.tastytrade import evaluate

from .fakes import SCAN_TIME, tradable_snapshot

# --- a fake ib_async module surface --------------------------------------


def _limit_order(action, totalQuantity, lmtPrice, **kwargs):
    return SimpleNamespace(
        action=action,
        totalQuantity=totalQuantity,
        lmtPrice=lmtPrice,
        orderRef=None,
        tif=None,
        account=None,
        orderId=77,
        **kwargs,
    )


FAKE_API = SimpleNamespace(
    Contract=lambda **kw: SimpleNamespace(**kw),
    ComboLeg=lambda **kw: SimpleNamespace(**kw),
    Option=lambda **kw: SimpleNamespace(**kw),
    LimitOrder=_limit_order,
    IB=None,
)


class FakeIB:
    """Minimal stand-in for the ``IB`` client.

    ``qualifyContracts`` assigns each option a ``conId`` equal to its strike, so
    a ``ComboLeg``'s ``conId`` names the strike it refers to and the assertions
    below can read as the trade itself.
    """

    def __init__(self, status: str = "Filled", connected: bool = True) -> None:
        self._status = status
        self._connected = connected
        self.placed: list[tuple] = []
        self.qualify_error: Exception | None = None

    def isConnected(self) -> bool:
        return self._connected

    def disconnect(self) -> None:
        self._connected = False

    def qualifyContracts(self, *contracts):
        if self.qualify_error is not None:
            raise self.qualify_error
        for contract in contracts:
            contract.conId = int(contract.strike)
        return list(contracts)

    def placeOrder(self, contract, order):
        self.placed.append((contract, order))
        return SimpleNamespace(
            contract=contract,
            order=order,
            orderStatus=SimpleNamespace(
                status=self._status, filled=3, avgFillPrice=1.75, orderId=77
            ),
            fills=[],
            log=[
                SimpleNamespace(
                    status=self._status,
                    errorCode=201,
                    message="Order rejected - reason:201 insufficient margin",
                )
            ],
            advancedError="",
        )

    def waitOnUpdate(self, timeout: float = 0) -> bool:
        return True


def canonical_proposal():
    """The mission-test proposal, produced by the real algorithm."""
    config = build_config({"universe": ["AAPL"]})
    proposal = evaluate(
        symbol="AAPL",
        snapshot=tradable_snapshot("AAPL"),
        portfolio=Portfolio(net_liquidation=Decimal(50_000), buying_power=Decimal(25_000)),
        strategy=config.strategy,
        risk=config.risk,
        now=SCAN_TIME,
    )
    assert not isinstance(proposal, NoTrade), proposal
    return config, proposal


def make_broker(ib: FakeIB):
    config, proposal = canonical_proposal()
    broker = IBKRBroker(config.ibkr, FixedClock(SCAN_TIME), ib=ib, api=FAKE_API)
    return broker, proposal


# --- the encoding ---------------------------------------------------------


def test_credit_spread_is_a_bought_bag_at_a_negative_limit():
    """A collected credit is expressed as a negative price, not as a sell."""
    ib = FakeIB()
    broker, proposal = make_broker(ib)

    broker.submit(proposal)

    _, order = ib.placed[0]
    assert order.action == "BUY"
    assert order.lmtPrice == -1.75, "a 1.75 credit is a -1.75 price to buy the bag"
    assert order.totalQuantity == 3.0


def test_combo_legs_read_literally_as_the_reviewed_trade():
    """The wire payload must say: sell the 185 put, buy the 180 put.

    This is the assertion that makes the position auditable. If it is ever
    inverted, the order fills as a debit spread and loses money in the shape of
    a trade nobody approved.
    """
    ib = FakeIB()
    broker, proposal = make_broker(ib)

    broker.submit(proposal)

    contract, _ = ib.placed[0]
    assert contract.secType == "BAG"
    assert contract.symbol == "AAPL"

    legs = {leg.conId: leg for leg in contract.comboLegs}
    assert set(legs) == {185, 180}
    assert legs[185].action == "SELL", "the 185 put is the short strike"
    assert legs[180].action == "BUY", "the 180 put is the long (protective) strike"
    assert legs[185].ratio == 1 and legs[180].ratio == 1


def test_durable_identity_is_stamped_before_transmission():
    """``orderRef`` is the only stamp that survives a dropped connection."""
    ib = FakeIB()
    broker, proposal = make_broker(ib)

    result = broker.submit(proposal)

    _, order = ib.placed[0]
    assert order.orderRef == proposal.proposal_id
    assert result.order_ref == proposal.proposal_id


def test_fill_price_is_reported_in_the_domain_sign():
    """A credit fill is positive in our vocabulary, matching ``limit_price``."""
    ib = FakeIB(status="Filled")
    broker, proposal = make_broker(ib)

    result = broker.submit(proposal)

    assert result.outcome is Outcome.FILLED
    assert result.filled_quantity == 3, "one package fill, not one per leg"
    assert result.fills[0].price == Decimal("1.75")


# --- failure translation --------------------------------------------------


def test_rejection_preserves_the_brokers_own_text():
    ib = FakeIB(status="Inactive")
    broker, proposal = make_broker(ib)

    result = broker.submit(proposal)

    assert result.outcome is Outcome.BROKER_REJECTED
    assert result.message == "Order rejected - reason:201 insufficient margin", (
        "the venue's own text must survive verbatim"
    )
    assert result.order_ref == proposal.proposal_id


def test_unqualifiable_leg_fails_before_anything_is_sent():
    """If a leg cannot be resolved, nothing may be transmitted."""
    ib = FakeIB()
    ib.qualify_error = RuntimeError("no security definition found")
    broker, proposal = make_broker(ib)

    with pytest.raises(SubmissionFailed):
        broker.submit(proposal)
    assert ib.placed == [], "nothing may reach the venue after a qualify failure"


def test_connection_lost_after_transmission_is_ambiguous_not_failed():
    """Losing the connection mid-flight is unknown, not known-failed.

    Reporting this as a failure would be a lie that could double-submit; the
    ambiguity is carried on the order's own reference.
    """

    class DropsAfterPlacing(FakeIB):
        def placeOrder(self, contract, order):
            trade = super().placeOrder(contract, order)
            self._connected = False
            trade.orderStatus.status = "PreSubmitted"
            return trade

    ib = DropsAfterPlacing(status="PreSubmitted")
    broker, proposal = make_broker(ib)

    with pytest.raises(ExecutionAmbiguous) as exc_info:
        broker.submit(proposal)
    assert exc_info.value.order_ref == proposal.proposal_id
