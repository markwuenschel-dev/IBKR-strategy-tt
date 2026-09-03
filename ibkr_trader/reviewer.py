"""The independent reviewer: one proposal in, one decision out.

This adapter is the second opinion required before any order is transmitted. It
has no lifecycle — no heartbeat, no liveness probe, no session lease, no retry
loop, no cache, no background thread. A proposal existing is the entire trigger,
and one proposal costs exactly one bounded request.

Two properties matter more than anything else here:

*Conservatism.* Only a well-formed JSON object carrying a real boolean ``true``
counts as approval. Every other shape — a timeout, a garbled body, prose around
the JSON, the string ``"true"``, a missing key — is refused. The system treats
:class:`~ibkr_trader.errors.ReviewTimeout` and
:class:`~ibkr_trader.errors.ReviewError` as "no trade", so failing loudly is
always cheaper than guessing generously.

*Purity of the payload.* :func:`build_review_payload` is a pure function with no
clock and no network, so what the reviewer was shown can be reconstructed from a
stored proposal alone. It reports the proposal's own numbers verbatim rather
than re-deriving risk from them: a reviewer fed a re-computed figure is checking
this module's arithmetic, not the algorithm's.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Protocol

import anthropic

from .clock import Clock
from .config import ReviewerConfig
from .errors import ReviewError, ReviewTimeout
from .models import (
    CONTRACT_MULTIPLIER,
    Portfolio,
    ProposalLeg,
    ReviewDecision,
    TradeProposal,
)

logger = logging.getLogger(__name__)

#: Instruction given to the reviewing model on every request.
#:
#: Stated as a refusal mandate rather than an advisory one: the model is told
#: what makes a trade unacceptable and that anything short of a clean JSON
#: object is a failed review, because the parser downstream will reject it
#: anyway. Kept as one frozen constant so the prompt is identical for every
#: proposal and the reviewer's behaviour cannot drift between symbols.
REVIEWER_SYSTEM_PROMPT = """You are an independent risk reviewer for a single, \
already-priced, defined-risk options credit spread. Another system selected this \
trade; your only job is to decide whether it should be allowed to reach the \
market. You are the last check before the order is transmitted.

You are given one JSON object describing the proposal, the measurements that \
selected it, and the account it would be placed in. Judge only that object. Do \
not assume facts that are not in it.

Reject the trade if any of the following is true:
- The stated maximum loss is wrong, missing, unbounded, or large relative to the \
account's net liquidation value or buying power.
- The position size is out of proportion to the account, or the buying-power \
effect would leave the account without meaningful room.
- Liquidity is poor: wide bid/ask spreads relative to the mid, thin or absent \
open interest or volume, or a leg with no two-sided market.
- The legs do not form the stated strategy, the credit or debit sign contradicts \
the stated posture, or the numbers are internally inconsistent.
- The stated selection criteria are not actually met by the measurements shown.
- Anything material is missing, contradictory, or implausible.

Approve only when the trade is coherent, the defined risk is genuinely bounded \
and appropriately sized, and the liquidity supports a reasonable fill. When in \
doubt, reject: declining a good trade costs an opportunity, approving a bad one \
costs capital.

Answer with ONLY a single JSON object and nothing else — no preamble, no \
explanation outside the object, no markdown:
{"approved": <true or false>, "reason": "<one or two sentences>"}

