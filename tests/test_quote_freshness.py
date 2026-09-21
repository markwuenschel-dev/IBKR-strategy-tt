"""A populated ticker must not satisfy a new decision.

Every other double in this suite hands back a ticker built fresh per
``reqMktData`` call, so no test here can observe what the vendor library
actually does: ``ib_async`` keeps one ``Ticker`` per contract, keyed by
``hash(contract)``, which for a non-BAG contract *is* the ``conId``
(``contract.py:161-185``). ``Wrapper.startTicker`` returns the existing object
when one is present (``wrapper.py:406-409``) and ``Wrapper.endTicker`` removes
only the reqId mappings -- it never drops the ticker and never clears a field
(``wrapper.py:416-419``). So the values a subscription wrote survive its own
cancellation and are visible to the next subscription on that contract.

``_await_quotes`` evaluates its completeness predicate *before* the first pump
(``scanner.py:1441-1444``). For the underlying the predicate is the weak one --
``_completeness`` returns ``_has_market`` because ``UNDERLYING_GENERIC_TICKS``
carries no open-interest tick (``scanner.py:1494``) -- and the underlying is
requested as a batch of one (``scanner.py:1321``). A ticker still carrying the
previous pass's bid and ask therefore satisfies the wait on iteration zero, the
loop returns having pumped nothing, and ``_quote_batches`` cancels the line in
its ``finally`` before a single packet has been read.

The second test covers the same wait being ended by the *absence* of a book
rather than by a stale one. ``ib_async`` writes ``Defaults.emptyPrice``, which
is ``-1`` and not NaN (``objects.py:587``), whenever a bid or ask arrives with
size 0 (``wrapper.py:990-1012``), and a size-only tick of 0 erases the price the
same way (``wrapper.py:1064-1085``). ``_finite`` rejects only None, NaN and Inf
(``scanner.py:210-227``), so ``-1`` passes it and ``_has_market`` reads an empty
book as a present one. ``_two_sided`` rejects it correctly afterwards
(``scanner.py:1581-1595``) -- but by then the wait has returned and the line is
gone. This is the same ``-1`` sentinel class that ``6377271`` fixed for bag
quotes, on a path that fix did not reach.

These are acceptance tests for a measurement, not regression guards: they
describe the behaviour a capture session needs in order to record the market
rather than the cache. Another successful *first* subscription tests nothing.
"""

from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from ibkr_trader.clock import FixedClock
from ibkr_trader.config import build_config
from ibkr_trader.scanner import (
    CURRENCY,
    EXCHANGE,
    UNDERLYING_GENERIC_TICKS,
    IBKRMarketData,
)

from .fakes import ACCOUNT, SCAN_TIME

CON_ID = 265598


def _stock(symbol, exchange, currency, **kwargs):
    return SimpleNamespace(symbol=symbol, exchange=exchange, currency=currency, **kwargs)


def _option(symbol, expiry, strike, right, exchange, **kwargs):
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


def adapter(ib):
    config = build_config({"universe": ["AAPL"], "ibkr": {"account": ACCOUNT}})
    return IBKRMarketData(
        ibkr_config=config.ibkr,
        strategy_config=config.strategy,
        clock=FixedClock(SCAN_TIME),
        ib=ib,
        api=FAKE_API,
    )


class CachingIB:
    """The vendor's ticker cache, reproduced.

    One ticker per ``conId``, created on first subscription and thereafter
    handed back still carrying whatever the last subscription wrote. Cancelling
    detaches the subscription; it does not clear the object.

    A fresh price arrives only after ``fresh_after`` pumps, and only while a
    subscription is actually live -- which is what makes "did this decision wait
    for a new observation?" an answerable question rather than a matter of
    inspecting a field that was already there.
    """

    def __init__(self, fresh_after: int = 2) -> None:
        self.tickers: dict[int, SimpleNamespace] = {}
        self.polls = 0
        self.subscriptions = 0
        self.open: set[int] = set()
        self._fresh_after = fresh_after
        self._live: set[int] = set()
        self._pumped: dict[int, int] = {}
        self._pending: dict[int, float] = {}

    def reqMktData(self, contract, _generic_ticks, _snapshot, _regulatory):
        con_id = contract.conId
        self.subscriptions += 1
        ticker = self.tickers.get(con_id)
        if ticker is None:
            # ib_async's Ticker starts every price field at NaN.
            ticker = SimpleNamespace(
                contract=contract,
                bid=math.nan,
                ask=math.nan,
                last=math.nan,
                close=math.nan,
                volume=math.nan,
                modelGreeks=None,
                lastGreeks=None,
                putOpenInterest=math.nan,
                impliedVolatility=math.nan,
            )
            self.tickers[con_id] = ticker
        self.open.add(con_id)
        self._live.add(con_id)
        self._pumped[con_id] = 0
        # Each subscription would observe a different market.
        self._pending[con_id] = 100.0 + self.subscriptions
        return ticker

    def cancelMktData(self, contract):
        self.open.discard(contract.conId)
        self._live.discard(contract.conId)

    def sleep(self, _seconds):
        self.polls += 1
        for con_id in list(self._live):
            self._pumped[con_id] += 1
            if self._pumped[con_id] >= self._fresh_after:
                ticker = self.tickers[con_id]
                price = self._pending[con_id]
                ticker.last = price
                ticker.bid = price - 0.05
                ticker.ask = price + 0.05


