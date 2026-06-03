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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tao_sentinel.config import Config
from tao_sentinel.models import Alert, Pool, StakePosition, SubnetInfo, ValidatorInfo

from tao_sentinel.alerts.notify import Notifier
from tao_sentinel.alerts.rules import RULES

logger = logging.getLogger(__name__)


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
    ) -> None:
        """Store the client, config and notifiers."""
        self.client = client
        self.config = config
        self.notifiers: list[Notifier] = list(notifiers or [])

    # ------------------------------------------------------------------ #
    # Snapshotting
    # ------------------------------------------------------------------ #
    def _needed_data(self) -> tuple[bool, set[int], set[str], set[int]]:
        """Inspect active watches and report what data must be fetched.

        Returns a tuple of:
            (need_pools, subnet_netuids, coldkeys, validator_netuids)

        Being precise here keeps us rate-frugal: pools/subnets are fetched at
        most once each, stakes only for watched coldkeys, validators only for
        watched netuids.
        """
        need_pools = False
        need_subnets = False
        coldkeys: set[str] = set()
        validator_netuids: set[int] = set()

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
            else:
                logger.warning("Unknown watch type %r; skipping", watch.type)

        # ``subnet_netuids`` is represented by the boolean need_subnets because
        # the subnets endpoint returns all subnets in one call. We surface it as
        # an empty set sentinel and a bool to keep the snapshot logic simple.
        return need_pools, need_subnets, coldkeys, validator_netuids

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
        need_pools, need_subnets, coldkeys, validator_netuids = self._needed_data()
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

        return {
            "pools": pools,
            "subnets": subnets,
            "stakes": stakes,
            "validators": validators,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

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
        }
        return alerts, new_state

    # ------------------------------------------------------------------ #
    # Snapshot (de)serialization for state persistence
    # ------------------------------------------------------------------ #
    @staticmethod
    def _serialize_snapshot(snapshot: dict) -> dict:
        """Convert a snapshot of pydantic models into JSON-safe primitives."""
        return {
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

    @staticmethod
    def _deserialize_snapshot(raw: dict | None) -> dict | None:
        """Rebuild a snapshot of pydantic models from its serialized form.

        Returns ``None`` if ``raw`` is falsy so callers can detect the baseline
        case.
        """
        if not raw:
            return None
        return {
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
    def dispatch(self, alerts: list[Alert]) -> None:
        """Send each alert to every notifier.

        Notifiers are documented to never raise, but the :class:`Notifier` ABC
        is public for extension and a user-supplied (or buggy built-in) channel
        may still raise. Each ``send`` is guarded so one bad channel can never
        drop other channels or remaining alerts: a raising notifier is logged
        and skipped, and dispatch continues.
        """
        for alert in alerts:
            for notifier in self.notifiers:
                try:
                    notifier.send(alert)
                except Exception as exc:
                    logger.warning(
                        "Notifier %s raised while sending %r: %s",
                        type(notifier).__name__,
                        alert.title,
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
                self.save_state(state)
                if alerts:
                    logger.info("Dispatching %d alert(s)", len(alerts))
                    self.dispatch(alerts)
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
