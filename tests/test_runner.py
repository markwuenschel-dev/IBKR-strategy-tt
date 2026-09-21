"""Loop behaviour, ranked submission, and operator output.

Three things are pinned here: repeating the scan needs no scheduler and no
second process; the pass trades its best-ranked candidates into the free
position slots rather than the first ones in universe order; and the
end-of-pass summary tells an operator why nothing traded without them opening
a database.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from decimal import Decimal

import pytest

from ibkr_trader import runner as runner_module
from ibkr_trader.clock import FixedClock
from ibkr_trader.errors import MarketDataError, SubmissionFailed
from ibkr_trader.models import (
    Action,
    ComboQuote,
    NoTrade,
    Outcome,
    Portfolio,
    Position,
    ProposalLeg,
    Right,
    TradeProposal,
    opening_combo_legs,
)
from ibkr_trader.store import SqliteStore

from .fakes import (
    GOOD_EXPIRY,
    SCAN_TIME,
    FakeBroker,
    StubMarketData,
    StubReviewer,
    tradable_snapshot,
)
from .harness import build_runner


class SlowMarket(StubMarketData):
    """A market whose every quote costs the injected clock real time.

    The suite's doubles are instantaneous, which is precisely why a scheduling
    bug measured in pass duration could not be caught: with a zero-length pass,
    sleeping *after* it and sleeping *until the next period* are the same thing.
    """

    def __init__(self, snapshots, clock, seconds):
        super().__init__(snapshots)
        self._test_clock = clock
        self._test_seconds = seconds

    def snapshot(self, symbol):
        self._test_clock.advance(self._test_seconds)
        return super().snapshot(symbol)


class TimestampingStore(SqliteStore):
    """Records the injected clock at the start of every run, scan or manage."""

    def __init__(self, path, clock):
        super().__init__(path, clock=clock)
        self.run_starts = []

    def start_run(self, **kwargs):
        self.run_starts.append(self._clock.now())
        return super().start_run(**kwargs)


def test_scans_start_one_interval_apart_not_one_interval_after_the_last_one_ended(tmp_path):
    """45 minutes between scans means scans *start* 45 minutes apart.

    Sleeping the whole interval after a pass made the real cycle
    ``pass_duration + interval``, so a 15-minute pass on a 45-minute interval
    began a scan every hour.
    """
    clock = FixedClock(SCAN_TIME)
    market = SlowMarket({"AAPL": tradable_snapshot("AAPL")}, clock, 20.0)
    store = TimestampingStore(tmp_path / "periods.sqlite3", clock)
    runner, _, _, _, _ = build_runner(
        tmp_path,
        market=market,
        clock=clock,
        store=store,
        overrides={"scan_interval_seconds": 300.0, "manage_interval_seconds": 1000.0},
    )

    runner.run_while(lambda: True, max_passes=3)

    gaps = [
        (b - a).total_seconds()
        for a, b in zip(store.run_starts, store.run_starts[1:], strict=False)
    ]
    assert gaps == [300.0, 300.0], "the period must not drift by the pass duration"


def test_management_passes_fill_the_wait_when_the_book_has_something_in_it(tmp_path):
    """A resting order should be looked at more often than the scan cadence.

    The scan is expensive and infrequent; reconciling a fill and resting its
    profit target is cheap and wants to happen soon after the fill.
    """
    clock = FixedClock(SCAN_TIME)
    market = StubMarketData({"AAPL": tradable_snapshot("AAPL")})
    store = TimestampingStore(tmp_path / "cadence.sqlite3", clock)
    runner, _, _, _, _ = build_runner(
        tmp_path,
        market=market,
        clock=clock,
        store=store,
        overrides={"scan_interval_seconds": 300.0, "manage_interval_seconds": 100.0},
    )

    runner.run_while(lambda: True, max_passes=1)

    # One scan at t0, then a management pass at each interval inside the wait.
    offsets = [(t - store.run_starts[0]).total_seconds() for t in store.run_starts]
    assert offsets == [0.0, 100.0, 200.0]


def test_no_management_pass_runs_while_there_is_nothing_to_manage(tmp_path):
    """An empty book must cost nothing between scans.

    ``StubMarketData`` raises for an unconfigured symbol, so this pass records
    an error and never submits -- leaving no spread and no opening order.
    """
    clock = FixedClock(SCAN_TIME)
    store = TimestampingStore(tmp_path / "idle.sqlite3", clock)
    runner, _, _, _, _ = build_runner(
        tmp_path,
        market=StubMarketData(),
        clock=clock,
        store=store,
        overrides={"scan_interval_seconds": 300.0, "manage_interval_seconds": 100.0},
    )

    runner.run_while(lambda: True, max_passes=1)

    assert len(store.run_starts) == 1, "only the scan; nothing to manage"
    assert sum(clock.slept) == 300.0, "still waits the full interval"


def test_repeat_scanning_needs_no_scheduler(tmp_path):
    """``run_while`` is the entire scheduling story.

    The same process loops, sleeps on the injected clock, and stops when the
    market closes. No controller, no worker, no tick receipts.
    """
    clock = FixedClock(SCAN_TIME)
    market = StubMarketData({"AAPL": tradable_snapshot("AAPL")})
    # Duplicate positions are disallowed by default, so let the same fixture
    # trade on every pass rather than modelling fills back into the portfolio.
    runner, _, _, broker, store = build_runner(
        tmp_path, market=market, clock=clock, overrides={"scan_interval_seconds": 60.0}
    )

    summaries = runner.run_while(lambda: True, max_passes=3)

    assert len(summaries) == 3
    assert broker.call_count == 3
    assert len(store.attempts()) == 3
    # It slept between passes, on the injected clock, costing no wall-clock time.
    assert clock.slept == [60.0, 60.0, 60.0]


def test_loop_stops_when_the_market_closes(tmp_path):
    """A closed market ends the loop; nothing needs to be torn down."""
    clock = FixedClock(SCAN_TIME)
    calls = {"n": 0}

    def market_is_open() -> bool:
        calls["n"] += 1
        return calls["n"] <= 2

    runner, _, _, broker, _ = build_runner(tmp_path, clock=clock)
    summaries = runner.run_while(market_is_open)

    assert len(summaries) == 1
    assert broker.call_count == 1


def test_closed_market_runs_no_passes_at_all(tmp_path):
    runner, _, _, broker, store = build_runner(tmp_path)
    assert runner.run_while(lambda: False) == []
    assert broker.call_count == 0
    assert store.attempts() == []


# --- operator output ------------------------------------------------------


def test_summary_reports_every_category(tmp_path):
    """The §12 summary, over a universe that exercises several outcomes."""
    market = StubMarketData(
        snapshots={
            "AAPL": tradable_snapshot("AAPL"),
            "QQQ": tradable_snapshot("QQQ", iv_rank=5.0),
        },
        failures={
            "NVDA": __import__(
                "ibkr_trader.errors", fromlist=["MarketDataError"]
            ).MarketDataError("option chain unavailable")
        },
    )
    runner, _, _, _, _ = build_runner(
        tmp_path, universe=("AAPL", "QQQ", "NVDA"), market=market
    )

    summary = runner.run_once()
    rendered = summary.render()

    assert "Scanned: 3" in rendered
    assert "No trade: 1" in rendered
    assert "Proposals: 1" in rendered
    assert "Reviewer approved: 1" in rendered
    assert "Orders submitted: 1" in rendered
    assert "Filled: 1" in rendered
    assert "Errors: 1" in rendered


def test_each_symbol_logs_one_outcome_line(tmp_path, caplog):
    """One line per symbol, naming the outcome and the reason."""
    market = StubMarketData(
        {"AAPL": tradable_snapshot("AAPL"), "QQQ": tradable_snapshot("QQQ", iv_rank=5.0)}
    )
    runner, _, _, _, _ = build_runner(tmp_path, universe=("AAPL", "QQQ"), market=market)

    with caplog.at_level(logging.INFO, logger="ibkr_trader.runner"):
        runner.run_once()

    lines = [r.getMessage() for r in caplog.records]
    aapl = next(line for line in lines if line.startswith("AAPL"))
    qqq = next(line for line in lines if line.startswith("QQQ"))

    assert "FILLED" in aapl
    assert "185/180 put credit spread @ 1.75" in aapl
    assert "NO_TRADE" in qqq
    assert "IV rank" in qqq


def test_rejected_review_reports_the_reviewers_reason(tmp_path, caplog):
    """The operator sees why the reviewer said no, not merely that it did."""
    reviewer = StubReviewer(approved=False, reason="spread width exceeds preference")
    runner, _, _, _, _ = build_runner(tmp_path, reviewer=reviewer)

    with caplog.at_level(logging.INFO, logger="ibkr_trader.runner"):
        runner.run_once()

    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("AAPL"))
    assert "REVIEW_REJECTED" in line
    assert "spread width exceeds preference" in line


# --- ranked submission into free slots --------------------------------------
#
# Three names that all qualify for the fixture's 185/180 spread and differ only
# in IV rank, so the ranking is decided by one figure and is checkable by hand:
# NVDA (80) > MSFT (60) > AAPL (40). The best is deliberately *last* in the
# universe, so any test that sees NVDA first has seen the ranking, not the
# universe order.

RANKED_UNIVERSE = ("AAPL", "MSFT", "NVDA")
BEST_FIRST = ["NVDA", "MSFT", "AAPL"]


def _ranked_market(**kwargs) -> StubMarketData:
    return StubMarketData(
        {
            "AAPL": tradable_snapshot("AAPL", iv_rank=40.0),
            "MSFT": tradable_snapshot("MSFT", iv_rank=60.0),
            "NVDA": tradable_snapshot("NVDA", iv_rank=80.0),
        },
        **kwargs,
    )


def _slots(n: int) -> dict:
    return {"risk": {"max_positions": n}}


def _by_symbol(summary) -> dict[str, object]:
    return {r.symbol: r for r in summary.results}


def test_the_best_ranked_candidates_take_the_free_slots(tmp_path):
    """Two slots, three qualifying names: the two best trade, the third is ranked out."""
    market = _ranked_market()
    runner, _, reviewer, broker, store = build_runner(
        tmp_path, universe=RANKED_UNIVERSE, market=market, overrides=_slots(2)
    )

    summary = runner.run_once()
    results = _by_symbol(summary)

    assert [p.symbol for p in broker.submitted] == ["NVDA", "MSFT"]
    assert results["NVDA"].outcome is Outcome.FILLED
    assert results["MSFT"].outcome is Outcome.FILLED
    assert results["AAPL"].outcome is Outcome.NOT_SELECTED
    assert "rank 3/3" in results["AAPL"].detail
    assert "no free position slot (0 open of 2)" in results["AAPL"].detail
    # The ranked-out candidate keeps its proposal so the record shows what lost.
    assert results["AAPL"].proposal is not None
    assert results["AAPL"].review is None

    # Reviews cost money: exactly the two that could trade were reviewed.
    assert reviewer.call_count == 2
    assert [p.symbol for p in reviewer.reviewed] == ["NVDA", "MSFT"]

    # Every symbol still ends the pass with exactly one row.
    assert sorted(a["symbol"] for a in store.attempts()) == sorted(RANKED_UNIVERSE)
    assert summary.not_selected == 1
    assert summary.proposals == 3
    assert "Ranked out: 1" in summary.render()


def test_submission_order_follows_score_not_universe_order(tmp_path):
    """With room for everyone, the order they are sent is still best first."""
    runner, _, reviewer, broker, _ = build_runner(
        tmp_path, universe=RANKED_UNIVERSE, market=_ranked_market()
    )

    summary = runner.run_once()

    assert [p.symbol for p in reviewer.reviewed] == BEST_FIRST
    assert [p.symbol for p in broker.submitted] == BEST_FIRST
    assert [r.symbol for r in summary.results] == BEST_FIRST
    assert summary.not_selected == 0
    assert "Ranked out" not in summary.render()


def test_the_ranked_table_is_logged_one_line_per_candidate(tmp_path, caplog):
    runner, *_ = build_runner(tmp_path, universe=RANKED_UNIVERSE, market=_ranked_market())

    with caplog.at_level(logging.INFO, logger="ibkr_trader.runner"):
        runner.run_once()

    table = [r.getMessage() for r in caplog.records if r.getMessage().startswith("candidate")]
    assert [line.split()[1] for line in table] == BEST_FIRST
    assert all("rank " in line and "score " in line for line in table)


def test_a_candidate_is_quoted_again_before_it_is_submitted(tmp_path):
    """Ranking is done on the scan's quote; submission is not.

    A symbol that reaches submission is quoted twice -- scan and re-quote -- and
    one that never becomes a candidate is quoted once.
    """
    market = StubMarketData(
        snapshots={"AAPL": tradable_snapshot("AAPL"), "QQQ": tradable_snapshot("QQQ", 5.0)},
        failures={"NVDA": MarketDataError("option chain unavailable")},
    )
    runner, _, _, broker, _ = build_runner(
        tmp_path, universe=("AAPL", "QQQ", "NVDA"), market=market
    )

    runner.run_once()

    assert [p.symbol for p in broker.submitted] == ["AAPL"]
    assert market.requested.count("AAPL") == 2
    assert market.requested.count("QQQ") == 1
    assert market.requested.count("NVDA") == 1


def test_a_candidate_that_no_longer_qualifies_on_requote_yields_its_slot(tmp_path):
    """The market moved between scan and submission: the next-ranked name trades."""
    market = _ranked_market(requotes={"NVDA": tradable_snapshot("NVDA", iv_rank=5.0)})
    runner, _, reviewer, broker, _ = build_runner(
        tmp_path, universe=RANKED_UNIVERSE, market=market, overrides=_slots(1)
    )

    summary = runner.run_once()
    results = _by_symbol(summary)

    assert results["NVDA"].outcome is Outcome.NO_TRADE
    assert results["NVDA"].detail.startswith("rank 1/3")
    assert "on re-quote: IV rank 5.0 below minimum" in results["NVDA"].detail
    assert results["MSFT"].outcome is Outcome.FILLED
    assert results["AAPL"].outcome is Outcome.NOT_SELECTED
    # The stale NVDA proposal was never offered to the reviewer.
    assert [p.symbol for p in reviewer.reviewed] == ["MSFT"]
    assert [p.symbol for p in broker.submitted] == ["MSFT"]


class RejectFirstReviewer(StubReviewer):
    """Rejects the first proposal it sees and approves the rest."""

    def review(self, proposal, portfolio):
        first = not self.reviewed
        decision = super().review(proposal, portfolio)
        if first:
            return replace(decision, approved=False, reason="thesis unconvincing")
        return decision


def test_a_review_rejection_frees_the_slot_for_the_next_candidate(tmp_path):
    """A rejection is not a submission, so it does not use up a slot."""
    reviewer = RejectFirstReviewer()
    runner, _, _, broker, _ = build_runner(
        tmp_path,
        universe=RANKED_UNIVERSE,
        market=_ranked_market(),
        reviewer=reviewer,
        overrides=_slots(1),
    )

    summary = runner.run_once()
    results = _by_symbol(summary)

    assert results["NVDA"].outcome is Outcome.REVIEW_REJECTED
    assert results["NVDA"].detail.endswith("; thesis unconvincing")
    assert results["MSFT"].outcome is Outcome.FILLED
    assert results["AAPL"].outcome is Outcome.NOT_SELECTED
    assert reviewer.call_count == 2
    assert [p.symbol for p in broker.submitted] == ["MSFT"]


class PortfolioSequence(StubMarketData):
    """Serves the given portfolios (or errors) in order, repeating the last.

    The runner reads the account once per symbol during the scan and once more,
    fresh, before it starts submitting. Listing one entry per scan read and then
    the fresh read lets a test change the book -- or break it -- between the two
    phases, which is the only way the slot count and the scan can disagree.
    """

    def __init__(self, snapshots, responses):
        super().__init__(snapshots)
        self._responses = list(responses)
        self.portfolio_reads = 0

    def portfolio(self) -> Portfolio:
        index = min(self.portfolio_reads, len(self._responses) - 1)
        self.portfolio_reads += 1
        response = self._responses[index]
        if isinstance(response, Exception):
            raise response
        return response


def _book(*symbols: str) -> Portfolio:
    return Portfolio(
        net_liquidation=Decimal(50_000),
        buying_power=Decimal(25_000),
        positions=tuple(Position(symbol=s, quantity=1) for s in symbols),
    )


def test_no_free_slot_ranks_every_candidate_out_without_a_review(tmp_path):
    """The book filled between the scan and submission: nobody is reviewed."""
    scan_reads = [_book()] * len(RANKED_UNIVERSE)
    market = PortfolioSequence(
        _ranked_market()._snapshots, scan_reads + [_book("TSLA", "AMD")]
    )
    runner, _, reviewer, broker, _ = build_runner(
        tmp_path, universe=RANKED_UNIVERSE, market=market, overrides=_slots(2)
    )

    summary = runner.run_once()

    assert {r.outcome for r in summary.results} == {Outcome.NOT_SELECTED}
    assert all("no free position slot (2 open of 2)" in r.detail for r in summary.results)
    assert reviewer.call_count == 0
    assert broker.call_count == 0
    # No re-quote either: nothing was going to be submitted.
    assert sorted(market.requested) == sorted(RANKED_UNIVERSE)
    assert summary.not_selected == 3


def test_a_failed_fresh_portfolio_read_fails_the_candidates_only(tmp_path):
    """Without the slot count nothing can be submitted; the scan's verdicts stand."""
    snapshots = {**_ranked_market()._snapshots, "QQQ": tradable_snapshot("QQQ", 5.0)}
    universe = (*RANKED_UNIVERSE, "QQQ")
    market = PortfolioSequence(
        snapshots, [_book()] * len(universe) + [MarketDataError("account unavailable")]
    )
    runner, _, reviewer, broker, store = build_runner(
        tmp_path, universe=universe, market=market
    )

    summary = runner.run_once()
    results = _by_symbol(summary)

    assert results["QQQ"].outcome is Outcome.NO_TRADE
    for symbol in RANKED_UNIVERSE:
        assert results[symbol].outcome is Outcome.DATA_ERROR
        assert results[symbol].detail.endswith("; account unavailable")
        assert results[symbol].detail.startswith("rank ")
    assert reviewer.call_count == 0
    assert broker.call_count == 0
    assert len(store.attempts()) == 4