class EmptyBookIB(CachingIB):
    """A contract whose book is genuinely empty when the subscription opens.

    ``-1`` on both sides is exactly what the vendor writes for a side with no
    size. It is not a price, and a decision must not be made from it.
    """

    def reqMktData(self, contract, generic_ticks, snapshot, regulatory):
        ticker = super().reqMktData(contract, generic_ticks, snapshot, regulatory)
        ticker.bid = -1.0
        ticker.ask = -1.0
        return ticker


def _underlying():
    return _stock("AAPL", EXCHANGE, CURRENCY, conId=CON_ID)


def test_a_resubscription_waits_for_a_new_observation():
    """The acceptance test: old fields must not answer a new question.

    Subscribe, let a real price arrive, release the line, then subscribe again
    on the same contract. The second decision must be made from what the second
    subscription observed -- not from what the first one left behind.
    """
    ib = CachingIB(fresh_after=2)
    market = adapter(ib)
    contract = _underlying()

    first = market._quote_underlying(ib, contract)
    assert float(market._underlying_price("AAPL", first)) == pytest.approx(101.0)
    polls_after_first = ib.polls
    assert polls_after_first > 0, "the fake never delivered anything; the test is vacuous"
    assert not ib.open, "the first line was not released"

    second = market._quote_underlying(ib, contract)

    assert ib.polls > polls_after_first, (
        "the second subscription returned without pumping: the wait was satisfied "
        "by fields the first subscription left on the cached ticker"
    )
    assert float(market._underlying_price("AAPL", second)) == pytest.approx(102.0), (
        "the decision used the previous subscription's price"
    )


def test_an_empty_book_does_not_satisfy_the_wait():
    """``-1`` is the vendor's 'no size on this side', not a quote.

    ``_has_market`` asks only whether the fields are finite, and ``-1`` is
    finite, so an empty book ends the wait and the line is cancelled before a
    real quote can arrive.
    """
    ib = EmptyBookIB(fresh_after=2)
    market = adapter(ib)

    ticker = market._quote_underlying(ib, _underlying())

    assert ib.polls > 0, (
        "the wait ended on a bid and ask of -1, which is the vendor's empty-book "
        "sentinel rather than a two-sided market"
    )
    assert float(market._underlying_price("AAPL", ticker)) == pytest.approx(101.0)


def test_the_completeness_predicate_rejects_the_empty_book_sentinel():
    """The unit beneath both tests above, asserted directly."""
    empty = SimpleNamespace(bid=-1.0, ask=-1.0)
    real = SimpleNamespace(bid=1.00, ask=1.10)

    assert IBKRMarketData._has_market(real) is True
    assert IBKRMarketData._has_market(empty) is False, (
        "a bid and ask of -1 is an empty book; treating it as present is what "
        "lets the quote wait return before any market has arrived"
    )


def test_the_underlying_is_quoted_with_the_weak_completeness_predicate():
    """Why the defect bites hardest on the underlying, pinned as a fact.

    ``UNDERLYING_GENERIC_TICKS`` carries no open-interest tick, so
    ``_completeness`` selects ``_has_market`` -- bid and ask alone -- and the
    underlying is requested one contract at a time. A batch of one that is
    already 'complete' pumps zero times.
    """
    predicate = IBKRMarketData._completeness(UNDERLYING_GENERIC_TICKS)

    assert predicate is IBKRMarketData._has_market
