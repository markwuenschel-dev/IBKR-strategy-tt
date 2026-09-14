"""Phase A of every pass: work the spreads the engine already holds.

The runner opens positions; this module is what happens to them afterwards.
It runs before the scan, once per pass, and does three things in order:

1. **Reconcile fills.** An opening order that reached the venue but had not
   filled when the pass that sent it ended is matched against the account's
   option positions. Both legs held with the right signs means the spread is
   open, and it gets a durable :class:`~ibkr_trader.models.Spread` row.
2. **Manage every live spread.** Tastytrade mechanics, nothing more: a GTC
   buy-back rests at half the credit from the day the spread is open; at
   ``manage_dte`` days to expiration the profit order is pulled and the spread
   is rolled for a net credit when one exists, otherwise closed.
3. **Report strays.** A held option leg that belongs to no live spread is
   recorded as ``UNMANAGED_POSITION`` and left alone.

Decisions are pure functions over what was read at the top of the run --
positions, working order references, quotes, the clock -- and every effect
goes through a port. Each spread is processed inside its own isolation
boundary, the same policy as the runner's per-symbol one: a failure on one
spread is recorded as ``ERROR`` for that spread and the next is processed.

One invariant is enforced in code rather than by convention: **no order placed
here ever adds contracts or widens a spread.** :func:`added_risk` is the named
check, and :meth:`Manager._place` refuses an order that fails it before the
broker is asked.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from decimal import ROUND_UP, Decimal

from . import tastytrade
from .clock import Clock, market_date
from .config import RunConfig
from .errors import (
    BrokerNotConnected,
    ExecutionAmbiguous,
    MarketDataError,
    ReviewError,
    ReviewTimeout,
    SubmissionFailed,
)
from .models import (
    Action,
    ComboLeg,
    ComboOrder,
    ExecutionResult,
    ManagementAction,
    ManagementKind,
    OpeningOrder,
    OptionLeg,
    OptionPosition,
    OptionQuote,
    Outcome,
    Portfolio,
    ReviewDecision,
    Right,
    Spread,
    SpreadStatus,
    SymbolResult,
    Tif,
    TradeProposal,
)
from .ports import Broker, MarketData, Reviewer, Store
from .tastytrade import TICK

log = logging.getLogger(__name__)

#: The symbol a run-wide failure is recorded under, when no spread was reached.
RUN_WIDE = "*"

#: Outcomes under which a management order is resting at the venue and must be
#: reconciled by a later pass. ``EXECUTION_AMBIGUOUS`` belongs here: the order
#: may be live, and the only safe assumption is that it is.
_RESTING_OUTCOMES = frozenset({Outcome.ACCEPTED, Outcome.WORKING, Outcome.EXECUTION_AMBIGUOUS})


class RiskIncreaseRefused(Exception):
    """A management order would have added contracts or width. Never sent."""


def round_up_to_tick(value: Decimal) -> Decimal:
    """Round a debit *up* to the tick.

    The opposite direction from :func:`tastytrade._round_credit`, for the
    opposite reason: a debit rounded down is a price we might not get filled
    at, and a buy-back a cent above the target is still "about half".
    """
    return value.quantize(TICK, rounding=ROUND_UP)


def added_risk(spread: Spread, quantity: int, width: Decimal) -> str | None:
    """Why an order of ``quantity`` contracts on a ``width``-wide spread adds risk.

    The named check behind the module's one hard invariant. ``None`` means the
    order is no larger than the spread it manages; anything else is the reason
    it must not be sent.
    """
    if quantity < 1:
        return f"quantity {quantity} is not a positive number of contracts"
    if quantity > spread.quantity:
        return f"quantity {quantity} exceeds the {spread.quantity} contract(s) held"
    if width > spread.width:
        return f"width {width} exceeds the {spread.width}-wide spread held"
    return None


def _width_of(legs: tuple[ComboLeg, ...]) -> Decimal:
    strikes = [leg.leg.strike for leg in legs]
    return max(strikes) - min(strikes)


@dataclass(frozen=True, slots=True)
class ManagementSummary:
    """What phase A did this pass, derived from the recorded actions."""

    run_id: str
    actions: tuple[ManagementAction, ...]

    def count(self, *kinds: ManagementKind) -> int:
        wanted = set(kinds)
        return sum(1 for a in self.actions if a.kind in wanted)

    @property
    def spreads_touched(self) -> int:
        return len({a.spread_id for a in self.actions if a.spread_id is not None})

    @property
    def errors(self) -> int:
        return self.count(ManagementKind.ERROR)

    def render(self) -> str:
        """The operator summary, one line per kind that occurred."""
        labels = (
            (ManagementKind.RECONCILED_FILL, "Fills reconciled"),
            (ManagementKind.PROFIT_TARGET_PLACED, "Profit targets placed"),
            (ManagementKind.PROFIT_TARGET_FILLED, "Profit targets filled"),
            (ManagementKind.PROFIT_TARGET_CANCELLED, "Profit targets cancelled"),
            (ManagementKind.ROLL_CLOSE, "Roll closes sent"),
            (ManagementKind.ROLL_OPEN, "Roll opens sent"),
            (ManagementKind.ROLL_DECLINED, "Rolls declined"),
            (ManagementKind.CLOSE, "Closes sent"),
            (ManagementKind.CLOSED, "Spreads closed"),
            (ManagementKind.NEEDS_DECISION, "Needs decision"),
            (ManagementKind.UNMANAGED_POSITION, "Unmanaged positions"),
            (ManagementKind.ERROR, "Management errors"),
        )
        lines = [f"Managed: {self.spreads_touched} spread(s), {len(self.actions)} action(s)"]
        for kind, label in labels:
            n = self.count(kind)
            if n:
                lines.append(f"{label}: {n}")
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class _Held:
    """Signed quantities of a spread's two legs, as the venue reports them."""

    short: int
    long: int

    @property
    def gone(self) -> bool:
        return self.short == 0 and self.long == 0

    @property
    def intact(self) -> bool:
        """Short leg short, long leg long: the spread as it was opened."""
        return self.short < 0 and self.long > 0

    @property
    def quantity(self) -> int:
        """Whole spreads held, when the legs are intact."""
        return min(-self.short, self.long)


