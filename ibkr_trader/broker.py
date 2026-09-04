"""The IBKR order-submission adapter.

This is the only place in the system that speaks TWS. Everything above it sees
:class:`~ibkr_trader.ports.Broker`: one proposal in, one
:class:`~ibkr_trader.models.ExecutionResult` out.

Two properties of this module matter more than the wire details:

*Durable identity.* ``proposal.proposal_id`` is stamped onto ``order.orderRef``
before the order is transmitted, so an interrupted submission is reconcilable by
reference. It is never derived after the fact.

*Ambiguity is per-order.* When the connection drops mid-transmission we cannot
tell whether the order reached the venue, so we raise
:class:`~ibkr_trader.errors.ExecutionAmbiguous` naming that one order and stop.
There is no latch, no gate file, no watcher, and no retry loop: the fact is
attached to the order, and the operator reconciles that order.

``ib_async`` is imported lazily. Importing this module must succeed on a machine
that has never talked to TWS, because the rest of the test suite imports it.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from .clock import Clock
from .config import IBKRConfig
from .errors import (
    BrokerError,
    BrokerNotConnected,
    ExecutionAmbiguous,
    SubmissionFailed,
)
from .models import (
    CONTRACT_MULTIPLIER,
    Action,
    ExecutionResult,
    Fill,
    Outcome,
    ProposalLeg,
    TradeProposal,
)

logger = logging.getLogger(__name__)

#: Routing for both the individual option legs and the combo itself.
_EXCHANGE = "SMART"
_CURRENCY = "USD"

#: How long we let a freshly placed order settle before reporting its state.
#:
#: A bounded pump, not a retry loop: it exists so a marketable spread reports
#: ``FILLED`` instead of ``WORKING`` on the same pass. Whatever state the order
#: is in when the budget runs out is reported honestly.
_SETTLE_POLLS = 10
_POLL_SECONDS = 0.25

#: IBKR order states that will not change again without further action.
_DONE_STATUSES = frozenset({"Filled", "Cancelled", "ApiCancelled", "Inactive"})

#: IBKR order status -> our terminal vocabulary.
#:
#: ``Inactive`` is IBKR's answer for "I looked at this and will not work it",
#: which is a rejection *by the venue* and therefore ``BROKER_REJECTED`` -- not
#: ``SubmissionFailed``, which means the order never got there at all.
#:
#: ``PendingCancel`` is a cancel *request* IBKR has not confirmed: the order can
#: still fill, and the venue can still refuse the cancel. It is therefore
#: ``WORKING`` and is deliberately absent from :data:`_DONE_STATUSES`, so the two
#: tables agree that this status is not settled.
#:
#: An unmapped or empty status is one nothing here can classify. It degrades to
#: ``EXECUTION_AMBIGUOUS`` rather than ``ACCEPTED``, because "I do not recognise
#: this" and "the venue acknowledged it" are different claims, and only the
#: former belongs in the reconciliation bucket.
_STATUS_OUTCOMES: Mapping[str, Outcome] = {
    "Filled": Outcome.FILLED,
    "Submitted": Outcome.WORKING,
    "PreSubmitted": Outcome.WORKING,
    "PendingSubmit": Outcome.WORKING,
    "PendingCancel": Outcome.WORKING,
    "ApiPending": Outcome.ACCEPTED,
    "ApiUpdate": Outcome.ACCEPTED,
    "Inactive": Outcome.BROKER_REJECTED,
    "Cancelled": Outcome.BROKER_REJECTED,
    "ApiCancelled": Outcome.BROKER_REJECTED,
}


class IBClient(Protocol):
    """The slice of ``ib_async.IB`` this adapter actually uses.

    Declared so a test can inject a recording double and exercise the whole
    submission path without TWS and without ``ib_async`` installed.
    """

    def isConnected(self) -> bool: ...

    def connect(self, host: str, port: int, clientId: int, timeout: float) -> Any: ...

    def disconnect(self) -> None: ...

    def qualifyContracts(self, *contracts: Any) -> list[Any]: ...

    def placeOrder(self, contract: Any, order: Any) -> Any: ...

    def waitOnUpdate(self, timeout: float = 0) -> bool: ...


class IBApi(Protocol):
    """The ``ib_async`` module surface used to build contracts and orders.

    Separate from :class:`IBClient` because the constructors are needed even
    when the client is a test double: injecting this is what keeps the contract
    builder reachable on a machine without ``ib_async``.
    """

    Contract: Any
    ComboLeg: Any
    Option: Any
    LimitOrder: Any
    IB: Any


def _load_api() -> IBApi:
    """Import ``ib_async`` on demand.

    Raises:
        BrokerNotConnected: the dependency is absent, so no connection to TWS
            can exist and nothing can be submitted.
    """
    try:
        import ib_async  # noqa: PLC0415 - deliberately lazy; see module docstring
    except ImportError as exc:
        logger.error("ib_async is not installed; the IBKR adapter is unusable")
        raise BrokerNotConnected(
            "ib_async is not installed; install it to submit orders to IBKR"
        ) from exc
    return ib_async


def _combo_leg_action(leg_action: Action) -> str:
    """Render one leg's action for the wire.

    The bag is always bought, so every leg executes exactly as written and this
    is an identity mapping. It exists as a named function so the guarantee is
    stated in one place rather than implied by an inline attribute access.
    """
    return leg_action.value


def _to_utc(moment: datetime | None, fallback: datetime) -> datetime:
    """Coerce a broker timestamp to an aware UTC instant."""
    if moment is None:
        return fallback
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _rejection_text(trade: Any) -> str:
    """The venue's own words for why it refused the order.

    Preserved verbatim and unprefixed. Operators diagnose IBKR rejections by
    their exact text (``"Order rejected - reason:201 ..."``); paraphrasing it
    destroys the only searchable part of the record.
    """
    reasons: list[str] = []
    for entry in getattr(trade, "log", ()) or ():
        if getattr(entry, "errorCode", 0) and getattr(entry, "message", ""):
            reasons.append(str(entry.message))
    advanced = getattr(trade, "advancedError", "")
    if advanced:
        reasons.append(str(advanced))
    if reasons:
        return "; ".join(dict.fromkeys(reasons))
    return f"IBKR reported status {trade.orderStatus.status!r} with no reason given"


class IBKRBroker:
    """Submits vertical spreads to IBKR as single combo (``BAG``) orders.

    Sign convention -- the single easiest thing here to get backwards:

    The bag is **always bought**, and its legs are always written exactly as the
    proposal states them. The net premium is carried by the *sign of the limit
    price*, which is IBKR's documented convention for combination orders: a net
    credit is expressed as a negative limit price.

    ``TradeProposal.limit_price`` uses our domain's sign -- positive means
    premium collected. The price paid to buy the bag is therefore its negation::

        order:  BUY 1 BAG @ -1.75          (negative limit price = net credit)
        legs:   SELL 185P ratio 1,  BUY 180P ratio 1
        effect: SELL 185P, BUY 180P   -> 1.75 collected per spread

    This encoding is chosen deliberately over the alternative (selling the bag
    with inverted legs). Both can be made to work, but only this one writes the
    legs *literally as reviewed*: the ``ComboLeg`` list reads "SELL 185 PUT, BUY
    180 PUT", which is the trade a human approved. The inverted form writes the
    opposite of what was approved and relies on TWS mirroring the legs back --
    a double negative that cannot be eyeballed and inverts the whole position if
    that assumption is ever wrong.

    Note:
        The leg/side/price encoding is the one part of this adapter that cannot
        be proven without a live TWS session. Confirm it once against a paper
        account -- the order preview must show a short 185 put and a long 180
        put for a **credit** -- before trusting it.
    """

    def __init__(
        self,
        config: IBKRConfig,
        clock: Clock,
        ib: IBClient | None = None,
        api: IBApi | None = None,
    ) -> None:
        """Build the adapter without touching the network.

        Args:
            config: TWS connection settings.
            clock: Sole source of time; supplies fill timestamps when IBKR's own
                execution timestamp is missing.
            ib: An existing client. Injecting one is how tests exercise
                submission without TWS; when omitted a real ``ib_async.IB`` is
                created on :meth:`connect`.
            api: The ``ib_async`` module, or a stand-in exposing ``Contract``,
                ``ComboLeg``, ``Option``, ``LimitOrder`` and ``IB``. Defaults to
                importing ``ib_async`` at first use.
        """
        self._config = config
        self._clock = clock
        self._ib = ib
        self._api = api

    # -- connection --------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        """False when submission cannot currently be attempted."""
        return self._ib is not None and bool(self._ib.isConnected())

    @property
    def client(self) -> IBClient:
        """The live TWS session, so market data can share this one connection.

        Exposed because the scanner and the broker are two views of the same
        session; opening a second one would burn another client id and another
        set of market-data lines for nothing.

        Liveness is checked, not just presence: a session that has dropped
        since :meth:`connect` returned leaves ``self._ib`` set but unusable, and
        handing that out lets a caller work against a dead socket.

        Raises:
            BrokerNotConnected: :meth:`connect` has not been called, or the
                session it opened is no longer live.
        """
        if self._ib is None:
            raise BrokerNotConnected("connect() must be called before use")
        if not self._ib.isConnected():
            raise BrokerNotConnected("the IBKR session is no longer connected")
        return self._ib

    def connect(self) -> None:
        """Establish (or confirm) the TWS session.

        Idempotent: an already-connected client is left alone, so a repeat scan
        does not churn the session.

        Raises:
            BrokerNotConnected: the session could not be established.
        """
        if self._ib is None:
            self._ib = self._require_api().IB()

        if self._ib.isConnected():
            return

        target = f"{self._config.host}:{self._config.port} clientId={self._config.client_id}"
        try:
            self._ib.connect(
                host=self._config.host,
                port=self._config.port,
                clientId=self._config.client_id,
                timeout=self._config.connect_timeout_seconds,
            )
        except Exception as exc:
            logger.error("IBKR connect to %s failed: %s", target, exc)
            raise BrokerNotConnected(f"cannot connect to IBKR at {target}: {exc}") from exc

        if not self._ib.isConnected():
            logger.error("IBKR connect to %s returned without a session", target)
            raise BrokerNotConnected(f"connected to {target} but the session is not live")

        logger.info("connected to IBKR at %s (paper=%s)", target, self._config.paper)

    def disconnect(self) -> None:
        """Close the TWS session if one is open.

        Raises:
            BrokerError: the transport failed while closing.
        """
        if self._ib is None:
            return
        try:
            self._ib.disconnect()
        except Exception as exc:
            logger.error("IBKR disconnect failed: %s", exc)
            raise BrokerError(f"failed to close the IBKR session: {exc}") from exc
        logger.info("disconnected from IBKR")

    # -- submission --------------------------------------------------------

    def submit(self, proposal: TradeProposal) -> ExecutionResult:
        """Submit ``proposal`` as one combo order and report what IBKR did.

        The ``orderRef`` stamp happens in :meth:`_build_order`, before
        ``placeOrder`` is called, so every order that can possibly exist at the
        venue carries the proposal id.

        Args:
            proposal: The reviewed, approved trade.

        Returns:
            An :class:`ExecutionResult` whose ``order_ref`` is always
            ``proposal.proposal_id``.

        Raises:
            BrokerNotConnected: no usable session; nothing was sent.
            SubmissionFailed: the order was definitively not accepted and never
                reached the venue.
            ExecutionAmbiguous: the connection dropped mid-transmission, so
                arrival can be neither confirmed nor ruled out.
        """
        if not self.is_connected:
            raise BrokerNotConnected(
                f"not connected to IBKR; proposal {proposal.proposal_id} was not sent"
            )
        assert self._ib is not None  # narrowed by is_connected

        bag = self._build_combo(proposal)
        order = self._build_order(proposal)

        try:
            trade = self._ib.placeOrder(bag, order)
        except Exception as exc:
            # Distinguish "never left" from "cannot tell". A dead session at
            # this point means the order may or may not have crossed the wire.
            if not self._ib.isConnected():
                logger.error("connection lost transmitting %s: %s", proposal.proposal_id, exc)
                raise ExecutionAmbiguous(
                    f"connection lost while transmitting order {proposal.proposal_id}: {exc}",
                    order_ref=proposal.proposal_id,
                ) from exc
            logger.error("placeOrder rejected %s: %s", proposal.proposal_id, exc)
            raise SubmissionFailed(
                f"IBKR refused order {proposal.proposal_id} before transmission: {exc}"
            ) from exc

        # Past this point the order is live at IBKR. Anything that fails while
        # reading its result is an ambiguity about a transmitted order, not a
        # generic error: it must carry the order_ref so the runner's isolation
        # boundary can persist the proposal instead of losing every trace of it.
        try:
            self._settle(trade, proposal)
            return self._interpret(trade, proposal)
        except (ExecutionAmbiguous, BrokerNotConnected, SubmissionFailed):
            raise
        except Exception as exc:
            logger.error(
                "could not read the result of transmitted order %s: %s",
                proposal.proposal_id,
                exc,
            )
            raise ExecutionAmbiguous(
                f"order {proposal.proposal_id} was transmitted but its result could "
                f"not be read: {exc}",
                order_ref=proposal.proposal_id,
            ) from exc

    # -- contract and order construction -----------------------------------

    def _build_combo(self, proposal: TradeProposal) -> Any:
        """Qualify every leg and assemble the ``BAG`` contract.

        Qualification is mandatory rather than opportunistic: a ``ComboLeg``
        identifies its leg only by ``conId``, so an unqualified leg silently
        produces a bag that is not the spread that was reviewed.

        Raises:
            SubmissionFailed: a leg could not be resolved to a contract, so
                nothing is transmitted.
        """
        api = self._require_api()
        options = [self._build_option(api, proposal.symbol, leg) for leg in proposal.legs]
        try:
            qualified = self._ib.qualifyContracts(*options)  # type: ignore[union-attr]
        except Exception as exc:
            logger.error("qualifying legs for %s failed: %s", proposal.proposal_id, exc)
            raise SubmissionFailed(
                f"cannot qualify option legs for {proposal.symbol} "
                f"({proposal.proposal_id}): {exc}"
            ) from exc

        if len(qualified) != len(proposal.legs):
            raise SubmissionFailed(
                f"IBKR qualified {len(qualified)} of {len(proposal.legs)} legs for "
                f"{proposal.symbol} ({proposal.proposal_id})"
            )

        combo_legs = []
        for leg, contract in zip(proposal.legs, qualified, strict=True):
            con_id = getattr(contract, "conId", 0) if contract is not None else 0
            if not con_id:
                raise SubmissionFailed(
                    f"IBKR could not resolve {proposal.symbol} {leg.expiry} "
                    f"{leg.strike} {leg.right.value} ({proposal.proposal_id})"
                )
            combo_legs.append(
                api.ComboLeg(
                    conId=int(con_id),
                    ratio=leg.ratio,
                    action=_combo_leg_action(leg.action),
                    exchange=_EXCHANGE,
                )
            )

        return api.Contract(
            secType="BAG",
            symbol=proposal.symbol,
            exchange=_EXCHANGE,
            currency=_CURRENCY,
            comboLegs=combo_legs,
        )

    def _build_option(self, api: IBApi, symbol: str, leg: ProposalLeg) -> Any:
        """One leg as an unqualified ``Option`` contract."""
        return api.Option(
            symbol=symbol,
            lastTradeDateOrContractMonth=leg.expiry.strftime("%Y%m%d"),
            strike=float(leg.strike),
            right=leg.right.value,
            exchange=_EXCHANGE,
            currency=_CURRENCY,
            multiplier=str(int(CONTRACT_MULTIPLIER)),
        )

    def _build_order(self, proposal: TradeProposal) -> Any:
        """The limit order for the bag, carrying the durable reference.

        ``orderRef`` is set here -- before the caller can transmit -- because
        that is the only stamp that survives a dropped connection.
        """
        api = self._require_api()
        order = api.LimitOrder(
            self._combo_action(proposal).value,
            float(proposal.quantity),
            # Negated: our sign is credit-positive, the wire wants the price
            # *paid* for the bag, so a collected credit is a negative limit.
            float(-proposal.limit_price),
        )
        order.orderRef = proposal.proposal_id
        order.tif = "DAY"
        if self._config.account:
            order.account = self._config.account
        return order

    def _combo_action(self, proposal: TradeProposal) -> Action:
        """Which side the bag goes out on: always BUY.

        Credit versus debit is carried by the sign of the limit price, not by
        the side. See the class docstring for why.
        """
        return Action.BUY

    def _require_api(self) -> IBApi:
        """The ``ib_async`` surface, imported on first use."""
        if self._api is None:
            self._api = _load_api()
        return self._api

    # -- reading the result ------------------------------------------------

    def _settle(self, trade: Any, proposal: TradeProposal) -> None:
        """Pump broker events for a bounded window so the status is current.

        Bounded by construction: at most :data:`_SETTLE_POLLS` polls, and it
        stops the moment the order reaches a done state. It never re-sends and
        never waits for a state it wants to see.

        Raises:
            ExecutionAmbiguous: the event stream failed. The order is already
                transmitted and we have lost the ability to observe it, which is
                exactly the state that cannot be resolved from here.
        """
        assert self._ib is not None
        for _ in range(_SETTLE_POLLS):
            if trade.orderStatus.status in _DONE_STATUSES:
                return
            try:
                self._ib.waitOnUpdate(timeout=_POLL_SECONDS)
            except Exception as exc:
                logger.error(
                    "event stream failed after transmitting %s: %s",
                    proposal.proposal_id,
                    exc,
                )
                raise ExecutionAmbiguous(
                    f"lost the IBKR event stream after transmitting order "
                    f"{proposal.proposal_id}; its state cannot be established: {exc}",
                    order_ref=proposal.proposal_id,
                ) from exc

    def _interpret(self, trade: Any, proposal: TradeProposal) -> ExecutionResult:
        """Translate a live ``Trade`` into our terminal vocabulary.

        Raises:
            ExecutionAmbiguous: the session died before IBKR said anything about
                an order that has already been transmitted.
        """
        status = str(trade.orderStatus.status or "")

        assert self._ib is not None
        if not self._ib.isConnected() and status not in _DONE_STATUSES:
            logger.error("connection lost with %s in state %r", proposal.proposal_id, status)
            raise ExecutionAmbiguous(
                f"connection to IBKR lost with order {proposal.proposal_id} in "
                f"state {status or 'unknown'}; arrival cannot be confirmed",
                order_ref=proposal.proposal_id,
            )

        outcome = _STATUS_OUTCOMES.get(status, Outcome.EXECUTION_AMBIGUOUS)

        if outcome is Outcome.BROKER_REJECTED:
            message = _rejection_text(trade)
            logger.warning("IBKR rejected %s: %s", proposal.proposal_id, message)
        elif outcome is Outcome.FILLED:
            message = f"filled at status {status}"
        elif status in _STATUS_OUTCOMES:
            message = f"IBKR status {status}"
        else:
            message = f"IBKR reported unrecognized status {status!r}; state unresolved"
            logger.warning("unmapped IBKR status %r for %s", status, proposal.proposal_id)

        return ExecutionResult(
            outcome=outcome,
            order_ref=proposal.proposal_id,
            broker_order_id=self._broker_order_id(trade),
            message=message,
            # Fills are read from whatever the venue reported, not gated on the
            # outcome: a DAY order that partially filled and then cancelled is
            # BROKER_REJECTED and still owns contracts. _build_fills returns ()
            # when nothing actually traded.
            fills=self._build_fills(trade, proposal),
        )

    def _broker_order_id(self, trade: Any) -> str | None:
        """IBKR's own handle on the order, preferring the permanent id."""
        status = trade.orderStatus
        perm_id = getattr(status, "permId", 0) or getattr(trade.order, "permId", 0)
        if perm_id:
            return str(perm_id)
        order_id = getattr(status, "orderId", 0) or getattr(trade.order, "orderId", 0)
        return str(order_id) if order_id else None

    def _build_fills(self, trade: Any, proposal: TradeProposal) -> tuple[Fill, ...]:
        """Collapse IBKR's per-leg executions into one package-level fill.

        ``Trade.fills`` carries one execution *per leg*, so summing their shares
        would report a two-leg vertical as twice the contracts it is. The bag's
        ``orderStatus`` is already stated in the unit our domain speaks --
        spreads filled, and the net price of the package -- so quantity and
        price come from there while ``Trade.fills`` supplies the real execution
        timestamp.

        The returned price carries the proposal's sign convention: positive for
        a credit received, negative for a debit paid, matching
        ``TradeProposal.limit_price``.

        Nothing is fabricated. A quantity or price the venue did not report is
        unknown, not zero, and an unknown fill is no fill: substituting the
        proposal's own quantity and limit price would write a *request* into the
        durable record as though it were an observed *fact*, with no marker
        saying so. Returns ``()`` whenever the venue has not reported a real
        execution, which is also how a genuinely unfilled order reports.
        """
        status = trade.orderStatus

        reported_quantity = getattr(status, "filled", None)
        if reported_quantity is None:
            return ()
        quantity = int(reported_quantity)
        if quantity <= 0:
            return ()

        reported_price = getattr(status, "avgFillPrice", None)
        if reported_price is None:
            return ()
        magnitude = Decimal(str(abs(reported_price)))
        if magnitude == 0:
            return ()
        price = magnitude if proposal.is_credit else -magnitude

        filled_at = self._clock.now()
        times = [
            _to_utc(getattr(fill, "time", None), filled_at)
            for fill in (getattr(trade, "fills", ()) or ())
        ]
        if times:
            filled_at = max(times)

        return (Fill(quantity=quantity, price=price, filled_at=filled_at),)
