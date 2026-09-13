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

Two backends implement the same port. :class:`ClaudeCodeReviewer` (the default)
runs the Claude Code CLI as a one-shot subprocess, so the review is billed to the
operator's Claude subscription and no API key exists on the machine.
:class:`ClaudeReviewer` calls the Anthropic API directly. They share the prompt,
the payload, and the verdict parser, so switching backends changes how the
answer is transported and nothing about what counts as an approval.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
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
    leg_payload,
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

    The derived figures are the leg's own, not a re-derivation of them: the
    reviewer is shown the same arithmetic the liquidity screen applied when it
    selected the contract. This used to recompute mid and spread here, and the
    two implementations disagreed on a dead book.
    """
    return leg_payload(leg, derived=True)


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

    A response cut off by the token budget is reported as exactly that. Without
    the check the truncated JSON surfaces as "not a valid verdict", which sends
    the operator looking at the prompt instead of at ``reviewer.max_tokens``.

    Raises:
        ReviewError: the response shape was unusable, carried no text, or was
            truncated before the model finished.
    """
    if getattr(message, "stop_reason", None) == "max_tokens":
        logger.warning(
            "review response truncated: proposal=%s stop_reason=max_tokens", proposal_id
        )
        raise ReviewError(
            f"reviewer response for proposal {proposal_id} was cut off by the token "
            f"budget (stop_reason=max_tokens); raise reviewer.max_tokens"
        )

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


#: The verdict shape handed to the CLI as ``--json-schema``.
#:
#: ``additionalProperties: false`` and both keys required, so the CLI's own
#: validation rejects the same shapes :func:`_verdict_from_object` rejects. The
#: parser still runs on what comes back: a schema the CLI enforces is an
#: assumption about the CLI, and approval must not rest on an assumption.
_VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "approved": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["approved", "reason"],
    "additionalProperties": False,
}

#: Environment variables removed from the CLI subprocess.
#:
#: The Claude Code CLI sets these in every process it spawns, and refuses to
#: start a session when it finds them, on the grounds that it is being run from
#: inside another Claude Code session. When this program is itself launched
#: from a Claude Code terminal the variables are inherited and every review
#: would fail before reaching the model. Stripping them says the truth: the
#: reviewer is an independent one-shot process, not a nested session.
_NESTED_SESSION_VARS = frozenset({"CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"})

#: Characters of the CLI's own message quoted in an error. Enough to say what
#: went wrong without pasting a whole transcript into the durable record.
_ERROR_EXCERPT_CHARS = 300

#: First line of the user turn, ahead of the JSON payload.
_CLI_USER_PREAMBLE = "Review this proposal and answer with the JSON object only."


def _excerpt(text: str | None) -> str:
    """The head of a CLI message, whitespace-normalised, for an error string."""
    return " ".join((text or "").split())[:_ERROR_EXCERPT_CHARS]


