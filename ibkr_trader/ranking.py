"""Rank the pass's trade proposals against each other.

Pure: no I/O, no clock, no configuration. The input is every proposal the
algorithm produced this pass; the output is the same proposals in the order
they should be offered to the reviewer, each carrying the score that put it
there and the three figures the score was built from.

Why rank at all
---------------
With a hundred-name universe and ten position slots, "process symbols in
universe order and trade whatever qualifies" fills the book with the first ten
names alphabetically that clear the screens, not the ten best setups. The
strategy's own preference is explicit: among otherwise valid candidates, favor
better credit relative to width, richer implied volatility, and better
liquidity. Ranking is how that preference becomes a decision.

Why percentile ranks, not raw values
------------------------------------
The three inputs live on unrelated scales -- a ratio near 0.35, a rank on
0-100, a spread fraction near 0.05 -- and a weighted sum of raw values would
be dominated by whichever happened to be numerically largest. Each input is
therefore first converted to its percentile *among this pass's candidates*
(1.0 = best of the pass, 0.0 = worst), and the weights combine percentiles.
The score of a proposal is thus relative to the field it competed in, which is
exactly the question the pass is answering: of the trades available today,
which are the best?

A single candidate is trivially the best of its field and scores 1.0.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from .models import Action, TradeProposal

#: How much each figure contributes to the composite score. Sum to 1.
#:
#: Credit relative to width is the strategy's stated premium bar and the
#: figure most directly tied to expected return, so it carries half the
#: weight. IV rank says whether premium is rich *for this name*, and leg
#: tightness says how much of the quoted credit will survive the fill.
WEIGHT_CREDIT_RATIO = 0.5
WEIGHT_IV_RANK = 0.3
WEIGHT_LIQUIDITY = 0.2


@dataclass(frozen=True, slots=True)
class RankedProposal:
    """One proposal with its place in the pass's field.

    ``rank`` is 1-based; 1 is the best. ``score`` is the weighted composite of
    the three percentiles, in [0, 1]. The three raw figures are carried so the
    recorded detail can show the operator *why* a candidate placed where it
    did, not just that it did.
    """

    proposal: TradeProposal
    rank: int
    score: float
    credit_ratio: float
    iv_rank: float
    liquidity: float

    @property
    def symbol(self) -> str:
        return self.proposal.symbol

    def describe(self, field_size: int) -> str:
        """One line for the record: place, score, and the figures behind it."""
        return (
            f"rank {self.rank}/{field_size} score {self.score:.2f} "
            f"(credit/width {self.credit_ratio:.2f}, IV rank {self.iv_rank:.0f}, "
            f"leg spread {self.liquidity:.1%})"
        )


def spread_width(proposal: TradeProposal) -> Decimal:
    """Short strike minus long strike, from the legs themselves.

    Width is not a field on the proposal because it is derived from the legs,
    and a second copy could disagree with them. For a put vertical the sold
    leg is the higher strike.
    """
    sold = [leg.strike for leg in proposal.legs if leg.action is Action.SELL]
    bought = [leg.strike for leg in proposal.legs if leg.action is Action.BUY]
    if not sold or not bought:
        raise ValueError(
            f"proposal {proposal.proposal_id} for {proposal.symbol} does not have both "
            f"a sold and a bought leg"
        )
    return max(sold) - min(bought)


def credit_ratio(proposal: TradeProposal) -> float:
    """Credit collected per point of width: the strategy's premium bar."""
    width = spread_width(proposal)
    if width <= 0:
        raise ValueError(
            f"proposal {proposal.proposal_id} for {proposal.symbol} has width {width}"
        )
    return float(proposal.limit_price / width)


def leg_liquidity(proposal: TradeProposal) -> float:
    """Mean relative bid/ask spread across the legs. Lower is better.

    A dead book on any leg is infinite in :class:`~ibkr_trader.models.Quoted`,
    and the mean keeps it infinite, so such a proposal ranks last on this
    figure rather than being silently averaged away.
    """
    return sum(leg.spread_pct for leg in proposal.legs) / len(proposal.legs)


def _percentiles(values: Sequence[float], *, higher_is_better: bool) -> list[float]:
    """Percentile of each value among all of them, 1.0 best and 0.0 worst.

    Ties share the mean of the positions they span, so two equal candidates
    receive the same percentile and neither is favored by input order.
    """
    n = len(values)
    if n == 1:
        return [1.0]
    percentiles: list[float] = []
    for i, value in enumerate(values):
        better = 0
        ties = 0
        for j, other in enumerate(values):
            if j == i:
                continue
            if other == value:
                ties += 1
            elif (other > value) if higher_is_better else (other < value):
                better += 1
        # Position from the top, counting half the ties as ahead of us.
        position = better + ties / 2
        percentiles.append(1.0 - position / (n - 1))
    return percentiles


def rank_proposals(proposals: Sequence[TradeProposal]) -> tuple[RankedProposal, ...]:
    """Order the pass's proposals best first.

    Ties on the composite score break toward the higher credit ratio, then the
    richer IV rank, then the symbol name, so the order is total and the same
    input always produces the same output.
    """
    if not proposals:
        return ()
    ratios = [credit_ratio(p) for p in proposals]
    ranks = [p.iv_rank for p in proposals]
    liquidity = [leg_liquidity(p) for p in proposals]

    ratio_pct = _percentiles(ratios, higher_is_better=True)
    rank_pct = _percentiles(ranks, higher_is_better=True)
    liquidity_pct = _percentiles(liquidity, higher_is_better=False)

    scored = []
    for i, proposal in enumerate(proposals):
        score = (
            WEIGHT_CREDIT_RATIO * ratio_pct[i]
            + WEIGHT_IV_RANK * rank_pct[i]
            + WEIGHT_LIQUIDITY * liquidity_pct[i]
        )
        scored.append((proposal, score, ratios[i], ranks[i], liquidity[i]))

    scored.sort(key=lambda s: (-s[1], -s[2], -s[3], s[0].symbol))
    return tuple(
        RankedProposal(
            proposal=proposal,
            rank=position,
            score=score,
            credit_ratio=ratio,
            iv_rank=iv_rank,
            liquidity=liq,
        )
        for position, (proposal, score, ratio, iv_rank, liq) in enumerate(scored, start=1)
    )
