"""The ``BAG`` contract, assembled in exactly one place.

A combo is quoted and traded as a synthetic contract whose legs are named only
by ``conId``. Two callers need one: :mod:`broker` builds the bag it transmits,
and :mod:`scanner` builds the bag it asks IBKR to quote. If those two ever
drifted apart -- a different exchange, a leg action inverted, a ratio dropped --
the measured market would describe a spread nobody is about to trade, and the
mismatch would be invisible: both would still be well-formed bags returning
plausible numbers.

So the assembly lives here, once, and both import it. The caller still owns
qualification, because the two paths fail differently: a leg the broker cannot
resolve aborts a submission, while a leg the scanner cannot resolve costs a
measurement and nothing else.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .models import Action, ComboLeg


def leg_action(action: Action) -> str:
    """The wire spelling of a leg's side.

    ``ComboLeg.action`` on the wire is the plain ``BUY``/``SELL`` string, and it
    is the leg's own side -- not the bag's. The bag always goes out as a ``BUY``
    (see ``broker._combo_action``); it is these per-leg actions that make it a
    short put vertical rather than a long one.
    """
    return action.value


def bag_contract(
    api: Any,
    symbol: str,
    legs: Sequence[ComboLeg],
    con_ids: Sequence[int],
    exchange: str,
    currency: str,
) -> Any:
    """The ``BAG`` contract for ``legs``, already resolved to ``con_ids``.

    Args:
        api: The ``ib_async`` module surface, passed in rather than imported so
            the vendor import stays lazy on both call paths.
        symbol: The underlying, which the bag carries as its own symbol.
        legs: The legs, in the order the caller means them.
        con_ids: Each leg's resolved contract id, positionally matched to
            ``legs``. Qualification is the caller's job; a zero here is a
            programming error, not a market condition, and raises.
        exchange: Routing venue, ``SMART`` on both current call paths.
        currency: Contract currency.

    Raises:
        ValueError: ``con_ids`` does not line up with ``legs``, or a leg
            resolved to no contract id. Either one means the bag would not be
            the spread the caller has in mind.
    """
    if len(con_ids) != len(legs):
        raise ValueError(f"{len(con_ids)} contract id(s) for {len(legs)} leg(s) of {symbol}")

    combo_legs = []
    for leg, con_id in zip(legs, con_ids, strict=True):
        if not con_id:
            raise ValueError(
                f"unresolved leg {leg.leg.symbol} {leg.leg.expiry} "
                f"{leg.leg.strike} {leg.leg.right.value} in {symbol} bag"
            )
        combo_legs.append(
            api.ComboLeg(
                conId=int(con_id),
                ratio=leg.ratio,
                action=leg_action(leg.action),
                exchange=exchange,
            )
        )

    return api.Contract(
        secType="BAG",
        symbol=symbol,
        exchange=exchange,
        currency=currency,
        comboLegs=combo_legs,
    )
