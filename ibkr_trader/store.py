"""Durable record of what the system did.

Five small tables, one row per fact. This is a record of history, not runtime
state: nothing here is read back to decide what the runner does next, and losing
the file would cost the audit trail, not the system's ability to operate. That
is deliberate — authoritative mutable cross-process state is what the previous
architecture died of.

Decimals are stored as TEXT so a price round-trips exactly. Timestamps are
stored as ISO-8601 strings in UTC.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .clock import Clock, SystemClock
from .models import SymbolResult

_SCHEMA = """
CREATE TABLE IF NOT EXISTS symbol_attempts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT    NOT NULL,
    symbol      TEXT    NOT NULL,
    outcome     TEXT    NOT NULL,
    detail      TEXT    NOT NULL DEFAULT '',
    proposal_id TEXT,
    recorded_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS trade_proposals (
    proposal_id      TEXT PRIMARY KEY,
    run_id           TEXT NOT NULL,
    symbol           TEXT NOT NULL,
    strategy         TEXT NOT NULL,
    expiry           TEXT NOT NULL,
    dte              INTEGER NOT NULL,
    quantity         INTEGER NOT NULL,
    limit_price      TEXT NOT NULL,
    max_profit       TEXT NOT NULL,
    max_loss         TEXT NOT NULL,
    underlying_price TEXT NOT NULL,
    iv_rank          REAL NOT NULL,
    short_delta      REAL NOT NULL,
    buying_power     TEXT NOT NULL,
    legs_json        TEXT NOT NULL,
    criteria_json    TEXT NOT NULL,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reviews (
    proposal_id TEXT PRIMARY KEY,
    approved    INTEGER NOT NULL,
    reason      TEXT NOT NULL,
    reviewer_id TEXT,
    reviewed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    proposal_id     TEXT PRIMARY KEY,
    order_ref       TEXT NOT NULL,
    broker_order_id TEXT,
    outcome         TEXT NOT NULL,
    message         TEXT NOT NULL DEFAULT '',
    recorded_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fills (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id TEXT NOT NULL,
    quantity    INTEGER NOT NULL,
    price       TEXT NOT NULL,
    filled_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_attempts_run ON symbol_attempts (run_id);
CREATE INDEX IF NOT EXISTS idx_fills_proposal ON fills (proposal_id);
"""


class SqliteStore:
    """SQLite-backed :class:`~ibkr_trader.ports.Store`."""

    def __init__(self, path: str | Path, clock: Clock | None = None) -> None:
        self._clock = clock or SystemClock()
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        """Close the underlying connection."""
        self._conn.close()

    def record(self, result: SymbolResult, run_id: str) -> None:
        """Persist everything known about one symbol attempt, atomically.

        Written in a single transaction so a proposal never exists in the record
        without the attempt that produced it.
        """
        proposal = result.proposal
        review = result.review
        execution = result.execution
        now = self._clock.now().isoformat()

        with self._conn:
            self._conn.execute(
                "INSERT INTO symbol_attempts "
                "(run_id, symbol, outcome, detail, proposal_id, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    result.symbol,
                    result.outcome.value,
                    result.detail,
                    proposal.proposal_id if proposal else None,
                    now,
                ),
            )

            if proposal is not None:
                self._conn.execute(
                    "INSERT OR REPLACE INTO trade_proposals ("
                    "proposal_id, run_id, symbol, strategy, expiry, dte, quantity, "
                    "limit_price, max_profit, max_loss, underlying_price, iv_rank, "
                    "short_delta, buying_power, legs_json, criteria_json, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        proposal.proposal_id,
                        run_id,
                        proposal.symbol,
                        proposal.strategy,
                        proposal.expiry.isoformat(),
                        proposal.dte,
                        proposal.quantity,
                        str(proposal.limit_price),
                        str(proposal.max_profit),
                        str(proposal.max_loss),
                        str(proposal.underlying_price),
                        proposal.iv_rank,
                        proposal.short_delta,
                        str(proposal.buying_power_effect),
                        json.dumps(_legs_as_json(proposal)),
                        json.dumps(dict(proposal.criteria)),
                        proposal.created_at.isoformat(),
                    ),
                )

            if proposal is not None and review is not None:
                self._conn.execute(
                    "INSERT OR REPLACE INTO reviews "
                    "(proposal_id, approved, reason, reviewer_id, reviewed_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        proposal.proposal_id,
                        1 if review.approved else 0,
                        review.reason,
                        review.reviewer_id,
                        review.reviewed_at.isoformat(),
                    ),
                )

            if proposal is not None and execution is not None:
                self._conn.execute(
                    "INSERT OR REPLACE INTO orders "
                    "(proposal_id, order_ref, broker_order_id, outcome, message, recorded_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        proposal.proposal_id,
                        execution.order_ref,
                        execution.broker_order_id,
                        execution.outcome.value,
                        execution.message,
                        now,
                    ),
                )
                for fill in execution.fills:
                    self._conn.execute(
                        "INSERT INTO fills (proposal_id, quantity, price, filled_at) "
                        "VALUES (?, ?, ?, ?)",
                        (
                            proposal.proposal_id,
                            fill.quantity,
                            str(fill.price),
                            fill.filled_at.isoformat(),
                        ),
                    )

    # --- read helpers, for operators and tests ---------------------------

    def attempts(self, run_id: str | None = None) -> list[dict[str, Any]]:
        """Symbol attempts, oldest first, optionally scoped to one run."""
        if run_id is None:
            return self._query("SELECT * FROM symbol_attempts ORDER BY id")
        return self._query(
            "SELECT * FROM symbol_attempts WHERE run_id = ? ORDER BY id", (run_id,)
        )

    def proposals(self) -> list[dict[str, Any]]:
        """All recorded trade proposals."""
        return self._query("SELECT * FROM trade_proposals ORDER BY created_at")

    def reviews(self) -> list[dict[str, Any]]:
        """All recorded reviewer decisions."""
        return self._query("SELECT * FROM reviews ORDER BY reviewed_at")

    def orders(self) -> list[dict[str, Any]]:
        """All recorded broker submissions."""
        return self._query("SELECT * FROM orders ORDER BY recorded_at")

    def fills(self) -> list[dict[str, Any]]:
        """All recorded fills."""
        return self._query("SELECT * FROM fills ORDER BY id")

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return [dict(row) for row in self._conn.execute(sql, params).fetchall()]


def _legs_as_json(proposal) -> list[dict[str, Any]]:
    """Serialize proposal legs, preserving exact decimal prices as strings."""
    return [
        {
            "action": leg.action.value,
            "right": leg.right.value,
            "strike": str(leg.strike),
            "expiry": leg.expiry.isoformat(),
            "ratio": leg.ratio,
            "bid": str(leg.bid),
            "ask": str(leg.ask),
            "delta": leg.delta,
            "open_interest": leg.open_interest,
            "volume": leg.volume,
        }
        for leg in proposal.legs
    ]
