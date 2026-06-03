"""Tests for the alert rule functions and the RULES registry.

Each of the four rule types is exercised in both a firing and a non-firing
configuration using hand-built ``prev``/``now`` snapshot dicts (no network, no
engine). Rules are pure functions, so these tests just feed them snapshots and
assert on the emitted :class:`~tao_sentinel.models.Alert` list.
"""

from __future__ import annotations

from tao_sentinel.alerts.rules import (
    RULES,
    emission_shift_rule,
    price_change_rule,
    stake_change_rule,
    validator_dereg_rule,
)
from tao_sentinel.config import WatchConfig
from tao_sentinel.models import Alert, Pool, StakePosition, SubnetInfo, ValidatorInfo

COLDKEY = "5TestColdkey"
HOTKEY_A = "5TestHotkeyA"
HOTKEY_B = "5TestHotkeyB"


def make_snapshot(pools=None, subnets=None, stakes=None, validators=None) -> dict:
    """Build a snapshot dict in the shape the rules expect.

    Mirrors ``WatchEngine.take_snapshot``: keys ``pools``, ``subnets``,
    ``stakes``, ``validators`` and ``timestamp``. Kept local so this module has
    no cross-test-file import dependency.
    """
    return {
        "pools": pools or {},
        "subnets": subnets or {},
        "stakes": stakes or {},
        "validators": validators or {},
        "timestamp": "2026-06-03T00:00:00+00:00",
    }


# --------------------------------------------------------------------------- #
# RULES registry
# --------------------------------------------------------------------------- #


def test_rules_registry_covers_all_watch_types():
    """The registry maps every supported watch type to its function."""
    assert set(RULES) == {
        "price_change",
        "stake_change",
        "validator_dereg",
        "emission_shift",
    }
    assert RULES["price_change"] is price_change_rule
    assert RULES["stake_change"] is stake_change_rule
    assert RULES["validator_dereg"] is validator_dereg_rule
    assert RULES["emission_shift"] is emission_shift_rule


# --------------------------------------------------------------------------- #
# price_change
# --------------------------------------------------------------------------- #


def _pool(netuid: int, price: float, name: str = "apex") -> Pool:
    return Pool(netuid=netuid, name=name, price_tao=price)


def test_price_change_fires_above_threshold():
    """A move beyond the threshold emits exactly one price_change alert."""
    watch = WatchConfig(type="price_change", netuid=1, threshold_pct=10.0)
    prev = make_snapshot(pools={1: _pool(1, 0.020)})
    now = make_snapshot(pools={1: _pool(1, 0.030)})  # +50%

    alerts = price_change_rule(watch, prev, now)

    assert len(alerts) == 1
    alert = alerts[0]
    assert isinstance(alert, Alert)
    assert alert.rule_type == "price_change"
    assert alert.netuid == 1
    # 50% >= 2 * 10% threshold => critical
    assert alert.severity == "critical"


def test_price_change_warning_between_one_and_two_thresholds():
    """A move at/above 1x but below 2x the threshold is a warning."""
    watch = WatchConfig(type="price_change", netuid=1, threshold_pct=10.0)
    prev = make_snapshot(pools={1: _pool(1, 0.020)})
    now = make_snapshot(pools={1: _pool(1, 0.0224)})  # +12%

    alerts = price_change_rule(watch, prev, now)

    assert len(alerts) == 1
    assert alerts[0].severity == "warning"


def test_price_change_does_not_fire_below_threshold():
    """A sub-threshold move emits nothing."""
    watch = WatchConfig(type="price_change", netuid=1, threshold_pct=10.0)
    prev = make_snapshot(pools={1: _pool(1, 0.020)})
    now = make_snapshot(pools={1: _pool(1, 0.021)})  # +5%

    assert price_change_rule(watch, prev, now) == []


def test_price_change_no_data_does_not_fire():
    """Missing pool data on either side yields no alert (and no error)."""
    watch = WatchConfig(type="price_change", netuid=1, threshold_pct=10.0)
    prev = make_snapshot(pools={})
    now = make_snapshot(pools={1: _pool(1, 0.030)})

    assert price_change_rule(watch, prev, now) == []


# --------------------------------------------------------------------------- #
# stake_change
# --------------------------------------------------------------------------- #


def _stake(netuid: int, alpha: float, hotkey: str = HOTKEY_A) -> StakePosition:
    return StakePosition(
        coldkey=COLDKEY, hotkey=hotkey, netuid=netuid, alpha_staked=alpha
    )


