"""The management half of the market-data adapter: held legs and per-leg quotes.

``portfolio()`` collapses holdings to underlying symbols, which is what the
concentration limits need and exactly what the spread manager cannot use: it
has to know *which* contracts are held, signed, to recognise a spread's legs
and to notice when they are gone. ``option_positions()`` is that view.
``quote()`` is the price of exactly those legs, outside the entry DTE band the
chain scan is confined to.

Both run through the real adapter with injected ``ib``/``api`` doubles, so
``ib_async`` is never imported.
"""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from ibkr_trader.clock import FixedClock
from ibkr_trader.config import build_config
from ibkr_trader.errors import MarketDataError
from ibkr_trader.models import OptionLeg, Right
from ibkr_trader.scanner import CURRENCY, EXCHANGE, IBKRMarketData

from .fakes import ACCOUNT, SCAN_TIME, PumpedDelivery
from .test_scanner_quoting import FAKE_API

EXPIRY = date(2026, 3, 20)


def adapter(ib, api=FAKE_API) -> IBKRMarketData:
    config = build_config({"universe": ["AAPL"], "ibkr": {"account": ACCOUNT}})
    return IBKRMarketData(
        ibkr_config=config.ibkr,
        strategy_config=config.strategy,
        clock=FixedClock(SCAN_TIME),
        ib=ib,
        api=api,
    )


# --- option_positions() ----------------------------------------------------


def option_row(
    strike: float,
    right: str,
    quantity: float,
    expiry: str = "20260320",
    avg_cost: float = 175.0,
    account: str = ACCOUNT,
    symbol: str = "AAPL",
):
    """One ``ib_async.Position`` for an option, as the position stream shapes it."""
    return SimpleNamespace(
        account=account,
        contract=SimpleNamespace(
            secType="OPT",
            symbol=symbol,
            lastTradeDateOrContractMonth=expiry,
            strike=strike,
            right=right,
            localSymbol=f"{symbol} {expiry[2:]}{right}{int(strike * 1000):08d}",
        ),
        position=quantity,
        avgCost=avg_cost,
    )


def stock_row(symbol: str = "AAPL", quantity: float = 100.0):
    return SimpleNamespace(
        account=ACCOUNT,
        contract=SimpleNamespace(secType="STK", symbol=symbol, localSymbol=symbol),
        position=quantity,
        avgCost=190.0,
    )


class PositionsIB:
    def __init__(self, rows=()):
        self._rows = list(rows)

    def positions(self, _account=""):
        return list(self._rows)


def test_held_options_are_reported_per_contract_signed_and_parsed():
    """A short 185 put and a long 180 put come back as two typed legs."""
    ib = PositionsIB(
        [
            option_row(185.0, "P", -3.0, avg_cost=175.0),
            option_row(180.0, "P", 3.0, avg_cost=52.5),
            option_row(200.0, "C", -1.0, expiry="20260417"),
        ]
    )

    held = adapter(ib).option_positions()

    assert [(p.leg.strike, p.leg.right, p.quantity) for p in held] == [
        (Decimal("185.0"), Right.PUT, -3),
        (Decimal("180.0"), Right.PUT, 3),
        (Decimal("200.0"), Right.CALL, -1),
    ]
    assert held[0].leg == OptionLeg(
        symbol="AAPL", expiry=EXPIRY, strike=Decimal("185.0"), right=Right.PUT
    )
    assert held[2].leg.expiry == date(2026, 4, 17), "YYYYMMDD from the position row"
    assert held[0].average_cost == Decimal("175.0"), "the venue's figure, unscaled"
    assert held[1].average_cost == Decimal("52.5")


def test_stock_rows_are_not_option_positions():
    ib = PositionsIB([stock_row(), option_row(185.0, "P", -1.0), stock_row("MSFT")])

    held = adapter(ib).option_positions()

    assert len(held) == 1
    assert held[0].leg.symbol == "AAPL" and held[0].leg.right is Right.PUT


def test_the_long_right_names_resolve_the_same_way():
    """``ib_async`` accepts ``PUT``/``CALL`` as well as ``P``/``C``; so do we."""
    ib = PositionsIB([option_row(185.0, "PUT", -1.0), option_row(200.0, "CALL", 1.0)])

    held = adapter(ib).option_positions()

    assert [p.leg.right for p in held] == [Right.PUT, Right.CALL]


def test_another_accounts_position_row_is_refused():
    """The same guard ``portfolio()`` applies: never read another book's rows."""
    ib = PositionsIB([option_row(185.0, "P", -1.0, account="DU9999999")])

    with pytest.raises(MarketDataError, match="DU9999999"):
        adapter(ib).option_positions()


def test_an_unidentifiable_held_option_is_an_error_not_a_dropped_row():
    """Dropping a leg the adapter cannot name would read as "the leg closed"."""
    ib = PositionsIB([option_row(185.0, "", -1.0)])

    with pytest.raises(MarketDataError, match="cannot be identified"):
        adapter(ib).option_positions()


def test_a_broken_position_stream_is_a_market_data_error():
    class Broken:
        def positions(self, _account=""):
            raise RuntimeError("stream unavailable")

    with pytest.raises(MarketDataError, match="Cannot read IBKR positions"):
        adapter(Broken()).option_positions()


# --- quote() ---------------------------------------------------------------


def leg(strike: str, right: Right = Right.PUT, expiry: date = EXPIRY) -> OptionLeg:
    return OptionLeg(symbol="AAPL", expiry=expiry, strike=Decimal(strike), right=right)


