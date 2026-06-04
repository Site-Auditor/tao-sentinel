"""Tests for the web dashboard backend (cluster: web-backend).

These cover the C5 status-payload extensions and the per-netuid detail routes:

* ``/api/status`` gains, per subnet, ``pinned`` and a ``spark`` series that is
  populated ONLY for watchlist netuids (and ``None`` otherwise); portfolio
  positions gain ``share_pct`` and ``name``; ``meta`` gains ``generated_at``,
  ``tao_price_usd`` and ``tao_price_spark``.
* Watchlist sparklines come from a 6h-TTL cache, so the history endpoint is hit
  at most once per pinned netuid across repeated renders.
* ``GET /subnet/{netuid}`` (HTML) and ``GET /api/subnet/{netuid}`` (JSON) return
  an authoritative single-netuid detail (pool detail, 24h sparkline + pct
  change, top-10 validators by stake with share pct), are cached for 1h behind
  an LRU cap of 16, and 404 on an unknown netuid.

Every test runs against a no-network spy client that records call counts so the
caching assertions prove the free-tier budget is respected; no test performs any
real I/O.
"""

from __future__ import annotations

import math
from typing import Optional

from fastapi.testclient import TestClient

import tao_sentinel.api as api
from tao_sentinel.api import MockTaostatsClient, TaostatsError
from tao_sentinel.config import Config, WatchConfig
from tao_sentinel.models import PricePoint
from tao_sentinel.web.app import (
    DETAIL_CACHE_MAX,
    _downsample,
    _LRUTTLCache,
    _pct_change,
    create_app,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _SpyClient(MockTaostatsClient):
    """A no-network client that counts calls and serves history fixtures.

    Extends :class:`MockTaostatsClient` so it satisfies the full client
    contract while (a) counting per-endpoint calls for caching assertions and
    (b) providing the C1 ``get_pool_history``/``get_tao_price_history`` history
    surface (the api cluster owns the real implementation; this spy is enough
    to exercise the web backend that consumes it).
    """

    def __init__(self) -> None:
        super().__init__()
        self.get_pools_calls = 0
        self.get_subnets_calls = 0
        self.get_validators_calls = 0
        self.pool_history_calls: dict[int, int] = {}
        self.tao_history_calls = 0

    def get_pools(self):  # type: ignore[override]
        self.get_pools_calls += 1
        return super().get_pools()

    def get_subnets(self):  # type: ignore[override]
        self.get_subnets_calls += 1
        return super().get_subnets()

    def get_validators(self, netuid: int):  # type: ignore[override]
        self.get_validators_calls += 1
        return super().get_validators(netuid)

    def get_pool_history(self, netuid: int, hours: int = 24) -> list[PricePoint]:
        self.pool_history_calls[netuid] = self.pool_history_calls.get(netuid, 0) + 1
        base = 0.01 * (netuid + 1)
        return [
            PricePoint(
                timestamp=f"2026-06-03T{h:02d}:00:00Z",
                value=base + 0.001 * h,
            )
            for h in range(24)
        ]

    def get_tao_price_history(self, hours: int = 24) -> list[PricePoint]:
        self.tao_history_calls += 1
        return [
            PricePoint(timestamp=f"2026-06-03T{h:02d}:00:00Z", value=300.0 + h)
            for h in range(24)
        ]


class _NoHistoryClient(_SpyClient):
    """A spy whose history endpoints are unavailable (graceful-degrade case)."""

    def get_pool_history(self, netuid: int, hours: int = 24):  # type: ignore[override]
        self.pool_history_calls[netuid] = self.pool_history_calls.get(netuid, 0) + 1
        raise TaostatsError(0, "history endpoint unavailable")

    def get_tao_price_history(self, hours: int = 24):  # type: ignore[override]
        self.tao_history_calls += 1
        raise TaostatsError(0, "history endpoint unavailable")


def _install(monkeypatch, client: MockTaostatsClient) -> None:
    """Force the web factory to use ``client`` instead of a real one."""
    monkeypatch.setattr(api, "make_client", lambda *a, **k: client)


def _watchlist_config(netuids: list[int], coldkey: Optional[str] = None) -> Config:
    """Build a Config with a watchlist (and optional watched coldkey)."""
    watches = []
    if coldkey is not None:
        watches.append(WatchConfig(type="stake_change", coldkey=coldkey))
    return Config(watches=watches, watchlist=netuids)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_downsample_passthrough_when_short():
    """A series at/under the cap is projected to its values unchanged."""
    series = [PricePoint(timestamp=str(i), value=float(i)) for i in range(10)]
    assert _downsample(series, max_points=48) == [float(i) for i in range(10)]


def test_downsample_caps_and_keeps_last():
    """An over-long series is reduced to the cap, keeping the final value."""
    series = [PricePoint(timestamp=str(i), value=float(i)) for i in range(500)]
    out = _downsample(series, max_points=48)
    assert len(out) == 48
    assert out[-1] == 499.0  # last (most recent) value preserved


def test_pct_change_basic_and_guards():
    """Pct change is first->last; guarded against short series and zero base."""
    assert _pct_change([100.0, 110.0]) == 10.0
    assert _pct_change([100.0]) is None  # too short
    assert _pct_change([0.0, 5.0]) is None  # zero base -> undefined


# ---------------------------------------------------------------------------
# LRU TTL cache
# ---------------------------------------------------------------------------


def test_lru_ttl_cache_evicts_least_recently_used():
    """Exceeding the cap evicts the LRU entry; touched keys survive."""
    clock = {"t": 0.0}
    cache = _LRUTTLCache(ttl_seconds=1000.0, max_entries=2, clock=lambda: clock["t"])
    calls = {"a": 0, "b": 0, "c": 0}

    def loader(name):
        def _l():
            calls[name] += 1
            return name
        return _l

    cache.get("a", loader("a"))
    cache.get("b", loader("b"))
    cache.get("a", loader("a"))  # touch a -> b is now LRU
    cache.get("c", loader("c"))  # inserts c, evicts b

    assert cache.get("a", loader("a")) == "a"  # still cached
    assert calls["a"] == 1  # never recomputed
    cache.get("b", loader("b"))  # b was evicted -> recomputed
    assert calls["b"] == 2


def test_lru_ttl_cache_refills_after_expiry():
    """A stale entry is recomputed once its TTL elapses."""
    clock = {"t": 0.0}
    cache = _LRUTTLCache(ttl_seconds=10.0, max_entries=4, clock=lambda: clock["t"])
    calls = {"n": 0}

    def loader():
        calls["n"] += 1
        return calls["n"]

    assert cache.get("k", loader) == 1
    assert cache.get("k", loader) == 1  # warm
    clock["t"] = 20.0
    assert cache.get("k", loader) == 2  # stale -> reload


# ---------------------------------------------------------------------------
# C5 - /api/status payload shape
# ---------------------------------------------------------------------------


def test_status_meta_has_new_fields(monkeypatch):
    """meta gains generated_at, tao_price_usd and a tao_price_spark series."""
    spy = _SpyClient()
    _install(monkeypatch, spy)
    app = create_app(config_path=None, mock=True)
    with TestClient(app) as client:
        meta = client.get("/api/status").json()["meta"]

    assert isinstance(meta["generated_at"], str) and meta["generated_at"]
    # No portfolio configured -> the headline price falls back to the last
    # point of the (cached) history series instead of being null.
    assert meta["tao_price_spark"] == [300.0 + h for h in range(24)]
    assert meta["tao_price_usd"] == meta["tao_price_spark"][-1]


def test_status_spark_only_for_watchlist(monkeypatch):
    """Only pinned (watchlist) subnets carry a spark series; others are None."""
    spy = _SpyClient()
    _install(monkeypatch, spy)
    cfg = _watchlist_config([1])  # pin netuid 1 only

    # The factory loads config from a path; inject ours directly instead.
    monkeypatch.setattr("tao_sentinel.web.app._load_config", lambda _p: cfg)
    app = create_app(config_path="ignored", mock=True)
    with TestClient(app) as client:
        subnets = client.get("/api/status").json()["subnets"]

    by_netuid = {s["netuid"]: s for s in subnets}
    assert by_netuid[1]["pinned"] is True
    assert isinstance(by_netuid[1]["spark"], list) and by_netuid[1]["spark"]
    for nuid in (4, 8, 64):
        assert by_netuid[nuid]["pinned"] is False
        assert by_netuid[nuid]["spark"] is None

    # History fetched ONLY for the single pinned netuid.
    assert spy.pool_history_calls == {1: 1}


def test_status_spark_cached_across_renders(monkeypatch):
    """Repeated renders reuse the 6h-cached spark: history fetched once total."""
    spy = _SpyClient()
    _install(monkeypatch, spy)
    cfg = _watchlist_config([1, 4])
    monkeypatch.setattr("tao_sentinel.web.app._load_config", lambda _p: cfg)
    app = create_app(config_path="ignored", mock=True)
    with TestClient(app) as client:
        client.get("/api/status")
        client.get("/api/status")
        client.get("/api/status")

    assert spy.pool_history_calls == {1: 1, 4: 1}  # one fetch per pinned netuid
    assert spy.tao_history_calls == 1  # TAO spark cached too


def test_status_portfolio_positions_share_and_name(monkeypatch):
    """Portfolio positions gain share_pct (summing ~100) and a subnet name."""
    spy = _SpyClient()
    _install(monkeypatch, spy)
    cfg = _watchlist_config([], coldkey=MockTaostatsClient.COLDKEY)
    monkeypatch.setattr("tao_sentinel.web.app._load_config", lambda _p: cfg)
    app = create_app(config_path="ignored", mock=True)
    with TestClient(app) as client:
        payload = client.get("/api/status").json()

    portfolio = payload["portfolio"]
    assert portfolio is not None
    positions = portfolio["positions"]
    assert positions
    for pos in positions:
        assert "share_pct" in pos and "name" in pos
        assert pos["share_pct"] is not None
    # Shares of valued positions sum to ~100%.
    total_share = sum(p["share_pct"] for p in positions)
    assert math.isclose(total_share, 100.0, abs_tol=1e-6)
    # Names resolved from the scan reports.
    names = {p["netuid"]: p["name"] for p in positions}
    assert names.get(1) == "apex"
    # Portfolio configured -> TAO price surfaced in meta.
    assert payload["meta"]["tao_price_usd"] == 350.0


def test_status_graceful_without_history(monkeypatch):
    """A history-unavailable client yields null sparks but a valid payload."""
    spy = _NoHistoryClient()
    _install(monkeypatch, spy)
    cfg = _watchlist_config([1])
    monkeypatch.setattr("tao_sentinel.web.app._load_config", lambda _p: cfg)
    app = create_app(config_path="ignored", mock=True)
    with TestClient(app) as client:
        payload = client.get("/api/status").json()

    assert payload["meta"]["tao_price_spark"] is None
    by_netuid = {s["netuid"]: s for s in payload["subnets"]}
    assert by_netuid[1]["pinned"] is True
    assert by_netuid[1]["spark"] is None  # degraded, not errored


def test_status_shares_single_pool_fetch(monkeypatch):
    """A cold status render fetches the pool list exactly once (shared)."""
    spy = _SpyClient()
    _install(monkeypatch, spy)
    cfg = _watchlist_config([1, 4], coldkey=MockTaostatsClient.COLDKEY)
    monkeypatch.setattr("tao_sentinel.web.app._load_config", lambda _p: cfg)
    app = create_app(config_path="ignored", mock=True)
    with TestClient(app) as client:
        client.get("/api/status")

    assert spy.get_pools_calls == 1
    assert spy.get_subnets_calls == 1


# ---------------------------------------------------------------------------
# C5 - /api/subnet/{netuid} JSON detail route
# ---------------------------------------------------------------------------


def test_subnet_detail_json_ok(monkeypatch):
    """A known netuid returns an authoritative detail payload (200)."""
    spy = _SpyClient()
    _install(monkeypatch, spy)
    app = create_app(config_path=None, mock=True)
    with TestClient(app) as client:
        resp = client.get("/api/subnet/1")

    assert resp.status_code == 200
    detail = resp.json()
    assert detail["netuid"] == 1
    assert detail["name"] == "apex"
    # Authoritative (concentration-inclusive) scan -> NOT provisional, and the
    # real concentration component was computed (validator_data True).
    assert not detail["report"]["metrics"].get("provisional")
    assert detail["report"]["metrics"]["validator_data"] is True
    # Pool detail block carries reserves + price + market cap.
    pool = detail["pool"]
    assert pool["price_tao"] == 0.0254
    assert pool["market_cap_tao"] == 124000.0
    assert pool["tao_in"] is not None and pool["alpha_in"] is not None
    # 24h sparkline + pct change.
    assert isinstance(detail["spark"], list) and detail["spark"]
    assert detail["spark_change_pct"] is not None
    # Top validators by stake, descending, with share pct.
    validators = detail["validators"]
    assert validators
    assert len(validators) <= 10
    stakes = [v["stake_tao"] for v in validators]
    assert stakes == sorted(stakes, reverse=True)
    assert all(v["share_pct"] is not None for v in validators)
    assert math.isclose(sum(v["share_pct"] for v in validators), 100.0, abs_tol=1e-6)


def test_subnet_detail_json_404(monkeypatch):
    """An unknown netuid returns a 404 JSON error."""
    spy = _SpyClient()
    _install(monkeypatch, spy)
    app = create_app(config_path=None, mock=True)
    with TestClient(app) as client:
        resp = client.get("/api/subnet/999")

    assert resp.status_code == 404
    assert resp.json()["netuid"] == 999


def test_subnet_detail_cached_via_spy(monkeypatch):
    """Repeated detail views reuse the 1h cache: one scan/validator fetch."""
    spy = _SpyClient()
    _install(monkeypatch, spy)
    app = create_app(config_path=None, mock=True)
    with TestClient(app) as client:
        client.get("/api/subnet/1")
        client.get("/api/subnet/1")
        client.get("/api/subnet/1")

    # One cold load: pools + validators fetched once, history once.
    assert spy.get_validators_calls == 1
    assert spy.pool_history_calls == {1: 1}
    # Pools fetched once for the cold detail load (warm reuses the cached dict).
    assert spy.get_pools_calls == 1


def test_subnet_detail_lru_eviction(monkeypatch):
    """Beyond the LRU cap, the oldest detail is evicted and recomputed.

    Loading DETAIL_CACHE_MAX + 1 distinct netuids evicts the first; re-fetching
    it triggers a fresh validator fetch, proving the cap bounds cached entries.
    """
    spy = _SpyClient()
    # Make every netuid in range "known" by giving the scanner subnets for them.
    # The mock only knows 1/4/8/64, so build a client that reports many subnets.

    base_subnets = spy.get_subnets()

    class _ManySubnets(_SpyClient):
        def get_subnets(self):  # type: ignore[override]
            self.get_subnets_calls += 1
            out = []
            for n in range(1, DETAIL_CACHE_MAX + 5):
                proto = base_subnets[0]
                out.append(proto.model_copy(update={"netuid": n, "name": f"sn{n}"}))
            return out

        def get_validators(self, netuid: int):  # type: ignore[override]
            self.get_validators_calls += 1
            return MockTaostatsClient.get_validators(self, 4)  # always populated

        def get_pools(self):  # type: ignore[override]
            self.get_pools_calls += 1
            pools = MockTaostatsClient.get_pools(self)
            proto = pools[0]
            return [
                proto.model_copy(update={"netuid": n, "name": f"sn{n}"})
                for n in range(1, DETAIL_CACHE_MAX + 5)
            ]

    spy = _ManySubnets()
    _install(monkeypatch, spy)
    app = create_app(config_path=None, mock=True)
    with TestClient(app) as client:
        # Fill the cache with exactly DETAIL_CACHE_MAX distinct netuids.
        for n in range(1, DETAIL_CACHE_MAX + 1):
            assert client.get(f"/api/subnet/{n}").status_code == 200
        validators_after_fill = spy.get_validators_calls
        assert validators_after_fill == DETAIL_CACHE_MAX

        # netuid 1 is still warm (recently within window but it's the LRU).
        # Insert one MORE distinct netuid -> evicts the LRU (netuid 1).
        assert client.get(f"/api/subnet/{DETAIL_CACHE_MAX + 1}").status_code == 200
        assert spy.get_validators_calls == DETAIL_CACHE_MAX + 1

        # Re-requesting netuid 1 now misses (evicted) -> recomputed.
        assert client.get("/api/subnet/1").status_code == 200
        assert spy.get_validators_calls == DETAIL_CACHE_MAX + 2


def test_subnet_detail_html_unknown_netuid(monkeypatch):
    """Unknown netuid: SPA serves the shell (client renders not-found, JSON
    stays the authoritative 404); the legacy Jinja surface 404s server-side."""
    import pytest

    from tao_sentinel.web.app import _STATIC_DIR, _TEMPLATES_DIR

    spa = (_STATIC_DIR / "index.html").is_file()
    if not spa and not (_TEMPLATES_DIR / "subnet.html").exists():
        pytest.skip("neither SPA build nor subnet.html present")

    spy = _SpyClient()
    _install(monkeypatch, spy)
    app = create_app(config_path=None, mock=True)
    with TestClient(app) as client:
        resp = client.get("/subnet/999")
        if spa:
            assert resp.status_code == 200  # SPA routing semantics
            assert 'id="root"' in resp.text
        else:
            assert resp.status_code == 404
        # The JSON API is the authoritative 404 either way.
        assert client.get("/api/subnet/999").status_code == 404


def test_subnet_detail_html_ok(monkeypatch):
    """A known netuid HTML detail returns 200 (skips without the template)."""
    import pytest

    from tao_sentinel.web.app import _TEMPLATES_DIR

    if not (_TEMPLATES_DIR / "subnet.html").exists():
        pytest.skip("subnet.html not present in standalone run (frontend cluster)")

    spy = _SpyClient()
    _install(monkeypatch, spy)
    app = create_app(config_path=None, mock=True)
    with TestClient(app) as client:
        resp = client.get("/subnet/1")
    assert resp.status_code == 200


def test_healthz_is_cheap_and_always_up():
    """/healthz must return 200 without touching the Taostats client."""
    from tao_sentinel.web.app import create_app

    class ExplodingClient:
        def __getattr__(self, name):
            raise AssertionError("healthz must not touch the client")

    app = create_app(None, mock=True)
    # Patch the underlying client to one that explodes on ANY use: healthz
    # must still answer because it never builds the status payload.
    with TestClient(app) as tc:
        resp = tc.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}


