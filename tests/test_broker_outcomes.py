"""How the IBKR adapter classifies what came back, and what it records.

`test_broker_encoding` pins what goes *out*. This module pins what is made of
what comes *back*: which venue status maps to which terminal outcome, when a
fill is real, and what happens when reading the result fails after the order is
already live. Every test here fails against the pre-fix adapter.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ibkr_trader.broker import IBKRBroker
from ibkr_trader.clock import FixedClock
from ibkr_trader.errors import BrokerNotConnected, ExecutionAmbiguous
from ibkr_trader.models import Outcome

from .fakes import SCAN_TIME
from .test_broker_encoding import FAKE_API, canonical_proposal


class ResultIB:
    """An IB client whose reported order result is fully controllable."""

    def __init__(
        self,
        status: str = "Filled",
        filled: int | None = 3,
        avg_fill_price: float | None = 1.75,
        connected: bool = True,
        order_status: object | None = None,
    ) -> None:
        self._status = status
        self._filled = filled
        self._price = avg_fill_price
        self._connected = connected
        self._order_status = order_status

    def isConnected(self) -> bool:
        return self._connected

    def disconnect(self) -> None:
        self._connected = False

    def qualifyContracts(self, *contracts):
        for contract in contracts:
            contract.conId = int(contract.strike)
        return list(contracts)

    def placeOrder(self, contract, order):
        if self._order_status is not None:
            order_status = self._order_status
        else:
            fields = {"status": self._status, "orderId": 77}
            if self._filled is not None:
                fields["filled"] = self._filled
            if self._price is not None:
                fields["avgFillPrice"] = self._price
            order_status = SimpleNamespace(**fields)
        return SimpleNamespace(
            contract=contract,
            order=order,
            orderStatus=order_status,
            fills=[],
            log=[SimpleNamespace(status=self._status, errorCode=201, message="rejected")],
            advancedError="",
        )

    def waitOnUpdate(self, timeout: float = 0) -> bool:
        return True


def submit_with(ib: ResultIB):
    config, proposal = canonical_proposal()
    broker = IBKRBroker(config.ibkr, FixedClock(SCAN_TIME), ib=ib, api=FAKE_API)
    return broker.submit(proposal), proposal


# --- INT-001 -------------------------------------------------------------


def test_a_partially_filled_then_cancelled_order_records_its_real_fill():
    """Contracts that actually traded must not be persisted as zero fills.

    IBKR reports a DAY order that partially filled and then cancelled as
    ``Cancelled`` with ``filled > 0``. The position exists. Recording it as a
    bare rejection with no fills loses every trace of contracts already owned.
    """
    result, _ = submit_with(ResultIB(status="Cancelled", filled=2, avg_fill_price=1.70))

    assert result.fills, "a partial fill was discarded"
    assert result.fills[0].quantity == 2


# --- INT-014 -------------------------------------------------------------


def test_an_unrecognized_status_is_not_reported_as_a_clean_acknowledgement():
    """An unmappable status means 'cannot classify', not 'accepted'.

    Defaulting to ACCEPTED gives the caller no way to tell a confidently
    acknowledged order from one whose state nobody understands, while still
    counting it as live.
    """
    result, _ = submit_with(ResultIB(status="SomeFutureStatus"))

    assert result.outcome is Outcome.EXECUTION_AMBIGUOUS


# --- INT-015 -------------------------------------------------------------


def test_pending_cancel_is_not_a_terminal_venue_rejection():
    """A cancel IBKR has not confirmed is still working, not rejected.

    ``PendingCancel`` is deliberately absent from the done-statuses set, so
    settle keeps polling it. Classifying the same status as a terminal
    BROKER_REJECTED made the two tables contradict each other.
    """
    result, _ = submit_with(ResultIB(status="PendingCancel"))

    assert result.outcome is Outcome.WORKING


# --- INT-018 -------------------------------------------------------------


def test_a_fill_is_not_fabricated_from_the_proposal_when_the_venue_reports_none():
    """Zero quantity and zero price must not fall back to what was requested.

    The old falsy-`or` fallbacks substituted the proposal's own quantity and
    limit price and wrote them to the durable record with no marker, turning a
    request into a recorded fact at the money boundary.
    """
    result, proposal = submit_with(ResultIB(status="Filled", filled=0, avg_fill_price=0.0))

    assert result.fills == ()
    assert proposal.quantity != 0  # the value that would have been fabricated


def test_a_missing_fill_field_is_not_fabricated_either():
    """An absent field is unknown, not zero, and still must not be invented."""
    result, _ = submit_with(ResultIB(status="Filled", filled=None, avg_fill_price=None))

    assert result.fills == ()


# --- INT-027 -------------------------------------------------------------


def test_client_refuses_to_hand_out_a_dead_session():
    """`client` guarded only on `is None`, so a dropped session was handed out."""
    config, _ = canonical_proposal()
    ib = ResultIB(connected=False)
    broker = IBKRBroker(config.ibkr, FixedClock(SCAN_TIME), ib=ib, api=FAKE_API)

    with pytest.raises(BrokerNotConnected):
        _ = broker.client


# --- INT-002 -------------------------------------------------------------


def test_a_failure_after_transmission_is_ambiguous_and_names_the_order():
    """Reading the result can fail after the order is already live at IBKR.

    Only `placeOrder` itself was guarded. Anything raising in settle or
    interpret escaped as a plain exception, and the runner's isolation boundary
    caught it without the proposal in scope -- so a transmitted order left no
    proposal_id anywhere on disk.
    """
    # An orderStatus with no `status` attribute: reading it raises AttributeError.
    broken = SimpleNamespace(orderId=77)

    with pytest.raises(ExecutionAmbiguous) as caught:
        submit_with(ResultIB(order_status=broken))

    _, proposal = canonical_proposal()
    assert caught.value.order_ref is not None
    assert "transmitted" in str(caught.value)
