"""The line budget, and the quoting path end to end.

``ibkr.refresh_limit`` is the number the whole config layer exists to protect:
an IBKR account holds a finite number of simultaneous market-data lines, and
exceeding it does not raise -- it silently starves later requests. The config
side has five tests. The runtime side had none, in a way the coverage number
actively hid.

``_quote_batches`` reports 100% statement *and* branch coverage. It reaches that
while never once being asked to form a second batch: both existing tests use
five contracts against the default limit of 100. Instrumenting ``_chunks``
across the whole suite gives ``calls producing >1 batch: 0``. The chunking could
be deleted outright -- replaced with a single unbatched loop -- and the suite
would stay green at 100% on that function.

So these tests assert the *semantics* rather than the lines: over limits small
enough to force several batches, no more than ``refresh_limit`` lines are ever
open at once, and every line is released. They cover both enforcement sites --
``_quote_batches`` for market-data lines and ``_qualify`` for contract-detail
requests, the second of which the original candidate never mentioned.

The enforcer is correct today; these went green on the first run. They are a
regression guard for a bound that is otherwise unguarded at runtime, not a bug
reproduction, and the commit message says so.

The end-to-end test at the bottom exists because it now can. Before the ``api``
seam landed, ``snapshot()`` could not be driven at all without ``ib_async``
installed, which is what made "the whole quoting path is uncovered" true when
it was written.
"""

from __future__ import annotations

import math
from datetime import timedelta
from types import SimpleNamespace

import pytest

from ibkr_trader.clock import FixedClock
from ibkr_trader.config import build_config
from ibkr_trader.models import Right
from ibkr_trader.scanner import (
    EXCHANGE,
    GREEKS_WAIT_SECONDS,
    OPTION_GENERIC_TICKS,
    QUOTE_POLL_SECONDS,
    QUOTE_WAIT_SECONDS,
    UNDERLYING_GENERIC_TICKS,
    IBKRMarketData,
)

from .fakes import ACCOUNT, SCAN_TIME, PumpedDelivery

# --- vendor doubles ------------------------------------------------------


def _stock(symbol, exchange, currency, **kwargs):
    return SimpleNamespace(
        symbol=symbol, exchange=exchange, currency=currency, conId=0, **kwargs
    )


def _option(symbol, expiry, strike, right, exchange, **kwargs):
    # `expiry` is positional here because that is how the adapter calls it, but
    # it is read back under the vendor's own name -- see scanner.py:914.
    return SimpleNamespace(
        symbol=symbol,
        lastTradeDateOrContractMonth=expiry,
        strike=strike,
        right=right,
        exchange=exchange,
        conId=0,
        **kwargs,
    )


FAKE_API = SimpleNamespace(Stock=_stock, Option=_option)


def option_ticker(contract):
    """A complete, healthy option ticker.

    Both sides are finite and present from the first poll, so ``_has_market``
    short-circuits ``_await_quotes`` and ``ib.sleep`` is never reached. Without
    that the loop burns its full iteration cap against a FixedClock whose
    ``now()`` never advances past the deadline.
    """
    return SimpleNamespace(
        contract=contract,
        bid=1.00,
        ask=1.10,
        modelGreeks=SimpleNamespace(delta=-0.30),
        putOpenInterest=500,
        volume=100,
    )


