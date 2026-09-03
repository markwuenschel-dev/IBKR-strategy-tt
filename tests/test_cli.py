"""The startup contract.

§7 requires that invalid configuration produces a named field, its supplied
value, the constraint it violated, a non-zero exit, zero orders, and no started
machinery. The last two are asserted structurally: if the process had got as far
as building anything, it would have created the SQLite file.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ibkr_trader.cli import EXIT_CONFIG_ERROR, is_market_open, main
from ibkr_trader.clock import FixedClock


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
