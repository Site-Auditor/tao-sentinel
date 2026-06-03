"""Tests for configuration loading/saving and API-key resolution.

Covers:

* A YAML round-trip: a written config reloads into an equivalent
  :class:`~tao_sentinel.config.Config`.
* The ``env:VARNAME`` indirection for ``api_key`` resolved at load time.
* The ``TAOSTATS_API_KEY`` environment-variable fallback.
* The bundled example config writes and parses cleanly.

Environment variables are manipulated only through ``monkeypatch`` so the tests
remain isolated and never depend on the ambient environment.
"""

from __future__ import annotations

import os
import stat
import textwrap

import pydantic
import pytest

from tao_sentinel.config import (
    API_KEY_ENV_FALLBACK,
    WATCH_TYPES,
    Config,
    TelegramConfig,
    WatchConfig,
    load_config,
    write_example_config,
)


def _write(path, text: str) -> str:
    """Write ``text`` (dedented) to ``path`` and return the path as a string."""
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return str(path)


@pytest.fixture(autouse=True)
def _clear_fallback_env(monkeypatch):
    """Ensure the fallback env var is unset unless a test sets it explicitly."""
    monkeypatch.delenv(API_KEY_ENV_FALLBACK, raising=False)


# --------------------------------------------------------------------------- #
# YAML round-trip
# --------------------------------------------------------------------------- #


def test_yaml_round_trip(tmp_path, monkeypatch):
    """A config serialized to YAML reloads into an equivalent Config."""
    monkeypatch.delenv(API_KEY_ENV_FALLBACK, raising=False)
    original = Config(
        api_key="tao-rawkey:abcd",
        telegram=TelegramConfig(bot_token="123:ABC", chat_id="999"),
        webhook_url="https://example.com/hook",
        watches=[
            WatchConfig(type="price_change", netuid=1, threshold_pct=12.5),
            WatchConfig(type="stake_change", coldkey="5Cold", threshold_pct=5.0),
        ],
        poll_interval_seconds=120,
        state_path="~/.tao-sentinel/state.json",
    )

    import yaml

    path = tmp_path / "round.yaml"
    path.write_text(yaml.safe_dump(original.model_dump()), encoding="utf-8")

    loaded = load_config(str(path))

    assert loaded.api_key == "tao-rawkey:abcd"  # raw key passes through verbatim
    assert loaded.webhook_url == "https://example.com/hook"
    assert loaded.poll_interval_seconds == 120
    assert loaded.telegram is not None
    assert loaded.telegram.bot_token == "123:ABC"
    assert loaded.telegram.chat_id == "999"
    assert len(loaded.watches) == 2
    assert loaded.watches[0].type == "price_change"
    assert loaded.watches[0].netuid == 1
    assert loaded.watches[0].threshold_pct == pytest.approx(12.5)
    assert loaded.watches[1].type == "stake_change"
    assert loaded.watches[1].coldkey == "5Cold"


def test_defaults_applied_for_sparse_config(tmp_path, monkeypatch):
    """Omitted fields fall back to their documented defaults."""
    monkeypatch.delenv(API_KEY_ENV_FALLBACK, raising=False)
    path = _write(
        tmp_path / "sparse.yaml",
        """
        watches: []
        """,
    )
    config = load_config(path)

    assert config.poll_interval_seconds == 3600
    assert config.state_path == "~/.tao-sentinel/state.json"
    assert config.watches == []
    assert config.telegram is None
    assert config.api_key is None  # nothing configured, no fallback set


def test_watch_threshold_defaults_to_ten(tmp_path):
    """A watch without an explicit threshold defaults to 10.0%."""
    path = _write(
        tmp_path / "w.yaml",
        """
        watches:
          - type: emission_shift
            netuid: 8
        """,
    )
    config = load_config(path)
    assert config.watches[0].threshold_pct == pytest.approx(10.0)


# --------------------------------------------------------------------------- #
# env: indirection
# --------------------------------------------------------------------------- #


def test_env_indirection_resolves_at_load(tmp_path, monkeypatch):
    """``api_key: env:VARNAME`` reads VARNAME from the environment at load."""
    monkeypatch.setenv("MY_TAO_KEY", "tao-resolved-from-env:zzzz")
    path = _write(
        tmp_path / "env.yaml",
        """
        api_key: env:MY_TAO_KEY
        watches: []
        """,
    )
    config = load_config(path)
    assert config.api_key == "tao-resolved-from-env:zzzz"