class BudgetIB(PumpedDelivery):
    """Records how many market-data lines are open at once, and batch sizes.

    ``peak`` is the number the line budget is actually about. Counting requests
    or cancels -- which the existing doubles do -- cannot detect a limit
    violation, because the totals are identical however the work is batched.
    """

    def __init__(self, chains=(), bars=()):
        super().__init__()
        self._chains = list(chains)
        self._bars = list(bars)
        self.open: set[int] = set()
        self.peak = 0
        self.qualify_batches: list[int] = []
        self.quote_requests = 0
        self.cancels = 0
        self.history_requests = 0
        self.market_data_type: int | None = None

    # -- the two enforcement sites -------------------------------------

    def reqMktData(self, contract, generic_ticks, snapshot, regulatory):
        self.quote_requests += 1
        self.open.add(id(contract))
        self.peak = max(self.peak, len(self.open))
        return self.serve(option_ticker(contract))

    def cancelMktData(self, contract):
        self.cancels += 1
        self.open.discard(id(contract))

    def qualifyContracts(self, *contracts):
        self.qualify_batches.append(len(contracts))
        for index, contract in enumerate(contracts, start=1):
            contract.conId = int(getattr(contract, "strike", 0) or 0) * 1000 + index
        return list(contracts)

    # -- the rest of the surface snapshot() touches ---------------------

    def reqMarketDataType(self, value):
        self.market_data_type = value

    def reqSecDefOptParams(self, symbol, fut_fop_exchange, sec_type, con_id):
        return list(self._chains)

    def reqHistoricalData(self, contract, **kwargs):
        self.history_requests += 1
        return list(self._bars)

    def sleep(self, seconds):
        self.pump()


def adapter(ib, refresh_limit=None, clock=None):
    ibkr: dict = {"account": ACCOUNT}
    if refresh_limit is not None:
        ibkr["refresh_limit"] = refresh_limit
    overrides: dict = {"universe": ["AAPL"], "ibkr": ibkr}
    config = build_config(overrides)
    return IBKRMarketData(
        ibkr_config=config.ibkr,
        strategy_config=config.strategy,
        clock=clock or FixedClock(SCAN_TIME),
        ib=ib,
        api=FAKE_API,
    )


# --- INT-012: the budget is enforced, not merely covered ----------------

CONTRACT_COUNT = 21
LIMITS = [1, 3, 7]


@pytest.mark.parametrize("limit", LIMITS)
def test_no_more_lines_are_open_at_once_than_the_budget_allows(limit):
    """The property the whole config ceiling exists to produce."""
    ib = BudgetIB()
    contracts = [SimpleNamespace(strike=i) for i in range(CONTRACT_COUNT)]

    collected = adapter(ib, limit)._quote_batches(ib, contracts, "")

    assert len(collected) == CONTRACT_COUNT, "every contract must still be quoted"
    assert ib.peak <= limit, f"{ib.peak} lines open at once against a budget of {limit}"
    assert ib.open == set(), "lines leaked"
    assert ib.cancels == CONTRACT_COUNT


@pytest.mark.parametrize("limit", LIMITS)
def test_the_budget_actually_forces_more_than_one_batch(limit):
    """Guards the guard.

    Both pre-existing tests quote five contracts against the default limit of
    100, so no second batch has ever formed anywhere in this suite. A budget
    test that also fits in one batch would assert nothing, and would report the
    same 100% coverage while doing it.
    """
    ib = BudgetIB()
    contracts = [SimpleNamespace(strike=i) for i in range(CONTRACT_COUNT)]

    adapter(ib, limit)._quote_batches(ib, contracts, "")

    assert math.ceil(CONTRACT_COUNT / limit) > 1, "this limit does not exercise batching"
    assert ib.peak == min(limit, CONTRACT_COUNT), (
        "each batch should fill the budget before being released"
    )


@pytest.mark.parametrize("limit", LIMITS)
def test_contract_qualification_respects_the_same_budget(limit):
    """The second enforcement site, which the candidate never named.

    ``_qualify`` spends contract-detail requests rather than market-data lines,
    but IBKR paces both and the adapter deliberately reuses the one configured
    number. Nothing asserted it.
    """
    ib = BudgetIB()
    candidates = [SimpleNamespace(strike=i + 1) for i in range(CONTRACT_COUNT)]

    qualified = adapter(ib, limit)._qualify(ib, "AAPL", candidates)

    assert len(qualified) == CONTRACT_COUNT
    assert ib.qualify_batches, "qualification never ran"
    assert max(ib.qualify_batches) <= limit, (
        f"a qualification batch of {max(ib.qualify_batches)} exceeds the budget {limit}"
    )
    assert len(ib.qualify_batches) == math.ceil(CONTRACT_COUNT / limit)


# --- the quoting path, end to end ---------------------------------------


