"""FastAPI application factory for the tao-sentinel dashboard.

The dashboard is a single, read-only page summarising the state of the
network as tao-sentinel sees it:

* a subnet health table produced by :class:`tao_sentinel.scanner.SubnetScanner`;
* a portfolio valuation section, shown when a coldkey is configured in one of
  the watches;
* the most recent alerts, read from the watch-engine state file if present.

The factory builds a Taostats client via :func:`tao_sentinel.api.make_client`
(falling back to the deterministic mock client when ``mock`` is set or no API
key is configured), wraps the (rate-limited) client calls in a small in-process
TTL cache so a busy dashboard does not exhaust the Taostats rate limit, and
serves both an HTML view (``GET /``) and a JSON view (``GET /api/status``) of
the same data.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Optional

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..api import TaostatsError
from ..config import Config, load_config
from ..models import HealthReport, Portfolio, PricePoint
from ..portfolio import PortfolioTracker
from ..scanner import SubnetScanner

logger = logging.getLogger(__name__)

#: Default cache time-to-live, in seconds (5 minutes), chosen to respect the
#: free-tier Taostats rate limit of ~5 calls/minute.
DEFAULT_CACHE_TTL_SECONDS = 300

#: TTL for sparkline history series, in seconds (6 hours). Sparklines change
#: slowly and each series costs a live history call, so a long TTL keeps the
#: dashboard's pinned-subnet and TAO-price sparklines well within the
#: free-tier budget (1 call per pinned netuid per 6h; +1 for TAO price).
SPARK_HISTORY_TTL_SECONDS = 6 * 3600

#: TTL for the per-netuid authoritative detail page, in seconds (1 hour).
DETAIL_TTL_SECONDS = 3600

#: LRU cap on the number of distinct per-netuid detail results held at once.
#: Bounds the live history/scan calls a flood of distinct detail views can
#: trigger (at most 16 cached entries; a 17th evicts the least-recently-used).
DETAIL_CACHE_MAX = 16

#: Maximum number of pinned subnets honoured from the config watchlist (the
#: config owner validates the cap, but we defensively clamp here too).
_MAX_WATCHLIST = 12

#: Number of top validators (by stake) surfaced on a subnet detail page.
_DETAIL_TOP_VALIDATORS = 10

#: Auto-refresh interval baked into the dashboard ``<meta>`` tag, in seconds.
DASHBOARD_REFRESH_SECONDS = 300

#: Directory holding the Jinja2 templates shipped with the package.
_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

#: Directory holding the built React SPA (emitted by `npm run build` in
#: frontend/; absent in source checkouts without node -> Jinja fallback).
_STATIC_DIR = Path(__file__).resolve().parent / "static"

#: Number of recent alerts surfaced on the dashboard.
_MAX_RECENT_ALERTS = 25

#: Keys an Alert-like dict is expected to carry (used to sniff state JSON).
_ALERT_KEYS = {"rule_type", "severity", "title", "message", "timestamp"}


class _TTLCache:
    """A tiny single-process time-to-live cache.

    Values are recomputed by the supplied loader only after the configured TTL
    has elapsed. This keeps dashboard rendering cheap and, more importantly,
    keeps the number of Taostats API calls well under the rate limit even when
    the page (and its JSON sibling) are refreshed frequently.

    Thread-safety / single-flight: :meth:`get` holds a single lock across the
    whole check-then-load so that when several threadpool-dispatched request
    handlers hit a cold (or just-expired) entry at the same time, exactly one
    of them runs the loader (and its expensive Taostats calls) while the others
    wait and then reuse the freshly stored result. This is what actually keeps
    concurrent dashboard requests from stampeding the API and burning the
    free-tier quota. The deliberate tradeoff is that the lock serialises cold
    renders: a slow loader blocks every other waiter until it completes, so a
    cold render is computed once but never concurrently.
    """

    def __init__(
        self,
        ttl_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        swr_background: bool = True,
    ) -> None:
        """Initialise the cache.

        Args:
            ttl_seconds: How long a cached value remains fresh.
            clock: Monotonic clock callable (injectable for testing).
            swr_background: Refresh stale entries on a background thread
                (stale-while-revalidate). Tests set ``False`` to run the
                refresh inline for determinism (the stale value is still
                returned for the triggering call; the NEXT call sees fresh).
        """
        self._ttl = ttl_seconds
        self._clock = clock
        self._swr_background = swr_background
        self._store: dict[str, tuple[float, Any]] = {}
        self._refreshing: set[str] = set()
        self._lock = threading.Lock()

    def get(self, key: str, loader: Callable[[], Any]) -> Any:
        """Return the cached value for ``key`` (stale-while-revalidate).

        * Fresh hit: returned immediately.
        * Stale hit: the STALE value is returned immediately and ONE
          background refresh is kicked off (guarded so concurrent stale hits
          do not stampede the loader). Requests therefore never block on the
          rate-limited Taostats refresh once a value exists -- the live cold
          load takes minutes behind the 5/min limiter, which previously
          exceeded the nginx proxy timeout and 504ed the first visitor.
        * No value at all (first boot): blocks under the lock (single-flight)
          exactly as before; the app's startup warm thread normally absorbs
          this before any user request arrives.

        Args:
            key: Cache key.
            loader: Zero-argument callable producing the value on a miss.

        Returns:
            The cached (possibly stale) or freshly computed value.
        """
        with self._lock:
            now = self._clock()
            cached = self._store.get(key)
            if cached is not None:
                if (now - cached[0]) < self._ttl:
                    return cached[1]
                # Stale: serve it, refresh at most once in the background.
                if key not in self._refreshing:
                    self._refreshing.add(key)
                    if self._swr_background:
                        threading.Thread(
                            target=self._refresh,
                            args=(key, loader),
                            daemon=True,
                            name=f"ttlcache-refresh-{key}",
                        ).start()
                    else:
                        # Test mode: refresh inline (outside the lock would be
                        # nicer but determinism wins; loaders are cheap mocks).
                        self._refresh_locked(key, loader, now)
                return cached[1]
            # Cold: block and load (single-flight via the held lock).
            value = loader()
            self._store[key] = (now, value)
            return value

    def _refresh(self, key: str, loader: Callable[[], Any]) -> None:
        """Background refresh of one stale key (loader runs WITHOUT the lock)."""
        try:
            value = loader()
        except Exception as exc:  # noqa: BLE001 - keep serving stale on failure
            logger.warning("Background cache refresh failed for %r: %s", key, exc)
            with self._lock:
                self._refreshing.discard(key)
            return
        with self._lock:
            self._store[key] = (self._clock(), value)
            self._refreshing.discard(key)

    def _refresh_locked(self, key: str, loader: Callable[[], Any], now: float) -> None:
        """Inline (test-mode) refresh; caller already holds the lock."""
        try:
            value = loader()
            self._store[key] = (now, value)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Inline cache refresh failed for %r: %s", key, exc)
        finally:
            self._refreshing.discard(key)

    def clear(self) -> None:
        """Drop all cached entries."""
        with self._lock:
            self._store.clear()
            self._refreshing.clear()


class _LRUTTLCache:
    """A time-to-live cache with a least-recently-used eviction cap.

    Behaves like :class:`_TTLCache` (single-flight, monotonic TTL) but bounds
    the number of distinct keys held at once: when a fresh insert would exceed
    :attr:`_max_entries`, the least-recently-used entry is evicted. This caps
    the live API work a flood of distinct keys can trigger -- used for the
    per-netuid detail pages, where each cold key costs an authoritative scan +
    a history fetch, so an unbounded cache would let arbitrary ``/subnet/{n}``
    traffic burn the free-tier quota.

    Single-flight: the whole check-then-load runs under one lock so a concurrent
    burst for the same cold key triggers exactly one loader call. As with
    :class:`_TTLCache` this serialises cold loads (a slow loader blocks every
    waiter), the deliberate tradeoff for never stampeding the API.
    """

    def __init__(
        self,
        ttl_seconds: float,
        max_entries: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initialise the cache.

        Args:
            ttl_seconds: How long a cached value remains fresh.
            max_entries: Maximum number of distinct keys retained (LRU cap).
            clock: Monotonic clock callable (injectable for testing).
        """
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._clock = clock
        self._store: "OrderedDict[str, tuple[float, Any]]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str, loader: Callable[[], Any]) -> Any:
        """Return the cached value for ``key``, recomputing it if stale.

        A hit moves the key to most-recently-used. A miss (cold or stale) runs
        ``loader`` under the lock, stores the result as most-recently-used, and
        evicts the least-recently-used entry when the cap is exceeded.

        Args:
            key: Cache key.
            loader: Zero-argument callable producing the value on a miss.

        Returns:
            The cached or freshly computed value.
        """
        with self._lock:
            now = self._clock()
            cached = self._store.get(key)
            if cached is not None and (now - cached[0]) < self._ttl:
                self._store.move_to_end(key)
                return cached[1]
            value = loader()
            self._store[key] = (now, value)
            self._store.move_to_end(key)
            while len(self._store) > self._max_entries:
                self._store.popitem(last=False)
            return value

    def clear(self) -> None:
        """Drop all cached entries."""
        with self._lock:
            self._store.clear()


