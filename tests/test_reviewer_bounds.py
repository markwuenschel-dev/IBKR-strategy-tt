"""That the reviewer's request bounds come from configuration, and are usable.

The existing bounds test in `test_reviewer` asserts the literals 90.0 and 1024,
which are exactly the config defaults, so it passes whether the values are
threaded through or hardcoded. These tests use non-default values, which only
a genuinely configuration-driven request can satisfy.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ibkr_trader.clock import FixedClock
from ibkr_trader.config import build_config
from ibkr_trader.errors import ReviewError
from ibkr_trader.reviewer import ClaudeReviewer, _response_text

from .fakes import SCAN_TIME
from .test_reviewer import PORTFOLIO, StubClient, canonical_proposal

# --- INT-025 -------------------------------------------------------------


def test_the_request_carries_non_default_bounds():
    """Hardcoding today's defaults must not satisfy this assertion."""
    _, proposal = canonical_proposal()
    config = build_config(
        {
            "universe": ["AAPL"],
            "reviewer": {"timeout_seconds": 12.5, "max_tokens": 4096},
        }
    )
    client = StubClient(text='{"approved": true, "reason": "ok"}')
    reviewer = ClaudeReviewer(config.reviewer, FixedClock(SCAN_TIME), client=client)

    reviewer.review(proposal, PORTFOLIO)

    call = client.calls[0]
    assert call["timeout"] == 12.5
    assert call["max_tokens"] == 4096
    assert call["model"] == config.reviewer.model


# --- INT-020 -------------------------------------------------------------


def test_the_token_budget_leaves_room_for_an_adaptive_thinking_model():
    """1024 output tokens is not a safe budget for a thinking model.

    The configured model runs adaptive thinking whenever the request omits a
    `thinking` parameter, and thinking tokens are output tokens: they come out
    of the same `max_tokens` the JSON verdict must also fit inside. A budget
    that small risks every review truncating, which is a permanent
    all-reviews-fail mode rather than an occasional one.
    """
    config = build_config({"universe": ["AAPL"]})

    assert config.reviewer.max_tokens >= 4096


def test_a_truncated_response_is_reported_as_truncation():
    """`stop_reason` was never inspected, so a cut-off answer looked like junk.

    Without this the operator sees "response was not valid JSON" and has no way
    to know the real cause was the token budget.
    """
    truncated = SimpleNamespace(
        stop_reason="max_tokens",
        content=[SimpleNamespace(type="text", text='{"approved": true, "reas')],
    )

    with pytest.raises(ReviewError) as caught:
        _response_text(truncated, "proposal-1")

    assert "max_tokens" in str(caught.value)


def test_a_normal_stop_reason_is_not_treated_as_truncation():
    """An ordinary answer must still pass through untouched."""
    complete = SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text='{"approved": true}')],
    )

    assert _response_text(complete, "proposal-1") == '{"approved": true}'