class ClaudeCodeReviewer:
    """Independent reviewer backed by one Claude Code CLI invocation per proposal.

    Satisfies :class:`~ibkr_trader.ports.Reviewer`. The CLI runs in headless
    print mode, isolated from everything a normal session would load: no
    settings, no ``CLAUDE.md``, no memory, no MCP servers, no tools, and no
    session persistence. The model sees the frozen system prompt and the JSON
    payload, nothing else, which is what makes a decision reconstructible from
    the stored proposal.

    Authentication is whatever ``claude`` is logged in as on this machine. That
    is the point of the backend: no API key is required or read.

    ``run`` is injectable because every interesting behaviour here is in how the
    process's output is *interpreted*, and that must be testable without
    spawning anything. The clock is used for exactly one thing: stamping
    ``reviewed_at``. It is never used to time out, poll, or retry.
    """

    def __init__(
        self,
        config: ReviewerConfig,
        clock: Clock,
        run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        """Wire the reviewer.

        Args:
            config: Command, model, and request timeout. ``max_tokens`` is not
                used: the CLI owns its own output budget.
            clock: Sole source of the ``reviewed_at`` stamp.
            run: Injected process runner with :func:`subprocess.run`'s
                signature. Defaults to :func:`subprocess.run` itself.
        """
        self._config = config
        self._clock = clock
        self._run = run or subprocess.run

    def review(self, proposal: TradeProposal, portfolio: Portfolio) -> ReviewDecision:
        """Return the verdict on exactly this proposal.

        One process, one decision, no fallback. Failure is never downgraded into
        a default answer, because a silent default would be indistinguishable
        from a real approval in the durable record.

        Raises:
            ReviewTimeout: no answer within ``config.timeout_seconds``.
            ReviewError: the command is not installed, the process failed, or
                the answer was not a strict JSON object with a boolean
                ``approved`` and a string ``reason``.
        """
        payload = build_review_payload(proposal, portfolio)
        completed = self._request(payload, proposal.proposal_id)
        output = _decode_cli_output(completed.stdout, proposal.proposal_id)
        approved, reason = _cli_verdict(output, proposal.proposal_id)

        logger.info(
            "review complete: proposal=%s symbol=%s approved=%s model=%s "
            "backend=claude-code num_turns=%s total_cost_usd=%s (list-price equivalent)",
            proposal.proposal_id,
            proposal.symbol,
            approved,
            self._config.model,
            output.get("num_turns"),
            output.get("total_cost_usd"),
        )
        return ReviewDecision(
            approved=approved,
            reason=reason,
            reviewer_id=f"claude-code/{self._config.model}",
            reviewed_at=self._clock.now(),
        )

    def _argv(self, executable: str, payload: Mapping[str, Any]) -> list[str]:
        """The exact command line, flag by flag.

        Every isolation flag is load-bearing:

        * ``--restricted``, ``--tools ""`` -- the model can take no action.
        * ``--setting-sources ""``, ``--strict-mcp-config`` -- nothing from the
          operator's own Claude Code configuration leaks into the review.
        * ``--no-session-persistence`` -- no transcript is written to disk.
        * ``--output-format json --json-schema`` -- the verdict arrives as a
          schema-validated object rather than free text.

        Two flags are deliberately absent. ``--bare`` would restrict
        authentication to an API key and skip the subscription login this
        backend exists to use. ``--max-turns 1`` would fail every review, since
        structured output is produced by an internal tool round-trip that the
        CLI reports as a second turn; the process timeout is the bound instead.
        """
        return [
            executable,
            "-p",
            "--restricted",
            "--strict-mcp-config",
            "--setting-sources",
            "",
            "--tools",
            "",
            "--no-session-persistence",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(_VERDICT_SCHEMA, separators=(",", ":")),
            "--system-prompt",
            REVIEWER_SYSTEM_PROMPT,
            "--model",
            self._config.model,
            _CLI_USER_PREAMBLE + "\n\n" + json.dumps(payload, sort_keys=True, indent=2),
        ]

    def _request(
        self, payload: Mapping[str, Any], proposal_id: str
    ) -> subprocess.CompletedProcess[str]:
        """Run the single bounded process, translating transport failure.

        Kept separate from parsing for the same reason as the API backend: a
        :class:`ReviewError` raised by the parser must not be caught and
        re-wrapped here, which would hide the real reason the answer was
        rejected.

        The command is resolved on every call rather than at construction so
        the error names the binary that was actually missing when the review
        ran, and so a reviewer constructed before ``claude`` was installed is
        not permanently broken.
        """
        command = self._config.command
        executable = shutil.which(command)
        if executable is None:
            logger.warning(
                "review command not found: proposal=%s command=%r", proposal_id, command
            )
            raise ReviewError(
                f"reviewer command {command!r} was not found on PATH for proposal "
                f"{proposal_id}; install the Claude Code CLI or set reviewer.command"
            )

        env = {k: v for k, v in os.environ.items() if k not in _NESTED_SESSION_VARS}
        try:
            completed = self._run(
                self._argv(executable, payload),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=env,
                timeout=self._config.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
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
        except OSError as exc:
            logger.warning(
                "review process could not start: proposal=%s command=%r error=%s",
                proposal_id,
                executable,
                exc.__class__.__name__,
            )
            raise ReviewError(
                f"reviewer process failed to start for proposal {proposal_id}: "
                f"{exc.__class__.__name__}: {exc}"
            ) from exc

        if completed.returncode != 0:
            logger.warning(
                "review process failed: proposal=%s returncode=%s stderr=%r",
                proposal_id,
                completed.returncode,
                _excerpt(completed.stderr),
            )
            raise ReviewError(
                f"reviewer process exited with status {completed.returncode} for "
                f"proposal {proposal_id}: {_excerpt(completed.stderr or completed.stdout)}"
            )
        return completed


def _decode_cli_output(stdout: str, proposal_id: str) -> dict[str, Any]:
    """Read the CLI's one JSON result object, refusing anything else.

    ``--output-format json`` promises a single object on stdout. A CLI that
    printed something else is a broken transport, not a verdict, so the parser
    never sees it. An ``is_error`` result is likewise refused here: its
    ``result`` field is the CLI's own message ("Not logged in", a model name it
    does not know), and quoting it is what lets the operator fix the cause.

    Raises:
        ReviewError: stdout was not a JSON object, or the CLI reported an error.
    """
    try:
        output = json.loads(stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning(
            "review CLI output was not JSON: proposal=%s stdout=%r",
            proposal_id,
            _excerpt(stdout),
        )
        raise ReviewError(
            f"reviewer CLI output for proposal {proposal_id} was not valid JSON: "
            f"{_excerpt(stdout)}"
        ) from exc

    if not isinstance(output, dict):
        logger.warning(
            "review CLI output was not an object: proposal=%s stdout=%r",
            proposal_id,
            _excerpt(stdout),
        )
        raise ReviewError(
            f"reviewer CLI output for proposal {proposal_id} was JSON "
            f"{type(output).__name__}, expected an object"
        )

    if output.get("is_error"):
        message = output.get("result")
        detail = _excerpt(message if isinstance(message, str) else json.dumps(message))
        logger.warning(
            "review CLI reported an error: proposal=%s result=%r", proposal_id, detail
        )
        raise ReviewError(
            f"reviewer CLI reported an error for proposal {proposal_id}: {detail}"
        )

    return output


def _cli_verdict(output: Mapping[str, Any], proposal_id: str) -> tuple[bool, str]:
    """Extract the verdict from a successful CLI result.

    ``structured_output`` is the schema-validated object and the expected path.
    When it is absent the assistant text in ``result`` is tried through the same
    strict parser the API backend uses, so a CLI that answered correctly but
    did not populate the structured field is still a review and not a mystery.
    Nothing looser than that: an absent ``result`` is a failed review.

    Raises:
        ReviewError: neither field carried a strict, complete JSON verdict.
    """
    structured = output.get("structured_output")
    if structured is not None:
        return _verdict_from_object(structured, proposal_id, json.dumps(structured))

    text = output.get("result")
    if not isinstance(text, str):
        logger.warning(
            "review CLI output carried no verdict: proposal=%s result=%r", proposal_id, text
        )
        raise ReviewError(
            f"reviewer CLI output for proposal {proposal_id} had no structured_output "
            f"and no text result"
        )
    return _parse_decision(text, proposal_id)


def _parse_decision(text: str, proposal_id: str) -> tuple[bool, str]:
    """Interpret a text response, refusing anything that is not clearly a verdict.

    The tolerances are deliberately tiny — surrounding whitespace and a markdown
    fence — and everything else is a failure. The shape check itself lives in
    :func:`_verdict_from_object`, which is shared with the CLI backend's
    structured path so there is exactly one definition of "approved".

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

    return _verdict_from_object(parsed, proposal_id, text)


def _verdict_from_object(parsed: Any, proposal_id: str, answer: str) -> tuple[bool, str]:
    """The one definition of a valid verdict, applied to already-decoded JSON.

    In particular ``"true"``, ``1``, ``null``, a missing key, and valid JSON
    that is not an object are all rejected: an approval that was inferred
    rather than stated is the one bug that costs real money here.

    Args:
        parsed: Whatever JSON decoding produced -- not assumed to be a dict.
        proposal_id: For the error message and log line.
        answer: The original answer, quoted verbatim in the warning so the
            record shows what was actually refused.

    Raises:
        ReviewError: the object was not a strict, complete JSON verdict.
    """
    if not isinstance(parsed, dict):
        logger.warning(
            "review answer was not an object: proposal=%s answer=%r", proposal_id, answer
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
            answer,
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
            answer,
        )
        raise ReviewError(
            f"reviewer answer for proposal {proposal_id} did not contain a string "
            f"'reason' field (got {reason!r})"
        )

    return approved, reason
