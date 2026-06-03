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
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from ..config import Config, load_config
from ..models import HealthReport, Portfolio
from ..portfolio import PortfolioTracker
from ..scanner import SubnetScanner

logger = logging.getLogger(__name__)

#: Default cache time-to-live, in seconds (5 minutes), chosen to respect the
#: free-tier Taostats rate limit of ~5 calls/minute.
DEFAULT_CACHE_TTL_SECONDS = 300

#: Auto-refresh interval baked into the dashboard ``<meta>`` tag, in seconds.
DASHBOARD_REFRESH_SECONDS = 300

#: Directory holding the Jinja2 templates shipped with the package.
_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

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

    def __init__(self, ttl_seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        """Initialise the cache.

        Args:
            ttl_seconds: How long a cached value remains fresh.
            clock: Monotonic clock callable (injectable for testing).
        """
        self._ttl = ttl_seconds
        self._clock = clock
        self._store: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str, loader: Callable[[], Any]) -> Any:
        """Return the cached value for ``key``, recomputing it if stale.

        The entire check-then-load runs under one lock (single-flight) so a
        concurrent burst of requests for the same cold key triggers exactly one
        loader call; the rest wait and reuse the result. See the class docstring
        for the serialise-cold-renders tradeoff.

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
                return cached[1]
            value = loader()
            self._store[key] = (now, value)
            return value

    def clear(self) -> None:
        """Drop all cached entries."""
        with self._lock:
            self._store.clear()


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


def create_app(config_path: Optional[str] = None, mock: bool = False) -> FastAPI:
    """Build the dashboard FastAPI application.

    Args:
        config_path: Optional path to a YAML config file. When omitted an empty
            default configuration is used (still fully functional in mock mode).
        mock: Force the deterministic mock client. The mock client is also used
            automatically when no API key is configured.

    Returns:
        A configured :class:`fastapi.FastAPI` instance.
    """
    # Imported lazily so the rest of the package (and tests that monkeypatch the
    # client factory) need not have a fully wired api module at import time.
    from ..api import make_client

    config = _load_config(config_path)
    client = make_client(config.api_key, mock)
    scanner = SubnetScanner(client)
    tracker = PortfolioTracker(client)
    coldkey = _first_watched_coldkey(config)

    cache = _TTLCache(DEFAULT_CACHE_TTL_SECONDS)
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    @asynccontextmanager
    async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
        """Close the owned Taostats client on app shutdown (finding 12)."""
        try:
            yield
        finally:
            client.close()

    app = FastAPI(title="tao-sentinel", version="0.1.0", lifespan=_lifespan)

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
        pools = client.get_pools()
        reports = scanner.scan(pools=pools)
        portfolio = tracker.get_portfolio(coldkey, pools=pools) if coldkey else None
        return reports, portfolio

    def _scan_and_portfolio() -> tuple[list[HealthReport], Optional[Portfolio]]:
        """Return the cached ``(reports, portfolio)`` pair, refilling if stale."""
        return cache.get("status", _load_scan_and_portfolio)

    def _build_status() -> dict[str, Any]:
        """Assemble the full dashboard payload as plain JSON-able dicts.

        The scan + portfolio data is computed together under one cache entry so
        the pool list is fetched once per refill (finding 19). The whole block
        is computed defensively so a transient client error does not blank the
        page.

        Returns:
            A dict with ``subnets``, ``portfolio``, ``alerts`` and ``meta`` keys.
        """
        subnets: list[dict] = []
        portfolio: Optional[dict] = None
        try:
            reports, valued = _scan_and_portfolio()
            subnets = [r.model_dump() for r in reports]
            portfolio = valued.model_dump() if valued is not None else None
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Dashboard data fetch failed: %s", exc)

        alerts = _load_recent_alerts(config.state_path)

        return {
            "subnets": subnets,
            "portfolio": portfolio,
            "alerts": alerts,
            "meta": {
                "mock": mock or not config.api_key,
                "coldkey": coldkey,
                "refresh_seconds": DASHBOARD_REFRESH_SECONDS,
                "n_subnets": len(subnets),
                "n_alerts": len(alerts),
                # True when the table shows concentration-blind all-subnets
                # scores; the template surfaces this as a visible caveat so
                # the disclosure is not buried in JSON metrics.
                "provisional": any(
                    s.get("metrics", {}).get("provisional") for s in subnets
                ),
            },
        }

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request) -> HTMLResponse:
        """Render the HTML dashboard."""
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

    @app.get("/api/status")
    def status_json() -> JSONResponse:
        """Return the dashboard data as JSON."""
        return JSONResponse(_build_status())

    return app
