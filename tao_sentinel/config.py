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
from pydantic import BaseModel, Field, field_validator, model_validator

logger = logging.getLogger(__name__)

#: Environment variable consulted as a fallback when no API key is configured.
API_KEY_ENV_FALLBACK = "TAOSTATS_API_KEY"

#: Prefix marking an ``env:VARNAME`` indirection in the ``api_key`` field.
_ENV_INDIRECTION_PREFIX = "env:"

#: Recognised watch types for :class:`WatchConfig`.
#:
#: The original four (``price_change``, ``stake_change``, ``validator_dereg``,
#: ``emission_shift``) are joined by the v0.2.0 additions (C2):
#:
#: * ``tao_price``         - TAO/USD percent move between ticks (netuid optional).
#: * ``market_cap``        - percent move of a subnet pool's market cap in TAO.
#: * ``registration_cost`` - registration cost DROP by ``threshold_pct``.
#: * ``new_subnet``        - a netuid that appears in the pool list (netuid optional).
#: * ``price_trend``       - |change| over the trailing 24h history >= threshold.
WATCH_TYPES = (
    "price_change",
    "stake_change",
    "validator_dereg",
    "emission_shift",
    "tao_price",
    "market_cap",
    "registration_cost",
    "new_subnet",
    "price_trend",
)

#: Watch types for which ``netuid`` is mandatory. ``price_trend`` fetches a
#: per-subnet 24h history series, so without a netuid there is nothing to fetch
#: or evaluate; the engine would silently skip it, so reject it at load time.
#: The other per-subnet types (``price_change``, ``validator_dereg``,
#: ``emission_shift``, ``market_cap``) are NOT listed here because their
#: pre-v0.2.0 behaviour permitted an absent netuid (a config-wide / best-effort
#: watch), and tightening that retroactively would break existing configs.
_NETUID_REQUIRED_TYPES = ("price_trend",)

#: Watch types that never need a netuid (documented for completeness; these are
#: simply not subject to the requirement above). ``tao_price`` tracks the
#: global TAO/USD price and ``new_subnet`` scans the whole pool list for newly
#: appearing subnets.
_NETUID_OPTIONAL_TYPES = ("tao_price", "new_subnet")

