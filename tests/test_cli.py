"""Regression tests for the CLI and web dashboard (cluster: cli-web).

These cover the cli-web cluster findings:

* finding 10 - the dashboard TTL cache is single-flight: a concurrent burst of
  cold requests triggers exactly ONE loader run (no API stampede).
* finding 12 - every CLI command closes its owned client (in a ``finally``),
  and the web app closes the client on shutdown.
* finding 19 - a cold dashboard refill fetches the pool list ONCE and shares it
  between the scan and the portfolio; warm renders cost zero API calls.
* finding 22 - a missing/malformed/invalid config produces a concise stderr
  error and a non-zero exit (no raw traceback) for ``watch``/``scan``/
  ``portfolio``.
* finding 23 - ``scan`` of a nonexistent netuid exits 1 in BOTH ``--json`` and
  table modes, and the error text never enters the JSON stdout stream.

Everything runs against the deterministic mock client (no network) or a small
counting stub; no test performs any I/O against Taostats.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from tao_sentinel import cli
from tao_sentinel.api import MockTaostatsClient, TaostatsError

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _SpyClient(MockTaostatsClient):
    """A mock client that records whether/when it was closed and what it served.

    Extends the real :class:`MockTaostatsClient` so it satisfies the full client
    contract (including the no-op ``close``/``__enter__``/``__exit__`` from C3)
    while counting calls for the assertions below.
    """

    def __init__(self) -> None:
        super().__init__()
        self.closed = False
        self.close_count = 0
        self.get_pools_calls = 0
        self.get_subnets_calls = 0

    def close(self) -> None:
        self.closed = True
        self.close_count += 1
        super().close()

    def get_pools(self):  # type: ignore[override]
        self.get_pools_calls += 1
        return super().get_pools()

    def get_subnets(self):  # type: ignore[override]
        self.get_subnets_calls += 1
        return super().get_subnets()


class _RaisingClient(MockTaostatsClient):
    """A mock client whose data calls raise :class:`TaostatsError` (post C1)."""

    def __init__(self) -> None:
        super().__init__()
        self.closed = False

    def close(self) -> None:
        self.closed = True
        super().close()

    def get_subnets(self):  # type: ignore[override]
        raise TaostatsError(0, "boom")

    def get_pools(self):  # type: ignore[override]
        raise TaostatsError(0, "boom")

    def get_stake_balances(self, coldkey: str):  # type: ignore[override]
        raise TaostatsError(0, "boom")


def _install_client(monkeypatch, client: MockTaostatsClient) -> None:
    """Force both the CLI and web factories to return ``client``."""
    monkeypatch.setattr(cli, "make_client", lambda *a, **k: client)
    import tao_sentinel.api as api

    monkeypatch.setattr(api, "make_client", lambda *a, **k: client)


def _write(path, text: str) -> str:
    path.write_text(text, encoding="utf-8")
    return str(path)


# ---------------------------------------------------------------------------
# finding 22 - clean error (no traceback) for bad config across commands
# ---------------------------------------------------------------------------


def test_scan_missing_config_clean_error(monkeypatch):
    """A nonexistent --config makes scan exit 1 with a concise stderr error."""
    _install_client(monkeypatch, MockTaostatsClient())
    result = runner.invoke(
        cli.app, ["scan", "1", "--mock", "-c", "/tmp/does_not_exist_xyz.yaml"]
    )
    assert result.exit_code == 1
    # No raw traceback leaked anywhere.
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr
    assert "Error:" in result.stderr


def test_watch_missing_config_clean_error(monkeypatch):
    _install_client(monkeypatch, MockTaostatsClient())
    result = runner.invoke(
        cli.app, ["watch", "--once", "--mock", "-c", "/tmp/nope_watch.yaml"]
    )
    assert result.exit_code == 1
    assert "Traceback" not in result.stderr
    assert "Error:" in result.stderr


def test_portfolio_missing_config_clean_error(monkeypatch):
    _install_client(monkeypatch, MockTaostatsClient())
    result = runner.invoke(
        cli.app,
        ["portfolio", "5xyz", "--mock", "-c", "/tmp/nope_portfolio.yaml"],
    )
    assert result.exit_code == 1
    assert "Traceback" not in result.stderr
    assert "Error:" in result.stderr


def test_scan_malformed_yaml_clean_error(monkeypatch, tmp_path):
    """Malformed YAML is reported as a clean error, not a PyYAML stack dump."""
    _install_client(monkeypatch, MockTaostatsClient())
    cfg = _write(tmp_path / "bad.yaml", "this: [unclosed\n bad: : :\n")
    result = runner.invoke(cli.app, ["scan", "1", "--mock", "-c", cfg])
    assert result.exit_code == 1
    assert "Traceback" not in result.stderr
    assert "Error:" in result.stderr


def test_scan_non_mapping_yaml_clean_error(monkeypatch, tmp_path):
    """A config that is valid YAML but not a mapping is a clean error (case 3).

    A top-level YAML *list* (or scalar) parses fine but ``load_config`` rejects
    it with a plain ``ValueError`` (config.py: "must contain a YAML mapping").
    That ``ValueError`` is NOT a ``pydantic.ValidationError`` (the reverse is
    true), so it must be caught explicitly; otherwise it escapes to typer as a
    full Rich traceback. Regression for finding 22 case (3).
    """
    _install_client(monkeypatch, MockTaostatsClient())
    cfg = _write(tmp_path / "toplist.yaml", "- type: price_change\n- type: stake_change\n")
    result = runner.invoke(cli.app, ["scan", "1", "--mock", "-c", cfg])
    assert result.exit_code == 1
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr
    assert "Error:" in result.stderr
    assert "must contain a YAML mapping" in result.stderr


def test_non_mapping_yaml_clean_across_all_commands(monkeypatch, tmp_path):
    """watch/scan/portfolio all reject a non-mapping config cleanly (no traceback).

    Each command wraps ``load_config`` in the same ``_USER_ERRORS`` handler, so
    the not-a-mapping ``ValueError`` must surface identically (concise stderr,
    exit 1) on every config-reading command - the inconsistency finding 22 calls
    out between the CLI and the web surface.
    """
    _install_client(monkeypatch, MockTaostatsClient())
    cfg = _write(tmp_path / "scalar.yaml", "just-a-scalar-string\n")

    invocations = [
        ["scan", "1", "--mock", "-c", cfg],
        ["watch", "--once", "--mock", "-c", cfg],
        ["portfolio", "5xyz", "--mock", "-c", cfg],
    ]
    for argv in invocations:
        result = runner.invoke(cli.app, argv)
        assert result.exit_code == 1, argv
        assert "Traceback" not in result.stdout, argv
        assert "Traceback" not in result.stderr, argv
        assert "Error:" in result.stderr, argv


def test_scan_non_mapping_yaml_keeps_json_stdout_clean(monkeypatch, tmp_path):
    """A non-mapping config in --json mode keeps stdout empty (error -> stderr).

    The config-load failure happens before any JSON is emitted, so the error
    text must never contaminate the JSON stdout stream (mirrors finding 23's
    stdout/stderr separation).
    """
    _install_client(monkeypatch, MockTaostatsClient())
    cfg = _write(tmp_path / "toplist.yaml", "- a\n- b\n")
    result = runner.invoke(cli.app, ["scan", "1", "--mock", "--json", "-c", cfg])
    assert result.exit_code == 1
    assert result.stdout.strip() == ""
    assert "Error:" in result.stderr


def test_user_errors_catches_plain_valueerror(monkeypatch):
    """The ``_USER_ERRORS`` tuple matches a *plain* ValueError, not just pydantic's.

    Guards against the regression where only ``pydantic.ValidationError`` was
    listed: since that is a *subclass* of ``ValueError``, a bare ``ValueError``
    raised by ``load_config`` for a non-mapping document would slip past the
    handler. This asserts the relationship at the type level so the fix can't
    silently regress.
    """
    import pydantic

    # pydantic.ValidationError is a ValueError, but not vice versa.
    assert issubclass(pydantic.ValidationError, ValueError)
    assert isinstance(ValueError("x"), cli._USER_ERRORS)
    assert ValueError in cli._USER_ERRORS


def test_scan_invalid_watch_type_raises_at_load(monkeypatch, tmp_path):
    """A typo'd watch ``type`` fails loudly at config load (C10/C15).

    The config layer's field_validator raises pydantic.ValidationError, which
    the CLI surfaces as a concise error + exit 1 rather than a traceback.
    """
    _install_client(monkeypatch, MockTaostatsClient())
    cfg = _write(
        tmp_path / "typo.yaml",
        "watches:\n  - type: pirce_change\n    netuid: 1\n    threshold_pct: 10\n",
    )
    result = runner.invoke(cli.app, ["scan", "1", "--mock", "-c", cfg])
    assert result.exit_code == 1
    assert "Traceback" not in result.stderr
    # The offending value should be named for the user.
    assert "pirce_change" in result.stderr


# ---------------------------------------------------------------------------
# finding 23 - scan of a nonexistent netuid: same exit code in both modes,
# and the error text never lands in the JSON stdout stream.
# ---------------------------------------------------------------------------


def test_scan_nonexistent_netuid_exits_1_table_mode(monkeypatch):
    _install_client(monkeypatch, MockTaostatsClient())
    result = runner.invoke(cli.app, ["scan", "999", "--mock"])
    assert result.exit_code == 1


def test_scan_nonexistent_netuid_exits_1_json_mode(monkeypatch):
    """JSON mode must agree with table mode: exit 1, and stdout stays clean."""
    _install_client(monkeypatch, MockTaostatsClient())
    result = runner.invoke(cli.app, ["scan", "999", "--mock", "--json"])
    assert result.exit_code == 1
    # The not-found error must NOT be printed into the JSON stdout stream.
    assert result.stdout.strip() == ""
    assert "Error:" in result.stderr


def test_scan_valid_netuid_json_is_clean(monkeypatch):
    """A successful JSON scan still emits parseable JSON on stdout, exit 0."""
    import json

    _install_client(monkeypatch, MockTaostatsClient())
    result = runner.invoke(cli.app, ["scan", "1", "--mock", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert isinstance(payload, list) and payload
    assert payload[0]["netuid"] == 1


# ---------------------------------------------------------------------------
# finding 12 - the owned client is closed (CLI finally + web shutdown).
# ---------------------------------------------------------------------------


def test_scan_closes_client(monkeypatch):
    spy = _SpyClient()
    _install_client(monkeypatch, spy)
    result = runner.invoke(cli.app, ["scan", "1", "--mock"])
    assert result.exit_code == 0
    assert spy.closed is True


def test_portfolio_closes_client(monkeypatch):
    spy = _SpyClient()
    _install_client(monkeypatch, spy)
    result = runner.invoke(
        cli.app, ["portfolio", MockTaostatsClient.COLDKEY, "--mock"]
    )
    assert result.exit_code == 0
    assert spy.closed is True


def test_watch_once_closes_client(monkeypatch, tmp_path):
    spy = _SpyClient()
    _install_client(monkeypatch, spy)
    cfg = _write(
        tmp_path / "ok.yaml",
        f"state_path: {tmp_path / 'state.json'}\n"
        "watches:\n  - type: price_change\n    netuid: 1\n    threshold_pct: 10\n",
    )
    result = runner.invoke(cli.app, ["watch", "--once", "--mock", "-c", cfg])
    assert result.exit_code == 0
    assert spy.closed is True


def test_scan_closes_client_even_on_api_error(monkeypatch):
    """A TaostatsError during the scan still closes the client (finally)."""
    bad = _RaisingClient()
    _install_client(monkeypatch, bad)
    result = runner.invoke(cli.app, ["scan", "1", "--mock"])
    assert result.exit_code == 1
    assert "Traceback" not in result.stderr
    assert "Error:" in result.stderr
    assert bad.closed is True


def test_web_app_closes_client_on_shutdown(monkeypatch):
    """The web app closes its owned client when the lifespan exits (finding 12)."""
    spy = _SpyClient()
    _install_client(monkeypatch, spy)
    from tao_sentinel.web.app import create_app

    app = create_app(config_path=None, mock=True)
    with TestClient(app) as client:
        client.get("/api/status")
    # Exiting the TestClient context runs the shutdown lifespan.
    assert spy.closed is True


# ---------------------------------------------------------------------------
# finding 10 - TTL cache is single-flight under concurrency (no API stampede).
# ---------------------------------------------------------------------------


def test_ttl_cache_single_flight_under_concurrency():
    """A concurrent burst of cold requests triggers exactly one loader run."""
    from tao_sentinel.web.app import _TTLCache

    cache = _TTLCache(ttl_seconds=1000.0)
    calls = {"n": 0}

    def loader():
        calls["n"] += 1
        time.sleep(0.05)  # widen the race window
        return "value"

    results: list[str] = []
    results_lock = threading.Lock()
    start = threading.Event()

    def worker():
        start.wait()
        value = cache.get("k", loader)
        with results_lock:
            results.append(value)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    start.set()
    for t in threads:
        t.join()

    assert calls["n"] == 1  # single-flight: loaded once despite 20 racers
    assert results == ["value"] * 20


def test_ttl_cache_refills_after_expiry():
    """A stale entry serves stale-while-revalidating; the refresh lands for
    the NEXT call (SWR semantics: requests never block once a value exists)."""
    from tao_sentinel.web.app import _TTLCache

    clock = {"t": 0.0}
    cache = _TTLCache(
        ttl_seconds=10.0, clock=lambda: clock["t"], swr_background=False
    )
    calls = {"n": 0}

    def loader():
        calls["n"] += 1
        return calls["n"]

    assert cache.get("k", loader) == 1
    assert cache.get("k", loader) == 1  # warm: no reload
    clock["t"] = 20.0  # expire
    assert cache.get("k", loader) == 1  # stale served; refresh ran inline
    assert calls["n"] == 2  # the reload DID happen
    assert cache.get("k", loader) == 2  # next call sees the fresh value


def test_web_concurrent_cold_requests_do_not_stampede(monkeypatch):
    """Concurrent cold /api/status requests share one underlying fetch."""
    spy = _SpyClient()
    # Slow the subnet fetch a touch so concurrent requests overlap in the loader.
    orig_get_subnets = spy.get_subnets

    def slow_get_subnets():
        time.sleep(0.05)
        return orig_get_subnets()

    monkeypatch.setattr(spy, "get_subnets", slow_get_subnets)
    _install_client(monkeypatch, spy)

    from tao_sentinel.web.app import create_app

    app = create_app(config_path=None, mock=True)
    with TestClient(app) as client:
        results: list[int] = []
        results_lock = threading.Lock()
        start = threading.Event()

        def hit():
            start.wait()
            resp = client.get("/api/status")
            with results_lock:
                results.append(resp.status_code)

        threads = [threading.Thread(target=hit) for _ in range(10)]
        for t in threads:
            t.start()
        start.set()
        for t in threads:
            t.join()

    assert results == [200] * 10
    # Single-flight cache => the expensive list fetches run once, not per request.
    assert spy.get_subnets_calls == 1
    assert spy.get_pools_calls == 1


# ---------------------------------------------------------------------------
# finding 19 - a cold dashboard refill fetches the pool list once and shares it.
# ---------------------------------------------------------------------------


def test_dashboard_cold_refill_fetches_pools_once(monkeypatch, tmp_path):
    """With a coldkey configured, a cold build fetches get_pools exactly once."""
    spy = _SpyClient()
    _install_client(monkeypatch, spy)

    # A config that names a watched coldkey so the portfolio section is built too.
    cfg = _write(
        tmp_path / "ck.yaml",
        "watches:\n"
        "  - type: stake_change\n"
        f"    coldkey: {MockTaostatsClient.COLDKEY}\n"
        "    threshold_pct: 10\n",
    )

    from tao_sentinel.web.app import create_app

    app = create_app(config_path=str(cfg), mock=True)
    with TestClient(app) as client:
        r1 = client.get("/api/status")
        assert r1.status_code == 200
        assert r1.json()["portfolio"] is not None  # portfolio section present

        # Cold render: pools fetched once and SHARED by scan + portfolio.
        assert spy.get_pools_calls == 1
        assert spy.get_subnets_calls == 1

        # Warm render (same TTL window): zero additional API calls.
        r2 = client.get("/api/status")
        assert r2.status_code == 200
        assert spy.get_pools_calls == 1
        assert spy.get_subnets_calls == 1


def test_dashboard_html_and_json_share_cache(monkeypatch):
    """``/`` and ``/api/status`` reuse one cache entry: pools fetched once total."""
    spy = _SpyClient()
    _install_client(monkeypatch, spy)

    from tao_sentinel.web.app import create_app

    app = create_app(config_path=None, mock=True)
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/status").status_code == 200
        assert spy.get_pools_calls == 1
        assert spy.get_subnets_calls == 1


# --------------------------------------------------------------------------- #
# watch --once notifier dispatch (E2E finding: --once used to bypass
# configured notifiers) and the --no-notify dry-run switch
# --------------------------------------------------------------------------- #


def _tampered_watch_setup(tmp_path):
    """Write a watch config + a baseline state tampered to fire price_change.

    Returns the config path. The mock pool for netuid 64 reports 0.0333 TAO,
    so a persisted baseline of 0.02664 is a +25% move (threshold 10%).
    """
    import json as _json

    state_path = tmp_path / "state.json"
    cfg = tmp_path / "watch.yaml"
    cfg.write_text(
        "state_path: " + str(state_path) + "\n"
        "webhook_url: http://127.0.0.1:1/unreachable\n"
        "watches:\n"
        "  - type: price_change\n"
        "    netuid: 64\n"
        "    threshold_pct: 10.0\n"
    )
    from tao_sentinel.alerts.engine import WatchEngine
    from tao_sentinel.api import MockTaostatsClient
    from tao_sentinel.config import load_config

    engine = WatchEngine(
        client=MockTaostatsClient(), config=load_config(str(cfg)), notifiers=[]
    )
    _, state = engine.run_once(engine.load_state())
    state["snapshot"]["pools"]["64"]["price_tao"] = 0.02664
    engine.save_state(state)
    return cfg


def test_watch_once_dispatches_to_configured_notifiers(tmp_path, monkeypatch):
    """--once must deliver through build_notifiers(), not console-only."""
    sent: list = []

    from tao_sentinel.alerts import notify as notify_mod

    class SpyNotifier(notify_mod.Notifier):
        def send(self, alert):
            sent.append(alert)

    real_build = notify_mod.build_notifiers
    monkeypatch.setattr(
        notify_mod,
        "build_notifiers",
        lambda cfg: real_build(cfg)[:1] + [SpyNotifier()],
    )

    cfg = _tampered_watch_setup(tmp_path)
    result = runner.invoke(cli.app, ["watch", "--once", "--mock", "-c", str(cfg)])
    assert result.exit_code == 0
    assert len(sent) == 1, "configured notifier did not receive the alert"
    assert sent[0].rule_type == "price_change"


def test_watch_once_no_notify_is_console_only(tmp_path, monkeypatch):
    """--no-notify must NOT construct the configured notifier set."""
    from tao_sentinel.alerts import notify as notify_mod

    called = []
    real_build = notify_mod.build_notifiers
    monkeypatch.setattr(
        notify_mod,
        "build_notifiers",
        lambda cfg: called.append(True) or real_build(cfg),
    )

    cfg = _tampered_watch_setup(tmp_path)
    result = runner.invoke(
        cli.app, ["watch", "--once", "--mock", "--no-notify", "-c", str(cfg)]
    )
    assert result.exit_code == 0
    assert "1 alert(s)" in result.output
    assert not called, "--no-notify still built the configured notifiers"


# --------------------------------------------------------------------------- #
# Dashboard provisional caveat (E2E finding: disclosure was JSON-only)
# --------------------------------------------------------------------------- #


def test_dashboard_surfaces_provisional_flag():
    """The provisional disclosure reaches whichever HTML surface is active.

    With the React build present, / serves the SPA shell and the client
    renders the caveat from meta.provisional (assert the flag + shell here);
    without it, the legacy Jinja page must carry the caveat text itself.
    """
    from tao_sentinel.web.app import _STATIC_DIR, create_app

    with TestClient(create_app(None, mock=True)) as client:
        html = client.get("/").text
        if (_STATIC_DIR / "index.html").is_file():
            assert 'id="root"' in html  # SPA shell
        else:
            assert "Provisional scores" in html  # legacy server-rendered
        status = client.get("/api/status").json()
        assert status["meta"]["provisional"] is True


# --------------------------------------------------------------------------- #
# JSON float rounding (E2E finding: 87.55000000000001 artifacts)
# --------------------------------------------------------------------------- #


def test_portfolio_json_has_no_float_artifacts(monkeypatch):
    """Summed totals must be rounded (9 dp) in the JSON stream."""
    import json as _json

    result = runner.invoke(
        cli.app,
        [
            "portfolio",
            "5MockColdkey0000000000000000000000000000000000000000000",
            "--mock",
            "--json",
        ],
    )
    assert result.exit_code == 0
    payload = _json.loads(result.stdout)
    assert payload["total_value_tao"] == 87.55
    assert "000000000001" not in result.stdout


# --------------------------------------------------------------------------- #
# C7 - version bump to 0.2.0
# --------------------------------------------------------------------------- #


def test_version_is_0_2_0():
    """The package __version__ reflects the v0.2.0 bump."""
    from tao_sentinel import __version__

    assert __version__ == "0.2.0"


def test_version_command_reports_0_2_0():
    """`tao-sentinel version` prints the bumped version."""
    result = runner.invoke(cli.app, ["version"])
    assert result.exit_code == 0
    assert "0.2.0" in result.output


# --------------------------------------------------------------------------- #
# C8 - watch --help mentions the new watch types
# --------------------------------------------------------------------------- #


def test_watch_help_mentions_new_watch_types():
    """`watch --help` documents the v0.2.0 watch types for discoverability."""
    result = runner.invoke(cli.app, ["watch", "--help"])
    assert result.exit_code == 0
    for watch_type in ("tao_price", "market_cap", "registration_cost",
                       "new_subnet", "price_trend"):
        assert watch_type in result.output
