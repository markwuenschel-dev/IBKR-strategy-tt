"""The startup contract.

§7 requires that invalid configuration produces a named field, its supplied
value, the constraint it violated, a non-zero exit, zero orders, and no started
machinery. The last two are asserted structurally: if the process had got as far
as building anything, it would have created the SQLite file.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ibkr_trader import cli
from ibkr_trader.cli import EXIT_CONFIG_ERROR, EXIT_OK, build_reviewer, is_market_open, main
from ibkr_trader.clock import FixedClock
from ibkr_trader.config import build_config
from ibkr_trader.reviewer import ClaudeCodeReviewer, ClaudeReviewer

from .fakes import ACCOUNT, SCAN_TIME


def write_config(tmp_path, body: str):
    path = tmp_path / "trader.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_invalid_refresh_limit_exits_nonzero_before_starting_anything(tmp_path, capsys):
    """The regression, end to end through the real entry point."""
    db = tmp_path / "trader.sqlite3"
    path = write_config(
        tmp_path,
        f"universe = ['AAPL']\ndatabase_path = '{db.as_posix()}'\n\n"
        "[ibkr]\nrefresh_limit = 300\n",
    )

    exit_code = main(["run", "--config", str(path)])

    assert exit_code == EXIT_CONFIG_ERROR

    report = capsys.readouterr().err
    assert "ibkr.refresh_limit" in report
    assert "300" in report
    assert "200" in report

    # Nothing was constructed: no store, therefore no connection and no orders.
    assert not db.exists(), "no machinery may start when configuration is invalid"


def test_missing_config_file_exits_nonzero(tmp_path, capsys):
    exit_code = main(["run", "--config", str(tmp_path / "absent.toml")])
    assert exit_code == EXIT_CONFIG_ERROR
    assert "absent.toml" in capsys.readouterr().err


def test_contradictory_but_individually_valid_settings_are_rejected(tmp_path, capsys):
    """Each field is in range; only the combination is impossible.

    This is the shape of the original defect, caught at the entry point.
    """
    path = write_config(
        tmp_path,
        "universe = ['AAPL']\n\n[strategy]\nmin_dte = 30\nmax_dte = 40\ntarget_dte = 55\n",
    )
    assert main(["run", "--config", str(path)]) == EXIT_CONFIG_ERROR
    assert "target_dte" in capsys.readouterr().err


# --- the manage command ---------------------------------------------------


class _RecordingRunner:
    """Stands in for the runner; records which entry point the CLI chose."""

    calls: list[str] = []

    def __init__(self, **kwargs) -> None:
        # The composition root must hand the runner the manager it built,
        # or `run` would manage through one object and `manage` through none.
        assert kwargs["manager"] is not None

    def run_once(self):
        self.calls.append("run_once")

    def run_while(self, _predicate):
        self.calls.append("run_while")

    def manage_once(self):
        self.calls.append("manage_once")


class _SessionStub:
    def __init__(self, *_args, **_kwargs) -> None:
        self.disconnects = 0

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        self.disconnects += 1

    @property
    def client(self):
        return object()


def test_the_manage_command_works_the_book_without_scanning(tmp_path, monkeypatch):
    """``manage`` reaches ``Runner.manage_once`` and nothing else, then tears down."""
    session = _SessionStub()
    monkeypatch.setattr(cli, "IBKRBroker", lambda *_a, **_k: session)
    monkeypatch.setattr(cli, "IBKRMarketData", lambda **_k: object())
    monkeypatch.setattr(cli, "Runner", _RecordingRunner)
    _RecordingRunner.calls = []
    db = tmp_path / "trader.sqlite3"
    path = write_config(
        tmp_path,
        f"universe = ['AAPL']\ndatabase_path = '{db.as_posix()}'\n\n"
        f"[ibkr]\naccount = '{ACCOUNT}'\n",
    )

    exit_code = main(["manage", "--config", str(path)])

    assert exit_code == EXIT_OK
    assert _RecordingRunner.calls == ["manage_once"]
    assert session.disconnects == 1


def test_the_manage_command_is_advertised_and_typos_are_refused(capsys):
    """An operator can discover it from ``--help``; argparse refuses anything else."""
    with pytest.raises(SystemExit) as stop:
        main(["--help"])
    assert stop.value.code == 0
    assert "manage" in capsys.readouterr().out

    with pytest.raises(SystemExit) as refused:
        main(["manag"])
    assert refused.value.code == 2


# --- reviewer backend selection -------------------------------------------


def _reviewer_config(**reviewer):
    return build_config(
        {"universe": ["AAPL"], "ibkr": {"account": ACCOUNT}, "reviewer": reviewer}
    ).reviewer


def test_the_default_backend_is_the_claude_code_cli():
    reviewer = build_reviewer(_reviewer_config(), FixedClock(SCAN_TIME))
    assert isinstance(reviewer, ClaudeCodeReviewer)


def test_the_api_backend_is_selected_by_configuration():
    reviewer = build_reviewer(_reviewer_config(backend="anthropic_api"), FixedClock(SCAN_TIME))
    assert isinstance(reviewer, ClaudeReviewer)


# --- market hours ---------------------------------------------------------


def test_market_is_open_during_the_session():
    # 2026-01-15 is a Thursday. 15:00 UTC = 10:00 New York.
    clock = FixedClock(datetime(2026, 1, 15, 15, 0, tzinfo=UTC))
    assert is_market_open(clock) is True


def test_market_is_closed_before_the_open():
    # 13:00 UTC = 08:00 New York.
    clock = FixedClock(datetime(2026, 1, 15, 13, 0, tzinfo=UTC))
    assert is_market_open(clock) is False


def test_market_is_closed_at_the_weekend():
    # 2026-01-17 is a Saturday.
    clock = FixedClock(datetime(2026, 1, 17, 15, 0, tzinfo=UTC))
    assert is_market_open(clock) is False
