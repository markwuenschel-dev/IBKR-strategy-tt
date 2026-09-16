"""The production runner.

This is the whole system:

    scan every symbol -> rank the proposals -> review and trade the best -> record

One process, one loop, no scheduler, no controller, no worker, no claims, no
leases, no gates, no receipts. If you want to know what this application does,
:meth:`Runner.run_once` is the answer, and the two per-symbol methods it calls
each fit on a screen.

A pass has five phases:

0. **Start.** Refuse unless the broker has verified an account; open the run
   record.
A. **Manage.** The spreads already on the book are worked first, by
   :class:`~ibkr_trader.manager.Manager`: fills reconciled, profit targets
   rested, the 21-DTE rule applied. This runs before any symbol is quoted, so
   an existing position is dealt with before a new one is considered, and a
   failure in it is recorded as one management error rather than stopping the
   scan.
B. **Scan.** Every symbol in the universe is quoted and evaluated. A symbol the
   algorithm declines, cannot rule on, or fails on is recorded on the spot. A
   symbol that produces a proposal is *held as a candidate*: nothing is
   recorded or reviewed for it yet.
C. **Rank.** The candidates are ordered best first by :mod:`~ibkr_trader.ranking`,
   from the figures the strategy says to prefer.
D. **Submit.** The free position slots are read from a fresh portfolio, and the
   candidates are offered to the reviewer in rank order until those slots are
   spent. Each is re-quoted and re-evaluated first, because with a hundred-name
   universe the quote it was ranked on may be many minutes old. Candidates
   left over once the slots are used up are recorded as ``NOT_SELECTED``
   without spending a review.

Two properties are load-bearing and everything else follows from them:

1. **Symbols are independent for failures.** Each one is quoted, evaluated,
   and traded inside its own boundary. An ordinary failure on SPY produces a
   recorded outcome for SPY and nothing else; the next symbol is evaluated
   regardless. No symbol-local failure is allowed to become a day-wide mode.
   What symbols are *not* independent in is submission: which candidates reach
   the reviewer, and in what order, is decided by rank across the whole pass,
   not by position in the universe.
2. **Nothing accumulates.** A pass leaves behind database rows and log lines
   only. There is no runtime state that a later pass has to reconcile, repair,
   or be gated on. The one thing a pass carries *within itself* is the buying
   power its earlier submissions already sent to the venue -- see
   :func:`_committed_capital` -- and that is derived from the pass's own
   results, never stored, and gone when the pass ends.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from functools import partial
from typing import TypeVar

from . import ranking, tastytrade
from .clock import Clock
from .config import RunConfig
from .errors import (
    BrokerNotConnected,
    ExecutionAmbiguous,
    MarketDataError,
    ReviewError,
    ReviewTimeout,
    SubmissionFailed,
)
from .manager import RUN_WIDE, ManagementSummary, Manager
from .models import (
    SUBMITTED_OUTCOMES,
    ExecutionResult,
    ManagementAction,
    ManagementKind,
    MarketSnapshot,
    NeedsDecision,
    NoTrade,
    Outcome,
    Portfolio,
    SymbolResult,
    TradeProposal,
)
from .ports import Broker, MarketData, Reviewer, Store
from .ranking import RankedProposal

log = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class PassSummary:
    """What one pass over the universe did.

    Counts are derived from the results rather than incremented as the loop
    runs, so the summary cannot drift out of step with the recorded outcomes.
    """

    run_id: str
    results: tuple[SymbolResult, ...]
    #: What phase A did before the scan. Rendered first, because an operator
    #: reading the summary wants to know about the positions they already
    #: hold before the ones they might open.
    management: ManagementSummary

    def _count(self, *outcomes: Outcome) -> int:
        wanted = set(outcomes)
        return sum(1 for r in self.results if r.outcome in wanted)

    @property
    def scanned(self) -> int:
        return len(self.results)

    @property
    def no_trade(self) -> int:
        return self._count(Outcome.NO_TRADE)

    @property
    def proposals(self) -> int:
        """Results carrying a proposal, whether it was reviewed or ranked out."""
        return sum(1 for r in self.results if r.proposal is not None)

    @property
    def approved(self) -> int:
        return sum(1 for r in self.results if r.review is not None and r.review.approved)

    @property
    def rejected(self) -> int:
        return self._count(Outcome.REVIEW_REJECTED)

    @property
    def review_failed(self) -> int:
        """Reviewer timed out or answered unusably. Counted together, as in §12."""
        return self._count(Outcome.REVIEW_TIMEOUT, Outcome.REVIEW_ERROR)

    @property
    def orders_submitted(self) -> int:
        return self._count(*SUBMITTED_OUTCOMES)

    @property
    def filled(self) -> int:
        return self._count(Outcome.FILLED)

    @property
    def working(self) -> int:
        return self._count(Outcome.WORKING, Outcome.ACCEPTED)

    @property
    def broker_rejected(self) -> int:
        """Refused *by the venue*. The order arrived and was turned down."""
        return self._count(Outcome.BROKER_REJECTED)

    @property
    def never_sent(self) -> int:
        """Refused before transmission. Nothing reached the venue.

        Split from ``broker_rejected`` because the two need different responses:
        a venue rejection is a question about the order, and a failure to send
        is a question about this process.
        """
        return self._count(Outcome.SUBMISSION_FAILED)

    @property
    def not_selected(self) -> int:
        """Proposals that ranked below the last free position slot.

        Counted apart from ``no_trade`` because the algorithm *did* find a
        trade; the pass simply had better ones than it had room for.
        """
        return self._count(Outcome.NOT_SELECTED)

    @property
    def awaiting_decision(self) -> int:
        """Trades the system declined to rule on alone, pending a human answer.

        Counted apart from ``no_trade`` because it is the absence of a decision
        rather than a decision, and apart from ``errors`` because nothing broke.
        """
        return self._count(Outcome.AWAITING_DECISION)

    @property
    def ambiguous(self) -> int:
        return self._count(Outcome.EXECUTION_AMBIGUOUS)

    @property
    def errors(self) -> int:
        return self._count(Outcome.ERROR, Outcome.DATA_ERROR)

    def render(self) -> str:
        """The end-of-pass operator summary.

        Deliberately small: an operator should never need to open a state file to
        find out why nothing traded.
        """
        lines = [
            self.management.render(),
            f"Scanned: {self.scanned}",
            f"No trade: {self.no_trade}",
            f"Proposals: {self.proposals}",
            f"Reviewer approved: {self.approved}",
            f"Reviewer rejected: {self.rejected}",
            f"Reviewer timeout/error: {self.review_failed}",
            f"Orders submitted: {self.orders_submitted}",
            f"Filled: {self.filled}",
            f"Working: {self.working}",
            f"Rejected by venue: {self.broker_rejected}",
            f"Never sent: {self.never_sent}",
        ]
        if self.not_selected:
            lines.append(f"Ranked out: {self.not_selected}")
        if self.awaiting_decision:
            lines.append(f"Awaiting decision: {self.awaiting_decision}")
        if self.ambiguous:
            lines.append(f"Ambiguous (needs reconciliation): {self.ambiguous}")
        if self.errors:
            lines.append(f"Errors: {self.errors}")
        return "\n".join(lines)


class Runner:
    """Owns the trading loop. Nothing else does."""

    def __init__(
        self,
        config: RunConfig,
        market_data: MarketData,
        reviewer: Reviewer,
        broker: Broker,
        store: Store,
        clock: Clock,
        manager: Manager | None = None,
    ) -> None:
        self._config = config
        self._market_data = market_data
        self._reviewer = reviewer
        self._broker = broker
        self._store = store
        self._clock = clock
        # Built here by default from the same five dependencies, so a caller
        # that wires the runner has wired phase A; supplied explicitly when a
        # test wants to observe or replace it.
        self._manager = manager or Manager(config, market_data, reviewer, broker, store, clock)

    # --- the loop --------------------------------------------------------

    def run_once(self) -> PassSummary:
        """Scan the universe, rank what qualifies, and trade the best of it.

        Every configured symbol ends the pass with exactly one recorded
        result. The phases are described in the module docstring.
        """
        # --- 0. start ---
        run_id = self._start_run()
        universe = self._config.universe
        log.info("starting pass %s over %d symbols", run_id, len(universe))

        # --- A. manage what is already held ---
        management = self._manage(run_id)

        results: list[SymbolResult] = []

        # --- B. scan and evaluate every symbol; hold the proposals ---
        #
        # No interrupt handling here, on purpose. Nothing is sent to the venue
        # during the scan, so an interrupt can simply unwind; the symbol it
        # lands on is left without a row, which is the truth about it.
        candidates: list[TradeProposal] = []
        for symbol in universe:
            evaluated = self._isolated(symbol, partial(self._evaluate_symbol, symbol))
            if isinstance(evaluated, TradeProposal):
                candidates.append(evaluated)
            else:
                results.append(self._conclude(evaluated, run_id))

        # --- C. rank ---
        ranked = ranking.rank_proposals(candidates)
        field = len(ranked)
        log.info("pass %s: %d of %d symbols proposed a trade", run_id, field, len(universe))
        for candidate in ranked:
            log.info("candidate %-6s %s", candidate.symbol, candidate.describe(field))

        # --- D. review and submit in rank order, while slots remain ---
        #
        # The slot count comes from a portfolio read *after* the scan rather
        # than from any of the per-symbol reads during it, so it reflects the
        # book as it stands at the moment the pass starts sending orders.
        try:
            portfolio = self._market_data.portfolio()
        except MarketDataError as exc:
            for candidate in ranked:
                results.append(
                    self._conclude(
                        SymbolResult(
                            candidate.symbol,
                            Outcome.DATA_ERROR,
                            f"{candidate.describe(field)}; {exc}",
                        ),
                        run_id,
                    )
                )
            return self._finish(run_id, results, management)

        held = portfolio.open_symbol_count
        max_positions = self._config.risk.max_positions
        slots = max(0, max_positions - held)
        log.info(
            "pass %s: %d free position slot(s), %d open of %d",
            run_id,
            slots,
            held,
            max_positions,
        )

        for candidate in ranked:
            # Both figures are re-derived from the results each time rather
            # than accumulated, so they cannot disagree with what was recorded
            # (the PassSummary rule).
            if _orders_submitted(results) >= slots:
                result = SymbolResult(
                    candidate.symbol,
                    Outcome.NOT_SELECTED,
                    f"{candidate.describe(field)}; "
                    f"no free position slot ({held} open of {max_positions})",
                    proposal=candidate.proposal,
                )
            else:
                committed = _committed_capital(results)
                try:
                    result = self._isolated(
                        candidate.symbol,
                        partial(self._trade_candidate, candidate, field, committed),
                    )
                except BaseException as exc:
                    # KeyboardInterrupt is not an Exception, so the isolation
                    # boundary cannot catch it. In this phase it can land inside
                    # submission, when an order may already be live at the
                    # venue -- and escaping before _record ran would leave the
                    # pass with no trace of the attempt at all. Record what is
                    # known, then let the signal continue unwinding.
                    log.warning(
                        "pass %s interrupted while processing %s", run_id, candidate.symbol
                    )
                    self._record(
                        SymbolResult(
                            symbol=candidate.symbol,
                            outcome=Outcome.EXECUTION_AMBIGUOUS,
                            detail=(
                                f"interrupted during processing: {type(exc).__name__}; "
                                f"an order for this symbol may be live at the venue"
                            ),
                        ),
                        run_id,
                    )
                    raise
            results.append(self._conclude(result, run_id))

        return self._finish(run_id, results, management)

    def run_while(
        self,
        market_is_open: Callable[[], bool],
        max_passes: int | None = None,
    ) -> list[PassSummary]:
        """Repeat the pass while the market is open.

        This is the entire scheduling story. There is no scheduler process and no
        "tick" concept: the same process that trades also decides when to go
        round again.

        Args:
            market_is_open: Consulted before each pass.
            max_passes: Optional ceiling, so tests terminate deterministically.
        """
        summaries: list[PassSummary] = []
        while market_is_open():
            if max_passes is not None and len(summaries) >= max_passes:
                break
            started = self._clock.now()
            summaries.append(self.run_once())
            if market_is_open():
                self._wait_for_next_scan(started, market_is_open)
        return summaries

    def _wait_for_next_scan(
        self, started: datetime, market_is_open: Callable[[], bool]
    ) -> None:
        """Sleep until the next scan is due, working the book on the way.

        The wait is measured from when the *last scan started*, not from when it
        finished, so the cadence the operator configured is the cadence they
        get. Sleeping the whole interval afterwards made the real period
        ``pass_duration + interval``, which on a hundred-name universe is a
        quarter of an hour of silent drift per pass.

        Management runs on its own, shorter cadence inside that wait, because
        the two jobs have opposite economics: a scan quotes thousands of
        contracts and is worth doing rarely, while reconciling a fill and
        resting its profit target is nearly free and wants to happen soon after
        the fill rather than up to a full scan interval later.

        A pass that overruns its period returns immediately rather than
        skipping ahead or sleeping a negative amount.
        """
        interval = self._config.scan_interval_seconds
        due = started + timedelta(seconds=interval)
        overrun = (self._clock.now() - due).total_seconds()
        if overrun > 0:
            log.warning(
                "pass took %.0fs longer than the %.0fs scan interval; "
                "starting the next one immediately",
                overrun,
                interval,
            )
            return

        while market_is_open():
            remaining = (due - self._clock.now()).total_seconds()
            if remaining <= 0:
                return
            self._clock.sleep(min(self._config.manage_interval_seconds, remaining))
            if (due - self._clock.now()).total_seconds() <= 0:
                # The scan is due now, and it manages before it scans.
                return
            if market_is_open() and self._has_book():
                self.manage_once()

    def _has_book(self) -> bool:
        """Whether the durable record holds anything worth a management pass.

        Read from the store rather than the broker: it costs a local query
        instead of a round trip, and both states that need managing are already
        recorded there -- a spread that is open, and an opening order that
        reached the venue and has not been reconciled into one.

        A stray position the store has never heard of is *not* covered here and
        does not need to be: the scan's own management phase runs the
        unmanaged-position sweep every period regardless.
        """
        return bool(self._store.live_spreads() or self._store.unreconciled_openings())

    def manage_once(self) -> ManagementSummary:
        """Phase A on its own: work the book without scanning for new trades.

        The same identity gate and run record as a full pass, so every
        management order is attributable to a verified account.
        """
        run_id = self._start_run()
        log.info("starting management run %s", run_id)
        management = self._manage(run_id)
        log.info("management run %s complete\n%s", run_id, management.render())
        return management

    def _start_run(self) -> str:
        """Phase 0: refuse without a verified account, then open the run record."""
        run_id = uuid.uuid4().hex
        verified_account = self._broker.verified_account
        if verified_account is None:
            raise BrokerNotConnected(
                "the broker has not verified an account; refusing to start a pass"
            )
        self._store.start_run(
            run_id=run_id,
            declared_mode="paper" if self._config.ibkr.paper else "live",
            verified_account=verified_account,
            host=self._config.ibkr.host,
            port=self._config.ibkr.port,
        )
        return run_id

    def _manage(self, run_id: str) -> ManagementSummary:
        """Phase A, guarded so that it can never stop phase B.

        The manager isolates each spread; what reaches here is a failure
        *before* any spread could be reached -- the position stream or the
        open-order stream could not be read. That is one recorded error for
        the run, not a reason to leave the universe unscanned.
        """
        try:
            return self._manager.run(run_id)
        except Exception as exc:  # noqa: BLE001 - documented boundary; same policy as _isolated
            log.exception("management failed before any spread was processed")
            action = ManagementAction(
                symbol=RUN_WIDE,
                kind=ManagementKind.ERROR,
                detail=f"{type(exc).__name__}: {exc}",
            )
            try:
                self._store.record_management(action, run_id)
            except Exception:  # noqa: BLE001 - logged, never silent; see _record
                log.exception("failed to record the management error for run %s", run_id)
            return ManagementSummary(run_id=run_id, actions=(action,))

    def _finish(
        self, run_id: str, results: list[SymbolResult], management: ManagementSummary
    ) -> PassSummary:
        summary = PassSummary(run_id=run_id, results=tuple(results), management=management)
        log.info("pass %s complete\n%s", run_id, summary.render())
        return summary

    # --- one symbol ------------------------------------------------------

    def _isolated(self, symbol: str, step: Callable[[], T]) -> T | SymbolResult:
        """Run one symbol's step inside its isolation boundary.

        The broad ``except Exception`` here is the single deliberate instance in
        the codebase, and it exists to satisfy the requirement that one symbol's
        unexpected failure must not stop the pass. It is *controlled boundary
        handling with an explicit policy*, not a silent failure path: the
        traceback is logged in full and the symbol gets a recorded ``ERROR``
        outcome. It never sets global state and never suppresses the next symbol.

        Expected, typed failures are handled precisely inside the step; anything
        reaching here is a genuine bug, and it is reported as one.
        """
        try:
            return step()
        except Exception as exc:  # noqa: BLE001 - documented isolation boundary
            log.exception("unhandled error processing %s", symbol)
            return SymbolResult(
                symbol=symbol,
                outcome=Outcome.ERROR,
                detail=f"{type(exc).__name__}: {exc}",
            )

    def _evaluate_symbol(self, symbol: str) -> SymbolResult | TradeProposal:
        """Phase B: scan -> evaluate, for exactly one symbol.

        A :class:`SymbolResult` is final and goes straight to the record. A
        :class:`TradeProposal` is a candidate for phase D and is recorded there,
        under whatever the ranking and the re-quote make of it.

        No committed-capital decrement is applied here: nothing has been
        submitted yet when the scan runs, so there is nothing to withhold.
        """
        try:
            snapshot, portfolio = self._quote(symbol)
        except MarketDataError as exc:
            return SymbolResult(symbol, Outcome.DATA_ERROR, str(exc))
        return self._decide(symbol, snapshot, portfolio)

    def _trade_candidate(
        self, candidate: RankedProposal, field: int, committed: Decimal
    ) -> SymbolResult:
        """Phase D: re-quote -> evaluate -> review -> submit, for one candidate.

        The candidate's own proposal is *not* what gets submitted. It was built
        on the scan's quote, and by the time its turn comes that quote may be
        minutes old and the book may have moved; so the symbol is quoted and
        evaluated again, with the capital this pass has already sent withheld,
        and only a proposal from that fresh evaluation goes to the reviewer.

        Every detail recorded here starts with the candidate's ranking line, so
        the record says why this symbol was reached before the others.

        Args:
            candidate: The ranked proposal whose turn it is.
            field: How many candidates were ranked, for the ``rank i/n`` line.
            committed: Buying power that earlier submissions in this same pass
                have already sent to the venue. Subtracted from the account's
                reported buying power before the algorithm sizes this trade.
        """
        symbol = candidate.symbol
        place = candidate.describe(field)
        try:
            snapshot, portfolio = self._quote(symbol)
        except MarketDataError as exc:
            return SymbolResult(symbol, Outcome.DATA_ERROR, f"{place}; on re-quote: {exc}")

        portfolio = _withhold_committed(symbol, portfolio, committed)
        decided = self._decide(symbol, snapshot, portfolio)
        if isinstance(decided, SymbolResult):
            return replace(decided, detail=f"{place}; on re-quote: {decided.detail}")

        result = self._review_and_submit(symbol, decided, portfolio)
        return replace(result, detail=f"{place}; {result.detail}")

    def _quote(self, symbol: str) -> tuple[MarketSnapshot, Portfolio]:
        """One symbol's market and the account, read together.

        Raises:
            MarketDataError: from either read; the caller decides what that
                means for the symbol at the phase it is in.
        """
        snapshot = self._market_data.snapshot(symbol)
        portfolio = self._market_data.portfolio()
        return snapshot, portfolio

    def _decide(
        self, symbol: str, snapshot: MarketSnapshot, portfolio: Portfolio
    ) -> SymbolResult | TradeProposal:
        """Ask the algorithm; classify everything that is not a proposal."""
        decision = tastytrade.evaluate(
            symbol=symbol,
            snapshot=snapshot,
            portfolio=portfolio,
            strategy=self._config.strategy,
            risk=self._config.risk,
            now=self._clock.now(),
        )
        if isinstance(decision, NoTrade):
            return SymbolResult(symbol, Outcome.NO_TRADE, decision.reason)
        if isinstance(decision, NeedsDecision):
            # Recorded and left for a human, then the pass continues. Not
            # reviewed and not submitted: a review would spend a request on a
            # trade that cannot proceed either way, and submitting is the exact
            # thing the unknown state makes unsafe.
            log.warning("%s: %s", symbol, decision.reason)
            return SymbolResult(
                symbol,
                Outcome.AWAITING_DECISION,
                decision.reason,
                proposal=decision.proposal,
            )
        return decision

    def _review_and_submit(
        self, symbol: str, proposal: TradeProposal, portfolio: Portfolio
    ) -> SymbolResult:
        """Exactly one independent review, because a trade now exists; then submit."""
        try:
            review = self._reviewer.review(proposal, portfolio)
        except ReviewTimeout as exc:
            return SymbolResult(symbol, Outcome.REVIEW_TIMEOUT, str(exc), proposal=proposal)
        except ReviewError as exc:
            return SymbolResult(symbol, Outcome.REVIEW_ERROR, str(exc), proposal=proposal)

        if not review.approved:
            return SymbolResult(
                symbol, Outcome.REVIEW_REJECTED, review.reason, proposal, review
            )

        return self._submit(symbol, proposal, review)

    def _submit(self, symbol: str, proposal: TradeProposal, review) -> SymbolResult:
        """Send an approved proposal to the broker and classify the outcome."""
        if not self._broker.is_connected:
            return SymbolResult(
                symbol,
                Outcome.SUBMISSION_FAILED,
                "broker not connected; nothing was transmitted",
                proposal,
                review,
            )

        try:
            execution = self._broker.submit(proposal)
        except ExecutionAmbiguous as exc:
            # The one failure that genuinely needs follow-up. It is recorded
            # against this order alone; it sets no global latch and does not
            # stop the remaining symbols.
            log.error(
                "ambiguous submission for %s (order_ref=%s): %s",
                symbol,
                exc.order_ref,
                exc,
            )
            return SymbolResult(
                symbol,
                Outcome.EXECUTION_AMBIGUOUS,
                str(exc),
                proposal,
                review,
                ExecutionResult(Outcome.EXECUTION_AMBIGUOUS, order_ref=exc.order_ref),
            )
        except (BrokerNotConnected, SubmissionFailed) as exc:
            return SymbolResult(symbol, Outcome.SUBMISSION_FAILED, str(exc), proposal, review)

        detail = execution.message or _describe(proposal, execution)
        return SymbolResult(symbol, execution.outcome, detail, proposal, review, execution)

    # --- recording -------------------------------------------------------

    def _conclude(self, result: SymbolResult, run_id: str) -> SymbolResult:
        """Persist and log one symbol's final result, whichever phase ended it."""
        self._record(result, run_id)
        log.info("%-6s %-18s %s", result.symbol, result.outcome.value, result.detail)
        return result

    def _record(self, result: SymbolResult, run_id: str) -> None:
        """Persist one outcome.

        A persistence failure is logged loudly but does not abort the pass: an
        order that already reached the broker is a fact regardless of whether we
        managed to write it down, and stopping here would strand the remaining
        symbols without improving anything.
        """
        try:
            self._store.record(result, run_id)
        except Exception:  # noqa: BLE001 - logged, never silent; see docstring
            log.exception(
                "failed to record %s outcome for %s (proposal_id=%s)",
                result.outcome.value,
                result.symbol,
                result.proposal.proposal_id if result.proposal else None,
            )