def historical_bars(count=252):
    """Enough non-flat closes for a percentile to mean something.

    ``_percentile_rank`` returns None below MIN_HISTORY_BARS or on a flat
    series, and ``_iv_rank`` then raises rather than defaulting.
    """
    return [SimpleNamespace(close=100.0 + 5.0 * math.sin(i / 7)) for i in range(count)]


def chain_row(strikes, expiries, trading_class="AAPL"):
    return SimpleNamespace(
        exchange=EXCHANGE,
        tradingClass=trading_class,
        strikes=list(strikes),
        expirations=[d.strftime("%Y%m%d") for d in expiries],
    )


class SnapshotIB(BudgetIB):
    """A BudgetIB that also answers the underlying-price request.

    ``underlying_iv`` is what generic tick 106 puts in the underlying ticker's
    ``impliedVolatility``; left NaN by default, as IBKR leaves it before the
    tick arrives, so the strike window falls back to its floor.
    """

    def __init__(self, chains=(), bars=(), underlying_iv=math.nan):
        super().__init__(chains, bars)
        self._underlying_iv = underlying_iv

    def reqMktData(self, contract, generic_ticks, snapshot, regulatory):
        if getattr(contract, "strike", None) is None:
            # the underlying: priced from the live book, not a greek
            self.quote_requests += 1
            self.open.add(id(contract))
            self.peak = max(self.peak, len(self.open))
            return self.serve(
                SimpleNamespace(
                    contract=contract,
                    last=195.0,
                    bid=194.9,
                    ask=195.1,
                    close=190.0,
                    impliedVolatility=self._underlying_iv,
                )
            )
        return super().reqMktData(contract, generic_ticks, snapshot, regulatory)


def test_a_whole_snapshot_can_be_taken_without_the_vendor_installed():
    """What the api seam bought, asserted rather than asserted-about.

    Before the seam this call was unreachable from a test at all: the two
    contract-building methods loaded ``ib_async`` directly, and ``snapshot``
    calls both. That is what made "the whole quoting path is uncovered" true.

    Strikes sit at and below spot deliberately -- STRIKE_WINDOW_ABOVE is 0.0,
    so a chain listing only strikes above spot narrows to nothing and raises.
    """
    expiries = [SCAN_TIME.date() + timedelta(days=d) for d in (30, 45)]
    ib = SnapshotIB(
        chains=[chain_row([175, 180, 185, 190, 195], expiries)],
        bars=historical_bars(),
    )

    snapshot = adapter(ib, refresh_limit=4).snapshot("AAPL")

    assert snapshot.symbol == "AAPL"
    assert snapshot.underlying_price == pytest.approx(195.0)
    assert snapshot.as_of == SCAN_TIME
    assert snapshot.chain, "the chain came back empty"
    assert {q.right for q in snapshot.chain} == {Right.PUT}
    assert set(snapshot.expiries()) == set(expiries)
    assert 0.0 <= snapshot.iv_rank <= 100.0
    assert snapshot.trading_class == "AAPL", (
        "the class the quotes describe must reach the caller"
    )

    # The budget holds across a real pass, not only in the unit above.
    assert ib.peak <= 4, f"{ib.peak} lines open at once against a budget of 4"
    assert ib.open == set(), "the pass leaked market-data lines"
    assert ib.market_data_type is not None, "the data type is set once per snapshot"


def test_every_quoted_contract_is_released_before_the_snapshot_returns():
    """The invariant the module docstring calls load-bearing.

    'Every market-data line this adapter opens is closed before snapshot()
    returns, so a pass leaves the connection exactly as it found it.' Nothing
    asserted that over a whole pass.
    """
    expiries = [SCAN_TIME.date() + timedelta(days=d) for d in (30, 45)]
    ib = SnapshotIB(chains=[chain_row([180, 185, 190, 195], expiries)], bars=historical_bars())

    adapter(ib, refresh_limit=3).snapshot("AAPL")

    assert ib.cancels == ib.quote_requests, (
        f"{ib.quote_requests} lines opened, {ib.cancels} cancelled"
    )
    assert ib.open == set()