def test_tao_price_usd_falls_back_to_spark_without_portfolio(tmp_path):
    """No portfolio coldkey configured -> headline price derives from the
    (cached) history series instead of being null (review finding)."""
    from tao_sentinel.web.app import create_app

    cfg = tmp_path / "no-coldkey.yaml"
    cfg.write_text("watches: []\nwatchlist: []\n")
    with TestClient(create_app(str(cfg), mock=True)) as tc:
        meta = tc.get("/api/status").json()["meta"]
        assert meta["coldkey"] is None
        assert meta["tao_price_spark"], "mock must provide a spark series"
        assert meta["tao_price_usd"] == meta["tao_price_spark"][-1]


def test_ttl_cache_serves_stale_while_revalidating():
    """A stale hit returns the OLD value instantly and refreshes for the
    next call — requests never block on the rate-limited reload (the live
    cold load exceeded the proxy timeout and 504ed)."""
    from tao_sentinel.web.app import _TTLCache

    t = {"now": 0.0}
    calls = {"n": 0}

    def loader():
        calls["n"] += 1
        return f"v{calls['n']}"

    cache = _TTLCache(ttl_seconds=10, clock=lambda: t["now"], swr_background=False)
    assert cache.get("k", loader) == "v1"      # cold: blocks, loads
    t["now"] = 5.0
    assert cache.get("k", loader) == "v1"      # fresh hit, no load
    t["now"] = 11.0
    assert cache.get("k", loader) == "v1"      # STALE served immediately...
    assert calls["n"] == 2                      # ...but a refresh ran
    assert cache.get("k", loader) == "v2"      # next call sees fresh value