# --- same-pass capital -----------------------------------------------------
#
# Buying power is read fresh per symbol, but the venue has not necessarily
# debited an order sent seconds earlier, so the runner subtracts what this pass
# has already sent before the algorithm sizes the next symbol. These tests pin
# that bookkeeping at the runner's own seam -- the portfolio it hands the
# algorithm -- with a canned algorithm, so the contract is observable
# independently of how the real one selects strikes. The last test goes through
# the real algorithm and reads the resulting order quantities instead.
#
# "Earlier" now means earlier in *rank order*, since that is the order in which
# the pass submits. The pairs below are identical on every ranked figure, so the
# tie-break on symbol name puts AAPL before QQQ and the tests read as before.

#: What ``StubMarketData`` reports by default: 50,000 net liquidation, 25,000
#: buying power (tests/fakes.py, ``StubMarketData.__init__``).
FULL_BUYING_POWER = Decimal(25_000)
NET_LIQUIDATION = Decimal(50_000)
#: Defined risk of the canned proposal: 3 contracts x 325.
EFFECT = Decimal(975)


def _leg(action: Action, strike: int, delta: float) -> ProposalLeg:
    return ProposalLeg(
        action=action,
        right=Right.PUT,
        strike=Decimal(strike),
        expiry=GOOD_EXPIRY,
        ratio=1,
        bid=Decimal("1.00"),
        ask=Decimal("1.10"),
        delta=delta,
        open_interest=500,
        volume=100,
    )


