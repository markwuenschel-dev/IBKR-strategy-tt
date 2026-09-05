"""Immutable domain values.

These are the typed boundary objects every layer speaks in. They are frozen: a
proposal that has been reviewed cannot be edited before it is submitted, so
"what was approved" and "what was sent" cannot silently diverge.

Money is ``Decimal`` throughout. Option prices are exact decimal quantities, and
binary floats would make stored records and round-trip comparisons
untrustworthy. Dimensionless statistics (delta, IV rank) stay ``float``.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

#: Standard equity-option multiplier: one contract covers 100 shares.
CONTRACT_MULTIPLIER = Decimal(100)


class Right(str, Enum):
    """Option right."""

    CALL = "C"
    PUT = "P"


class Action(str, Enum):
    """Order side for a single leg."""

    BUY = "BUY"
    SELL = "SELL"


class Outcome(str, Enum):
    """Terminal outcome of processing one symbol.

    This is the *entire* state vocabulary of the system. There is no global
    state machine; each symbol independently ends in exactly one of these.
    """

    NO_TRADE = "NO_TRADE"
    DATA_ERROR = "DATA_ERROR"
    REVIEW_REJECTED = "REVIEW_REJECTED"
    REVIEW_TIMEOUT = "REVIEW_TIMEOUT"
    REVIEW_ERROR = "REVIEW_ERROR"
    SUBMISSION_FAILED = "SUBMISSION_FAILED"
    BROKER_REJECTED = "BROKER_REJECTED"
    EXECUTION_AMBIGUOUS = "EXECUTION_AMBIGUOUS"
    ACCEPTED = "ACCEPTED"
    WORKING = "WORKING"
    FILLED = "FILLED"
    ERROR = "ERROR"


#: Outcomes meaning the order reached the venue.
#:
#: "Submitted" is about arrival, not about survival. ``BROKER_REJECTED`` belongs
#: here: the venue saw the order and refused it, which is precisely what
#: ``SUBMISSION_FAILED`` does *not* mean -- that one never left this process.
#: Excluding a venue rejection made the operator line read "Orders submitted: 0"
#: for a pass that really did put an order on the wire.
SUBMITTED_OUTCOMES = frozenset(
    {
        Outcome.ACCEPTED,
        Outcome.WORKING,
        Outcome.FILLED,
        Outcome.EXECUTION_AMBIGUOUS,
        Outcome.BROKER_REJECTED,
    }
)


class Quoted:
    """Bid/ask arithmetic, defined once for every type that carries a market.

    Two types carry the same two numbers: :class:`OptionQuote`, which the
    liquidity screen reads to *select* a contract, and :class:`ProposalLeg`,
    which travels to the reviewer to *justify* that selection. They described
    the same market with two implementations that disagreed on the degenerate
    case, so the figure shown to the reviewer was not always the figure the
    screen had applied. One definition removes the possibility.

    Mixin rather than a shared dataclass base: both subclasses are frozen,
    slotted dataclasses with different field sets, and only the derived
    properties are common to them.
    """

    __slots__ = ()

    bid: Decimal
    ask: Decimal

    @property
    def mid(self) -> Decimal:
        """Midpoint of the bid/ask spread."""
        return (self.bid + self.ask) / Decimal(2)

    @property
    def spread(self) -> Decimal:
        """Absolute bid/ask spread."""
        return self.ask - self.bid

    @property
    def spread_pct(self) -> float:
        """Bid/ask spread as a fraction of mid; infinite when mid is zero.

        This is the liquidity screen: a wide relative spread means the fill will
        be poor no matter how attractive the theoretical credit looks.

        Infinity is deliberate and load-bearing. ``tastytrade`` tests
        ``spread_pct > max_spread_pct``, so a dead book must compare *greater*
        than any configured bound and be rejected. ``None`` would raise inside
        the pure algorithm and zero would read as a perfectly tight market. The
        wire needs a JSON-safe value instead, and :func:`leg_payload` is the one
        place that converts -- a serialization rule, not a second arithmetic.
        """
        mid = self.mid
        if mid <= 0:
            return float("inf")
        return float(self.spread / mid)


@dataclass(frozen=True, slots=True)
class OptionQuote(Quoted):
    """One option contract and its current market, as seen at scan time."""

    symbol: str
    expiry: date
    strike: Decimal
    right: Right
    bid: Decimal
    ask: Decimal
    delta: float
    open_interest: int
    volume: int


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    """Everything the algorithm is allowed to see about one symbol.

    Passing a snapshot (rather than a live feed handle) into the pure algorithm
    is what keeps the functional core free of hidden clocks and network calls.
    """

    symbol: str
    underlying_price: Decimal
    iv_rank: float
    as_of: datetime
    chain: tuple[OptionQuote, ...]

    def expiries(self) -> tuple[date, ...]:
        """Distinct expiries present in the chain, ascending."""
        return tuple(sorted({q.expiry for q in self.chain}))

    def puts_for(self, expiry: date) -> tuple[OptionQuote, ...]:
        """Put quotes for one expiry, ascending by strike."""
        return tuple(
            sorted(
                (q for q in self.chain if q.expiry == expiry and q.right is Right.PUT),
                key=lambda q: q.strike,
            )
        )


@dataclass(frozen=True, slots=True)
class Position:
    """Existing exposure in one underlying.

    ``pending`` distinguishes a filled holding from an order that is still
    working at the broker. Both occupy a concentration slot: an unfilled order
    is about to become a position, so treating it as free capacity is how a
    scan loop ends up stacking duplicate orders on the same underlying while the
    first one rests.
    """

    symbol: str
    quantity: int
    description: str = ""
    pending: bool = False


@dataclass(frozen=True, slots=True)
class Portfolio:
    """Account state the algorithm consults for sizing and concentration limits."""

    net_liquidation: Decimal
    buying_power: Decimal
    positions: tuple[Position, ...] = ()

    def positions_for(self, symbol: str) -> tuple[Position, ...]:
        """Existing positions in one underlying."""
        return tuple(p for p in self.positions if p.symbol == symbol)

    def has_position(self, symbol: str) -> bool:
        """True when this underlying already has exposure.

        Counts working orders as well as filled holdings — see :class:`Position`.
        """
        return any(p.quantity != 0 for p in self.positions_for(symbol))

    @property
    def open_symbol_count(self) -> int:
        """Number of distinct underlyings currently held."""
        return len({p.symbol for p in self.positions if p.quantity != 0})


@dataclass(frozen=True, slots=True)
class ProposalLeg(Quoted):
    """One leg of a proposed spread, carrying the quote that justified it.

    The liquidity fields travel with the leg so the reviewer receives the actual
    market that was used, not a re-derived approximation of it -- and, since the
    derived figures now come from :class:`Quoted`, not a re-derived
    approximation of the *derived* numbers either.
    """

    action: Action
    right: Right
    strike: Decimal
    expiry: date
    ratio: int
    bid: Decimal
    ask: Decimal
    delta: float
    open_interest: int
    volume: int


def _new_proposal_id() -> str:
    """Fresh durable proposal identity.

    Assigned at construction, before review and before submission, so one id
    names the trade in the reviewer record, the broker ``orderRef``, and every
    persisted row.
    """
    return uuid.uuid4().hex


@dataclass(frozen=True, slots=True)
class TradeProposal:
    """A concrete, fully-priced trade the algorithm wants to place.

    A proposal existing at all is what triggers independent review; the reviewer
    is never consulted before this point.
    """

    symbol: str
    strategy: str
    expiry: date
    dte: int
    legs: tuple[ProposalLeg, ...]
    quantity: int
    limit_price: Decimal
    max_profit: Decimal
    max_loss: Decimal
    underlying_price: Decimal
    iv_rank: float
    short_delta: float
    buying_power_effect: Decimal
    criteria: Mapping[str, str]
    created_at: datetime
    proposal_id: str = field(default_factory=_new_proposal_id)

    @property
    def is_credit(self) -> bool:
        """True when the trade collects premium (the Tastytrade default posture)."""
        return self.limit_price > 0

    @property
    def total_credit(self) -> Decimal:
        """Total premium collected across all contracts, in account currency."""
        return self.limit_price * Decimal(self.quantity) * CONTRACT_MULTIPLIER


@dataclass(frozen=True, slots=True)
class NoTrade:
    """The algorithm declined this symbol, with the operator-facing reason."""

    reason: str


@dataclass(frozen=True, slots=True)
class ReviewDecision:
    """The independent reviewer's verdict on exactly one proposal."""

    approved: bool
    reason: str
    reviewer_id: str | None
    reviewed_at: datetime


