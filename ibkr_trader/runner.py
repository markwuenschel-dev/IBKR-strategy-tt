"""The production runner.

This is the whole system:

    scan -> tastytrade -> review -> trade -> record -> next symbol

One process, one loop, no scheduler, no controller, no worker, no claims, no
leases, no gates, no receipts. If you want to know what this application does,
:meth:`Runner._process_symbol` is the answer and it fits on a screen.

Two properties are load-bearing and everything else follows from them:

1. **Symbols are independent.** Each one is processed inside its own boundary.
   An ordinary failure on SPY produces a recorded outcome for SPY and nothing
   else; QQQ is evaluated next regardless. No symbol-local failure is allowed to
   become a day-wide mode.
2. **Nothing accumulates.** A pass leaves behind database rows and log lines
   only. There is no runtime state that a later pass has to reconcile, repair,
   or be gated on.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from . import tastytrade
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
from .models import (
    SUBMITTED_OUTCOMES,
    ExecutionResult,
    NoTrade,
    Outcome,
    SymbolResult,
    TradeProposal,
)
from .ports import Broker, MarketData, Reviewer, Store

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PassSummary:
    """What one pass over the universe did.

    Counts are derived from the results rather than incremented as the loop
    runs, so the summary cannot drift out of step with the recorded outcomes.
    """

    run_id: str
    results: tuple[SymbolResult, ...]

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
    ) -> None:
        self._config = config
        self._market_data = market_data
        self._reviewer = reviewer
        self._broker = broker
        self._store = store
        self._clock = clock

    # --- the loop --------------------------------------------------------

    def run_once(self) -> PassSummary:
        """Process every configured symbol exactly once."""
        run_id = uuid.uuid4().hex
        log.info("starting pass %s over %d symbols", run_id, len(self._config.universe))

        results: list[SymbolResult] = []
        for symbol in self._config.universe:
            try:
                result = self._process_symbol_safely(symbol)
            except BaseException as exc:
                # KeyboardInterrupt is not an Exception, so the isolation
                # boundary inside _process_symbol_safely cannot catch it. It can
                # land inside submission, when an order may already be live at
                # the venue -- and escaping here before _record ran would leave
                # the pass with no trace of the attempt at all. Record what is
                # known, then let the signal continue unwinding.
                log.warning("pass %s interrupted while processing %s", run_id, symbol)
                self._record(
                    SymbolResult(
                        symbol=symbol,
                        outcome=Outcome.EXECUTION_AMBIGUOUS,
                        detail=(
                            f"interrupted during processing: {type(exc).__name__}; "
                            f"an order for this symbol may be live at the venue"
                        ),
                    ),
                    run_id,
                )
                raise
            self._record(result, run_id)
            log.info("%-6s %-18s %s", result.symbol, result.outcome.value, result.detail)
            results.append(result)

        summary = PassSummary(run_id=run_id, results=tuple(results))
        log.info("pass %s complete\n%s", run_id, summary.render())
        return summary

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
            summaries.append(self.run_once())
            if market_is_open():
                self._clock.sleep(self._config.scan_interval_seconds)
        return summaries

    # --- one symbol ------------------------------------------------------

    def _process_symbol_safely(self, symbol: str) -> SymbolResult:
        """Run one symbol inside its isolation boundary.

        The broad ``except Exception`` here is the single deliberate instance in
        the codebase, and it exists to satisfy the requirement that one symbol's
        unexpected failure must not stop the scan. It is *controlled boundary
        handling with an explicit policy*, not a silent failure path: the
        traceback is logged in full and the symbol gets a recorded ``ERROR``
        outcome. It never sets global state and never suppresses the next symbol.

        Expected, typed failures are handled precisely in
        :meth:`_process_symbol`; anything reaching here is a genuine bug, and it
        is reported as one.
        """
        try:
            return self._process_symbol(symbol)
        except Exception as exc:  # noqa: BLE001 - documented isolation boundary
            log.exception("unhandled error processing %s", symbol)
            return SymbolResult(
                symbol=symbol,
                outcome=Outcome.ERROR,
                detail=f"{type(exc).__name__}: {exc}",
            )

    def _process_symbol(self, symbol: str) -> SymbolResult:
        """scan -> evaluate -> review -> submit, for exactly one symbol."""
        # --- 1. scan ---
        try:
            snapshot = self._market_data.snapshot(symbol)
            portfolio = self._market_data.portfolio()
        except MarketDataError as exc:
            return SymbolResult(symbol, Outcome.DATA_ERROR, str(exc))

        # --- 2. the algorithm decides whether a trade exists ---
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
        proposal = decision

        # --- 3. exactly one independent review, because a trade now exists ---
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

        # --- 4. submit ---
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