def _proposal(symbol: str, effect: Decimal = EFFECT) -> TradeProposal:
    """A fully-formed proposal whose defined risk is ``effect``."""
    return TradeProposal(
        symbol=symbol,
        strategy="put_credit_spread",
        expiry=GOOD_EXPIRY,
        dte=45,
        legs=(_leg(Action.SELL, 185, -0.30), _leg(Action.BUY, 180, -0.20)),
        quantity=3,
        limit_price=Decimal("1.75"),
        max_profit=Decimal(525),
        max_loss=effect,
        underlying_price=Decimal(195),
        iv_rank=45.0,
        short_delta=-0.30,
        buying_power_effect=effect,
        criteria={},
        created_at=SCAN_TIME,
    )


class CannedAlgorithm:
    """Stands in for ``tastytrade.evaluate`` and records the portfolio it saw.

    Returns a fixed decision per symbol. The recorded portfolios are the
    evidence: what the runner handed the algorithm is exactly the sizing input.
    """

    def __init__(self, decisions: dict[str, TradeProposal | NoTrade]) -> None:
        self._decisions = decisions
        self.seen: dict[str, Portfolio] = {}

    def __call__(self, *, symbol, snapshot, portfolio, strategy, risk, now):  # noqa: ARG002
        self.seen[symbol] = portfolio
        return self._decisions[symbol]


