"""Configuration loading/saving for tao-sentinel.

The configuration is a YAML file deserialised into the :class:`Config` pydantic
model. The API key supports an ``env:VARNAME`` indirection: if the configured
``api_key`` is the literal string ``"env:SOME_VAR"`` the value is read from the
environment variable ``SOME_VAR`` at load time. If no API key is configured (or
it resolves to nothing) the ``TAOSTATS_API_KEY`` environment variable is used as
a fallback.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import yaml
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

#: Environment variable consulted as a fallback when no API key is configured.
API_KEY_ENV_FALLBACK = "TAOSTATS_API_KEY"

#: Prefix marking an ``env:VARNAME`` indirection in the ``api_key`` field.
_ENV_INDIRECTION_PREFIX = "env:"

#: Recognised watch types for :class:`WatchConfig`.
WATCH_TYPES = (
    "price_change",
    "stake_change",
    "validator_dereg",
    "emission_shift",
)


class TelegramConfig(BaseModel):
    """Telegram notification settings.

    Attributes:
        bot_token: Telegram bot token used to call the sendMessage API.
        chat_id: Destination chat id for alert messages.
    """

    bot_token: str
    chat_id: str


class WatchConfig(BaseModel):
    """A single watch definition.

    Attributes:
        type: Watch type; one of :data:`WATCH_TYPES`
            (``price_change``, ``stake_change``, ``validator_dereg``,
            ``emission_shift``).
        netuid: Subnet id the watch targets, if applicable.
        coldkey: Coldkey ss58 address the watch targets, if applicable.
        hotkey: Hotkey ss58 address the watch targets, if applicable.
        threshold_pct: Percentage threshold that triggers the watch.
    """

    type: str
    netuid: Optional[int] = None
    coldkey: Optional[str] = None
    hotkey: Optional[str] = None
    threshold_pct: float = 10.0

    @field_validator("type")
    @classmethod
    def _validate_type(cls, value: str) -> str:
        """Reject watch types outside :data:`WATCH_TYPES`.

        A typo'd or unsupported ``type`` would otherwise be silently dropped at
        runtime (no data source fetched, no rule registered), so the intended
        watch never fires. Failing loudly at load time turns that silent dead
        watch into an immediate, actionable config error.

        Args:
            value: The configured watch type string.

        Returns:
            The validated ``value`` unchanged.

        Raises:
            ValueError: If ``value`` is not one of :data:`WATCH_TYPES`.
        """
        if value not in WATCH_TYPES:
            valid = ", ".join(WATCH_TYPES)
            raise ValueError(
                f"unknown watch type {value!r}; valid types are: {valid}"
            )
        return value


class Config(BaseModel):
    """Top-level tao-sentinel configuration.

    Attributes:
        api_key: Taostats API key, or an ``env:VARNAME`` indirection string.
            Resolved to its final value by :func:`load_config`.
        telegram: Optional Telegram notification settings.
        webhook_url: Optional generic webhook URL for alert delivery.
        watches: List of configured watches.
        poll_interval_seconds: Seconds between watch-engine polls. Defaults to
            3600 (hourly) to stay within the documented ~10k calls/month
            free-tier budget; see the example config for the per-tick cost math.
        state_path: Path to the JSON state file (``~`` is expanded by consumers).
    """

    api_key: Optional[str] = None
    telegram: Optional[TelegramConfig] = None
    webhook_url: Optional[str] = None
    watches: list[WatchConfig] = Field(default_factory=list)
    poll_interval_seconds: int = 3600
    state_path: str = "~/.tao-sentinel/state.json"


def _resolve_api_key(api_key: Optional[str]) -> Optional[str]:
    """Resolve an API key value, applying ``env:`` indirection and the fallback.

    Resolution order:
        1. If ``api_key`` is ``"env:VARNAME"``, read ``VARNAME`` from the
           environment (``None`` if unset).
        2. Otherwise use ``api_key`` verbatim.
        3. If the result is empty/``None``, fall back to the
           :data:`API_KEY_ENV_FALLBACK` (``TAOSTATS_API_KEY``) environment
           variable.

    Args:
        api_key: The raw configured value (may be ``None``).

    Returns:
        The resolved API key, or ``None`` if none could be determined.
    """
    resolved: Optional[str] = api_key
    if api_key and api_key.startswith(_ENV_INDIRECTION_PREFIX):
        var_name = api_key[len(_ENV_INDIRECTION_PREFIX):].strip()
        resolved = os.environ.get(var_name)
        if resolved is None:
            logger.warning(
                "api_key references env var %r which is not set", var_name
            )
    if not resolved:
        resolved = os.environ.get(API_KEY_ENV_FALLBACK)
    return resolved or None


def load_config(path: str) -> Config:
    """Load a :class:`Config` from a YAML file.

    The ``api_key`` field is resolved through :func:`_resolve_api_key`, applying
    ``env:VARNAME`` indirection and the ``TAOSTATS_API_KEY`` environment-variable
    fallback.

    Args:
        path: Filesystem path to the YAML configuration file.

    Returns:
        The parsed and key-resolved :class:`Config`.
    """
    expanded = os.path.expanduser(path)
    with open(expanded, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"config file {path!r} must contain a YAML mapping")

    config = Config.model_validate(raw)
    config.api_key = _resolve_api_key(config.api_key)
    return config


def write_example_config(path: str) -> None:
    """Write a commented example configuration to ``path``.

    The file is chmod'd to ``0o600`` (owner read/write only) after writing,
    because the README and the example itself invite users to store a raw
    Taostats API key and a Telegram bot token in it; a world-readable secrets
    file would expose those credentials to every local user on a shared host.

    Args:
        path: Filesystem path to write the example YAML to.
    """
    expanded = os.path.expanduser(path)
    parent = os.path.dirname(expanded)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(expanded, "w", encoding="utf-8") as fh:
        fh.write(_EXAMPLE_CONFIG)
    os.chmod(expanded, 0o600)


_EXAMPLE_CONFIG = """\
# tao-sentinel configuration
#
# Taostats API key. You can either put the raw key here, use the
# "env:VARNAME" indirection to read it from an environment variable at load
# time, or omit it entirely and set the TAOSTATS_API_KEY environment variable.
# If no key is available, tao-sentinel falls back to the deterministic mock
# client so everything still works without network access.
api_key: env:TAOSTATS_API_KEY

