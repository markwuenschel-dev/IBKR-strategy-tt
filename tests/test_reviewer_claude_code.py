"""The Claude Code CLI backend: the process is bounded, isolated, and disbelieved.

Everything here runs without spawning anything. A fake ``run`` records the
exact argv and keyword arguments the reviewer would hand to ``subprocess.run``
and returns a canned ``CompletedProcess``, so the tests can pin the command
line flag by flag and hold the output interpretation to the same fail-closed
bar as the API backend.

The command is ``sys.executable`` throughout: ``shutil.which`` resolves an
absolute path to an existing file, so the tests do not depend on ``claude``
being installed wherever they run.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys

import pytest

from ibkr_trader.clock import FixedClock
from ibkr_trader.config import build_config
from ibkr_trader.errors import ReviewError, ReviewTimeout
from ibkr_trader.reviewer import REVIEWER_SYSTEM_PROMPT, ClaudeCodeReviewer

from .fakes import ACCOUNT, SCAN_TIME
from .test_reviewer import MALFORMED_ANSWERS, PORTFOLIO, canonical_proposal

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {"approved": {"type": "boolean"}, "reason": {"type": "string"}},
    "required": ["approved", "reason"],
    "additionalProperties": False,
}


def cli_output(**fields) -> str:
    """One ``--output-format json`` result object, as the CLI prints it."""
    base = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "num_turns": 2,
        "total_cost_usd": 0.0123,
        "session_id": "00000000-0000-0000-0000-000000000000",
        "result": "",
    }
    return json.dumps({**base, **fields})


def approved_output(reason: str = "ok") -> str:
    verdict = {"approved": True, "reason": reason}
    return cli_output(result=json.dumps(verdict), structured_output=verdict)


class FakeRun:
    """``subprocess.run`` stand-in: records the call, returns or raises once."""

    def __init__(
        self,
        stdout: str = "",
        returncode: int = 0,
        stderr: str = "",
        error: Exception | None = None,
    ) -> None:
        self._stdout = stdout
        self._returncode = returncode
        self._stderr = stderr
        self._error = error
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if self._error is not None:
            raise self._error
        return subprocess.CompletedProcess(argv, self._returncode, self._stdout, self._stderr)


def reviewer_config(**overrides):
    reviewer = {"command": sys.executable, **overrides}
    return build_config(
        {"universe": ["AAPL"], "ibkr": {"account": ACCOUNT}, "reviewer": reviewer}
    ).reviewer


def review_with(run: FakeRun, **overrides):
    _, proposal = canonical_proposal()
    reviewer = ClaudeCodeReviewer(reviewer_config(**overrides), FixedClock(SCAN_TIME), run=run)
    return reviewer, proposal


def flag_value(argv: list[str], flag: str) -> str:
    """The argument following ``flag``; fails if the flag is absent."""
    assert flag in argv, f"{flag} missing from {argv[:12]}..."
    return argv[argv.index(flag) + 1]


# --- the command line -----------------------------------------------------


def test_argv_carries_every_isolation_flag_and_no_forbidden_one():
    """The verified invocation, flag by flag.

    ``--bare`` is forbidden because it restricts authentication to an API key,
    which defeats the backend. ``--max-turns`` is forbidden because structured
    output costs an internal second turn; a cap of one fails every review.
    """
    run = FakeRun(approved_output())
    reviewer, proposal = review_with(run)

    reviewer.review(proposal, PORTFOLIO)

    (argv, _), *_ = run.calls
    assert argv[0] == shutil.which(sys.executable)
    assert "-p" in argv
    for flag in ("--restricted", "--strict-mcp-config", "--no-session-persistence"):
        assert flag in argv, flag
    assert flag_value(argv, "--setting-sources") == ""
    assert flag_value(argv, "--tools") == ""
    assert flag_value(argv, "--output-format") == "json"
    assert json.loads(flag_value(argv, "--json-schema")) == VERDICT_SCHEMA
    assert flag_value(argv, "--system-prompt") == REVIEWER_SYSTEM_PROMPT
    assert "--bare" not in argv
    assert "--max-turns" not in argv


def test_argv_honours_the_configured_model_and_command():
    run = FakeRun(approved_output())
    reviewer, proposal = review_with(run, model="claude-opus-5")

    reviewer.review(proposal, PORTFOLIO)

    (argv, _), *_ = run.calls
    assert flag_value(argv, "--model") == "claude-opus-5"
    assert argv[0] == shutil.which(sys.executable)


def test_the_user_prompt_is_the_payload_behind_one_instruction_line():
    """The positional argument is the review payload, not a summary of it."""
    run = FakeRun(approved_output())
    reviewer, proposal = review_with(run)

    reviewer.review(proposal, PORTFOLIO)

    (argv, _), *_ = run.calls
    prompt = argv[-1]
    first_line, _, body = prompt.partition("\n\n")
    assert first_line == "Review this proposal and answer with the JSON object only."
    payload = json.loads(body)
    assert payload["proposal_id"] == proposal.proposal_id
    assert payload["symbol"] == "AAPL"


# --- the process environment ---------------------------------------------


def test_the_nested_session_guard_variables_are_stripped_and_nothing_else(monkeypatch):
    """Inherited from a Claude Code terminal, these make the CLI refuse to run."""
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.setenv("IBKR_TRADER_TEST_MARKER", "kept")
    run = FakeRun(approved_output())
    reviewer, proposal = review_with(run)

    reviewer.review(proposal, PORTFOLIO)

    (_, kwargs), *_ = run.calls
    env = kwargs["env"]
    assert "CLAUDECODE" not in env
    assert "CLAUDE_CODE_ENTRYPOINT" not in env
    assert env["IBKR_TRADER_TEST_MARKER"] == "kept"
    assert "PATH" in env


def test_stdin_is_closed_and_output_is_captured_as_utf8_text():
    run = FakeRun(approved_output())
    reviewer, proposal = review_with(run)

    reviewer.review(proposal, PORTFOLIO)

    (_, kwargs), *_ = run.calls
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["encoding"] == "utf-8"


def test_the_process_timeout_is_the_configured_one():
    """A non-default value, so a hardcoded 90 cannot satisfy this."""
    run = FakeRun(approved_output())
    reviewer, proposal = review_with(run, timeout_seconds=12.5)

    reviewer.review(proposal, PORTFOLIO)

    (_, kwargs), *_ = run.calls
    assert kwargs["timeout"] == 12.5


# --- interpreting the result ---------------------------------------------


def test_structured_approval_is_accepted():
    run = FakeRun(approved_output("bounded and liquid"))
    reviewer, proposal = review_with(run, model="claude-sonnet-5")

    decision = reviewer.review(proposal, PORTFOLIO)

    assert decision.approved is True
    assert decision.reason == "bounded and liquid"
    assert decision.reviewer_id == "claude-code/claude-sonnet-5"
    assert decision.reviewed_at == SCAN_TIME
    assert len(run.calls) == 1, "exactly one process per proposal"


def test_structured_rejection_is_accepted():
    verdict = {"approved": False, "reason": "too wide"}
    run = FakeRun(cli_output(result=json.dumps(verdict), structured_output=verdict))
    reviewer, proposal = review_with(run)

    decision = reviewer.review(proposal, PORTFOLIO)

    assert decision.approved is False
    assert decision.reason == "too wide"


def test_a_missing_structured_output_falls_back_to_the_text_result():
    """A correct answer in ``result`` alone is still a review."""
    run = FakeRun(cli_output(result='{"approved": true, "reason": "ok"}'))
    reviewer, proposal = review_with(run)

    assert reviewer.review(proposal, PORTFOLIO).approved is True


def test_a_cli_error_is_a_review_error_quoting_the_cli_message():
    """The failure ``--bare`` produces, and the one a logged-out machine produces."""
    run = FakeRun(cli_output(is_error=True, result="Not logged in · Please run /login"))
    reviewer, proposal = review_with(run)

    with pytest.raises(ReviewError) as caught:
        reviewer.review(proposal, PORTFOLIO)

    assert "Not logged in" in str(caught.value)
    assert proposal.proposal_id in str(caught.value)


def test_a_non_zero_exit_is_a_review_error_quoting_stderr():
    run = FakeRun(stdout="", returncode=1, stderr="error: unknown option '--frobnicate'")
    reviewer, proposal = review_with(run)

    with pytest.raises(ReviewError) as caught:
        reviewer.review(proposal, PORTFOLIO)

    assert "--frobnicate" in str(caught.value)
    assert proposal.proposal_id in str(caught.value)


def test_a_timeout_is_a_review_timeout_not_a_review_error():
    """Silence and gibberish are different operational facts."""
    run = FakeRun(error=subprocess.TimeoutExpired(cmd=["claude"], timeout=12.5))
    reviewer, proposal = review_with(run, timeout_seconds=12.5)

    with pytest.raises(ReviewTimeout):
        reviewer.review(proposal, PORTFOLIO)


def test_non_json_stdout_is_a_review_error():
    run = FakeRun(stdout="Welcome to Claude Code!\n")
    reviewer, proposal = review_with(run)

    with pytest.raises(ReviewError):
        reviewer.review(proposal, PORTFOLIO)


def test_a_json_result_that_is_not_an_object_is_a_review_error():
    run = FakeRun(stdout='[{"approved": true, "reason": "ok"}]')
    reviewer, proposal = review_with(run)

    with pytest.raises(ReviewError):
        reviewer.review(proposal, PORTFOLIO)


def test_a_result_with_neither_verdict_field_is_a_review_error():
    run = FakeRun(cli_output(result=None))
    reviewer, proposal = review_with(run)

    with pytest.raises(ReviewError):
        reviewer.review(proposal, PORTFOLIO)


@pytest.mark.parametrize("response", MALFORMED_ANSWERS)
def test_malformed_text_results_fail_closed_through_the_shared_parser(response):
    """The API backend's list, verbatim, applied to the CLI's text fallback."""
    run = FakeRun(cli_output(result=response))
    reviewer, proposal = review_with(run)

    with pytest.raises(ReviewError):
        reviewer.review(proposal, PORTFOLIO)


@pytest.mark.parametrize(
    "structured",
    [
        {"approved": "true", "reason": "ok"},
        {"approved": 1, "reason": "ok"},
        {"reason": "ok"},
        {"approved": True},
        [{"approved": True, "reason": "ok"}],
    ],
)
def test_a_malformed_structured_output_is_not_trusted_either(structured):
    """The schema is the CLI's promise; the parser is ours."""
    run = FakeRun(cli_output(result=json.dumps(structured), structured_output=structured))
    reviewer, proposal = review_with(run)

    with pytest.raises(ReviewError):
        reviewer.review(proposal, PORTFOLIO)


def test_a_missing_command_is_a_review_error_naming_it():
    run = FakeRun(approved_output())
    reviewer, proposal = review_with(run, command="ibkr-trader-no-such-command-xyz")

    with pytest.raises(ReviewError) as caught:
        reviewer.review(proposal, PORTFOLIO)

    assert "ibkr-trader-no-such-command-xyz" in str(caught.value)
    assert run.calls == [], "nothing may be spawned when the command is absent"


def test_exactly_one_process_even_when_the_answer_is_refused():
    """No retry loop: one proposal gets one review attempt."""
    run = FakeRun(cli_output(result="not json at all"))
    reviewer, proposal = review_with(run)

    with pytest.raises(ReviewError):
        reviewer.review(proposal, PORTFOLIO)
    assert len(run.calls) == 1
