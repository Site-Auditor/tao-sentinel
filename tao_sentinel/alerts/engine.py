"""The watch engine: snapshotting, rule evaluation, state, and the run loop.

:class:`WatchEngine` ties together a Taostats client, a :class:`Config`, and a
list of :class:`~tao_sentinel.alerts.notify.Notifier` instances. It takes
rate-frugal snapshots of only the data the active watches need, evaluates the
rule registry against the previous snapshot, persists state to disk, and can
run forever dispatching alerts to the configured notifiers.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from tao_sentinel.config import Config
from tao_sentinel.models import Alert, Pool, StakePosition, SubnetInfo, ValidatorInfo

from tao_sentinel.alerts.notify import Notifier
from tao_sentinel.alerts.rules import RULES

logger = logging.getLogger(__name__)

#: TTL for the in-memory per-netuid alpha-price history cache used by the
#: ``price_trend`` watch. The contract pins this at 6 hours: history is slow-
#: moving and the free-tier API budget cannot absorb a fetch every tick.
_HISTORY_TTL_SECONDS = 6 * 3600

#: Trailing window (hours) requested for ``price_trend`` history fetches.
_HISTORY_HOURS = 24


class WatchEngine:
    """Evaluate configured watches against periodic API snapshots.

    Args:
        client: A Taostats client (real or mock) exposing ``get_pools``,
            ``get_stake_balances``, ``get_subnets`` and ``get_validators``.
        config: The loaded :class:`~tao_sentinel.config.Config`.
        notifiers: Notifiers to dispatch alerts to in :meth:`run_forever`.
    """

    def __init__(
        self,
        client: Any,
        config: Config,
        notifiers: list[Notifier] | None = None,
        clock=time.monotonic,
    ) -> None:
        """Store the client, config and notifiers.

        Args:
            clock: Monotonic time source (seconds) used to age the in-memory
                history cache; injectable so tests can advance time without
                sleeping.
        """
        self.client = client
        self.config = config
        self.notifiers: list[Notifier] = list(notifiers or [])
        self._clock = clock
        # In-memory 6h-TTL history cache for ``price_trend``: maps netuid ->
        # (fetched_at_monotonic, [PricePoint, ...]). Not persisted; the last
        # fetched series itself rides along in the snapshot so a restart still
        # has a baseline to evaluate against on the next tick.
        self._history_cache: dict[int, tuple[float, list[Any]]] = {}

    # ------------------------------------------------------------------ #
    # Snapshotting
    # ------------------------------------------------------------------ #
    def _needed_data(
        self,
    ) -> tuple[bool, bool, set[str], set[int], bool, set[int]]:
        """Inspect active watches and report what data must be fetched.

        Returns a tuple of::

            (need_pools, need_subnets, coldkeys, validator_netuids,
             need_tao_price, history_netuids)

        Being precise here keeps us rate-frugal: pools/subnets are fetched at
        most once each, stakes only for watched coldkeys, validators only for
        watched netuids, the TAO/USD price only when a ``tao_price`` watch is
        active, and per-netuid history only for ``price_trend`` watches (and
        even then through a 6h cache, so most ticks spend zero history calls).
        ``market_cap`` and ``new_subnet`` reuse the pools source;
        ``registration_cost`` reuses the subnets source - none of those add
        extra calls.
        """
        need_pools = False
        need_subnets = False
        need_tao_price = False
        coldkeys: set[str] = set()
        validator_netuids: set[int] = set()
        history_netuids: set[int] = set()

        for watch in self.config.watches:
            if watch.type == "price_change":
                need_pools = True
            elif watch.type == "stake_change":
                if watch.coldkey:
                    coldkeys.add(watch.coldkey)
            elif watch.type == "validator_dereg":
                if watch.netuid is not None:
                    validator_netuids.add(watch.netuid)
            elif watch.type == "emission_shift":
                need_subnets = True
            elif watch.type == "tao_price":
                need_tao_price = True
            elif watch.type == "market_cap":
                need_pools = True
            elif watch.type == "new_subnet":
                need_pools = True
            elif watch.type == "registration_cost":
                need_subnets = True
            elif watch.type == "price_trend":
                need_pools = True  # for the subnet name on the alert
                if watch.netuid is not None:
                    history_netuids.add(watch.netuid)
            else:
                logger.warning("Unknown watch type %r; skipping", watch.type)

        # ``need_subnets`` is a boolean because the subnets endpoint returns all
        # subnets in one (paginated) call; same for pools and the TAO price.
        return (
            need_pools,
            need_subnets,
            coldkeys,
            validator_netuids,
            need_tao_price,
            history_netuids,
        )

    def take_snapshot(self, prev_snapshot: dict | None = None) -> dict:
        """Fetch exactly the data the active watches require.

        Returns a snapshot dict with keys ``pools``, ``subnets``, ``stakes``,
        ``validators`` and ``timestamp``.

        A failed fetch on one source must NOT look like "this data is gone":
        substituting empty data would make the rules report every prior
        validator as deregistered and every prior stake position as removed
        (both ``critical``) on any transient 429/5xx, and would then poison the
        saved baseline. Instead, when a source fails we carry forward that
        source's data from ``prev_snapshot`` if available (so the rules see no
        change and emit nothing), and only fall back to empty when there is no
        prior data to carry forward. Either way the failure is logged.

        Args:
            prev_snapshot: The previous snapshot (deserialized), used to carry
                forward data for any source whose fetch fails this tick.
        """
        (
            need_pools,
            need_subnets,
            coldkeys,
            validator_netuids,
            need_tao_price,
            history_netuids,
        ) = self._needed_data()
        prev_snapshot = prev_snapshot or {}

        pools: dict[int, Pool] = {}
        subnets: dict[int, SubnetInfo] = {}
        stakes: dict[str, list[StakePosition]] = {}
        validators: dict[int, list[ValidatorInfo]] = {}

        if need_pools:
            try:
                for pool in self.client.get_pools():
                    pools[pool.netuid] = pool
            except Exception as exc:
                logger.warning("Failed to fetch pools: %s; carrying forward.", exc)
                pools = dict(prev_snapshot.get("pools", {}))

        if need_subnets:
            try:
                for subnet in self.client.get_subnets():
                    subnets[subnet.netuid] = subnet
            except Exception as exc:
                logger.warning("Failed to fetch subnets: %s; carrying forward.", exc)
                subnets = dict(prev_snapshot.get("subnets", {}))

        prev_stakes = prev_snapshot.get("stakes", {})
        for coldkey in coldkeys:
            try:
                stakes[coldkey] = list(self.client.get_stake_balances(coldkey))
            except Exception as exc:
                logger.warning(
                    "Failed to fetch stakes for %s: %s; carrying forward.",
                    coldkey,
                    exc,
                )
                # Carry forward prior positions so a flaky fetch is not read as
                # "stake removed". Omit the key entirely if we have no prior
                # data (the rules treat a missing key the same as no positions,
                # but with nothing prior there is also nothing to compare).
                if coldkey in prev_stakes:
                    stakes[coldkey] = list(prev_stakes[coldkey])

        prev_validators = prev_snapshot.get("validators", {})
        for netuid in validator_netuids:
            try:
                validators[netuid] = list(self.client.get_validators(netuid))
            except Exception as exc:
                logger.warning(
                    "Failed to fetch validators for netuid %s: %s; "
                    "carrying forward.",
                    netuid,
                    exc,
                )
                # Carry forward prior validators so a flaky fetch is not read as
                # a mass deregistration.
                if netuid in prev_validators:
                    validators[netuid] = list(prev_validators[netuid])

        snapshot: dict[str, Any] = {
            "pools": pools,
            "subnets": subnets,
            "stakes": stakes,
            "validators": validators,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # ---- TAO/USD spot (1 call/tick, only when a tao_price watch exists) --
        if need_tao_price:
            tao_price = self._fetch_tao_price()
            if tao_price is None:
                # Carry forward the prior price so a flaky fetch is read as
                # "no change" rather than dropping the baseline.
                prev_price = prev_snapshot.get("tao_price")
                if prev_price is not None:
                    snapshot["tao_price"] = prev_price
            else:
                snapshot["tao_price"] = tao_price

        # ---- Per-netuid alpha-price history (6h-cached; price_trend only) ----
        if history_netuids:
            prev_history = prev_snapshot.get("history", {}) or {}
            history: dict[str, list[list]] = {}
            for netuid in history_netuids:
                series = self._history_for(netuid)
                if series is None:
                    # Fetch failed and nothing cached: carry forward the prior
                    # series so the trend rule still has a baseline.
                    carried = prev_history.get(str(netuid)) or prev_history.get(
                        netuid
                    )
                    if carried:
                        history[str(netuid)] = [list(p) for p in carried]
                    continue
                history[str(netuid)] = [[p.timestamp, p.value] for p in series]
            if history:
                snapshot["history"] = history

        return snapshot

    def _fetch_tao_price(self) -> float | None:
        """Fetch the latest TAO/USD spot via the price-history endpoint.

        Uses the most recent point of a short history series (1 call). Returns
        ``None`` on any failure so the caller can carry forward the prior price
        rather than dropping the baseline.
        """
        try:
            series = self.client.get_tao_price_history(hours=_HISTORY_HOURS)
        except Exception as exc:
            logger.warning("Failed to fetch TAO price: %s; carrying forward.", exc)
            return None
        if not series:
            return None
        # Series is chronological ascending; the last point is the latest spot.
        return float(series[-1].value)

    def _history_for(self, netuid: int) -> list[Any] | None:
        """Return the cached/fetched alpha-price history for ``netuid``.

        Honors a 6h in-memory TTL keyed on the injected monotonic clock so the
        free-tier budget is not blown by per-tick fetches. The cache is
        LRU-capped at 16 entries (the most recently used netuids survive). On a
        fetch failure with no usable cache entry, returns ``None`` so the caller
        can carry forward a prior series.
        """
        now = self._clock()
        cached = self._history_cache.get(netuid)
        if cached is not None and (now - cached[0]) < _HISTORY_TTL_SECONDS:
            # Refresh LRU ordering on a hit.
            self._history_cache[netuid] = self._history_cache.pop(netuid)
            return cached[1]

        try:
            series = list(
                self.client.get_pool_history(netuid, hours=_HISTORY_HOURS)
            )
        except Exception as exc:
            logger.warning(
                "Failed to fetch history for netuid %s: %s; using cache/baseline.",
                netuid,
                exc,
            )
            # Serve a stale cache entry if we have one rather than nothing.
            return cached[1] if cached is not None else None

        self._history_cache[netuid] = (now, series)
        # LRU cap at 16: evict the oldest-inserted entries beyond the cap.
        while len(self._history_cache) > 16:
            oldest = next(iter(self._history_cache))
            del self._history_cache[oldest]
        return series

    # ------------------------------------------------------------------ #
    # Evaluation
    # ------------------------------------------------------------------ #
    def evaluate(self, prev: dict, now: dict) -> list[Alert]:
        """Run every configured watch's rule against ``prev`` and ``now``.

        Unknown watch types are skipped with a warning. A rule that raises is
        logged and skipped so one bad watch cannot suppress the others.
        """
        alerts: list[Alert] = []
        for watch in self.config.watches:
            rule = RULES.get(watch.type)
            if rule is None:
                logger.warning("No rule registered for watch type %r", watch.type)
                continue
            try:
                alerts.extend(rule(watch, prev, now))
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "Rule %r raised while evaluating: %s", watch.type, exc
                )
        return alerts

    def run_once(self, prev_state: dict | None) -> tuple[list[Alert], dict]:
        """Take a fresh snapshot, evaluate against ``prev_state``, return state.

        On the very first run (``prev_state`` empty / without a snapshot) no
        alerts are produced - we only have a baseline to compare against on the
        next run. Returns ``(alerts, new_state)`` where ``new_state`` embeds the
        latest snapshot under the ``snapshot`` key (JSON-serialized form).
        """
        prev_state = prev_state or {}
        # A valid-JSON-but-stale-schema snapshot (a model field renamed/removed/
        # retyped between versions, or a hand-edit that still parses as JSON)
        # makes _deserialize_snapshot raise inside pydantic. Guard it so a
        # poisoned baseline never crashes the loop: log a warning and treat it
        # as no baseline so the next tick re-establishes a fresh one.
        try:
            prev_snapshot = self._deserialize_snapshot(prev_state.get("snapshot"))
        except Exception as exc:
            logger.warning(
                "Failed to deserialize previous snapshot (%s); "
                "treating as no baseline and re-establishing.",
                exc,
            )
            prev_snapshot = None

        # Carry forward prior per-source data on a transient fetch failure so a
        # flaky API does not look like a mass deregistration / stake wipe and so
        # the good baseline is not overwritten with empty data.
        now_snapshot = self.take_snapshot(prev_snapshot)

        if prev_snapshot is None:
            logger.info("No previous snapshot; establishing baseline.")
            alerts: list[Alert] = []
        else:
            alerts = self.evaluate(prev_snapshot, now_snapshot)

        new_state = {
            "snapshot": self._serialize_snapshot(now_snapshot),
            "last_run": now_snapshot["timestamp"],
            "last_alerts": [a.model_dump() for a in alerts],
            # Carry the cooldown ledger forward untouched here; ``dispatch``
            # prunes/updates it when alerts are actually sent.
            "last_alerted": dict(prev_state.get("last_alerted", {})),
        }
        return alerts, new_state

    # ------------------------------------------------------------------ #
    # Snapshot (de)serialization for state persistence
    # ------------------------------------------------------------------ #
    @staticmethod
    def _serialize_snapshot(snapshot: dict) -> dict:
        """Convert a snapshot of pydantic models into JSON-safe primitives."""
        out: dict[str, Any] = {
            "pools": {
                str(k): v.model_dump() for k, v in snapshot.get("pools", {}).items()
            },
            "subnets": {
                str(k): v.model_dump() for k, v in snapshot.get("subnets", {}).items()
            },
            "stakes": {
                k: [p.model_dump() for p in v]
                for k, v in snapshot.get("stakes", {}).items()
            },
            "validators": {
                str(k): [val.model_dump() for val in v]
                for k, v in snapshot.get("validators", {}).items()
            },
            "timestamp": snapshot.get("timestamp"),
        }
        # Optional v0.2.0 keys; already JSON-safe (a float and a list-of-pairs).
        if "tao_price" in snapshot:
            out["tao_price"] = snapshot["tao_price"]
        if "history" in snapshot:
            out["history"] = {
                str(k): [[p[0], p[1]] for p in v]
                for k, v in snapshot["history"].items()
            }
        return out

    @staticmethod
    def _deserialize_snapshot(raw: dict | None) -> dict | None:
        """Rebuild a snapshot of pydantic models from its serialized form.

        Returns ``None`` if ``raw`` is falsy so callers can detect the baseline
        case.
        """
        if not raw:
            return None
        snapshot: dict[str, Any] = {
            "pools": {
                int(k): Pool(**v) for k, v in (raw.get("pools") or {}).items()
            },
            "subnets": {
                int(k): SubnetInfo(**v) for k, v in (raw.get("subnets") or {}).items()
            },
            "stakes": {
                k: [StakePosition(**p) for p in v]
                for k, v in (raw.get("stakes") or {}).items()
            },
            "validators": {
                int(k): [ValidatorInfo(**val) for val in v]
                for k, v in (raw.get("validators") or {}).items()
            },
            "timestamp": raw.get("timestamp"),
        }
        # Optional v0.2.0 keys: history is kept keyed by the stringified netuid
        # (the rules accept either form) and as plain ``[ts, value]`` pairs.
        if raw.get("tao_price") is not None:
            snapshot["tao_price"] = raw["tao_price"]
        if raw.get("history"):
            snapshot["history"] = {
                str(k): [[p[0], p[1]] for p in v]
                for k, v in raw["history"].items()
            }
        return snapshot

    # ------------------------------------------------------------------ #
    # State persistence
    # ------------------------------------------------------------------ #
    def _state_file(self) -> Path:
        """Return the resolved path to the JSON state file."""
        return Path(os.path.expanduser(self.config.state_path))

    def load_state(self) -> dict:
        """Load persisted state from disk, returning ``{}`` if none exists.

        A corrupt (non-JSON) state file is a real event worth surfacing: it
        signals a crash mid-save or external tampering. Rather than silently
        conflating it with "no file", we log a WARNING and best-effort rename
        the corrupt file to ``<name>.corrupt`` so it is preserved for inspection
        and is not silently overwritten on the next save, then return ``{}``.
        """
        path = self._state_file()
        if not path.exists():
            return {}
        try:
            with path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except json.JSONDecodeError as exc:
            logger.warning(
                "State file %s is corrupt (%s); preserving as <name>.corrupt "
                "and starting from no baseline.",
                path,
                exc,
            )
            corrupt = path.with_name(path.name + ".corrupt")
            try:
                os.replace(path, corrupt)
            except OSError as rename_exc:
                logger.warning(
                    "Failed to preserve corrupt state file %s: %s",
                    path,
                    rename_exc,
                )
            return {}
        except OSError as exc:
            logger.warning("Failed to load state from %s: %s", path, exc)
            return {}

    def save_state(self, state: dict) -> None:
        """Persist ``state`` as JSON atomically, creating the dir if needed.

        Writes to a temporary file in the SAME directory (so ``os.replace`` is
        an atomic rename on the same filesystem), flushes and fsyncs the data,
        chmods it to ``0o600``, then atomically replaces the target. This
        guarantees :meth:`load_state` only ever sees a complete prior state or
        the previous good one - never a truncated file from a crash mid-write.
        The parent directory is created with mode ``0o700`` since the state
        contains privacy-sensitive watched wallet addresses and holdings.
        """
        path = self._state_file()
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, tmp_name = tempfile.mkstemp(
                dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(state, fh, indent=2)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.chmod(tmp_name, 0o600)
                os.replace(tmp_name, path)
            except BaseException:
                # Clean up the temp file on any failure so we never leave
                # ``.tmp`` debris behind.
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
        except OSError as exc:
            logger.warning("Failed to save state to %s: %s", path, exc)

    # ------------------------------------------------------------------ #
    # Dispatch + loop
    # ------------------------------------------------------------------ #
    #: Severity rank for escalation comparisons (higher == more severe).
    _SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}

    @staticmethod
    def _cooldown_key(alert: Alert) -> str:
        """Build the dedup key for ``alert``: ``rule_type|netuid|coldkey|hotkey``.

        ``netuid`` comes off the alert; ``coldkey``/``hotkey`` are not carried on
        the :class:`Alert` model, so they are absent (empty) here - the key is
        still stable per (rule_type, netuid), which is the natural granularity
        for the subnet- and price-oriented rules. Missing components render as
        empty strings so the key shape is constant.
        """
        netuid = "" if alert.netuid is None else str(alert.netuid)
        return f"{alert.rule_type}|{netuid}||"

    def _apply_cooldown(
        self, alerts: list[Alert], last_alerted: dict
    ) -> list[Alert]:
        """Filter ``alerts`` against the cooldown ledger, updating it in place.

        An alert is suppressed when an identical key fired within
        ``config.alert_cooldown_minutes`` UNLESS its severity escalated above
        the severity recorded for that key (info < warning < critical). A
        cooldown of ``0`` disables suppression entirely. Surviving alerts have
        their ``(timestamp, severity)`` recorded under their key so the next
        tick can compare against them; the ledger persists in the state file.
        """
        cooldown_minutes = getattr(self.config, "alert_cooldown_minutes", 60)
        if cooldown_minutes <= 0:
            # Disabled: everything passes, but still record for downstream view.
            for alert in alerts:
                last_alerted[self._cooldown_key(alert)] = {
                    "timestamp": alert.timestamp,
                    "severity": alert.severity,
                }
            return list(alerts)

        window = timedelta(minutes=cooldown_minutes)
        now = datetime.now(timezone.utc)
        kept: list[Alert] = []
        # Keys first written during THIS batch must not suppress later alerts in
        # the same batch: distinct simultaneous subjects (e.g. two validators
        # deregistering on one subnet in one tick) collapse onto the same
        # (rule_type, netuid) key because coldkey/hotkey are absent from the
        # Alert model. Without this guard the first such alert would silently
        # suppress every other genuinely-distinct same-tick event. Cross-tick
        # repeats still dedup because those priors come from ``last_alerted``.
        seen_this_batch: set[str] = set()
        for alert in alerts:
            key = self._cooldown_key(alert)
            prior = last_alerted.get(key)
            suppressed = False
            if prior is not None and key not in seen_this_batch:
                last_ts = self._parse_iso(prior.get("timestamp"))
                within_window = last_ts is not None and (now - last_ts) < window
                prior_rank = self._SEVERITY_RANK.get(prior.get("severity"), 0)
                this_rank = self._SEVERITY_RANK.get(alert.severity, 0)
                escalated = this_rank > prior_rank
                if within_window and not escalated:
                    suppressed = True
            if suppressed:
                logger.debug("Cooldown suppressed alert %r (key=%s)", alert.title, key)
                continue
            kept.append(alert)
            seen_this_batch.add(key)
            last_alerted[key] = {
                "timestamp": alert.timestamp,
                "severity": alert.severity,
            }
        return kept

    @staticmethod
    def _parse_iso(value: str | None) -> datetime | None:
        """Parse an ISO-8601 timestamp into an aware UTC datetime, or ``None``."""
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(value)
        except (ValueError, TypeError):
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    def dispatch(self, alerts: list[Alert], last_alerted: dict | None = None) -> None:
        """Dedup via cooldown, then deliver the survivors to every notifier.

        First applies cooldown suppression (mutating ``last_alerted`` in place if
        provided, so the caller can persist the updated ledger). Then each
        notifier receives the surviving batch via :meth:`Notifier.send_many` -
        exactly once - so a notifier that batches (Telegram digest) sends a
        single combined message while per-alert channels (webhook) still POST
        one body each.

        Notifiers are documented to never raise, but the :class:`Notifier` ABC
        is public for extension and a user-supplied (or buggy built-in) channel
        may still raise. Each ``send_many`` is guarded so one bad channel can
        never drop the others.
        """
        if last_alerted is None:
            last_alerted = {}
        to_send = self._apply_cooldown(alerts, last_alerted)
        if not to_send:
            return
        for notifier in self.notifiers:
            try:
                notifier.send_many(to_send)
            except Exception as exc:
                logger.warning(
                    "Notifier %s raised while sending a batch of %d alert(s): %s",
                    type(notifier).__name__,
                    len(to_send),
                    exc,
                )

    def run_forever(self, sleep=time.sleep) -> None:
        """Poll on ``config.poll_interval_seconds``, dispatching alerts forever.

        Loads persisted state on startup, then repeatedly runs a single cycle,
        saves the new state, dispatches any alerts, and sleeps. The ``sleep``
        callable is injectable for testing. Each iteration is wrapped so that a
        transient per-cycle error (a fetch blowing up in an unexpected way,
        serialization, dispatch, etc.) is logged and the loop sleeps and
        continues rather than dying permanently. The loop exits cleanly only on
        :class:`KeyboardInterrupt`.
        """
        state = self.load_state()
        logger.info(
            "Starting watch loop: %d watch(es), poll every %ds",
            len(self.config.watches),
            self.config.poll_interval_seconds,
        )
        while True:
            try:
                alerts, state = self.run_once(state)
                if alerts:
                    logger.info("Dispatching %d alert(s)", len(alerts))
                    # ``dispatch`` mutates the cooldown ledger in place; persist
                    # the updated ledger AFTER dispatch so a suppressed-then-
                    # escalated alert is tracked across restarts.
                    last_alerted = state.setdefault("last_alerted", {})
                    self.dispatch(alerts, last_alerted)
                self.save_state(state)
            except KeyboardInterrupt:  # pragma: no cover - interactive only
                logger.info("Watch loop interrupted; exiting.")
                return
            except Exception:
                logger.exception(
                    "Watch cycle failed; will retry after the poll interval."
                )
            try:
                sleep(self.config.poll_interval_seconds)
            except KeyboardInterrupt:  # pragma: no cover - interactive only
                logger.info("Watch loop interrupted; exiting.")
                return
