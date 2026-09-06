"""The contract-building half of the market-data adapter, without the vendor.

``IBKRMarketData`` already accepts its *client* (``ib``), which is why the
quoting helpers are testable. It does not accept the ``ib_async`` *module*, and
two methods need the module rather than the client because they **construct**
contracts: ``_qualified_underlying`` builds a ``Stock`` and ``_chain_contracts``
builds every candidate ``Option``. Both call the module-level loader directly,
so on a machine without ``ib_async`` they raise before doing anything a test
could observe -- and ``snapshot`` calls both, which is what puts the whole
snapshot path out of reach.

``IBKRBroker`` solved exactly this with an injected ``api`` seam
(``broker.py`` ``IBApi``), which is what lets ``tests/test_broker_encoding.py``
drive the real encoder with the dependency absent. These tests hold the market
data adapter to the same standard.

Every test here fails before the seam exists: ``IBKRMarketData`` has no ``api``
parameter, so construction raises ``TypeError``.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from ibkr_trader.clock import FixedClock
from ibkr_trader.config import build_config
from ibkr_trader.errors import MarketDataError
from ibkr_trader.models import Right
from ibkr_trader.scanner import CURRENCY, EXCHANGE, IBKRMarketData

from .fakes import ACCOUNT, SCAN_TIME

# --- a fake ib_async module surface --------------------------------------
#
# Mirrors tests/test_broker_encoding.py's FAKE_API. Both constructors record
# their arguments positionally and by keyword, because the assertions below are
# about the exact call the adapter makes.


def _stock(symbol, exchange, currency, **kwargs):
    return SimpleNamespace(
        kind="Stock",
        symbol=symbol,
        exchange=exchange,
        currency=currency,
        conId=0,
        **kwargs,
    )


def _option(symbol, expiry, strike, right, exchange, **kwargs):
    return SimpleNamespace(
        kind="Option",
        symbol=symbol,
        expiry=expiry,
        strike=strike,
        right=right,
        exchange=exchange,
        conId=0,
        **kwargs,
    )


FAKE_API = SimpleNamespace(Stock=_stock, Option=_option)


class FakeIB:
    """The client half of the seam -- already injectable today."""

    def __init__(self, chains=(), qualify=None):
        self._chains = chains
        self._qualify = qualify
        self.qualified: list = []
        self.chain_requests: list = []

    def qualifyContracts(self, *contracts):
        self.qualified.extend(contracts)
        if self._qualify is not None:
            return self._qualify(contracts)
        # IBKR stamps a conId on a resolved contract; mirror that.
        for c in contracts:
            c.conId = int(getattr(c, "strike", 1) or 1)
        return list(contracts)

    def reqSecDefOptParams(self, symbol, fut_fop_exchange, sec_type, con_id):
        self.chain_requests.append((symbol, fut_fop_exchange, sec_type, con_id))
        return list(self._chains)


def adapter(ib=None, api=FAKE_API, **overrides):
    config = build_config({"universe": ["AAPL"], "ibkr": {"account": ACCOUNT}, **overrides})
    return IBKRMarketData(
        ibkr_config=config.ibkr,
        strategy_config=config.strategy,
        clock=FixedClock(SCAN_TIME),
        ib=ib,
        api=api,
    )


def chain_definition(strikes, expiries, trading_class="AAPL"):
    return SimpleNamespace(
        exchange=EXCHANGE,
        tradingClass=trading_class,
        strikes=list(strikes),
        expirations=[d.strftime("%Y%m%d") for d in expiries],
    )


# --- ARCH-C3 -------------------------------------------------------------


def test_the_underlying_is_qualified_through_the_injected_api():
    """``_qualified_underlying`` builds a Stock; that is the vendor call."""
    ib = FakeIB()
    contract = adapter(ib)._qualified_underlying(ib, "AAPL")

    assert contract.kind == "Stock"
    assert (contract.symbol, contract.exchange, contract.currency) == (
        "AAPL",
        EXCHANGE,
        CURRENCY,
    )
    assert contract.conId, "an unqualified underlying yields no chain at all"


def test_an_unresolvable_underlying_still_fails_as_a_market_data_error():
    """The seam must not change what happens when IBKR resolves nothing."""
    ib = FakeIB(qualify=lambda _contracts: [])

    with pytest.raises(MarketDataError, match="did not resolve an underlying"):
        adapter(ib)._qualified_underlying(ib, "AAPL")


def test_the_candidate_chain_is_built_through_the_injected_api():
    """``_chain_contracts`` is the other vendor call, and the bigger one.

    It is where the line budget is actually made achievable: the full chain is
    narrowed to a DTE band, to puts, and to a strike window before a single
    quote is requested. None of that was reachable from a test.
    """
    as_of = SCAN_TIME
    expiries = [as_of.date() + timedelta(days=d) for d in (30, 45)]
    ib = FakeIB(chains=[chain_definition([180, 190, 195, 200], expiries)])
    underlying = SimpleNamespace(symbol="AAPL", conId=1234)

    candidates, trading_class = adapter(ib)._chain_contracts(
        ib, "AAPL", underlying, Decimal("195.00"), as_of
    )

    assert ib.chain_requests == [("AAPL", "", "STK", 1234)]
    assert candidates, "the narrowed chain collapsed to nothing"
    assert all(c.kind == "Option" for c in candidates)
    assert {c.right for c in candidates} == {Right.PUT.value}
    assert {c.exchange for c in candidates} == {EXCHANGE}
    assert {c.currency for c in candidates} == {CURRENCY}
    assert {c.tradingClass for c in candidates} == {"AAPL"}
    assert trading_class == "AAPL", "the selected class must reach the caller"
    assert {c.expiry for c in candidates} == {d.strftime("%Y%m%d") for d in expiries}
    # Two expiries x the strikes inside the window around 195.
    assert len(candidates) == 2 * len({c.strike for c in candidates})


def test_the_vendor_module_is_never_loaded_when_an_api_is_injected(monkeypatch):
    """The point of the seam: no import attempt at all, injected or not.

    Asserted by making the loader itself fatal. Note this cannot be phrased as
    "an exploding double propagates its error" -- ``_qualified_underlying``
    wraps anything the constructor raises in ``MarketDataError``, so a failure
    inside the seam is indistinguishable from a failure at the venue. Only the
    loader's own absence from the call graph proves the claim.
    """
    calls: list[str] = []

    def never(*_args, **_kwargs):
        calls.append("loaded")
        raise AssertionError("the vendor module was loaded despite an injected api")

    monkeypatch.setattr("ibkr_trader.scanner._require_ib_async", never)

    ib = FakeIB()
    contract = adapter(ib)._qualified_underlying(ib, "AAPL")

    assert contract.kind == "Stock"
    assert calls == []


def test_without_an_injected_api_the_missing_dependency_still_reports_itself():
    """Default behaviour is unchanged: the loader runs and explains itself.

    ``ib_async`` is genuinely absent in this environment, so this exercises the
    real fallback rather than a simulated one.
    """
    pytest.importorskip  # noqa: B018 - documents the assumption below
    try:
        import ib_async  # noqa: F401
    except ImportError:
        pass
    else:  # pragma: no cover - only on a machine with the vendor installed
        pytest.skip("ib_async is installed; the absent-dependency path is unreachable")

    ib = FakeIB()

    with pytest.raises(MarketDataError, match="ib_async is not installed"):
        adapter(ib, api=None)._qualified_underlying(ib, "AAPL")
