"""Configuration must fail before anything runs.

The governing defect: a policy layer accepted ``refresh_limit = 300`` while the
runtime type that actually used it only allowed ``<= 200``, so the contradiction
surfaced mid-scan instead of at startup. V4 has one configuration model, so that
class of bug has nowhere to live — and this suite pins it shut.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ibkr_trader.config import MAX_REFRESH_LIMIT, build_config, load_config
from ibkr_trader.errors import ConfigError

from .fakes import ACCOUNT


def minimal(**overrides) -> dict:
    """Smallest valid settings dict, with targeted overrides."""
    settings: dict = {"universe": ["AAPL"], "ibkr": {"account": ACCOUNT}}
    for key, value in overrides.items():
        # Merge the ibkr block rather than replacing it. Every config now needs
        # an account, so a targeted override of one connection field must not
        # silently drop it.
        if key == "ibkr" and isinstance(value, dict):
            settings["ibkr"] = {**settings["ibkr"], **value}
        else:
            settings[key] = value
    return settings


# --- the regression -------------------------------------------------------


def test_refresh_limit_above_ceiling_is_rejected_at_construction():
    """The exact prior failure: 300 must not survive configuration loading.

    Asserted against :data:`MAX_REFRESH_LIMIT` rather than a literal 200, so
    raising the real ceiling cannot leave this test silently passing for the
    wrong reason.
    """
    with pytest.raises(ConfigError) as exc_info:
        build_config(minimal(ibkr={"refresh_limit": 300}))

    message = str(exc_info.value)
    assert "ibkr.refresh_limit" in message, "the offending field must be named"
    assert "300" in message, "the supplied value must be shown"
    assert str(MAX_REFRESH_LIMIT) in message, "the actual constraint must be shown"


def test_refresh_limit_at_the_ceiling_is_accepted():
    """The boundary itself is valid; the rejection is of values above it."""
    config = build_config(minimal(ibkr={"refresh_limit": MAX_REFRESH_LIMIT}))
    assert config.ibkr.refresh_limit == MAX_REFRESH_LIMIT


def test_refresh_limit_ceiling_is_the_value_the_runtime_uses():
    """There is one ceiling, and the validator enforces that same one.

    This is the invariant the original bug violated: the number that validates
    and the number the runtime honours must be the same object, not two
    constants that agree by coincidence.
    """
    field = type(build_config(minimal())).model_fields["ibkr"]
    ibkr_model = field.annotation
    constraints = ibkr_model.model_fields["refresh_limit"].metadata
    ceilings = [getattr(c, "le", None) for c in constraints]
    assert MAX_REFRESH_LIMIT in ceilings


# --- failing loudly and early ---------------------------------------------


def test_config_error_reports_field_value_and_constraint():
    """§7's report format: what was wrong, what you gave, what is allowed."""
    with pytest.raises(ConfigError) as exc_info:
        build_config(minimal(risk={"max_risk_per_trade": 5.0}))

    message = str(exc_info.value)
    assert "risk.max_risk_per_trade" in message
    assert "5.0" in message
    assert "field:" in message and "supplied:" in message and "constraint:" in message


def test_unknown_setting_is_rejected_rather_than_ignored():
    """A typo'd key must not silently fall back to a default."""
    with pytest.raises(ConfigError) as exc_info:
        build_config(minimal(scan_interval_second=60))
    assert "scan_interval_second" in str(exc_info.value)


def test_empty_universe_is_rejected():
    """A run with nothing to scan is a configuration mistake, not a no-op."""
    with pytest.raises(ConfigError):
        build_config({"universe": [], "ibkr": {"account": ACCOUNT}})


def test_duplicate_symbol_is_rejected():
    """Duplicates would double-size a position without saying so."""
    with pytest.raises(ConfigError) as exc_info:
        build_config({"universe": ["AAPL", "AAPL"], "ibkr": {"account": ACCOUNT}})
    assert "duplicate" in str(exc_info.value).lower()


def test_lowercase_symbol_is_rejected():
    """Symbols are compared by identity elsewhere; casing must be settled here."""
    with pytest.raises(ConfigError) as exc_info:
        build_config({"universe": ["aapl"], "ibkr": {"account": ACCOUNT}})
    assert "AAPL" in str(exc_info.value)


# --- cross-field invariants ----------------------------------------------


def test_incoherent_dte_band_is_rejected():
    """A band that can never select an expiry is invalid, not merely unlucky."""
    with pytest.raises(ConfigError) as exc_info:
        build_config(minimal(strategy={"min_dte": 60, "max_dte": 30}))
    assert "min_dte" in str(exc_info.value)


def test_target_dte_outside_band_is_rejected():
    """A target the band excludes is a contradiction between two valid fields.

    Exactly the shape of the original defect: each field passes on its own, and
    only the combination is impossible.
    """
    with pytest.raises(ConfigError) as exc_info:
        build_config(minimal(strategy={"min_dte": 30, "max_dte": 45, "target_dte": 60}))
    assert "target_dte" in str(exc_info.value)


def test_delta_band_excluding_its_target_is_rejected():
    with pytest.raises(ConfigError) as exc_info:
        build_config(
            minimal(
                strategy={
                    "min_short_delta": 0.10,
                    "max_short_delta": 0.20,
                    "short_delta_target": 0.30,
                }
            )
        )
    assert "short_delta_target" in str(exc_info.value)


def test_paper_run_pointed_at_a_live_port_warns_but_builds():
    """Demoted from a refusal, deliberately.

    IBKR documents these ports as *defaults* that can be changed to any open
    socket, so the number is a hint about intent and never evidence about the
    session. Enforcement moved to the account check at connect, which reads the
    connection that actually opened. See ``tests/test_account_identity.py``,
    which owns both ports and the identity gate.
    """
    config = build_config(minimal(ibkr={"paper": True, "port": 7496}))

    assert config.ibkr.port == 7496


def test_live_port_is_allowed_when_paper_is_explicitly_disabled():
    """The guard blocks an inconsistency, not a deliberate choice."""
    config = build_config(minimal(ibkr={"paper": False, "port": 7496}))
    assert config.ibkr.port == 7496


# --- the config object is immutable --------------------------------------


def test_config_cannot_be_mutated_after_validation():
    """Validated-then-mutated is the same failure mode by another route."""
    config = build_config(minimal())
    with pytest.raises(ValidationError):
        config.ibkr.refresh_limit = 300


# --- file loading ---------------------------------------------------------


def test_load_config_reads_toml(tmp_path):
    path = tmp_path / "trader.toml"
    path.write_text(
        f"universe = ['AAPL', 'MSFT']\n\n[ibkr]\naccount = '{ACCOUNT}'\nrefresh_limit = 50\n",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.universe == ("AAPL", "MSFT")
    assert config.ibkr.refresh_limit == 50


def test_load_config_rejects_bad_value_from_file(tmp_path):
    """The file path enforces identical constraints to the in-memory path."""
    path = tmp_path / "trader.toml"
    path.write_text(
        f"universe = ['AAPL']\n\n[ibkr]\naccount = '{ACCOUNT}'\nrefresh_limit = 300\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as exc_info:
        load_config(path)
    assert "refresh_limit" in str(exc_info.value)
    assert str(path) in str(exc_info.value), "the report must name the offending file"


def test_missing_config_file_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.toml")


def test_malformed_toml_is_a_config_error(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text("universe = [", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)