def test_stake_change_fires_on_large_move():
    """A position changing beyond the threshold emits a warning alert."""
    watch = WatchConfig(type="stake_change", coldkey=COLDKEY, threshold_pct=10.0)
    prev = make_snapshot(stakes={COLDKEY: [_stake(1, 1000.0)]})
    now = make_snapshot(stakes={COLDKEY: [_stake(1, 1300.0)]})  # +30%

    alerts = stake_change_rule(watch, prev, now)

    assert len(alerts) == 1
    assert alerts[0].rule_type == "stake_change"
    assert alerts[0].severity == "warning"
    assert alerts[0].netuid == 1


def test_stake_change_fires_on_disappearance_as_critical():
    """A position that vanishes (fully unstaked) is reported as critical."""
    watch = WatchConfig(type="stake_change", coldkey=COLDKEY, threshold_pct=10.0)
    prev = make_snapshot(stakes={COLDKEY: [_stake(1, 1000.0)]})
    now = make_snapshot(stakes={COLDKEY: []})

    alerts = stake_change_rule(watch, prev, now)

    assert len(alerts) == 1
    assert alerts[0].severity == "critical"
    assert alerts[0].netuid == 1


def test_stake_change_fires_on_appearance_as_info():
    """A brand-new position is reported as info."""
    watch = WatchConfig(type="stake_change", coldkey=COLDKEY, threshold_pct=10.0)
    prev = make_snapshot(stakes={COLDKEY: []})
    now = make_snapshot(stakes={COLDKEY: [_stake(4, 500.0)]})

    alerts = stake_change_rule(watch, prev, now)

    assert len(alerts) == 1
    assert alerts[0].severity == "info"
    assert alerts[0].netuid == 4


def test_stake_change_coldkey_absent_from_prev_does_not_fire():
    """Finding 2: a freshly-added watch (coldkey absent from the prior snapshot)
    must not emit a spurious 'info' burst for every pre-existing position.

    When the coldkey key is entirely missing from ``prev['stakes']`` there is no
    baseline for it, so this tick only establishes one and emits nothing -
    mirroring the validator_dereg ``if not prev_vals`` guard. Contrast with
    ``test_stake_change_fires_on_appearance_as_info`` where the coldkey is
    PRESENT with an empty list, which is a genuine appearance signal.
    """
    watch = WatchConfig(type="stake_change", coldkey=COLDKEY, threshold_pct=10.0)
    # prev has NO entry for COLDKEY at all (watch just added; never fetched).
    prev = make_snapshot(stakes={})
    now = make_snapshot(
        stakes={COLDKEY: [_stake(1, 1000.0, HOTKEY_A), _stake(2, 500.0, HOTKEY_B)]}
    )

    assert stake_change_rule(watch, prev, now) == []


def test_stake_change_present_empty_list_still_signals_appearance():
    """A coldkey PRESENT with an empty prev list remains a real appearance
    signal (the guard only suppresses a wholly-absent coldkey)."""
    watch = WatchConfig(type="stake_change", coldkey=COLDKEY, threshold_pct=10.0)
    prev = make_snapshot(stakes={COLDKEY: []})
    now = make_snapshot(stakes={COLDKEY: [_stake(7, 250.0)]})

    alerts = stake_change_rule(watch, prev, now)

    assert len(alerts) == 1
    assert alerts[0].severity == "info"
    assert alerts[0].netuid == 7


def test_stake_change_does_not_fire_below_threshold():
    """A small move on an existing position emits nothing."""
    watch = WatchConfig(type="stake_change", coldkey=COLDKEY, threshold_pct=10.0)
    prev = make_snapshot(stakes={COLDKEY: [_stake(1, 1000.0)]})
    now = make_snapshot(stakes={COLDKEY: [_stake(1, 1050.0)]})  # +5%

    assert stake_change_rule(watch, prev, now) == []


def test_stake_change_hotkey_filter_restricts_scope():
    """When a hotkey is configured, other hotkeys' changes are ignored."""
    watch = WatchConfig(
        type="stake_change", coldkey=COLDKEY, hotkey=HOTKEY_A, threshold_pct=10.0
    )
    prev = make_snapshot(
        stakes={COLDKEY: [_stake(1, 1000.0, HOTKEY_A), _stake(2, 1000.0, HOTKEY_B)]}
    )
    # Only the unwatched hotkey B moves.
    now = make_snapshot(
        stakes={COLDKEY: [_stake(1, 1000.0, HOTKEY_A), _stake(2, 5000.0, HOTKEY_B)]}
    )

    assert stake_change_rule(watch, prev, now) == []


