"""Durable record of what the system did, and the one thing it remembers.

Eight small tables, one row per fact. Six of them are a record of history and
nothing more: the trading pass writes them and never reads them back to decide
what it does next. That sentence used to cover the whole file, and it no
longer does. The two management tables are read.

``spreads`` is read on every management pass because a spread's original
credit -- the figure every profit target and every roll is measured against --
exists nowhere else once the opening order has filled. The venue reports the
legs it holds and an average cost it is free to leave at zero; it does not
remember what this engine was paid to put them on. ``orders`` joined to
``trade_proposals`` is read once more, for opening orders that reached the
venue and have no spread row yet, so a fill the submission window did not see
is reconciled instead of forgotten. The trading pass itself still reads
nothing.

One write is deliberately on the critical path: a pass must record the
verified account it is about to trade before it may process a symbol. Losing
the file therefore stops new passes rather than permitting orders whose
account provenance cannot be reconstructed.

The schema carries a version in ``PRAGMA user_version``. It did not, and a file
of any older shape was accepted silently: every statement was ``CREATE TABLE IF
NOT EXISTS``, so a database missing a column would open cleanly and fail on the
first write that named it, and a database written by a newer engine would open
cleanly and be misread. Now an unversioned or older file is upgraded in place
and a newer one is refused by name.

Decimals are stored as TEXT so a price round-trips exactly. Timestamps are
stored as ISO-8601 strings in UTC.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, TypeVar

from .clock import Clock, SystemClock
from .errors import TraderError
from .models import (
    LIVE_SPREAD_STATUSES,
    SUBMITTED_OUTCOMES,
    Action,
    ComboLeg,
    ManagementAction,
    OpeningOrder,
    Outcome,
    Spread,
    SpreadStatus,
    SymbolResult,
    leg_payload,
)

#: The schema version this engine writes. Bump it whenever a statement is
#: added to :data:`_SCHEMA_V2` or a later block, and add the upgrade step in
#: :func:`_apply_schema`.
SCHEMA_VERSION = 3

#: The six history tables. Version 1 of the schema, though no engine ever
#: stamped that number: files of this shape carry ``user_version`` 0.
_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS runs (
    run_id           TEXT PRIMARY KEY,
    declared_mode    TEXT NOT NULL,
    verified_account TEXT NOT NULL,
    host             TEXT NOT NULL,
    port             INTEGER NOT NULL,
    started_at       TEXT NOT NULL
);

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

#: The two management tables, added in version 2. ``spreads`` is one row per
#: spread the engine opened, replaced in place as its status moves;
#: ``management_actions`` is one row per thing the manager did or observed.
_SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS spreads (
    spread_id         TEXT PRIMARY KEY,
    symbol            TEXT NOT NULL,
    expiry            TEXT NOT NULL,
    short_strike      TEXT NOT NULL,
    long_strike       TEXT NOT NULL,
    quantity          INTEGER NOT NULL,
    open_credit       TEXT NOT NULL,
    opened_at         TEXT NOT NULL,
    status            TEXT NOT NULL,
    profit_order_ref  TEXT,
    closing_order_ref TEXT,
    closed_at         TEXT,
    close_price       TEXT,
    close_reason      TEXT NOT NULL DEFAULT '',
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS management_actions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            TEXT NOT NULL,
    spread_id         TEXT,
    symbol            TEXT NOT NULL,
    kind              TEXT NOT NULL,
    detail            TEXT NOT NULL DEFAULT '',
    order_ref         TEXT,
    purpose           TEXT,
    tif               TEXT,
    limit_price       TEXT,
    quantity          INTEGER,
    legs_json         TEXT,
    broker_order_id   TEXT,
    execution_outcome TEXT,
    execution_message TEXT,
    recorded_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_spreads_status ON spreads (status);
CREATE INDEX IF NOT EXISTS idx_management_run ON management_actions (run_id);
CREATE INDEX IF NOT EXISTS idx_management_spread ON management_actions (spread_id);
"""

