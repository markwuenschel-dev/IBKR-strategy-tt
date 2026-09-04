"""The reviewer must never turn confusion into approval.

The reviewer is the last check before real orders. Its parsing is therefore
deliberately unforgiving: approval requires an unambiguous JSON boolean, and
everything else — malformed output, a stringy "true", a missing field, an
apology, silence — is an error that blocks the trade.

These are cheap always-on invariants, and they are the ones worth having: a
permissive parser here would fail open, in the direction of placing orders.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from ibkr_trader.clock import FixedClock
from ibkr_trader.config import build_config
from ibkr_trader.errors import ReviewError, ReviewTimeout
from ibkr_trader.models import NoTrade, Portfolio
from ibkr_trader.reviewer import ClaudeReviewer, build_review_payload
from ibkr_trader.tastytrade import evaluate

from .fakes import SCAN_TIME, tradable_snapshot

PORTFOLIO = Portfolio(net_liquidation=Decimal(50_000), buying_power=Decimal(25_000))


def canonical_proposal():
    """The mission-test proposal, produced by the real algorithm."""
    config = build_config({"universe": ["AAPL"]})
    proposal = evaluate(
        symbol="AAPL",
        snapshot=tradable_snapshot("AAPL"),
        portfolio=PORTFOLIO,
        strategy=config.strategy,
        risk=config.risk,
        now=SCAN_TIME,
    )
    assert not isinstance(proposal, NoTrade), proposal
    return config, proposal


class StubClient:
    """Anthropic client stand-in returning one canned response or raising."""

    def __init__(self, text: str | None = None, error: Exception | None = None) -> None:
        self._text = text
        self._error = error
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self._text)])


def review_with(text: str | None = None, error: Exception | None = None):
    config, proposal = canonical_proposal()
    client = StubClient(text=text, error=error)
    reviewer = ClaudeReviewer(config.reviewer, FixedClock(SCAN_TIME), client=client)
    return reviewer, proposal, client


# --- the payload the reviewer is given ------------------------------------


def test_payload_carries_everything_needed_for_an_independent_decision():
    """A reviewer that cannot see the risk cannot judge it.

    Pins the §5 payload contract: without max loss, liquidity and sizing, the
    reviewer would be rubber-stamping rather than reviewing.
    """
    _, proposal = canonical_proposal()

    payload = build_review_payload(proposal, PORTFOLIO)
    flat = repr(payload)

    for required in ("AAPL", "185", "180", "1.75", "45"):
        assert required in flat, f"payload must mention {required}"

    # The payload must be JSON-serializable: it crosses a network boundary.
    import json

    json.dumps(payload)


def test_payload_is_pure_and_deterministic():
    """Same proposal in, identical payload out — no clock, no randomness."""
    _, proposal = canonical_proposal()
    assert build_review_payload(proposal, PORTFOLIO) == build_review_payload(
        proposal, PORTFOLIO
    )


# --- approval requires an unambiguous answer -------------------------------


def test_clean_approval_is_accepted():
    reviewer, proposal, client = review_with('{"approved": true, "reason": "ok"}')

    decision = reviewer.review(proposal, PORTFOLIO)

    assert decision.approved is True
    assert decision.reason == "ok"
    assert decision.reviewed_at == SCAN_TIME
    assert len(client.calls) == 1, "exactly one request per proposal"


def test_clean_rejection_is_accepted():
    reviewer, proposal, _ = review_with('{"approved": false, "reason": "too wide"}')

    decision = reviewer.review(proposal, PORTFOLIO)

    assert decision.approved is False
    assert decision.reason == "too wide"


def test_fenced_json_is_tolerated():
    """A ```json fence is the one formatting liberty allowed."""
    reviewer, proposal, _ = review_with('```json\n{"approved": true, "reason": "ok"}\n```')
    assert reviewer.review(proposal, PORTFOLIO).approved is True


@pytest.mark.parametrize(
    "response",
    [
        '{"approved": "true", "reason": "ok"}',  # string, not boolean
        '{"approved": 1, "reason": "ok"}',  # truthy int
        '{"approved": null, "reason": "ok"}',  # null
        '{"reason": "ok"}',  # missing field
        '{"approved": true}',  # missing reason
        "yes, this trade looks fine to me",  # prose
        'Sure! Here you go: {"approved": true}',  # prose-wrapped JSON
        '[{"approved": true, "reason": "ok"}]',  # array, not object
        "",  # empty
        "{",  # truncated
    ],
)
def test_anything_less_than_an_explicit_boolean_is_a_review_error(response):
    """Every ambiguous answer must fail closed.

    This is the single most important behaviour in the module: each of these
    inputs, if leniently parsed, would place a real order that no reviewer
    actually approved.
    """
    reviewer, proposal, _ = review_with(response)
    with pytest.raises(ReviewError):
        reviewer.review(proposal, PORTFOLIO)


def test_timeout_is_distinct_from_a_malformed_answer():
    """Silence and gibberish are different operational facts.

    The runner maps them to different outcomes, so conflating them would hide
    which one is happening.
    """
    reviewer, proposal, _ = review_with(error=TimeoutError("timed out"))
    with pytest.raises(ReviewTimeout):
        reviewer.review(proposal, PORTFOLIO)


def test_transport_failure_becomes_a_review_error_not_a_raw_sdk_exception():
    """No SDK exception may escape into the runner."""
    reviewer, proposal, _ = review_with(error=RuntimeError("connection reset"))
    with pytest.raises(ReviewError):
        reviewer.review(proposal, PORTFOLIO)


def test_reviewer_sends_exactly_one_request_even_when_it_fails():
    """No retry loop: one proposal gets one review attempt."""
    reviewer, proposal, client = review_with("not json at all")
    with pytest.raises(ReviewError):
        reviewer.review(proposal, PORTFOLIO)
    assert len(client.calls) == 1


def test_request_is_bounded_by_configured_timeout_and_tokens():
    """The single request carries the configured bounds.

    Asserted against the configuration, not against literals. Literals equal to
    today's defaults would pass just as happily if the implementation hardcoded
    them; `test_reviewer_bounds` drives the same request from a *non-default*
    config, which is what actually distinguishes the two.
    """
    config, _ = canonical_proposal()
    reviewer, proposal, client = review_with('{"approved": true, "reason": "ok"}')
    reviewer.review(proposal, PORTFOLIO)

    call = client.calls[0]
    assert call["timeout"] == config.reviewer.timeout_seconds
    assert call["max_tokens"] == config.reviewer.max_tokens