class QuoteIB(PumpedDelivery):
    """The client half of ``quote()``: qualify, then a line-budgeted quote.

    ``books`` maps a strike to ``(bid, ask)``; ``None`` means the venue never
    sent either side (NaN, as ib_async leaves an unsent tick). ``sleep`` is a
    no-op so a dead book runs ``_await_quotes`` to its iteration cap instantly
    rather than blocking.
    """

    def __init__(self, books=None, qualify=None):
        super().__init__()
        self._books = books or {}
        self._qualify = qualify
        self.qualified: list = []
        self.requested: list = []
        self.open: set[int] = set()
        self.market_data_type: int | None = None

    def reqMarketDataType(self, value):
        self.market_data_type = value

    def qualifyContracts(self, *contracts):
        self.qualified.extend(contracts)
        if self._qualify is not None:
            return self._qualify(contracts)
        for contract in contracts:
            contract.conId = int(contract.strike)
        return list(contracts)

    def reqMktData(self, contract, _generic_ticks, _snapshot, _regulatory):
        self.requested.append(contract)
        self.open.add(id(contract))
        book = self._books.get(float(contract.strike), (1.00, 1.10))
        if book is None:
            return SimpleNamespace(contract=contract, bid=math.nan, ask=math.nan)
        bid, ask = book
        return self.serve(SimpleNamespace(
            contract=contract,
            bid=bid,
            ask=ask,
            modelGreeks=SimpleNamespace(delta=-0.30),
            putOpenInterest=500,
            callOpenInterest=700,
            volume=100,
        ))

    def cancelMktData(self, contract):
        self.open.discard(id(contract))

    def sleep(self, _seconds):
        self.pump()


def test_quotes_come_back_one_per_leg_in_the_order_asked():
    """Identity is the leg's own, not re-read off the ticker."""
    ib = QuoteIB(books={185.0: (2.40, 2.50), 180.0: (1.00, 1.10), 200.0: (0.30, 0.40)})
    legs = [leg("185"), leg("180"), leg("200", Right.CALL)]

    quotes = adapter(ib).quote(legs)

    assert [(q.strike, q.right) for q in quotes] == [
        (Decimal("185"), Right.PUT),
        (Decimal("180"), Right.PUT),
        (Decimal("200"), Right.CALL),
    ]
    assert [(q.bid, q.ask) for q in quotes] == [
        (Decimal("2.4"), Decimal("2.5")),
        (Decimal("1.0"), Decimal("1.1")),
        (Decimal("0.3"), Decimal("0.4")),
    ]
    assert all(q.symbol == "AAPL" and q.expiry == EXPIRY for q in quotes)
    assert quotes[0].delta == pytest.approx(-0.30)
    assert quotes[0].open_interest == 500, "put open interest for a put"
    assert quotes[2].open_interest == 700, "call open interest for a call"
    assert ib.open == set(), "every line released before quote() returned"
    assert ib.market_data_type is not None, "the data type is set for this call too"


def test_a_dead_book_is_returned_as_zero_zero_not_dropped():
    """The caller zips the answer against its legs; the length must hold."""
    ib = QuoteIB(books={185.0: (2.40, 2.50), 180.0: None})

    quotes = adapter(ib).quote([leg("185"), leg("180")])

    assert len(quotes) == 2
    dead = quotes[1]
    assert (dead.strike, dead.bid, dead.ask) == (Decimal("180"), Decimal(0), Decimal(0))
    assert dead.spread_pct == math.inf, "a dead book must fail every spread screen"
    assert quotes[0].bid == Decimal("2.4"), "the live leg is unaffected"
    assert ib.open == set()


def test_a_leg_that_will_not_qualify_raises_naming_it():
    """The venue answers positionally, with None for the contract it cannot resolve."""

    def second_unresolved(contracts):
        contracts[0].conId = 185
        return [contracts[0], None]

    ib = QuoteIB(qualify=second_unresolved)

    with pytest.raises(MarketDataError, match=r"AAPL 2026-03-20 180 P"):
        adapter(ib).quote([leg("185"), leg("180")])
    assert ib.requested == [], "nothing may be quoted after a failed qualification"


def test_a_double_that_answers_with_nothing_is_read_as_unresolved_too():
    ib = QuoteIB(qualify=lambda _contracts: [])

    with pytest.raises(MarketDataError, match="did not resolve option leg AAPL"):
        adapter(ib).quote([leg("185")])


def test_a_qualification_failure_is_a_market_data_error():
    def explode(_contracts):
        raise RuntimeError("no security definition")

    with pytest.raises(MarketDataError, match="cannot qualify option legs"):
        adapter(QuoteIB(qualify=explode)).quote([leg("185")])


def test_leg_contracts_are_routed_like_the_brokers_and_carry_no_trading_class():
    """The quote must describe the contract the broker will trade."""
    ib = QuoteIB()

    adapter(ib).quote([leg("185")])

    (contract,) = ib.qualified
    assert contract.symbol == "AAPL"
    assert contract.lastTradeDateOrContractMonth == "20260320"
    assert contract.strike == 185.0
    assert contract.right == "P"
    assert (contract.exchange, contract.currency, contract.multiplier) == (
        EXCHANGE,
        CURRENCY,
        "100",
    )
    assert not hasattr(contract, "tradingClass")


def test_no_legs_means_no_venue_traffic():
    class Untouchable:
        def __getattr__(self, name):
            raise AssertionError(f"quote(()) touched ib.{name}")

    assert adapter(Untouchable()).quote([]) == ()
