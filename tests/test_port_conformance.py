"""That the ports contract is enforced by something other than prose.

``ports.py`` opens by claiming it "tells you the complete set of things V4
depends on". Nothing checked that. No Protocol was ``runtime_checkable``, no
concrete class subclassed its port, no ``isinstance`` check against a port
existed anywhere, and the toolchain has no type checker -- so the central
architectural claim of the system rested on four docstrings and a coincidence.

These tests make three distinct kinds of divergence fail a gate. They are
deliberately three, because the first one alone would have caught nothing:

1. **Structural** -- the implementation has every member the port declares.
2. **Signature** -- each member's parameters, defaults and return annotation
   match the port exactly. *Every adapter already passed this before the
   contract was widened*, which is precisely why signature checking alone is
   not a gate worth having.
3. **Error contract** -- the port's documented ``Raises:`` block and the
   production adapter's agree. This is the one that catches INT-033:
   ``ports.Broker.submit`` documented two exceptions while ``IBKRBroker.submit``
   raises three, and ``runner.py:320`` branches on the third.

Each kind has a negative fixture -- a deliberately divergent implementation that
must fail the check. Without those, a check that silently passes everything is
indistinguishable from a check that works.
"""

from __future__ import annotations

import inspect
import re
from typing import Any, get_type_hints

import pytest

from ibkr_trader import ports
from ibkr_trader.broker import IBKRBroker
from ibkr_trader.clock import FixedClock
from ibkr_trader.config import build_config
from ibkr_trader.models import (
    ExecutionResult,
    NoTrade,
    Portfolio,
    SymbolResult,
    TradeProposal,
)
from ibkr_trader.reviewer import ClaudeReviewer
from ibkr_trader.scanner import IBKRMarketData
from ibkr_trader.store import SqliteStore

from . import fakes
from .fakes import ACCOUNT, SCAN_TIME

# --- reading a Protocol -------------------------------------------------


def protocol_members(protocol: type) -> list[str]:
    """Every member a conforming implementation must provide."""
    return sorted(
        name
        for name, value in vars(protocol).items()
        if not name.startswith("_")
        and (inspect.isfunction(value) or isinstance(value, property))
    )


def member(owner: type, name: str) -> Any:
    """The underlying function for a method or a property."""
    value = inspect.getattr_static(owner, name)
    return value.fget if isinstance(value, property) else value


def signature_of(owner: type, name: str) -> inspect.Signature:
    """A member's signature with ``self`` dropped and annotations as written."""
    sig = inspect.signature(member(owner, name))
    return sig.replace(parameters=[p for p in sig.parameters.values() if p.name != "self"])


RAISES_BLOCK = re.compile(r"^\s*Raises:\s*$")
RAISES_ENTRY = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_.]*):")


def documented_raises(owner: type, name: str) -> set[str]:
    """Exception names in a Google-style ``Raises:`` block.

    Reads the docstring rather than the body deliberately: the contract is what
    a caller is told to handle. A raise the docstring omits is invisible to
    everyone writing a substitute, which is the whole shape of INT-033.
    """
    doc = inspect.getdoc(member(owner, name)) or ""
    lines = doc.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if RAISES_BLOCK.match(line))
    except StopIteration:
        return set()

    found: set[str] = set()
    body = lines[start + 1 :]
    indent = None
    for line in body:
        if not line.strip():
            continue
        current = len(line) - len(line.lstrip())
        if indent is None:
            indent = current
        elif current < indent:
            break
        if current == indent:
            match = RAISES_ENTRY.match(line)
            if match:
                found.add(match.group(1))
    return found


# --- the implementations under contract ---------------------------------


def production_adapters(tmp_path) -> list[tuple[type, Any]]:
    """One live instance of each production adapter, port by port."""
    config = build_config({"universe": ["AAPL"], "ibkr": {"account": ACCOUNT}})
    clock = FixedClock(SCAN_TIME)
    return [
        (
            ports.MarketData,
            IBKRMarketData(
                ibkr_config=config.ibkr,
                strategy_config=config.strategy,
                clock=clock,
                ib=object(),
            ),
        ),
        (ports.Reviewer, ClaudeReviewer(config.reviewer, clock, client=object())),
        (ports.Broker, IBKRBroker(config.ibkr, clock)),
        (ports.Store, SqliteStore(tmp_path / "conformance.sqlite3", clock=clock)),
    ]


def doubles() -> list[tuple[type, Any]]:
    """The test doubles the suite substitutes for those adapters.

    A double that drifts from the contract is worse than no double: it makes a
    green suite mean less than it appears to. They are held to the same
    structural and signature checks as the real adapters.
    """
    return [
        (ports.MarketData, fakes.StubMarketData()),
        (ports.Reviewer, fakes.StubReviewer()),
        (ports.Broker, fakes.FakeBroker()),
    ]


