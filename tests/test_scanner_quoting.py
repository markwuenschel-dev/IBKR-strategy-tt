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
from ibkr_trader.scanner import EXCHANGE, IBKRMarketData

from .fakes import ACCOUNT, SCAN_TIME

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


class BudgetIB:
    """Records how many market-data lines are open at once, and batch sizes.

    ``peak`` is the number the line budget is actually about. Counting requests
    or cancels -- which the existing doubles do -- cannot detect a limit
    violation, because the totals are identical however the work is batched.
    """

    def __init__(self, chains=(), bars=()):
        self._chains = list(chains)
        self._bars = list(bars)
        self.open: set[int] = set()
        self.peak = 0
        self.qualify_batches: list[int] = []
        self.quote_requests = 0
        self.cancels = 0
        self.market_data_type: int | None = None

    # -- the two enforcement sites -------------------------------------

    def reqMktData(self, contract, generic_ticks, snapshot, regulatory):
        self.quote_requests += 1
        self.open.add(id(contract))
        self.peak = max(self.peak, len(self.open))
        return option_ticker(contract)

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
        return list(self._bars)

    def sleep(self, seconds):
        raise AssertionError("a healthy ticker must not need polling")


def adapter(ib, refresh_limit=None):
    ibkr: dict = {"account": ACCOUNT}
    if refresh_limit is not None:
        ibkr["refresh_limit"] = refresh_limit
    overrides: dict = {"universe": ["AAPL"], "ibkr": ibkr}
    config = build_config(overrides)
    return IBKRMarketData(
        ibkr_config=config.ibkr,
        strategy_config=config.strategy,
        clock=FixedClock(SCAN_TIME),
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
    """A BudgetIB that also answers the underlying-price request."""

    def reqMktData(self, contract, generic_ticks, snapshot, regulatory):
        if getattr(contract, "strike", None) is None:
            # the underlying: priced from the live book, not a greek
            self.quote_requests += 1
            self.open.add(id(contract))
            self.peak = max(self.peak, len(self.open))
            return SimpleNamespace(
                contract=contract, last=195.0, bid=194.9, ask=195.1, close=190.0
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