#: Added in version 3. One row per opening order that reached the venue and
#: then stopped being workable without producing a position -- a DAY limit that
#: expired unfilled at the close, or one cancelled outside this process.
#:
#: The table exists because ``unreconciled_openings`` has no age or status
#: predicate: without a marker the same dead row is re-read and silently
#: skipped on every pass forever, and nothing ever tells the operator that an
#: order failed to fill. That silence makes a screen that admits unfillable
#: spreads look exactly like a screen that admits nothing.
_SCHEMA_V3 = """
CREATE TABLE IF NOT EXISTS abandoned_openings (
    proposal_id TEXT PRIMARY KEY,
    detail      TEXT NOT NULL DEFAULT '',
    noticed_at  TEXT NOT NULL
);
"""

#: Status values a spread row must carry to be returned by ``live_spreads``.
#: Sorted so the query text, and therefore the plan, is the same every open.
_LIVE_STATUS_VALUES = tuple(sorted(status.value for status in LIVE_SPREAD_STATUSES))

#: Order outcomes meaning the opening order reached the venue and may have
#: filled. ``BROKER_REJECTED`` reached the venue too, but nothing can have
#: filled from it, so there is nothing to reconcile.
_REACHED_VENUE_VALUES = tuple(
    sorted(o.value for o in SUBMITTED_OUTCOMES if o is not Outcome.BROKER_REJECTED)
)


#: How long a competing writer waits for the lock before giving up.
#:
#: Passed to :func:`sqlite3.connect` and set as ``busy_timeout`` so both the
#: Python driver and SQLite itself wait rather than failing instantly.
_BUSY_TIMEOUT_SECONDS = 5.0


class SchemaVersionError(TraderError):
    """The database was written by a newer engine than this one.

    Opening it anyway would read columns this code does not know about as if
    they were absent, and write rows the newer engine would misread. Refusing
    is the only response that leaves the file as it was found.
    """