class _ValidatorMemoClient:
    """Per-load client proxy that fetches each subnet's validators at most once.

    A single-netuid detail load needs the validator list twice: once for the
    scanner's concentration scoring and once for the top-10 table. Wrapping the
    real client here memoizes ``get_validators`` per netuid (and degrades a
    :class:`TaostatsError` to an empty list) so a cold detail load makes ONE
    validator call, keeping the documented per-view budget honest. All other
    attributes delegate straight through to the wrapped client.
    """

    def __init__(self, client: object) -> None:
        """Wrap ``client``, sharing its real fetches.

        Args:
            client: The underlying Taostats client (real or mock).
        """
        self._client = client
        self._validators: dict[int, list] = {}

    def get_validators(self, netuid: int) -> list:
        """Return the (memoized) validator list for ``netuid``.

        The underlying fetch happens once per netuid; a transport/API error is
        logged and cached as an empty list so the page degrades gracefully.
        """
        if netuid not in self._validators:
            try:
                self._validators[netuid] = self._client.get_validators(netuid)
            except TaostatsError:
                logger.warning("Validator fetch failed for netuid %s.", netuid)
                self._validators[netuid] = []
        return self._validators[netuid]

    def __getattr__(self, name: str) -> Any:
        """Delegate any non-overridden attribute to the wrapped client."""
        return getattr(self._client, name)


