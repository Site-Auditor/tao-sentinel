"""Tests for :class:`tao_sentinel.alerts.engine.WatchEngine`.

These tests drive the engine directly (no network) with small stub clients and
hand-built config, covering the correctness-critical glue the rule-level tests
do not: per-source carry-forward on fetch failure, snapshot serialize/
deserialize round-trips (including int-key coercion), first-run baseline
behavior, poisoned/corrupt state recovery, atomic + private state writes, and
dispatch resilience to a raising notifier.
"""

from __future__ import annotations

import json
import os
import stat

from tao_sentinel.alerts.engine import WatchEngine
from tao_sentinel.alerts.notify import Notifier
from tao_sentinel.config import Config, WatchConfig
from tao_sentinel.models import (
    Alert,
    Pool,
    StakePosition,
    SubnetInfo,
    ValidatorInfo,
)

COLDKEY = "5EngineColdkey"
HOTKEY = "5EngineHotkey"


# --------------------------------------------------------------------------- #
# Stub clients / notifiers
# --------------------------------------------------------------------------- #


class _StubClient:
    """A client whose per-source methods return queued values or raise.

    Each ``*_results`` is a list consumed one entry per call; an entry that is
    an ``Exception`` (class or instance) is raised, simulating a transient API
    failure on that specific call.
    """

    def __init__(self, pools=None, subnets=None, stakes=None, validators=None):
        self._pools = list(pools or [])
        self._subnets = list(subnets or [])
        self._stakes = list(stakes or [])
        self._validators = list(validators or [])

    @staticmethod
    def _take(queue, default):
        if not queue:
            return default
        item = queue.pop(0)
        if isinstance(item, Exception) or (
            isinstance(item, type) and issubclass(item, Exception)
        ):
            raise item if isinstance(item, Exception) else item("stub failure")
        return item

    def get_pools(self):
        return self._take(self._pools, [])

    def get_subnets(self):
        return self._take(self._subnets, [])

    def get_stake_balances(self, coldkey):
        return self._take(self._stakes, [])

    def get_validators(self, netuid):
        return self._take(self._validators, [])


class _RecordingNotifier(Notifier):
    """A notifier that records the titles of every alert it receives."""

    def __init__(self):
        self.got: list[str] = []

    def send(self, alert: Alert) -> None:
        self.got.append(alert.title)


class _RaisingNotifier(Notifier):
    """A notifier whose send always raises, simulating a broken channel."""

    def send(self, alert: Alert) -> None:
        raise RuntimeError("boom")


def _alert(title: str = "t") -> Alert:
    return Alert(
        rule_type="price_change",
        severity="info",
        title=title,
        message="m",
        timestamp="2026-06-03T00:00:00+00:00",
    )


def _val_watch_config(state_path: str) -> Config:
    return Config(
        watches=[WatchConfig(type="validator_dereg", netuid=64, hotkey=HOTKEY)],
        state_path=state_path,
    )


# --------------------------------------------------------------------------- #
# Finding 25: carry-forward on per-source fetch failure
# --------------------------------------------------------------------------- #


def test_carry_forward_suppresses_spurious_dereg_on_validator_fetch_failure(tmp_path):
    """A transient validator fetch failure must NOT look like a mass dereg.

    Tick 1 establishes a baseline with an active validator. Tick 2's
    get_validators raises; carry-forward reuses the prior validators so the
    rule sees no change and emits nothing, and the baseline is preserved.
    """
    config = _val_watch_config(str(tmp_path / "state.json"))
    client = _StubClient(
        validators=[
            [ValidatorInfo(hotkey=HOTKEY, netuid=64, stake_tao=500.0, active=True)],
            RuntimeError,  # second call (tick 2) fails
        ]
    )
    eng = WatchEngine(client, config, notifiers=[])

    alerts1, state1 = eng.run_once(None)
    assert alerts1 == []  # first run is baseline only

    alerts2, state2 = eng.run_once(state1)
    assert alerts2 == []  # carry-forward: no spurious critical dereg

    # The validator is still carried forward in the persisted baseline.
    carried = state2["snapshot"]["validators"]["64"]
    assert len(carried) == 1
    assert carried[0]["hotkey"] == HOTKEY