def test_a_chain_listing_only_strikes_above_spot_fails_loudly():
    """Not silently as an empty chain -- the runner needs a named data error."""
    from ibkr_trader.errors import MarketDataError

    expiries = [SCAN_TIME.date() + timedelta(days=30)]
    ib = SnapshotIB(chains=[chain_row([300, 310, 320], expiries)], bars=historical_bars())

    with pytest.raises(MarketDataError, match="no listed strike"):
        adapter(ib, refresh_limit=4).snapshot("AAPL")

    assert ib.open == set(), "a failed pass must not leak lines either"


def test_the_underlying_iv_widens_the_strike_window_through_a_real_snapshot():
    """The reading has to travel from the ticker to the window, not only exist.

    Spot is 195. At the 0.15 floor the window stops at 165.75, so 150 is out.
    At IV 0.60 the window is 1.2 * 0.60 * sqrt(60/365) = 0.292, the floor
    drops to 138.1 and 150 is in. Read off ``Ticker.impliedVolatility`` --
    which is where generic tick 106 lands in ib_async 2.1.
    """
    expiries = [SCAN_TIME.date() + timedelta(days=30)]
    chains = [chain_row([150, 170, 180, 190, 195], expiries)]

    calm = SnapshotIB(chains=chains, bars=historical_bars())
    calm_strikes = {float(q.strike) for q in adapter(calm, 4).snapshot("AAPL").chain}
    assert 150.0 not in calm_strikes, "the floor should not reach 23% OTM"
    assert 170.0 in calm_strikes

    volatile = SnapshotIB(chains=chains, bars=historical_bars(), underlying_iv=0.60)
    volatile_strikes = {float(q.strike) for q in adapter(volatile, 4).snapshot("AAPL").chain}
    assert 150.0 in volatile_strikes, f"IV 0.60 did not widen the window: {volatile_strikes}"


# --- the IV-rank cache ----------------------------------------------------
#
# IBKR paces historical data at roughly 60 requests per 10 minutes. A 102-name
# universe on a 30-minute loop would exceed that every pass unless the rank --
# a function of daily bars, so constant within a market date -- is remembered.


def _one_day_chain():
    # Mid-band, not on ``min_dte``: one of the tests below advances the clock
    # a day, and an expiry sitting exactly on the boundary would then fall out
    # of the DTE band and fail the pass for a reason unrelated to the cache.
    expiries = [SCAN_TIME.date() + timedelta(days=45)]
    return [chain_row([180, 185, 190, 195], expiries)]


def test_a_second_snapshot_on_the_same_market_date_requests_no_history():
    """The hit skips ``reqHistoricalData`` entirely; nothing else is skipped."""
    ib = SnapshotIB(chains=_one_day_chain(), bars=historical_bars())
    market = adapter(ib, 4)

    first = market.snapshot("AAPL")
    after_first = ib.history_requests
    assert after_first == 1, f"the first pass should fetch history once, not {after_first}"

    quotes_before = ib.quote_requests
    second = market.snapshot("AAPL")

    assert ib.history_requests == after_first, "the second pass re-fetched history"
    assert second.iv_rank == first.iv_rank
    assert ib.quote_requests > quotes_before, "quotes must still be refreshed every pass"
    assert ib.open == set()


def test_the_cache_is_per_symbol():
    """A hit for AAPL must not answer for MSFT."""
    ib = SnapshotIB(chains=_one_day_chain(), bars=historical_bars())
    market = adapter(ib, 4)

    market.snapshot("AAPL")
    market.snapshot("MSFT")

    assert ib.history_requests == 2


def test_a_new_market_date_refetches_the_history():
    """Daily bars gain a row at the close; the next market date must see it.

    The clock crosses midnight *Eastern*, which is what the key is built on.
    SCAN_TIME is 14:30 UTC on the 15th; 12 hours later it is 02:30 UTC on the
    16th, still the 15th in New York, and no refetch is due yet. Six more
    hours make it the 16th in both zones.
    """
    clock = FixedClock(SCAN_TIME)
    ib = SnapshotIB(chains=_one_day_chain(), bars=historical_bars())
    market = adapter(ib, 4, clock=clock)

    market.snapshot("AAPL")
    clock.advance(12 * 3600)  # 02:30 UTC on the 16th == 21:30 Eastern on the 15th
    market.snapshot("AAPL")
    assert ib.history_requests == 1, "still the same Eastern market date"

    clock.advance(6 * 3600)  # 08:30 UTC on the 16th == 03:30 Eastern on the 16th
    market.snapshot("AAPL")
    assert ib.history_requests == 2, "a new market date must refetch"