def _downsample(series: list[PricePoint], max_points: int = 48) -> list[float]:
    """Reduce a chronological :class:`PricePoint` series to a list of floats.

    The client already downsamples history to <= 48 points (C1), so this is a
    cheap projection-to-values that also defends against an over-long series by
    evenly sampling down to ``max_points`` while always keeping the final
    (most recent) point so the sparkline's last-value dot is accurate.

    Args:
        series: Chronological-ascending price points.
        max_points: Hard cap on the number of returned values.

    Returns:
        A list of floats (possibly empty), oldest-first.
    """
    values = [p.value for p in series]
    if len(values) <= max_points:
        return values
    # Evenly stride down to max_points, then force-include the last value.
    step = len(values) / max_points
    sampled = [values[int(i * step)] for i in range(max_points)]
    sampled[-1] = values[-1]
    return sampled


def _pct_change(values: list[float]) -> Optional[float]:
    """Return the percentage change from the first to the last value.

    Args:
        values: A series of numeric values, oldest-first.

    Returns:
        The percent change (``(last - first) / first * 100``), or ``None`` when
        the series is too short or its first value is zero (undefined change).
    """
    if len(values) < 2 or values[0] == 0:
        return None
    return (values[-1] - values[0]) / values[0] * 100.0


def _load_config(config_path: Optional[str]) -> Config:
    """Load configuration from ``config_path`` or return an empty default.

    A missing or unreadable config path is tolerated: the dashboard still works
    (in mock mode, or against an env-provided key) with an empty configuration.

    Args:
        config_path: Path to a YAML config file, or ``None``.

    Returns:
        A :class:`~tao_sentinel.config.Config` instance.
    """
    if not config_path:
        return Config()
    try:
        return load_config(config_path)
    except FileNotFoundError:
        logger.warning("Config file %r not found; using defaults.", config_path)
        return Config()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Failed to load config %r (%s); using defaults.", config_path, exc)
        return Config()


def _first_watched_coldkey(config: Config) -> Optional[str]:
    """Return the first coldkey referenced by any watch, if any.

    Args:
        config: The loaded configuration.

    Returns:
        The coldkey ss58 string, or ``None`` when no watch names one.
    """
    for watch in config.watches:
        if watch.coldkey:
            return watch.coldkey
    return None