# --------------------------------------------------------------------------- #
# validator_dereg
# --------------------------------------------------------------------------- #


def _val(hotkey: str, netuid: int, stake: float, active: bool = True) -> ValidatorInfo:
    return ValidatorInfo(
        hotkey=hotkey, netuid=netuid, stake_tao=stake, vtrust=0.9, active=active
    )


def test_validator_dereg_fires_on_missing_hotkey():
    """A previously active hotkey missing from the new metagraph is critical."""
    watch = WatchConfig(type="validator_dereg", netuid=64, hotkey=HOTKEY_A)
    prev = make_snapshot(validators={64: [_val(HOTKEY_A, 64, 500.0, active=True)]})
    now = make_snapshot(validators={64: []})

    alerts = validator_dereg_rule(watch, prev, now)

    assert len(alerts) == 1
    assert alerts[0].rule_type == "validator_dereg"
    assert alerts[0].severity == "critical"
    assert alerts[0].netuid == 64


def test_validator_dereg_fires_when_marked_inactive():
    """A hotkey still present but flipped to inactive deregisters."""
    watch = WatchConfig(type="validator_dereg", netuid=64, hotkey=HOTKEY_A)
    prev = make_snapshot(validators={64: [_val(HOTKEY_A, 64, 500.0, active=True)]})
    now = make_snapshot(validators={64: [_val(HOTKEY_A, 64, 500.0, active=False)]})

    alerts = validator_dereg_rule(watch, prev, now)

    assert len(alerts) == 1
    assert alerts[0].severity == "critical"


def test_validator_dereg_does_not_fire_when_still_active():
    """A validator that stays active produces no alert."""
    watch = WatchConfig(type="validator_dereg", netuid=64, hotkey=HOTKEY_A)
    prev = make_snapshot(validators={64: [_val(HOTKEY_A, 64, 500.0, active=True)]})
    now = make_snapshot(validators={64: [_val(HOTKEY_A, 64, 520.0, active=True)]})

    assert validator_dereg_rule(watch, prev, now) == []


def test_validator_dereg_no_baseline_does_not_fire():
    """With no previous validators there is nothing to compare against."""
    watch = WatchConfig(type="validator_dereg", netuid=64, hotkey=HOTKEY_A)
    prev = make_snapshot(validators={})
    now = make_snapshot(validators={64: []})

    assert validator_dereg_rule(watch, prev, now) == []


# --------------------------------------------------------------------------- #
# emission_shift
# --------------------------------------------------------------------------- #


def _subnet(netuid: int, emission: float, name: str = "apex") -> SubnetInfo:
    return SubnetInfo(netuid=netuid, name=name, emission_pct=emission)


def test_emission_shift_fires_on_relative_move():
    """A relative emission move beyond the threshold emits a warning."""
    watch = WatchConfig(type="emission_shift", netuid=8, threshold_pct=20.0)
    prev = make_snapshot(subnets={8: _subnet(8, 10.0)})
    now = make_snapshot(subnets={8: _subnet(8, 13.0)})  # +30% relative

    alerts = emission_shift_rule(watch, prev, now)

    assert len(alerts) == 1
    assert alerts[0].rule_type == "emission_shift"
    assert alerts[0].severity == "warning"
    assert alerts[0].netuid == 8


def test_emission_shift_does_not_fire_below_threshold():
    """A small relative move emits nothing."""
    watch = WatchConfig(type="emission_shift", netuid=8, threshold_pct=20.0)
    prev = make_snapshot(subnets={8: _subnet(8, 10.0)})
    now = make_snapshot(subnets={8: _subnet(8, 11.0)})  # +10% relative

    assert emission_shift_rule(watch, prev, now) == []


def test_emission_shift_no_data_does_not_fire():
    """Missing emission data on either side yields no alert."""
    watch = WatchConfig(type="emission_shift", netuid=8, threshold_pct=20.0)
    prev = make_snapshot(subnets={8: SubnetInfo(netuid=8, emission_pct=None)})
    now = make_snapshot(subnets={8: _subnet(8, 13.0)})

    assert emission_shift_rule(watch, prev, now) == []
