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

Strategy: short put vertical (put credit spread). Sell a put at the short delta
target, buy a further out-of-the-money put at the long delta target to define
the risk, and collect a credit worth a minimum fraction of the resulting width.
The width is whatever the distance between those two strikes happens to be on
this chain -- an output of selection, never an input to it.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import ROUND_DOWN, Decimal

from .clock import market_date
from .config import RiskConfig, StrategyConfig
from .models import (
    CONTRACT_MULTIPLIER,
    Action,
    MarketSnapshot,
    NeedsDecision,
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

    "Today" is the US market calendar date, not the UTC date: an expiry is a US
    date, and for the hours after 8 pm Eastern the UTC date is already tomorrow,
    which would count every expiry one day short and push a 30-DTE expiry out
    of a 30-day band that should include it.
    """
    today = market_date(snapshot.as_of)
    candidates = [
        (expiry, (expiry - today).days)
        for expiry in snapshot.expiries()
        if strategy.min_dte <= (expiry - today).days <= strategy.max_dte
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda item: (abs(item[1] - strategy.target_dte), item[1]))
    return candidates[0][0]


def _rank_short_puts(
    puts: tuple[OptionQuote, ...], strategy: StrategyConfig
) -> tuple[OptionQuote, ...]:
    """Rank every short-strike candidate in the delta band, best first.

    Put deltas are negative; the band is expressed in absolute terms. Ties break
    toward the lower strike (further out of the money, the safer side).

    Every candidate is returned, not only the best one: a candidate whose
    partner strike is missing, or whose either leg is illiquid, must not end the
    search while tradable alternatives remain in the band.
    """
    candidates = [
        put
        for put in puts
        if strategy.min_short_delta <= abs(put.delta) <= strategy.max_short_delta
    ]
    candidates.sort(key=lambda p: (abs(abs(p.delta) - strategy.short_delta_target), p.strike))
    return tuple(candidates)


def _rank_long_puts(
    puts: tuple[OptionQuote, ...], short_put: OptionQuote, strategy: StrategyConfig
) -> tuple[OptionQuote, ...]:
    """Rank every long-strike candidate for one short put, best first.

    The long put defines the risk, so it must sit strictly below the short
    strike, and it is chosen by delta rather than by a fixed distance: the
    spread is as wide as the gap between the two target deltas happens to be
    on this chain. Ties break toward the *higher* strike -- the narrower
    spread -- because at equal delta distance the narrower spread risks less
    per contract for the same short leg.

    Every candidate in the band is returned so that an illiquid best choice
    can be skipped rather than disqualify the short strike it partners.
    """
    candidates = [
        put
        for put in puts
        if put.strike < short_put.strike
        and strategy.min_long_delta <= abs(put.delta) <= strategy.max_long_delta
    ]
    candidates.sort(key=lambda p: (abs(abs(p.delta) - strategy.long_delta_target), -p.strike))
    return tuple(candidates)


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
) -> tuple[int, str]:
    """Contracts to trade, and the name of the ceiling that bound.

    Three independent ceilings apply -- the per-trade risk budget, available
    buying power, and the hard contract cap -- and any of them can be the one
    that produces zero. Returning which one bound lets the caller state the true
    reason for a refusal instead of attributing every refusal to the budget.

    Returns 0 when a single contract already exceeds the tightest ceiling, which
    the caller reports as a no-trade rather than rounding up to one.
    """
    if max_loss_per_contract <= 0:
        return 0, "defined risk"
    budget = portfolio.net_liquidation * Decimal(str(risk.max_risk_per_trade))
    # Ordered most-meaningful first, so a tie is attributed to the risk budget.
    ceilings = (
        (int(budget // max_loss_per_contract), "risk budget"),
        (int(portfolio.buying_power // max_loss_per_contract), "buying power"),
        (risk.max_contracts, "contract cap"),
    )
    quantity, binding = min(ceilings, key=lambda ceiling: ceiling[0])
    return max(0, quantity), binding


def _sizing_refusal(
    binding: str,
    max_loss_per_contract: Decimal,
    portfolio: Portfolio,
    risk: RiskConfig,
) -> str:
    """Explain a zero-contract result in terms of the ceiling that produced it."""
    if binding == "buying power":
        return (
            f"defined risk {max_loss_per_contract} per contract exceeds available "
            f"buying power {portfolio.buying_power}"
        )
    if binding == "contract cap":
        return f"the contract cap is {risk.max_contracts}, so no position can be opened"
    if binding == "defined risk":
        return "defined risk per contract is not positive; the spread cannot be sized"
    budget = portfolio.net_liquidation * Decimal(str(risk.max_risk_per_trade))
    return (
        f"defined risk {max_loss_per_contract} per contract exceeds the "
        f"{risk.max_risk_per_trade:.1%} per-trade budget ({budget}) on "
        f"{portfolio.net_liquidation} net liquidation"
    )


def evaluate(
    symbol: str,
    snapshot: MarketSnapshot,
    portfolio: Portfolio,
    strategy: StrategyConfig,
    risk: RiskConfig,
    now: datetime,
) -> TradeProposal | NoTrade | NeedsDecision:
    """Decide whether to trade one symbol.

    Args:
        symbol: The underlying being evaluated.
        snapshot: Market and option-chain data, already fetched.
        portfolio: Account state, for sizing and concentration limits.
        strategy: Selection criteria.
        risk: Sizing and portfolio ceilings.
        now: Proposal timestamp, injected so this function stays pure.

    Returns:
        A :class:`TradeProposal` to place; :class:`NoTrade` with the
        operator-facing reason it was declined; or :class:`NeedsDecision` when a
        trade exists but cannot be ruled on here. The third is not a refusal --
        it is the absence of a decision, and it carries the proposal so a human
        has the trade in front of them. It is returned only after a proposal has
        been built, so a symbol that would have been declined anyway never
        spends one.
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
    dte = (expiry - market_date(snapshot.as_of)).days

    puts = snapshot.puts_for(expiry)
    if not puts:
        return NoTrade(f"no put quotes for {expiry.isoformat()}")

    # --- short strike, its partner, and liquidity, with backtracking ---
    #
    # The candidates are tried in preference order rather than committing to the
    # single best delta: a missing partner or an illiquid leg disqualifies that
    # candidate, not the whole symbol, while tradable alternatives remain. The
    # same holds one level down -- an illiquid best long put yields to the next
    # long put in the band before the short strike itself is given up on.
    candidates = _rank_short_puts(puts, strategy)
    if not candidates:
        return NoTrade(
            f"no {dte}-DTE short put between {strategy.min_short_delta:.2f} and "
            f"{strategy.max_short_delta:.2f} delta"
        )

    pair: tuple[OptionQuote, OptionQuote] | None = None
    first_failure: str | None = None
    for candidate in candidates:
        # The short leg is screened before its partners are even ranked: if it
        # cannot be filled, no choice of long put rescues the spread.
        failure = _liquidity_failure(candidate, strategy)
        if failure is None:
            partners = _rank_long_puts(puts, candidate, strategy)
            if not partners:
                failure = (
                    f"no put below the {candidate.strike} short strike between "
                    f"{strategy.min_long_delta:.2f} and {strategy.max_long_delta:.2f} "
                    f"delta to define the spread"
                )
            for partner in partners:
                partner_failure = _liquidity_failure(partner, strategy)
                if partner_failure is None:
                    pair = (candidate, partner)
                    break
                failure = failure or partner_failure
        if pair is not None:
            break
        # The preferred candidate's failure is the one the operator asked about.
        first_failure = first_failure or failure

    if pair is None:
        return NoTrade(first_failure or "no tradable spread in the delta band")
    short_put, long_put = pair
    # Derived, never configured: the risk is defined by wherever the long delta
    # target landed on this chain.
    width = short_put.strike - long_put.strike

    # --- premium must justify the risk ---
    #
    # The ratio is tested on the unrounded credit. Rounding down first makes the
    # screen strictly stricter than the configured minimum, rejecting spreads
    # the configuration accepts; only the order's price is rounded, because the
    # rounded figure is what can actually be collected.
    raw_credit = short_put.mid - long_put.mid
    if raw_credit <= 0:
        return NoTrade(f"{short_put.strike}/{long_put.strike} put spread offers no net credit")
    credit_ratio = float(raw_credit / width)
    if credit_ratio < strategy.min_credit_ratio:
        return NoTrade(
            f"credit {_round_credit(raw_credit)} is {credit_ratio:.1%} of the "
            f"{width:.2f}-wide spread, below the "
            f"{strategy.min_credit_ratio:.1%} minimum"
        )

    credit = _round_credit(raw_credit)
    if credit <= 0:
        return NoTrade(
            f"{short_put.strike}/{long_put.strike} put spread credit rounds to zero "
            f"at a {TICK} tick"
        )

    # --- defined risk and sizing ---
    max_loss_per_contract = (width - credit) * CONTRACT_MULTIPLIER
    quantity, binding = _position_size(max_loss_per_contract, portfolio, risk)
    if quantity < 1:
        return NoTrade(_sizing_refusal(binding, max_loss_per_contract, portfolio, risk))

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
        "long_delta": (
            f"{abs(long_put.delta):.2f} within "
            f"[{strategy.min_long_delta:.2f}, {strategy.max_long_delta:.2f}], "
            f"target {strategy.long_delta_target:.2f}"
        ),
        "credit_ratio": (
            f"{credit_ratio:.1%} of width >= {strategy.min_credit_ratio:.1%} minimum"
        ),
        # Derived from the two selected strikes, so the reviewer and the stored
        # record still see the width the risk arithmetic actually used.
        "spread_width": f"{width:.2f}",
        "liquidity": (
            f"short spread {short_put.spread_pct:.1%} / OI {short_put.open_interest}; "
            f"long spread {long_put.spread_pct:.1%} / OI {long_put.open_interest}"
        ),
        "sizing": (
            f"{quantity} contract(s); {max_loss} max loss vs "
            f"{risk.max_risk_per_trade:.1%} of {portfolio.net_liquidation}"
        ),
    }

    proposal = TradeProposal(
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

    if snapshot.trading_class and snapshot.trading_class != symbol:
        # The quote and the order must name the same instrument, and today they
        # cannot be shown to: `broker._build_option` sends no tradingClass at
        # all, so the venue would resolve the *standard* contract at this strike
        # and expiry while the quote, the screen and the review all referred to
        # the non-standard one. The failure mode is not a rejected order -- it
        # is a filled one, on a different deliverable, that nothing in the
        # record distinguishes from the intended trade. Refused here until the
        # broker carries the class; see the tracked broker-side defect.
        return NeedsDecision(
            symbol=symbol,
            reason=(
                f"{symbol} quoted on the non-standard trading class "
                f"{snapshot.trading_class!r}; the broker submits no trading "
                f"class, so the order would name a different instrument than "
                f"the one quoted and reviewed"
            ),
            proposal=proposal,
        )

    if not portfolio.pending_orders_known:
        # Every guard above that could have refused this trade reads rows the
        # adapter synthesizes from still-working orders, and those rows are
        # missing. The guards did not pass -- they were not evaluated. Asking
        # is the only honest answer available, and it is asked here rather than
        # before the chain work so a symbol that would have been declined
        # anyway does not spend a human decision.
        return NeedsDecision(
            symbol=symbol,
            reason=(
                "working-order state is unknown, so neither the duplicate-symbol "
                "guard nor the position limit could be evaluated for this trade"
            ),
            proposal=proposal,
        )

    return proposal