Emit = Callable[[ManagementAction], None]


class Manager:
    """Works the open spreads. Owns phase A, and nothing else does."""

    def __init__(
        self,
        config: RunConfig,
        market_data: MarketData,
        reviewer: Reviewer,
        broker: Broker,
        store: Store,
        clock: Clock,
    ) -> None:
        self._config = config
        self._market_data = market_data
        self._reviewer = reviewer
        self._broker = broker
        self._store = store
        self._clock = clock

    # --- the run ---------------------------------------------------------

    def run(self, run_id: str) -> ManagementSummary:
        """Reconcile, manage, and report, for every spread on the book.

        Raises:
            MarketDataError: the position stream could not be read. Nothing
                can be managed without it, so this escapes to the caller
                rather than being recorded against any one spread.
            BrokerError: the working-order stream could not be read; same.
        """
        actions: list[ManagementAction] = []

        def emit(action: ManagementAction) -> None:
            self._record(action, run_id)
            actions.append(action)
            log.info("%-6s %-24s %s", action.symbol, action.kind.value, action.detail)

        held = _index_positions(self._market_data.option_positions())
        refs = self._broker.working_order_refs()

        # 1. fills the submitting pass did not see
        for opening in self._store.unreconciled_openings():
            self._isolated(
                opening.symbol,
                opening.proposal_id,
                lambda opening=opening: self._reconcile(opening, held, refs, emit),
                emit,
            )

        # 2. every live spread, including the ones reconciled a moment ago
        spreads = self._store.live_spreads()
        for spread in spreads:
            self._isolated(
                spread.symbol,
                spread.spread_id,
                lambda spread=spread: self._manage(spread, held, refs, run_id, emit),
                emit,
            )

        # 3. what the account holds that none of the above explains
        managed = {leg for s in spreads for leg in (s.short_leg, s.long_leg)}
        for symbol, legs in _strays(held, managed).items():
            emit(
                ManagementAction(
                    symbol,
                    ManagementKind.UNMANAGED_POSITION,
                    "held option legs belong to no spread the engine opened: "
                    + ", ".join(legs),
                )
            )

        return ManagementSummary(run_id=run_id, actions=tuple(actions))

    def _isolated(
        self, symbol: str, spread_id: str, step: Callable[[], None], emit: Emit
    ) -> None:
        """One spread's step inside its isolation boundary.

        The same deliberate ``except Exception`` as ``Runner._isolated``, for
        the same reason: one spread's unexpected failure must not leave the
        rest of the book unmanaged. The traceback is logged in full and the
        spread gets a recorded ``ERROR``.
        """
        try:
            step()
        except Exception as exc:  # noqa: BLE001 - documented isolation boundary
            log.exception("unhandled error managing %s (spread %s)", symbol, spread_id)
            emit(
                ManagementAction(
                    symbol,
                    ManagementKind.ERROR,
                    f"{type(exc).__name__}: {exc}",
                    spread_id=spread_id,
                )
            )
        except BaseException as exc:
            # KeyboardInterrupt is not an Exception, so the boundary above
            # cannot catch it. A step here may be mid-placement, with an order
            # already live at the venue; escaping before anything was recorded
            # would leave the book with no trace of that. Record what is known,
            # then let the signal continue unwinding -- the same rule the
            # runner applies to an interrupted submission.
            log.warning("interrupted while managing %s (spread %s)", symbol, spread_id)
            emit(
                ManagementAction(
                    symbol,
                    ManagementKind.ERROR,
                    f"interrupted while managing: {type(exc).__name__}; "
                    f"an order for this spread may be live at the venue",
                    spread_id=spread_id,
                )
            )
            raise

    # --- 1. reconciliation -----------------------------------------------

    def _report_unfilled(
        self, opening: OpeningOrder, refs: frozenset[str], emit: Emit
    ) -> None:
        """Say what became of an opening order whose legs are not held.

        Two states, and the difference between them is the whole point: an
        order still in the broker's working set is resting and may yet fill,
        while one absent from that set on a *later* session date is a DAY
        limit (``broker.py`` builds every opening order with ``Tif.DAY``) that
        expired unfilled and never will.

        Neither state used to be reported at all. That silence is what makes a
        liquidity screen admitting spreads nobody will fill look identical, from
        the outside, to a screen admitting nothing -- no positions either way.

        The session date has to agree before anything is marked, because
        ``working_order_refs`` sees only this client's orders
        (``scanner._pending_positions`` reads ``openTrades()``, not
        ``reqAllOpenOrders``). A restarted TWS empties that set while the order
        is still live, and marking it then would stop its eventual fill from
        ever being reconciled.
        """
        now = self._clock.now()
        placed_on = market_date(opening.recorded_at)
        legs = f"{opening.short_strike}/{opening.long_strike} {opening.expiry.isoformat()}"
        if opening.proposal_id in refs or placed_on >= market_date(now):
            resting = (now - opening.recorded_at).total_seconds() / 3600
            emit(
                ManagementAction(
                    opening.symbol,
                    ManagementKind.OPENING_WORKING,
                    f"{opening.quantity}x {legs} resting at {opening.limit_price} "
                    f"for {resting:.1f}h, not filled",
                    spread_id=opening.proposal_id,
                )
            )
            return
        detail = (
            f"{opening.quantity}x {legs} at {opening.limit_price} never filled; "
            f"the DAY order placed {placed_on.isoformat()} is no longer working"
        )
        self._store.record_abandoned_opening(opening.proposal_id, detail)
        emit(
            ManagementAction(
                opening.symbol,
                ManagementKind.OPENING_UNFILLED,
                detail,
                spread_id=opening.proposal_id,
            )
        )

    def _reconcile(
        self,
        opening: OpeningOrder,
        held: Mapping[OptionLeg, int],
        refs: frozenset[str],
        emit: Emit,
    ) -> None:
        short_leg = OptionLeg(opening.symbol, opening.expiry, opening.short_strike, Right.PUT)
        long_leg = OptionLeg(opening.symbol, opening.expiry, opening.long_strike, Right.PUT)
        legs = _Held(held.get(short_leg, 0), held.get(long_leg, 0))
        if not legs.intact:
            self._report_unfilled(opening, refs, emit)
            return

        quantity = min(legs.quantity, opening.quantity)
        credit = opening.fill_price if opening.fill_price is not None else opening.limit_price
        spread = Spread(
            spread_id=opening.proposal_id,
            symbol=opening.symbol,
            expiry=opening.expiry,
            short_strike=opening.short_strike,
            long_strike=opening.long_strike,
            quantity=quantity,
            open_credit=credit,
            opened_at=self._clock.now(),
            status=SpreadStatus.OPEN,
        )
        self._store.record_spread(spread)
        source = "fill" if opening.fill_price is not None else "limit"
        emit(
            ManagementAction(
                spread.symbol,
                ManagementKind.RECONCILED_FILL,
                f"{quantity}x {_describe_legs(spread)} held; open credit {credit} ({source})",
                spread_id=spread.spread_id,
            )
        )

    # --- 2. one spread ---------------------------------------------------

    def _manage(
        self,
        spread: Spread,
        held: Mapping[OptionLeg, int],
        refs: frozenset[str],
        run_id: str,
        emit: Emit,
    ) -> None:
        if spread.status is SpreadStatus.NEEDS_DECISION:
            # A human owns this one. The engine does not touch it, even to
            # notice that it is gone.
            log.debug("%s: spread %s awaits a human decision", spread.symbol, spread.spread_id)
            return

        legs = _Held(held.get(spread.short_leg, 0), held.get(spread.long_leg, 0))

        if legs.gone:
            self._legs_gone(spread, refs, run_id, emit)
            return

        if not legs.intact:
            self._needs_decision(
                spread,
                f"legs held unevenly: short {spread.short_strike}P x{legs.short}, "
                f"long {spread.long_strike}P x{legs.long}; the engine will not act alone",
                emit,
            )
            return

        quantity = min(spread.quantity, legs.quantity)
        if quantity < spread.quantity:
            log.warning(
                "%s: spread %s records %d contract(s) but %d are held; managing %d",
                spread.symbol,
                spread.spread_id,
                spread.quantity,
                legs.quantity,
                quantity,
            )

        if (
            spread.status in (SpreadStatus.CLOSING, SpreadStatus.ROLLING)
            and spread.closing_order_ref in refs
        ):
            return  # the closing order is still working; nothing to add
        if spread.status in (SpreadStatus.CLOSING, SpreadStatus.ROLLING):
            # The DAY order lapsed without filling. The record says a close is
            # resting; the venue says nothing is. Make the record true first.
            log.info(
                "%s: closing order %s for spread %s is no longer working",
                spread.symbol,
                spread.closing_order_ref,
                spread.spread_id,
            )
            spread = replace(spread, status=SpreadStatus.OPEN, closing_order_ref=None)
            self._store.record_spread(spread)

        dte = (spread.expiry - market_date(self._clock.now())).days
        if dte > self._config.management.manage_dte:
            self._ensure_profit_target(spread, quantity, refs, emit)
        else:
            self._manage_at_dte(spread, quantity, dte, refs, run_id, emit)

    def _legs_gone(
        self, spread: Spread, refs: frozenset[str], run_id: str, emit: Emit
    ) -> None:
        """Neither leg is held any more: the book says the spread is closed."""
        if spread.status is SpreadStatus.PROFIT_ORDER_RESTING:
            if spread.profit_order_ref is not None and spread.profit_order_ref not in refs:
                self._close_record(spread, "profit target filled", emit)
                emit(
                    ManagementAction(
                        spread.symbol,
                        ManagementKind.PROFIT_TARGET_FILLED,
                        f"profit order {spread.profit_order_ref} is no longer working "
                        f"and the legs are gone",
                        spread_id=spread.spread_id,
                    )
                )
                return
            # The legs are gone but the buy-back still rests: someone closed
            # the spread another way. Left working, that order would *open* a
            # long put spread the moment it filled.
            self._cancel_profit_target(spread, emit)
            self._close_record(spread, "legs no longer held", emit)
            return

        if spread.status is SpreadStatus.CLOSING:
            self._close_record(spread, "closed", emit)
            return

        if spread.status is SpreadStatus.ROLLING:
            # ``close_price`` was written when the closing half was sent, so
            # the opening half can still ask whether the roll nets a credit.
            debit = spread.close_price if spread.close_price is not None else Decimal(0)
            closed = self._close_record(spread, "roll close filled", emit, close_price=debit)
            # The closing half is done; the opening half runs fresh, because
            # the proposal the closing half was made on is a pass old.
            self._roll_open(closed, closed.quantity, debit, run_id, emit)
            return

        self._close_record(spread, "legs no longer held", emit)

    def _ensure_profit_target(
        self, spread: Spread, quantity: int, refs: frozenset[str], emit: Emit
    ) -> None:
        """A GTC buy-back at the target rests from the day the spread is open."""
        if (
            spread.status is SpreadStatus.PROFIT_ORDER_RESTING
            and spread.profit_order_ref in refs
        ):
            return
        if spread.status is SpreadStatus.PROFIT_ORDER_RESTING:
            log.warning(
                "%s: profit order %s for spread %s vanished while the legs are held; "
                "placing it again",
                spread.symbol,
                spread.profit_order_ref,
                spread.spread_id,
            )

        ratio = Decimal(str(self._config.management.profit_target_ratio))
        target = round_up_to_tick(spread.open_credit * ratio)
        order = ComboOrder(
            symbol=spread.symbol,
            legs=_closing_legs(spread),
            quantity=quantity,
            limit_price=-target,
            tif=Tif.GTC,
            order_ref=uuid.uuid4().hex,
            purpose="PROFIT_TARGET",
        )
        execution = self._place(spread, order)
        emit(
            ManagementAction(
                spread.symbol,
                ManagementKind.PROFIT_TARGET_PLACED,
                f"GTC buy-back {quantity}x {_describe_legs(spread)} @ {target} debit "
                f"({ratio:.0%} of {spread.open_credit} credit): {_outcome_text(execution)}",
                spread_id=spread.spread_id,
                order=order,
                execution=execution,
            )
        )
        if execution.outcome is Outcome.FILLED:
            self._close_record(
                replace(spread, profit_order_ref=order.order_ref),
                "profit target filled",
                emit,
                close_price=target,
            )
            emit(
                ManagementAction(
                    spread.symbol,
                    ManagementKind.PROFIT_TARGET_FILLED,
                    f"filled on submission at {target} debit",
                    spread_id=spread.spread_id,
                    order=order,
                    execution=execution,
                )
            )
        elif execution.outcome in _RESTING_OUTCOMES:
            self._store.record_spread(
                replace(
                    spread,
                    status=SpreadStatus.PROFIT_ORDER_RESTING,
                    profit_order_ref=order.order_ref,
                )
            )
        else:
            # SUBMISSION_FAILED / BROKER_REJECTED: recorded above; the spread
            # stays OPEN and the next pass tries again.
            self._store.record_spread(replace(spread, status=SpreadStatus.OPEN))

    def _manage_at_dte(
        self,
        spread: Spread,
        quantity: int,
        dte: int,
        refs: frozenset[str],
        run_id: str,
        emit: Emit,
    ) -> None:
        """At ``manage_dte`` the spread stops being held: roll for a credit, or close."""
        if (
            spread.status is SpreadStatus.PROFIT_ORDER_RESTING
            and spread.profit_order_ref in refs
        ):
            cancelled = self._cancel_profit_target(spread, emit)
            if not cancelled:
                # It was working a moment ago and cannot be cancelled now: it
                # has most likely just filled, and a close on top of a fill
                # would open the opposite spread. Stop; the next pass
                # reconciles against the positions.
                return
        spread = replace(spread, status=SpreadStatus.OPEN, profit_order_ref=None)
        self._store.record_spread(spread)

        short_quote, long_quote = self._market_data.quote([spread.short_leg, spread.long_leg])
        dead = [q for q in (short_quote, long_quote) if _dead_book(q)]
        if dead:
            self._needs_decision(
                spread,
                f"{dte} DTE and no usable market to close on: "
                + "; ".join(f"{q.strike}P bid {q.bid} ask {q.ask}" for q in dead),
                emit,
            )
            return
        raw_debit = short_quote.mid - long_quote.mid
        if raw_debit <= 0:
            self._needs_decision(
                spread,
                f"{dte} DTE and the legs quote at no net debit "
                f"({short_quote.mid} short mid, {long_quote.mid} long mid)",
                emit,
            )
            return
        debit = round_up_to_tick(raw_debit)

        if self._config.management.roll:
            if self._roll(spread, quantity, dte, debit, run_id, emit):
                return
        else:
            emit(
                ManagementAction(
                    spread.symbol,
                    ManagementKind.ROLL_DECLINED,
                    f"{dte} DTE; rolling is disabled (management.roll = false)",
                    spread_id=spread.spread_id,
                )
            )

        self._close(spread, quantity, dte, debit, emit)

    # --- rolling ---------------------------------------------------------

    def _roll(
        self,
        spread: Spread,
        quantity: int,
        dte: int,
        debit: Decimal,
        run_id: str,
        emit: Emit,
    ) -> bool:
        """Try to roll. True when the roll is under way and the close must not run.

        False means the roll was declined -- recorded, with the reason -- and
        the caller falls through to the plain close.
        """
        prepared = self._roll_proposal(spread, quantity, debit, emit)
        if prepared is None:
            return False
        proposal, portfolio = prepared

        review = self._review_roll(spread, proposal, portfolio, emit)
        if review is None:
            return False

        close = ComboOrder(
            symbol=spread.symbol,
            legs=_closing_legs(spread),
            quantity=proposal.quantity,
            limit_price=-debit,
            tif=Tif.DAY,
            order_ref=uuid.uuid4().hex,
            purpose="ROLL_CLOSE",
        )
        execution = self._place(spread, close)
        emit(
            ManagementAction(
                spread.symbol,
                ManagementKind.ROLL_CLOSE,
                f"{dte} DTE; buy back {proposal.quantity}x {_describe_legs(spread)} "
                f"@ {debit} debit to roll into {_describe_proposal(proposal)}: "
                f"{_outcome_text(execution)}",
                spread_id=spread.spread_id,
                order=close,
                execution=execution,
            )
        )
        if execution.outcome is Outcome.FILLED:
            remainder = spread.quantity - proposal.quantity
            if remainder <= 0:
                self._close_record(spread, "rolled", emit, close_price=debit)
            else:
                # Part of the spread was rolled; the rest is still held and is
                # managed again next pass, bounded by what the venue reports.
                log.info(
                    "%s: %d of %d contract(s) rolled; %d remain held",
                    spread.symbol,
                    proposal.quantity,
                    spread.quantity,
                    remainder,
                )
            self._roll_open(spread, proposal.quantity, debit, run_id, emit, proposal, review)
            return True
        if execution.outcome in _RESTING_OUTCOMES:
            self._store.record_spread(
                replace(
                    spread,
                    status=SpreadStatus.ROLLING,
                    closing_order_ref=close.order_ref,
                    close_price=debit,
                )
            )
            return True
        # Refused before or at the venue. Recorded; the spread stays OPEN and
        # the next pass tries again from the top.
        return True

    def _roll_proposal(
        self, spread: Spread, quantity: int, debit: Decimal, emit: Emit
    ) -> tuple[TradeProposal, Portfolio] | None:
        """The next-cycle spread this one would roll into, sized to it.

        The algorithm is asked exactly as the scan asks it, except that this
        symbol's own positions are hidden from it: the roll *replaces* them,
        so the duplicate guard must not refuse on their account.
        """

        def declined(reason: str) -> None:
            emit(
                ManagementAction(
                    spread.symbol,
                    ManagementKind.ROLL_DECLINED,
                    reason,
                    spread_id=spread.spread_id,
                )
            )

        try:
            snapshot = self._market_data.snapshot(spread.symbol)
            portfolio = self._market_data.portfolio()
        except MarketDataError as exc:
            declined(f"no roll: {exc}")
            return None
        portfolio = replace(
            portfolio,
            positions=tuple(p for p in portfolio.positions if p.symbol != spread.symbol),
        )
        decision = tastytrade.evaluate(
            symbol=spread.symbol,
            snapshot=snapshot,
            portfolio=portfolio,
            strategy=self._config.strategy,
            risk=self._config.risk,
            now=self._clock.now(),
        )
        if not isinstance(decision, TradeProposal):
            declined(f"no roll: {decision.reason}")
            return None
        if decision.quantity < 1:
            declined("no roll: the next cycle sizes to zero contracts")
            return None

        roll_quantity = min(quantity, decision.quantity)
        net = decision.limit_price - debit
        if net <= 0:
            declined(
                f"no roll: {_describe_proposal(decision)} pays {decision.limit_price} "
                f"against a {debit} debit to close (net {net})"
            )
            return None
        width = _width_of(_opening_legs(decision))
        reason = added_risk(spread, roll_quantity, width)
        if reason is not None:
            declined(f"no roll: {reason}")
            return None

        proposal = _sized(decision, roll_quantity)
        proposal = replace(
            proposal,
            criteria={
                **proposal.criteria,
                "roll": (
                    f"replaces spread {spread.spread_id} ({_describe_legs(spread)}) "
                    f"closed at {debit} debit; net credit {net}"
                ),
            },
        )
        return proposal, portfolio

    def _review_roll(
        self, spread: Spread, proposal: TradeProposal, portfolio: Portfolio, emit: Emit
    ) -> ReviewDecision | None:
        """One independent review of the roll, fail-closed like the runner's."""
        try:
            review = self._reviewer.review(proposal, portfolio)
        except (ReviewTimeout, ReviewError) as exc:
            reason = f"no roll: review {type(exc).__name__}: {exc}"
            review = None
        else:
            reason = (
                None if review.approved else f"no roll: reviewer rejected: {review.reason}"
            )
        if reason is not None:
            emit(
                ManagementAction(
                    spread.symbol,
                    ManagementKind.ROLL_DECLINED,
                    reason,
                    spread_id=spread.spread_id,
                )
            )
            return None
        return review

    def _roll_open(
        self,
        spread: Spread,
        quantity: int,
        debit: Decimal,
        run_id: str,
        emit: Emit,
        proposal: TradeProposal | None = None,
        review: ReviewDecision | None = None,
    ) -> None:
        """The opening half of a roll, once the closing half has filled.

        Called with a proposal and review when the close filled in this pass,
        and without them when the close filled between passes: the earlier
        proposal is stale by then, so the evaluation and review run again.
        """
        if proposal is None or review is None:
            prepared = self._roll_proposal(spread, quantity, debit, emit)
            if prepared is None:
                return
            proposal, portfolio = prepared
            review = self._review_roll(spread, proposal, portfolio, emit)
            if review is None:
                return

        order = ComboOrder(
            symbol=spread.symbol,
            legs=_opening_legs(proposal),
            quantity=proposal.quantity,
            limit_price=proposal.limit_price,
            tif=Tif.DAY,
            order_ref=proposal.proposal_id,
            purpose="ROLL_OPEN",
        )
        execution = self._place(spread, order)
        emit(
            ManagementAction(
                spread.symbol,
                ManagementKind.ROLL_OPEN,
                f"open {_describe_proposal(proposal)}: {_outcome_text(execution)}",
                spread_id=spread.spread_id,
                order=order,
                execution=execution,
            )
        )
        # The opening half is an opening order like any other: it goes into the
        # orders/fills record, which is what reconciliation reads next pass.
        result = SymbolResult(
            spread.symbol,
            execution.outcome,
            f"roll of spread {spread.spread_id}: {_outcome_text(execution)}",
            proposal=proposal,
            review=review,
            execution=execution,
        )
        try:
            self._store.record(result, run_id)
        except Exception:  # noqa: BLE001 - logged, never silent; the runner's policy
            log.exception(
                "failed to record roll-open %s for %s", proposal.proposal_id, spread.symbol
            )
        if execution.outcome is Outcome.FILLED:
            filled = execution.filled_quantity or proposal.quantity
            credit = _average_fill(execution) or proposal.limit_price
            self._store.record_spread(
                Spread(
                    spread_id=proposal.proposal_id,
                    symbol=proposal.symbol,
                    expiry=proposal.expiry,
                    short_strike=_strike(proposal, Action.SELL),
                    long_strike=_strike(proposal, Action.BUY),
                    quantity=min(filled, proposal.quantity),
                    open_credit=credit,
                    opened_at=self._clock.now(),
                    status=SpreadStatus.OPEN,
                )
            )

    # --- closing ---------------------------------------------------------

    def _close(
        self, spread: Spread, quantity: int, dte: int, debit: Decimal, emit: Emit
    ) -> None:
        order = ComboOrder(
            symbol=spread.symbol,
            legs=_closing_legs(spread),
            quantity=quantity,
            limit_price=-debit,
            tif=Tif.DAY,
            order_ref=uuid.uuid4().hex,
            purpose="CLOSE",
        )
        execution = self._place(spread, order)
        emit(
            ManagementAction(
                spread.symbol,
                ManagementKind.CLOSE,
                f"{dte} DTE; buy back {quantity}x {_describe_legs(spread)} @ {debit} debit: "
                f"{_outcome_text(execution)}",
                spread_id=spread.spread_id,
                order=order,
                execution=execution,
            )
        )
        if execution.outcome is Outcome.FILLED:
            self._close_record(spread, "closed", emit, close_price=debit)
        elif execution.outcome in _RESTING_OUTCOMES:
            self._store.record_spread(
                replace(spread, status=SpreadStatus.CLOSING, closing_order_ref=order.order_ref)
            )

    def _cancel_profit_target(self, spread: Spread, emit: Emit) -> bool:
        ref = spread.profit_order_ref
        if ref is None:
            return True
        cancelled = self._broker.cancel(ref)
        found = "a working order was cancelled" if cancelled else "no working order found"
        emit(
            ManagementAction(
                spread.symbol,
                ManagementKind.PROFIT_TARGET_CANCELLED,
                f"cancel {ref}: {found} (cancelled={cancelled})",
                spread_id=spread.spread_id,
            )
        )
        return cancelled

    def _close_record(
        self, spread: Spread, reason: str, emit: Emit, close_price: Decimal | None = None
    ) -> Spread:
        closed = replace(
            spread,
            status=SpreadStatus.CLOSED,
            closed_at=self._clock.now(),
            close_price=close_price,
            close_reason=reason,
        )
        self._store.record_spread(closed)
        emit(
            ManagementAction(
                spread.symbol,
                ManagementKind.CLOSED,
                f"{spread.quantity}x {_describe_legs(spread)}: {reason}",
                spread_id=spread.spread_id,
            )
        )
        return closed

    def _needs_decision(self, spread: Spread, reason: str, emit: Emit) -> None:
        log.warning("%s: %s", spread.symbol, reason)
        self._store.record_spread(replace(spread, status=SpreadStatus.NEEDS_DECISION))
        emit(
            ManagementAction(
                spread.symbol,
                ManagementKind.NEEDS_DECISION,
                reason,
                spread_id=spread.spread_id,
            )
        )

    # --- the one way an order leaves this module -------------------------

    def _place(self, spread: Spread, order: ComboOrder) -> ExecutionResult:
        """Send one management order, having refused any that adds risk.

        Every order this module places comes through here, so the invariant
        is checked at one chokepoint rather than remembered at five call
        sites. A submission failure is returned as an outcome, never raised:
        the caller records it and the spread is retried next pass.

        Raises:
            RiskIncreaseRefused: the order is larger, or wider, than the
                spread it manages. Nothing was sent.
        """
        reason = added_risk(spread, order.quantity, _width_of(order.legs))
        if reason is not None:
            raise RiskIncreaseRefused(f"{order.purpose} order refused: {reason}")

        if not self._broker.is_connected:
            return ExecutionResult(
                Outcome.SUBMISSION_FAILED,
                order.order_ref,
                message="broker not connected; nothing was transmitted",
            )
        try:
            return self._broker.place(order)
        except ExecutionAmbiguous as exc:
            log.error(
                "ambiguous %s for %s (order_ref=%s): %s",
                order.purpose,
                order.symbol,
                exc.order_ref,
                exc,
            )
            return ExecutionResult(
                Outcome.EXECUTION_AMBIGUOUS, exc.order_ref, message=str(exc)
            )
        except (BrokerNotConnected, SubmissionFailed) as exc:
            return ExecutionResult(
                Outcome.SUBMISSION_FAILED, order.order_ref, message=str(exc)
            )

    def _record(self, action: ManagementAction, run_id: str) -> None:
        """Persist one step. Logged loudly on failure, never aborting -- the runner's rule."""
        try:
            self._store.record_management(action, run_id)
        except Exception:  # noqa: BLE001 - logged, never silent; see docstring
            log.exception(
                "failed to record %s for %s (spread %s)",
                action.kind.value,
                action.symbol,
                action.spread_id,
            )


