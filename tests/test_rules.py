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
    market_cap_rule,
    new_subnet_rule,
    price_change_rule,
    price_trend_rule,
    registration_cost_rule,
    stake_change_rule,
    tao_price_rule,
    validator_dereg_rule,
)
from tao_sentinel.config import WatchConfig
from tao_sentinel.models import Alert, Pool, StakePosition, SubnetInfo, ValidatorInfo

COLDKEY = "5TestColdkey"
HOTKEY_A = "5TestHotkeyA"
HOTKEY_B = "5TestHotkeyB"


def make_snapshot(
    pools=None,
    subnets=None,
    stakes=None,
    validators=None,
    tao_price=None,
    history=None,
) -> dict:
    """Build a snapshot dict in the shape the rules expect.

    Mirrors ``WatchEngine.take_snapshot``: keys ``pools``, ``subnets``,
    ``stakes``, ``validators`` and ``timestamp`` plus the optional v0.2.0
    ``tao_price`` / ``history`` keys. Kept local so this module has no
    cross-test-file import dependency.
    """
    snap = {
        "pools": pools or {},
        "subnets": subnets or {},
        "stakes": stakes or {},
        "validators": validators or {},
        "timestamp": "2026-06-03T00:00:00+00:00",
    }
    if tao_price is not None:
        snap["tao_price"] = tao_price
    if history is not None:
        snap["history"] = history
    return snap


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
        "tao_price",
        "market_cap",
        "registration_cost",
        "new_subnet",
        "price_trend",
    }
    assert RULES["price_change"] is price_change_rule
    assert RULES["stake_change"] is stake_change_rule
    assert RULES["validator_dereg"] is validator_dereg_rule
    assert RULES["emission_shift"] is emission_shift_rule
    assert RULES["tao_price"] is tao_price_rule
    assert RULES["market_cap"] is market_cap_rule
    assert RULES["registration_cost"] is registration_cost_rule
    assert RULES["new_subnet"] is new_subnet_rule
    assert RULES["price_trend"] is price_trend_rule


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


# --------------------------------------------------------------------------- #
# tao_price (C2)
# --------------------------------------------------------------------------- #


def test_tao_price_fires_warning_between_one_and_two_thresholds():
    """A TAO/USD move at/above 1x but below 2x the threshold is a warning."""
    watch = WatchConfig(type="tao_price", threshold_pct=10.0)
    prev = make_snapshot(tao_price=200.0)
    now = make_snapshot(tao_price=224.0)  # +12%

    alerts = tao_price_rule(watch, prev, now)

    assert len(alerts) == 1
    assert alerts[0].rule_type == "tao_price"
    assert alerts[0].severity == "warning"
    # Network-wide: carries no netuid.
    assert alerts[0].netuid is None


def test_tao_price_fires_critical_on_big_move():
    """A move >= 2x the threshold escalates to critical."""
    watch = WatchConfig(type="tao_price", threshold_pct=10.0)
    prev = make_snapshot(tao_price=200.0)
    now = make_snapshot(tao_price=160.0)  # -20%

    alerts = tao_price_rule(watch, prev, now)

    assert len(alerts) == 1
    assert alerts[0].severity == "critical"


def test_tao_price_does_not_fire_below_threshold():
    """A sub-threshold TAO/USD move emits nothing."""
    watch = WatchConfig(type="tao_price", threshold_pct=10.0)
    prev = make_snapshot(tao_price=200.0)
    now = make_snapshot(tao_price=205.0)  # +2.5%

    assert tao_price_rule(watch, prev, now) == []


def test_tao_price_no_data_does_not_fire():
    """Missing the tao_price key on either side yields no alert."""
    watch = WatchConfig(type="tao_price", threshold_pct=10.0)
    prev = make_snapshot()  # no tao_price
    now = make_snapshot(tao_price=205.0)

    assert tao_price_rule(watch, prev, now) == []


