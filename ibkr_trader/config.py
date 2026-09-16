"""The one effective runtime configuration.

There is exactly one configuration model, and it is the same object execution
uses. There is no "loose policy" layer that validates permissively at startup
and a stricter runtime type constructed later during a scan.

That split is what allowed the prior ``refresh_limit = 300`` defect: policy
loading accepted it, and the constraint (``<= 200``) only bit once a scan was
already underway. Here the ceiling lives in :data:`MAX_REFRESH_LIMIT`, is
applied by the field constraint, and is quoted in the error message, so the
limit cannot drift away from the value that enforces it.

Every model is frozen (config cannot mutate mid-run) and forbids unknown keys
(a typo'd setting is an error, not a silently ignored default).
"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .errors import ConfigError

logger = logging.getLogger(__name__)

#: Hard ceiling on concurrent market-data refresh lines.
#:
#: IBKR accounts carry a finite number of simultaneous market-data lines; asking
#: for more than the runtime can hold produces mid-scan failures rather than a
#: startup error. This is the single source of truth for that bound.
MAX_REFRESH_LIMIT = 200


class _Base(BaseModel):
    """Frozen, closed-world base for every configuration section."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class IBKRConfig(_Base):
    """Connection settings for TWS / IB Gateway."""

    host: str = "127.0.0.1"
    port: int = Field(default=7497, ge=1, le=65535)
    client_id: int = Field(default=1, ge=0)
    #: The IBKR account this process trades. Required, and verified.
    #:
    #: Unset, ``ib.accountValues("")`` returns the *union* of every account
    #: under the login rather than a default one, and the account totals are
    #: then resolved by independent scans over that flat list -- so net
    #: liquidation and buying power could come from two different books and
    #: describe neither. Naming the account is what makes the reads and the
    #: order address the same place, and it is what
    #: :meth:`~ibkr_trader.broker.IBKRBroker.connect` checks the session against.
    account: str = Field(min_length=1)
    paper: bool = True
    connect_timeout_seconds: float = Field(default=10.0, gt=0, le=120)

    #: Concurrent market-data refresh lines. See :data:`MAX_REFRESH_LIMIT`.
    refresh_limit: int = Field(default=100, ge=1, le=MAX_REFRESH_LIMIT)

    #: IBKR market-data type: 1 live, 2 frozen, 3 delayed, 4 delayed-frozen.
    #:
    #: Live (1) is correct for trading. Options stop quoting outside regular
    #: hours, so a live run before the open finds no bid/ask and records data
    #: errors rather than trading -- which is the safe behaviour, not a bug.
    #: Frozen (2) returns the previous session's last quotes and is what makes
    #: an off-hours dry run possible. It is deliberately explicit: stale prices
    #: should never be used without someone choosing them.
    market_data_type: int = Field(default=1, ge=1, le=4)

    @model_validator(mode="after")
    def _warn_on_a_live_port_for_a_paper_run(self) -> IBKRConfig:
        """Warn -- not refuse -- when a paper run names a conventionally live port.

        This used to raise, and demoting it is deliberate. IBKR documents
        7496/7497/4001/4002 as *defaults* that "can be changed to any open
        socket port", and specifically warns about running paper and live TWS on
        one machine. So a port number is a hint about intent, never evidence
        about the session: a live TWS configured on 7497 passes this check, and
        an SSH tunnel or a container port-map makes that an ordinary deployment
        rather than an exotic one. Refusing on it would block those deployments
        while still missing the hazard it was written for.

        The enforcement lives where the evidence is: ``connect()`` requires the
        session to report the account this config names. That check reads the
        connection that actually opened rather than the number used to open it.

        What this warning knows is that two *configured* values disagree. It
        says nothing about whether the session is paper or live, because nothing
        at configuration time can.
        """
        live_ports = {7496, 4001}
        if self.paper and self.port in live_ports:
            logger.warning(
                "port %d is conventionally a live-trading port but paper=true; "
                "7497 (TWS) and 4002 (Gateway) are the paper defaults. This is a "
                "hint only -- the account check at connect is what enforces which "
                "book is traded.",
                self.port,
            )
        return self


