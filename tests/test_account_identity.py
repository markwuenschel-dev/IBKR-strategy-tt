"""Which account this process trades, and how it knows.

Three defects converge here, and the fix for all of them is one property: the
account the operator named must be the account the session actually reached.

``account`` was optional. Unset, ``ib.accountValues("")`` returns the *union* of
every account under the login -- not a default account -- and ``_account_amount``
then scans that flat list once per tag. Net liquidation and buying power could
therefore resolve to two different accounts, producing a portfolio describing no
real book, which the sizing arithmetic and the LLM reviewer both then treated as
fact. IBKR rejects the resulting order rather than misrouting it, so no money
moved to the wrong place -- but the trade was judged against a fiction.

The ``paper`` flag did not help. It changed no runtime behaviour at all: two
code reads, a startup validator and a log argument, and the log argument printed
``paper=True`` on whatever session had actually opened.

**What this does and does not prove.** ``managedAccounts()`` proves the process
reached the specifically configured account. It does *not* prove that account is
a paper account: IBKR exposes no paper/live indicator, the ``DU`` convention is
undocumented, and this repository's own paper account ``DUR318607`` breaks the
obvious regex. Paper safety rests on the operator naming the intended paper
account. Nothing here may claim otherwise.
"""

from __future__ import annotations

import logging
from decimal import Decimal

import pytest

from ibkr_trader.broker import IBClient, IBKRBroker
from ibkr_trader.clock import FixedClock
from ibkr_trader.config import build_config
from ibkr_trader.errors import BrokerNotConnected, ConfigError, MarketDataError
from ibkr_trader.scanner import IBKRMarketData

from .fakes import SCAN_TIME

ACCOUNT = "DU1234567"
OTHER = "DU7654321"


def settings(**ibkr):
    return {"universe": ["AAPL"], "ibkr": {"account": ACCOUNT, **ibkr}}


# --- Q1: the account is named, or the config does not build ---------------


def test_a_config_with_no_connection_block_is_rejected():
    """`ibkr` lost its default, because that default had no account to give.

    Rejected at construction rather than at connect because a frozen, fully
    validated config is this system's stated precondition for touching the
    network at all.
    """
    with pytest.raises(ConfigError, match="ibkr"):
        build_config({"universe": ["AAPL"]})


def test_a_connection_block_without_an_account_names_the_account():
    """The likelier mistake, and the one whose message has to be useful.

    Asserted separately from the case above because they fail differently: an
    absent `ibkr` block reports `ibkr`, and only a present-but-incomplete one
    can report `ibkr.account`. A single test matching "account" would have
    passed on the wrong error.
    """
    with pytest.raises(ConfigError, match="account"):
        build_config({"universe": ["AAPL"], "ibkr": {"port": 7497}})


def test_an_empty_account_is_rejected_too():
    """`""` reached exactly the same union-of-all-accounts path as None."""
    with pytest.raises(ConfigError, match="account"):
        build_config({"universe": ["AAPL"], "ibkr": {"account": ""}})


def test_a_named_account_builds():
    config = build_config(settings())

    assert config.ibkr.account == ACCOUNT


# --- Q2: the port check warns; identity enforces --------------------------


@pytest.mark.parametrize("live_port", [7496, 4001])
def test_a_paper_run_on_a_live_port_warns_rather_than_refusing(live_port, caplog):
    """Demoted deliberately, and 4001 is covered for the first time.

    IBKR documents these as *defaults* that "can be changed to any open socket
    port", and specifically warns about running paper and live TWS on one
    machine. A port number is therefore a hint, not evidence: a live session on
    7497 passes this check, and a tunnel makes that ordinary. Refusing on it
    would block legitimate multi-instance deployments while still missing the
    hazard it was written for.
    """
    with caplog.at_level(logging.WARNING):
        config = build_config(settings(port=live_port))

    assert config.ibkr.port == live_port, "the run is not blocked"
    assert any(str(live_port) in r.message for r in caplog.records), (
        f"port {live_port} produced no warning"
    )