def _watchlist(config: Config) -> list[int]:
    """Return the configured pinned-subnet watchlist, clamped to the cap.

    The watchlist field is owned by the config cluster (C4); it is read
    defensively here so the dashboard degrades to "no pins" rather than
    erroring if the field is absent. Order is preserved and the list is
    clamped to :data:`_MAX_WATCHLIST` (the config owner also validates the cap).

    Args:
        config: The loaded configuration.

    Returns:
        A list of netuid ints (possibly empty), in config order, capped.
    """
    raw = getattr(config, "watchlist", None) or []
    out: list[int] = []
    for item in raw:
        try:
            out.append(int(item))
        except (TypeError, ValueError):  # pragma: no cover - defensive
            continue
    return out[:_MAX_WATCHLIST]


def _looks_like_alert(value: Any) -> bool:
    """Return whether ``value`` is a dict carrying the Alert field names."""
    return isinstance(value, dict) and _ALERT_KEYS.issubset(value.keys())


def _extract_alerts(state: Any) -> list[dict]:
    """Best-effort extraction of alert dicts from a watch-engine state blob.

    The state file is owned and written by the watch engine; this reader makes
    no assumptions about its exact schema beyond "JSON". It searches, in order,
    for a top-level ``alerts`` (or ``recent_alerts``) list of alert-shaped
    dicts, then falls back to scanning any nested list for alert-shaped dicts.

    Args:
        state: The parsed JSON state (any shape).

    Returns:
        A list of alert-shaped dicts (possibly empty).
    """
    if isinstance(state, dict):
        for key in ("alerts", "recent_alerts", "last_alerts"):
            candidate = state.get(key)
            if isinstance(candidate, list):
                found = [a for a in candidate if _looks_like_alert(a)]
                if found:
                    return found

    # Generic fallback: walk the structure for any list of alert-shaped dicts.
    found: list[dict] = []

    def _walk(node: Any) -> None:
        if _looks_like_alert(node):
            found.append(node)
        elif isinstance(node, dict):
            for child in node.values():
                _walk(child)
        elif isinstance(node, list):
            for child in node:
                _walk(child)

    _walk(state)
    return found


def _load_recent_alerts(state_path: str) -> list[dict]:
    """Load recent alerts from the watch-engine state file, if present.

    Args:
        state_path: Path to the state JSON (``~`` is expanded).

    Returns:
        Up to :data:`_MAX_RECENT_ALERTS` alert dicts, most recent first; empty
        when the file is absent or unreadable.
    """
    expanded = os.path.expanduser(state_path)
    if not os.path.exists(expanded):
        return []
    try:
        with open(expanded, "r", encoding="utf-8") as fh:
            state = json.load(fh)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Failed to read state file %r: %s", state_path, exc)
        return []

    alerts = _extract_alerts(state)

    # Sort newest-first by ISO timestamp when available; stable otherwise.
    def _ts(alert: dict) -> str:
        ts = alert.get("timestamp")
        return ts if isinstance(ts, str) else ""

    alerts.sort(key=_ts, reverse=True)
    return alerts[:_MAX_RECENT_ALERTS]