def test_carry_forward_preserves_pools_on_pool_fetch_failure(tmp_path):
    """A pools fetch failure carries forward prior pools rather than emptying."""
    config = Config(
        watches=[WatchConfig(type="price_change", netuid=1, threshold_pct=10.0)],
        state_path=str(tmp_path / "state.json"),
    )
    client = _StubClient(
        pools=[
            [Pool(netuid=1, name="apex", price_tao=0.02)],
            RuntimeError,  # tick 2 fails
        ]
    )
    eng = WatchEngine(client, config, notifiers=[])

    _, state1 = eng.run_once(None)
    alerts2, state2 = eng.run_once(state1)

    assert alerts2 == []  # no price_change alert; pool carried forward unchanged
    assert state2["snapshot"]["pools"]["1"]["price_tao"] == 0.02


def test_run_once_first_run_emits_no_alerts(tmp_path):
    """run_once with no prior state establishes a baseline and emits nothing."""
    config = _val_watch_config(str(tmp_path / "state.json"))
    client = _StubClient(
        validators=[
            [ValidatorInfo(hotkey=HOTKEY, netuid=64, stake_tao=500.0, active=True)]
        ]
    )
    eng = WatchEngine(client, config, notifiers=[])

    alerts, state = eng.run_once(None)

    assert alerts == []
    assert "snapshot" in state


# --------------------------------------------------------------------------- #
# Snapshot (de)serialization round-trip incl. int-key coercion
# --------------------------------------------------------------------------- #


def test_serialize_deserialize_round_trip_coerces_int_keys():
    """Round-tripping a snapshot restores int keys for pools/subnets/validators."""
    snapshot = {
        "pools": {1: Pool(netuid=1, name="apex", price_tao=0.02)},
        "subnets": {8: SubnetInfo(netuid=8, name="s8", emission_pct=10.0)},
        "stakes": {
            COLDKEY: [
                StakePosition(
                    coldkey=COLDKEY, hotkey=HOTKEY, netuid=1, alpha_staked=100.0
                )
            ]
        },
        "validators": {
            64: [ValidatorInfo(hotkey=HOTKEY, netuid=64, stake_tao=500.0, active=True)]
        },
        "timestamp": "2026-06-03T00:00:00+00:00",
    }

    serialized = WatchEngine._serialize_snapshot(snapshot)
    # JSON object keys are strings.
    assert set(serialized["pools"]) == {"1"}
    assert set(serialized["validators"]) == {"64"}

    restored = WatchEngine._deserialize_snapshot(serialized)
    assert set(restored["pools"]) == {1}
    assert set(restored["subnets"]) == {8}
    assert set(restored["validators"]) == {64}
    assert restored["stakes"][COLDKEY][0].alpha_staked == 100.0
    assert restored["pools"][1].price_tao == 0.02


def test_deserialize_none_is_baseline():
    """A falsy raw snapshot deserializes to None so callers detect baseline."""
    assert WatchEngine._deserialize_snapshot(None) is None
    assert WatchEngine._deserialize_snapshot({}) is None


# --------------------------------------------------------------------------- #
# Finding 1: schema-drift snapshot does not crash run_once
# --------------------------------------------------------------------------- #


def test_run_once_recovers_from_schema_drifted_snapshot(tmp_path):
    """A valid-JSON snapshot whose schema has drifted (Pool missing price_tao)
    must not crash run_once: it is treated as no baseline and a fresh one is
    re-established, with no alerts on that recovery tick."""
    config = Config(
        watches=[WatchConfig(type="price_change", netuid=1, threshold_pct=10.0)],
        state_path=str(tmp_path / "state.json"),
    )
    # Pool is missing the required ``price_tao`` field -> ValidationError on load.
    poisoned_state = {
        "snapshot": {
            "pools": {"1": {"netuid": 1, "name": "apex"}},
            "subnets": {},
            "stakes": {},
            "validators": {},
            "timestamp": "t",
        },
        "last_run": "t",
        "last_alerts": [],
    }
    client = _StubClient(pools=[[Pool(netuid=1, name="apex", price_tao=0.02)]])
    eng = WatchEngine(client, config, notifiers=[])

    # Must not raise; recovery tick re-baselines and emits nothing.
    alerts, state = eng.run_once(poisoned_state)
    assert alerts == []
    assert state["snapshot"]["pools"]["1"]["price_tao"] == 0.02


# --------------------------------------------------------------------------- #
# Finding 13: atomic save + corrupt-state recovery
# --------------------------------------------------------------------------- #


def test_load_state_missing_file_returns_empty(tmp_path):
    config = Config(state_path=str(tmp_path / "nope" / "state.json"))
    eng = WatchEngine(client=None, config=config, notifiers=[])
    assert eng.load_state() == {}


