"""Live IBKR market data: the adapter behind the :class:`~ibkr_trader.ports.MarketData` port.

This module turns a TWS/Gateway connection into the one immutable
:class:`~ibkr_trader.models.MarketSnapshot` the pure algorithm evaluates. It is
deliberately the *only* place in the system that knows what a ``Ticker``, an
``OptionChain`` or a NaN price is; everything downstream sees exact ``Decimal``
prices and finished quotes.

Four properties are load-bearing:

*No quote survives the call.* There is no background subscription, no
reconnection daemon and no module-level state. Every market-data line this
adapter opens is closed before :meth:`IBKRMarketData.snapshot` -- or
:meth:`IBKRMarketData.quote`, the management path's per-leg equivalent --
returns, so a pass leaves the connection exactly as it found it. A stale quote that outlives
the pass that fetched it is worse than no quote at all — it would price a real
order off a market that no longer exists. The one thing that does survive is
the *IV rank*, a derived number rather than a quote, held per process and per
market date (see :meth:`IBKRMarketData._iv_rank`): the one-year history it is
computed from cannot change within a trading day, and re-requesting it for
every symbol on every pass is what would trip IBKR's historical-data pacing.

*The line budget is enforced here or nowhere.* An IBKR account holds a finite
number of simultaneous market-data lines (``ibkr.refresh_limit``, ceilinged by
:data:`~ibkr_trader.config.MAX_REFRESH_LIMIT`). Exceeding it does not fail
cleanly at startup — it fails mid-scan, per contract, with data silently
missing. So the chain is narrowed *before* any quote is requested and the
survivors are quoted in batches of at most ``refresh_limit``, each batch
cancelled before the next opens. See :meth:`IBKRMarketData._quote_batches`.

*The connection is injected, never created.* ``ib`` is constructed and connected
by the caller so the broker adapter and this adapter share one client, one
client id, and one lifecycle. Connecting here would give the process two
half-owned sockets and no single place to close them.

*The vendor module is injected too, not only the client.* Reading the account,
position and order streams needs the connection alone, but qualifying an
underlying and building a chain need ``ib_async`` itself, to **construct**
contracts. Those constructors arrive through ``api``, defaulting to the real
import, exactly as :class:`~ibkr_trader.broker.IBKRBroker` takes its own
``api``. Without that seam the two contract-building methods — and therefore
:meth:`IBKRMarketData.snapshot`, which calls both — are unreachable on a
machine where the package is not installed.

``ib_async`` is imported lazily, at call time. Importing this module must not
require the package: the configuration, algorithm and mission tests all import
the package tree without ever touching a broker.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Iterator, Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from .clock import Clock, market_date
from .config import IBKRConfig, StrategyConfig
from .errors import MarketDataError
from .models import (
    CONTRACT_MULTIPLIER,
    MarketSnapshot,
    OptionLeg,
    OptionPosition,
    OptionQuote,
    Portfolio,
    Position,
    Right,
)

logger = logging.getLogger(__name__)

#: Routing defaults. US equity options are quoted through SMART unless an
#: account explicitly routes elsewhere, which V4 does not.
EXCHANGE = "SMART"
CURRENCY = "USD"

#: Generic tick list requested for every option contract.
#:
#: 100 = option volume, 101 = option open interest, 106 = implied volatility.
#: Without 100/101 the ``volume``/``openInterest`` ticker fields stay NaN and
#: the liquidity screen in :mod:`~ibkr_trader.tastytrade` has nothing to screen
#: on, so these are requested explicitly rather than relying on defaults.
OPTION_GENERIC_TICKS = "100,101,106"

#: Order statuses meaning the order is no longer live at the broker.
INACTIVE_ORDER_STATUSES = frozenset(
    {"Filled", "Cancelled", "ApiCancelled", "Inactive", "PendingCancel"}
)

#: 104 = historical volatility, 106 = implied volatility, on the underlying.
UNDERLYING_GENERIC_TICKS = "104,106"

#: How long one batch of quotes is given to arrive, and the polling step.
#:
#: Bounded on both sides: a deadline measured on the injected clock *and* a hard
#: iteration cap. The cap is what guarantees termination — a frozen or mocked
#: clock would make a deadline-only loop spin forever, and this adapter must
#: never be the thing that hangs a pass.
QUOTE_WAIT_SECONDS = 6.0
QUOTE_POLL_SECONDS = 0.25

#: The generic tick carrying option open interest (27 call / 28 put).
#:
#: Named separately from :data:`OPTION_GENERIC_TICKS` because the completeness
#: rule is keyed on it: a request that did not pay for this tick has no option
#: fields to wait for. Keep the two in step.
OPEN_INTEREST_TICK = "101"

#: Extra pumping allowed once the whole batch has a two-sided book.
#:
#: Model greeks and open interest arrive *after* top of book, and the batch's
#: subscriptions are cancelled the moment the wait returns
#: (:meth:`IBKRMarketData._quote_batches`), so a wait satisfied by bid/ask alone
#: guarantees delta and open interest are never seen. This is the window spent
#: waiting for them.
#:
#: **Nested inside :data:`QUOTE_WAIT_SECONDS`, never added to it**: a batch can
#: still never take longer than it does today. A strike that simply never
#: reports open interest therefore costs this window, not the whole budget.
GREEKS_WAIT_SECONDS = 2.0

#: Strike window above spot, as a fraction of the underlying price.
#:
#: The strategy sells puts at 0.20-0.40 delta and buys a further strike below,
#: so strikes far from spot can never be selected. Quoting them would consume
#: the line budget to produce rows the algorithm discards. Asymmetric because
#: the chain is puts-only: the useful strikes sit below spot. The window
#: *below* spot is not a constant: see :meth:`IBKRMarketData._strike_window`.
#:
#: Zero, because a put at or above spot is in the money with a delta past 0.50
#: and can never reach the 0.20-0.40 short band -- so every line spent above
#: spot is spent on a row the algorithm always discards, which is exactly the
#: waste this window exists to prevent. Spot itself stays inside the window so
#: an at-the-money listed strike is still quoted.
STRIKE_WINDOW_ABOVE = 0.0

#: Denominator that turns a calendar DTE into the fraction of a year an
#: annualized volatility is scaled by: ``iv * sqrt(dte / DAYS_PER_YEAR)``.
DAYS_PER_YEAR = 365.0

#: Lookback used for IV rank, and the realized-volatility window of the proxy.
IV_HISTORY_DURATION = "1 Y"
IV_HISTORY_BAR_SIZE = "1 day"
HV_WINDOW_DAYS = 30
TRADING_DAYS_PER_YEAR = 252

#: Fewest daily observations that make a percentile meaningful. Below this the
#: high/low of the series is an artifact of the sample, not a real range.
MIN_HISTORY_BARS = 60

#: Account tags read for sizing. ``BuyingPower`` is preferred; ``AvailableFunds``
#: is the cash-account equivalent for accounts that do not report the former.
NET_LIQUIDATION_TAG = "NetLiquidation"
BUYING_POWER_TAGS = ("BuyingPower", "AvailableFunds")

#: Currencies an account value may be denominated in. ``BASE`` is IBKR's summary
#: row for a multi-currency account and is the one sizing should use.
ACCOUNT_CURRENCIES = ("BASE", CURRENCY)


class MarketDataApi(Protocol):
    """The ``ib_async`` module surface used to build market-data contracts.

    Deliberately separate from :class:`~ibkr_trader.broker.IBApi`, which names
    the constructors the *order* path needs. The two adapters ask the same
    vendor module for different things, and sharing one declaration here would
    make each adapter depend on the other's requirements — the coupling a
    single venue-translation owner would have to resolve deliberately, not by
    accident.

    Only the constructors belong here. The connected client arrives separately
    as ``ib``, because reading account, position and order state needs the
    connection but not the module.
    """

    Stock: Any
    Option: Any


def _require_ib_async() -> MarketDataApi:
    """Import ``ib_async`` at call time, or fail as a market-data error.

    Deferred so ``import ibkr_trader.scanner`` costs nothing and works in an
    environment that never talks to a broker.
    """
    try:
        import ib_async
    except ImportError as exc:  # pragma: no cover - depends on the environment
        logger.error("ib_async is not installed; live market data is unavailable")
        raise MarketDataError(
            "ib_async is not installed; install it to use live IBKR market data"
        ) from exc
    return ib_async


def _finite(value: Any) -> float | None:
    """Return ``value`` as a float, or None when it is not a real number.

    IBKR routinely reports NaN for a field it simply has not sent yet — an
    unopened market, an unsubscribed tick, a strike with no quote. NaN compares
    false against every bound, so an unguarded NaN slips silently through the
    liquidity screens instead of being rejected. Every number read off a ticker
    goes through here.
    """
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _price(value: float) -> Decimal:
    """Convert a broker-reported price to an exact ``Decimal``.

    Via ``str`` deliberately: ``Decimal(1.15)`` is the binary expansion of 1.15
    and would make stored records and round-trip comparisons untrustworthy,
    which is the reason :mod:`~ibkr_trader.models` is Decimal throughout.
    """
    return Decimal(str(value))


def _count(value: Any) -> int:
    """Read a size/open-interest tick as a non-negative integer.

    Missing becomes 0 rather than being dropped or optimistically filled. 0 is
    the conservative reading: it fails ``min_open_interest`` and produces a
    no-trade, whereas a guessed value would let an illiquid strike through.
    """
    number = _finite(value)
    if number is None or number < 0:
        return 0
    return int(number)


def _parse_expiry(raw: str) -> date | None:
    """Parse an IBKR ``YYYYMMDD`` expiration string, or None if malformed."""
    try:
        return datetime.strptime(raw, "%Y%m%d").date()
    except (TypeError, ValueError):
        return None


def _chunks(items: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    """Split ``items`` into consecutive slices of at most ``size``."""
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _realized_volatility_series(closes: Sequence[float], window: int) -> list[float]:
    """Rolling annualized realized volatility of a daily close series.

    Returns one observation per complete ``window``, so the result is directly
    comparable to a series of implied-volatility readings.
    """
    log_returns: list[float] = []
    for previous, current in zip(closes, closes[1:], strict=False):
        if previous <= 0 or current <= 0:
            continue
        log_returns.append(math.log(current / previous))

    if window < 2 or len(log_returns) < window:
        return []

    series: list[float] = []
    for end in range(window, len(log_returns) + 1):
        sample = log_returns[end - window : end]
        mean = sum(sample) / window
        variance = sum((value - mean) ** 2 for value in sample) / (window - 1)
        series.append(math.sqrt(variance) * math.sqrt(TRADING_DAYS_PER_YEAR))
    return series


def _percentile_rank(current: float, series: Sequence[float]) -> float | None:
    """Position of ``current`` in the high/low range of ``series``, 0-100.

    This is the classic IV-rank definition (range position), not IV percentile
    (fraction of days below). Returns None when the series is too short or flat
    to carry information, so the caller can escalate rather than report a
    meaningless 0 or 50.
    """
    if len(series) < MIN_HISTORY_BARS:
        return None
    low = min(series)
    high = max(series)
    if high - low <= 0:
        return None
    return max(0.0, min(100.0, 100.0 * (current - low) / (high - low)))


def _whole_contracts(quantity: float) -> int:
    """Round a reported size away from zero.

    A fractional holding is still a holding. ``int()`` truncates 0.5 to 0, which
    drops the position from the portfolio entirely and hides it from the
    duplicate-symbol guard and the concentration limit -- and "is there exposure
    in this symbol" is the only question this number is read to answer. Whole
    sizes are unchanged.
    """
    if quantity == 0:
        return 0
    magnitude = math.ceil(abs(quantity))
    return magnitude if quantity > 0 else -magnitude


class IBKRMarketData:
    """Live market data and account state from a connected TWS/Gateway client.

    Satisfies :class:`~ibkr_trader.ports.MarketData`. One call to
    :meth:`snapshot` is one bounded, self-contained round trip: qualify the
    underlying, narrow the chain, quote it within the line budget, release every
    line, return an immutable snapshot.

    Args:
        ibkr_config: Connection settings; only ``refresh_limit`` and ``account``
            are consulted here, since the connection itself is not ours to make.
        strategy_config: Selection criteria. ``min_dte``/``max_dte`` bound the
            chain *before* quoting, which is what keeps the request count sane.
        clock: The only source of time. Stamps ``as_of`` and bounds the wait for
            quotes; nothing in this module calls ``datetime.now``.
        ib: A connected ``ib_async.IB``. Injected so the broker adapter and this
            adapter share one connection, and so tests can substitute a double.
        api: The ``ib_async`` module surface used to construct contracts.
            Defaults to importing it at first use. Injecting it is what keeps
            the contract-building path reachable without the package installed.
    """

    def __init__(
        self,
        ibkr_config: IBKRConfig,
        strategy_config: StrategyConfig,
        clock: Clock,
        ib: Any | None = None,
        api: MarketDataApi | None = None,
    ) -> None:
        self._ibkr_config = ibkr_config
        self._strategy_config = strategy_config
        self._clock = clock
        self._ib = ib
        self._api = api
        #: Successful IV-rank results, keyed by ``(symbol, market date)``. See
        #: :meth:`_iv_rank` for why this exists and what it deliberately omits.
        self._iv_rank_cache: dict[tuple[str, date], tuple[float, float]] = {}

    # ------------------------------------------------------------------
    # MarketData protocol
    # ------------------------------------------------------------------

    def snapshot(self, symbol: str) -> MarketSnapshot:
        """Return the current market for ``symbol``.

        Fetches the underlying price, narrows the option chain to the
        configured DTE band and a strike window around spot, quotes the
        survivors in batches that respect ``refresh_limit``, and computes an IV
        rank (see :meth:`_iv_rank`).

        Args:
            symbol: The underlying, upper case.

        Returns:
            An immutable :class:`~ibkr_trader.models.MarketSnapshot` whose chain
            contains put quotes only — the sole strategy is a put vertical, and
            :meth:`~ibkr_trader.models.MarketSnapshot.puts_for` is the only
            accessor it uses, so calls are never requested rather than requested
            and discarded.

        Raises:
            MarketDataError: the underlying price, the chain, the volatility
                history, or every quote in the chain was unavailable or
                unusable. Every failure mode is one error type, because the
                runner's response to all of them is identical: record a data
                error for this symbol and move to the next.
        """
        ib = self._client()
        as_of = self._clock.now()
        self._apply_market_data_type(ib)

        underlying = self._qualified_underlying(ib, symbol)
        underlying_ticker = self._quote_underlying(ib, underlying)
        underlying_price = self._underlying_price(symbol, underlying_ticker)
        # Generic tick 106 on the underlying (UNDERLYING_GENERIC_TICKS) lands
        # in ``Ticker.impliedVolatility``. It sizes the strike window only; the
        # rank below is measured against history, not this reading.
        underlying_iv = _finite(getattr(underlying_ticker, "impliedVolatility", None))

        contracts, trading_class = self._chain_contracts(
            ib, symbol, underlying, underlying_price, as_of, underlying_iv
        )
        chain = self._quote_chain(ib, symbol, contracts)
        if not chain:
            raise MarketDataError(
                f"{symbol}: no usable option quotes in {len(contracts)} contracts "
                f"(all missing a bid/ask)"
            )

        implied_volatility, iv_rank = self._iv_rank(ib, symbol, underlying, as_of)

        logger.info(
            "%s snapshot: price=%s quotes=%d expiries=%d iv=%.4f iv_rank=%.1f",
            symbol,
            underlying_price,
            len(chain),
            len({quote.expiry for quote in chain}),
            implied_volatility,
            iv_rank,
        )
        return MarketSnapshot(
            symbol=symbol,
            underlying_price=underlying_price,
            iv_rank=iv_rank,
            as_of=as_of,
            chain=chain,
            trading_class=trading_class,
        )

    def portfolio(self) -> Portfolio:
        """Return account state used for sizing and concentration limits.

        Reads ``NetLiquidation`` and ``BuyingPower`` (falling back to
        ``AvailableFunds``) from the account-value stream the client maintains,
        and open positions from the position stream. Both are already-subscribed
        client state, so this costs no market-data line.

        The underlying-symbol keying and the working-order synthesis below are
        both obligations of :meth:`~ibkr_trader.ports.MarketData.portfolio`, not
        choices this adapter makes. They are restated here because this is where
        they are *implemented*; the port is where they are *required*. They used
        to be stated only here, which meant any other conforming implementation
        -- including the suite's own default double -- could omit them and
        silently disable the duplicate-order guard.

        Positions are keyed by *underlying* symbol, not by option local symbol:
        the concentration limits the algorithm applies are per underlying, so a
        short put on SPY must count as a SPY position.

        Orders still working at the broker are reported too, flagged
        ``pending``. IBKR's position stream lists only *filled* holdings, so
        without this a limit order that has not yet filled is invisible to the
        concentration check and the next pass proposes the same trade again.

        Raises:
            MarketDataError: account values are missing or unparsable. Sizing
                against a guessed net liquidation value is the one failure here
                that could produce a real, wrongly-sized order.
        """
        ib = self._client()
        account = self._ibkr_config.account

        try:
            values = list(ib.accountValues(account))
        except Exception as exc:
            logger.exception("Failed to read account values for account %r", account)
            raise MarketDataError(f"Cannot read IBKR account values: {exc}") from exc

        self._require_one_account(values, account, "account value")

        net_liquidation = self._account_amount(values, (NET_LIQUIDATION_TAG,))
        if net_liquidation is None:
            raise MarketDataError(
                f"IBKR reported no {NET_LIQUIDATION_TAG} for account "
                f"{account or '<default>'}; cannot size a trade"
            )
        buying_power = self._account_amount(values, BUYING_POWER_TAGS)
        if buying_power is None:
            raise MarketDataError(
                f"IBKR reported none of {', '.join(BUYING_POWER_TAGS)} for account "
                f"{account or '<default>'}; cannot size a trade"
            )

        try:
            raw_positions = list(ib.positions(account))
        except Exception as exc:
            logger.exception("Failed to read positions for account %r", account)
            raise MarketDataError(f"Cannot read IBKR positions: {exc}") from exc

        self._require_one_account(raw_positions, account, "position")

        positions: list[Position] = []
        for raw in raw_positions:
            contract = getattr(raw, "contract", None)
            symbol = getattr(contract, "symbol", "") or ""
            reported = _finite(getattr(raw, "position", None))
            quantity = _whole_contracts(reported) if reported is not None else 0
            if not symbol or quantity == 0:
                continue
            positions.append(
                Position(
                    symbol=symbol,
                    quantity=quantity,
                    description=getattr(contract, "localSymbol", "") or symbol,
                )
            )

        pending, pending_known = self._pending_positions(ib, account)
        positions.extend(pending)

        logger.debug(
            "Portfolio: net_liquidation=%s buying_power=%s positions=%d "
            "(%d pending, known=%s)",
            net_liquidation,
            buying_power,
            len(positions),
            len(pending),
            pending_known,
        )
        return Portfolio(
            net_liquidation=net_liquidation,
            buying_power=buying_power,
            positions=tuple(positions),
            pending_orders_known=pending_known,
        )

    def option_positions(self) -> tuple[OptionPosition, ...]:
        """Every option contract the account holds, one row per contract.

        The per-leg view :meth:`portfolio` deliberately collapses to underlying
        symbols. Read from the same position stream, filtered to
        ``secType == "OPT"``; stock and everything else is not a leg and is
        skipped, not reported.

        ``average_cost`` is ``Position.avgCost`` **exactly as the venue sends
        it**, unscaled: ``ib_async`` stores the wire value without arithmetic
        (``decoder.py`` ``updatePortfolio``/``position`` pass ``float(avgCost)``
        straight through), and IBKR reports an option's average cost per
        *contract* -- the per-share price, commission included, already
        multiplied by the contract multiplier -- so a 1.75 put shows as
        roughly 175.0. Nothing here derives from it; it is carried so a
        record can say what the venue said.

        A held option that cannot be identified -- no parsable expiry, strike
        or right -- is an error, not a skipped row. The caller uses this list to
        decide whether a spread's legs are still there, and silently dropping
        a leg it cannot name would read as "the position closed".

        Raises:
            MarketDataError: the position stream could not be read, a row
                belongs to another account, or a held option cannot be
                identified.
        """
        ib = self._client()
        account = self._ibkr_config.account

        try:
            raw_positions = list(ib.positions(account))
        except Exception as exc:
            logger.exception("Failed to read positions for account %r", account)
            raise MarketDataError(f"Cannot read IBKR positions: {exc}") from exc

        self._require_one_account(raw_positions, account, "position")

        held: list[OptionPosition] = []
        for raw in raw_positions:
            contract = getattr(raw, "contract", None)
            if str(getattr(contract, "secType", "") or "") != "OPT":
                continue
            reported = _finite(getattr(raw, "position", None))
            quantity = int(reported) if reported is not None else 0
            if quantity == 0:
                continue
            average_cost = _finite(getattr(raw, "avgCost", None))
            held.append(
                OptionPosition(
                    leg=self._held_leg(contract),
                    quantity=quantity,
                    average_cost=(
                        _price(average_cost) if average_cost is not None else Decimal(0)
                    ),
                )
            )
        logger.debug("Option positions: %d contracts held", len(held))
        return tuple(held)

    def quote(self, legs: Sequence[OptionLeg]) -> tuple[OptionQuote, ...]:
        """Current market for exactly these contracts, in the same order.

        The management path: a spread under management is usually outside the
        entry DTE band, so :meth:`snapshot` never quotes its legs. Each leg is
        built as an ``Option`` on SMART/USD with the standard multiplier,
        qualified in one batch, and quoted through the same line-budgeted
        machinery as the chain (:meth:`_quote_batches`), so every line is
        released before this returns exactly as it is for a snapshot.

        A leg the venue quotes one-sided or not at all is still returned, as a
        dead book -- bid 0, ask 0, so ``spread_pct`` is infinite -- rather than
        dropped or raised. The caller decides what an unquotable leg means; a
        missing entry would silently change the *length* of an answer the
        caller zips against its own legs.

        Raises:
            MarketDataError: a leg could not be qualified (named in the
                message), or the market-data request itself failed.
        """
        wanted = tuple(legs)
        if not wanted:
            return ()
        ib = self._client()
        api = self._require_api()

        contracts = [self._leg_contract(api, leg) for leg in wanted]
        resolved = self._qualify_legs(ib, wanted, contracts)
        self._apply_market_data_type(ib)
        tickers = self._quote_batches(ib, resolved, OPTION_GENERIC_TICKS)
        if len(tickers) != len(wanted):
            raise MarketDataError(
                f"IBKR returned {len(tickers)} tickers for {len(wanted)} legs; "
                f"cannot match quotes to legs"
            )
        return tuple(
            self._leg_quote(leg, ticker) for leg, ticker in zip(wanted, tickers, strict=True)
        )

    @staticmethod
    def _describe_leg(leg: OptionLeg) -> str:
        """One leg as an operator would write it: ``AAPL 2026-03-20 185 P``."""
        return f"{leg.symbol} {leg.expiry.isoformat()} {leg.strike} {leg.right.value}"

    @classmethod
    def _held_leg(cls, contract: Any) -> OptionLeg:
        """The identity of a held option, from the venue's contract fields.

        ``lastTradeDateOrContractMonth`` is ``YYYYMMDD`` on a position row;
        ``right`` is ``P``/``C`` but is read by its first letter so the long
        forms ``ib_async`` also accepts (``PUT``/``CALL``) resolve the same way.

        Raises:
            MarketDataError: any of symbol, expiry, strike or right is missing
                or unparsable.
        """
        symbol = str(getattr(contract, "symbol", "") or "")
        raw_expiry = getattr(contract, "lastTradeDateOrContractMonth", "") or ""
        expiry = _parse_expiry(str(raw_expiry))
        strike = _finite(getattr(contract, "strike", None))
        raw_right = str(getattr(contract, "right", "") or "").upper()[:1]
        right = {Right.PUT.value: Right.PUT, Right.CALL.value: Right.CALL}.get(raw_right)
        if not symbol or expiry is None or strike is None or strike <= 0 or right is None:
            raise MarketDataError(
                f"IBKR reported an option position that cannot be identified: "
                f"{getattr(contract, 'localSymbol', '') or contract!r}"
            )
        return OptionLeg(symbol=symbol, expiry=expiry, strike=_price(strike), right=right)

    @staticmethod
    def _leg_contract(api: MarketDataApi, leg: OptionLeg) -> Any:
        """One leg as an unqualified ``Option``, routed as the broker routes it.

        No ``tradingClass``, matching ``broker._build_option``: a management
        order is placed against the contract the broker resolves, so the quote
        must describe that same contract.
        """
        return api.Option(
            leg.symbol,
            leg.expiry.strftime("%Y%m%d"),
            float(leg.strike),
            leg.right.value,
            EXCHANGE,
            currency=CURRENCY,
            multiplier=str(int(CONTRACT_MULTIPLIER)),
        )

    def _qualify_legs(
        self, ib: Any, legs: Sequence[OptionLeg], contracts: Sequence[Any]
    ) -> list[Any]:
        """Resolve every leg's contract, or name the leg that would not resolve.

        Unlike :meth:`_qualify` this is all-or-nothing: the chain is a grid
        whose misses are expected, but a leg here is a contract the account
        holds or held, and one that will not resolve is a fact the caller must
        hear about, not a row to drop.

        ``qualifyContracts`` answers positionally -- a slot is ``None`` for a
        contract it could not resolve -- and stamps the resolved ones in place.
        A double that returns fewer objects than it was given is read the same
        way: whichever contract carries no ``conId`` afterwards is unresolved.
        """
        try:
            answered = list(ib.qualifyContracts(*contracts))
        except Exception as exc:
            named = ", ".join(self._describe_leg(leg) for leg in legs)
            logger.exception("Failed to qualify option legs %s", named)
            raise MarketDataError(f"cannot qualify option legs {named}: {exc}") from exc

        positional = answered if len(answered) == len(contracts) else [None] * len(contracts)
        resolved: list[Any] = []
        for leg, contract, answer in zip(legs, contracts, positional, strict=True):
            answered_ok = answer is not None and getattr(answer, "conId", 0)
            candidate = answer if answered_ok else contract
            if not getattr(candidate, "conId", 0):
                raise MarketDataError(
                    f"IBKR did not resolve option leg {self._describe_leg(leg)} on "
                    f"{EXCHANGE}/{CURRENCY}"
                )
            resolved.append(candidate)
        return resolved

    def _leg_quote(self, leg: OptionLeg, ticker: Any) -> OptionQuote:
        """One leg's ``OptionQuote``, carrying the leg's own identity.

        Identity comes from ``leg``, never from the ticker's contract: the
        caller asked about this contract and must get an answer keyed exactly
        as it asked. Market fields are read the way :meth:`_build_quote` reads
        them; a book with no usable two sides is reported as bid 0 / ask 0.
        """
        market = self._two_sided(ticker)
        if market is None:
            logger.debug(
                "%s: no usable bid/ask; reporting a dead book", self._describe_leg(leg)
            )
            bid = ask = Decimal(0)
        else:
            bid, ask = market
        return OptionQuote(
            symbol=leg.symbol,
            expiry=leg.expiry,
            strike=leg.strike,
            right=leg.right,
            bid=bid,
            ask=ask,
            delta=self._delta(ticker),
            open_interest=self._open_interest(ticker, leg.right),
            volume=_count(getattr(ticker, "volume", None)),
        )

    # ------------------------------------------------------------------
    # Underlying
    # ------------------------------------------------------------------

    @staticmethod
    def _require_one_account(rows: Sequence[Any], account: str, kind: str) -> None:
        """Refuse rows that are not this account's, before any is read as a total.

        Defence in depth. ``accountValues(account)`` and ``positions(account)``
        filter vendor-side, and ``connect()`` has already refused a session that
        does not report this account -- so these rows should not arrive. If they
        do, an assumption is wrong somewhere, and the wrong thing to do with an
        account total whose provenance is uncertain is to size a trade with it.

        This is the exact defect that made an unnamed account hazardous. With
        ``account`` blank, ``accountValues("")`` returned the *union* of every
        account under the login rather than a default one, and the totals were
        then resolved by independent scans over that flat list -- so net
        liquidation could come from one book and buying power from another, and
        the portfolio described neither. Naming the account made that
        unreachable; this makes it unconstructable.

        Rows carrying no account at all are allowed through: a test double need
        not model a field the adapter is not otherwise reading, and their
        absence is not evidence of another book.

        Raises:
            MarketDataError: any row names an account other than this one.
        """
        foreign = sorted(
            {
                str(getattr(row, "account", "") or "")
                for row in rows
                if str(getattr(row, "account", "") or "") not in ("", account)
            }
        )
        if foreign:
            logger.error(
                "IBKR returned %s rows for %s, not the configured account %s",
                kind,
                ", ".join(foreign),
                account,
            )
            raise MarketDataError(
                f"IBKR returned {kind} rows for account(s) "
                f"{', '.join(foreign)} but this process trades {account}; "
                f"refusing to size against another book"
            )

    def _pending_positions(self, ib: Any, account: str) -> tuple[list[Position], bool]:
        """Underlyings with an order still working at the broker, and whether we know.

        Counted as exposure because an unfilled order is about to become a
        position.

        **Scope, stated exactly.** ``openTrades()`` reports the orders of *this
        client*. This reader does not call ``reqAllOpenOrders`` (the broker's
        ``working_order_refs`` does, for management, but its answer is not
        consulted here) or the master-client mechanism, and ``ibkr.client_id``
        defaults to 1 rather than 0, so an order entered by hand in TWS or
        placed by another process is not seen by the concentration check. That
        is a real gap, stated rather than papered over -- an earlier version of
        this docstring claimed the opposite.

        **On failure the caller is told, not defaulted.** Reporting "no working
        orders" for a read that failed is indistinguishable from a genuinely
        empty book, and both concentration guards key on these rows existing --
        so they are skipped rather than failed, silently. The previous rationale
        for defaulting was that any duplicate admitted is bounded by
        ``max_positions``; that reasoning is circular, because ``max_positions``
        is enforced against ``open_symbol_count``, which is computed from the
        very rows the default just discarded.

        So the flag travels instead. The pass is not aborted -- account values
        and positions were read successfully, and other symbols are unaffected
        -- but a trade that would have been submitted under a guard that was
        never evaluated becomes a decision for a human rather than an order.

        Returns:
            The pending rows, and False when the order stream could not be read.
        """
        try:
            trades = list(ib.openTrades())
        except Exception:
            logger.exception("Failed to read open orders for account %r", account)
            return [], False

        pending: list[Position] = []
        for trade in trades:
            order = getattr(trade, "order", None)
            if account and getattr(order, "account", "") not in ("", account):
                continue
            if not self._is_active(trade):
                continue
            symbol = getattr(getattr(trade, "contract", None), "symbol", "") or ""
            quantity = _whole_contracts(_finite(getattr(order, "totalQuantity", None)) or 0.0)
            if not symbol or quantity == 0:
                continue
            status = getattr(getattr(trade, "orderStatus", None), "status", "") or "working"
            pending.append(
                Position(
                    symbol=symbol,
                    quantity=quantity,
                    description=f"working order ({status})",
                    pending=True,
                )
            )
        return pending, True

    @staticmethod
    def _is_active(trade: Any) -> bool:
        """Whether a trade is still live at the broker.

        Prefers ``Trade.isActive()``; falls back to the status string so a test
        double need not reimplement ib_async's state machine.
        """
        is_active = getattr(trade, "isActive", None)
        if callable(is_active):
            try:
                return bool(is_active())
            except Exception:
                logger.exception("Trade.isActive() failed; falling back to status")
        status = getattr(getattr(trade, "orderStatus", None), "status", "") or ""
        return status not in INACTIVE_ORDER_STATUSES

    def _client(self) -> Any:
        """Return the injected client, or fail before touching the network.

        Deliberately does *not* require ``ib_async`` itself. Reading the
        account, position and order streams only touches the injected client;
        the module is needed to *construct* contracts, so the requirement lives
        at those two call sites instead. That keeps ``portfolio()`` usable — and
        testable — without the dependency present.
        """
        if self._ib is None:
            raise MarketDataError(
                "IBKRMarketData was constructed without an IB client; the "
                "connection is injected by the caller and shared with the broker"
            )
        return self._ib

    def _require_api(self) -> MarketDataApi:
        """The ``ib_async`` constructor surface, imported on first use."""
        if self._api is None:
            self._api = _require_ib_async()
        return self._api

    def _apply_market_data_type(self, ib: Any) -> None:
        """Select live, frozen or delayed quotes for this scan.

        Set once per snapshot rather than per request: it is a session-wide
        setting on the IBKR client, so applying it per contract would be noise.

        Options stop quoting outside regular trading hours. Under the default
        live setting a pre-market scan therefore finds no bid/ask and records a
        data error, which is correct -- an order priced off an empty book is
        worse than no order. ``market_data_type = 2`` opts explicitly into the
        previous session's last quotes for off-hours dry runs.
        """
        try:
            ib.reqMarketDataType(self._ibkr_config.market_data_type)
        except Exception as exc:
            logger.exception("Failed to set market data type")
            raise MarketDataError(
                f"cannot set IBKR market data type {self._ibkr_config.market_data_type}: {exc}"
            ) from exc

    def _qualified_underlying(self, ib: Any, symbol: str) -> Any:
        """Resolve ``symbol`` to a contract carrying a ``conId``.

        The conId is not optional convenience: ``reqSecDefOptParams`` is keyed by
        it, so an unqualified underlying yields no chain at all.
        """
        api = self._require_api()
        try:
            stock = api.Stock(symbol, EXCHANGE, CURRENCY)
            qualified = ib.qualifyContracts(stock)
        except Exception as exc:
            logger.exception("Failed to qualify underlying %s", symbol)
            raise MarketDataError(f"{symbol}: cannot qualify underlying: {exc}") from exc

        contract = next((c for c in qualified if getattr(c, "conId", 0)), None)
        if contract is None:
            raise MarketDataError(
                f"{symbol}: IBKR did not resolve an underlying contract on "
                f"{EXCHANGE}/{CURRENCY}"
            )
        return contract

    def _underlying_price(self, symbol: str, ticker: Any) -> Decimal:
        """Pick the most defensible price available from an underlying ticker.

        Order is last trade, then bid/ask midpoint, then previous close. Last
        trade first because that is the price a strike selection should be
        measured against; the midpoint is the outside-hours fallback and the
        close is what remains when the book is empty.

        The midpoint precedes the close deliberately. A live two-sided book is
        the current price; the close is the previous session's. Ranking the
        close first meant selecting strikes against yesterday, which is what
        this ordering exists to avoid -- the code used to contradict the
        sentence above it.
        """
        candidates = (
            _finite(getattr(ticker, "last", None)),
            self._midpoint(ticker),
            _finite(getattr(ticker, "close", None)),
        )
        for value in candidates:
            if value is not None and value > 0:
                return _price(value)
        raise MarketDataError(
            f"{symbol}: no usable underlying price (last, close and midpoint all "
            f"missing or non-positive)"
        )

    @staticmethod
    def _midpoint(ticker: Any) -> float | None:
        """Bid/ask midpoint, or None when either side is missing."""
        bid = _finite(getattr(ticker, "bid", None))
        ask = _finite(getattr(ticker, "ask", None))
        if bid is None or ask is None or bid <= 0 or ask <= 0:
            return None
        return (bid + ask) / 2.0

    # ------------------------------------------------------------------
    # Chain selection
    # ------------------------------------------------------------------

    def _chain_contracts(
        self,
        ib: Any,
        symbol: str,
        underlying: Any,
        underlying_price: Decimal,
        as_of: datetime,
        implied_volatility: float | None,
    ) -> tuple[list[Any], str]:
        """Build the narrowed list of option contracts worth quoting.

        This is the step that makes the line budget achievable rather than
        merely enforced. The full chain for a liquid ETF is thousands of
        contracts; after filtering to the configured DTE band, to puts, and to a
        strike window around spot, it is tens. Filtering *before* requesting
        quotes is the whole point — a post-filter would still have paid for
        every line.

        ``implied_volatility`` is the underlying's current IV from its own
        ticker, or None when IBKR did not send one; it widens the strike window
        on a volatile name (see :meth:`_strike_window`) and nothing else.

        The selected chain's trading class is returned alongside the contracts
        rather than discarded. This is the only place that knows which
        instrument the quotes describe, and the algorithm needs it: an order
        carrying no trading class resolves to the *standard* contract, so
        quoting a non-standard class and submitting from those quotes would
        trade something other than what was reviewed.

        Returns:
            The qualified contracts, and the trading class they were built on.
        """
        api = self._require_api()
        try:
            chains = ib.reqSecDefOptParams(
                underlying.symbol,
                "",  # futFopExchange: blank for equity options
                "STK",
                underlying.conId,
            )
        except Exception as exc:
            logger.exception("Failed to request option chain parameters for %s", symbol)
            raise MarketDataError(f"{symbol}: cannot read option chain: {exc}") from exc

        chain = self._preferred_chain(chains, symbol)
        if chain is None:
            raise MarketDataError(f"{symbol}: IBKR returned no option chain definition")

        # The market date, not ``as_of.date()``: ``as_of`` is UTC, and after
        # 8 pm Eastern the UTC date is already tomorrow. Counting DTE from it
        # would drop an expiry sitting exactly on ``min_dte`` and admit one a
        # day past ``max_dte``.
        today = market_date(as_of)
        expiries = self._expiries_in_band(chain, today)
        if not expiries:
            raise MarketDataError(
                f"{symbol}: no listed expiry between {self._strategy_config.min_dte} "
                f"and {self._strategy_config.max_dte} DTE"
            )

        window = self._strike_window(symbol, implied_volatility)
        strikes = self._strikes_near(chain, underlying_price, window)
        if not strikes:
            raise MarketDataError(
                f"{symbol}: no listed strike within {window:.1%} below {underlying_price}"
            )

        trading_class = getattr(chain, "tradingClass", "") or symbol
        candidates = [
            api.Option(
                symbol,
                expiry.strftime("%Y%m%d"),
                strike,
                Right.PUT.value,
                EXCHANGE,
                currency=CURRENCY,
                tradingClass=trading_class,
            )
            for expiry in expiries
            for strike in strikes
        ]
        logger.debug(
            "%s: %d expiries x %d strikes = %d candidate contracts",
            symbol,
            len(expiries),
            len(strikes),
            len(candidates),
        )
        return self._qualify(ib, symbol, candidates), trading_class

    @staticmethod
    def _preferred_chain(chains: Sequence[Any], symbol: str) -> Any | None:
        """Choose one chain definition from the several IBKR returns.

        IBKR returns one row per *(exchange, trading class)* pair — not one per
        exchange, which is what an earlier version of this docstring said, and
        the difference is the whole point of the second filter below.

        Three preferences, in order:

        1. **SMART**, the routing this adapter quotes and trades on. Unchanged,
           and deliberately so: ``ib_async`` preserves ``SMART`` when qualifying
           a contract precisely because substituting a concrete exchange can
           produce an invalid one.
        2. **The trading class matching the underlying.** Selection used to be
           on strike count alone, so a row describing a *non-standard* class —
           ``AAPL1``, an adjusted contract left behind by a split or a special
           dividend — won whenever it happened to list more strikes. Options on
           an adjusted class are a different instrument with a different
           deliverable, so a longer list of them is not "a more complete view of
           the same options"; it is a view of other options.
        3. **The most strikes**, among rows that are otherwise equivalent. This
           was the whole rule before and remains right where it was never wrong:
           two rows for the same class really are two views of the same options.

        A non-standard row is still returned when it is the only one. Whether a
        trade on it is acceptable is a decision for the caller, which is why the
        selected class travels on the snapshot rather than being discarded here.
        """
        usable = [c for c in chains if getattr(c, "expirations", None)]
        if not usable:
            return None
        smart = [c for c in usable if getattr(c, "exchange", "") == EXCHANGE]
        pool = smart or usable
        # An absent tradingClass is not a match. `_chain_contracts` falls back to
        # the symbol when building a contract, which is right there and would be
        # wrong here: it would make an unlabelled row look confirmed-standard.
        matching = [c for c in pool if getattr(c, "tradingClass", "") == symbol]
        return max(matching or pool, key=lambda c: len(getattr(c, "strikes", ()) or ()))

    def _expiries_in_band(self, chain: Any, today: date) -> list[date]:
        """Expiries inside ``[min_dte, max_dte]``, ascending.

        The band comes from ``StrategyConfig``, whose validator already
        guarantees ``min_dte <= max_dte``, so no re-check is needed here.
        """
        strategy = self._strategy_config
        selected: list[date] = []
        for raw in getattr(chain, "expirations", ()) or ():
            expiry = _parse_expiry(str(raw))
            if expiry is None:
                logger.debug("Ignoring unparsable expiration %r", raw)
                continue
            dte = (expiry - today).days
            if strategy.min_dte <= dte <= strategy.max_dte:
                selected.append(expiry)
        return sorted(selected)

    def _strike_window(self, symbol: str, implied_volatility: float | None) -> float:
        """How far below spot to look for strikes, as a fraction of spot.

        The window must reach the long strike: the short strike sits at
        0.20-0.40 delta and the long strike a further strike below it. Where
        the 0.20-delta put sits depends on the underlying's volatility, so a
        fixed percentage is either too narrow for a volatile name -- the whole
        band lands below the window and the symbol reports "no listed strike"
        -- or wastefully wide for a calm one, spending qualification round
        trips on rows the algorithm discards.

        So the window is the larger of two numbers::

            max(strike_window_pct,
                strike_window_iv_multiple * iv * sqrt(max_dte / 365))

        The second term is ``strike_window_iv_multiple`` standard deviations of
        the underlying's move over the *longest* admissible expiry, using the
        IV IBKR reports on the underlying's own ticker. It is a sizing
        heuristic, not a delta model: ``sqrt(max_dte / 365)`` scales an
        annualized volatility to the horizon, and the multiple is the operator's
        margin. A multiple of 0 disables the term and the floor stands alone.

        A missing, non-finite or non-positive IV is not an error here. The
        reading is a convenience for narrowing, and the floor is a complete
        answer on its own; the symbol is still scanned, and the fallback is
        logged so an unexpectedly narrow window can be traced to its cause.
        Every input and the chosen window go to the DEBUG log for the same
        reason: a run's strike selection must be auditable from its log alone.
        """
        strategy = self._strategy_config
        floor = strategy.strike_window_pct
        multiple = strategy.strike_window_iv_multiple
        horizon = math.sqrt(strategy.max_dte / DAYS_PER_YEAR)

        if multiple <= 0:
            logger.debug(
                "%s: strike window %.4f (floor; iv scaling disabled, multiple=%s)",
                symbol,
                floor,
                multiple,
            )
            return floor

        if implied_volatility is None or implied_volatility <= 0:
            logger.debug(
                "%s: strike window %.4f (floor; underlying iv unavailable: %r)",
                symbol,
                floor,
                implied_volatility,
            )
            return floor

        scaled = multiple * implied_volatility * horizon
        window = max(floor, scaled)
        logger.debug(
            "%s: strike window %.4f (%s; floor=%.4f multiple=%.2f iv=%.4f "
            "max_dte=%d horizon=%.4f scaled=%.4f)",
            symbol,
            window,
            "iv-scaled" if scaled > floor else "floor",
            floor,
            multiple,
            implied_volatility,
            strategy.max_dte,
            horizon,
            scaled,
        )
        return window

    def _strikes_near(
        self, chain: Any, underlying_price: Decimal, window: float
    ) -> list[float]:
        """Listed strikes inside ``[spot * (1 - window), spot]``, ascending.

        ``window`` comes from :meth:`_strike_window`; the upper bound is spot
        itself (:data:`STRIKE_WINDOW_ABOVE`).
        """
        spot = float(underlying_price)
        low = spot * (1.0 - window)
        high = spot * (1.0 + STRIKE_WINDOW_ABOVE)
        selected = {
            value
            for raw in getattr(chain, "strikes", ()) or ()
            if (value := _finite(raw)) is not None and low <= value <= high
        }
        return sorted(selected)

    def _qualify(self, ib: Any, symbol: str, candidates: Sequence[Any]) -> list[Any]:
        """Resolve candidate options to real, listed contracts.

        Batched at ``refresh_limit`` as well. Qualification spends contract-detail
        requests rather than market-data lines, but IBKR paces both, and reusing
        the one configured bound keeps a single number describing how hard this
        adapter is allowed to lean on the connection.

        Unqualifiable candidates are dropped, not fatal: a strike/expiry pair the
        exchange never listed is an expected miss, since the grid is built from
        the cross product of expiries and strikes.
        """
        limit = self._ibkr_config.refresh_limit
        qualified: list[Any] = []
        try:
            for batch in _chunks(candidates, limit):
                resolved = ib.qualifyContracts(*batch)
                qualified.extend(c for c in resolved if c and getattr(c, "conId", 0))
        except Exception as exc:
            logger.exception("Failed to qualify option contracts for %s", symbol)
            raise MarketDataError(f"{symbol}: cannot qualify option contracts: {exc}") from exc

        if not qualified:
            raise MarketDataError(
                f"{symbol}: none of {len(candidates)} candidate contracts are listed"
            )
        logger.debug(
            "%s: %d of %d candidate contracts qualified",
            symbol,
            len(qualified),
            len(candidates),
        )
        return qualified

    # ------------------------------------------------------------------
    # Quoting
    # ------------------------------------------------------------------

    def _quote_underlying(self, ib: Any, underlying: Any) -> Any:
        """Open, wait on, and release a single market-data line for the stock.

        Requested and released before any option batch, so the underlying never
        competes with the chain for the line budget.
        """
        tickers = self._quote_batches(ib, [underlying], UNDERLYING_GENERIC_TICKS)
        if not tickers:
            raise MarketDataError(
                f"{underlying.symbol}: no market data returned for the underlying"
            )
        return tickers[0]

    def _quote_chain(
        self, ib: Any, symbol: str, contracts: Sequence[Any]
    ) -> tuple[OptionQuote, ...]:
        """Quote every contract and build the usable subset as ``OptionQuote``."""
        tickers = self._quote_batches(ib, contracts, OPTION_GENERIC_TICKS)
        quotes: list[OptionQuote] = []
        skipped = 0
        for ticker in tickers:
            quote = self._build_quote(symbol, ticker)
            if quote is None:
                skipped += 1
                continue
            quotes.append(quote)
        if skipped:
            logger.debug(
                "%s: skipped %d of %d contracts with no usable bid/ask",
                symbol,
                skipped,
                len(tickers),
            )
        return tuple(quotes)

    def _quote_batches(
        self, ib: Any, contracts: Sequence[Any], generic_ticks: str
    ) -> list[Any]:
        """Request quotes in batches that never exceed ``refresh_limit``.

        **This is where ``ibkr.refresh_limit`` is enforced.** An IBKR account
        holds a finite number of simultaneous market-data lines; asking for more
        does not raise, it silently starves later requests. So at most
        ``refresh_limit`` lines are open at any instant, and every line in a
        batch is cancelled in a ``finally`` before the next batch opens. The
        cancellation is unconditional: an exception mid-batch must not leak
        lines, or the *next* symbol in the pass inherits a smaller budget than
        the configuration promised.

        The wait is bounded twice over — by a deadline measured on the injected
        clock and by a hard iteration cap — so a symbol that never receives data
        costs one bounded delay rather than hanging the pass. It is not a retry
        loop: each contract is requested exactly once.
        """
        limit = self._ibkr_config.refresh_limit
        collected: list[Any] = []
        try:
            for batch in _chunks(contracts, limit):
                # Track what was actually opened rather than assuming the whole
                # batch was: a reqMktData that raises part-way through leaves
                # every earlier line of the batch open, and the old list
                # comprehension ran outside the try, so those lines leaked.
                opened: list[Any] = []
                try:
                    tickers = []
                    for contract in batch:
                        tickers.append(ib.reqMktData(contract, generic_ticks, False, False))
                        opened.append(contract)
                    self._await_quotes(ib, tickers, self._completeness(generic_ticks))
                    collected.extend(tickers)
                finally:
                    for contract in opened:
                        # One cancel that raises must not skip the rest, or the
                        # remainder of the batch leaks for the same reason.
                        try:
                            ib.cancelMktData(contract)
                        except Exception:
                            logger.exception(
                                "Failed to cancel a market-data line for %r", contract
                            )
        except Exception as exc:
            logger.exception("Market data request failed for %d contracts", len(contracts))
            raise MarketDataError(f"Cannot obtain market data: {exc}") from exc
        return collected

    def _await_quotes(
        self,
        ib: Any,
        tickers: Sequence[Any],
        is_complete: Callable[[Any], bool] | None = None,
    ) -> None:
        """Pump until every ticker carries what the screens read, or time is up.

        Per ticker, not per batch: each one leaves ``pending`` the moment its own
        fields arrive, and this returns on the last of them rather than on a
        schedule.

        The predicate used to be ``all(_has_market(...))`` — bid and ask only —
        which ignored the two fields the strategy actually screens on. Worse, it
        was checked *before* the first pump, so on any ticker whose book was
        already up this returned having pumped zero times. Since
        :meth:`_quote_batches` cancels the batch's lines the instant this
        returns, the model greeks and open interest that were one packet away
        were never collected, and :meth:`_build_quote` silently substituted a
        0.0 delta and an open interest of 0 — indistinguishable, downstream,
        from a real market that fails the screens.

        Incomplete tickers are still not fatal: they degrade through the
        documented fallbacks, and :meth:`_log_incomplete` names what was missing
        so a pass full of them is diagnosable rather than mysterious.

        ``ib.sleep`` rather than ``clock.sleep``: the injected clock's sleep
        blocks the thread, and a blocked thread never runs the socket reader, so
        no tick would ever arrive. The clock still owns *time* — it measures the
        deadline — while ``ib`` owns the pumping. ``ib.waitOnUpdate`` is
        deliberately *not* the pump: it returns on any packet at all, so on a
        busy stream the iteration cap below would elapse in milliseconds and the
        wait would collapse to nothing.
        """
        is_complete = is_complete or self._has_market
        deadline = self._clock.now() + timedelta(seconds=QUOTE_WAIT_SECONDS)
        max_polls = max(1, int(QUOTE_WAIT_SECONDS / QUOTE_POLL_SECONDS))
        grace_polls = max(1, int(GREEKS_WAIT_SECONDS / QUOTE_POLL_SECONDS))

        pending = list(tickers)
        since_book = 0
        for _ in range(max_polls):
            pending = [ticker for ticker in pending if not is_complete(ticker)]
            if not pending:
                return
            if all(self._has_market(ticker) for ticker in tickers):
                # Top of book is up everywhere, so only the slow fields are
                # outstanding. Spend a bounded extra window on them rather than
                # the whole budget: one strike that never reports open interest
                # must not cost every symbol the full wait.
                if since_book >= grace_polls:
                    break
                since_book += 1
            if self._clock.now() >= deadline:
                break
            ib.sleep(QUOTE_POLL_SECONDS)
        self._log_incomplete(pending)

    @classmethod
    def _log_incomplete(cls, pending: Sequence[Any]) -> None:
        """Name what was still missing when the wait ended.

        The whole point of the line: a 0.0 delta and an open interest of 0 are
        indistinguishable downstream from a real market that fails the screens,
        so without this a pass starved of greeks looks like a quiet market.
        """
        if not pending:
            return
        no_book = sum(1 for ticker in pending if not cls._has_market(ticker))
        no_delta = sum(
            1
            for ticker in pending
            if cls._has_market(ticker) and cls._model_delta(ticker) is None
        )
        logger.debug(
            "quote wait ended with %d of the batch incomplete "
            "(%d no book, %d no delta, %d no open interest)",
            len(pending),
            no_book,
            no_delta,
            len(pending) - no_book - no_delta,
        )

    @classmethod
    def _completeness(cls, generic_ticks: str) -> Callable[[Any], bool]:
        """The arrival test for exactly the fields this request paid a tick for.

        A request that never asked for open interest has no option fields to
        wait for. The underlying is the case that matters —
        :data:`UNDERLYING_GENERIC_TICKS` carries no greeks and no open interest —
        and waiting on fields it never subscribed to would spend the grace
        window on every symbol for nothing.
        """
        requested = {tick.strip() for tick in generic_ticks.split(",")}
        return cls._has_option_market if OPEN_INTEREST_TICK in requested else cls._has_market

    @staticmethod
    def _has_market(ticker: Any) -> bool:
        """True when both sides of this ticker's book have arrived."""
        return (
            _finite(getattr(ticker, "bid", None)) is not None
            and _finite(getattr(ticker, "ask", None)) is not None
        )

    @classmethod
    def _has_option_market(cls, ticker: Any) -> bool:
        """True when an option ticker carries everything the screens read.

        Bid and ask are not enough: ``tastytrade._rank_short_puts`` and
        ``_rank_long_puts`` both screen on delta, and ``_liquidity_failure``
        screens on open interest. All three arrive after top of book.
        """
        return (
            cls._has_market(ticker)
            and cls._model_delta(ticker) is not None
            and cls._open_interest_arrived(ticker)
        )

    @staticmethod
    def _open_interest_arrived(ticker: Any) -> bool:
        """True once the open-interest tick has been delivered for either side.

        ``_finite`` rather than ``_count``: the venue writes a real ``0`` for a
        strike with genuinely no open interest and leaves NaN for one it has not
        sent yet, and ``_count`` flattens both to ``0``. Only the NaN means
        "keep waiting". Either side counts — IBKR sends only the one matching
        the contract's right, and threading that right through here would buy
        nothing.
        """
        return any(
            _finite(getattr(ticker, field, None)) is not None
            for field in ("putOpenInterest", "callOpenInterest")
        )

    def _build_quote(
        self, symbol: str, ticker: Any, right: Right = Right.PUT
    ) -> OptionQuote | None:
        """Turn one option ticker into an ``OptionQuote``, or None if unusable.

        A missing bid or ask makes the row unusable: every downstream screen
        (spread percentage, credit, limit price) is defined in terms of both
        sides, and a one-sided market cannot be filled at a computed midpoint.

        Missing *greeks* are not fatal here, but they are not harmless either.
        Delta falls back to 0.0, which cannot be mistaken for a real reading:
        ``StrategyConfig.min_short_delta`` is constrained ``> 0``, so a 0.0-delta
        quote can never be selected as the short strike. Note it cannot serve as
        the *long* leg either — ``tastytrade._rank_long_puts`` filters on
        ``min_long_delta <= abs(delta) <= max_long_delta``, so 0.0 fails that
        band too. Such a quote is therefore invisible to selection, and the
        refusal blames the delta band rather than the missing tick, which is why
        :meth:`_await_quotes` waits for the greek rather than relying on this
        fallback.

        ``right`` is what the ticker was requested as; the chain only ever asks
        for puts, so it defaults to that. The identity fields come from the
        ticker's contract, which is what :meth:`_leg_quote` deliberately does
        not do -- see there.
        """
        contract = getattr(ticker, "contract", None)
        if contract is None:
            return None
        expiry = _parse_expiry(str(getattr(contract, "lastTradeDateOrContractMonth", "")))
        strike = _finite(getattr(contract, "strike", None))
        market = self._two_sided(ticker)
        if expiry is None or strike is None or market is None:
            return None
        bid, ask = market

        return OptionQuote(
            symbol=symbol,
            expiry=expiry,
            strike=_price(strike),
            right=right,
            bid=bid,
            ask=ask,
            delta=self._delta(ticker),
            open_interest=self._open_interest(ticker, right),
            volume=_count(getattr(ticker, "volume", None)),
        )

    @staticmethod
    def _two_sided(ticker: Any) -> tuple[Decimal, Decimal] | None:
        """The ticker's bid and ask as exact prices, or None if not a usable book.

        Both sides must be finite, the ask positive, the bid non-negative and
        not above the ask. Anything else is one-sided, crossed or unsent, and
        no midpoint computed from it can be filled.
        """
        bid = _finite(getattr(ticker, "bid", None))
        ask = _finite(getattr(ticker, "ask", None))
        if bid is None or ask is None:
            return None
        if bid < 0 or ask <= 0 or ask < bid:
            return None
        return _price(bid), _price(ask)

    @staticmethod
    def _model_delta(ticker: Any) -> float | None:
        """This ticker's real model delta, or None when none has arrived.

        Split from :meth:`_delta` because the two questions are different: the
        wait needs to know whether a reading *exists*, and only the quote needs
        the fallback. Collapsing them is what let a not-yet-arrived greek be
        read as a real 0.0.
        """
        greeks = getattr(ticker, "modelGreeks", None) or getattr(ticker, "lastGreeks", None)
        return _finite(getattr(greeks, "delta", None)) if greeks else None

    @classmethod
    def _delta(cls, ticker: Any) -> float:
        """Model delta, falling back to last-computed greeks, then to 0.0."""
        delta = cls._model_delta(ticker)
        return delta if delta is not None else 0.0

    @staticmethod
    def _open_interest(ticker: Any, right: Right) -> int:
        """Open interest for the side the contract is on (generic tick 101)."""
        field = "putOpenInterest" if right is Right.PUT else "callOpenInterest"
        return _count(getattr(ticker, field, None))

    # ------------------------------------------------------------------
    # Volatility
    # ------------------------------------------------------------------

    def _iv_rank(
        self, ib: Any, symbol: str, underlying: Any, as_of: datetime
    ) -> tuple[float, float]:
        """Current implied volatility and its rank over a one-year lookback.

        Two methods, tried in order, both of them real measurements — this never
        returns a placeholder or a hardcoded constant.

        **Cached per symbol and market date.** Both methods spend a
        ``reqHistoricalData`` request, and IBKR paces historical data at
        roughly 60 requests per 10 minutes. A 102-name universe scanned every
        30 minutes would exceed that on every pass, and the answer cannot change
        between passes anyway: the series is daily bars, so within one market
        date (:func:`~ibkr_trader.clock.market_date` of ``as_of``) a second
        request returns the same numbers. A hit therefore skips the request
        entirely. Only a *successful* result is cached -- a failure is raised,
        not remembered, so the next pass tries again rather than repeating a
        transient error all day. The cache lives on this instance and dies with
        the process: ``run`` (one pass) never sees a hit, ``loop`` does from its
        second pass onward.

        **Primary: true IV rank.** One year of daily bars with
        ``whatToShow='OPTION_IMPLIED_VOLATILITY'`` gives IBKR's own daily
        at-the-money implied-volatility series for the underlying. The rank is
        the classic range position of the latest reading::

            iv_rank = 100 * (IV_today - IV_low_1y) / (IV_high_1y - IV_low_1y)

        **Fallback: realized-volatility rank.** When that series is missing,
        too short (< :data:`MIN_HISTORY_BARS` bars) or flat, one year of daily
        ``TRADES`` bars is converted to a rolling 30-day annualized standard
        deviation of log returns, and the latest value is ranked in the same way
        within that series.

        Limitations of the fallback, which the operator must weigh before
        trusting a ``min_iv_rank`` gate built on it:

        * It measures *realized* volatility, not implied. It is backward-looking
          and systematically misses the volatility risk premium, so it typically
          reads lower than a true IV rank in calm markets.
        * It lags. A 30-day window needs weeks to reflect a regime change, so it
          understates the richness of premium immediately after a shock and
          overstates it for weeks after volatility has actually subsided.
        * It cannot see event risk. Implied volatility rises ahead of an
          earnings date or a scheduled announcement; realized volatility does
          not move until after the fact — exactly the setups a premium seller
          most needs to distinguish.
        * Both methods rank against a rolling one-year window, so the 0 and 100
          endpoints are re-anchored every day and are not comparable across time
          or across symbols.

        A fallback reading is logged at WARNING with the reason, so a rank that
        drove a real trade can be identified as a proxy afterwards.

        Raises:
            MarketDataError: neither series is available or usable. Selling
                premium is a bet on volatility being rich; with no measurement
                of richness there is no trade to evaluate, so this fails rather
                than defaulting.
        """
        key = (symbol, market_date(as_of))
        cached = self._iv_rank_cache.get(key)
        if cached is not None:
            logger.debug(
                "%s: iv rank %.1f (iv=%.4f) reused from cache for market date %s",
                symbol,
                cached[1],
                cached[0],
                key[1],
            )
            return cached

        result = self._compute_iv_rank(ib, symbol, underlying)
        self._iv_rank_cache[key] = result
        return result

    def _compute_iv_rank(self, ib: Any, symbol: str, underlying: Any) -> tuple[float, float]:
        """The uncached body of :meth:`_iv_rank`: one or two history requests."""
        try:
            iv_series = self._historical_closes(ib, underlying, "OPTION_IMPLIED_VOLATILITY")
            rank = _percentile_rank(iv_series[-1], iv_series) if iv_series else None
            if rank is not None:
                return iv_series[-1], rank

            logger.warning(
                "%s: implied-volatility history unusable (%d bars); falling back to "
                "a %d-day realized-volatility rank, which lags IV and ignores event risk",
                symbol,
                len(iv_series),
                HV_WINDOW_DAYS,
            )
            closes = self._historical_closes(ib, underlying, "TRADES")
            hv_series = _realized_volatility_series(closes, HV_WINDOW_DAYS)
            rank = _percentile_rank(hv_series[-1], hv_series) if hv_series else None
            if rank is not None:
                return hv_series[-1], rank
        except Exception as exc:
            logger.exception("Failed to compute IV rank for %s", symbol)
            raise MarketDataError(f"{symbol}: cannot compute IV rank: {exc}") from exc

        raise MarketDataError(
            f"{symbol}: no usable volatility history; neither implied nor realized "
            f"volatility could be ranked over {IV_HISTORY_DURATION}"
        )

    def _historical_closes(self, ib: Any, contract: Any, what_to_show: str) -> list[float]:
        """Daily closes for one year of ``what_to_show`` bars, oldest first.

        Non-finite closes are dropped rather than zero-filled: a zero would drag
        the low of the range to the floor and make every rank read 100.
        """
        bars = ib.reqHistoricalData(
            contract,
            endDateTime="",
            durationStr=IV_HISTORY_DURATION,
            barSizeSetting=IV_HISTORY_BAR_SIZE,
            whatToShow=what_to_show,
            useRTH=True,
        )
        closes: list[float] = []
        for bar in bars or ():
            close = _finite(getattr(bar, "close", None))
            if close is not None and close > 0:
                closes.append(close)
        return closes

    # ------------------------------------------------------------------
    # Account values
    # ------------------------------------------------------------------

    @staticmethod
    def _account_amount(values: Sequence[Any], tags: Sequence[str]) -> Decimal | None:
        """First parsable amount among ``tags``, in preference order.

        Restricted to the base/account currency so a multi-currency account's
        per-currency rows cannot be mistaken for the account total.
        """
        for tag in tags:
            for value in values:
                if getattr(value, "tag", "") != tag:
                    continue
                if getattr(value, "currency", "") not in ACCOUNT_CURRENCIES:
                    continue
                try:
                    return Decimal(str(getattr(value, "value", "")))
                except (InvalidOperation, TypeError):
                    logger.debug(
                        "Ignoring unparsable account value %s=%r",
                        tag,
                        getattr(value, "value", None),
                    )
        return None