def test_a_failed_rank_is_not_cached():
    """A transient failure must not be remembered for the rest of the day.

    With no bars both the IV and the realized-volatility series are empty, so
    ``_iv_rank`` raises after two history requests. When the bars appear on
    the next pass the rank must be computed, not the failure replayed.
    """
    from ibkr_trader.errors import MarketDataError

    ib = SnapshotIB(chains=_one_day_chain(), bars=[])
    market = adapter(ib, 4)

    with pytest.raises(MarketDataError, match="no usable volatility history"):
        market.snapshot("AAPL")
    failed_requests = ib.history_requests
    assert failed_requests == 2, "IV then TRADES: both series were tried"

    ib._bars = historical_bars()
    snapshot = market.snapshot("AAPL")

    assert ib.history_requests > failed_requests, "the failure was served from cache"
    assert 0.0 <= snapshot.iv_rank <= 100.0


def test_a_non_standard_trading_class_reaches_the_snapshot_intact():
    """The adapter is the only thing that knows which instrument was quoted.

    If it reports "" here, the algorithm's instrument-identity guard silently
    stops guarding: an empty class means "not reported", so every non-standard
    chain would look ordinary. Asserted through a real snapshot() rather than a
    double, because a double asserting its own input proves nothing about the
    adapter.
    """
    expiries = [SCAN_TIME.date() + timedelta(days=30)]
    ib = SnapshotIB(
        chains=[chain_row([180, 185, 190, 195], expiries, trading_class="AAPL1")],
        bars=historical_bars(),
    )

    snapshot = adapter(ib, refresh_limit=4).snapshot("AAPL")

    assert snapshot.trading_class == "AAPL1"


# --- late-arriving fields: greeks and open interest ----------------------


class LateFieldsIB:
    """Tickers whose greeks and open interest land *after* top of book.

    Every other double in this suite serves a *static* ticker, which is exactly
    why no test here could observe the ordering that defines the defect: bid and
    ask arrive first, model greeks and open interest one or more packets later,
    and ``_quote_batches`` cancels the line the instant ``_await_quotes``
    returns. A wait that is satisfied by bid/ask alone therefore guarantees the
    two fields the strategy screens on are never seen.

    ``polls`` counts pumps, so a test can assert *when* the wait ended rather
    than only what it collected.
    """

    def __init__(self, greeks_after=2, oi_after=4):
        self._greeks_after = greeks_after
        self._oi_after = oi_after
        self.tickers: list[SimpleNamespace] = []
        self.polls = 0
        self.open: set[int] = set()

    def reqMktData(self, contract, generic_ticks, snapshot, regulatory):
        self.open.add(id(contract))
        # Nothing is populated at subscription time: top of book is the *first*
        # thing to arrive, not something already there. That ordering is the
        # whole point of this double, and pre-setting it hid the fact that a
        # subscription which never pumps sees nothing at all.
        ticker = SimpleNamespace(
            contract=contract,
            bid=math.nan,
            ask=math.nan,
            modelGreeks=None,
            putOpenInterest=math.nan,
            volume=math.nan,
        )
        self.tickers.append(ticker)
        return ticker

    def cancelMktData(self, contract):
        self.open.discard(id(contract))

    def sleep(self, seconds):
        self.polls += 1
        for index, ticker in enumerate(self.tickers):
            if self.polls >= 1 + index:
                ticker.bid = 1.00
                ticker.ask = 1.10
                ticker.volume = 100
            if self.polls >= self._greeks_after + index:
                ticker.modelGreeks = SimpleNamespace(delta=-0.30)
            if self.polls >= self._oi_after + index:
                ticker.putOpenInterest = 500


def _late_contract(strike):
    """A put contract carrying the identity fields ``_build_quote`` reads."""
    expiry = (SCAN_TIME.date() + timedelta(days=45)).strftime("%Y%m%d")
    return _option("AAPL", expiry, strike, Right.PUT.value, EXCHANGE)