# --- pure helpers ----------------------------------------------------------


def _index_positions(positions: tuple[OptionPosition, ...]) -> dict[OptionLeg, int]:
    """Net signed quantity per contract. A venue may report one leg in pieces."""
    held: dict[OptionLeg, int] = {}
    for position in positions:
        held[position.leg] = held.get(position.leg, 0) + position.quantity
    return {leg: qty for leg, qty in held.items() if qty != 0}


def _strays(held: Mapping[OptionLeg, int], managed: set[OptionLeg]) -> dict[str, list[str]]:
    """Held legs no live spread accounts for, grouped by underlying."""
    strays: dict[str, list[str]] = {}
    for leg, qty in sorted(held.items(), key=lambda item: (item[0].symbol, item[0].expiry)):
        if leg in managed:
            continue
        strays.setdefault(leg.symbol, []).append(
            f"{leg.expiry.isoformat()} {leg.strike}{leg.right.value} x{qty:+d}"
        )
    return strays


def _closing_legs(spread: Spread) -> tuple[ComboLeg, ...]:
    """Buy back the short put, sell the long put: the mirror of the opening."""
    return (
        ComboLeg(spread.short_leg, Action.BUY),
        ComboLeg(spread.long_leg, Action.SELL),
    )


def _opening_legs(proposal: TradeProposal) -> tuple[ComboLeg, ...]:
    return tuple(
        ComboLeg(OptionLeg(proposal.symbol, leg.expiry, leg.strike, leg.right), leg.action)
        for leg in proposal.legs
    )


