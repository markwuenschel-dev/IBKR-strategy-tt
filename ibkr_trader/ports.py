"""The contract surface between the runner and the outside world.

Four narrow protocols, all of them effectful. Everything the runner touches that
is *not* pure computation is reachable only through one of these, which is what
makes the whole pipeline testable without a network.

Reading this file tells you the complete set of things V4 depends on — and
``tests/test_port_conformance.py`` is what keeps that sentence true. Every
protocol here is ``runtime_checkable``, every member's signature is compared
against its adapter's, and every documented ``Raises:`` block must match the
adapter's. Before that the claim rested on four docstrings saying "Satisfies
ports.X" and on nothing that could notice when one stopped being true.

One dependency is deliberately *not* here. ``cli.py`` reads ``IBKRBroker.client``
so the two adapters share one session; that is a vendor object, and naming it in
a port would put ``ib_async`` inside the abstraction the ports exist to keep it
out of. It stays a concrete dependency of the composition root until venue
translation has an owner.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from .models import (
    ComboOrder,
    ExecutionResult,
    ManagementAction,
    MarketSnapshot,
    OpeningOrder,
    OptionLeg,
    OptionPosition,
    OptionQuote,
    Portfolio,
    ReviewDecision,
    Spread,
    SymbolResult,
    TradeProposal,
)


@runtime_checkable
class MarketData(Protocol):
    """Source of the per-symbol snapshot the algorithm evaluates."""

    def snapshot(self, symbol: str) -> MarketSnapshot:
        """Return the current market for ``symbol``.

        Raises:
            MarketDataError: quote or chain data is unavailable or unusable.
        """
        ...

    def option_positions(self) -> tuple[OptionPosition, ...]:
        """Every option contract the account holds, one row per contract.

        The per-leg view :meth:`portfolio` deliberately collapses. The manager
        needs it to recognise which held legs make up which spread, and to
        notice when a spread's legs are gone.

        Raises:
            MarketDataError: the position stream could not be read.
        """
        ...

    def quote(self, legs: Sequence[OptionLeg]) -> tuple[OptionQuote, ...]:
        """Current market for exactly these contracts, in the same order.

        A spread under management is usually outside the entry DTE band, so
        :meth:`snapshot` never quotes its legs; this does. A leg with no usable
        bid/ask is still returned, with the same dead-book figures the
        snapshot would carry, so the caller decides what an unquotable leg
        means.

        Raises:
            MarketDataError: a leg could not be qualified or quoted at all.
        """
        ...

    def portfolio(self) -> Portfolio:
        """Return current account state used for sizing and concentration limits.

        Positions are keyed by *underlying* symbol, never by option local symbol.
        The concentration limit is per underlying, so a substitute that keys by
        contract would conform to this signature and never match a symbol.

        Orders still working at the broker must be reported too, as positions
        flagged ``pending``. A venue's position stream lists only *filled*
        holdings, so an implementation returning those alone leaves an unfilled
        order invisible to the duplicate-order guard — and that guard keys on a
        position *existing*, so it is skipped rather than failed. At the default
        interval an order resting for half an hour becomes six duplicate orders.

        This is an obligation on every implementation, not a description of one
        adapter's behaviour. It is the reason the guard can be trusted at all.

        An implementation that *cannot* determine the working orders — the order
        stream failed, the venue answered partially — must say so by returning
        ``Portfolio(pending_orders_known=False)`` rather than omitting the rows
        silently. The two are indistinguishable to a caller otherwise, and the
        guards key on a row *existing*, so an omission skips them instead of
        failing them. Under that flag the algorithm stops short of submitting
        and raises the trade for a human decision; it does not halt the pass,
        and other symbols are unaffected.

        Raises:
            MarketDataError: account state is unavailable or unusable. Note this
                is for the sizing numbers, not for the working orders: an
                unreadable order stream is reported through the flag above,
                because it does not make the rest of the account state unusable.
        """
        ...


@runtime_checkable
class Reviewer(Protocol):
    """The independent second opinion on one concrete proposal.

    Consulted only when a proposal exists. There is no heartbeat, no liveness
    probe, and no session lease: the reviewer is a function of a proposal.
    """

    def review(self, proposal: TradeProposal, portfolio: Portfolio) -> ReviewDecision:
        """Return an approve/reject decision for exactly this proposal.

        Raises:
            ReviewTimeout: no answer within the configured deadline.
            ReviewError: the answer could not be parsed conservatively, or the
                transport carrying it failed. Both are the same thing to a
                caller: no usable verdict exists, and none may be invented.
        """
        ...


@runtime_checkable
class Broker(Protocol):
    """Order submission, the session behind it, and that session's lifecycle."""

    def submit(self, proposal: TradeProposal) -> ExecutionResult:
        """Submit ``proposal`` and report what the venue did with it.

        Implementations must stamp ``proposal.proposal_id`` onto the order as its
        durable reference *before* transmitting, so an interrupted submission can
        be reconciled by reference rather than guessed at.

        Raises:
            BrokerNotConnected: the connection is unusable; nothing was sent.
            SubmissionFailed: the order was definitively not accepted and never
                reached the venue. ``runner.py`` branches on this to record
                ``Outcome.SUBMISSION_FAILED``, so a substitute that never raises
                it leaves that classification permanently unreachable.
            ExecutionAmbiguous: the connection dropped mid-transmission, so
                arrival can be neither confirmed nor ruled out.
        """
        ...

    def place(self, order: ComboOrder) -> ExecutionResult:
        """Transmit a combo order and report what the venue did with it.

        :meth:`submit` is this for an opening proposal; management orders
        (profit target, close, roll halves) come through here. Same stamping
        rule: ``order.order_ref`` is on the venue order before transmission.

        Raises:
            BrokerNotConnected: the connection is unusable; nothing was sent.
            SubmissionFailed: the order was definitively not accepted and never
                reached the venue.
            ExecutionAmbiguous: the connection dropped mid-transmission.
        """
        ...

    def cancel(self, order_ref: str) -> bool:
        """Cancel the working order carrying ``order_ref``.

        Returns True when a working order with that reference was found and a
        cancel was transmitted; False when no such order is working (already
        filled, already cancelled, or never existed). A False is not an error:
        the caller reconciles against positions to learn which it was.

        Raises:
            BrokerNotConnected: the connection is unusable; nothing was sent.
            BrokerError: the open-order stream could not be read, or the cancel
                could not be transmitted.
        """
        ...

    def working_order_refs(self) -> frozenset[str]:
        """References of every order currently working for the account.

        Read from the venue, not from this process's memory, so a profit
        target placed by an earlier process is still seen after a restart.

        Raises:
            BrokerNotConnected: the connection is unusable.
            BrokerError: the open-order stream could not be read.
        """
        ...

    def connect(self) -> None:
        """Establish the session. Called once, before any submission.

        On the contract because the composition root calls it, and a contract
        that omits what the composition root calls is not the complete set of
        anything.

        Raises:
            BrokerNotConnected: no usable session could be established.
        """
        ...

    def disconnect(self) -> None:
        """Release the session. A broker that never connected releases nothing.

        The obligation this states is the *caller's*, not the implementation's:
        teardown runs on the failure path as well as the success path, so a
        caller must suppress what this raises or it will replace the failure
        actually being reported. ``cli.py`` does exactly that.

        Raises:
            BrokerError: the transport failed while closing.
        """
        ...

    @property
    def is_connected(self) -> bool:
        """False when submission cannot currently be attempted."""
        ...

    @property
    def verified_account(self) -> str | None:
        """Account identity confirmed by the connected venue session."""
        ...