class StrategyConfig(_Base):
    """Tastytrade short-put-vertical selection parameters.

    These are the algorithm's qualification criteria. They live here as data
    rather than as branching code, per the declarative/schema-first principle.
    """

    #: Sell premium only when volatility is historically rich.
    min_iv_rank: float = Field(default=30.0, ge=0.0, le=100.0)

    #: Preferred days to expiration, and the acceptable band around it.
    target_dte: int = Field(default=45, ge=1, le=365)
    min_dte: int = Field(default=30, ge=1, le=365)
    max_dte: int = Field(default=60, ge=1, le=365)

    #: Short-strike delta target and acceptable band (absolute value).
    short_delta_target: float = Field(default=0.30, gt=0.0, lt=1.0)
    min_short_delta: float = Field(default=0.20, gt=0.0, lt=1.0)
    max_short_delta: float = Field(default=0.40, gt=0.0, lt=1.0)

    #: Long-strike delta target and acceptable band (absolute value).
    #:
    #: The long put is chosen by delta, not by a fixed width: the spread is as
    #: wide as the distance between the 0.30-delta and 0.20-delta strikes
    #: happens to be. Width is therefore an output of selection, never an
    #: input, and the credit-to-width test below is applied to whatever width
    #: results.
    long_delta_target: float = Field(default=0.20, gt=0.0, lt=1.0)
    min_long_delta: float = Field(default=0.10, gt=0.0, lt=1.0)
    max_long_delta: float = Field(default=0.25, gt=0.0, lt=1.0)

    #: Minimum credit as a fraction of spread width (classic Tastytrade: 1/3).
    min_credit_ratio: float = Field(default=1.0 / 3.0, gt=0.0, lt=1.0)

    #: Floor on how far below spot to look for strikes, as a fraction of spot.
    #:
    #: The window must reach the long strike, and on a high-volatility
    #: underlying the 0.20-delta put sits well beyond 15% OTM. The scanner
    #: therefore widens this floor to ``strike_window_iv_multiple`` standard
    #: deviations over the longest admissible expiry, using the underlying's
    #: current implied volatility:
    #: ``max(strike_window_pct, multiple * iv * sqrt(max_dte / 365))``.
    #: Every extra percent costs contract-qualification round trips per scan.
    strike_window_pct: float = Field(default=0.15, gt=0.0, le=1.0)
    #: Set to 0 to disable the volatility scaling and use the floor alone.
    strike_window_iv_multiple: float = Field(default=1.2, ge=0.0, le=5.0)

    #: Liquidity screens applied to every leg. Bid/ask width is deliberately
    #: absent: it was a gate, it refused more symbols than every other rule
    #: combined, and it is now a ranking preference and a reviewer input
    #: instead. See ``tastytrade._liquidity_failure``.
    min_open_interest: int = Field(default=100, ge=0)
    min_volume: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _bands_are_coherent(self) -> StrategyConfig:
        """Reject bands that can never select anything."""
        if self.min_dte > self.max_dte:
            raise ValueError(
                f"min_dte ({self.min_dte}) must not exceed max_dte ({self.max_dte})"
            )
        if not self.min_dte <= self.target_dte <= self.max_dte:
            raise ValueError(
                f"target_dte ({self.target_dte}) must lie within "
                f"[min_dte, max_dte] = [{self.min_dte}, {self.max_dte}]"
            )
        if self.min_short_delta > self.max_short_delta:
            raise ValueError(
                f"min_short_delta ({self.min_short_delta}) must not exceed "
                f"max_short_delta ({self.max_short_delta})"
            )
        if not self.min_short_delta <= self.short_delta_target <= self.max_short_delta:
            raise ValueError(
                f"short_delta_target ({self.short_delta_target}) must lie within "
                f"[{self.min_short_delta}, {self.max_short_delta}]"
            )
        if self.min_long_delta > self.max_long_delta:
            raise ValueError(
                f"min_long_delta ({self.min_long_delta}) must not exceed "
                f"max_long_delta ({self.max_long_delta})"
            )
        if not self.min_long_delta <= self.long_delta_target <= self.max_long_delta:
            raise ValueError(
                f"long_delta_target ({self.long_delta_target}) must lie within "
                f"[{self.min_long_delta}, {self.max_long_delta}]"
            )
        # The long put defines risk below the short put, so it must be the
        # further-out-of-the-money leg. Equal targets would select the same
        # strike for both legs: a spread of zero width and zero credit.
        if self.long_delta_target >= self.short_delta_target:
            raise ValueError(
                f"long_delta_target ({self.long_delta_target}) must be below "
                f"short_delta_target ({self.short_delta_target})"
            )
        return self