#: Maximum number of pinned subnets allowed in :attr:`Config.watchlist`. Each
#: pinned subnet costs one (6h-cached) history call for its sparkline, so the
#: cap keeps the dashboard's worst-case API spend bounded (see C4/C8).
MAX_WATCHLIST = 12


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
        type: Watch type; one of :data:`WATCH_TYPES`. The original four
            (``price_change``, ``stake_change``, ``validator_dereg``,
            ``emission_shift``) plus the v0.2.0 additions (``tao_price``,
            ``market_cap``, ``registration_cost``, ``new_subnet``,
            ``price_trend``).
        netuid: Subnet id the watch targets. Required for ``price_trend``;
            optional (and ignored) for the global ``tao_price`` and
            ``new_subnet`` types; optional for the remaining per-subnet types.
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

    @model_validator(mode="after")
    def _validate_netuid_present(self) -> "WatchConfig":
        """Require ``netuid`` for watch types that cannot work without one.

        ``price_trend`` evaluates a per-subnet 24h history series; with no
        ``netuid`` the engine has nothing to fetch and would silently skip the
        watch. Surfacing the omission at load time (the same philosophy as
        :meth:`_validate_type`) turns a silent dead watch into an actionable
        config error.

        This runs as an after-model validator (not a per-field one) so it fires
        even when ``netuid`` is omitted entirely and falls back to its ``None``
        default -- a per-field validator does not run for a defaulted field.

        Returns:
            ``self`` unchanged.

        Raises:
            ValueError: If a netuid-requiring watch type omits ``netuid``.
        """
        if self.type in _NETUID_REQUIRED_TYPES and self.netuid is None:
            raise ValueError(
                f"watch type {self.type!r} requires a netuid"
            )
        return self


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
        alert_cooldown_minutes: Suppress an identical alert (same
            rule_type|netuid|coldkey|hotkey key) if one fired within this many
            minutes, UNLESS its severity escalated. ``0`` disables dedup.
            Defaults to 60.
        watchlist: Subnet netuids to pin in the dashboard's watchlist section,
            each rendered with a 6h-cached sparkline. Capped at
            :data:`MAX_WATCHLIST` (12) to keep the dashboard's history-call
            spend bounded.
    """

    api_key: Optional[str] = None
    telegram: Optional[TelegramConfig] = None
    webhook_url: Optional[str] = None
    watches: list[WatchConfig] = Field(default_factory=list)
    poll_interval_seconds: int = 3600
    state_path: str = "~/.tao-sentinel/state.json"
    alert_cooldown_minutes: int = 60
    watchlist: list[int] = Field(default_factory=list)

    @field_validator("alert_cooldown_minutes")
    @classmethod
    def _validate_cooldown(cls, value: int) -> int:
        """Reject a negative cooldown (a negative window is meaningless).

        ``0`` is allowed and disables dedup; any positive value is a window in
        minutes.

        Args:
            value: The configured cooldown in minutes.

        Returns:
            The validated ``value`` unchanged.

        Raises:
            ValueError: If ``value`` is negative.
        """
        if value < 0:
            raise ValueError(
                f"alert_cooldown_minutes must be >= 0, got {value}"
            )
        return value

    @field_validator("watchlist")
    @classmethod
    def _validate_watchlist(cls, value: list[int]) -> list[int]:
        """Cap the watchlist at :data:`MAX_WATCHLIST` and require unique netuids.

        Each pinned subnet costs one 6h-cached history call for its sparkline,
        so an unbounded watchlist would let the dashboard breach the API budget
        the rest of the contract is built to protect. Duplicates are rejected
        too: they would render the same pinned subnet twice and waste a cache
        slot without adding information.

        Args:
            value: The configured list of netuids.

        Returns:
            The validated list unchanged.

        Raises:
            ValueError: If the list exceeds :data:`MAX_WATCHLIST` entries or
                contains duplicates.
        """
        if len(value) > MAX_WATCHLIST:
            raise ValueError(
                f"watchlist may pin at most {MAX_WATCHLIST} subnets, "
                f"got {len(value)}"
            )
        if len(set(value)) != len(value):
            raise ValueError("watchlist must not contain duplicate netuids")
        return value


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
# your watches touch. Shared sources are fetched ONCE per tick:
#   pools    - shared by price_change, market_cap, and new_subnet watches
#   subnets  - shared by emission_shift and registration_cost watches
#   coldkey  - one source per watched coldkey (stake_change)
#   netuid   - one source per watched netuid (validator_dereg)
#   tao_price- the global TAO/USD price, +1 call/tick when ANY tao_price
#              watch is present (0 if none)
#   history  - price_trend reads a per-netuid 24h series, but through a 6h TTL
#              cache: 1 call per watched netuid only once per 6h, NOT per tick
# Each LIST source is paginated at 100 rows/page, so per-source HTTP calls =
# ceil(rows / 100) pages: with >100 subnets/pools on mainnet today, pools and
# subnets are 2 HTTP calls each, not 1. tao_price/history are 1 call each.
#
# The watches below touch pools (2) + subnets (2) + 1 coldkey (1) + 1 netuid
# (1) + tao_price (1) = 7 calls/tick. The single price_trend watch adds a
# history call only every 6h: 1 netuid * (30*24 / 6) = 120 calls/month.
# Calls/month = per_tick_calls * (3600 / poll_interval_seconds) * 24 * 30
#               + price_trend_history (~120/month here).
# The Taostats free tier is ~10k calls/month, so keep the total under 10000:
#   3600s (hourly) -> 7 * 1 * 24 * 30 + 120  =  5,160 calls/month  (safe)
#    900s          -> 7 * 4 * 24 * 30 + 120  = 20,280 calls/month   (OVER budget)
#    300s          -> 7 * 12 * 24 * 30 + 120 = 60,600 calls/month   (~6x OVER)
# Dashboard extras (separate process, NOT per-tick): each pinned watchlist
# subnet costs 1 history call per 6h, the header TAO-price sparkline 1/6h, and
# each uncached /subnet/{netuid} detail view 1 call (LRU-capped at 16 views).
# Lower poll_interval_seconds only on a paid plan or with fewer/cheaper watches.
poll_interval_seconds: 3600

# Where to persist watch-engine state between runs.
state_path: ~/.tao-sentinel/state.json

# Suppress duplicate alerts: an identical alert (same rule type + subnet +
# coldkey + hotkey) is skipped if one already fired within this many minutes,
# UNLESS its severity escalated (info < warning < critical). Set to 0 to send
# every alert every tick (no dedup).
alert_cooldown_minutes: 60

# Subnets to pin at the top of the web dashboard, each with a sparkline.
# Capped at 12; each pinned subnet costs one 6h-cached history call.
watchlist: [1, 64]

# Watches to evaluate each poll. Supported types:
#   price_change      - alpha pool price moves beyond threshold_pct
#   stake_change      - a coldkey position changes by threshold_pct
#   validator_dereg   - a watched hotkey leaves/goes inactive on a subnet
#   emission_shift    - subnet emission_pct moves beyond threshold_pct
#   tao_price         - TAO/USD price moves beyond threshold_pct (no netuid)
#   market_cap        - a subnet pool's market cap (TAO) moves beyond threshold
#   registration_cost - a subnet's registration cost DROPS by threshold_pct
#                       (a cheap-registration sniper)
#   new_subnet        - a brand-new subnet appears in the pool list (no netuid)
#   price_trend       - |change| over the trailing 24h history >= threshold_pct
#                       (requires netuid; uses the 6h-cached history endpoint)
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

  # Alert when the TAO/USD price moves more than 5% between ticks.
  - type: tao_price
    threshold_pct: 5.0

  # Alert when subnet 4's pool market cap (TAO) moves more than 25%.
  - type: market_cap
    netuid: 4
    threshold_pct: 25.0

  # Alert when subnet 8's registration cost DROPS by more than 30%.
  - type: registration_cost
    netuid: 8
    threshold_pct: 30.0

  # Alert (info) whenever a brand-new subnet appears in the pool list.
  - type: new_subnet

  # Alert when subnet 1's trailing-24h price trend exceeds 15% in either
  # direction (requires netuid; reads the 6h-cached history series).
  - type: price_trend
    netuid: 1
    threshold_pct: 15.0
"""