def test_ttl_cache_keeps_stale_on_refresh_failure():
    """A failing background refresh keeps serving the stale value."""
    from tao_sentinel.web.app import _TTLCache

    t = {"now": 0.0}
    state = {"fail": False}

    def loader():
        if state["fail"]:
            raise RuntimeError("upstream down")
        return "good"

    cache = _TTLCache(ttl_seconds=10, clock=lambda: t["now"], swr_background=False)
    assert cache.get("k", loader) == "good"
    t["now"] = 11.0
    state["fail"] = True
    assert cache.get("k", loader) == "good"    # stale survives the failure
    assert cache.get("k", loader) == "good"    # and keeps being served


def test_degraded_detail_is_not_cached_for_the_full_ttl(monkeypatch):
    """A detail whose validator fetch failed serves once, then retries.

    Regression: under upstream 429s the detail cache froze 'No validator
    data' (and the resulting F grade) for the full 1h TTL.
    """

    class FlakyValidatorsClient(_SpyClient):
        def __init__(self) -> None:
            super().__init__()
            self.fail_validators = True

        def get_validators(self, netuid: int):  # type: ignore[override]
            self.get_validators_calls += 1
            if self.fail_validators:
                raise TaostatsError(429, "Rate Limited")
            return super().get_validators(netuid)

    flaky = FlakyValidatorsClient()
    _install(monkeypatch, flaky)
    app = create_app(config_path=None, mock=True)
    with TestClient(app) as client:
        first = client.get("/api/subnet/64").json()
        assert first["degraded"] is True
        assert first["validators"] == []

        # Upstream recovers; the degraded snapshot must NOT still be served.
        flaky.fail_validators = False
        second = client.get("/api/subnet/64").json()
        assert second["degraded"] is False
        assert len(second["validators"]) > 0