class RiskConfig(_Base):
    """Position sizing and portfolio-level ceilings."""

    #: Maximum defined risk of one trade as a fraction of net liquidation value.
    max_risk_per_trade: float = Field(default=0.02, gt=0.0, le=1.0)

    #: Ceiling on contracts in a single order, regardless of account size.
    max_contracts: int = Field(default=10, ge=1)

    #: Maximum number of distinct underlyings held at once.
    max_positions: int = Field(default=10, ge=1)

    #: Whether a second position may be opened in an underlying already held.
    allow_duplicate_symbol: bool = False


class ReviewerConfig(_Base):
    """Independent reviewer settings.

    Note what is absent: no heartbeat interval, no liveness deadline, no session
    lease. The reviewer is invoked per proposal and has no lifecycle.
    """

    #: Which transport carries the review.
    #:
    #: ``claude_code`` runs the Claude Code CLI as a subprocess, billed to the
    #: subscription ``claude`` is logged in as; no API key is read or needed.
    #: ``anthropic_api`` calls the API directly and requires
    #: ``ANTHROPIC_API_KEY``. A Literal rather than a free string so a typo is a
    #: startup error, not a silent fall-through to whichever branch is last.
    backend: Literal["claude_code", "anthropic_api"] = "claude_code"
    #: The CLI executable for the ``claude_code`` backend, resolved on PATH at
    #: each review. Ignored by ``anthropic_api``.
    command: str = Field(default="claude", min_length=1)
    model: str = "claude-sonnet-5"
    timeout_seconds: float = Field(default=90.0, gt=0, le=600)
    # Output ceiling for the ``anthropic_api`` backend only; the CLI owns its
    # own budget and never sees this value.
    #
    # Thinking tokens are output tokens on an adaptive-thinking model, and the
    # JSON verdict has to fit in the same budget. 1024 risked truncating every
    # review -- a permanent all-reviews-fail mode, not an occasional one.
    max_tokens: int = Field(default=8192, ge=64, le=32768)


class ManagementConfig(_Base):
    """How an open spread is worked after the fill.

    Tastytrade mechanics: take profit at half the credit, and at 21 days to
    expiration stop passively holding -- roll for a credit if one exists,
    otherwise close. Nothing here ever adds contracts or widens a spread.
    """

    #: Buy the spread back when its value falls to this fraction of the credit.
    profit_target_ratio: float = Field(default=0.5, gt=0.0, lt=1.0)

    #: Days to expiration at or below which a spread is managed, not held.
    manage_dte: int = Field(default=21, ge=0, le=365)

    #: Attempt a roll to the next cycle at ``manage_dte``. When False, or when
    #: no roll is available for a net credit, the spread is closed instead.
    roll: bool = True