def _late_quote(ib, contract, market):
    """Quote one contract through the real batching path and build its quote."""
    (ticker,) = market._quote_batches(ib, [contract], OPTION_GENERIC_TICKS)
    return market._build_quote("AAPL", ticker)


def test_greeks_that_arrive_after_top_of_book_still_reach_the_quote():
    """The bug: a wait satisfied by bid/ask alone throws away delta and OI.

    ``_await_quotes`` checked ``all(_has_market(...))`` -- bid and ask only --
    *before* the first pump, and ``_quote_batches`` cancels the subscription the
    moment it returns. So on any ticker whose book is already up, the greeks and
    open interest that were one packet away were never collected, and
    ``_build_quote`` silently substituted 0.0 and 0.

    Downstream that is indistinguishable from a real market: a 0.0 delta fails
    both delta bands and an open interest of 0 fails ``min_open_interest``, so
    every symbol reports NO_TRADE for what looks like a market condition.
    """
    ib = LateFieldsIB(greeks_after=2, oi_after=4)
    contract = _late_contract(185)

    quote = _late_quote(ib, contract, adapter(ib))

    assert ib.polls > 0, "the wait returned without pumping, so nothing could arrive"
    assert quote.delta == pytest.approx(-0.30), "the real delta was thrown away"
    assert quote.open_interest == 500, "the real open interest was thrown away"


def test_the_wait_ends_on_the_last_ticker_not_the_first():
    """Per-ticker, not per-batch: one straggler must not be abandoned.

    ``LateFieldsIB`` staggers each ticker by its index, so the second completes
    strictly later than the first. The wait must run until the last one is
    ready -- and then stop, rather than spending the rest of the budget.
    """
    ib = LateFieldsIB(greeks_after=1, oi_after=2)
    contracts = [_late_contract(185), _late_contract(180)]
    market = adapter(ib)

    tickers = market._quote_batches(ib, contracts, OPTION_GENERIC_TICKS)
    quotes = [market._build_quote("AAPL", ticker) for ticker in tickers]

    assert [q.open_interest for q in quotes] == [500, 500], "a straggler was abandoned"
    assert ib.polls == 3, "the wait should end on the last ticker, not run to the cap"


def test_a_ticker_that_never_reports_open_interest_degrades_rather_than_aborting():
    """One silent strike must not cost the batch its whole budget.

    Open interest that never arrives is a real condition, and the documented
    fallback (0) is correct. What must not happen is paying the full
    ``QUOTE_WAIT_SECONDS`` for it on every batch: once the whole batch has a
    two-sided book, only the nested grace window is spent.
    """
    ib = LateFieldsIB(greeks_after=1, oi_after=9_999)
    contract = _late_contract(185)

    quote = _late_quote(ib, contract, adapter(ib))

    assert quote.delta == pytest.approx(-0.30), "delta did arrive and must be kept"
    assert quote.open_interest == 0, "the documented fallback still applies"
    grace_polls = int(GREEKS_WAIT_SECONDS / QUOTE_POLL_SECONDS)
    cap = int(QUOTE_WAIT_SECONDS / QUOTE_POLL_SECONDS)
    assert ib.polls <= grace_polls + 1, "the grace window, not the full wait, bounds this"
    assert ib.polls < cap, "the batch must not burn its whole budget on one silent strike"


def test_the_underlying_never_waits_for_greeks_it_did_not_subscribe_to():
    """Completeness is keyed on the ticks the request actually paid for.

    ``UNDERLYING_GENERIC_TICKS`` does not ask for open interest, and the
    underlying ticker carries no greeks at all. Applying the option predicate
    to it would make every symbol pay the grace window for fields that are never
    coming -- so ``BudgetIB.sleep`` raising is the assertion here.
    """
    ib = BudgetIB()
    contracts = [SimpleNamespace(strike=None)]

    collected = adapter(ib)._quote_batches(ib, contracts, UNDERLYING_GENERIC_TICKS)

    assert len(collected) == 1, "the underlying was still quoted"