# --------------------------------------------------------------------------- #
# market_cap (C2)
# --------------------------------------------------------------------------- #


def _cap_pool(netuid: int, cap: float, name: str = "apex") -> Pool:
    return Pool(netuid=netuid, name=name, price_tao=0.01, market_cap_tao=cap)


def test_market_cap_fires_on_relative_move():
    """A market-cap move beyond the threshold emits a warning."""
    watch = WatchConfig(type="market_cap", netuid=1, threshold_pct=15.0)
    prev = make_snapshot(pools={1: _cap_pool(1, 10000.0)})
    now = make_snapshot(pools={1: _cap_pool(1, 12000.0)})  # +20%

    alerts = market_cap_rule(watch, prev, now)

    assert len(alerts) == 1
    assert alerts[0].rule_type == "market_cap"
    assert alerts[0].severity == "warning"
    assert alerts[0].netuid == 1


def test_market_cap_does_not_fire_below_threshold():
    """A small market-cap move emits nothing."""
    watch = WatchConfig(type="market_cap", netuid=1, threshold_pct=15.0)
    prev = make_snapshot(pools={1: _cap_pool(1, 10000.0)})
    now = make_snapshot(pools={1: _cap_pool(1, 10500.0)})  # +5%

    assert market_cap_rule(watch, prev, now) == []


def test_market_cap_missing_cap_does_not_fire():
    """A pool without market_cap_tao on either side yields no alert."""
    watch = WatchConfig(type="market_cap", netuid=1, threshold_pct=15.0)
    prev = make_snapshot(pools={1: Pool(netuid=1, price_tao=0.01)})
    now = make_snapshot(pools={1: _cap_pool(1, 12000.0)})

    assert market_cap_rule(watch, prev, now) == []


# --------------------------------------------------------------------------- #
# registration_cost (C2) - DROP-only sniper
# --------------------------------------------------------------------------- #


def _reg_subnet(netuid: int, cost: float, name: str = "apex") -> SubnetInfo:
    return SubnetInfo(netuid=netuid, name=name, registration_cost_tao=cost)


def test_registration_cost_fires_on_drop():
    """A registration-cost DROP beyond the threshold emits a warning."""
    watch = WatchConfig(type="registration_cost", netuid=1, threshold_pct=30.0)
    prev = make_snapshot(subnets={1: _reg_subnet(1, 100.0)})
    now = make_snapshot(subnets={1: _reg_subnet(1, 60.0)})  # -40%

    alerts = registration_cost_rule(watch, prev, now)

    assert len(alerts) == 1
    assert alerts[0].rule_type == "registration_cost"
    assert alerts[0].severity == "warning"
    assert alerts[0].netuid == 1


def test_registration_cost_does_not_fire_on_rise():
    """A registration-cost INCREASE never fires (sniper only cares about drops)."""
    watch = WatchConfig(type="registration_cost", netuid=1, threshold_pct=30.0)
    prev = make_snapshot(subnets={1: _reg_subnet(1, 100.0)})
    now = make_snapshot(subnets={1: _reg_subnet(1, 200.0)})  # +100%

    assert registration_cost_rule(watch, prev, now) == []


def test_registration_cost_does_not_fire_on_small_drop():
    """A drop smaller than the threshold emits nothing."""
    watch = WatchConfig(type="registration_cost", netuid=1, threshold_pct=30.0)
    prev = make_snapshot(subnets={1: _reg_subnet(1, 100.0)})
    now = make_snapshot(subnets={1: _reg_subnet(1, 90.0)})  # -10%

    assert registration_cost_rule(watch, prev, now) == []


# --------------------------------------------------------------------------- #
# new_subnet (C2)
# --------------------------------------------------------------------------- #