def test_load_state_corrupt_file_recovers_and_preserves(tmp_path):
    """A truncated/corrupt state file is renamed to <name>.corrupt and load
    returns {} rather than crashing (finding 13)."""
    state_path = tmp_path / "state.json"
    state_path.write_text('{"snapshot": {"pools": ', encoding="utf-8")  # truncated
    config = Config(state_path=str(state_path))
    eng = WatchEngine(client=None, config=config, notifiers=[])

    assert eng.load_state() == {}
    assert not state_path.exists()
    assert (tmp_path / "state.json.corrupt").exists()


def test_save_state_is_atomic_and_round_trips(tmp_path):
    """save_state writes a complete, reloadable file with no leftover .tmp."""
    state_path = tmp_path / "sub" / "state.json"
    config = Config(state_path=str(state_path))
    eng = WatchEngine(client=None, config=config, notifiers=[])

    eng.save_state({"hello": "world"})

    with state_path.open(encoding="utf-8") as fh:
        assert json.load(fh) == {"hello": "world"}
    # No temp debris left behind in the directory.
    leftovers = [p.name for p in state_path.parent.iterdir() if p.name != "state.json"]
    assert leftovers == []


def test_save_state_after_corrupt_does_not_clobber_preserved_copy(tmp_path):
    """End-to-end: a corrupt baseline is preserved, the next save writes a fresh
    valid file, and the .corrupt copy survives for inspection."""
    state_path = tmp_path / "state.json"
    state_path.write_text("not json at all", encoding="utf-8")
    config = Config(state_path=str(state_path))
    eng = WatchEngine(client=None, config=config, notifiers=[])

    assert eng.load_state() == {}
    eng.save_state({"fresh": True})

    assert (tmp_path / "state.json.corrupt").read_text(encoding="utf-8") == (
        "not json at all"
    )
    with state_path.open(encoding="utf-8") as fh:
        assert json.load(fh) == {"fresh": True}


# --------------------------------------------------------------------------- #
# Finding 20: state file + parent dir written private
# --------------------------------------------------------------------------- #


def test_save_state_writes_private_file_and_dir(tmp_path):
    """The state file is 0o600 and its created parent dir is 0o700 (finding 20)."""
    state_path = tmp_path / "subdir" / "state.json"
    config = Config(state_path=str(state_path))
    eng = WatchEngine(client=None, config=config, notifiers=[])

    eng.save_state({"x": 1})

    file_mode = stat.S_IMODE(os.stat(state_path).st_mode)
    dir_mode = stat.S_IMODE(os.stat(state_path.parent).st_mode)
    assert file_mode == 0o600
    assert dir_mode == 0o700


# --------------------------------------------------------------------------- #
# Finding 3: dispatch resilience + run_forever does not die on errors
# --------------------------------------------------------------------------- #


def test_dispatch_one_raising_notifier_does_not_block_others():
    """A raising notifier must not drop other channels or remaining alerts."""
    good = _RecordingNotifier()
    eng = WatchEngine(
        client=None,
        config=Config(),
        notifiers=[_RaisingNotifier(), good],
    )

    eng.dispatch([_alert("a1"), _alert("a2")])

    # The good notifier still received both alerts despite the raising one.
    assert good.got == ["a1", "a2"]


def test_run_forever_keyboardinterrupt_exits_cleanly(tmp_path):
    """KeyboardInterrupt from sleep cleanly exits the loop."""

    config = _val_watch_config(str(tmp_path / "state.json"))
    client = _StubClient(
        validators=[
            [ValidatorInfo(hotkey=HOTKEY, netuid=64, stake_tao=500.0, active=True)]
        ]
    )
    eng = WatchEngine(client, config, notifiers=[])

    def _sleep(_):
        raise KeyboardInterrupt

    # Returns cleanly (does not raise).
    eng.run_forever(sleep=_sleep)


def test_run_forever_survives_a_failing_cycle_then_exits(tmp_path):
    """A non-KeyboardInterrupt error in a cycle is logged and the loop continues
    to the next iteration rather than dying permanently (finding 3)."""

    config = _val_watch_config(str(tmp_path / "state.json"))

    calls = {"n": 0}

    class _Boom(WatchEngine):
        def run_once(self, prev_state):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient cycle failure")
            # Second iteration succeeds, then we interrupt via sleep.
            return [], {"snapshot": None}

    eng = _Boom(client=None, config=config, notifiers=[])

    sleeps = {"n": 0}

    def _sleep(_):
        sleeps["n"] += 1
        if sleeps["n"] >= 2:
            raise KeyboardInterrupt

    eng.run_forever(sleep=_sleep)

    # The first cycle raised but the loop kept going to a second cycle.
    assert calls["n"] == 2
