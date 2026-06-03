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


# Registry mapping watch ``type`` -> evaluation function.
RULES: dict[str, RuleFunc] = {
    "price_change": price_change_rule,
    "stake_change": stake_change_rule,
    "validator_dereg": validator_dereg_rule,
    "emission_shift": emission_shift_rule,
}
