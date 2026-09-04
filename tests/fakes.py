"""Test doubles for the four external boundaries, and chain-building helpers.

These stand in for market data, the reviewer, and the broker. Everything inside
the application boundary — the algorithm, the runner, the store — is real in
every test that uses these.

Each double records what it was asked to do, so tests can assert on *absence*
("the broker was never called") as directly as on presence.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

from ibkr_trader.errors import MarketDataError
from ibkr_trader.models import (
    ExecutionResult,
    Fill,
    MarketSnapshot,
    OptionQuote,
    Outcome,
    Portfolio,
    Position,
    ReviewDecision,
    Right,
    TradeProposal,
)


def quote(
    symbol: str,
    expiry: date,
    strike: str,
    right: Right,
    bid: str,
    ask: str,
    delta: float,
    open_interest: int = 500,
    volume: int = 100,
) -> OptionQuote:
    """Build one option quote from readable decimal strings."""
    return OptionQuote(
        symbol=symbol,
        expiry=expiry,
        strike=Decimal(strike),
        right=right,
        bid=Decimal(bid),
        ask=Decimal(ask),
        delta=delta,
        open_interest=open_interest,
        volume=volume,
    )


class StubMarketData:
    """Serves pre-built snapshots; raises for symbols configured to fail.

    Symbol lookup is explicit: an unconfigured symbol raises rather than
    returning empty data, so a test cannot pass by accident.

    ``working_orders`` exists because :meth:`ibkr_trader.ports.MarketData.portfolio`
    obliges every implementation to report orders still working at the broker as
    ``pending`` positions. This double is the suite-wide default, so if it could
    not express that obligation, the default substitute would be one that
    silently disables the duplicate-order guard -- which is exactly what it was
    before, and why ``test_pending_orders.py`` had to hand-replicate the real
    adapter's synthesis in a local subclass to test the guard at all.
    """

    def __init__(
        self,
        snapshots: dict[str, MarketSnapshot] | None = None,
        failures: dict[str, Exception] | None = None,
        portfolio: Portfolio | None = None,
        working_orders: dict[str, int] | None = None,
    ) -> None:
        self._snapshots = snapshots or {}
        self._failures = failures or {}
        self._portfolio = portfolio or Portfolio(
            net_liquidation=Decimal(50_000), buying_power=Decimal(25_000)
        )
        self._working_orders = dict(working_orders or {})
        self.requested: list[str] = []

    def snapshot(self, symbol: str) -> MarketSnapshot:
        self.requested.append(symbol)
        if symbol in self._failures:
            raise self._failures[symbol]
        if symbol not in self._snapshots:
            raise MarketDataError(f"no market data configured for {symbol}")
        return self._snapshots[symbol]

    def portfolio(self) -> Portfolio:
        """Filled holdings plus any still-working orders, as the port requires."""
        if not self._working_orders:
            return self._portfolio
        pending = tuple(
            Position(
                symbol=symbol,
                quantity=quantity,
                description="working order (Submitted)",
                pending=True,
            )
            for symbol, quantity in self._working_orders.items()
        )
        return replace(self._portfolio, positions=self._portfolio.positions + pending)


class StubReviewer:
    """Returns a fixed decision, or raises a configured error.

    Records every proposal it was shown, which is how tests assert the reviewer
    is consulted exactly once per proposal and never before one exists.
    """

    def __init__(
        self,
        approved: bool = True,
        reason: str = "meets stated criteria",
        error: Exception | None = None,
        reviewer_id: str = "stub-reviewer",
    ) -> None:
        self._approved = approved
        self._reason = reason
        self._error = error
        self._reviewer_id = reviewer_id
        self.reviewed: list[TradeProposal] = []

    @property
    def call_count(self) -> int:
        return len(self.reviewed)

    def review(self, proposal: TradeProposal, portfolio: Portfolio) -> ReviewDecision:
        self.reviewed.append(proposal)
        if self._error is not None:
            raise self._error
        return ReviewDecision(
            approved=self._approved,
            reason=self._reason,
            reviewer_id=self._reviewer_id,
            reviewed_at=datetime(2026, 1, 15, 14, 31, tzinfo=UTC),
        )


class FakeBroker:
    """A broker with the same observable semantics as the real adapter.

    Fills at the proposal's limit price by default. Configurable to reject, to
    fail before transmission, or to raise an ambiguity.
    """

    def __init__(
        self,
        outcome: Outcome = Outcome.FILLED,
        message: str = "",
        error: Exception | None = None,
        connected: bool = True,
    ) -> None:
        self._outcome = outcome
        self._message = message
        self._error = error
        self._connected = connected
        self.submitted: list[TradeProposal] = []
        self.connect_calls = 0
        self.disconnect_calls = 0

    @property
    def call_count(self) -> int:
        return len(self.submitted)

    @property
    def is_connected(self) -> bool:
        return self._connected

    def connect(self) -> None:
        """Nothing to establish, but the port declares it and cli.py calls it."""
        self._connected = True
        self.connect_calls += 1

    def disconnect(self) -> None:
        """Idempotent, and counted -- teardown asserts it happened exactly once."""
        self._connected = False
        self.disconnect_calls += 1

    def submit(self, proposal: TradeProposal) -> ExecutionResult:
        self.submitted.append(proposal)
        if self._error is not None:
            raise self._error

        fills: tuple[Fill, ...] = ()
        if self._outcome is Outcome.FILLED:
            fills = (
                Fill(
                    quantity=proposal.quantity,
                    price=proposal.limit_price,
                    filled_at=datetime(2026, 1, 15, 14, 32, tzinfo=UTC),
                ),
            )
        return ExecutionResult(
            outcome=self._outcome,
            order_ref=proposal.proposal_id,
            broker_order_id=f"fake-{len(self.submitted)}",
            message=self._message,
            fills=fills,
        )


# --- Chain fixtures -------------------------------------------------------
#
# One canonical tradable snapshot, built so exactly one spread qualifies. The
# numbers are chosen to make the expected order arithmetic checkable by hand:
#
#   short 185 put  mid 3.40   (bid 3.35 / ask 3.45,  delta -0.30)
#   long  180 put  mid 1.65   (bid 1.60 / ask 1.70,  delta -0.20)
#   net credit     1.75  =  0.35 x 5-wide, clearing the 1/3 minimum
#   max loss/ct    (5.00 - 1.75) x 100 = 325
#   sizing         2% of 50,000 = 1,000 budget -> floor(1000/325) = 3 contracts

SCAN_TIME = datetime(2026, 1, 15, 14, 30, tzinfo=UTC)
NEAR_EXPIRY = date(2026, 1, 29)  # 14 DTE - inside no band, must be rejected
GOOD_EXPIRY = date(2026, 3, 1)  # 45 DTE - the target
FAR_EXPIRY = date(2026, 4, 15)  # 90 DTE - too far, must be rejected

EXPECTED_SHORT_STRIKE = Decimal(185)
EXPECTED_LONG_STRIKE = Decimal(180)
EXPECTED_CREDIT = Decimal("1.75")
EXPECTED_QUANTITY = 3


def _put_ladder(symbol: str, expiry: date) -> list[OptionQuote]:
    """A put ladder whose 185/180 pair is the only qualifying vertical.

    Deltas fall as strikes fall, mimicking a real chain. Only the 185 strike sits
    in the 0.20-0.40 short-delta band with a partner 5 points below it that also
    passes the liquidity screen.
    """
    ladder = [
        # strike, bid,    ask,    delta
        ("205", "9.80", "10.00", -0.62),
        ("200", "7.30", "7.50", -0.52),
        ("195", "5.40", "5.55", -0.43),
        ("190", "4.30", "4.45", -0.36),
        ("185", "3.35", "3.45", -0.30),
        ("180", "1.60", "1.70", -0.20),
        ("175", "0.90", "1.00", -0.13),
        ("170", "0.45", "0.55", -0.08),
    ]
    return [
        quote(symbol, expiry, strike, Right.PUT, bid, ask, delta)
        for strike, bid, ask, delta in ladder
    ]


def tradable_snapshot(symbol: str = "AAPL", iv_rank: float = 45.0) -> MarketSnapshot:
    """A snapshot containing exactly one qualifying put credit spread.

    Includes a too-near and a too-far expiry so that a test passing this fixture
    proves the DTE band is actually applied, not merely that some spread exists.
    """
    chain: list[OptionQuote] = []
    for expiry in (NEAR_EXPIRY, GOOD_EXPIRY, FAR_EXPIRY):
        chain.extend(_put_ladder(symbol, expiry))
    return MarketSnapshot(
        symbol=symbol,
        underlying_price=Decimal("195.00"),
        iv_rank=iv_rank,
        implied_volatility=0.32,
        as_of=SCAN_TIME,
        chain=tuple(chain),
    )


def illiquid_snapshot(symbol: str = "XYZ") -> MarketSnapshot:
    """A snapshot where the right strikes exist but the market is unusably wide.

    Verifies that "no trade" comes from the liquidity screen rather than from an
    absent chain.
    """
    wide = [
        quote(symbol, GOOD_EXPIRY, "185", Right.PUT, "2.00", "4.80", -0.30),
        quote(symbol, GOOD_EXPIRY, "180", Right.PUT, "0.40", "2.90", -0.20),
    ]
    return MarketSnapshot(
        symbol=symbol,
        underlying_price=Decimal("195.00"),
        iv_rank=55.0,
        implied_volatility=0.40,
        as_of=SCAN_TIME,
        chain=tuple(wide),
    )
