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
from datetime import datetime, timedelta, timezone

from tao_sentinel.alerts.engine import WatchEngine
from tao_sentinel.alerts.notify import Notifier, TelegramNotifier
from tao_sentinel.config import Config, WatchConfig
from tao_sentinel.models import (
    Alert,
    Pool,
    PricePoint,
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


class _BatchRecordingNotifier(Notifier):
    """Records each ``send_many`` batch so we can assert one-call batching."""

    def __init__(self):
        self.batches: list[list[str]] = []

    def send(self, alert: Alert) -> None:  # pragma: no cover - default path unused
        self.batches.append([alert.title])

    def send_many(self, alerts: list[Alert]) -> None:
        self.batches.append([a.title for a in alerts])


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


# --------------------------------------------------------------------------- #
# C2: new sources in _needed_data + take_snapshot (tao_price, history cache)
# --------------------------------------------------------------------------- #


class _HistoryClient(_StubClient):
    """A stub client that also serves tao-price and per-netuid pool history.

    Counts each history/price call so tests can prove the 6h cache spends zero
    extra calls on a warm tick.
    """

    def __init__(self, *, tao_price_series=None, pool_history=None, **kwargs):
        super().__init__(**kwargs)
        self._tao_price_series = tao_price_series
        self._pool_history = pool_history or {}
        self.tao_price_calls = 0
        self.pool_history_calls: dict[int, int] = {}

    def get_tao_price_history(self, hours: int = 24):
        self.tao_price_calls += 1
        if isinstance(self._tao_price_series, Exception) or (
            isinstance(self._tao_price_series, type)
            and issubclass(self._tao_price_series, Exception)
        ):
            raise (
                self._tao_price_series
                if isinstance(self._tao_price_series, Exception)
                else self._tao_price_series("boom")
            )
        return list(self._tao_price_series or [])

    def get_pool_history(self, netuid: int, hours: int = 24):
        self.pool_history_calls[netuid] = self.pool_history_calls.get(netuid, 0) + 1
        series = self._pool_history.get(netuid)
        if isinstance(series, Exception) or (
            isinstance(series, type) and issubclass(series, Exception)
        ):
            raise series if isinstance(series, Exception) else series("boom")
        return list(series or [])


def _pp(hour: int, value: float) -> PricePoint:
    return PricePoint(timestamp=f"2026-06-03T{hour:02d}:00:00+00:00", value=value)


def test_take_snapshot_records_tao_price_from_latest_point(tmp_path):
    """A tao_price watch fetches the price series and stores the latest spot."""
    config = Config(
        watches=[WatchConfig(type="tao_price", threshold_pct=5.0)],
        state_path=str(tmp_path / "s.json"),
    )
    client = _HistoryClient(tao_price_series=[_pp(0, 200.0), _pp(1, 210.0)])
    eng = WatchEngine(client, config, notifiers=[])

    snap = eng.take_snapshot(None)

    assert snap["tao_price"] == 210.0  # last (latest) point
    assert client.tao_price_calls == 1


def test_take_snapshot_carries_forward_tao_price_on_failure(tmp_path):
    """A failed price fetch carries forward the prior price (no baseline drop)."""
    config = Config(
        watches=[WatchConfig(type="tao_price", threshold_pct=5.0)],
        state_path=str(tmp_path / "s.json"),
    )
    client = _HistoryClient(tao_price_series=RuntimeError)
    eng = WatchEngine(client, config, notifiers=[])

    snap = eng.take_snapshot({"tao_price": 199.0})

    assert snap["tao_price"] == 199.0


def test_history_cache_is_6h_ttl_and_avoids_extra_calls(tmp_path):
    """price_trend history is fetched once and reused within the 6h TTL."""
    config = Config(
        watches=[WatchConfig(type="price_trend", netuid=1, threshold_pct=10.0)],
        state_path=str(tmp_path / "s.json"),
    )
    client = _HistoryClient(
        pools=[[Pool(netuid=1, name="apex", price_tao=0.02)]] * 3,
        pool_history={1: [_pp(0, 0.02), _pp(1, 0.03)]},
    )
    fake_time = {"t": 1000.0}
    eng = WatchEngine(client, config, notifiers=[], clock=lambda: fake_time["t"])

    snap1 = eng.take_snapshot(None)
    assert snap1["history"]["1"] == [
        ["2026-06-03T00:00:00+00:00", 0.02],
        ["2026-06-03T01:00:00+00:00", 0.03],
    ]
    assert client.pool_history_calls[1] == 1

    # 1h later: still inside the 6h TTL -> no new call.
    fake_time["t"] += 3600
    eng.take_snapshot(snap1)
    assert client.pool_history_calls[1] == 1

    # 7h later: TTL expired -> one refresh.
    fake_time["t"] += 7 * 3600
    eng.take_snapshot(snap1)
    assert client.pool_history_calls[1] == 2


def test_history_cache_lru_capped_at_16(tmp_path):
    """The per-netuid history cache never holds more than 16 entries."""
    netuids = list(range(1, 21))
    config = Config(
        watches=[
            WatchConfig(type="price_trend", netuid=n, threshold_pct=10.0)
            for n in netuids
        ],
        state_path=str(tmp_path / "s.json"),
    )
    client = _HistoryClient(
        pools=[[Pool(netuid=n, name=f"s{n}", price_tao=0.02) for n in netuids]],
        pool_history={n: [_pp(0, 0.02), _pp(1, 0.03)] for n in netuids},
    )
    eng = WatchEngine(client, config, notifiers=[])

    eng.take_snapshot(None)

    assert len(eng._history_cache) == 16


def test_history_carry_forward_on_fetch_failure(tmp_path):
    """A failed history fetch with empty cache carries forward the prior series."""
    config = Config(
        watches=[WatchConfig(type="price_trend", netuid=1, threshold_pct=10.0)],
        state_path=str(tmp_path / "s.json"),
    )
    client = _HistoryClient(
        pools=[[Pool(netuid=1, name="apex", price_tao=0.02)]],
        pool_history={1: RuntimeError},
    )
    eng = WatchEngine(client, config, notifiers=[])

    prev = {"history": {"1": [["2026-06-03T00:00:00+00:00", 0.02]]}}
    snap = eng.take_snapshot(prev)

    assert snap["history"]["1"] == [["2026-06-03T00:00:00+00:00", 0.02]]


def test_snapshot_round_trip_preserves_tao_price_and_history():
    """tao_price and history survive serialize -> deserialize unchanged."""
    snapshot = {
        "pools": {},
        "subnets": {},
        "stakes": {},
        "validators": {},
        "timestamp": "2026-06-03T00:00:00+00:00",
        "tao_price": 210.0,
        "history": {"1": [["2026-06-03T00:00:00+00:00", 0.02]]},
    }
    serialized = WatchEngine._serialize_snapshot(snapshot)
    restored = WatchEngine._deserialize_snapshot(serialized)

    assert restored["tao_price"] == 210.0
    assert restored["history"] == {"1": [["2026-06-03T00:00:00+00:00", 0.02]]}


def test_price_trend_end_to_end_fires_via_engine(tmp_path):
    """A drifting 24h history fires price_trend on the second (post-baseline) tick."""
    config = Config(
        watches=[WatchConfig(type="price_trend", netuid=1, threshold_pct=10.0)],
        state_path=str(tmp_path / "s.json"),
        alert_cooldown_minutes=0,
    )
    client = _HistoryClient(
        pools=[[Pool(netuid=1, name="apex", price_tao=0.03)]] * 2,
        pool_history={1: [_pp(0, 0.02), _pp(23, 0.03)]},  # +50%
    )
    eng = WatchEngine(client, config, notifiers=[])

    _, state1 = eng.run_once(None)  # baseline
    alerts2, _ = eng.run_once(state1)

    assert [a.rule_type for a in alerts2] == ["price_trend"]
    assert alerts2[0].severity == "critical"


# --------------------------------------------------------------------------- #
# C3: cooldown dedup + severity escalation + persistence
# --------------------------------------------------------------------------- #


def _info(rule="price_change", netuid=1, severity="info", ts=None) -> Alert:
    return Alert(
        rule_type=rule,
        severity=severity,
        title=f"{rule}-{netuid}-{severity}",
        message="m",
        netuid=netuid,
        timestamp=ts or datetime.now(timezone.utc).isoformat(),
    )


def test_cooldown_suppresses_repeat_within_window(tmp_path):
    """An identical (rule_type, netuid) key within the window is suppressed."""
    config = Config(alert_cooldown_minutes=60)
    rec = _BatchRecordingNotifier()
    eng = WatchEngine(client=None, config=config, notifiers=[rec])

    ledger: dict = {}
    eng.dispatch([_info()], ledger)
    eng.dispatch([_info()], ledger)  # same key, still in cooldown -> suppressed

    # Only the first batch was delivered.
    assert rec.batches == [["price_change-1-info"]]


def test_cooldown_allows_escalated_severity(tmp_path):
    """A repeat within the window still fires if its severity escalated."""
    config = Config(alert_cooldown_minutes=60)
    rec = _BatchRecordingNotifier()
    eng = WatchEngine(client=None, config=config, notifiers=[rec])

    ledger: dict = {}
    eng.dispatch([_info(severity="warning")], ledger)
    # Same key, escalated warning -> critical: must pass despite the cooldown.
    eng.dispatch([_info(severity="critical")], ledger)
    # A second critical (no further escalation) is suppressed.
    eng.dispatch([_info(severity="critical")], ledger)

    assert rec.batches == [
        ["price_change-1-warning"],
        ["price_change-1-critical"],
    ]
    assert ledger["price_change|1||"]["severity"] == "critical"


def test_cooldown_zero_disables_suppression(tmp_path):
    """alert_cooldown_minutes == 0 lets every alert through."""
    config = Config(alert_cooldown_minutes=0)
    rec = _BatchRecordingNotifier()
    eng = WatchEngine(client=None, config=config, notifiers=[rec])

    ledger: dict = {}
    eng.dispatch([_info()], ledger)
    eng.dispatch([_info()], ledger)

    assert rec.batches == [["price_change-1-info"], ["price_change-1-info"]]


def test_cooldown_expired_window_fires_again(tmp_path):
    """A repeat after the window elapses fires again."""
    config = Config(alert_cooldown_minutes=60)
    rec = _BatchRecordingNotifier()
    eng = WatchEngine(client=None, config=config, notifiers=[rec])

    # Seed the ledger with an OLD timestamp (2h ago) for the key.
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    ledger = {"price_change|1||": {"timestamp": old, "severity": "info"}}
    eng.dispatch([_info()], ledger)

    assert rec.batches == [["price_change-1-info"]]


def test_cooldown_persists_across_run_once_state(tmp_path):
    """The cooldown ledger round-trips through run_once state and the state file."""
    config = Config(
        watches=[WatchConfig(type="price_change", netuid=1, threshold_pct=10.0)],
        state_path=str(tmp_path / "s.json"),
        alert_cooldown_minutes=60,
    )
    client = _StubClient(pools=[[Pool(netuid=1, name="apex", price_tao=0.02)]])
    eng = WatchEngine(client, config, notifiers=[])

    _, state = eng.run_once(None)
    # Simulate a dispatch having recorded a key, then persist + reload.
    ledger = state.setdefault("last_alerted", {})
    eng.dispatch([_info()], ledger)
    eng.save_state(state)

    reloaded = eng.load_state()
    assert "price_change|1||" in reloaded["last_alerted"]


# --------------------------------------------------------------------------- #
# C3: send_many batching (one call per notifier per dispatch)
# --------------------------------------------------------------------------- #


def test_dispatch_calls_send_many_once_per_notifier(tmp_path):
    """dispatch delivers the whole surviving batch via a single send_many call."""
    config = Config(alert_cooldown_minutes=0)
    rec = _BatchRecordingNotifier()
    eng = WatchEngine(client=None, config=config, notifiers=[rec])

    eng.dispatch(
        [_info(netuid=1), _info(netuid=4), _info(rule="emission_shift", netuid=8)],
        {},
    )

    assert len(rec.batches) == 1
    assert rec.batches[0] == [
        "price_change-1-info",
        "price_change-4-info",
        "emission_shift-8-info",
    ]


def test_dispatch_one_raising_notifier_does_not_block_others_batch(tmp_path):
    """A notifier raising in send_many must not drop the other notifier."""
    config = Config(alert_cooldown_minutes=0)
    good = _BatchRecordingNotifier()

    class _RaisingMany(Notifier):
        def send(self, alert):  # pragma: no cover
            raise RuntimeError("boom")

        def send_many(self, alerts):
            raise RuntimeError("boom")

    eng = WatchEngine(client=None, config=config, notifiers=[_RaisingMany(), good])
    eng.dispatch([_info()], {})

    assert good.batches == [["price_change-1-info"]]


# --------------------------------------------------------------------------- #
# C3: TelegramNotifier.send_many digest (one combined, severity-grouped message)
# --------------------------------------------------------------------------- #


def test_telegram_send_many_builds_one_severity_grouped_digest(monkeypatch):
    """send_many POSTs a single combined message grouped by severity."""
    posts: list[str] = []
    notifier = TelegramNotifier(bot_token="x", chat_id="y")
    monkeypatch.setattr(notifier, "_post", lambda text: posts.append(text))

    notifier.send_many(
        [
            _info(netuid=1, severity="info"),
            _info(rule="validator_dereg", netuid=64, severity="critical"),
            _info(rule="emission_shift", netuid=8, severity="warning"),
        ]
    )

    assert len(posts) == 1
    body = posts[0]
    # Critical group is listed first, then warning, then info.
    assert body.index("CRITICAL") < body.index("WARNING") < body.index("INFO")


def test_telegram_send_many_truncates_long_batches(monkeypatch):
    """A flood is capped under the Telegram limit with an '...and N more' marker."""
    posts: list[str] = []
    notifier = TelegramNotifier(bot_token="x", chat_id="y")
    monkeypatch.setattr(notifier, "_post", lambda text: posts.append(text))

    notifier.send_many([_info(netuid=n, severity="warning") for n in range(500)])

    assert len(posts) == 1
    body = posts[0]
    assert len(body) <= 3500
    assert "more" in body


def test_telegram_send_many_empty_sends_nothing(monkeypatch):
    """An empty batch posts nothing."""
    posts: list[str] = []
    notifier = TelegramNotifier(bot_token="x", chat_id="y")
    monkeypatch.setattr(notifier, "_post", lambda text: posts.append(text))

    notifier.send_many([])

    assert posts == []
