"""Alert rule functions and the RULES registry.

Each rule is a pure function with the signature::

    func(watch: WatchConfig, prev: dict, now: dict) -> list[Alert]

where ``prev`` and ``now`` are snapshot dicts produced by
:meth:`tao_sentinel.alerts.engine.WatchEngine.take_snapshot`. A snapshot has
the shape::

    {
        "pools": {netuid: Pool, ...},
        "subnets": {netuid: SubnetInfo, ...},
        "stakes": {coldkey: [StakePosition, ...], ...},
        "validators": {netuid: [ValidatorInfo, ...], ...},
        "timestamp": "<iso8601>",
        # Optional v0.2.0 keys (only present when a watch needs them):
        "tao_price": <float>,                       # TAO/USD spot at this tick
        "history": {"<netuid>": [[ts, value], ...]} # 24h alpha-price series
    }

Rules are deterministic given their inputs and never perform any I/O. They
compare the previous snapshot against the current one and emit zero or more
:class:`~tao_sentinel.models.Alert` objects describing what changed.

The module-level :data:`RULES` dict maps a watch ``type`` string to the
function that evaluates it. The engine looks watches up here, so adding a new
rule type is a matter of writing a function and registering it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable

from tao_sentinel.config import WatchConfig
from tao_sentinel.models import Alert, Pool, StakePosition, SubnetInfo, ValidatorInfo

logger = logging.getLogger(__name__)

# Signature shared by every rule function.
RuleFunc = Callable[[WatchConfig, dict, dict], "list[Alert]"]


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _pct_change(old: float, new: float) -> float:
    """Return the signed percentage change from ``old`` to ``new``.

    If ``old`` is zero the change is treated as 0%% when ``new`` is also zero,
    otherwise as +100%% (an appearance from nothing) to avoid division by zero.
    """
    if old == 0:
        return 0.0 if new == 0 else 100.0
    return (new - old) / abs(old) * 100.0


def _get_pool(snapshot: dict, netuid: int) -> Pool | None:
    """Return the :class:`Pool` for ``netuid`` in ``snapshot`` if present."""
    return snapshot.get("pools", {}).get(netuid)


def _get_subnet(snapshot: dict, netuid: int) -> SubnetInfo | None:
    """Return the :class:`SubnetInfo` for ``netuid`` in ``snapshot`` if present."""
    return snapshot.get("subnets", {}).get(netuid)


def _get_validators(snapshot: dict, netuid: int) -> list[ValidatorInfo]:
    """Return the validator list for ``netuid`` in ``snapshot`` (empty if absent)."""
    return snapshot.get("validators", {}).get(netuid, [])


def _get_stakes(snapshot: dict, coldkey: str) -> list[StakePosition]:
    """Return the stake positions for ``coldkey`` in ``snapshot`` (empty if absent)."""
    return snapshot.get("stakes", {}).get(coldkey, [])


def _get_tao_price(snapshot: dict) -> float | None:
    """Return the TAO/USD spot price stored on ``snapshot`` if present."""
    value = snapshot.get("tao_price")
    return float(value) if value is not None else None


def _get_history(snapshot: dict, netuid: int) -> list[tuple[str, float]]:
    """Return the stored ``[[ts, value], ...]`` history for ``netuid``.

    The engine persists history keyed by the stringified netuid (snapshots are
    JSON-round-tripped through the state file). We accept either an ``int`` or
    ``str`` key so the rule works on both freshly-fetched and reloaded
    snapshots, and coerce each pair to ``(str, float)``.
    """
    raw = snapshot.get("history", {})
    series = raw.get(str(netuid))
    if series is None:
        series = raw.get(netuid)
    if not series:
        return []
    out: list[tuple[str, float]] = []
    for pair in series:
        try:
            ts, value = pair[0], pair[1]
            out.append((str(ts), float(value)))
        except (TypeError, ValueError, IndexError):
            continue
    return out


def price_change_rule(watch: WatchConfig, prev: dict, now: dict) -> list[Alert]:
    """Fire when a pool's ``price_tao`` moves beyond ``threshold_pct``.

    Compares the watched subnet's price in ``prev`` against ``now``. Emits a
    single alert (``warning``, or ``critical`` for moves >= 2x threshold) when
    the absolute percentage move meets or exceeds the configured threshold.
    """
    if watch.netuid is None:
        return []

    prev_pool = _get_pool(prev, watch.netuid)
    now_pool = _get_pool(now, watch.netuid)
    if prev_pool is None or now_pool is None:
        return []

    change = _pct_change(prev_pool.price_tao, now_pool.price_tao)
    if abs(change) < watch.threshold_pct:
        return []

    direction = "up" if change >= 0 else "down"
    severity = "critical" if abs(change) >= 2 * watch.threshold_pct else "warning"
    name = now_pool.name or f"subnet {watch.netuid}"
    return [
        Alert(
            rule_type="price_change",
            severity=severity,
            title=f"Price moved {direction} {abs(change):.1f}% on {name}",
            message=(
                f"Subnet {watch.netuid} ({name}) alpha price went from "
                f"{prev_pool.price_tao:.6f} to {now_pool.price_tao:.6f} TAO "
                f"({change:+.1f}%, threshold {watch.threshold_pct:.1f}%)."
            ),
            netuid=watch.netuid,
            timestamp=_now_iso(),
        )
    ]


def stake_change_rule(watch: WatchConfig, prev: dict, now: dict) -> list[Alert]:
    """Fire when any stake position changes by more than ``threshold_pct``.

    A position is keyed by (hotkey, netuid) under the watched coldkey. An alert
    is emitted when a position's ``alpha_staked`` moves beyond the threshold,
    when a new position appears, or when an existing position disappears
    (fully unstaked). Disappearance is treated as ``critical``.

    When the watched coldkey is entirely absent from the previous snapshot's
    ``stakes`` (e.g. the watch was just added to the config, so the prior tick
    never fetched this coldkey) there is no baseline for it, so we emit nothing
    and let this tick establish the baseline - mirroring the
    ``validator_dereg`` ``if not prev_vals: return []`` guard. This is distinct
    from a coldkey that is PRESENT with an empty list, which is a genuine
    all-positions-closed (or, going forward, all-opened) signal.
    """
    if watch.coldkey is None:
        return []

    prev_stakes = prev.get("stakes", {})
    if watch.coldkey not in prev_stakes:
        return []

    prev_positions = _get_stakes(prev, watch.coldkey)
    now_positions = _get_stakes(now, watch.coldkey)

    def _key(p: StakePosition) -> tuple[str, int]:
        return (p.hotkey, p.netuid)

    prev_by_key = {_key(p): p for p in prev_positions}
    now_by_key = {_key(p): p for p in now_positions}

    # If a specific hotkey is configured, restrict to it.
    def _matches(key: tuple[str, int]) -> bool:
        if watch.hotkey is not None and key[0] != watch.hotkey:
            return False
        if watch.netuid is not None and key[1] != watch.netuid:
            return False
        return True

    alerts: list[Alert] = []

    # New or changed positions.
    for key, now_pos in now_by_key.items():
        if not _matches(key):
            continue
        prev_pos = prev_by_key.get(key)
        if prev_pos is None:
            alerts.append(
                Alert(
                    rule_type="stake_change",
                    severity="info",
                    title=f"New stake position on subnet {now_pos.netuid}",
                    message=(
                        f"Coldkey {watch.coldkey} opened a stake of "
                        f"{now_pos.alpha_staked:.4f} alpha on subnet "
                        f"{now_pos.netuid} via hotkey {now_pos.hotkey}."
                    ),
                    netuid=now_pos.netuid,
                    timestamp=_now_iso(),
                )
            )
            continue
        change = _pct_change(prev_pos.alpha_staked, now_pos.alpha_staked)
        if abs(change) >= watch.threshold_pct:
            direction = "increased" if change >= 0 else "decreased"
            alerts.append(
                Alert(
                    rule_type="stake_change",
                    severity="warning",
                    title=(
                        f"Stake {direction} {abs(change):.1f}% on subnet "
                        f"{now_pos.netuid}"
                    ),
                    message=(
                        f"Coldkey {watch.coldkey} stake on subnet "
                        f"{now_pos.netuid} (hotkey {now_pos.hotkey}) went from "
                        f"{prev_pos.alpha_staked:.4f} to "
                        f"{now_pos.alpha_staked:.4f} alpha "
                        f"({change:+.1f}%, threshold {watch.threshold_pct:.1f}%)."
                    ),
                    netuid=now_pos.netuid,
                    timestamp=_now_iso(),
                )
            )

    # Disappeared positions.
    for key, prev_pos in prev_by_key.items():
        if not _matches(key):
            continue
        if key not in now_by_key:
            alerts.append(
                Alert(
                    rule_type="stake_change",
                    severity="critical",
                    title=f"Stake position removed on subnet {prev_pos.netuid}",
                    message=(
                        f"Coldkey {watch.coldkey} no longer holds a stake on "
                        f"subnet {prev_pos.netuid} (hotkey {prev_pos.hotkey}); "
                        f"previous balance was {prev_pos.alpha_staked:.4f} alpha."
                    ),
                    netuid=prev_pos.netuid,
                    timestamp=_now_iso(),
                )
            )

    return alerts


def validator_dereg_rule(watch: WatchConfig, prev: dict, now: dict) -> list[Alert]:
    """Fire when a tracked validator is deregistered on a subnet.

    A hotkey that was present and active in the previous validator snapshot for
    the watched netuid but is missing or marked inactive now is reported as a
    ``critical`` deregistration. If ``watch.hotkey`` is set only that hotkey is
    considered; otherwise every previously active validator on the subnet is
    monitored.
    """
    if watch.netuid is None:
        return []

    prev_vals = _get_validators(prev, watch.netuid)
    now_vals = _get_validators(now, watch.netuid)
    if not prev_vals:
        return []

    now_by_hotkey = {v.hotkey: v for v in now_vals}

    def _is_active(v: ValidatorInfo | None) -> bool:
        # ``active`` defaults to None in the model; treat None as active so we
        # only alert on an explicit False or a missing validator.
        return v is not None and v.active is not False

    alerts: list[Alert] = []
    for prev_val in prev_vals:
        if watch.hotkey is not None and prev_val.hotkey != watch.hotkey:
            continue
        if not _is_active(prev_val):
            # Was not active before; nothing to compare against.
            continue
        now_val = now_by_hotkey.get(prev_val.hotkey)
        if _is_active(now_val):
            continue
        reason = "missing from the metagraph" if now_val is None else "marked inactive"
        alerts.append(
            Alert(
                rule_type="validator_dereg",
                severity="critical",
                title=f"Validator deregistered on subnet {watch.netuid}",
                message=(
                    f"Validator {prev_val.hotkey} on subnet {watch.netuid} is "
                    f"now {reason} (previously active with "
                    f"{prev_val.stake_tao:.2f} TAO stake)."
                ),
                netuid=watch.netuid,
                timestamp=_now_iso(),
            )
        )

    return alerts


def emission_shift_rule(watch: WatchConfig, prev: dict, now: dict) -> list[Alert]:
    """Fire when a subnet's ``emission_pct`` shifts beyond ``threshold_pct``.

    The threshold is interpreted as a *relative* move of the emission share
    (i.e. percentage change of the emission percentage), matching the contract.
    """
    if watch.netuid is None:
        return []

    prev_subnet = _get_subnet(prev, watch.netuid)
    now_subnet = _get_subnet(now, watch.netuid)
    if prev_subnet is None or now_subnet is None:
        return []
    if prev_subnet.emission_pct is None or now_subnet.emission_pct is None:
        return []

    change = _pct_change(prev_subnet.emission_pct, now_subnet.emission_pct)
    if abs(change) < watch.threshold_pct:
        return []

    direction = "up" if change >= 0 else "down"
    name = now_subnet.name or f"subnet {watch.netuid}"
    return [
        Alert(
            rule_type="emission_shift",
            severity="warning",
            title=f"Emission shifted {direction} {abs(change):.1f}% on {name}",
            message=(
                f"Subnet {watch.netuid} ({name}) emission share went from "
                f"{prev_subnet.emission_pct:.4f}% to {now_subnet.emission_pct:.4f}% "
                f"({change:+.1f}% relative, threshold {watch.threshold_pct:.1f}%)."
            ),
            netuid=watch.netuid,
            timestamp=_now_iso(),
        )
    ]


def tao_price_rule(watch: WatchConfig, prev: dict, now: dict) -> list[Alert]:
    """Fire when the TAO/USD spot price moves beyond ``threshold_pct``.

    Compares the engine-supplied ``tao_price`` (USD) between ticks. This watch
    is network-wide, so ``netuid`` is not required and the emitted alert carries
    no netuid. Severity escalates to ``critical`` for moves >= 2x the threshold.
    """
    prev_price = _get_tao_price(prev)
    now_price = _get_tao_price(now)
    if prev_price is None or now_price is None:
        return []

    change = _pct_change(prev_price, now_price)
    if abs(change) < watch.threshold_pct:
        return []

    direction = "up" if change >= 0 else "down"
    severity = "critical" if abs(change) >= 2 * watch.threshold_pct else "warning"
    return [
        Alert(
            rule_type="tao_price",
            severity=severity,
            title=f"TAO price moved {direction} {abs(change):.1f}%",
            message=(
                f"TAO/USD went from ${prev_price:,.2f} to ${now_price:,.2f} "
                f"({change:+.1f}%, threshold {watch.threshold_pct:.1f}%)."
            ),
            netuid=None,
            timestamp=_now_iso(),
        )
    ]


def market_cap_rule(watch: WatchConfig, prev: dict, now: dict) -> list[Alert]:
    """Fire when a subnet pool's ``market_cap_tao`` moves beyond ``threshold_pct``.

    Reuses the ``pools`` snapshot source (the pool carries ``market_cap_tao``).
    Severity escalates to ``critical`` for moves >= 2x the threshold.
    """
    if watch.netuid is None:
        return []

    prev_pool = _get_pool(prev, watch.netuid)
    now_pool = _get_pool(now, watch.netuid)
    if prev_pool is None or now_pool is None:
        return []
    if prev_pool.market_cap_tao is None or now_pool.market_cap_tao is None:
        return []

    change = _pct_change(prev_pool.market_cap_tao, now_pool.market_cap_tao)
    if abs(change) < watch.threshold_pct:
        return []

    direction = "up" if change >= 0 else "down"
    severity = "critical" if abs(change) >= 2 * watch.threshold_pct else "warning"
    name = now_pool.name or f"subnet {watch.netuid}"
    return [
        Alert(
            rule_type="market_cap",
            severity=severity,
            title=f"Market cap moved {direction} {abs(change):.1f}% on {name}",
            message=(
                f"Subnet {watch.netuid} ({name}) market cap went from "
                f"{prev_pool.market_cap_tao:,.2f} to {now_pool.market_cap_tao:,.2f} "
                f"TAO ({change:+.1f}%, threshold {watch.threshold_pct:.1f}%)."
            ),
            netuid=watch.netuid,
            timestamp=_now_iso(),
        )
    ]


def registration_cost_rule(watch: WatchConfig, prev: dict, now: dict) -> list[Alert]:
    """Fire when a subnet's ``registration_cost_tao`` DROPS by ``threshold_pct``.

    A cheap-registration sniper: only a *decrease* of at least ``threshold_pct``
    (relative) is reported, since the use case is catching a window where
    registering becomes cheap. Reuses the ``subnets`` snapshot source.
    """
    if watch.netuid is None:
        return []

    prev_subnet = _get_subnet(prev, watch.netuid)
    now_subnet = _get_subnet(now, watch.netuid)
    if prev_subnet is None or now_subnet is None:
        return []
    prev_cost = prev_subnet.registration_cost_tao
    now_cost = now_subnet.registration_cost_tao
    if prev_cost is None or now_cost is None:
        return []

    change = _pct_change(prev_cost, now_cost)
    # Only a drop (negative change) of at least the threshold magnitude fires.
    if change > -watch.threshold_pct:
        return []

    name = now_subnet.name or f"subnet {watch.netuid}"
    return [
        Alert(
            rule_type="registration_cost",
            severity="warning",
            title=f"Registration cost dropped {abs(change):.1f}% on {name}",
            message=(
                f"Subnet {watch.netuid} ({name}) registration cost fell from "
                f"{prev_cost:.4f} to {now_cost:.4f} TAO "
                f"({change:+.1f}%, threshold {watch.threshold_pct:.1f}% drop)."
            ),
            netuid=watch.netuid,
            timestamp=_now_iso(),
        )
    ]


def new_subnet_rule(watch: WatchConfig, prev: dict, now: dict) -> list[Alert]:
    """Fire (info) for each netuid present in ``now`` pools but absent from ``prev``.

    A subnet-launch radar; ``netuid`` is not required (it watches the whole pool
    set). On the very first run there is no prior pool set to diff against, so
    nothing fires: an empty ``prev`` pools mapping is treated as "no baseline"
    rather than "everything is new", mirroring the other first-run guards.
    """
    prev_pools = prev.get("pools", {})
    now_pools = now.get("pools", {})
    if not prev_pools:
        return []

    alerts: list[Alert] = []
    for netuid in sorted(now_pools):
        if netuid in prev_pools:
            continue
        pool = now_pools[netuid]
        name = pool.name or f"subnet {netuid}"
        alerts.append(
            Alert(
                rule_type="new_subnet",
                severity="info",
                title=f"New subnet detected: {name}",
                message=(
                    f"Subnet {netuid} ({name}) appeared in the pool set with an "
                    f"alpha price of {pool.price_tao:.6f} TAO."
                ),
                netuid=netuid,
                timestamp=_now_iso(),
            )
        )
    return alerts


def price_trend_rule(watch: WatchConfig, prev: dict, now: dict) -> list[Alert]:
    """Fire when the trailing-24h alpha-price move for a netuid >= ``threshold_pct``.

    Uses the engine-supplied per-netuid ``history`` series (first..last point of
    the trailing 24h, chronological ascending) rather than the tick-to-tick pool
    diff, so it catches a slow drift that never trips ``price_change`` between
    two adjacent polls. Requires ``netuid``. Severity escalates to ``critical``
    for moves >= 2x the threshold.
    """
    if watch.netuid is None:
        return []

    series = _get_history(now, watch.netuid)
    if len(series) < 2:
        return []

    first_value = series[0][1]
    last_value = series[-1][1]
    change = _pct_change(first_value, last_value)
    if abs(change) < watch.threshold_pct:
        return []

    pool = _get_pool(now, watch.netuid)
    name = (pool.name if pool else None) or f"subnet {watch.netuid}"
    direction = "up" if change >= 0 else "down"
    severity = "critical" if abs(change) >= 2 * watch.threshold_pct else "warning"
    return [
        Alert(
            rule_type="price_trend",
            severity=severity,
            title=f"24h price trend {direction} {abs(change):.1f}% on {name}",
            message=(
                f"Subnet {watch.netuid} ({name}) alpha price moved from "
                f"{first_value:.6f} to {last_value:.6f} TAO over the trailing 24h "
                f"({change:+.1f}%, threshold {watch.threshold_pct:.1f}%)."
            ),
            netuid=watch.netuid,
            timestamp=_now_iso(),
        )
    ]


# Registry mapping watch ``type`` -> evaluation function.
RULES: dict[str, RuleFunc] = {
    "price_change": price_change_rule,
    "stake_change": stake_change_rule,
    "validator_dereg": validator_dereg_rule,
    "emission_shift": emission_shift_rule,
    "tao_price": tao_price_rule,
    "market_cap": market_cap_rule,
    "registration_cost": registration_cost_rule,
    "new_subnet": new_subnet_rule,
    "price_trend": price_trend_rule,
}
