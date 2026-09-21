"""The shared ``BAG`` constructor.

One bag builder serves two callers -- the broker transmits what it returns, the
scanner quotes it -- so its guarantees are what make "the market we measured is
the market the order meets" a fact rather than a hope.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from ibkr_trader.bag import bag_contract
from ibkr_trader.models import Action, ComboLeg, OptionLeg, Right

FAKE_API = SimpleNamespace(
    Contract=lambda **kw: SimpleNamespace(**kw),
    ComboLeg=lambda **kw: SimpleNamespace(**kw),
)

EXPIRY = date(2026, 10, 30)
LEGS = (
    ComboLeg(OptionLeg("AAPL", EXPIRY, Decimal("185"), Right.PUT), Action.SELL),
    ComboLeg(OptionLeg("AAPL", EXPIRY, Decimal("180"), Right.PUT), Action.BUY),
)


def test_the_bag_carries_each_leg_s_own_side_not_the_bag_s():
    """The bag always goes out as a BUY; the legs are what make it a short
    put vertical rather than a long one."""
    bag = bag_contract(FAKE_API, "AAPL", LEGS, [101, 102], "SMART", "USD")

    assert bag.secType == "BAG"
    assert bag.symbol == "AAPL"
    assert bag.exchange == "SMART"
    assert bag.currency == "USD"
    assert [(leg.conId, leg.action, leg.ratio) for leg in bag.comboLegs] == [
        (101, "SELL", 1),
        (102, "BUY", 1),
    ]


def test_an_unresolved_leg_is_refused_rather_than_sent_as_contract_zero():
    """``conId=0`` is not a contract. A bag built with one is well-formed and
    wrong -- the failure mode worth making loud."""
    with pytest.raises(ValueError, match="unresolved leg AAPL"):
        bag_contract(FAKE_API, "AAPL", LEGS, [101, 0], "SMART", "USD")


def test_contract_ids_that_do_not_line_up_with_the_legs_are_refused():
    """Positional matching is the whole contract between caller and builder."""
    with pytest.raises(ValueError, match="1 contract id\\(s\\) for 2 leg\\(s\\)"):
        bag_contract(FAKE_API, "AAPL", LEGS, [101], "SMART", "USD")