def _strike(proposal: TradeProposal, action: Action) -> Decimal:
    return next(leg.strike for leg in proposal.legs if leg.action is action)


def _sized(proposal: TradeProposal, quantity: int) -> TradeProposal:
    """The same proposal at ``quantity`` contracts, per-contract figures scaled."""
    if quantity == proposal.quantity:
        return proposal
    scale = Decimal(quantity) / Decimal(proposal.quantity)
    return replace(
        proposal,
        quantity=quantity,
        max_profit=proposal.max_profit * scale,
        max_loss=proposal.max_loss * scale,
        buying_power_effect=proposal.buying_power_effect * scale,
    )


def _dead_book(q: OptionQuote) -> bool:
    """No usable market: the same test the liquidity screen's infinity encodes."""
    return q.mid <= 0


def _average_fill(execution: ExecutionResult) -> Decimal | None:
    filled = execution.filled_quantity
    if filled <= 0:
        return None
    total = sum((f.price * f.quantity for f in execution.fills), Decimal(0))
    return total / Decimal(filled)


def _describe_legs(spread: Spread) -> str:
    return (
        f"{_format_strike(spread.short_strike)}/{_format_strike(spread.long_strike)} "
        f"put spread {spread.expiry.isoformat()}"
    )


def _describe_proposal(proposal: TradeProposal) -> str:
    strikes = "/".join(_format_strike(leg.strike) for leg in proposal.legs)
    return (
        f"{proposal.quantity}x {strikes} put spread {proposal.expiry.isoformat()} "
        f"@ {proposal.limit_price}"
    )


def _outcome_text(execution: ExecutionResult) -> str:
    text = execution.outcome.value
    if execution.message:
        text += f" ({execution.message})"
    return text


def _format_strike(strike: Decimal) -> str:
    text = format(strike, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text