# Optional Telegram notifications. Remove this block to disable.
# telegram:
#   bot_token: "123456:ABC-your-bot-token"
#   chat_id: "123456789"

# Optional generic webhook. Alerts are POSTed as JSON. Remove to disable.
# webhook_url: "https://example.com/hooks/tao-sentinel"

# How often (in seconds) to poll for changes when running `watch`.
#
# MONTHLY BUDGET: each poll ("tick") spends API calls per distinct data source
# your watches touch -- pools (all price_change watches share one source),
# subnets (all emission_shift watches share one source), one source per watched
# coldkey (stake_change), and one source per watched netuid (validator_dereg).
# Each LIST source is paginated at 100 rows/page, so per-source HTTP calls =
# ceil(rows / 100) pages: with >100 subnets/pools on mainnet today, pools and
# subnets are 2 HTTP calls each, not 1.
# The watches below touch 4 sources; at 2 pages each that is 8 calls/tick.
# Calls/month = calls_per_tick * (3600 / poll_interval_seconds) * 24 * 30.
# The Taostats free tier is ~10k calls/month, so keep that product under 10000:
#   3600s (hourly) -> 8 * 1 * 24 * 30   =  5,760 calls/month  (safe)
#    900s          -> 8 * 4 * 24 * 30   = 23,040 calls/month   (OVER budget)
#    300s          -> 8 * 12 * 24 * 30  = 69,120 calls/month   (~6.9x OVER)
# Lower this only if you have a paid plan or fewer/cheaper watches.
poll_interval_seconds: 3600

# Where to persist watch-engine state between runs.
state_path: ~/.tao-sentinel/state.json

# Watches to evaluate each poll. Supported types:
#   price_change      - alpha pool price moves beyond threshold_pct
#   stake_change      - a coldkey position changes by threshold_pct
#   validator_dereg   - a watched hotkey leaves/goes inactive on a subnet
#   emission_shift    - subnet emission_pct moves beyond threshold_pct
watches:
  # Alert when subnet 1 (apex) alpha price moves more than 10%.
  - type: price_change
    netuid: 1
    threshold_pct: 10.0

  # Alert when this coldkey's positions change by more than 5%.
  - type: stake_change
    coldkey: "5MockColdkeyExampleReplaceWithYourOwnSs58Address"
    threshold_pct: 5.0

  # Alert (critical) if this hotkey deregisters / goes inactive on subnet 64.
  - type: validator_dereg
    netuid: 64
    hotkey: "5MockHotkeyExampleReplaceWithYourValidatorSs58Address"

  # Alert when subnet 8's emission share shifts by more than 20% (relative).
  - type: emission_shift
    netuid: 8
    threshold_pct: 20.0
"""
