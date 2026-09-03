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

import tomllib
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .errors import ConfigError

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
    account: str | None = None
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
    def _paper_port_is_consistent(self) -> IBKRConfig:
        """Reject a live-trading port while the run claims to be paper-only.

        V4 is paper-first. 7496/4001 are the live TWS/Gateway ports; silently
        pointing a "paper" run at them is the one configuration mistake that
        loses real money.
        """
        live_ports = {7496, 4001}
        if self.paper and self.port in live_ports:
            raise ValueError(
                f"port {self.port} is a live-trading port but paper=true; "
                f"use 7497 (TWS paper) or 4002 (Gateway paper), "
                f"or set paper=false deliberately"
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

    #: Width of the vertical, in strike points.
    spread_width: Decimal = Field(default=Decimal(5), gt=0)

    #: Minimum credit as a fraction of spread width (classic Tastytrade: 1/3).
    min_credit_ratio: float = Field(default=1.0 / 3.0, gt=0.0, lt=1.0)

    #: How far below spot to look for strikes, as a fraction of spot.
    #:
    #: Must cover the short strike (0.20-0.40 delta, typically 3-10% OTM) plus
    #: the long strike a further ``spread_width`` below it. Widen it for
    #: high-volatility underlyings, whose target delta sits further out; every
    #: extra percent costs contract-qualification round trips on every scan.
    strike_window_pct: float = Field(default=0.15, gt=0.0, le=1.0)

    #: Liquidity screens applied to every leg.
    max_spread_pct: float = Field(default=0.10, gt=0.0, le=1.0)
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

    model: str = "claude-sonnet-5"
    timeout_seconds: float = Field(default=90.0, gt=0, le=600)
    max_tokens: int = Field(default=1024, ge=64, le=8192)


class RunConfig(_Base):
    """Top-level runtime configuration."""

    universe: tuple[str, ...] = Field(min_length=1)
    ibkr: IBKRConfig = IBKRConfig()
    strategy: StrategyConfig = StrategyConfig()
    risk: RiskConfig = RiskConfig()
    reviewer: ReviewerConfig = ReviewerConfig()

    #: Where the durable record lives.
    database_path: Path = Path("ibkr_trader.sqlite3")

    #: Seconds between passes when running continuously. Ignored for a single pass.
    scan_interval_seconds: float = Field(default=300.0, gt=0, le=86_400)

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