def create_app(
    config_path: Optional[str] = None,
    mock: bool = False,
    warm_on_startup: bool = True,
) -> FastAPI:
    """Build the dashboard FastAPI application.

    Args:
        config_path: Optional path to a YAML config file. When omitted an empty
            default configuration is used (still fully functional in mock mode).
        mock: Force the deterministic mock client. The mock client is also used
            automatically when no API key is configured.
        warm_on_startup: Populate the status cache on a background thread at
            startup so the first visitor never blocks on the rate-limited cold
            load. Tests may disable for determinism.

    Returns:
        A configured :class:`fastapi.FastAPI` instance.
    """
    # Imported lazily so the rest of the package (and tests that monkeypatch the
    # client factory) need not have a fully wired api module at import time.
    from ..api import make_client

    config = _load_config(config_path)
    client = make_client(
        config.api_key, mock, rate_limit_file=config.rate_limit_path()
    )
    scanner = SubnetScanner(client)
    tracker = PortfolioTracker(client)
    coldkey = _first_watched_coldkey(config)
    watchlist = _watchlist(config)
    pinned = set(watchlist)

    cache = _TTLCache(DEFAULT_CACHE_TTL_SECONDS)
    # Pool list behind its own 5-min TTL cache (a SEPARATE instance from
    # ``cache`` so the scan loader -- which runs while holding ``cache``'s lock
    # -- can reuse the pool fetch without re-entering the same non-reentrant
    # lock). Shared by the dashboard scan and the per-netuid detail loader so a
    # single fetch serves both.
    pools_cache = _TTLCache(DEFAULT_CACHE_TTL_SECONDS)
    # Sparkline series live behind a long (6h) TTL because each one costs a
    # live history call; keys are "tao" and "pool:<netuid>".
    spark_cache = _TTLCache(SPARK_HISTORY_TTL_SECONDS)
    # Per-netuid authoritative detail behind a 1h TTL, LRU-capped at 16.
    detail_cache = _LRUTTLCache(DETAIL_TTL_SECONDS, DETAIL_CACHE_MAX)
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    def _warm_cache() -> None:
        """Populate the status cache once so the first visitor never blocks.

        Runs on a daemon thread at startup: the cold load is paced by the
        5/min rate limiter (worst case minutes on live), and serving it
        on-request 504ed behind the proxy timeout. After this completes,
        stale-while-revalidate keeps every subsequent request instant.
        """
        try:
            _build_status()
            logger.info("Startup cache warm complete.")
        except Exception as exc:  # noqa: BLE001 - warm is best-effort
            logger.warning("Startup cache warm failed: %s", exc)

    @asynccontextmanager
    async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
        """Warm the cache in the background; close the client on shutdown."""
        if warm_on_startup:
            threading.Thread(
                target=_warm_cache, daemon=True, name="startup-cache-warm"
            ).start()
        try:
            yield
        finally:
            client.close()

    app = FastAPI(title="tao-sentinel", version="0.2.0", lifespan=_lifespan)

    def _load_scan_and_portfolio() -> tuple[list[HealthReport], Optional[Portfolio]]:
        """Fetch the dashboard's expensive data, sharing one pool fetch.

        Pools are fetched exactly ONCE here and handed to both ``scanner.scan``
        and ``tracker.get_portfolio`` (via their ``pools=`` parameter), so a
        single cache refill no longer fetches the pool list twice (finding 19).
        The result is cached as a whole, so warm renders cost zero API calls.

        Returns:
            A ``(reports, portfolio)`` tuple; ``portfolio`` is ``None`` when no
            coldkey is configured.
        """
        pools = _pools()
        reports = scanner.scan(pools=pools)
        portfolio = tracker.get_portfolio(coldkey, pools=pools) if coldkey else None
        return reports, portfolio

    def _pools() -> list:
        """Return the 5-min-cached pool list, shared by the dashboard scan and
        the per-netuid detail loader so a single fetch serves both.

        The detail page validates an unknown netuid against the (cheap, cached)
        scan set before ever reaching here, and when it does reach here it reuses
        the same fetch the dashboard already paid for -- so the documented
        per-view call budget holds and unknown netuids cost zero live calls.
        """
        return pools_cache.get("pools", client.get_pools)

    def _scan_and_portfolio() -> tuple[list[HealthReport], Optional[Portfolio]]:
        """Return the cached ``(reports, portfolio)`` pair, refilling if stale."""
        return cache.get("status", _load_scan_and_portfolio)

    def _pool_spark(netuid: int) -> Optional[list[float]]:
        """Return the 6h-cached alpha-price sparkline for one subnet, or ``None``.

        Each distinct netuid's history is fetched at most once per
        :data:`SPARK_HISTORY_TTL_SECONDS` window. A missing client method or a
        :class:`TaostatsError` (history endpoint unavailable) degrades to
        ``None`` so the row simply renders without a sparkline rather than
        failing the whole payload.
        """
        def _load() -> Optional[list[float]]:
            getter = getattr(client, "get_pool_history", None)
            if getter is None:  # pragma: no cover - api owner provides this
                return None
            try:
                series = getter(netuid)
            except TaostatsError:
                logger.warning(
                    "Pool history unavailable for netuid %s; no sparkline.", netuid
                )
                return None
            return _downsample(series)

        return spark_cache.get(f"pool:{netuid}", _load)

    def _tao_price_spark() -> Optional[list[float]]:
        """Return the 6h-cached TAO/USD price sparkline, or ``None``."""
        def _load() -> Optional[list[float]]:
            getter = getattr(client, "get_tao_price_history", None)
            if getter is None:  # pragma: no cover - api owner provides this
                return None
            try:
                series = getter()
            except TaostatsError:
                logger.warning("TAO price history unavailable; no sparkline.")
                return None
            return _downsample(series)

        return spark_cache.get("tao", _load)

    def _build_status() -> dict[str, Any]:
        """Assemble the full dashboard payload as plain JSON-able dicts.

        The scan + portfolio data is computed together under one cache entry so
        the pool list is fetched once per refill (finding 19). Watchlist rows
        gain a 6h-cached sparkline (``spark``) and a ``pinned`` flag; portfolio
        positions gain ``share_pct`` and ``name``; ``meta`` gains
        ``generated_at``, ``tao_price_usd`` and ``tao_price_spark``. The whole
        block is computed defensively so a transient client error does not blank
        the page.

        Returns:
            A dict with ``subnets``, ``portfolio``, ``alerts`` and ``meta`` keys.
        """
        subnets: list[dict] = []
        portfolio: Optional[dict] = None
        tao_price_usd: Optional[float] = None
        try:
            reports, valued = _scan_and_portfolio()
            name_by_netuid = {r.netuid: r.name for r in reports}
            subnets = []
            for report in reports:
                row = report.model_dump()
                is_pinned = report.netuid in pinned
                row["pinned"] = is_pinned
                # Sparklines are fetched ONLY for pinned (watchlist) subnets so
                # the dashboard's history-call budget is bounded by len(watchlist).
                row["spark"] = _pool_spark(report.netuid) if is_pinned else None
                subnets.append(row)

            if valued is not None:
                portfolio = valued.model_dump()
                tao_price_usd = valued.tao_price_usd
                total = valued.total_value_tao
                positions = portfolio.get("positions") or []
                for pos in positions:
                    value = pos.get("value_tao")
                    pos["share_pct"] = (
                        (value / total * 100.0)
                        if value is not None and total
                        else None
                    )
                    pos["name"] = name_by_netuid.get(pos.get("netuid"))
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Dashboard data fetch failed: %s", exc)

        alerts = _load_recent_alerts(config.state_path)

        # Without a configured portfolio coldkey there is no TaoPrice fetch,
        # so derive the headline TAO/USD figure from the (6h-cached) history
        # series instead -- zero extra API calls, and the header price no
        # longer depends on portfolio configuration (review finding).
        tao_spark = _tao_price_spark()
        if tao_price_usd is None and tao_spark:
            tao_price_usd = tao_spark[-1]

        return {
            "subnets": subnets,
            "portfolio": portfolio,
            "alerts": alerts,
            "meta": {
                "mock": mock or not config.api_key,
                "coldkey": coldkey,
                "watchlist": watchlist,
                "refresh_seconds": DASHBOARD_REFRESH_SECONDS,
                "n_subnets": len(subnets),
                "n_alerts": len(alerts),
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "tao_price_usd": tao_price_usd,
                "tao_price_spark": tao_spark,
                # True when the table shows concentration-blind all-subnets
                # scores; the template surfaces this as a visible caveat so
                # the disclosure is not buried in JSON metrics.
                "provisional": any(
                    s.get("metrics", {}).get("provisional") for s in subnets
                ),
            },
        }

    def _load_subnet_detail(netuid: int) -> Optional[dict[str, Any]]:
        """Compute the authoritative single-netuid detail payload, or ``None``.

        This is the cold-path loader behind :data:`DETAIL_TTL_SECONDS` /
        :data:`DETAIL_CACHE_MAX`. It fetches the pool list ONCE and shares it
        with the authoritative (concentration-inclusive) scan; an unknown netuid
        (absent from both the scan and the pool list) returns ``None`` so the
        route can answer 404.

        The returned dict carries: ``netuid``, ``name``, the full authoritative
        ``report`` (score/grade/metrics/warnings), a ``pool`` detail block
        (price, market cap, tao_in/alpha_in reserves), a 24h ``spark`` series
        with ``spark_change_pct``, and ``validators`` (top 10 by stake with a
        ``share_pct`` of total active stake).

        Budget: a single cold load fetches the validator list ONCE -- the same
        fetch feeds both the scanner's concentration scoring and the top-10
        table -- via a one-shot memoizing scanner proxy, so the documented
        "1 call per uncached view" holds (pools once + validators once + history
        once) rather than fetching validators twice.
        """
        pools = _pools()
        pool = next((p for p in pools if p.netuid == netuid), None)

        # Score through a memoizing proxy so the scanner's validator fetch and
        # the top-10 table below share ONE call (not two) per cold load.
        memo = _ValidatorMemoClient(client)
        reports = SubnetScanner(memo).scan(netuid=netuid, pools=pools)
        if not reports and pool is None:
            return None
        report = reports[0] if reports else None

        # Top validators by stake, with share of total ACTIVE stake. Reuses the
        # memoized fetch above (no second API call).
        validators_out: list[dict] = []
        validators_list = memo.get_validators(netuid)
        active = [
            v for v in validators_list if v.active is not False and v.stake_tao > 0
        ]
        total_stake = sum(v.stake_tao for v in active)
        for v in sorted(active, key=lambda x: x.stake_tao, reverse=True)[
            :_DETAIL_TOP_VALIDATORS
        ]:
            validators_out.append(
                {
                    "hotkey": v.hotkey,
                    "stake_tao": v.stake_tao,
                    "vtrust": v.vtrust,
                    "share_pct": (
                        v.stake_tao / total_stake * 100.0 if total_stake else None
                    ),
                }
            )

        spark = _pool_spark(netuid)
        spark_change_pct = _pct_change(spark) if spark else None

        pool_detail: Optional[dict] = None
        if pool is not None:
            pool_detail = {
                "netuid": pool.netuid,
                "name": pool.name,
                "price_tao": pool.price_tao,
                "market_cap_tao": pool.market_cap_tao,
                "tao_in": pool.tao_in,
                "alpha_in": pool.alpha_in,
            }

        name = (report.name if report else None) or (pool.name if pool else None)
        return {
            "netuid": netuid,
            "name": name,
            "report": report.model_dump() if report else None,
            "pool": pool_detail,
            "spark": spark,
            "spark_change_pct": spark_change_pct,
            "validators": validators_out,
        }

    def _known_netuids() -> set[int]:
        """Return the set of netuids the dashboard already knows about.

        Sourced from the 5-min-cached ``_scan_and_portfolio`` reports (the same
        data the dashboard renders), so this costs ZERO additional live calls on
        a warm cache. Used to reject unknown/out-of-range netuids BEFORE the
        expensive per-detail fetch, so an unauthenticated flood of distinct
        bogus netuids cannot churn the LRU and burn the monthly API quota.
        """
        try:
            reports, _ = _scan_and_portfolio()
        except Exception:  # pragma: no cover - defensive
            return set()
        return {r.netuid for r in reports}

    def _subnet_detail(netuid: int) -> Optional[dict[str, Any]]:
        """Return the 1h-cached, LRU-bounded detail payload for ``netuid``.

        Unknown netuids are rejected against the cheap, 5-min-cached set of
        known netuids BEFORE any per-detail fetch, so a flood of distinct
        out-of-range netuids costs zero live calls and cannot evict the LRU.
        """
        if netuid not in _known_netuids():
            return None
        return detail_cache.get(f"detail:{netuid}", lambda: _load_subnet_detail(netuid))

    # ---- HTML surface -----------------------------------------------------
    # When the React SPA build is present (tao_sentinel/web/static, emitted by
    # `npm run build` in frontend/ and shipped in the wheel/Docker image), it
    # IS the dashboard: / and /subnet/{netuid} serve the SPA shell and the
    # client renders from the JSON API. Without the build (source checkout
    # with no node, tests), the legacy server-rendered Jinja templates serve
    # the same data so the package still works standalone.
    _spa_index = _STATIC_DIR / "index.html"
    _serve_spa = _spa_index.is_file()
    if _serve_spa:
        app.mount(
            "/assets",
            StaticFiles(directory=str(_STATIC_DIR / "assets")),
            name="assets",
        )

        @app.get("/", response_class=HTMLResponse, include_in_schema=False)
        def spa_root() -> FileResponse:
            """Serve the SPA shell; data loads client-side from /api/status."""
            return FileResponse(_spa_index)

        @app.get("/favicon.svg", include_in_schema=False)
        def spa_favicon() -> FileResponse:
            return FileResponse(_STATIC_DIR / "favicon.svg")

    else:

        @app.get("/", response_class=HTMLResponse)
        def dashboard(request: Request) -> HTMLResponse:
            """Render the legacy server-side HTML dashboard."""
            status = _build_status()
            return templates.TemplateResponse(
                request,
                "dashboard.html",
                {
                    "subnets": status["subnets"],
                    "portfolio": status["portfolio"],
                    "alerts": status["alerts"],
                    "meta": status["meta"],
                    "refresh_seconds": DASHBOARD_REFRESH_SECONDS,
                },
            )

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        """Liveness probe: cheap and never touches the Taostats client.

        Container healthchecks must hit THIS route, not ``/api/status`` --
        the status payload blocks on the rate limiter during a cold cache
        (worst case minutes under live 429s), which made the dashboard
        container flap unhealthy on fresh deploys.
        """
        return JSONResponse({"ok": True})

    @app.get("/api/status")
    def status_json() -> JSONResponse:
        """Return the dashboard data as JSON."""
        return JSONResponse(_build_status())

    if _serve_spa:

        @app.get(
            "/subnet/{netuid}",
            response_class=HTMLResponse,
            include_in_schema=False,
        )
        def spa_subnet(netuid: int) -> FileResponse:
            """Serve the SPA shell for client-side routed detail pages.

            The SPA fetches ``/api/subnet/{netuid}`` itself and renders its own
            not-found state, so even an unknown netuid gets the shell (HTTP 200
            with a client-rendered 404 view — standard SPA routing semantics;
            the JSON API remains the authoritative 404).
            """
            return FileResponse(_spa_index)

    else:

        @app.get("/subnet/{netuid}", response_class=HTMLResponse)
        def subnet_detail_html(request: Request, netuid: int) -> HTMLResponse:
            """Render the legacy single-subnet detail page (404 if unknown).

            The detail JSON payload is flattened into the ``subnet`` context
            object the ``subnet.html`` template expects: score/grade/
            provisional/warnings hoisted out of the nested ``report``,
            alongside ``pool``, ``spark``, ``spark_change_pct`` and
            ``validators``. An unknown netuid renders a small standalone 404
            page (the template has no not-found branch).
            """
            detail = _subnet_detail(netuid)
            if detail is None:
                return HTMLResponse(_not_found_html(netuid), status_code=404)
            return templates.TemplateResponse(
                request,
                "subnet.html",
                {"subnet": _detail_template_context(detail), "meta": _detail_meta()},
            )

    @app.get("/api/subnet/{netuid}")
    def subnet_detail_json(netuid: int) -> JSONResponse:
        """Return the authoritative single-subnet detail as JSON (404 if unknown)."""
        detail = _subnet_detail(netuid)
        if detail is None:
            return JSONResponse(
                {"error": "subnet not found", "netuid": netuid}, status_code=404
            )
        return JSONResponse(detail)

    def _detail_meta() -> dict[str, Any]:
        """Return the small meta block shared by the detail page/JSON."""
        return {
            "mock": mock or not config.api_key,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    return app


def _detail_template_context(detail: dict[str, Any]) -> dict[str, Any]:
    """Flatten a detail payload into the ``subnet.html`` template's view object.

    The JSON API shape nests scoring under ``report``; the template wants
    ``score``/``grade``/``provisional``/``warnings`` at the top level next to
    ``pool``/``spark``/``spark_change_pct``/``validators``. Missing pieces (e.g.
    a pool-only netuid with no scan report) default sensibly so the template
    never dereferences ``None``.

    Args:
        detail: A non-``None`` detail payload from ``_subnet_detail``.

    Returns:
        A flat context dict for the template.
    """
    report = detail.get("report") or {}
    metrics = report.get("metrics") or {}
    return {
        "netuid": detail["netuid"],
        "name": detail.get("name"),
        "score": report.get("score", 0.0),
        "grade": report.get("grade", "F"),
        "provisional": bool(metrics.get("provisional")),
        "warnings": report.get("warnings") or [],
        "pool": detail.get("pool"),
        "spark": detail.get("spark"),
        "spark_change_pct": detail.get("spark_change_pct"),
        "validators": detail.get("validators") or [],
    }


def _not_found_html(netuid: int) -> str:
    """Return a minimal dark-theme 404 page for an unknown netuid.

    Rendered standalone (not via ``subnet.html``) because that template assumes
    a present ``subnet`` object and has no not-found branch.
    """
    return (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\"/>"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"/>"
        f"<title>tao-sentinel &middot; subnet {netuid} not found</title>"
        "<style>html,body{margin:0;background:#0d1117;color:#e6edf3;"
        "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,"
        "Helvetica,Arial,sans-serif;}.wrap{max-width:640px;margin:0 auto;"
        "padding:64px 20px;text-align:center;}a{color:#58a6ff;"
        "text-decoration:none;}h1{font-size:22px;}</style></head><body>"
        "<div class=\"wrap\"><h1>Subnet not found</h1>"
        f"<p>No subnet with netuid <code>{netuid}</code> was found in the "
        "current pool or subnet list.</p>"
        "<p><a href=\"/\">&larr; back to dashboard</a></p></div></body></html>"
    )
