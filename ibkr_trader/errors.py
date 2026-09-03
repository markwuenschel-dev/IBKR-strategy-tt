"""Typed domain errors.

Principle: no silent failures, no bare ``except``. Every adapter translates its
transport-specific failures into one of these, so the runner branches on
*meaning* rather than on the incidental exception type of some library.
"""

from __future__ import annotations


class TraderError(Exception):
    """Base for every error this application raises deliberately."""


class ConfigError(TraderError):
    """Runtime configuration is unusable. Raised before anything connects."""


class MarketDataError(TraderError):
    """Market data or option chain could not be obtained for a symbol."""


class ReviewTimeout(TraderError):
    """The independent reviewer did not answer within its deadline."""


class ReviewError(TraderError):
    """The reviewer answered, but the answer was unusable (malformed/unparsable).

    Deliberately distinct from :class:`ReviewTimeout`: a garbled answer and a
    missing answer are different operational facts. Both are treated as "no
    trade", never as approval.
    """


class BrokerError(TraderError):
    """Base for broker-side failures."""


class BrokerNotConnected(BrokerError):
    """The broker connection is unusable, so submission cannot be attempted."""


class SubmissionFailed(BrokerError):
    """The order was definitively *not* accepted; it never reached the venue."""


class ExecutionAmbiguous(BrokerError):
    """Submission outcome is genuinely unknown.

    Raised only when the connection dropped mid-transmission, so we can neither
    confirm nor rule out that the order reached IBKR. This is the one failure
    that legitimately requires reconciliation, and it is scoped to the single
    order. It never latches global state.
    """

    def __init__(self, message: str, order_ref: str) -> None:
        super().__init__(message)
        self.order_ref = order_ref
