"""Live IBKR market data: the adapter behind the :class:`~ibkr_trader.ports.MarketData` port.

This module turns a TWS/Gateway connection into the one immutable
:class:`~ibkr_trader.models.MarketSnapshot` the pure algorithm evaluates. It is
deliberately the *only* place in the system that knows what a ``Ticker``, an
``OptionChain`` or a NaN price is; everything downstream sees exact ``Decimal``
prices and finished quotes.

Four properties are load-bearing:

*Nothing survives the call.* There is no cache, no background subscription, no
reconnection daemon and no module-level state. Every market-data line this
adapter opens is closed before :meth:`IBKRMarketData.snapshot` returns, so a
pass leaves the connection exactly as it found it. A stale quote that outlives
the pass that fetched it is worse than no quote at all — it would price a real
order off a market that no longer exists.

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
from collections.abc import Iterator, Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from .clock import Clock
from .config import IBKRConfig, StrategyConfig
from .errors import MarketDataError
from .models import MarketSnapshot, OptionQuote, Portfolio, Position, Right

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

#: Strike window above spot, as a fraction of the underlying price.
#:
#: The strategy sells puts at 0.20-0.40 delta and buys a fixed width below, so
#: strikes far from spot can never be selected. Quoting them would consume the
#: line budget to produce rows the algorithm discards. Asymmetric because the
#: chain is puts-only: the useful strikes sit below spot.
#:
#: Zero, because a put at or above spot is in the money with a delta past 0.50
#: and can never reach the 0.20-0.40 short band -- so every line spent above
#: spot is spent on a row the algorithm always discards, which is exactly the
#: waste this window exists to prevent. Spot itself stays inside the window so
#: an at-the-money listed strike is still quoted.
STRIKE_WINDOW_ABOVE = 0.0

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

        contracts = self._chain_contracts(ib, symbol, underlying, underlying_price, as_of)
        chain = self._quote_chain(ib, symbol, contracts)
        if not chain:
            raise MarketDataError(
                f"{symbol}: no usable option quotes in {len(contracts)} contracts "
                f"(all missing a bid/ask)"
            )

        implied_volatility, iv_rank = self._iv_rank(ib, symbol, underlying)

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
            implied_volatility=implied_volatility,
            as_of=as_of,
            chain=chain,
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
        account = self._ibkr_config.account or ""

        try:
            values = list(ib.accountValues(account))
        except Exception as exc:
            logger.exception("Failed to read account values for account %r", account)
            raise MarketDataError(f"Cannot read IBKR account values: {exc}") from exc

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

        pending = self._pending_positions(ib, account)
        positions.extend(pending)

        logger.debug(
            "Portfolio: net_liquidation=%s buying_power=%s positions=%d (%d pending)",
            net_liquidation,
            buying_power,
            len(positions),
            len(pending),
        )
        return Portfolio(
            net_liquidation=net_liquidation,
            buying_power=buying_power,
            positions=tuple(positions),
        )

    # ------------------------------------------------------------------
    # Underlying
    # ------------------------------------------------------------------

    def _pending_positions(self, ib: Any, account: str) -> list[Position]:
        """Underlyings with an order still working at the broker.

        Counted as exposure because an unfilled order is about to become a
        position. Every live order is included, not only orders this process
        placed: a manually entered spread on the same underlying is the same
        concentration risk.

        A failure here is logged and treated as "no working orders" rather than
        raised. Positions and account values -- the numbers a trade is sized
        against -- have already been read successfully at this point; refusing
        to scan because the *order* stream hiccuped would be a worse trade-off
        than proceeding, and the duplicate this could admit is bounded by
        ``max_positions``.
        """
        try:
            trades = list(ib.openTrades())
        except Exception:
            logger.exception("Failed to read open orders for account %r", account)
            return []

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
        return pending

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
    ) -> list[Any]:
        """Build the narrowed list of option contracts worth quoting.

        This is the step that makes the line budget achievable rather than
        merely enforced. The full chain for a liquid ETF is thousands of
        contracts; after filtering to the configured DTE band, to puts, and to a
        strike window around spot, it is tens. Filtering *before* requesting
        quotes is the whole point — a post-filter would still have paid for
        every line.
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

        chain = self._preferred_chain(chains)
        if chain is None:
            raise MarketDataError(f"{symbol}: IBKR returned no option chain definition")

        today = as_of.date()
        expiries = self._expiries_in_band(chain, today)
        if not expiries:
            raise MarketDataError(
                f"{symbol}: no listed expiry between {self._strategy_config.min_dte} "
                f"and {self._strategy_config.max_dte} DTE"
            )

        strikes = self._strikes_near(chain, underlying_price)
        if not strikes:
            raise MarketDataError(
                f"{symbol}: no listed strike within the window around {underlying_price}"
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
        return self._qualify(ib, symbol, candidates)

    @staticmethod
    def _preferred_chain(chains: Sequence[Any]) -> Any | None:
        """Choose one chain definition from the several IBKR returns.

        IBKR returns one row per exchange. Prefer SMART — the routing this
        adapter quotes and trades on — and otherwise take the row listing the
        most strikes, which is the most complete view of the same options.
        """
        usable = [c for c in chains if getattr(c, "expirations", None)]
        if not usable:
            return None
        smart = [c for c in usable if getattr(c, "exchange", "") == EXCHANGE]
        pool = smart or usable
        return max(pool, key=lambda c: len(getattr(c, "strikes", ()) or ()))

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

    def _strikes_near(self, chain: Any, underlying_price: Decimal) -> list[float]:
        """Listed strikes inside the window around spot, ascending.

        The window must be wide enough below spot to hold both legs: the short
        strike sits at 0.20-0.40 delta and the long strike a further
        ``spread_width`` below it.
        """
        spot = float(underlying_price)
        window = self._strategy_config.strike_window_pct
        low = spot * (1.0 - window) - float(self._strategy_config.spread_width)
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
                    self._await_quotes(ib, tickers)
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

    def _await_quotes(self, ib: Any, tickers: Sequence[Any]) -> None:
        """Pump the client's event loop until quotes arrive or the wait expires.

        ``ib.sleep`` rather than ``clock.sleep``: the injected clock's sleep
        blocks the thread, and a blocked thread never runs the socket reader, so
        no tick would ever arrive. The clock still owns *time* — it measures the
        deadline — while ``ib`` owns the pumping.
        """
        deadline = self._clock.now() + timedelta(seconds=QUOTE_WAIT_SECONDS)
        max_polls = max(1, int(QUOTE_WAIT_SECONDS / QUOTE_POLL_SECONDS))
        for _ in range(max_polls):
            if all(self._has_market(ticker) for ticker in tickers):
                return
            if self._clock.now() >= deadline:
                return
            ib.sleep(QUOTE_POLL_SECONDS)

    @staticmethod
    def _has_market(ticker: Any) -> bool:
        """True when both sides of this ticker's book have arrived."""
        return (
            _finite(getattr(ticker, "bid", None)) is not None
            and _finite(getattr(ticker, "ask", None)) is not None
        )

    def _build_quote(self, symbol: str, ticker: Any) -> OptionQuote | None:
        """Turn one option ticker into an ``OptionQuote``, or None if unusable.

        A missing bid or ask makes the row unusable: every downstream screen
        (spread percentage, credit, limit price) is defined in terms of both
        sides, and a one-sided market cannot be filled at a computed midpoint.

        Missing *greeks* are not fatal. Delta falls back to 0.0, which cannot be
        mistaken for a real reading: ``StrategyConfig.min_short_delta`` is
        constrained ``> 0``, so a 0.0-delta quote can never be selected as the
        short strike, while still remaining available as the long leg (which is
        chosen by strike, not by delta).
        """
        contract = getattr(ticker, "contract", None)
        if contract is None:
            return None
        expiry = _parse_expiry(str(getattr(contract, "lastTradeDateOrContractMonth", "")))
        strike = _finite(getattr(contract, "strike", None))
        bid = _finite(getattr(ticker, "bid", None))
        ask = _finite(getattr(ticker, "ask", None))
        if expiry is None or strike is None or bid is None or ask is None:
            return None
        if bid < 0 or ask <= 0 or ask < bid:
            return None

        greeks = getattr(ticker, "modelGreeks", None) or getattr(ticker, "lastGreeks", None)
        delta = _finite(getattr(greeks, "delta", None)) if greeks else None

        return OptionQuote(
            symbol=symbol,
            expiry=expiry,
            strike=_price(strike),
            right=Right.PUT,
            bid=_price(bid),
            ask=_price(ask),
            delta=delta if delta is not None else 0.0,
            open_interest=_count(getattr(ticker, "putOpenInterest", None)),
            volume=_count(getattr(ticker, "volume", None)),
        )

    # ------------------------------------------------------------------
    # Volatility
    # ------------------------------------------------------------------

    def _iv_rank(self, ib: Any, symbol: str, underlying: Any) -> tuple[float, float]:
        """Compute current implied volatility and its rank over a one-year lookback.

        Two methods, tried in order, both of them real measurements — this never
        returns a placeholder or a hardcoded constant.

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