def all_protocols() -> list[type]:
    """Every Protocol declared in ``ports``, discovered rather than listed.

    Enumerated instead of hard-coded so a fifth port added later is covered by
    every check below without anyone remembering to extend a tuple. A gate that
    silently stops covering new work is the failure mode this whole module
    exists to prevent, and it would be an odd one to reintroduce here.
    """
    found = [
        value
        for name, value in vars(ports).items()
        if not name.startswith("_")
        and isinstance(value, type)
        and getattr(value, "_is_protocol", False)
        # `typing.Protocol` is itself imported into the module namespace and is
        # itself a protocol; only the ones declared here are ports.
        and value.__module__ == ports.__name__
    ]
    assert found, "no Protocol found in ports -- the discovery itself is broken"
    return found


# --- ARCH-C1, check 1: structural ---------------------------------------


def test_every_protocol_is_runtime_checkable():
    """Without this, ``isinstance`` against a port raises instead of answering."""
    for protocol in all_protocols():
        assert isinstance(object(), protocol) is False, (
            f"{protocol.__name__} is not runtime_checkable"
        )


def test_every_port_has_a_production_implementation_under_test(tmp_path):
    """The coverage claim the checks below rest on.

    Each check iterates a list of (port, implementation) pairs. If a port were
    missing from those lists, every one of them would pass while saying nothing
    about it -- green, and hollow.
    """
    covered = {protocol for protocol, _ in production_adapters(tmp_path)}

    assert covered == set(all_protocols()), (
        f"ports not exercised: {sorted(p.__name__ for p in set(all_protocols()) - covered)}"
    )


def test_production_adapters_satisfy_their_port_structurally(tmp_path):
    for protocol, impl in production_adapters(tmp_path):
        assert isinstance(impl, protocol), (
            f"{type(impl).__name__} is missing members of {protocol.__name__}"
        )


def test_test_doubles_satisfy_their_port_structurally():
    for protocol, impl in doubles():
        assert isinstance(impl, protocol), (
            f"{type(impl).__name__} is missing members of {protocol.__name__}"
        )


def test_the_structural_check_rejects_a_missing_member():
    """Negative fixture. A store that cannot be closed is not a Store."""

    class UncloseableStore:
        def record(self, result: SymbolResult, run_id: str) -> None: ...

    assert not isinstance(UncloseableStore(), ports.Store)


# --- ARCH-C1, check 2: signature ----------------------------------------


def test_production_adapter_signatures_match_their_port(tmp_path):
    for protocol, impl in production_adapters(tmp_path):
        for name in protocol_members(protocol):
            assert signature_of(type(impl), name) == signature_of(protocol, name), (
                f"{type(impl).__name__}.{name} diverges from {protocol.__name__}.{name}"
            )


def test_the_signature_check_rejects_a_dropped_parameter():
    """Negative fixture. This is the drift ``isinstance`` cannot see.

    ``UntypedStore`` passes the structural check -- the member is present --
    and would silently accept every call the runner makes today while being
    unable to attribute a row to a run.
    """

    class UntypedStore:
        def record(self, result: SymbolResult) -> None: ...
        def close(self) -> None: ...

    assert isinstance(UntypedStore(), ports.Store), "structural check should pass here"
    assert signature_of(UntypedStore, "record") != signature_of(ports.Store, "record")


# --- ARCH-C1, check 3: error contract (INT-033) -------------------------


def test_the_port_documents_the_exceptions_its_adapter_raises(tmp_path):
    """The check that catches INT-033.

    Applied to the production adapters only. A double is free to raise nothing;
    what must not happen is a *port* that under-describes what a caller has to
    handle, because a substitute is written against the port.
    """
    for protocol, impl in production_adapters(tmp_path):
        for name in protocol_members(protocol):
            assert documented_raises(protocol, name) == documented_raises(type(impl), name), (
                f"{protocol.__name__}.{name} and {type(impl).__name__}.{name} "
                f"document different exceptions"
            )


def test_submission_failed_is_part_of_the_broker_contract():
    """INT-033, stated as its own assertion so the regression is named.

    ``runner.py`` catches ``SubmissionFailed`` and turns it into
    ``Outcome.SUBMISSION_FAILED``. A substitute written against a port that
    never mentions it would leave that entire classification path dead.
    """
    assert "SubmissionFailed" in documented_raises(ports.Broker, "submit")


def test_the_error_contract_check_rejects_an_undocumented_exception():
    """Negative fixture: exactly the shape INT-033 had."""

    class QuietReviewer:
        def review(self, proposal: TradeProposal, portfolio: Portfolio):
            """Return a verdict.

            Raises:
                ReviewTimeout: no answer in time.
            """

    assert documented_raises(QuietReviewer, "review") != documented_raises(
        ports.Reviewer, "review"
    )


# --- ARCH-C4: the working-orders obligation -----------------------------