def test_new_subnet_fires_for_appearing_netuid():
    """A netuid present now but absent from prev pools emits an info alert."""
    watch = WatchConfig(type="new_subnet")
    prev = make_snapshot(pools={1: _pool(1, 0.02)})
    now = make_snapshot(pools={1: _pool(1, 0.02), 129: _pool(129, 0.001, "newbie")})

    alerts = new_subnet_rule(watch, prev, now)

    assert len(alerts) == 1
    assert alerts[0].rule_type == "new_subnet"
    assert alerts[0].severity == "info"
    assert alerts[0].netuid == 129


def test_new_subnet_first_run_safety_emits_nothing():
    """On the very first run (empty prev pools) nothing fires - not 'all new'."""
    watch = WatchConfig(type="new_subnet")
    prev = make_snapshot(pools={})  # no baseline
    now = make_snapshot(pools={1: _pool(1, 0.02), 4: _pool(4, 0.03)})

    assert new_subnet_rule(watch, prev, now) == []


def test_new_subnet_does_not_fire_for_known_subnets():
    """No appearance -> no alert."""
    watch = WatchConfig(type="new_subnet")
    prev = make_snapshot(pools={1: _pool(1, 0.02), 4: _pool(4, 0.03)})
    now = make_snapshot(pools={1: _pool(1, 0.025), 4: _pool(4, 0.031)})

    assert new_subnet_rule(watch, prev, now) == []


# --------------------------------------------------------------------------- #
# price_trend (C2) - trailing-24h history move
# --------------------------------------------------------------------------- #


def _series(*values: float) -> list[list]:
    """Build a chronological-ascending [[ts, value], ...] history series."""
    return [[f"2026-06-03T{i:02d}:00:00+00:00", v] for i, v in enumerate(values)]


def test_price_trend_fires_on_trailing_move():
    """A first..last 24h move beyond the threshold emits a warning."""
    watch = WatchConfig(type="price_trend", netuid=1, threshold_pct=10.0)
    prev = make_snapshot()
    now = make_snapshot(
        pools={1: _pool(1, 0.024)},
        history={"1": _series(0.020, 0.021, 0.022, 0.024)},  # +20%
    )

    alerts = price_trend_rule(watch, prev, now)

    assert len(alerts) == 1
    assert alerts[0].rule_type == "price_trend"
    # 20% == 2x the 10% threshold -> critical.
    assert alerts[0].severity == "critical"
    assert alerts[0].netuid == 1


def test_price_trend_warning_between_one_and_two_thresholds():
    """A move at/above 1x but below 2x the threshold is a warning."""
    watch = WatchConfig(type="price_trend", netuid=1, threshold_pct=10.0)
    now = make_snapshot(history={"1": _series(0.020, 0.0224)})  # +12%

    alerts = price_trend_rule(watch, make_snapshot(), now)

    assert len(alerts) == 1
    assert alerts[0].severity == "warning"


def test_price_trend_does_not_fire_below_threshold():
    """A sub-threshold trailing move emits nothing."""
    watch = WatchConfig(type="price_trend", netuid=1, threshold_pct=10.0)
    now = make_snapshot(history={"1": _series(0.020, 0.0205, 0.021)})  # +5%

    assert price_trend_rule(watch, make_snapshot(), now) == []


def test_price_trend_insufficient_history_does_not_fire():
    """Fewer than two points (or no history) yields no alert."""
    watch = WatchConfig(type="price_trend", netuid=1, threshold_pct=10.0)

    assert price_trend_rule(watch, make_snapshot(), make_snapshot()) == []
    one_point = make_snapshot(history={"1": _series(0.02)})
    assert price_trend_rule(watch, make_snapshot(), one_point) == []


def test_price_trend_accepts_int_keyed_history():
    """History keyed by an int netuid (fresh snapshot) is also read correctly."""
    watch = WatchConfig(type="price_trend", netuid=1, threshold_pct=10.0)
    now = make_snapshot(history={1: _series(0.020, 0.030)})  # +50%, int key

    alerts = price_trend_rule(watch, make_snapshot(), now)

    assert len(alerts) == 1
    assert alerts[0].severity == "critical"
