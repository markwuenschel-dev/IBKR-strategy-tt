"""What the store remembers, and what it refuses to open.

The management tables are the first rows the store reads back, so every
column has to survive the round trip exactly: a strike stored as ``185`` and
read as ``185.0`` would be a different contract at the venue.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from ibkr_trader.clock import FixedClock
from ibkr_trader.models import (
    LIVE_SPREAD_STATUSES,
    SUBMITTED_OUTCOMES,
    Action,
    ComboLeg,
    ComboOrder,
    ExecutionResult,
    Fill,
    ManagementAction,
    ManagementKind,
    OptionLeg,
    Outcome,
    ProposalLeg,
    Right,
    Spread,
    SpreadStatus,
    SymbolResult,
    Tif,
    TradeProposal,
)
from ibkr_trader.store import SCHEMA_VERSION, SchemaVersionError, SqliteStore

from .fakes import GOOD_EXPIRY, SCAN_TIME

RUN = "run-1"

#: The six history tables exactly as the store created them before it carried
#: a version marker. Spelled out rather than imported so the upgrade is tested
#: against what old files actually contain, not against the store's own idea
#: of its past.
_OLD_SCHEMA = """
CREATE TABLE runs (
    run_id TEXT PRIMARY KEY, declared_mode TEXT NOT NULL, verified_account TEXT NOT NULL,
    host TEXT NOT NULL, port INTEGER NOT NULL, started_at TEXT NOT NULL
);
CREATE TABLE symbol_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, symbol TEXT NOT NULL,
    outcome TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', proposal_id TEXT,
    recorded_at TEXT NOT NULL
);
CREATE TABLE trade_proposals (
    proposal_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, symbol TEXT NOT NULL,
    strategy TEXT NOT NULL, expiry TEXT NOT NULL, dte INTEGER NOT NULL,
    quantity INTEGER NOT NULL, limit_price TEXT NOT NULL, max_profit TEXT NOT NULL,
    max_loss TEXT NOT NULL, underlying_price TEXT NOT NULL, iv_rank REAL NOT NULL,
    short_delta REAL NOT NULL, buying_power TEXT NOT NULL, legs_json TEXT NOT NULL,
    criteria_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE reviews (
    proposal_id TEXT PRIMARY KEY, approved INTEGER NOT NULL, reason TEXT NOT NULL,
    reviewer_id TEXT, reviewed_at TEXT NOT NULL
);
CREATE TABLE orders (
    proposal_id TEXT PRIMARY KEY, order_ref TEXT NOT NULL, broker_order_id TEXT,
    outcome TEXT NOT NULL, message TEXT NOT NULL DEFAULT '', recorded_at TEXT NOT NULL
);
CREATE TABLE fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT, proposal_id TEXT NOT NULL,
    quantity INTEGER NOT NULL, price TEXT NOT NULL, filled_at TEXT NOT NULL
);
CREATE INDEX idx_attempts_run ON symbol_attempts (run_id);
CREATE INDEX idx_fills_proposal ON fills (proposal_id);
"""


# --- builders -------------------------------------------------------------


def _leg(action: Action, strike: str) -> ProposalLeg:
    return ProposalLeg(
        action=action,
        right=Right.PUT,
        strike=Decimal(strike),
        expiry=GOOD_EXPIRY,
        ratio=1,
        bid=Decimal("1.00"),
        ask=Decimal("1.10"),
        delta=-0.25,
        open_interest=500,
        volume=100,
    )


def _proposal(proposal_id: str, symbol: str = "AAPL") -> TradeProposal:
    return TradeProposal(
        symbol=symbol,
        strategy="put_credit_spread",
        expiry=GOOD_EXPIRY,
        dte=45,
        legs=(_leg(Action.SELL, "185.50"), _leg(Action.BUY, "180.50")),
        quantity=3,
        limit_price=Decimal("1.75"),
        max_profit=Decimal(525),
        max_loss=Decimal(975),
        underlying_price=Decimal(195),
        iv_rank=45.0,
        short_delta=-0.30,
        buying_power_effect=Decimal(975),
        criteria={},
        created_at=SCAN_TIME,
        proposal_id=proposal_id,
    )


def _fill(quantity: int, price: str) -> Fill:
    return Fill(quantity=quantity, price=Decimal(price), filled_at=SCAN_TIME)


def _record_opening(
    store: SqliteStore, proposal_id: str, outcome: Outcome, fills: tuple[Fill, ...] = ()
) -> TradeProposal:
    """Record one symbol attempt whose order reported ``outcome``."""
    proposal = _proposal(proposal_id)
    execution = ExecutionResult(
        outcome=outcome, order_ref=proposal_id, broker_order_id="b-1", fills=fills
    )
    store.record(
        SymbolResult(
            symbol=proposal.symbol,
            outcome=outcome,
            detail="",
            proposal=proposal,
            execution=execution,
        ),
        RUN,
    )
    return proposal


def _spread(
    spread_id: str = "p-1", status: SpreadStatus = SpreadStatus.OPEN, **overrides
) -> Spread:
    fields = {
        "spread_id": spread_id,
        "symbol": "AAPL",
        "expiry": GOOD_EXPIRY,
        "short_strike": Decimal("185.50"),
        "long_strike": Decimal("180.50"),
        "quantity": 3,
        "open_credit": Decimal("1.75"),
        "opened_at": SCAN_TIME,
        "status": status,
    }
    fields.update(overrides)
    return Spread(**fields)


def _option_leg(strike: str) -> OptionLeg:
    return OptionLeg("AAPL", GOOD_EXPIRY, Decimal(strike), Right.PUT)


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row[0] for row in rows}


def _user_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(SCAN_TIME)


@pytest.fixture
def store(tmp_path, clock):
    store = SqliteStore(tmp_path / "trader.sqlite3", clock=clock)
    yield store
    store.close()


# --- schema version -------------------------------------------------------


def test_a_fresh_file_is_stamped_with_the_current_version_and_both_new_tables(store):
    assert SCHEMA_VERSION == 2
    assert _user_version(store._conn) == 2
    assert {"spreads", "management_actions"} <= _tables(store._conn)


def test_a_pre_versioned_file_is_upgraded_in_place_and_keeps_its_rows(tmp_path):
    """Before the marker, every file read as version 0. Its history must survive."""
    path = tmp_path / "old.sqlite3"
    old = sqlite3.connect(path)
    old.executescript(_OLD_SCHEMA)
    old.execute(
        "INSERT INTO runs VALUES "
        "('r-old', 'paper', 'DU1', 'host', 4002, '2026-01-01T00:00:00+00:00')"
    )
    old.execute(
        "INSERT INTO symbol_attempts (run_id, symbol, outcome, detail, recorded_at) "
        "VALUES ('r-old', 'AAPL', 'NO_TRADE', 'kept', '2026-01-01T00:00:00+00:00')"
    )
    old.commit()
    assert _user_version(old) == 0
    old.close()

    store = SqliteStore(path)
    try:
        assert _user_version(store._conn) == 2
        assert {"spreads", "management_actions"} <= _tables(store._conn)
        assert [row["run_id"] for row in store.runs()] == ["r-old"]
        assert [row["detail"] for row in store.attempts()] == ["kept"]
        assert store.live_spreads() == ()
    finally:
        store.close()


def test_a_version_one_file_gains_the_management_tables(tmp_path):
    path = tmp_path / "v1.sqlite3"
    old = sqlite3.connect(path)
    old.executescript(_OLD_SCHEMA + "PRAGMA user_version = 1;")
    old.close()

    store = SqliteStore(path)
    try:
        assert _user_version(store._conn) == 2
        assert {"spreads", "management_actions"} <= _tables(store._conn)
    finally:
        store.close()


def test_a_file_from_a_newer_engine_is_refused_by_name_and_left_alone(tmp_path):
    path = tmp_path / "future.sqlite3"
    newer = sqlite3.connect(path)
    newer.execute("PRAGMA user_version = 3")
    newer.commit()
    newer.close()

    with pytest.raises(SchemaVersionError) as info:
        SqliteStore(path)

    message = str(info.value)
    assert re.search(re.escape(str(path)), message)
    assert "version 3" in message
    assert "version 2" in message

    check = sqlite3.connect(path)
    try:
        assert _user_version(check) == 3
        assert _tables(check) == set()
    finally:
        check.close()


# --- spreads --------------------------------------------------------------


def test_record_spread_inserts_then_replaces_and_updated_at_follows_the_clock(store, clock):
    store.record_spread(_spread("p-1"))
    first = store.spreads()
    assert [row["status"] for row in first] == [SpreadStatus.OPEN.value]
    assert first[0]["updated_at"] == SCAN_TIME.isoformat()

    clock.advance(600)
    resting = _spread("p-1", SpreadStatus.PROFIT_ORDER_RESTING, profit_order_ref="p-1:pt")
    store.record_spread(resting)

    rows = store.spreads()
    assert len(rows) == 1, "a replace must not add a second row for the same id"
    assert rows[0]["status"] == SpreadStatus.PROFIT_ORDER_RESTING.value
    assert rows[0]["profit_order_ref"] == "p-1:pt"
    assert rows[0]["updated_at"] == (SCAN_TIME + timedelta(seconds=600)).isoformat()
    assert store.spread("p-1") == resting


def test_live_spreads_returns_every_live_status_and_no_closed_one(store):
    """One spread per status, opened a minute apart, in reverse order of writing."""
    by_status = {}
    for offset, status in enumerate(reversed(list(SpreadStatus))):
        is_closed = status is SpreadStatus.CLOSED
        spread = _spread(
            f"s-{status.value}",
            status,
            opened_at=SCAN_TIME + timedelta(minutes=offset),
            closed_at=SCAN_TIME + timedelta(hours=1) if is_closed else None,
            close_price=Decimal("0.85") if is_closed else None,
            close_reason="profit target" if is_closed else "",
        )
        by_status[status] = spread
        store.record_spread(spread)

    live = store.live_spreads()

    assert {s.status for s in live} == set(LIVE_SPREAD_STATUSES)
    assert SpreadStatus.CLOSED not in {s.status for s in live}
    assert [s.opened_at for s in live] == sorted(s.opened_at for s in live)
    assert set(live) == {by_status[status] for status in LIVE_SPREAD_STATUSES}

    closed = store.spread(f"s-{SpreadStatus.CLOSED.value}")
    assert closed == by_status[SpreadStatus.CLOSED]
    assert closed.closed_at.tzinfo is not None
    assert str(closed.close_price) == "0.85"


def test_a_spread_rehydrates_with_exact_types(store):
    store.record_spread(_spread("p-1"))

    (spread,) = store.live_spreads()

    assert isinstance(spread, Spread)
    assert str(spread.short_strike) == "185.50"
    assert str(spread.long_strike) == "180.50"
    assert str(spread.open_credit) == "1.75"
    assert spread.expiry == GOOD_EXPIRY
    assert spread.opened_at == SCAN_TIME
    assert spread.opened_at.tzinfo is not None
    assert spread.status is SpreadStatus.OPEN
    assert spread.profit_order_ref is None
    assert spread.closing_order_ref is None
    assert spread.closed_at is None
    assert spread.close_price is None
    assert spread.close_reason == ""


def test_an_unknown_spread_id_reads_as_none(store):
    assert store.spread("never-recorded") is None


# --- unreconciled openings ------------------------------------------------


def test_a_working_order_with_no_spread_row_is_an_unreconciled_opening(store):
    proposal = _record_opening(store, "p-1", Outcome.WORKING)

    (opening,) = store.unreconciled_openings()

    assert opening.proposal_id == "p-1"
    assert opening.symbol == proposal.symbol
    assert opening.expiry == GOOD_EXPIRY
    assert str(opening.short_strike) == "185.50"
    assert str(opening.long_strike) == "180.50"
    assert opening.quantity == 3
    assert str(opening.limit_price) == "1.75"
    assert opening.filled_quantity == 0
    assert opening.fill_price is None
    assert opening.recorded_at == SCAN_TIME


def test_a_filled_order_reports_the_quantity_weighted_average_fill(store):
    _record_opening(store, "p-1", Outcome.FILLED, fills=(_fill(2, "1.70"), _fill(1, "1.85")))

    (opening,) = store.unreconciled_openings()

    assert opening.filled_quantity == 3
    # (2 x 1.70 + 1 x 1.85) / 3 = 5.25 / 3, exactly, in Decimal.
    assert opening.fill_price == Decimal("1.75")
    assert isinstance(opening.fill_price, Decimal)


@pytest.mark.parametrize(
    "outcome", sorted(SUBMITTED_OUTCOMES - {Outcome.BROKER_REJECTED}, key=lambda o: o.value)
)
def test_every_outcome_that_reached_the_venue_is_reported(store, outcome):
    _record_opening(store, "p-1", outcome)

    assert [o.proposal_id for o in store.unreconciled_openings()] == ["p-1"]


def test_a_broker_rejected_order_is_not_an_opening(store):
    _record_opening(store, "p-1", Outcome.BROKER_REJECTED)

    assert store.unreconciled_openings() == ()


def test_a_submission_that_never_left_the_process_is_not_an_opening(store):
    _record_opening(store, "p-1", Outcome.SUBMISSION_FAILED)

    assert store.unreconciled_openings() == ()


def test_an_opening_with_a_spread_row_is_reconciled(store):
    _record_opening(store, "p-1", Outcome.FILLED, fills=(_fill(3, "1.75"),))
    _record_opening(store, "p-2", Outcome.WORKING)
    store.record_spread(_spread("p-1"))

    assert [o.proposal_id for o in store.unreconciled_openings()] == ["p-2"]


def test_openings_are_ordered_by_when_the_order_was_recorded(store, clock):
    _record_opening(store, "p-late", Outcome.WORKING)
    clock.advance(-60)
    _record_opening(store, "p-early", Outcome.WORKING)

    assert [o.proposal_id for o in store.unreconciled_openings()] == ["p-early", "p-late"]


# --- management actions ---------------------------------------------------


def test_record_management_stores_every_column_and_round_trips_the_legs(store):
    order = ComboOrder(
        symbol="AAPL",
        legs=(
            ComboLeg(_option_leg("185.50"), Action.BUY),
            ComboLeg(_option_leg("180.50"), Action.SELL, ratio=2),
        ),
        quantity=3,
        limit_price=Decimal("-0.85"),
        tif=Tif.GTC,
        order_ref="p-1:pt",
        purpose="PROFIT_TARGET",
    )
    execution = ExecutionResult(
        outcome=Outcome.WORKING, order_ref="p-1:pt", broker_order_id="ib-77", message="ok"
    )
    action = ManagementAction(
        symbol="AAPL",
        kind=ManagementKind.PROFIT_TARGET_PLACED,
        detail="resting at 0.85",
        spread_id="p-1",
        order=order,
        execution=execution,
    )

    store.record_management(action, RUN)

    (row,) = store.management_actions(RUN)
    assert row["id"] == 1
    assert row["run_id"] == RUN
    assert row["spread_id"] == "p-1"
    assert row["symbol"] == "AAPL"
    assert row["kind"] == "PROFIT_TARGET_PLACED"
    assert row["detail"] == "resting at 0.85"
    assert row["order_ref"] == "p-1:pt"
    assert row["purpose"] == "PROFIT_TARGET"
    assert row["tif"] == "GTC"
    assert row["limit_price"] == "-0.85"
    assert row["quantity"] == 3
    assert json.loads(row["legs_json"]) == [
        {
            "symbol": "AAPL",
            "expiry": GOOD_EXPIRY.isoformat(),
            "strike": "185.50",
            "right": "P",
            "action": "BUY",
            "ratio": 1,
        },
        {
            "symbol": "AAPL",
            "expiry": GOOD_EXPIRY.isoformat(),
            "strike": "180.50",
            "right": "P",
            "action": "SELL",
            "ratio": 2,
        },
    ]
    assert row["broker_order_id"] == "ib-77"
    assert row["execution_outcome"] == "WORKING"
    assert row["execution_message"] == "ok"
    assert row["recorded_at"] == SCAN_TIME.isoformat()
    assert store.management_actions("some-other-run") == []


def test_an_observation_without_an_order_stores_nulls_for_the_order_columns(store):
    action = ManagementAction(
        symbol="MSFT",
        kind=ManagementKind.UNMANAGED_POSITION,
        detail="held legs match no spread",
    )

    store.record_management(action, RUN)

    (row,) = store.management_actions()
    assert row["spread_id"] is None
    assert row["kind"] == "UNMANAGED_POSITION"
    for column in (
        "order_ref",
        "purpose",
        "tif",
        "limit_price",
        "quantity",
        "legs_json",
        "broker_order_id",
        "execution_outcome",
        "execution_message",
    ):
        assert row[column] is None, column


def test_management_rows_keep_their_order_of_recording(store, clock):
    for kind in (ManagementKind.RECONCILED_FILL, ManagementKind.PROFIT_TARGET_PLACED):
        store.record_management(ManagementAction("AAPL", kind, "", spread_id="p-1"), RUN)
        clock.advance(1)

    rows = store.management_actions()
    assert [row["kind"] for row in rows] == ["RECONCILED_FILL", "PROFIT_TARGET_PLACED"]
    assert [row["recorded_at"] for row in rows] == [
        SCAN_TIME.isoformat(),
        (SCAN_TIME + timedelta(seconds=1)).isoformat(),
    ]


def test_a_storage_failure_propagates_unchanged(tmp_path):
    store = SqliteStore(tmp_path / "trader.sqlite3", clock=FixedClock(SCAN_TIME))
    store.close()
    action = ManagementAction("AAPL", ManagementKind.ERROR, "boom")

    with pytest.raises(sqlite3.ProgrammingError):
        store.record_management(action, RUN)
    with pytest.raises(sqlite3.ProgrammingError):
        store.record_spread(_spread())


def test_timestamps_are_stored_in_utc_iso_form(store):
    store.record_spread(_spread("p-1", opened_at=datetime(2026, 1, 15, 14, 30, tzinfo=UTC)))

    assert store.spreads()[0]["opened_at"] == "2026-01-15T14:30:00+00:00"
