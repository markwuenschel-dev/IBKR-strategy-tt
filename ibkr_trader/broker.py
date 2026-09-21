"""The IBKR order-submission adapter.

This is the only place in the system that speaks TWS. Everything above it sees
:class:`~ibkr_trader.ports.Broker`: one order in -- a reviewed proposal through
:meth:`IBKRBroker.submit`, or any :class:`~ibkr_trader.models.ComboOrder`
through :meth:`IBKRBroker.place` -- and one
:class:`~ibkr_trader.models.ExecutionResult` out; plus, by reference alone, a
cancel (:meth:`IBKRBroker.cancel`) and a lookup of what is still working
(:meth:`IBKRBroker.working_order_refs`).

Two properties of this module matter more than the wire details:

*Durable identity.* ``ComboOrder.order_ref`` -- the proposal id, for an opening
order -- is stamped onto ``order.orderRef`` before the order is transmitted, so
an interrupted submission is reconcilable by reference. It is never derived
after the fact, and it is the only key the cancel and working-order lookups use.

*Ambiguity is per-order.* When the connection drops mid-transmission we cannot
tell whether the order reached the venue, so we raise
:class:`~ibkr_trader.errors.ExecutionAmbiguous` naming that one order and stop.
There is no latch, no gate file, no watcher, and no retry loop: the fact is
attached to the order, and the operator reconciles that order.

``ib_async`` is imported lazily. Importing this module must succeed on a machine
that has never talked to TWS, because the rest of the test suite imports it.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from .bag import bag_contract
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
    ComboOrder,
    ExecutionResult,
    Fill,
    OptionLeg,
    Outcome,
    Tif,
    TradeProposal,
    opening_combo_legs,
)

logger = logging.getLogger(__name__)

#: Routing for both the individual option legs and the combo itself.
_EXCHANGE = "SMART"
_CURRENCY = "USD"

#: ``ComboOrder.purpose`` of the order a reviewed proposal is transmitted as.
OPENING_PURPOSE = "OPEN"

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

    def managedAccounts(self) -> list[str]: ...

    def qualifyContracts(self, *contracts: Any) -> list[Any]: ...

    def placeOrder(self, contract: Any, order: Any) -> Any: ...

    def waitOnUpdate(self, timeout: float = 0) -> bool: ...

    def openTrades(self) -> list[Any]: ...

    def reqAllOpenOrders(self) -> list[Any]: ...

    def cancelOrder(self, order: Any) -> Any: ...


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


def _opening_order(proposal: TradeProposal) -> ComboOrder:
    """The opening ``ComboOrder`` a reviewed proposal is transmitted as.

    Legs are carried over exactly as the proposal states them, in the same
    order and with the same actions, because the bag's legs are what a human
    approved (see the class docstring's sign convention). The reference is the
    proposal id, so the durable identity stamped on the venue order is the one
    already in the reviewer record and every persisted row.
    """
    return ComboOrder(
        symbol=proposal.symbol,
        legs=opening_combo_legs(proposal),
        quantity=proposal.quantity,
        limit_price=proposal.limit_price,
        tif=Tif.DAY,
        order_ref=proposal.proposal_id,
        purpose=OPENING_PURPOSE,
    )


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

    Satisfies :class:`~ibkr_trader.ports.Broker`, checked by
    ``tests/test_port_conformance.py`` rather than asserted here: structure,
    signatures, and the documented ``Raises:`` block all have to agree with the
    port. Said on the class because the claim used to live in the module
    docstring 180 lines above and never named the class it was about.

    Sign convention -- the single easiest thing here to get backwards:

    The bag is **always bought**, and its legs are always written exactly as the
    proposal states them. The net premium is carried by the *sign of the limit
    price*, which is IBKR's documented convention for combination orders: a net
    credit is expressed as a negative limit price.

    ``ComboOrder.limit_price`` -- and ``TradeProposal.limit_price``, which an
    opening order copies -- uses our domain's sign: positive means premium
    collected. The price paid to buy the bag is therefore its negation::

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

    The same encoding closes a spread. A closing order is the mirror of the
    opening legs at a *debit*: its ``limit_price`` is negative, so the bag is
    bought at a positive price and the legs again read literally.

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
        self._verified_account: str | None = None

    # -- connection --------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        """False when submission cannot currently be attempted."""
        return self._ib is not None and bool(self._ib.isConnected())

    @property
    def verified_account(self) -> str | None:
        """Account confirmed by ``managedAccounts()`` for this session."""
        if not self.is_connected:
            return None
        return self._verified_account

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

        target = f"{self._config.host}:{self._config.port} clientId={self._config.client_id}"
        if self._ib.isConnected():
            if self._verified_account is None:
                self._require_configured_account(target)
            return

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

        self._require_configured_account(target)
        logger.info("connected to IBKR at %s, account %s", target, self._config.account)

    def _require_configured_account(self, target: str) -> None:
        """Refuse a session that did not reach the account this config names.

        **What this proves, exactly.** That the process reached the account the
        operator named. It does *not* prove that account is a paper account:
        IBKR exposes no paper/live indicator anywhere in the handshake, the
        ``DU`` prefix is a convention it has never documented, and this
        repository's own verified paper account -- ``DUR318607`` -- does not
        match the shape people usually assume. Paper safety is therefore an
        operator-naming discipline that this check enforces as *identity*. No
        message here may imply more than that.

        Why identity rather than the port: a config copied between machines is
        the realistic way the wrong book gets traded, and a port number cannot
        see it. This can.

        The session is closed before the error propagates. Refusing while
        leaving a live socket open would be worse than not checking, and the
        caller only learns the session is unusable -- it should not also have to
        clean up after the check that said so.
        """
        try:
            accounts = [str(a) for a in self._ib.managedAccounts()]
        except Exception as exc:
            self._close_quietly()
            logger.exception("Could not read the account list from %s", target)
            raise BrokerNotConnected(
                f"connected to {target} but could not read the account list: {exc}"
            ) from exc

        if not accounts:
            self._close_quietly()
            logger.error("IBKR at %s reported no accounts", target)
            raise BrokerNotConnected(
                f"connected to {target} but it reported no accounts, so the "
                f"configured account {self._config.account!r} cannot be verified"
            )

        if self._config.account not in accounts:
            self._close_quietly()
            logger.error(
                "IBKR at %s reports accounts %s, not the configured %s",
                target,
                accounts,
                self._config.account,
            )
            raise BrokerNotConnected(
                f"configured account {self._config.account!r} is not among the "
                f"accounts this session reports ({', '.join(accounts)}); refusing "
                f"to trade a book that was not named"
            )
        self._verified_account = self._config.account

    def _close_quietly(self) -> None:
        """Drop the session without letting teardown replace the real failure."""
        self._verified_account = None
        with contextlib.suppress(Exception):
            if self._ib is not None:
                self._ib.disconnect()

    def disconnect(self) -> None:
        """Close the TWS session if one is open.

        Raises:
            BrokerError: the transport failed while closing.
        """
        self._verified_account = None
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
        """Submit ``proposal`` as one opening combo order and report what IBKR did.

        A proposal is the opening case of :meth:`place`: its legs, quantity and
        limit price become a :class:`~ibkr_trader.models.ComboOrder` with
        purpose ``OPEN``, a ``DAY`` time in force and ``proposal.proposal_id``
        as the durable reference (see :func:`_opening_order`). Qualification,
        the stamp-before-transmit rule and every exception mapping are
        :meth:`place`'s, so an opening credit and its closing debit go out
        through one path.

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
        return self.place(_opening_order(proposal))

    def place(self, order: ComboOrder) -> ExecutionResult:
        """Transmit ``order`` as one combo order and report what IBKR did.

        The ``orderRef`` stamp happens in :meth:`_build_order`, before
        ``placeOrder`` is called, so every order that can possibly exist at the
        venue carries ``order.order_ref``.

        Args:
            order: The combo to transmit, in the domain's price convention.

        Returns:
            An :class:`ExecutionResult` whose ``order_ref`` is always
            ``order.order_ref``.

        Raises:
            BrokerNotConnected: no usable session; nothing was sent.
            SubmissionFailed: the order was definitively not accepted and never
                reached the venue.
            ExecutionAmbiguous: the connection dropped mid-transmission, so
                arrival can be neither confirmed nor ruled out.
        """
        if not self.is_connected:
            raise BrokerNotConnected(
                f"not connected to IBKR; order {order.order_ref} was not sent"
            )
        assert self._ib is not None  # narrowed by is_connected

        bag = self._build_combo(order)
        venue_order = self._build_order(order)

        try:
            trade = self._ib.placeOrder(bag, venue_order)
        except Exception as exc:
            # Distinguish "never left" from "cannot tell". A dead session at
            # this point means the order may or may not have crossed the wire.
            if not self._ib.isConnected():
                logger.error("connection lost transmitting %s: %s", order.order_ref, exc)
                raise ExecutionAmbiguous(
                    f"connection lost while transmitting order {order.order_ref}: {exc}",
                    order_ref=order.order_ref,
                ) from exc
            logger.error("placeOrder rejected %s: %s", order.order_ref, exc)
            raise SubmissionFailed(
                f"IBKR refused order {order.order_ref} before transmission: {exc}"
            ) from exc

        # Past this point the order is live at IBKR. Anything that fails while
        # reading its result is an ambiguity about a transmitted order, not a
        # generic error: it must carry the order_ref so the runner's isolation
        # boundary can persist the proposal instead of losing every trace of it.
        try:
            self._settle(trade, order)
            return self._interpret(trade, order)
        except (ExecutionAmbiguous, BrokerNotConnected, SubmissionFailed):
            raise
        except Exception as exc:
            logger.error(
                "could not read the result of transmitted order %s: %s",
                order.order_ref,
                exc,
            )
            raise ExecutionAmbiguous(
                f"order {order.order_ref} was transmitted but its result could "
                f"not be read: {exc}",
                order_ref=order.order_ref,
            ) from exc

    # -- working orders ----------------------------------------------------

    def cancel(self, order_ref: str) -> bool:
        """Cancel the working order carrying ``order_ref``.

        Looked up at the venue, never in this process's memory: first among this
        client's own open trades (``openTrades``), then among every client's
        (``reqAllOpenOrders``), so a profit target resting since an earlier
        process can still be pulled after a restart. Only orders for the
        verified account, and only ones not already in a done state, count as
        working -- see :meth:`_is_working_for_us`.

        Transmitting the cancel is all this promises. IBKR confirms it
        asynchronously and can still fill the order first, which is why a
        ``True`` here is followed by a reconciliation against positions rather
        than treated as proof the order is gone.

        Returns:
            True when a working order carried the reference and a cancel was
            transmitted; False when no working order carries it (filled,
            cancelled, or never existed). False is not an error.

        Raises:
            BrokerNotConnected: no usable session; nothing was sent.
            BrokerError: the open-order stream could not be read, or the
                cancel could not be transmitted.
        """
        ib = self.client
        trade = self._find_working(ib, order_ref)
        if trade is None:
            logger.info("no working order carries ref %s; nothing to cancel", order_ref)
            return False
        try:
            ib.cancelOrder(trade.order)
        except Exception as exc:
            logger.error("cancel of %s could not be transmitted: %s", order_ref, exc)
            raise BrokerError(
                f"could not transmit a cancel for order {order_ref}: {exc}"
            ) from exc
        logger.info("cancel transmitted for order %s", order_ref)
        return True

    def working_order_refs(self) -> frozenset[str]:
        """References of every order currently working for the verified account.

        Read through ``reqAllOpenOrders`` -- every client's orders, not only
        this one's -- because the caller is asking what is resting at the
        venue, and a restart changes nothing about that. Orders for another
        account under the same login, orders already in a done state, and
        orders carrying no reference at all are dropped: the first are not
        ours, the second are not working, and the third cannot be matched to
        anything this system placed.

        Raises:
            BrokerNotConnected: no usable session.
            BrokerError: the open-order stream could not be read.
        """
        ib = self.client
        refs: set[str] = set()
        for trade in self._read_open_orders(ib.reqAllOpenOrders, "reqAllOpenOrders"):
            if not self._is_working_for_us(trade):
                continue
            ref = str(getattr(getattr(trade, "order", None), "orderRef", "") or "")
            if ref:
                refs.add(ref)
        return frozenset(refs)

    def _find_working(self, ib: IBClient, order_ref: str) -> Any | None:
        """The working trade stamped ``order_ref``, or None.

        This client's own trades first because they cost no round trip;
        ``reqAllOpenOrders`` second because it is the only view that includes
        an order placed by a process that is no longer running.
        """
        sources = (
            (ib.openTrades, "openTrades"),
            (ib.reqAllOpenOrders, "reqAllOpenOrders"),
        )
        for read, name in sources:
            for trade in self._read_open_orders(read, name):
                order = getattr(trade, "order", None)
                if getattr(order, "orderRef", "") != order_ref:
                    continue
                if self._is_working_for_us(trade):
                    return trade
        return None

    @staticmethod
    def _read_open_orders(read: Any, name: str) -> list[Any]:
        """One open-order stream as a list, or a ``BrokerError`` naming it."""
        try:
            return list(read())
        except Exception as exc:
            logger.error("could not read open orders via %s: %s", name, exc)
            raise BrokerError(f"could not read the open-order stream ({name}): {exc}") from exc

    def _is_working_for_us(self, trade: Any) -> bool:
        """Whether ``trade`` is this account's and not yet in a done state.

        An order carrying no account is admitted, matching the scanner's
        reading of the same field: a double need not model it, and its absence
        is not evidence of another book.
        """
        order = getattr(trade, "order", None)
        account = self._verified_account or self._config.account
        if getattr(order, "account", "") not in ("", account):
            return False
        status = str(getattr(getattr(trade, "orderStatus", None), "status", "") or "")
        return status not in _DONE_STATUSES

    # -- contract and order construction -----------------------------------

    def _build_combo(self, order: ComboOrder) -> Any:
        """Qualify every leg and assemble the ``BAG`` contract.

        Qualification is mandatory rather than opportunistic: a ``ComboLeg``
        identifies its leg only by ``conId``, so an unqualified leg silently
        produces a bag that is not the spread that was reviewed.

        Raises:
            SubmissionFailed: a leg could not be resolved to a contract, so
                nothing is transmitted.
        """
        api = self._require_api()
        options = [self._build_option(api, combo_leg.leg) for combo_leg in order.legs]
        try:
            qualified = self._ib.qualifyContracts(*options)  # type: ignore[union-attr]
        except Exception as exc:
            logger.error("qualifying legs for %s failed: %s", order.order_ref, exc)
            raise SubmissionFailed(
                f"cannot qualify option legs for {order.symbol} ({order.order_ref}): {exc}"
            ) from exc

        if len(qualified) != len(order.legs):
            raise SubmissionFailed(
                f"IBKR qualified {len(qualified)} of {len(order.legs)} legs for "
                f"{order.symbol} ({order.order_ref})"
            )

        con_ids = [
            int(getattr(contract, "conId", 0) or 0) if contract is not None else 0
            for contract in qualified
        ]
        try:
            return bag_contract(api, order.symbol, order.legs, con_ids, _EXCHANGE, _CURRENCY)
        except ValueError as exc:
            # The shared builder refuses an unresolved leg; here that refusal
            # means nothing is transmitted, which is what SubmissionFailed says.
            raise SubmissionFailed(f"{exc} ({order.order_ref})") from exc

    def _build_option(self, api: IBApi, leg: OptionLeg) -> Any:
        """One leg as an unqualified ``Option`` contract."""
        return api.Option(
            symbol=leg.symbol,
            lastTradeDateOrContractMonth=leg.expiry.strftime("%Y%m%d"),
            strike=float(leg.strike),
            right=leg.right.value,
            exchange=_EXCHANGE,
            currency=_CURRENCY,
            multiplier=str(int(CONTRACT_MULTIPLIER)),
        )

    def _build_order(self, order: ComboOrder) -> Any:
        """The limit order for the bag, carrying the durable reference.

        ``orderRef`` is set here -- before the caller can transmit -- because
        that is the only stamp that survives a dropped connection.
        """
        api = self._require_api()
        venue_order = api.LimitOrder(
            self._combo_action(order).value,
            float(order.quantity),
            # Negated: our sign is credit-positive, the wire wants the price
            # *paid* for the bag, so a collected credit is a negative limit.
            float(-order.limit_price),
        )
        venue_order.orderRef = order.order_ref
        venue_order.tif = order.tif.value
        if self._config.account:
            venue_order.account = self._config.account
        return venue_order

    def _combo_action(self, order: ComboOrder) -> Action:
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

    def _settle(self, trade: Any, order: ComboOrder) -> None:
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
                    order.order_ref,
                    exc,
                )
                raise ExecutionAmbiguous(
                    f"lost the IBKR event stream after transmitting order "
                    f"{order.order_ref}; its state cannot be established: {exc}",
                    order_ref=order.order_ref,
                ) from exc

    def _interpret(self, trade: Any, order: ComboOrder) -> ExecutionResult:
        """Translate a live ``Trade`` into our terminal vocabulary.

        Raises:
            ExecutionAmbiguous: the session died before IBKR said anything about
                an order that has already been transmitted.
        """
        status = str(trade.orderStatus.status or "")

        assert self._ib is not None
        if not self._ib.isConnected() and status not in _DONE_STATUSES:
            logger.error("connection lost with %s in state %r", order.order_ref, status)
            raise ExecutionAmbiguous(
                f"connection to IBKR lost with order {order.order_ref} in "
                f"state {status or 'unknown'}; arrival cannot be confirmed",
                order_ref=order.order_ref,
            )

        outcome = _STATUS_OUTCOMES.get(status, Outcome.EXECUTION_AMBIGUOUS)

        if outcome is Outcome.BROKER_REJECTED:
            message = _rejection_text(trade)
            logger.warning("IBKR rejected %s: %s", order.order_ref, message)
        elif outcome is Outcome.FILLED:
            message = f"filled at status {status}"
        elif status in _STATUS_OUTCOMES:
            message = f"IBKR status {status}"
        else:
            message = f"IBKR reported unrecognized status {status!r}; state unresolved"
            logger.warning("unmapped IBKR status %r for %s", status, order.order_ref)

        return ExecutionResult(
            outcome=outcome,
            order_ref=order.order_ref,
            broker_order_id=self._broker_order_id(trade),
            message=message,
            # Fills are read from whatever the venue reported, not gated on the
            # outcome: a DAY order that partially filled and then cancelled is
            # BROKER_REJECTED and still owns contracts. _build_fills returns ()
            # when nothing actually traded.
            fills=self._build_fills(trade, order),
        )

    def _broker_order_id(self, trade: Any) -> str | None:
        """IBKR's own handle on the order, preferring the permanent id."""
        status = trade.orderStatus
        perm_id = getattr(status, "permId", 0) or getattr(trade.order, "permId", 0)
        if perm_id:
            return str(perm_id)
        order_id = getattr(status, "orderId", 0) or getattr(trade.order, "orderId", 0)
        return str(order_id) if order_id else None

    def _build_fills(self, trade: Any, order: ComboOrder) -> tuple[Fill, ...]:
        """Collapse IBKR's per-leg executions into one package-level fill.

        ``Trade.fills`` carries one execution *per leg*, so summing their shares
        would report a two-leg vertical as twice the contracts it is. The bag's
        ``orderStatus`` is already stated in the unit our domain speaks --
        spreads filled, and the net price of the package -- so quantity and
        price come from there while ``Trade.fills`` supplies the real execution
        timestamp.

        The returned price carries the order's sign convention: positive for a
        credit received, negative for a debit paid, matching
        ``ComboOrder.limit_price`` (and therefore ``TradeProposal.limit_price``
        for an opening order).

        Nothing is fabricated. A quantity or price the venue did not report is
        unknown, not zero, and an unknown fill is no fill: substituting the
        order's own quantity and limit price would write a *request* into the
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
        price = magnitude if order.is_credit else -magnitude

        filled_at = self._clock.now()
        times = [
            _to_utc(getattr(fill, "time", None), filled_at)
            for fill in (getattr(trade, "fills", ()) or ())
        ]
        if times:
            filled_at = max(times)

        return (Fill(quantity=quantity, price=price, filled_at=filled_at),)
