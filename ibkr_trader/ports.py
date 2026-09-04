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

from typing import Protocol, runtime_checkable

from .models import (
    ExecutionResult,
    MarketSnapshot,
    Portfolio,
    ReviewDecision,
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

        Raises:
            MarketDataError: account state is unavailable or unusable.
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


@runtime_checkable
class Store(Protocol):
    """Durable record of what the system did."""

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

    def close(self) -> None:
        """Release the underlying handle. Safe to call more than once.

        On the contract because the composition root calls it in teardown. It
        was not, and the method sat with zero callers while the connection
        leaked — a contract that never mentioned it could not have made that
        visible.
        """
        ...