def _run_pair(tmp_path, monkeypatch, *, first, broker=None, reviewer=None):
    """Run AAPL then QQQ through the real runner with a canned algorithm.

    ``first`` is AAPL's decision; QQQ always proposes. Returns the summary and
    the algorithm so a test can read what QQQ was sized against.
    """
    algorithm = CannedAlgorithm({"AAPL": first, "QQQ": _proposal("QQQ")})
    monkeypatch.setattr(runner_module.tastytrade, "evaluate", algorithm)
    market = StubMarketData(
        {"AAPL": tradable_snapshot("AAPL"), "QQQ": tradable_snapshot("QQQ")}
    )
    runner, _, _, _, store = build_runner(
        tmp_path, universe=("AAPL", "QQQ"), market=market, broker=broker, reviewer=reviewer
    )
    summary = runner.run_once()
    store.close()
    return summary, algorithm


@pytest.mark.parametrize(
    ("first", "broker", "reviewer", "expected_outcome", "expected_committed"),
    [
        pytest.param(
            _proposal("AAPL"),
            FakeBroker(outcome=Outcome.FILLED),
            None,
            Outcome.FILLED,
            EFFECT,
            id="filled-commits",
        ),
        pytest.param(
            _proposal("AAPL"),
            FakeBroker(outcome=Outcome.WORKING, message="PreSubmitted"),
            None,
            Outcome.WORKING,
            EFFECT,
            id="working-commits",
        ),
        pytest.param(
            # In SUBMITTED_OUTCOMES (models.py) because the order *arrived*;
            # the runner draws the same line and holds its margin for the pass.
            _proposal("AAPL"),
            FakeBroker(outcome=Outcome.BROKER_REJECTED, message="rejected by venue"),
            None,
            Outcome.BROKER_REJECTED,
            EFFECT,
            id="broker-rejected-commits",
        ),
        pytest.param(
            _proposal("AAPL"),
            FakeBroker(error=SubmissionFailed("transport dropped before send")),
            None,
            Outcome.SUBMISSION_FAILED,
            Decimal(0),
            id="submission-failed-commits-nothing",
        ),
        pytest.param(
            _proposal("AAPL"),
            None,
            StubReviewer(approved=False, reason="thesis unconvincing"),
            Outcome.REVIEW_REJECTED,
            Decimal(0),
            id="review-rejected-commits-nothing",
        ),
        pytest.param(
            NoTrade("IV rank 5.0 below minimum 30.0"),
            None,
            None,
            Outcome.NO_TRADE,
            Decimal(0),
            id="no-trade-commits-nothing",
        ),
    ],
)
def test_capital_committed_by_an_earlier_symbol_is_withheld_from_the_next(
    tmp_path, monkeypatch, first, broker, reviewer, expected_outcome, expected_committed
):
    summary, algorithm = _run_pair(
        tmp_path, monkeypatch, first=first, broker=broker, reviewer=reviewer
    )

    assert summary.results[0].outcome is expected_outcome
    # The first symbol always sees the account as reported.
    assert algorithm.seen["AAPL"].buying_power == FULL_BUYING_POWER
    # The second sees it less whatever the first actually sent to the venue.
    assert algorithm.seen["QQQ"].buying_power == FULL_BUYING_POWER - expected_committed
    # The risk budget is a fraction of account value, so that is never reduced.
    assert algorithm.seen["QQQ"].net_liquidation == NET_LIQUIDATION