"approved" must be a JSON boolean, never a string. "reason" must be a string \
stating the specific fact that decided it."""

#: Matches a whole response wrapped in a markdown code fence.
#:
#: Tolerated because a fence is a formatting habit rather than a semantic
#: difference; nothing looser is accepted, so prose around the JSON still fails.
_FENCE_RE = re.compile(r"\A```(?:json)?\s*(?P<body>.*?)\s*```\Z", re.DOTALL)


def _leg_payload(leg: ProposalLeg) -> dict[str, Any]:
    """Render one leg with the liquidity the reviewer needs to judge the fill.

    ``spread_pct`` is the only derived number here: it is unambiguous arithmetic
    on the leg's own quote, and it is what a human would compute first. It is
    ``None`` — not infinity — when the mid is non-positive, so the payload stays
    valid JSON while still showing the market is unusable.
    """
    mid = (leg.bid + leg.ask) / Decimal(2)
    spread = leg.ask - leg.bid
    return {
        "action": leg.action.value,
        "right": leg.right.value,
        "strike": str(leg.strike),
        "expiry": leg.expiry.isoformat(),
        "ratio": leg.ratio,
        "bid": str(leg.bid),
        "ask": str(leg.ask),
        "mid": str(mid),
        "spread": str(spread),
        "spread_pct": float(spread / mid) if mid > 0 else None,
        "delta": leg.delta,
        "open_interest": leg.open_interest,
        "volume": leg.volume,
    }


def build_review_payload(proposal: TradeProposal, portfolio: Portfolio) -> dict[str, Any]:
    """Render exactly what the reviewer is allowed to see, as plain JSON types.

    Pure and deterministic: no clock, no network, no ambient state. The same
    proposal and portfolio always produce the same dict, so a stored decision can
    be re-explained later from the record alone.

    ``Decimal`` becomes ``str`` rather than ``float`` because the reviewer is
    being asked about money; a strike or credit that renders as ``3.4499999`` is
    a reason for it to distrust the whole payload. Dates become ISO strings.
    Dimensionless statistics (delta, IV rank, spread percentage) stay numeric,
    since that is what they are being compared against.

    Args:
        proposal: The fully-priced trade awaiting review.
        portfolio: Account state the trade would be placed into.

    Returns:
        A JSON-serializable dict. Money is stringified; nothing is rounded.
    """
    return {
        "proposal_id": proposal.proposal_id,
        "symbol": proposal.symbol,
        "strategy": proposal.strategy,
        "underlying_price": str(proposal.underlying_price),
        "expiration": proposal.expiry.isoformat(),
        "dte": proposal.dte,
        "created_at": proposal.created_at.isoformat(),
        "order": {
            "quantity": proposal.quantity,
            "contract_multiplier": str(CONTRACT_MULTIPLIER),
            "limit_price": str(proposal.limit_price),
            "credit_or_debit": "CREDIT" if proposal.is_credit else "DEBIT",
            "total_credit": str(proposal.total_credit),
        },
        "risk": {
            "max_profit": str(proposal.max_profit),
            "max_loss": str(proposal.max_loss),
            "buying_power_effect": str(proposal.buying_power_effect),
        },
        "legs": [_leg_payload(leg) for leg in proposal.legs],
        "volatility": {
            "iv_rank": proposal.iv_rank,
            "short_delta": proposal.short_delta,
        },
        "account": {
            "net_liquidation": str(portfolio.net_liquidation),
            "buying_power": str(portfolio.buying_power),
            "open_symbol_count": portfolio.open_symbol_count,
            "existing_positions_in_symbol": [
                {
                    "symbol": position.symbol,
                    "quantity": position.quantity,
                    "description": position.description,
                }
                for position in portfolio.positions_for(proposal.symbol)
            ],
        },
        "selection_criteria": dict(proposal.criteria),
    }


class _MessageCreator(Protocol):
    """The one SDK call this adapter makes."""

    def create(self, **kwargs: Any) -> Any:
        """Send one message request and return the response object."""
        ...


class _AnthropicClient(Protocol):
    """The whole client surface this adapter depends on.

    Narrow on purpose: a test stub only has to expose ``messages.create``, so
    every parsing and translation path can be exercised without a network.
    """

    @property
    def messages(self) -> _MessageCreator:
        """Messages resource."""
        ...


class ClaudeReviewer:
    """Independent reviewer backed by one Claude request per proposal.

    Satisfies :class:`~ibkr_trader.ports.Reviewer`. The client is injectable
    because every interesting behaviour of this class is in how it *interprets*
    a response, and that must be testable without a network or an API key.

    The clock is used for exactly one thing: stamping ``reviewed_at``. It is
    never used to time out, poll, or retry.
    """

    def __init__(
        self,
        config: ReviewerConfig,
        clock: Clock,
        client: _AnthropicClient | None = None,
    ) -> None:
        """Wire the reviewer.

        Args:
            config: Model, request timeout, and output ceiling.
            clock: Sole source of the ``reviewed_at`` stamp.
            client: Injected client. When omitted, a default client is built with
                ``max_retries=0`` so that one proposal provably means one request:
                the SDK's built-in retry would otherwise turn a rate-limited or
                5xx review into several unbudgeted calls.
        """
        self._config = config
        self._clock = clock
        self._client: _AnthropicClient = client or anthropic.Anthropic(
            max_retries=0, timeout=config.timeout_seconds
        )

    def review(self, proposal: TradeProposal, portfolio: Portfolio) -> ReviewDecision:
        """Return the verdict on exactly this proposal.

        One request, one decision, no fallback. Failure is never downgraded into
        a default answer, because a silent default would be indistinguishable
        from a real approval in the durable record.

        Raises:
            ReviewTimeout: no answer within ``config.timeout_seconds``.
            ReviewError: the transport failed, or the answer was not a strict
                JSON object with a boolean ``approved`` and a string ``reason``.
        """
        payload = build_review_payload(proposal, portfolio)
        message = self._request(payload, proposal.proposal_id)
        approved, reason = _parse_decision(
            _response_text(message, proposal.proposal_id), proposal.proposal_id
        )

        logger.info(
            "review complete: proposal=%s symbol=%s approved=%s model=%s",
            proposal.proposal_id,
            proposal.symbol,
            approved,
            self._config.model,
        )
        return ReviewDecision(
            approved=approved,
            reason=reason,
            reviewer_id=self._config.model,
            reviewed_at=self._clock.now(),
        )

    def _request(self, payload: Mapping[str, Any], proposal_id: str) -> Any:
        """Make the single bounded call, translating transport failure.

        Kept separate from parsing so that a :class:`ReviewError` raised by the
        parser cannot be caught and re-wrapped by this method's own translation
        clause, which would hide the real reason the answer was rejected.
        """
        try:
            return self._client.messages.create(
                model=self._config.model,
                max_tokens=self._config.max_tokens,
                system=REVIEWER_SYSTEM_PROMPT,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            "Review this trade proposal and answer with only the "
                            "JSON object described in your instructions.\n\n"
                            + json.dumps(payload, sort_keys=True, indent=2)
                        ),
                    }
                ],
                timeout=self._config.timeout_seconds,
            )
        except (anthropic.APITimeoutError, TimeoutError) as exc:
            logger.warning(
                "review timed out: proposal=%s model=%s timeout=%ss",
                proposal_id,
                self._config.model,
                self._config.timeout_seconds,
            )
            raise ReviewTimeout(
                f"reviewer did not answer within {self._config.timeout_seconds}s "
                f"for proposal {proposal_id}"
            ) from exc
        except Exception as exc:
            logger.warning(
                "review request failed: proposal=%s model=%s error=%s",
                proposal_id,
                self._config.model,
                exc.__class__.__name__,
            )
            raise ReviewError(
                f"reviewer request failed for proposal {proposal_id}: "
                f"{exc.__class__.__name__}: {exc}"
            ) from exc


def _response_text(message: Any, proposal_id: str) -> str:
    """Concatenate the text blocks of a response.

    A response may legitimately carry non-text blocks (thinking, for instance),
    so blocks are filtered by type rather than indexed positionally. A response
    with no text at all is a failed review, not an empty one.

    Raises:
        ReviewError: the response shape was unusable or carried no text.
    """
    try:
        blocks = list(message.content)
    except (AttributeError, TypeError) as exc:
        logger.warning("review response had no content: proposal=%s", proposal_id)
        raise ReviewError(
            f"reviewer response for proposal {proposal_id} had no content blocks"
        ) from exc

    text = "".join(block.text for block in blocks if getattr(block, "type", None) == "text")
    if not text.strip():
        logger.warning("review response had no text: proposal=%s", proposal_id)
        raise ReviewError(f"reviewer response for proposal {proposal_id} contained no text")
    return text


def _parse_decision(text: str, proposal_id: str) -> tuple[bool, str]:
    """Interpret the response, refusing anything that is not clearly a verdict.

    The tolerances are deliberately tiny — surrounding whitespace and a markdown
    fence — and everything else is a failure. In particular ``"true"``, ``1``,
    ``null``, a missing key, and valid JSON that is not an object are all
    rejected: an approval that was inferred rather than stated is the one bug
    that costs real money here.

    Returns:
        The verdict and the reviewer's stated reason.

    Raises:
        ReviewError: the answer was not a strict, complete JSON verdict.
    """
    stripped = text.strip()
    fenced = _FENCE_RE.match(stripped)
    if fenced is not None:
        stripped = fenced.group("body").strip()

    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("review answer was not JSON: proposal=%s answer=%r", proposal_id, text)
        raise ReviewError(
            f"reviewer answer for proposal {proposal_id} was not valid JSON"
        ) from exc

    if not isinstance(parsed, dict):
        logger.warning(
            "review answer was not an object: proposal=%s answer=%r", proposal_id, text
        )
        raise ReviewError(
            f"reviewer answer for proposal {proposal_id} was JSON "
            f"{type(parsed).__name__}, expected an object"
        )

    approved = parsed.get("approved")
    if not isinstance(approved, bool):
        logger.warning(
            "review answer had no boolean 'approved': proposal=%s answer=%r",
            proposal_id,
            text,
        )
        raise ReviewError(
            f"reviewer answer for proposal {proposal_id} did not contain a boolean "
            f"'approved' field (got {approved!r})"
        )

    reason = parsed.get("reason")
    if not isinstance(reason, str):
        logger.warning(
            "review answer had no string 'reason': proposal=%s answer=%r",
            proposal_id,
            text,
        )
        raise ReviewError(
            f"reviewer answer for proposal {proposal_id} did not contain a string "
            f"'reason' field (got {reason!r})"
        )

    return approved, reason
