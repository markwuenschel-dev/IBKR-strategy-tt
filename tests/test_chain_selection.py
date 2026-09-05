"""Which of IBKR's several chain rows gets used, and what happens if it is odd.

``reqSecDefOptParams`` returns one row per *(exchange, tradingClass)* pair, not
one per exchange as the old docstring said. ``_preferred_chain`` picked among
them on strike count alone: it never took the underlying symbol as a parameter
and never read ``tradingClass``. So the row describing a non-standard class --
``AAPL1``, an adjusted contract from a split or a special dividend -- wins
whenever it happens to list more strikes.

No test covered multi-row selection at all. The one test that reached
``_preferred_chain`` passed a single-element list, so the rule was never
exercised on the input it exists to handle.

Two claims the original candidate made are NOT tested here because they were
contradicted on recheck and withdrawn:

* that the exchange is taken from a different row than the trading class. It is
  not: the selected row's ``.exchange`` is read at exactly one line in the whole
  package, inside the selection itself, and on the SMART path it equals
  ``EXCHANGE`` by construction. The proposed "use the chosen row's own exchange"
  fix is contradicted by ``ib_async``'s own source, which preserves ``SMART``
  deliberately because overwriting it "can create invalid contract".
* that a bad selection surfaces silently. It does not -- every contract failing
  to qualify raises ``MarketDataError`` and the runner records a named
  per-symbol data error.

What a wrong class actually costs is a *different* defect, in a different file:
``broker._build_option`` omits ``tradingClass`` entirely, so the broker would
resolve and trade the standard contract while the quote and the review referred
to the non-standard one. That is tracked separately. Until it is fixed, a
proposal on a non-standard class is refused before submission -- see
``test_escalation.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ibkr_trader.clock import FixedClock
from ibkr_trader.config import build_config
from ibkr_trader.scanner import EXCHANGE, IBKRMarketData

from .fakes import SCAN_TIME


def row(trading_class, strikes, exchange=EXCHANGE, expirations=("20260220",)):
    return SimpleNamespace(
        exchange=exchange,
        tradingClass=trading_class,
        strikes=list(strikes),
        expirations=list(expirations),
    )


def adapter():
    config = build_config({"universe": ["AAPL"]})
    return IBKRMarketData(
        ibkr_config=config.ibkr,
        strategy_config=config.strategy,
        clock=FixedClock(SCAN_TIME),
        ib=object(),
    )


# --- INT-023, the part that survived recheck ----------------------------


def test_the_row_matching_the_underlying_wins_over_a_longer_one():
    """The whole fix, in one assertion.

    Strike count decided this before, so the adjusted class won purely by being
    larger. Options on an adjusted class are a different instrument with a
    different deliverable; selecting it is not a more complete view of the same
    options, which is what the old docstring assumed.
    """
    chains = [row("AAPL1", range(100, 200)), row("AAPL", range(100, 120))]

    chosen = adapter()._preferred_chain(chains, "AAPL")

    assert chosen.tradingClass == "AAPL"
    assert len(chosen.strikes) == 20, "the shorter matching row is still the right one"


def test_strike_count_still_decides_between_equally_valid_rows():
    """The old rule survives where it was never wrong.

    Two rows for the same class -- IBKR does return these -- are genuinely two
    views of the same options, and the fuller one is better.
    """
    chains = [row("AAPL", range(100, 110)), row("AAPL", range(100, 190))]

    chosen = adapter()._preferred_chain(chains, "AAPL")

    assert len(chosen.strikes) == 90


def test_a_non_standard_class_is_still_used_when_it_is_the_only_one():
    """Refusing to return anything would be worse than returning the odd row.

    The caller can tell -- the snapshot carries the class -- and the decision
    about whether to trade it belongs downstream, not here.
    """
    chains = [row("AAPL1", range(100, 120))]

    chosen = adapter()._preferred_chain(chains, "AAPL")

    assert chosen.tradingClass == "AAPL1"


def test_smart_routing_still_outranks_a_matching_class_elsewhere():
    """Exchange preference is untouched by this change, deliberately.

    Selecting a non-SMART row would move the adapter off the routing it quotes
    and trades on, and ib_async preserves SMART on qualification for exactly
    that reason.
    """
    chains = [row("AAPL", range(100, 200), exchange="AMEX"), row("AAPL1", range(100, 110))]

    chosen = adapter()._preferred_chain(chains, "AAPL")

    assert chosen.exchange == EXCHANGE


def test_a_row_with_no_expirations_is_never_selected():
    """Pre-existing behaviour, pinned because the new rule reorders the filters."""
    chains = [row("AAPL", range(100, 200), expirations=()), row("AAPL1", [100, 105])]

    chosen = adapter()._preferred_chain(chains, "AAPL")

    assert chosen.tradingClass == "AAPL1"


def test_no_usable_row_returns_nothing():
    assert adapter()._preferred_chain([], "AAPL") is None
    assert adapter()._preferred_chain([row("AAPL", [100], expirations=())], "AAPL") is None


@pytest.mark.parametrize("missing", [None, ""])
def test_a_row_without_a_trading_class_does_not_masquerade_as_a_match(missing):
    """An absent class must not be read as matching the symbol.

    ``_chain_contracts`` falls back to the symbol when the attribute is empty,
    which is right for *building* a contract but would be wrong here: it would
    make an unlabelled row indistinguishable from a confirmed standard one.
    """
    standard = row("AAPL", [100, 105])
    unlabelled = row(missing, range(100, 200))

    chosen = adapter()._preferred_chain([unlabelled, standard], "AAPL")

    assert chosen is standard