def _orders_submitted(results: Iterable[SymbolResult]) -> int:
    """Orders this pass has put on the wire so far, by the same test as the summary."""
    return sum(1 for r in results if r.outcome in SUBMITTED_OUTCOMES)


def _committed_capital(results: Iterable[SymbolResult]) -> Decimal:
    """Buying power the pass has already sent to the venue, from its results.

    Membership in :data:`~ibkr_trader.models.SUBMITTED_OUTCOMES` is the whole
    test. That set is defined by *arrival* -- ``BROKER_REJECTED`` is in it
    because the order reached the venue, and ``SUBMISSION_FAILED`` is not
    because it never left this process -- and the same line is drawn here on
    purpose. Within a pass this process does not learn what the venue went on
    to do with an order, so any order that arrived is treated as holding its
    defined-risk margin (``buying_power_effect``, which for a short vertical is
    the max loss). The error that produces is sizing the next symbol smaller
    than strictly necessary, which is the safe direction.
    """
    return sum(
        (
            r.proposal.buying_power_effect
            for r in results
            if r.outcome in SUBMITTED_OUTCOMES and r.proposal is not None
        ),
        Decimal(0),
    )


def _withhold_committed(symbol: str, portfolio: Portfolio, committed: Decimal) -> Portfolio:
    """The account as the algorithm should see it, less what this pass already sent.

    The account's buying power is read fresh per candidate, but the venue has
    not necessarily debited orders sent seconds ago, so without this every
    candidate in a pass would be sized against the same free cash. Net
    liquidation is deliberately left alone: the per-trade risk budget is a
    fraction of account value, not of what is uncommitted.
    """
    if committed <= 0:
        return portfolio
    log.debug(
        "%s: buying power %s less %s committed earlier this pass",
        symbol,
        portfolio.buying_power,
        committed,
    )
    return replace(
        portfolio,
        buying_power=max(Decimal(0), portfolio.buying_power - committed),
    )


def _format_strike(strike: Decimal) -> str:
    """Render a strike the way an operator writes it: ``180``, ``187.5``.

    ``Decimal.normalize`` is not usable here -- it renders 180 as ``1.8E+2``.
    Fixed-point formatting followed by trimming keeps whole strikes whole and
    fractional strikes exact.
    """
    text = format(strike, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _describe(proposal: TradeProposal, execution: ExecutionResult) -> str:
    """Operator-facing one-liner for a submitted order.

    Renders as, for example: ``3x 185/180 put credit spread @ 1.75``.
    """
    strikes = "/".join(_format_strike(leg.strike) for leg in proposal.legs)
    return f"{proposal.quantity}x {strikes} put credit spread @ {proposal.limit_price}"