def test_the_warning_does_not_claim_the_session_is_live_or_paper(caplog):
    """It reports that two *configured* values disagree. That is all it knows.

    Nothing at configuration time has opened a socket, so a message asserting
    the session is live would be a claim the code cannot support -- which is the
    precise failure the old `paper=%s` log line committed at connect.
    """
    with caplog.at_level(logging.WARNING):
        build_config(settings(port=7496))

    warning = next(r.getMessage() for r in caplog.records if "7496" in r.getMessage())

    assert "conventionally" in warning, "the warning must not state this as fact"
    assert "account check" in warning, "it must point at what actually enforces"
    for overclaim in ("is live", "is a live session", "you are live"):
        assert overclaim not in warning


@pytest.mark.parametrize("paper_port", [7497, 4002])
def test_a_paper_run_on_a_paper_port_is_silent(paper_port, caplog):
    with caplog.at_level(logging.WARNING):
        build_config(settings(port=paper_port))

    assert not [r for r in caplog.records if str(paper_port) in r.message]


# --- Q1/Q3: the session must return the account we named ------------------


class FakeIB:
    """A connected client that reports a configurable account list."""

    def __init__(self, accounts=(ACCOUNT,), connected=True):
        self._accounts = list(accounts)
        self._connected = False
        self._will_connect = connected
        self.disconnects = 0

    def isConnected(self) -> bool:
        return self._connected

    def connect(self, host, port, clientId, timeout):
        self._connected = self._will_connect

    def disconnect(self) -> None:
        self._connected = False
        self.disconnects += 1

    def managedAccounts(self) -> list[str]:
        return list(self._accounts)

    def qualifyContracts(self, *contracts):
        return list(contracts)

    def placeOrder(self, contract, order):
        raise AssertionError("no order should be placed in these tests")

    def waitOnUpdate(self, timeout: float = 0) -> bool:
        return True


def broker(ib, **ibkr):
    config = build_config(settings(**ibkr))
    return IBKRBroker(config.ibkr, FixedClock(SCAN_TIME), ib=ib)


def test_a_session_that_cannot_be_asked_for_accounts_fails_closed():
    """The previous version of this test could not fail, so it is gone.

    It asserted `hasattr(IBClient, "managedAccounts")` -- that the Protocol
    contains what the Protocol declares. True by construction, and it survived
    every mutation of the guard it was supposed to protect.

    The property that matters is what happens to a session object that cannot
    answer the question: the guard must refuse and close, not skip itself. That
    also proves the call site exists, which is what the old assertion was
    reaching for and could not reach.
    """

    class Truncated:
        """A client with no `managedAccounts` -- an older vendor, or a bad double."""

        def __init__(self) -> None:
            self._connected = False
            self.disconnects = 0

        def isConnected(self) -> bool:
            return self._connected

        def connect(self, host, port, clientId, timeout):
            self._connected = True

        def disconnect(self) -> None:
            self._connected = False
            self.disconnects += 1

    ib = Truncated()

    with pytest.raises(BrokerNotConnected, match="account list"):
        broker(ib).connect()

    assert ib.disconnects == 1, "an unusable session was left open"
    assert not ib.isConnected()
    # Secondary, and weak on its own: the typed contract names the call the
    # behaviour above depends on, so a conforming double knows to supply it.
    assert hasattr(IBClient, "managedAccounts")


def test_connecting_to_the_named_account_succeeds():
    ib = FakeIB(accounts=[ACCOUNT])

    broker(ib).connect()

    assert ib.isConnected()
    assert ib.disconnects == 0


def test_connecting_to_a_different_account_refuses_to_start():
    """The whole point. The session opened, but not to the book we were told.

    A config copied between machines is the realistic way this happens, and it
    is exactly the case a port number cannot see.
    """
    ib = FakeIB(accounts=[OTHER])

    with pytest.raises(BrokerNotConnected) as caught:
        broker(ib).connect()

    message = str(caught.value)
    assert ACCOUNT in message, "the message must name what was expected"
    assert OTHER in message, "and what was found, or it is not diagnosable"


def test_a_refused_session_is_closed_before_the_error_propagates():
    """Refusing while leaving a live socket open would be worse than not checking.

    The caller sees `BrokerNotConnected`, which `cli.py` already maps to a
    non-zero exit, so nothing else has to change to make the failure clean.
    """
    ib = FakeIB(accounts=[OTHER])

    with pytest.raises(BrokerNotConnected):
        broker(ib).connect()

    assert ib.disconnects == 1
    assert not ib.isConnected()