def test_env_indirection_missing_var_falls_back_then_none(tmp_path, monkeypatch):
    """An unset indirection var with no fallback resolves to ``None``."""
    monkeypatch.delenv("ABSENT_VAR", raising=False)
    monkeypatch.delenv(API_KEY_ENV_FALLBACK, raising=False)
    path = _write(
        tmp_path / "env_missing.yaml",
        """
        api_key: env:ABSENT_VAR
        watches: []
        """,
    )
    config = load_config(path)
    assert config.api_key is None


def test_env_indirection_missing_var_uses_fallback(tmp_path, monkeypatch):
    """An unset indirection var falls back to TAOSTATS_API_KEY when present."""
    monkeypatch.delenv("ABSENT_VAR", raising=False)
    monkeypatch.setenv(API_KEY_ENV_FALLBACK, "tao-fallback-key:ffff")
    path = _write(
        tmp_path / "env_fallback.yaml",
        """
        api_key: env:ABSENT_VAR
        watches: []
        """,
    )
    config = load_config(path)
    assert config.api_key == "tao-fallback-key:ffff"


# --------------------------------------------------------------------------- #
# TAOSTATS_API_KEY fallback
# --------------------------------------------------------------------------- #


def test_fallback_env_used_when_no_api_key_configured(tmp_path, monkeypatch):
    """An entirely absent api_key falls back to TAOSTATS_API_KEY."""
    monkeypatch.setenv(API_KEY_ENV_FALLBACK, "tao-ambient-key:eeee")
    path = _write(
        tmp_path / "nokey.yaml",
        """
        watches: []
        """,
    )
    config = load_config(path)
    assert config.api_key == "tao-ambient-key:eeee"


def test_explicit_key_takes_precedence_over_fallback(tmp_path, monkeypatch):
    """A literal api_key wins over the TAOSTATS_API_KEY fallback."""
    monkeypatch.setenv(API_KEY_ENV_FALLBACK, "tao-ambient-key:eeee")
    path = _write(
        tmp_path / "explicit.yaml",
        """
        api_key: tao-explicit-key:dddd
        watches: []
        """,
    )
    config = load_config(path)
    assert config.api_key == "tao-explicit-key:dddd"


# --------------------------------------------------------------------------- #
# Example config
# --------------------------------------------------------------------------- #


def test_write_example_config_is_loadable(tmp_path, monkeypatch):
    """The bundled commented example writes and parses without error."""
    monkeypatch.delenv(API_KEY_ENV_FALLBACK, raising=False)
    path = tmp_path / "sentinel.yaml"
    write_example_config(str(path))

    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "#" in text  # it is a commented example

    config = load_config(str(path))
    assert isinstance(config, Config)
    # The example uses env: indirection for the key; with no env set it resolves
    # to None, and the mock client is used downstream.
    assert config.api_key is None
    # The example ships with at least one of each watch type wired up.
    types_present = {w.type for w in config.watches}
    assert {"price_change", "stake_change", "validator_dereg", "emission_shift"} <= types_present


def test_write_example_config_is_owner_only(tmp_path):
    """The written example config is mode 0o600 (no group/world access).

    Regression for finding 7: the file invites the user to store a raw API key
    and Telegram bot token, so it must not be world-readable on a shared host.
    """
    path = tmp_path / "sentinel.yaml"
    write_example_config(str(path))

    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


# --------------------------------------------------------------------------- #
# Watch-type validation (finding 11)
# --------------------------------------------------------------------------- #


def test_unknown_watch_type_rejected_at_load(tmp_path):
    """A typo'd watch ``type`` fails loudly at load instead of silently dying.

    Regression for finding 11: ``pirce_change`` (typo) previously passed
    validation and was then dropped at runtime so the watch never fired. Now it
    raises a ValidationError at load time.
    """
    path = _write(
        tmp_path / "typo.yaml",
        """
        watches:
          - type: pirce_change
            netuid: 1
            threshold_pct: 10.0
        """,
    )
    with pytest.raises(pydantic.ValidationError) as excinfo:
        load_config(path)
    # The message names the offending value and lists the valid types.
    message = str(excinfo.value)
    assert "pirce_change" in message
    assert "price_change" in message


def test_unknown_watch_type_rejected_at_model_validate():
    """Direct model construction with an unknown type also raises."""
    with pytest.raises(pydantic.ValidationError):
        Config.model_validate(
            {"watches": [{"type": "bogus", "netuid": 1, "threshold_pct": 10}]}
        )


@pytest.mark.parametrize("watch_type", WATCH_TYPES)
def test_all_known_watch_types_accepted(watch_type):
    """Every canonical watch type validates cleanly."""
    watch = WatchConfig(type=watch_type, netuid=1)
    assert watch.type == watch_type
