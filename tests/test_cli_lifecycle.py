"""Session and database lifecycle around a run.

`test_cli` covers the startup contract for bad configuration. This module
covers what happens to the two things the process opens -- a TWS session and a
SQLite connection -- when wiring fails, when the run finishes, and when closing
them fails. Every test here fails against the pre-fix entry point.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ibkr_trader import cli
from ibkr_trader.cli import EXIT_BROKER_ERROR, EXIT_OK, build_runner, main
from ibkr_trader.clock import FixedClock
from ibkr_trader.config import build_config
from ibkr_trader.errors import BrokerError

from .fakes import ACCOUNT

SCAN_TIME = datetime(2026, 1, 15, 14, 30, tzinfo=UTC)


class RecordingBroker:
    """Stands in for IBKRBroker, recording whether the session was closed."""

    def __init__(self, *args, disconnect_error: Exception | None = None, **kwargs) -> None:
        self.connected = False
        self.disconnects = 0
        self._disconnect_error = disconnect_error

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.disconnects += 1
        self.connected = False
        if self._disconnect_error is not None:
            raise self._disconnect_error

    @property
    def is_connected(self) -> bool:
        return self.connected

    @property
    def client(self):
        return object()


def config_for(tmp_path):
    db = tmp_path / "trader.sqlite3"
    return build_config(
        {
            "universe": ["AAPL"],
            "database_path": db.as_posix(),
            "ibkr": {"account": ACCOUNT},
        },
        source="<test>",
    )


# --- INT-007 -------------------------------------------------------------


def test_a_failure_while_wiring_closes_the_session_it_already_opened(tmp_path, monkeypatch):
    """connect() succeeds, then construction fails: the session must not leak.

    Everything between broker.connect() and the return was unprotected, and the
    only caller catches BrokerNotConnected -- so a SqliteStore that cannot open
    its file propagated out with a live TWS session and no reference to it.
    """
    broker = RecordingBroker()
    monkeypatch.setattr(cli, "IBKRBroker", lambda *a, **k: broker)
    monkeypatch.setattr(cli, "IBKRMarketData", lambda **k: object())

    def exploding_store(*args, **kwargs):
        raise OSError("cannot open the database file")

    monkeypatch.setattr(cli, "SqliteStore", exploding_store)

    with pytest.raises(OSError):
        build_runner(config_for(tmp_path), FixedClock(SCAN_TIME))

    assert broker.disconnects == 1, "the live session was leaked"
    assert not broker.connected


# --- INT-024 -------------------------------------------------------------


def test_a_completed_run_closes_the_database(tmp_path, monkeypatch):
    """SqliteStore.close() existed with zero callers; the connection leaked."""
    closed: list[bool] = []

    class RecordingStore:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def record(self, *args, **kwargs) -> None:
            pass

        def close(self) -> None:
            closed.append(True)

    broker = RecordingBroker()
    monkeypatch.setattr(cli, "IBKRBroker", lambda *a, **k: broker)
    monkeypatch.setattr(cli, "IBKRMarketData", lambda **k: object())
    monkeypatch.setattr(cli, "SqliteStore", RecordingStore)
    monkeypatch.setattr(cli, "Runner", lambda **k: _NullRunner())

    path = tmp_path / "trader.toml"
    db = tmp_path / "trader.sqlite3"
    path.write_text(
        f"universe = ['AAPL']\ndatabase_path = '{db.as_posix()}'\n"
        f"\n[ibkr]\naccount = '{ACCOUNT}'\n",
        encoding="utf-8",
    )

    exit_code = main(["run", "--config", str(path)])

    assert exit_code == EXIT_OK
    assert closed == [True], "the SQLite connection was never closed"
    assert broker.disconnects == 1


class _NullRunner:
    def run_once(self):
        return None

    def run_while(self, _predicate):
        return None


# --- INT-008 -------------------------------------------------------------


def test_a_failing_disconnect_does_not_destroy_the_documented_exit_code(
    tmp_path, monkeypatch, capsys
):
    """Teardown must not raise over the outcome of the run.

    The bare `finally: broker.disconnect()` let a BrokerError propagate out of
    main(), so `raise SystemExit(main())` never ran and the process exited 1 --
    a code the README does not document -- after a run that otherwise succeeded.
    """
    broker = RecordingBroker(disconnect_error=BrokerError("failed to close the session"))
    monkeypatch.setattr(cli, "IBKRBroker", lambda *a, **k: broker)
    monkeypatch.setattr(cli, "IBKRMarketData", lambda **k: object())
    monkeypatch.setattr(cli, "SqliteStore", lambda *a, **k: _NullStore())
    monkeypatch.setattr(cli, "Runner", lambda **k: _NullRunner())

    path = tmp_path / "trader.toml"
    db = tmp_path / "trader.sqlite3"
    path.write_text(
        f"universe = ['AAPL']\ndatabase_path = '{db.as_posix()}'\n"
        f"\n[ibkr]\naccount = '{ACCOUNT}'\n",
        encoding="utf-8",
    )

    exit_code = main(["run", "--config", str(path)])

    assert exit_code == EXIT_BROKER_ERROR
    assert "close" in capsys.readouterr().err.lower()


class _NullStore:
    def record(self, *args, **kwargs) -> None:
        pass

    def close(self) -> None:
        pass