@dataclass(frozen=True, slots=True)
class Fill:
    """A realized fill."""

    quantity: int
    price: Decimal
    filled_at: datetime


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """What the broker did with a submitted order."""

    outcome: Outcome
    order_ref: str
    broker_order_id: str | None = None
    message: str = ""
    fills: tuple[Fill, ...] = ()

    @property
    def filled_quantity(self) -> int:
        """Total contracts filled."""
        return sum(f.quantity for f in self.fills)


@dataclass(frozen=True, slots=True)
class SymbolResult:
    """The single record of what happened to one symbol this pass."""

    symbol: str
    outcome: Outcome
    detail: str
    proposal: TradeProposal | None = None
    review: ReviewDecision | None = None
    execution: ExecutionResult | None = None


def leg_payload(leg: ProposalLeg, *, derived: bool = False) -> dict[str, Any]:
    """The wire shape of one leg -- the only definition of it.

    Two serializers used to maintain this list of fields by hand: the reviewer's
    JSON payload and the store's ``legs_json`` column. They had already drifted
    by three keys, and nothing would have noticed if they drifted by a fourth or
    started disagreeing about how a shared one was computed.

    They are still allowed to differ in *content* -- the reviewer needs the
    derived liquidity figures to judge a fill, the audit row does not -- so
    ``derived`` selects which. What they can no longer differ in is arithmetic.

    Prices are rendered as strings so a ``Decimal`` round-trips exactly through
    JSON. ``spread_pct`` is the exception: it is genuinely a ratio, and it is
    ``None`` rather than infinity when the book is dead, because ``inf`` is not
    valid JSON. That conversion happens here and nowhere else -- the domain
    value stays infinite, which is what makes the liquidity screen reject.
    """
    payload: dict[str, Any] = {
        "action": leg.action.value,
        "right": leg.right.value,
        "strike": str(leg.strike),
        "expiry": leg.expiry.isoformat(),
        "ratio": leg.ratio,
        "bid": str(leg.bid),
        "ask": str(leg.ask),
        "delta": leg.delta,
        "open_interest": leg.open_interest,
        "volume": leg.volume,
    }
    if not derived:
        return payload

    spread_pct = leg.spread_pct
    return {
        **payload,
        "mid": str(leg.mid),
        "spread": str(leg.spread),
        "spread_pct": None if math.isinf(spread_pct) else spread_pct,
    }
