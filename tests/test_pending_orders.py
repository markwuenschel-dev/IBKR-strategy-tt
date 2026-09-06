"""A working order must not be re-proposed on the next pass.

IBKR's position stream reports only *filled* holdings. A limit order that has
not filled yet is therefore invisible to a naive concentration check, and a
repeat scan will propose and submit the same trade again every interval until it
fills. At the default 300s interval, an order resting for half an hour becomes
six duplicate orders.

Two layers are covered here: the scanner reporting working orders as pending
exposure, and the algorithm honouring that report across consecutive passes.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from ibkr_trader.clock import FixedClock
from ibkr_trader.config import build_config
from ibkr_trader.models import Outcome, Portfolio, Position
from ibkr_trader.scanner import IBKRMarketData

from .fakes import ACCOUNT, SCAN_TIME, FakeBroker, StubMarketData, tradable_snapshot
from .harness import build_runner

# --- layer 1: the scanner reports working orders ---------------------------


def account_value(tag: str, value: str):
    return SimpleNamespace(tag=tag, value=value, currency="USD", account=ACCOUNT)


def trade(symbol: str, qty: float, status: str, active: bool = True, account: str = ""):
    return SimpleNamespace(
        contract=SimpleNamespace(symbol=symbol),
        order=SimpleNamespace(totalQuantity=qty, account=account),
        orderStatus=SimpleNamespace(status=status),
        isActive=lambda: active,
    )


class AccountStubIB:
    """Minimal IB client exposing the three streams ``portfolio()`` reads."""

    def __init__(self, trades=(), positions=()):
        self._trades = list(trades)
        self._positions = list(positions)

    def accountValues(self, account=""):
        return [
            account_value("NetLiquidation", "50000"),
            account_value("BuyingPower", "25000"),
        ]

    def positions(self, account=""):
        return self._positions

    def openTrades(self):
        return self._trades


def market_data(ib):
    config = build_config({"universe": ["AAPL"], "ibkr": {"account": ACCOUNT}})
    return IBKRMarketData(config.ibkr, config.strategy, FixedClock(SCAN_TIME), ib=ib)


def test_working_order_is_reported_as_pending_exposure():
    """The fix: a live order shows up in the portfolio, flagged pending."""
    md = market_data(AccountStubIB(trades=[trade("SPY", 1, "PreSubmitted")]))

    portfolio = md.portfolio()

    assert portfolio.has_position("SPY") is True
    (position,) = portfolio.positions_for("SPY")
    assert position.pending is True
    assert position.quantity == 1
    assert "PreSubmitted" in position.description


def test_finished_orders_are_not_reported_as_exposure():
    """A filled or cancelled order is history, not a live commitment."""
    md = market_data(
        AccountStubIB(
            trades=[
                trade("SPY", 1, "Filled", active=False),
                trade("QQQ", 1, "Cancelled", active=False),
            ]
        )
    )
    portfolio = md.portfolio()
    assert portfolio.has_position("SPY") is False
    assert portfolio.has_position("QQQ") is False


def test_pending_and_filled_exposure_are_both_counted():
    filled = SimpleNamespace(
        contract=SimpleNamespace(symbol="AAPL", localSymbol="AAPL 185P"), position=-3.0
    )
    md = market_data(AccountStubIB(trades=[trade("SPY", 2, "Submitted")], positions=[filled]))

    portfolio = md.portfolio()

    assert portfolio.open_symbol_count == 2
    assert portfolio.has_position("AAPL") is True
    assert portfolio.has_position("SPY") is True


class BrokenOrders(AccountStubIB):
    def openTrades(self):
        raise RuntimeError("order stream unavailable")


def test_order_stream_failure_does_not_abort_the_scan():
    """Sizing data already read successfully; a broken order stream is degraded,
    not fatal."""
    portfolio = market_data(BrokenOrders()).portfolio()
    assert portfolio.net_liquidation == Decimal(50_000)
    assert portfolio.positions == ()


def test_order_stream_failure_is_reported_rather_than_defaulted():
    """Degraded is not the same as empty, and the caller has to be able to tell.

    This is the assertion that was missing: `positions == ()` above is equally
    true of a healthy account with no working orders, so on its own it cannot
    distinguish "nothing outstanding" from "I could not find out". Both
    concentration guards key on these rows existing, so the difference decides
    whether the algorithm may rule on a trade by itself.
    """
    healthy = market_data(AccountStubIB()).portfolio()
    degraded = market_data(BrokenOrders()).portfolio()

    assert healthy.positions == degraded.positions == ()
    assert healthy.pending_orders_known is True
    assert degraded.pending_orders_known is False


# --- layer 2: the loop does not stack duplicate orders ---------------------


class AccountAwareMarketData(StubMarketData):
    """Market data whose portfolio reflects orders already sent to the broker.

    This is what the real scanner now does: an order still working at IBKR is
    reported as pending exposure. Without it the runner has no way to know it
    already has an order out.
    """

    def __init__(self, snapshots, broker):
        super().__init__(snapshots)
        self._broker = broker

    def portfolio(self) -> Portfolio:
        pending = tuple(
            Position(
                symbol=p.symbol,
                quantity=p.quantity,
                description="working order (Submitted)",
                pending=True,
            )
            for p in self._broker.submitted
        )
        return Portfolio(
            net_liquidation=Decimal(50_000),
            buying_power=Decimal(25_000),
            positions=pending,
        )


def test_resting_order_is_not_submitted_twice_on_the_next_pass(tmp_path):
    """The regression: two passes, one unfilled order, exactly one submission."""
    broker = FakeBroker(outcome=Outcome.WORKING)
    market = AccountAwareMarketData({"AAPL": tradable_snapshot("AAPL")}, broker)
    clock = FixedClock(SCAN_TIME)
    runner, _, reviewer, _, store = build_runner(
        tmp_path, market=market, broker=broker, clock=clock
    )

    summaries = runner.run_while(lambda: True, max_passes=2)

    assert len(summaries) == 2
    assert summaries[0].results[0].outcome is Outcome.WORKING
    assert summaries[1].results[0].outcome is Outcome.NO_TRADE
    assert "working order" in summaries[1].results[0].detail

    assert broker.call_count == 1, "the resting order must not be duplicated"
    assert reviewer.call_count == 1, "no second proposal means no second review"
    assert len(store.orders()) == 1
    assert len(store.attempts()) == 2


def test_duplicate_is_allowed_when_explicitly_configured(tmp_path):
    """The guard is a policy, not a hard block; opting out still works."""
    broker = FakeBroker(outcome=Outcome.WORKING)
    market = AccountAwareMarketData({"AAPL": tradable_snapshot("AAPL")}, broker)
    runner, _, _, _, _ = build_runner(
        tmp_path,
        market=market,
        broker=broker,
        clock=FixedClock(SCAN_TIME),
        overrides={"risk": {"allow_duplicate_symbol": True}},
    )

    runner.run_while(lambda: True, max_passes=2)

    assert broker.call_count == 2