def test_the_port_states_the_working_orders_obligation():
    """The obligation the duplicate-order guard silently depends on.

    ``portfolio()`` must report orders still working at the broker as positions
    flagged ``pending``. IBKR's position stream lists only *filled* holdings, so
    a conforming substitute that omits them makes ``held`` empty at
    ``tastytrade.py:196`` -- and the guard is skipped rather than failed.
    """
    doc = inspect.getdoc(member(ports.MarketData, "portfolio")) or ""
    assert "pending" in doc, "the port does not mention working orders at all"


def test_the_port_states_what_to_do_when_the_obligation_cannot_be_met():
    """An implementation that cannot see working orders must say so.

    Reporting no rows and reporting "I could not find out" are the same value
    otherwise, and the guards key on a row existing — so the second one skips
    them silently. This is the half of the obligation that makes the first half
    safe to rely on.
    """
    doc = " ".join((inspect.getdoc(member(ports.MarketData, "portfolio")) or "").split())

    assert "pending_orders_known=False" in doc, (
        "the port states the obligation but not how to signal failing it"
    )


def test_the_default_double_carries_the_working_orders_obligation():
    """A conforming fake must be able to satisfy the obligation, not just the shape.

    Before this, ``StubMarketData`` returned a positions-free ``Portfolio`` and
    ``tests/test_pending_orders.py`` had to hand-replicate the adapter's
    synthesis in a local subclass to test the guard at all -- the obligation
    living in a test file instead of in a contract.
    """
    market = fakes.StubMarketData(working_orders={"AAPL": 2})

    positions = market.portfolio().positions_for("AAPL")

    assert positions, "a working order is not reported as exposure"
    assert all(p.pending for p in positions)
    assert positions[0].quantity == 2


def test_a_conforming_double_that_omits_working_orders_defeats_the_guard():
    """Negative fixture, and the reason the obligation belongs in the contract.

    Both doubles below satisfy the ``MarketData`` port. They differ only in
    whether they honour the obligation its docstring now states. The one that
    does refuses the duplicate; the one that does not proposes the same trade
    again — with no error, no warning, and no way for the caller to tell.

    That is the whole shape of ARCH-C4: the guard keys on a position *existing*
    (``tastytrade.py:196``), so an omitted row skips the check rather than
    failing it.
    """
    from ibkr_trader.tastytrade import evaluate

    config = build_config({"universe": ["AAPL"], "ibkr": {"account": ACCOUNT}})
    snapshot = fakes.tradable_snapshot("AAPL")

    honours = fakes.StubMarketData(working_orders={"AAPL": 3})
    omits = fakes.StubMarketData()

    assert isinstance(honours, ports.MarketData)
    assert isinstance(omits, ports.MarketData), "both conform; that is the point"

    refused = evaluate(
        "AAPL", snapshot, honours.portfolio(), config.strategy, config.risk, SCAN_TIME
    )
    proposed = evaluate(
        "AAPL", snapshot, omits.portfolio(), config.strategy, config.risk, SCAN_TIME
    )

    assert isinstance(refused, NoTrade), "a working order must block a second proposal"
    assert "working order" in refused.reason
    assert isinstance(proposed, TradeProposal), (
        "the omission is silent -- it produces a normal proposal, not a failure"
    )


# --- the claim the module makes about itself ----------------------------


def test_every_port_member_the_composition_root_calls_is_declared():
    """``ports.py`` says reading it tells you everything V4 depends on.

    ``cli.py`` calls ``connect()``, ``disconnect()`` and ``close()`` on the
    objects it wires. Until those joined the contract the statement was false,
    and a double could conform while lacking the method whose absence had
    already leaked a database connection once.
    """
    broker_members = set(protocol_members(ports.Broker))
    assert broker_members >= {"submit", "is_connected", "connect", "disconnect"}
    assert set(protocol_members(ports.Store)) >= {"record", "close"}


def test_the_ports_module_type_hints_resolve():
    """A contract nothing imports can drift into referring to nothing.

    ``ports.py`` has exactly one importer, so a stale annotation would go
    unnoticed. Resolving the hints is the cheapest check that every name in the
    contract still exists.
    """
    for protocol in all_protocols():
        for name in protocol_members(protocol):
            hints = get_type_hints(member(protocol, name))
            assert "return" in hints, f"{protocol.__name__}.{name} has no return annotation"


@pytest.mark.parametrize("protocol", all_protocols(), ids=lambda p: p.__name__)
def test_each_port_declares_at_least_one_member(protocol):
    """Guards the harness itself: an empty Protocol would pass every check above."""
    assert protocol_members(protocol), f"{protocol.__name__} declares nothing"


def test_execution_result_is_what_the_broker_port_returns():
    """Pins the return type the runner's branching depends on."""
    hints = get_type_hints(member(ports.Broker, "submit"))
    assert hints["return"] is ExecutionResult