class RunConfig(_Base):
    """Top-level runtime configuration."""

    universe: tuple[str, ...] = Field(min_length=1)
    #: Required, and deliberately without a default: ``ibkr.account`` must be
    #: named, so the connection settings cannot be conjured from nothing.
    ibkr: IBKRConfig
    strategy: StrategyConfig = StrategyConfig()
    risk: RiskConfig = RiskConfig()
    reviewer: ReviewerConfig = ReviewerConfig()
    management: ManagementConfig = ManagementConfig()

    #: Where the durable record lives.
    database_path: Path = Path("ibkr_trader.sqlite3")

    #: Seconds between the *start* of one pass and the start of the next when
    #: running continuously. A period, not a gap: a pass that takes fifteen
    #: minutes on a forty-five minute interval is followed by thirty minutes of
    #: waiting, not forty-five. A pass that overruns the period starts the next
    #: one immediately. Ignored for a single pass.
    scan_interval_seconds: float = Field(default=300.0, gt=0, le=86_400)

    #: Seconds between management-only passes taken *while waiting* for the next
    #: scan. Management is cheap and wants to happen soon after a fill; the scan
    #: is expensive and does not. A management pass is skipped entirely when the
    #: book holds nothing, so an idle session costs nothing extra.
    #:
    #: Not constrained against ``scan_interval_seconds``: when it is the longer
    #: of the two the wait simply never reaches one, which is a coherent
    #: "only manage on the scan cadence" and needs no error.
    manage_interval_seconds: float = Field(default=900.0, gt=0, le=86_400)

    @model_validator(mode="after")
    def _management_precedes_entry(self) -> RunConfig:
        """A spread must be entered strictly before it becomes manageable."""
        if self.management.manage_dte >= self.strategy.min_dte:
            raise ValueError(
                f"management.manage_dte ({self.management.manage_dte}) must be below "
                f"strategy.min_dte ({self.strategy.min_dte}); otherwise a spread would "
                f"be managed on the day it was opened"
            )
        return self

    @model_validator(mode="after")
    def _universe_is_clean(self) -> RunConfig:
        """Reject blank, lowercase-ambiguous, or duplicated symbols."""
        seen: set[str] = set()
        for raw in self.universe:
            symbol = raw.strip()
            if not symbol:
                raise ValueError("universe contains an empty symbol")
            if symbol != symbol.upper():
                raise ValueError(
                    f"universe symbol {raw!r} must be upper case (use {symbol.upper()!r})"
                )
            if symbol in seen:
                raise ValueError(f"universe contains duplicate symbol {symbol!r}")
            seen.add(symbol)
        return self


def _format_validation_error(error: ValidationError, source: str) -> str:
    """Render a pydantic failure as the operator-facing report §7 requires.

    Names the exact field, the supplied value, and the constraint that rejected
    it, so the fix is obvious without reading the source.
    """
    lines = [f"Invalid configuration in {source}:"]
    for item in error.errors():
        location = ".".join(str(part) for part in item["loc"]) or "<root>"
        supplied = item.get("input", "<missing>")
        constraint = item["msg"]
        lines.append(f"  field:      {location}")
        lines.append(f"  supplied:   {supplied!r}")
        lines.append(f"  constraint: {constraint}")
        context = item.get("ctx")
        if context:
            bounds = ", ".join(f"{k}={v}" for k, v in sorted(context.items()))
            if bounds:
                lines.append(f"  limit:      {bounds}")
        lines.append("")
    return "\n".join(lines).rstrip()


def build_config(data: dict[str, Any], source: str = "<memory>") -> RunConfig:
    """Construct the complete runtime configuration, or fail.

    This is the only way configuration is created. It performs *all* validation,
    including every cross-field invariant, so a returned :class:`RunConfig` is
    known-usable and nothing downstream needs to re-check it.

    Raises:
        ConfigError: with the offending field, its value, and the constraint.
    """
    try:
        return RunConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc, source)) from exc


def load_config(path: str | Path) -> RunConfig:
    """Load and fully validate configuration from a TOML file.

    Raises:
        ConfigError: the file is missing, unparsable, or invalid.
    """
    config_path = Path(path)
    try:
        raw = config_path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"Cannot read configuration file {config_path}: {exc}") from exc

    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"Cannot parse configuration file {config_path}: {exc}") from exc

    return build_config(data, source=str(config_path))