def test_committed_capital_never_drives_buying_power_below_zero(tmp_path, monkeypatch):
    """Over-commitment floors at zero rather than handing the algorithm a debt."""
    summary, algorithm = _run_pair(
        tmp_path,
        monkeypatch,
        first=_proposal("AAPL", effect=FULL_BUYING_POWER + Decimal(1)),
    )

    assert summary.results[0].outcome is Outcome.FILLED
    assert algorithm.seen["QQQ"].buying_power == Decimal(0)


def test_committed_capital_is_reset_every_pass(tmp_path, monkeypatch):
    """Nothing carries across passes: the second pass starts from the account."""
    algorithm = CannedAlgorithm({"AAPL": _proposal("AAPL")})
    monkeypatch.setattr(runner_module.tastytrade, "evaluate", algorithm)
    runner, _, _, broker, store = build_runner(tmp_path)

    runner.run_while(lambda: True, max_passes=2)
    store.close()

    # The canned algorithm keeps only the latest portfolio per symbol, and on
    # the second pass AAPL is again the first symbol: it must see the full
    # figure, not the figure less what pass one sent.
    assert broker.call_count == 2
    assert algorithm.seen["AAPL"].buying_power == FULL_BUYING_POWER


def test_a_non_zero_decrement_is_logged_at_debug(tmp_path, monkeypatch, caplog):
    with caplog.at_level(logging.DEBUG, logger="ibkr_trader.runner"):
        _run_pair(tmp_path, monkeypatch, first=_proposal("AAPL"))

    debug = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    assert any(line.startswith("QQQ") and "975 committed" in line for line in debug)
    assert not any(line.startswith("AAPL") and "committed" in line for line in debug)