def test_an_account_among_several_is_accepted():
    """Multi-account logins are a normal IBKR configuration, not an error.

    What was hazardous was never *having* several; it was not saying which.
    """
    ib = FakeIB(accounts=[OTHER, ACCOUNT, "DU999"])

    broker(ib).connect()

    assert ib.isConnected()


def test_a_session_reporting_no_accounts_refuses_to_start():
    """Unverifiable is not the same as verified, and must not be treated as it."""
    ib = FakeIB(accounts=[])

    with pytest.raises(BrokerNotConnected, match="no accounts"):
        broker(ib).connect()


def test_the_connect_log_names_the_account_and_claims_nothing_about_paper(caplog):
    """`paper=True` was printed against whatever session had actually opened.

    The log may state what was verified -- the endpoint and the account -- and
    must not restate a config flag as though the session had confirmed it.
    """
    ib = FakeIB(accounts=[ACCOUNT])

    with caplog.at_level(logging.INFO):
        broker(ib).connect()

    connected = [r.getMessage() for r in caplog.records if "connected" in r.getMessage()]
    assert connected, "the connection is not logged at all"
    line = connected[0]
    assert ACCOUNT in line
    assert "paper=" not in line, "the session cannot confirm paper; do not print it"


# --- Q4: what live arming is, and what it is not ---------------------------


def test_a_deliberate_live_run_with_a_confirmed_account_proceeds():
    """Renamed. It asserts one outcome, and its old name claimed two.

    `test_live_requires_both_the_flag_and_a_confirmed_account` described a
    conjunction while asserting only this half, which is exactly the kind of
    name that gets believed. What the flag actually does is pinned below.
    """
    ib = FakeIB(accounts=[ACCOUNT])
    live = broker(ib, paper=False, port=7496)

    live.connect()

    assert ib.isConnected()


def test_the_paper_flag_is_read_by_nothing_but_the_port_warning():
    """The gap, pinned deliberately, because the README used to deny it.

    Live-path authority was ruled to be `paper = false` AND a named account AND
    the session returning it. Two of those three are enforced. The flag is not:
    it has no reader outside the config module, so a `paper = true` run naming a
    live account connects to it and trades -- and nothing objects.

    This is not fixable here. IBKR exposes no paper/live indicator, so there is
    nothing for the flag to be checked against; it can only be *recorded*. The
    run-level audit record is where it gets its first real reader, and this test
    is what makes that arrival announce itself instead of landing quietly.

    Enumerated rather than grepped: a substring search would count the word in
    docstrings and prose, which is how this gap stayed invisible.
    """
    import ast
    from pathlib import Path

    package = Path(__file__).resolve().parent.parent / "ibkr_trader"
    readers: dict[str, list[int]] = {}
    for module in sorted(package.glob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        lines = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr == "paper"
        ]
        if lines:
            readers[module.name] = lines

    assert set(readers) == {"config.py"}, (
        f"the paper flag acquired a reader outside config.py: {readers}. If that "
        f"reader is the audit record, this test has done its job -- update it to "
        f"state the new contract rather than deleting it."
    )


def test_a_paper_run_is_not_stopped_from_reaching_any_account_it_names():
    """The consequence of the above, stated as behaviour rather than structure.

    The account guard enforces identity, not mode: it asks whether the session
    reached the account you named, and `paper = true` gives it nothing further
    to ask. Naming a different account is what this guard catches; naming a
    live one it cannot.
    """
    live_looking = "U1234567"
    ib = FakeIB(accounts=[live_looking])

    broker(ib, paper=True, account=live_looking).connect()

    assert ib.isConnected(), "the flag stopped a connection it has no way to judge"


def test_live_with_the_wrong_account_still_refuses():
    """The guard is identity, not mode. It does not relax because live was chosen."""
    ib = FakeIB(accounts=[OTHER])

    with pytest.raises(BrokerNotConnected):
        broker(ib, paper=False, port=7496).connect()


# --- The operator-facing documents -----------------------------------------


