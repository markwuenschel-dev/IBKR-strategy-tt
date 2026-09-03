"""The Tastytrade trade-qualification algorithm.

This module is the functional core: pure, deterministic, and free of I/O, clocks
and mutable state. It receives a snapshot and returns a decision. That is what
makes the mission test reproducible without a network.

The algorithm owns *all* trade qualification — volatility, expiry, delta,
liquidity, premium, defined risk, sizing and portfolio limits. Those are trading
criteria, not operational gates: they resolve to exactly one of two outcomes,

    TradeProposal   -- place this specific trade
    NoTrade(reason) -- do not trade this symbol, and here is why

and never to persisted readiness state. Adding a criterion means adding a check
here, not adding a state file.

Strategy: short put vertical (put credit spread). Sell a put at the target
delta, buy a further out-of-the-money put a fixed width below it to define the
risk, and collect a credit worth a minimum fraction of that width.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import ROUND_DOWN, Decimal

from .config import RiskConfig, StrategyConfig
from .models import (
    CONTRACT_MULTIPLIER,
    Action,
    MarketSnapshot,
    NoTrade,
    OptionQuote,
    Portfolio,
    ProposalLeg,
    Right,
    TradeProposal,
)

STRATEGY_NAME = "short_put_vertical"

#: Price increment orders are rounded to.
TICK = Decimal("0.01")


def _round_credit(value: Decimal) -> Decimal:
    """Round a credit down to the nearest tick.

    Rounds *down* deliberately: quoting a credit we cannot actually collect
    would overstate both the premium and the maximum profit.
    """
    return value.quantize(TICK, rounding=ROUND_DOWN)


def _select_expiry(snapshot: MarketSnapshot, strategy: StrategyConfig) -> date | None:
    """Pick the expiry closest to the target DTE within the allowed band.

    Ties break toward the nearer expiry, so selection is deterministic for any
    chain rather than dependent on dictionary or exchange ordering.
    """
    today = snapshot.as_of.date()
    candidates = [
        (expiry, (expiry - today).days)
        for expiry in snapshot.expiries()
        if strategy.min_dte <= (expiry - today).days <= strategy.max_dte
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda item: (abs(item[1] - strategy.target_dte), item[1]))
    return candidates[0][0]


def _select_short_put(
    puts: tuple[OptionQuote, ...], strategy: StrategyConfig
) -> OptionQuote | None:
    """Pick the short strike closest to the target delta within the band.

    Put deltas are negative; the band is expressed in absolute terms. Ties break
    toward the lower strike (further out of the money, the safer side).
    """
    candidates = [
        put
        for put in puts
        if strategy.min_short_delta <= abs(put.delta) <= strategy.max_short_delta
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda p: (abs(abs(p.delta) - strategy.short_delta_target), p.strike))
    return candidates[0]


def _liquidity_failure(quote: OptionQuote, strategy: StrategyConfig) -> str | None:
    """Return why this leg is untradable, or None when it passes.

    Checked per leg, because a spread is only as fillable as its worse side.
    """
    if quote.bid <= 0:
        return f"{quote.strike} put has no bid"
    if quote.spread_pct > strategy.max_spread_pct:
        return (
            f"{quote.strike} put bid/ask spread is {quote.spread_pct:.1%} of mid, "
            f"above the {strategy.max_spread_pct:.1%} limit"
        )
    if quote.open_interest < strategy.min_open_interest:
        return (
            f"{quote.strike} put open interest {quote.open_interest} "
            f"below minimum {strategy.min_open_interest}"
        )
    if quote.volume < strategy.min_volume:
        return f"{quote.strike} put volume {quote.volume} below minimum {strategy.min_volume}"
    return None


def _position_size(
    max_loss_per_contract: Decimal, portfolio: Portfolio, risk: RiskConfig
) -> int:
    """Contracts to trade, bounded by risk budget, buying power and hard cap.

    Returns 0 when a single contract already exceeds the per-trade risk budget,
    which the caller reports as a no-trade rather than rounding up to one.
    """
    if max_loss_per_contract <= 0:
        return 0
    budget = portfolio.net_liquidation * Decimal(str(risk.max_risk_per_trade))
    by_risk = int(budget // max_loss_per_contract)
    by_buying_power = int(portfolio.buying_power // max_loss_per_contract)
    return max(0, min(by_risk, by_buying_power, risk.max_contracts))


def evaluate(
    symbol: str,
    snapshot: MarketSnapshot,
    portfolio: Portfolio,
    strategy: StrategyConfig,
    risk: RiskConfig,
    now: datetime,
) -> TradeProposal | NoTrade:
    """Decide whether to trade one symbol.

    Args:
        symbol: The underlying being evaluated.
        snapshot: Market and option-chain data, already fetched.
        portfolio: Account state, for sizing and concentration limits.
        strategy: Selection criteria.
        risk: Sizing and portfolio ceilings.
        now: Proposal timestamp, injected so this function stays pure.

    Returns:
        A :class:`TradeProposal` to place, or :class:`NoTrade` with the
        operator-facing reason it was declined.
    """
    # --- volatility: only sell premium when it is historically rich ---
    if snapshot.iv_rank < strategy.min_iv_rank:
        return NoTrade(
            f"IV rank {snapshot.iv_rank:.1f} below minimum {strategy.min_iv_rank:.1f}"
        )

    # --- portfolio concentration, before doing any chain work ---
    held = [p for p in portfolio.positions_for(symbol) if p.quantity != 0]
    if held and not risk.allow_duplicate_symbol:
        # A resting order counts. Without this, a repeat scan re-proposes the
        # same trade every interval until the first one fills.
        if all(p.pending for p in held):
            return NoTrade(f"a working order in {symbol} is already outstanding")
        return NoTrade(f"already holding a position in {symbol}")
    if (
        not portfolio.has_position(symbol)
        and portfolio.open_symbol_count >= risk.max_positions
    ):
        return NoTrade(
            f"portfolio already holds {portfolio.open_symbol_count} underlyings "
            f"(limit {risk.max_positions})"
        )

    # --- expiry ---
    expiry = _select_expiry(snapshot, strategy)
    if expiry is None:
        return NoTrade(f"no expiry between {strategy.min_dte} and {strategy.max_dte} DTE")
    dte = (expiry - snapshot.as_of.date()).days

    puts = snapshot.puts_for(expiry)
    if not puts:
        return NoTrade(f"no put quotes for {expiry.isoformat()}")

    # --- short strike ---
    short_put = _select_short_put(puts, strategy)
    if short_put is None:
        return NoTrade(
            f"no {dte}-DTE short put between {strategy.min_short_delta:.2f} and "
            f"{strategy.max_short_delta:.2f} delta"
        )

    # --- long strike defines the risk ---
    long_strike = short_put.strike - strategy.spread_width
    long_put = next((p for p in puts if p.strike == long_strike), None)
    if long_put is None:
        return NoTrade(
            f"no put at {long_strike} to define a {strategy.spread_width}-wide "
            f"spread below the {short_put.strike} short strike"
        )

    # --- liquidity, both legs ---
    for leg_quote in (short_put, long_put):
        failure = _liquidity_failure(leg_quote, strategy)
        if failure is not None:
            return NoTrade(failure)

    # --- premium must justify the risk ---
    credit = _round_credit(short_put.mid - long_put.mid)
    if credit <= 0:
        return NoTrade(f"{short_put.strike}/{long_put.strike} put spread offers no net credit")
    credit_ratio = float(credit / strategy.spread_width)
    if credit_ratio < strategy.min_credit_ratio:
        return NoTrade(
            f"credit {credit} is {credit_ratio:.1%} of the {strategy.spread_width}-wide "
            f"spread, below the {strategy.min_credit_ratio:.1%} minimum"
        )

    # --- defined risk and sizing ---
    max_loss_per_contract = (strategy.spread_width - credit) * CONTRACT_MULTIPLIER
    quantity = _position_size(max_loss_per_contract, portfolio, risk)
    if quantity < 1:
        return NoTrade(
            f"defined risk {max_loss_per_contract} per contract exceeds the "
            f"{risk.max_risk_per_trade:.1%} per-trade budget on "
            f"{portfolio.net_liquidation} net liquidation"
        )

    max_loss = max_loss_per_contract * quantity
    max_profit = credit * CONTRACT_MULTIPLIER * quantity

    legs = (
        ProposalLeg(
            action=Action.SELL,
            right=Right.PUT,
            strike=short_put.strike,
            expiry=expiry,
            ratio=1,
            bid=short_put.bid,
            ask=short_put.ask,
            delta=short_put.delta,
            open_interest=short_put.open_interest,
            volume=short_put.volume,
        ),
        ProposalLeg(
            action=Action.BUY,
            right=Right.PUT,
            strike=long_put.strike,
            expiry=expiry,
            ratio=1,
            bid=long_put.bid,
            ask=long_put.ask,
            delta=long_put.delta,
            open_interest=long_put.open_interest,
            volume=long_put.volume,
        ),
    )

    # Recorded so the reviewer sees *why* this trade was selected, not just what
    # it is, and so a rejected trade can be argued with after the fact.
    criteria = {
        "iv_rank": f"{snapshot.iv_rank:.1f} >= {strategy.min_iv_rank:.1f}",
        "dte": (
            f"{dte} within [{strategy.min_dte}, {strategy.max_dte}], "
            f"target {strategy.target_dte}"
        ),
        "short_delta": (
            f"{abs(short_put.delta):.2f} within "
            f"[{strategy.min_short_delta:.2f}, {strategy.max_short_delta:.2f}]"
        ),
        "credit_ratio": (
            f"{credit_ratio:.1%} of width >= {strategy.min_credit_ratio:.1%} minimum"
        ),
        "spread_width": str(strategy.spread_width),
        "liquidity": (
            f"short spread {short_put.spread_pct:.1%} / OI {short_put.open_interest}; "
            f"long spread {long_put.spread_pct:.1%} / OI {long_put.open_interest}"
        ),
        "sizing": (
            f"{quantity} contract(s); {max_loss} max loss vs "
            f"{risk.max_risk_per_trade:.1%} of {portfolio.net_liquidation}"
        ),
    }

    return TradeProposal(
        symbol=symbol,
        strategy=STRATEGY_NAME,
        expiry=expiry,
        dte=dte,
        legs=legs,
        quantity=quantity,
        limit_price=credit,
        max_profit=max_profit,
        max_loss=max_loss,
        underlying_price=snapshot.underlying_price,
        iv_rank=snapshot.iv_rank,
        short_delta=short_put.delta,
        # For a defined-risk vertical, buying power reduction is the max loss.
        buying_power_effect=max_loss,
        criteria=criteria,
        created_at=now,
    )
