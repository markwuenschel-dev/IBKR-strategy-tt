"""The contract surface between the runner and the outside world.

Four narrow protocols, all of them effectful. Everything the runner touches that
is *not* pure computation is reachable only through one of these, which is what
makes the whole pipeline testable without a network.

Reading this file tells you the complete set of things V4 depends on.
"""

from __future__ import annotations

from typing import Protocol

from .models import (
    ExecutionResult,
    MarketSnapshot,
    Portfolio,
    ReviewDecision,
    SymbolResult,
    TradeProposal,
)


class MarketData(Protocol):
    """Source of the per-symbol snapshot the algorithm evaluates."""

    def snapshot(self, symbol: str) -> MarketSnapshot:
        """Return the current market for ``symbol``.

        Raises:
            MarketDataError: quote or chain data is unavailable or unusable.
        """
        ...

    def portfolio(self) -> Portfolio:
        """Return current account state used for sizing and concentration limits."""
        ...


class Reviewer(Protocol):
    """The independent second opinion on one concrete proposal.

    Consulted only when a proposal exists. There is no heartbeat, no liveness
    probe, and no session lease: the reviewer is a function of a proposal.
    """

    def review(self, proposal: TradeProposal, portfolio: Portfolio) -> ReviewDecision:
        """Return an approve/reject decision for exactly this proposal.

        Raises:
            ReviewTimeout: no answer within the configured deadline.
            ReviewError: the answer could not be parsed conservatively.
        """
        ...


class Broker(Protocol):
    """Order submission and the account state behind it."""

    def submit(self, proposal: TradeProposal) -> ExecutionResult:
        """Submit ``proposal`` and report what the venue did with it.

        Implementations must stamp ``proposal.proposal_id`` onto the order as its
        durable reference *before* transmitting, so an interrupted submission can
        be reconciled by reference rather than guessed at.

        Raises:
            BrokerNotConnected: the connection is unusable; nothing was sent.
            ExecutionAmbiguous: the connection dropped mid-transmission.
        """
        ...

    @property
    def is_connected(self) -> bool:
        """False when submission cannot currently be attempted."""
        ...


class Store(Protocol):
    """Durable record of what the system did."""

    def record(self, result: SymbolResult, run_id: str) -> None:
        """Persist the outcome of one symbol attempt."""
        ...