def test_the_operator_documents_do_not_overclaim_what_the_check_proves():
    """The documents are half the deliverable, and nothing else guards them.

    An operator decides what to point this at by reading these two files, so a
    sentence here that overstates the guarantee is the same defect as a log line
    that does -- and the log line has a test. Both must state the non-proof, and
    neither may restate the enforcement claim the README carried until the
    verification round caught it.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent

    def prose(path):
        """Emphasis out and case flattened: `does *not* prove` and `does NOT prove`
        are the same statement, and neither file should have to phrase it one way
        to satisfy a test."""
        return (root / path).read_text(encoding="utf-8").replace("*", "").lower()

    readme = prose("README.md")
    example = prose("trader.example.toml")

    for name, text in (("README.md", readme), ("trader.example.toml", example)):
        assert "not prove" in text, f"{name} never states what the check cannot prove"
        assert "paper account" in text, f"{name} does not name the property at issue"

    withdrawn = [
        "neither alone is enough",
        "requires both `paper = false`",
    ]
    for claim in withdrawn:
        assert claim not in readme, (
            f"README.md claims {claim!r}, an enforcement of the paper flag that "
            f"does not exist; see test_the_paper_flag_is_read_by_nothing_but_the_port_warning"
        )


# --- Q1(c): defence in depth on the read side -----------------------------


def account_value(tag, value, account=ACCOUNT, currency="USD"):
    from types import SimpleNamespace

    return SimpleNamespace(tag=tag, value=value, currency=currency, account=account)


class AccountIB(FakeIB):
    def __init__(self, values=(), positions=()):
        super().__init__()
        self._values = list(values)
        self._positions = list(positions)

    def accountValues(self, account=""):
        return self._values

    def positions(self, account=""):
        return self._positions

    def openTrades(self):
        return []


def market_data(ib):
    config = build_config(settings())
    return IBKRMarketData(
        ibkr_config=config.ibkr,
        strategy_config=config.strategy,
        clock=FixedClock(SCAN_TIME),
        ib=ib,
    )


def test_account_rows_belonging_to_another_account_are_refused():
    """Vendor-side filtering is not something to depend on silently.

    `accountValues(account)` filters by account, so with the account required
    these rows should not arrive. If they do, something is wrong with an
    assumption -- and sizing against them is precisely the failure this whole
    item exists to prevent, so it fails loudly instead.
    """
    ib = AccountIB(
        values=[
            account_value("NetLiquidation", "50000", account=OTHER),
            account_value("BuyingPower", "25000", account=OTHER),
        ]
    )

    with pytest.raises(MarketDataError, match="account"):
        market_data(ib).portfolio()


def test_account_rows_spanning_two_accounts_are_refused():
    """The exact shape of the original defect, now impossible to construct.

    Unset, the two figures below came from a single flattened list and could
    resolve to different books. A portfolio blended from two accounts describes
    neither.
    """
    ib = AccountIB(
        values=[
            account_value("NetLiquidation", "50000"),
            account_value("BuyingPower", "25000", account=OTHER),
        ]
    )

    with pytest.raises(MarketDataError, match="account"):
        market_data(ib).portfolio()


def test_position_rows_from_another_account_are_refused():
    """Over-counting exposure is fail-safe; it is still not this account's book."""
    from types import SimpleNamespace

    ib = AccountIB(
        values=[
            account_value("NetLiquidation", "50000"),
            account_value("BuyingPower", "25000"),
        ],
        positions=[
            SimpleNamespace(
                account=OTHER,
                contract=SimpleNamespace(symbol="SPY", localSymbol="SPY"),
                position=1.0,
            )
        ],
    )

    with pytest.raises(MarketDataError, match="account"):
        market_data(ib).portfolio()


def test_a_clean_single_account_read_still_works():
    """The ordinary path must be undisturbed by all of the above."""
    ib = AccountIB(
        values=[
            account_value("NetLiquidation", "50000"),
            account_value("BuyingPower", "25000"),
        ]
    )

    portfolio = market_data(ib).portfolio()

    assert portfolio.net_liquidation == Decimal(50_000)
    assert portfolio.buying_power == Decimal(25_000)
    assert portfolio.pending_orders_known is True