def _apply_schema(conn: sqlite3.Connection, path: Path) -> None:
    """Bring ``conn`` to :data:`SCHEMA_VERSION`, or refuse a newer file.

    Version 0 is both a fresh file and one written before the marker existed;
    the full schema is idempotent, so both take the same path. Version 1 is
    reserved for a file stamped with the history tables alone.
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise SchemaVersionError(
            f"{path} has schema version {version}; this engine understands "
            f"version {SCHEMA_VERSION}. It was written by a newer engine."
        )
    if version == 0:
        conn.executescript(_SCHEMA_V1 + _SCHEMA_V2 + _SCHEMA_V3)
    elif version == 1:
        conn.executescript(_SCHEMA_V2 + _SCHEMA_V3)
    elif version == 2:
        conn.executescript(_SCHEMA_V3)
    # PRAGMA takes no bound parameters; the value is a module-level int.
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


class SqliteStore:
    """SQLite-backed :class:`~ibkr_trader.ports.Store`.

    Satisfies that port including ``close()``, which joined the contract after
    it spent a release with zero callers while the connection leaked.
    Conformance is checked by ``tests/test_port_conformance.py`` rather than
    asserted by this sentence -- the previous version of it was a noun phrase
    describing what the class *is*, which is not a claim anything could check.
    """

    def __init__(self, path: str | Path, clock: Clock | None = None) -> None:
        self._clock = clock or SystemClock()
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, timeout=_BUSY_TIMEOUT_SECONDS)
        self._conn.row_factory = sqlite3.Row
        # Nothing stops a second process being pointed at the same database.
        # On the default rollback journal that second writer gets `database is
        # locked` immediately -- sqlite3's default busy_timeout is 0 -- and the
        # runner logs the failure and carries on, so the outcome is lost with
        # only a log line to show for it. WAL lets a reader and a writer
        # coexist; the busy timeout makes a competing writer wait for the lock
        # instead of failing instantly.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(f"PRAGMA busy_timeout={int(_BUSY_TIMEOUT_SECONDS * 1000)}")
        try:
            _apply_schema(self._conn, self._path)
        except SchemaVersionError:
            self._conn.close()
            raise

    def close(self) -> None:
        """Close the underlying connection."""
        self._conn.close()

    def start_run(
        self,
        run_id: str,
        declared_mode: str,
        verified_account: str,
        host: str,
        port: int,
    ) -> None:
        """Persist the identity of a pass before it may process any symbol.

        Unlike symbol-attempt recording, failure here propagates and aborts the
        pass. Trading without a durable statement of the verified account would
        recreate the exact audit ambiguity this row exists to close.

        Raises:
            Exception: the underlying storage failure, unchanged.
        """
        with self._conn:
            self._conn.execute(
                "INSERT INTO runs "
                "(run_id, declared_mode, verified_account, host, port, started_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    declared_mode,
                    verified_account,
                    host,
                    port,
                    self._clock.now().isoformat(),
                ),
            )

    def record(self, result: SymbolResult, run_id: str) -> None:
        """Persist everything known about one symbol attempt, atomically.

        Written in a single transaction so a proposal never exists in the record
        without the attempt that produced it.

        Raises:
            Exception: nothing here is translated into a domain error. A locked
                database, a full disk, a connection already closed, or a value
                JSON cannot encode all propagate as whatever ``sqlite3`` or
                ``json`` raised. That is why ``runner.py`` guards the call with
                a blanket handler -- losing the audit row is preferable to
                losing the pass, but the caller has to know to make that choice.
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

    # --- management: the rows the manager reads back ----------------------

    def record_spread(self, spread: Spread) -> None:
        """Insert or replace the row for ``spread``, keyed by its id.

        ``updated_at`` is the clock at the time of the write, so a status that
        changed and a status that was merely re-recorded are told apart.

        Raises:
            Exception: the underlying storage failure, unchanged.
        """
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO spreads ("
                "spread_id, symbol, expiry, short_strike, long_strike, quantity, "
                "open_credit, opened_at, status, profit_order_ref, closing_order_ref, "
                "closed_at, close_price, close_reason, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    spread.spread_id,
                    spread.symbol,
                    spread.expiry.isoformat(),
                    str(spread.short_strike),
                    str(spread.long_strike),
                    spread.quantity,
                    str(spread.open_credit),
                    spread.opened_at.isoformat(),
                    spread.status.value,
                    spread.profit_order_ref,
                    spread.closing_order_ref,
                    _optional(datetime.isoformat, spread.closed_at),
                    _optional(str, spread.close_price),
                    spread.close_reason,
                    self._clock.now().isoformat(),
                ),
            )

    def record_abandoned_opening(self, proposal_id: str, detail: str) -> None:
        """Mark an opening order that will never fill, so it stops being re-read.

        Idempotent: the manager reaches this once per order, but a re-run over
        the same database must not fail on the primary key.
        """
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO abandoned_openings "
                "(proposal_id, detail, noticed_at) VALUES (?, ?, ?)",
                (proposal_id, detail, self._clock.now().isoformat()),
            )

    def live_spreads(self) -> tuple[Spread, ...]:
        """Every spread whose status is in ``LIVE_SPREAD_STATUSES``.

        Ordered by when each was opened, then by id, so two passes over an
        unchanged book see the same sequence.
        """
        placeholders = ", ".join("?" for _ in _LIVE_STATUS_VALUES)
        rows = self._conn.execute(
            f"SELECT * FROM spreads WHERE status IN ({placeholders}) "
            "ORDER BY opened_at, spread_id",
            _LIVE_STATUS_VALUES,
        ).fetchall()
        return tuple(_spread_from_row(row) for row in rows)

    def spread(self, spread_id: str) -> Spread | None:
        """The spread recorded under ``spread_id``, whatever its status."""
        row = self._conn.execute(
            "SELECT * FROM spreads WHERE spread_id = ?", (spread_id,)
        ).fetchone()
        return None if row is None else _spread_from_row(row)

    def unreconciled_openings(self) -> tuple[OpeningOrder, ...]:
        """Opening orders that reached the venue and have no spread row yet.

        "Reached the venue" is an order outcome in ``SUBMITTED_OUTCOMES`` other
        than ``BROKER_REJECTED``; ``SUBMISSION_FAILED`` never left this process
        and a rejection cannot have filled. The strikes come from the
        proposal's own legs, the fill figures from whatever fills the
        submission window recorded -- which may be none, for a ``WORKING``
        order, and is what the manager reconciles against the account.

        Orders marked by :meth:`record_abandoned_opening` are excluded. There
        is deliberately still no age predicate here: deciding an order is dead
        needs the broker's working set, which this class cannot see, so that
        judgement belongs to the manager and is durable only once it is made.
        """
        placeholders = ", ".join("?" for _ in _REACHED_VENUE_VALUES)
        rows = self._conn.execute(
            "SELECT p.proposal_id, p.symbol, p.expiry, p.quantity, p.limit_price, "
            "p.legs_json, o.recorded_at "
            "FROM orders AS o JOIN trade_proposals AS p ON p.proposal_id = o.proposal_id "
            f"WHERE o.outcome IN ({placeholders}) "
            "AND NOT EXISTS (SELECT 1 FROM spreads AS s WHERE s.spread_id = o.proposal_id) "
            "AND NOT EXISTS (SELECT 1 FROM abandoned_openings AS a "
            "                WHERE a.proposal_id = o.proposal_id) "
            "ORDER BY o.recorded_at, o.proposal_id",
            _REACHED_VENUE_VALUES,
        ).fetchall()
        return tuple(self._opening_from_row(row) for row in rows)

    def _opening_from_row(self, row: sqlite3.Row) -> OpeningOrder:
        proposal_id = row["proposal_id"]
        legs = json.loads(row["legs_json"])
        fills = self._conn.execute(
            "SELECT quantity, price FROM fills WHERE proposal_id = ? ORDER BY id",
            (proposal_id,),
        ).fetchall()
        filled_quantity = sum(fill["quantity"] for fill in fills)
        fill_price: Decimal | None = None
        if filled_quantity:
            notional = sum(
                Decimal(fill["price"]) * Decimal(fill["quantity"]) for fill in fills
            )
            fill_price = notional / Decimal(filled_quantity)
        return OpeningOrder(
            proposal_id=proposal_id,
            symbol=row["symbol"],
            expiry=date.fromisoformat(row["expiry"]),
            short_strike=_strike_of(legs, Action.SELL, proposal_id),
            long_strike=_strike_of(legs, Action.BUY, proposal_id),
            quantity=row["quantity"],
            limit_price=Decimal(row["limit_price"]),
            filled_quantity=filled_quantity,
            fill_price=fill_price,
            recorded_at=datetime.fromisoformat(row["recorded_at"]),
        )

    def record_management(self, action: ManagementAction, run_id: str) -> None:
        """Persist one management step in a single transaction.

        The order columns are ``NULL`` for an observation that placed nothing;
        ``order_ref`` falls back to the execution's reference when the action
        carries a result for an order placed by an earlier pass.

        Raises:
            Exception: the underlying storage failure, unchanged -- the same
                policy as :meth:`record`, and the manager guards it the same way.
        """
        order = action.order
        execution = action.execution
        order_ref = order.order_ref if order else _optional(lambda e: e.order_ref, execution)
        with self._conn:
            self._conn.execute(
                "INSERT INTO management_actions ("
                "run_id, spread_id, symbol, kind, detail, order_ref, purpose, tif, "
                "limit_price, quantity, legs_json, broker_order_id, execution_outcome, "
                "execution_message, recorded_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    action.spread_id,
                    action.symbol,
                    action.kind.value,
                    action.detail,
                    order_ref,
                    _optional(lambda o: o.purpose, order),
                    _optional(lambda o: o.tif.value, order),
                    _optional(lambda o: str(o.limit_price), order),
                    _optional(lambda o: o.quantity, order),
                    _optional(lambda o: json.dumps(_combo_legs_as_json(o.legs)), order),
                    _optional(lambda e: e.broker_order_id, execution),
                    _optional(lambda e: e.outcome.value, execution),
                    _optional(lambda e: e.message, execution),
                    self._clock.now().isoformat(),
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

    def runs(self) -> list[dict[str, Any]]:
        """Pass identities, oldest first."""
        return self._query("SELECT * FROM runs ORDER BY rowid")

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

    def spreads(self) -> list[dict[str, Any]]:
        """Every spread row as stored, live or closed, oldest opened first."""
        return self._query("SELECT * FROM spreads ORDER BY opened_at, spread_id")

    def management_actions(self, run_id: str | None = None) -> list[dict[str, Any]]:
        """Management steps, oldest first, optionally scoped to one run."""
        if run_id is None:
            return self._query("SELECT * FROM management_actions ORDER BY id")
        return self._query(
            "SELECT * FROM management_actions WHERE run_id = ? ORDER BY id", (run_id,)
        )

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return [dict(row) for row in self._conn.execute(sql, params).fetchall()]


_T = TypeVar("_T")
_R = TypeVar("_R")


def _optional(convert: Callable[[_T], _R], value: _T | None) -> _R | None:
    """``convert(value)``, or ``None`` for ``None`` -- nullable columns both ways."""
    return None if value is None else convert(value)


def _spread_from_row(row: sqlite3.Row) -> Spread:
    """Rehydrate a ``spreads`` row into the exact value that was recorded."""
    return Spread(
        spread_id=row["spread_id"],
        symbol=row["symbol"],
        expiry=date.fromisoformat(row["expiry"]),
        short_strike=Decimal(row["short_strike"]),
        long_strike=Decimal(row["long_strike"]),
        quantity=row["quantity"],
        open_credit=Decimal(row["open_credit"]),
        opened_at=datetime.fromisoformat(row["opened_at"]),
        status=SpreadStatus(row["status"]),
        profit_order_ref=row["profit_order_ref"],
        closing_order_ref=row["closing_order_ref"],
        closed_at=_optional(datetime.fromisoformat, row["closed_at"]),
        close_price=_optional(Decimal, row["close_price"]),
        close_reason=row["close_reason"],
    )


def _strike_of(legs: list[dict[str, Any]], action: Action, proposal_id: str) -> Decimal:
    """The strike of the one leg in ``legs`` on side ``action``.

    A vertical has exactly one leg per side. Anything else is not a row this
    engine wrote, and guessing a strike for it would hand the manager a spread
    that does not match the legs the account holds.
    """
    strikes = [leg["strike"] for leg in legs if leg["action"] == action.value]
    if len(strikes) != 1:
        raise ValueError(
            f"proposal {proposal_id} has {len(strikes)} {action.value} legs; "
            "expected exactly one"
        )
    return Decimal(strikes[0])


def _legs_as_json(proposal) -> list[dict[str, Any]]:
    """Serialize proposal legs, preserving exact decimal prices as strings.

    The audit row carries the raw market only. The derived liquidity figures are
    reproducible from bid and ask at any time, and storing a second copy of a
    computed number is how the stored one drifts from the computation.
    """
    return [leg_payload(leg) for leg in proposal.legs]


def _combo_legs_as_json(legs: tuple[ComboLeg, ...]) -> list[dict[str, Any]]:
    """Serialize combo legs in the shape of :func:`leg_payload`, minus the quote.

    A management order carries no market -- the manager priced it from a quote
    it did not keep -- so the wire shape is the contract identity and the side.
    """
    return [
        {
            "symbol": combo.leg.symbol,
            "expiry": combo.leg.expiry.isoformat(),
            "strike": str(combo.leg.strike),
            "right": combo.leg.right.value,
            "action": combo.action.value,
            "ratio": combo.ratio,
        }
        for combo in legs
    ]