def test_the_real_algorithm_sizes_later_symbols_against_what_is_left(tmp_path):
    """Through the production algorithm: three identical names, one shrinking book.

    Each name qualifies for the fixture's 185/180 spread at 325 defined risk per
    contract. With 1,300 of buying power the risk budget (1,000 -> 3 contracts)
    binds first; after those 975 are sent, 325 remains, which is one contract;
    after that, nothing. The three tie on every ranked figure, so they are
    submitted in name order: AAPL, MSFT, then QQQ finds the book empty.
    """
    market = StubMarketData(
        {s: tradable_snapshot(s) for s in ("AAPL", "QQQ", "MSFT")},
        portfolio=Portfolio(net_liquidation=NET_LIQUIDATION, buying_power=Decimal(1_300)),
    )
    runner, _, _, broker, store = build_runner(
        tmp_path, universe=("AAPL", "QQQ", "MSFT"), market=market
    )

    summary = runner.run_once()
    store.close()

    assert [(p.symbol, p.quantity) for p in broker.submitted] == [("AAPL", 3), ("MSFT", 1)]
    assert [(r.symbol, r.outcome) for r in summary.results] == [
        ("AAPL", Outcome.FILLED),
        ("MSFT", Outcome.FILLED),
        ("QQQ", Outcome.NO_TRADE),
    ]
    assert "on re-quote" in summary.results[2].detail
    assert "buying power 0" in summary.results[2].detail


