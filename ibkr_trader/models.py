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

    ``NOT_SELECTED`` is the one outcome decided across symbols rather than
    within one: the algorithm proposed a trade, but candidates ranked above it
    took every free position slot this pass. It was neither reviewed nor
    submitted, and it is not a ``NO_TRADE`` -- the trade existed.
    """

    NO_TRADE = "NO_TRADE"
    NOT_SELECTED = "NOT_SELECTED"
    DATA_ERROR = "DATA_ERROR"
    REVIEW_REJECTED = "REVIEW_REJECTED"
    REVIEW_TIMEOUT = "REVIEW_TIMEOUT"
    REVIEW_ERROR = "REVIEW_ERROR"
    SUBMISSION_FAILED = "SUBMISSION_FAILED"
    BROKER_REJECTED = "BROKER_REJECTED"
    AWAITING_DECISION = "AWAITING_DECISION"
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

    #: The venue's trading class for the quoted chain, when it reported one.
    #:
    #: Empty means "not reported", which is not the same as "standard" and must
    #: not be read as evidence of either. A value differing from ``symbol``
    #: names a *non-standard* class -- an adjusted contract left behind by a
    #: split or a special dividend -- whose deliverable differs from the
    #: standard option at the same strike and expiry. It travels on the snapshot
    #: because the quote and the order must refer to the same instrument, and
    #: today only the quote side knows which one it is.
    trading_class: str = ""

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

    #: False when the venue's working-order stream could not be read.
    #:
    #: Both concentration guards key on rows synthesized from still-working
    #: orders. If that read fails and the failure is not carried here, the
    #: resulting portfolio is indistinguishable from one that genuinely has no
    #: working orders -- and the guards are then *skipped* rather than failed,
    #: silently. This flag is the whole difference between a decision the
    #: algorithm may make alone and one it may not.
    pending_orders_known: bool = True

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
class NeedsDecision:
    """The algorithm cannot rule on this symbol safely and is asking a human.

    Distinct from :class:`NoTrade` in the only way that matters: a ``NoTrade``
    is a decision, and this is the absence of one. Declining to trade because a
    guard refused is a fact about the market; declining because a guard could
    not be evaluated is a fact about the system, and the two need different
    responses from whoever is reading.

    ``proposal`` is the trade that would have been submitted, carried as
    *context for the ruling* rather than as a commitment to submit it. A quote
    decays in minutes, so an approval that arrives later authorises proceeding
    on a fresh evaluation of this symbol -- never replaying this exact order at
    this exact price.
    """

    symbol: str
    reason: str
    proposal: TradeProposal | None = None


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


# --- management: what the engine holds and how it works it ------------------


class Tif(str, Enum):
    """Time in force. ``GTC`` is used only by the resting profit-target order."""

    DAY = "DAY"
    GTC = "GTC"


@dataclass(frozen=True, slots=True)
class OptionLeg:
    """Identity of one listed option contract, independent of any quote."""

    symbol: str
    expiry: date
    strike: Decimal
    right: Right


@dataclass(frozen=True, slots=True)
class OptionPosition:
    """One option contract the account holds, as the venue reports it.

    ``quantity`` is signed: negative is short. ``average_cost`` is per contract
    in account currency as the venue reports it, and may be zero when the venue
    does not supply one; nothing here is derived from it.
    """

    leg: OptionLeg
    quantity: int
    average_cost: Decimal = Decimal(0)


@dataclass(frozen=True, slots=True)
class ComboLeg:
    """One leg of a combo order: which contract, and which way."""

    leg: OptionLeg
    action: Action
    ratio: int = 1


@dataclass(frozen=True, slots=True)
class ComboOrder:
    """A multi-leg order to place, in the same price convention as a proposal.

    ``limit_price`` is per spread, per share: positive is a credit received,
    negative is a debit paid. The broker encodes it for the venue exactly as it
    does a proposal's price (a ``BUY`` of the bag at the negated figure), so an
    opening credit spread and its closing debit order go through one path.

    ``order_ref`` is stamped on the venue order before transmission, so any
    order that can exist at the venue can be found again by reference.
    ``purpose`` names why the order exists (``OPEN``, ``PROFIT_TARGET``,
    ``CLOSE``, ``ROLL_CLOSE``, ``ROLL_OPEN``); it is recorded, never branched on
    by the broker.
    """

    symbol: str
    legs: tuple[ComboLeg, ...]
    quantity: int
    limit_price: Decimal
    tif: Tif
    order_ref: str
    purpose: str

    @property
    def is_credit(self) -> bool:
        return self.limit_price > 0


class SpreadStatus(str, Enum):
    """Lifecycle of one spread the engine opened.

    OPEN: filled, no closing order resting yet.
    PROFIT_ORDER_RESTING: a GTC buy-back at the profit target is working.
    CLOSING: a closing order was sent (profit target abandoned or unavailable).
    ROLLING: the closing half of a roll was sent; the opening half follows once
        the legs are gone.
    CLOSED: the legs are no longer held.
    NEEDS_DECISION: the engine declines to act alone; a human must rule.
    """

    OPEN = "OPEN"
    PROFIT_ORDER_RESTING = "PROFIT_ORDER_RESTING"
    CLOSING = "CLOSING"
    ROLLING = "ROLLING"
    CLOSED = "CLOSED"
    NEEDS_DECISION = "NEEDS_DECISION"


#: Statuses under which the spread still occupies the book.
LIVE_SPREAD_STATUSES = frozenset(
    {
        SpreadStatus.OPEN,
        SpreadStatus.PROFIT_ORDER_RESTING,
        SpreadStatus.CLOSING,
        SpreadStatus.ROLLING,
        SpreadStatus.NEEDS_DECISION,
    }
)


@dataclass(frozen=True, slots=True)
class Spread:
    """A short put vertical the engine opened and is responsible for.

    ``spread_id`` is the opening proposal's id, so the record of why the trade
    was made and the record of how it was managed share one key.
    ``open_credit`` is per spread, per share (a ``1.75`` credit), taken from
    the fill when the venue reported one and from the order's limit otherwise.
    """

    spread_id: str
    symbol: str
    expiry: date
    short_strike: Decimal
    long_strike: Decimal
    quantity: int
    open_credit: Decimal
    opened_at: datetime
    status: SpreadStatus
    profit_order_ref: str | None = None
    closing_order_ref: str | None = None
    closed_at: datetime | None = None
    close_price: Decimal | None = None
    close_reason: str = ""

    @property
    def width(self) -> Decimal:
        return self.short_strike - self.long_strike

    @property
    def short_leg(self) -> OptionLeg:
        return OptionLeg(self.symbol, self.expiry, self.short_strike, Right.PUT)

    @property
    def long_leg(self) -> OptionLeg:
        return OptionLeg(self.symbol, self.expiry, self.long_strike, Right.PUT)

    @property
    def max_profit(self) -> Decimal:
        return self.open_credit * CONTRACT_MULTIPLIER * Decimal(self.quantity)


@dataclass(frozen=True, slots=True)
class OpeningOrder:
    """What the store knows about an opening order that reached the venue.

    The manager reconciles these against the account's option positions to
    discover fills the submission window did not see. ``fill_price`` is the
    average fill per spread when fills were recorded, else ``None``.
    """

    proposal_id: str
    symbol: str
    expiry: date
    short_strike: Decimal
    long_strike: Decimal
    quantity: int
    limit_price: Decimal
    filled_quantity: int
    fill_price: Decimal | None
    recorded_at: datetime


class ManagementKind(str, Enum):
    """Every distinct thing the manager can do or observe, one per record."""

    RECONCILED_FILL = "RECONCILED_FILL"
    OPENING_WORKING = "OPENING_WORKING"
    OPENING_UNFILLED = "OPENING_UNFILLED"
    PROFIT_TARGET_PLACED = "PROFIT_TARGET_PLACED"
    PROFIT_TARGET_FILLED = "PROFIT_TARGET_FILLED"
    PROFIT_TARGET_CANCELLED = "PROFIT_TARGET_CANCELLED"
    ROLL_CLOSE = "ROLL_CLOSE"
    ROLL_OPEN = "ROLL_OPEN"
    ROLL_DECLINED = "ROLL_DECLINED"
    CLOSE = "CLOSE"
    CLOSED = "CLOSED"
    UNMANAGED_POSITION = "UNMANAGED_POSITION"
    NEEDS_DECISION = "NEEDS_DECISION"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class ManagementAction:
    """One recorded management step for one spread (or one stray position)."""

    symbol: str
    kind: ManagementKind
    detail: str
    spread_id: str | None = None
    order: ComboOrder | None = None
    execution: ExecutionResult | None = None


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