@runtime_checkable
class Store(Protocol):
    """Durable record of what the system did."""

    def start_run(
        self,
        run_id: str,
        declared_mode: str,
        verified_account: str,
        host: str,
        port: int,
    ) -> None:
        """Durably identify a pass before it processes any symbol.

        Raises:
            Exception: storage failures propagate and prevent the pass.
        """
        ...

    def record(self, result: SymbolResult, run_id: str) -> None:
        """Persist the outcome of one symbol attempt.

        Raises:
            Exception: storage failures are *not* translated into a domain
                error — whatever the backend raised propagates unchanged. Stated
                rather than left silent because ``runner.py`` guards this call
                with a blanket handler, and a reader of the contract alone could
                not tell that it needed to.
        """
        ...

    def record_spread(self, spread: Spread) -> None:
        """Insert or replace the durable row for one spread, keyed by id.

        Raises:
            Exception: storage failures propagate unchanged.
        """
        ...

    def live_spreads(self) -> tuple[Spread, ...]:
        """Every spread whose status is in ``LIVE_SPREAD_STATUSES``."""
        ...

    def unreconciled_openings(self) -> tuple[OpeningOrder, ...]:
        """Opening orders that reached the venue and have no spread row yet.

        An order is "reached the venue" when its recorded outcome is in
        ``SUBMITTED_OUTCOMES`` other than ``BROKER_REJECTED``.
        """
        ...

    def record_management(self, action: ManagementAction, run_id: str) -> None:
        """Persist one management step.

        Raises:
            Exception: storage failures propagate unchanged; the manager guards
                the call the way the runner guards :meth:`record`.
        """
        ...

    def close(self) -> None:
        """Release the underlying handle. Safe to call more than once.

        On the contract because the composition root calls it in teardown. It
        was not, and the method sat with zero callers while the connection
        leaked — a contract that never mentioned it could not have made that
        visible.
        """
        ...