# --- the bag's own market, recorded before review ----------------------------


def test_the_reviewer_sees_where_the_spread_itself_is_quoted(tmp_path):
    """Every price this engine computes comes from leg arithmetic; a combo fills
    against the bag's own book. Recording it is what makes an unfilled order
    diagnosable afterwards."""
    market = StubMarketData(
        {"AAPL": tradable_snapshot("AAPL")},
        # Wire prices are what is *paid* for the bag, so a credit spread is
        # negative on both sides: crossing here collects 1.50.
        combo_quotes={
            "AAPL": ComboQuote(wire_bid=Decimal("-1.90"), wire_ask=Decimal("-1.50"))
        },
    )
    runner, _, reviewer, _, _ = build_runner(tmp_path, market=market)

    summary = runner.run_once()

    (reviewed,) = reviewer.reviewed
    assert reviewed.criteria["combo_market"] == (
        "bag quotes 1.50 to 1.90 credit, mid 1.70; "
        "our limit 1.75 is 0.25 above the marketable credit"
    )
    assert summary.results[0].outcome is Outcome.FILLED


def test_the_quoted_bag_is_the_bag_that_gets_submitted(tmp_path):
    """A quote of a *different* spread would be worse than no quote: it would
    read as evidence. The legs asked about are the legs transmitted."""
    market = StubMarketData(
        {"AAPL": tradable_snapshot("AAPL")},
        combo_quotes={
            "AAPL": ComboQuote(wire_bid=Decimal("-1.90"), wire_ask=Decimal("-1.50"))
        },
    )
    runner, _, _, broker, _ = build_runner(tmp_path, market=market)

    runner.run_once()

    (symbol, quoted_legs) = market.combo_quoted[0]
    (submitted,) = broker.submitted
    assert symbol == submitted.symbol
    assert quoted_legs == opening_combo_legs(submitted)


def test_a_bag_the_venue_will_not_quote_still_trades(tmp_path):
    """The default case, and the one that must not become a refusal path."""
    runner, _, reviewer, broker, _ = build_runner(
        tmp_path, market=StubMarketData({"AAPL": tradable_snapshot("AAPL")})
    )

    summary = runner.run_once()

    (reviewed,) = reviewer.reviewed
    assert reviewed.criteria["combo_market"] == "not quoted by the venue"
    assert len(broker.submitted) == 1
    assert summary.results[0].outcome is Outcome.FILLED


def test_a_failed_combo_quote_never_costs_a_reviewed_trade(tmp_path):
    """Instrumentation failing must be indistinguishable, to the trade, from
    instrumentation being absent."""
    market = StubMarketData(
        {"AAPL": tradable_snapshot("AAPL")},
        combo_quote_error=MarketDataError("no market data line available"),
    )
    runner, _, reviewer, broker, _ = build_runner(tmp_path, market=market)

    summary = runner.run_once()

    (reviewed,) = reviewer.reviewed
    assert "combo_market" not in reviewed.criteria
    assert len(broker.submitted) == 1
    assert summary.results[0].outcome is Outcome.FILLED
