"""Tests for the Taostats API client layer (no network).

Covers:

* :class:`~tao_sentinel.api.RateLimiter` blocking behaviour driven by an
  injected fake clock/sleep (no real wall-clock delay).
* :data:`~tao_sentinel.api.ENDPOINTS` registry completeness.
* RAO -> TAO conversion of canned response dicts via the module's small parse
  helpers (``parse_pool``, ``parse_stake_position``, etc.).
* The :func:`~tao_sentinel.api.make_client` factory falling back to the mock.
"""

from __future__ import annotations

import threading

import httpx
import pytest

from tao_sentinel.api import (
    ENDPOINTS,
    MAX_HISTORY_POINTS,
    MAX_PAGES,
    MAX_RETRIES,
    MockTaostatsClient,
    RAO_PER_TAO,
    RateLimiter,
    TaostatsClient,
    TaostatsError,
    _amount_maybe_rao,
    downsample_points,
    make_client,
    normalize_emission_shares,
    parse_pool,
    parse_price_point,
    parse_stake_position,
    parse_subnet_info,
    parse_tao_price,
    parse_validator_info,
    rao_to_tao,
)
from tao_sentinel.models import (
    Pool,
    PricePoint,
    StakePosition,
    SubnetInfo,
    TaoPrice,
    ValidatorInfo,
)


def _make_client(handler, *, rate_limiter=None, retry_sleep=None):
    """Build a :class:`TaostatsClient` wired to a ``MockTransport`` handler.

    The rate limiter defaults to an effectively-unlimited one so tests exercise
    HTTP/retry behaviour without real throttling delay, and ``retry_sleep``
    defaults to a no-op so backoff never blocks the test.
    """
    transport = httpx.MockTransport(handler)
    client = httpx.Client(base_url="https://api.taostats.io", transport=transport)
    return TaostatsClient(
        "test-key",
        client=client,
        rate_limiter=rate_limiter or RateLimiter(1_000_000),
        retry_sleep=retry_sleep if retry_sleep is not None else (lambda _s: None),
    )


# --------------------------------------------------------------------------- #
# RateLimiter
# --------------------------------------------------------------------------- #


def test_rate_limiter_allows_burst_up_to_capacity(fake_clock, fake_sleep):
    """The first ``calls_per_min`` acquisitions never block."""
    limiter = RateLimiter(5, clock=fake_clock, sleep=fake_sleep)
    for _ in range(5):
        limiter.acquire()
    assert fake_sleep.calls == []  # no sleeping while tokens remain


def test_rate_limiter_blocks_after_capacity_exhausted(fake_clock, fake_sleep):
    """The (N+1)th acquisition with no time elapsed must block (sleep)."""
    limiter = RateLimiter(5, clock=fake_clock, sleep=fake_sleep)
    for _ in range(5):
        limiter.acquire()
    assert fake_sleep.calls == []

    # Bucket is now empty and the clock has not advanced on its own; the next
    # acquire must wait for a token to regenerate. FakeSleep advances the clock,
    # so the call still returns (deterministically) rather than hanging.
    limiter.acquire()

    assert len(fake_sleep.calls) >= 1
    # Refill rate is 5/60 tokens per second, so one token takes 12 seconds.
    assert fake_sleep.total_slept == pytest.approx(12.0, rel=1e-6)


def test_rate_limiter_does_not_block_when_time_passes(fake_clock, fake_sleep):
    """Advancing the clock past the refill window avoids any blocking."""
    limiter = RateLimiter(5, clock=fake_clock, sleep=fake_sleep)
    for _ in range(5):
        limiter.acquire()

    # Let a full minute elapse: the bucket refills to capacity.
    fake_clock.advance(60.0)
    for _ in range(5):
        limiter.acquire()

    assert fake_sleep.calls == []  # never had to wait


def test_rate_limiter_rejects_non_positive_rate():
    """A non-positive rate is a programming error and is rejected."""
    with pytest.raises(ValueError):
        RateLimiter(0)


# --------------------------------------------------------------------------- #
# ENDPOINTS registry
# --------------------------------------------------------------------------- #


REQUIRED_ENDPOINT_KEYS = {
    "tao_price",
    "pools",
    "stake_balances",
    "subnets",
    "validators",
    "tao_price_history",
    "pool_history",
}


def test_endpoints_registry_is_complete():
    """Every public client method has a corresponding endpoint path."""
    assert REQUIRED_ENDPOINT_KEYS.issubset(ENDPOINTS.keys())


def test_endpoints_paths_are_well_formed():
    """Each endpoint path is a non-empty string rooted under ``/api``."""
    for key in REQUIRED_ENDPOINT_KEYS:
        path = ENDPOINTS[key]
        assert isinstance(path, str)
        assert path.startswith("/api/"), f"{key} path should live under /api/"


# --------------------------------------------------------------------------- #
# RAO -> TAO conversion + parse helpers
# --------------------------------------------------------------------------- #


def test_rao_to_tao_divides_by_1e9():
    """RAO string amounts are divided by 1e9 to whole TAO/alpha."""
    assert RAO_PER_TAO == 1_000_000_000.0
    assert rao_to_tao("1000000000") == pytest.approx(1.0)
    assert rao_to_tao("38386670943") == pytest.approx(38.386670943)
    assert rao_to_tao(2_500_000_000) == pytest.approx(2.5)


def test_rao_to_tao_handles_unparseable_values():
    """Empty/None/garbage RAO values degrade to ``None`` rather than raising."""
    assert rao_to_tao(None) is None
    assert rao_to_tao("") is None
    assert rao_to_tao("not-a-number") is None


def test_parse_pool_converts_reserves_but_not_price():
    """``price`` stays TAO-denominated; reserve fields are RAO -> /1e9."""
    item = {
        "netuid": 1,
        "name": "apex",
        "price": "0.0254",  # already TAO per alpha; NOT divided
        "market_cap": "124000.0",
        "total_tao": "123456789000000",  # RAO -> /1e9
        "alpha_in_pool": "4567890000000000",  # alpha-RAO -> /1e9
    }
    pool = parse_pool(item)
    assert isinstance(pool, Pool)
    assert pool.netuid == 1
    assert pool.name == "apex"
    assert pool.price_tao == pytest.approx(0.0254)  # unscaled
    assert pool.market_cap_tao == pytest.approx(124000.0)
    assert pool.tao_in == pytest.approx(123456.789)
    assert pool.alpha_in == pytest.approx(4567890.0)


def test_parse_stake_position_converts_alpha_balance():
    """``balance`` (alpha-RAO) -> whole alpha; nested ss58 objects unwrapped."""
    item = {
        "coldkey": {"ss58": "5CGwColdkey", "hex": "0x01"},
        "hotkey": {"ss58": "5HK5Hotkey", "hex": "0x02"},
        "netuid": 64,
        "balance": "1000000000",  # 1 alpha
        "balance_as_tao": "0.42",  # already TAO value
    }
    pos = parse_stake_position(item)
    assert isinstance(pos, StakePosition)
    assert pos.coldkey == "5CGwColdkey"
    assert pos.hotkey == "5HK5Hotkey"
    assert pos.netuid == 64
    assert pos.alpha_staked == pytest.approx(1.0)
    assert pos.value_tao == pytest.approx(0.42)


def test_parse_subnet_info_uses_min_burn_as_registration_proxy():
    """``min_burn`` (RAO) becomes the registration cost when none given."""
    item = {
        "netuid": 8,
        "name": "chutes",
        "emission_pct": 11.0,
        "max_validators": 64,
        "min_burn": "1100000000",  # 1.1 TAO
    }
    info = parse_subnet_info(item)
    assert isinstance(info, SubnetInfo)
    assert info.netuid == 8
    assert info.name == "chutes"
    assert info.emission_pct == pytest.approx(11.0)
    assert info.n_validators == 64
    assert info.registration_cost_tao == pytest.approx(1.1)


def test_parse_validator_info_converts_stake_and_reads_vtrust():
    """Per-subnet ``stake`` (alpha-RAO) -> /1e9; ``validator_trust`` is vtrust."""
    item = {
        "hotkey": {"ss58": "5FValidator", "hex": "0x03"},
        "netuid": 1,
        "stake": "50000000000000",  # 50000 alpha
        "validator_trust": "0.987",
        "active": True,
    }
    val = parse_validator_info(item)
    assert isinstance(val, ValidatorInfo)
    assert val.hotkey == "5FValidator"
    assert val.netuid == 1
    assert val.stake_tao == pytest.approx(50000.0)
    assert val.vtrust == pytest.approx(0.987)
    assert val.active is True


def test_parse_tao_price_reads_usd_string():
    """TAO/USD ``price`` is whole-token (not RAO) and parsed as a float."""
    item = {"price": "412.37", "last_updated": "2026-06-03T00:00:00Z"}
    price = parse_tao_price(item)
    assert isinstance(price, TaoPrice)
    assert price.usd == pytest.approx(412.37)
    assert price.timestamp == "2026-06-03T00:00:00Z"


# --------------------------------------------------------------------------- #
# make_client factory
# --------------------------------------------------------------------------- #


def test_make_client_returns_mock_when_forced():
    """``mock=True`` always yields the mock client."""
    client = make_client(api_key="tao-some-key", mock=True)
    assert isinstance(client, MockTaostatsClient)


def test_make_client_returns_mock_when_no_key():
    """Absent an API key, the factory falls back to the mock client."""
    assert isinstance(make_client(api_key=None, mock=False), MockTaostatsClient)
    assert isinstance(make_client(api_key="", mock=False), MockTaostatsClient)


def test_make_client_returns_real_client_with_key():
    """A real key with ``mock=False`` yields a live ``TaostatsClient``."""
    client = make_client(api_key="tao-7051ffef:92a1cf8a", mock=False)
    assert isinstance(client, TaostatsClient)
    assert client.api_key == "tao-7051ffef:92a1cf8a"


# --------------------------------------------------------------------------- #
# Denomination heuristic & emission normalization
# --------------------------------------------------------------------------- #


def test_amount_maybe_rao_passes_tao_scale_values_through():
    """Plausible TAO-scale amounts are not rescaled."""
    assert _amount_maybe_rao(210000.0) == 210000.0
    assert _amount_maybe_rao("175000") == 175000.0
    assert _amount_maybe_rao(None) is None


def test_amount_maybe_rao_converts_rao_scale_values():
    """Amounts beyond the 21M-TAO supply bound must be RAO -> divide by 1e9."""
    assert _amount_maybe_rao("152095953327852416") == pytest.approx(
        152095953327852416 / RAO_PER_TAO
    )
    assert _amount_maybe_rao(2.85e14) == pytest.approx(2.85e14 / RAO_PER_TAO)


def test_parse_pool_market_cap_uses_heuristic():
    """A RAO-denominated market_cap lands in the model as TAO."""
    pool = parse_pool({"netuid": 64, "price": "0.0333",
                       "market_cap": "175000000000000"})
    assert pool.market_cap_tao == pytest.approx(175000.0)


def test_normalize_emission_shares_rescales_raw_amounts():
    """Raw TAO emission amounts are rescaled to percentages summing to 100."""
    subnets = [
        SubnetInfo(netuid=1, emission_pct=30.0),
        SubnetInfo(netuid=2, emission_pct=70.0),
        SubnetInfo(netuid=3, emission_pct=None),
    ]
    out = normalize_emission_shares(subnets)
    assert out[0].emission_pct == pytest.approx(30.0)
    assert out[1].emission_pct == pytest.approx(70.0)
    assert out[2].emission_pct is None
    # Amounts on an arbitrary scale normalize to the same shares.
    scaled = normalize_emission_shares([
        SubnetInfo(netuid=1, emission_pct=3.0),
        SubnetInfo(netuid=2, emission_pct=7.0),
    ])
    assert scaled[0].emission_pct == pytest.approx(30.0)
    assert scaled[1].emission_pct == pytest.approx(70.0)


def test_normalize_emission_shares_noop_without_data():
    """No emission data -> list unchanged, no division by zero."""
    subnets = [SubnetInfo(netuid=1), SubnetInfo(netuid=2)]
    assert normalize_emission_shares(subnets) == subnets


# --------------------------------------------------------------------------- #
# Finding 4 / 26 / C4: provenance-gated emission normalization in get_subnets
# --------------------------------------------------------------------------- #


def test_get_subnets_keeps_native_percentages_unchanged():
    """When rows carry native ``emission_pct``, get_subnets does NOT rescale.

    These percentages sum to 35.1 (a truncated/partial network view), and the
    pre-fix code would have rescaled them to sum to 100, corrupting genuine
    percentages. Provenance gating must leave them untouched.
    """
    payload = {
        "pagination": {"next_page": None},
        "data": [
            {"netuid": 1, "emission_pct": 8.5},
            {"netuid": 2, "emission_pct": 6.2},
            {"netuid": 3, "emission_pct": 11.0},
            {"netuid": 4, "emission_pct": 9.4},
        ],
    }
    client = _make_client(lambda _r: httpx.Response(200, json=payload))
    subnets = client.get_subnets()
    assert [round(s.emission_pct, 2) for s in subnets] == [8.5, 6.2, 11.0, 9.4]


def test_get_subnets_normalizes_only_raw_amount_fallback():
    """With no native percentage, raw RAO emission is rescaled to sum 100."""
    payload = {
        "pagination": {"next_page": None},
        "data": [
            {"netuid": 1, "emission": "30000000000"},  # 30 TAO
            {"netuid": 2, "emission": "70000000000"},  # 70 TAO
        ],
    }
    client = _make_client(lambda _r: httpx.Response(200, json=payload))
    subnets = client.get_subnets()
    assert [round(s.emission_pct, 2) for s in subnets] == [30.0, 70.0]


def test_get_subnets_native_pct_on_any_row_disables_normalization():
    """A single native ``emission_pct`` row marks the whole set as percentages."""
    payload = {
        "pagination": {"next_page": None},
        "data": [
            {"netuid": 1, "emission_pct": 8.5},
            # No native pct here, but the set is provenance == native overall.
            {"netuid": 2, "emission": "70000000000"},
        ],
    }
    client = _make_client(lambda _r: httpx.Response(200, json=payload))
    subnets = client.get_subnets()
    # netuid 1's value is left as-is (not rescaled to sum 100 with netuid 2).
    by_netuid = {s.netuid: s.emission_pct for s in subnets}
    assert by_netuid[1] == pytest.approx(8.5)


def test_mock_client_subnets_match_native_percentage_case():
    """The mock represents the native-percentage case and stays un-normalized.

    The fixtures sum to 35.1 (not 100); the mock mirrors the real client's
    native-percentage branch, which leaves percentages untouched.
    """
    subnets = MockTaostatsClient().get_subnets()
    total = sum(s.emission_pct for s in subnets)
    assert total == pytest.approx(35.1)
    assert total != pytest.approx(100.0)


# --------------------------------------------------------------------------- #
# Finding 15: parse_subnet_info market_cap uses the RAO heuristic
# --------------------------------------------------------------------------- #


def test_parse_subnet_info_market_cap_uses_rao_heuristic():
    """A RAO-denominated subnet market_cap normalizes to TAO like parse_pool."""
    info = parse_subnet_info({"netuid": 1, "market_cap": "124000000000000"})
    pool = parse_pool({"netuid": 1, "price": "0.0254",
                       "market_cap": "124000000000000"})
    assert info.market_cap_tao == pytest.approx(124000.0)
    # Both code paths now agree (no 1e9x mismatch).
    assert info.market_cap_tao == pytest.approx(pool.market_cap_tao)


# --------------------------------------------------------------------------- #
# Finding 16 / C5: _amount_maybe_rao threshold lowered to 3e7
# --------------------------------------------------------------------------- #


def test_amount_maybe_rao_threshold_lowered_to_3e7():
    """Values above 3e7 (just over the 21M-TAO supply cap) are treated as RAO."""
    # 5 TAO worth of RAO (5e9) is now correctly converted (was a blind spot).
    assert _amount_maybe_rao("5000000000") == pytest.approx(5.0)
    # Just above the threshold -> RAO.
    assert _amount_maybe_rao(3.0e7 + 1) == pytest.approx((3.0e7 + 1) / RAO_PER_TAO)
    # At/below the threshold -> passed through as TAO (the documented blind spot
    # for genuine sub-0.03-TAO RAO dust).
    assert _amount_maybe_rao(3.0e7) == pytest.approx(3.0e7)
    assert _amount_maybe_rao(0.03) == pytest.approx(0.03)


# --------------------------------------------------------------------------- #
# Finding 17 / C1: transport-error normalization + 429/5xx retry
# --------------------------------------------------------------------------- #


def test_get_normalizes_transport_error_to_status_zero():
    """A transport/timeout error becomes TaostatsError(0, ...), one type."""

    def handler(_request):
        raise httpx.ConnectTimeout("connection timed out")

    client = _make_client(handler)
    with pytest.raises(TaostatsError) as excinfo:
        client.get_tao_price()
    assert excinfo.value.status == 0
    assert "connection timed out" in str(excinfo.value)


def test_get_retries_429_then_succeeds():
    """A 429 is retried and the subsequent 200 succeeds."""
    calls = {"n": 0}

    def handler(_request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"message": "rate limited"})
        return httpx.Response(200, json={"data": [{"price": "412.0"}]})

    client = _make_client(handler)
    price = client.get_tao_price()
    assert price.usd == pytest.approx(412.0)
    assert calls["n"] == 2  # one retry


def test_get_honors_integer_retry_after_via_injected_sleep():
    """An integer Retry-After header drives the retry wait (capped at 30)."""
    slept: list[float] = []
    calls = {"n": 0}

    def handler(_request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "7"},
                                  json={"message": "rl"})
        return httpx.Response(200, json={"data": [{"price": "1.0"}]})

    client = _make_client(handler, retry_sleep=slept.append)
    client.get_tao_price()
    assert slept == [7.0]


def test_get_caps_retry_after_at_thirty_seconds():
    """A hostile/large Retry-After is capped at 30s."""
    slept: list[float] = []
    calls = {"n": 0}

    def handler(_request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, headers={"Retry-After": "999"},
                                  json={"message": "down"})
        return httpx.Response(200, json={"data": [{"price": "1.0"}]})

    client = _make_client(handler, retry_sleep=slept.append)
    client.get_tao_price()
    assert slept == [30.0]


def test_get_falls_back_to_2s_then_4s_backoff_then_raises():
    """With no Retry-After, backoff is 2s then 4s, then the error propagates."""
    slept: list[float] = []
    calls = {"n": 0}

    def handler(_request):
        calls["n"] += 1
        return httpx.Response(500, json={"message": "boom"})

    client = _make_client(handler, retry_sleep=slept.append)
    with pytest.raises(TaostatsError) as excinfo:
        client.get_tao_price()
    assert excinfo.value.status == 500
    assert slept == [2.0, 4.0]
    assert calls["n"] == MAX_RETRIES + 1  # initial attempt + 2 retries


def test_get_does_not_retry_4xx_other_than_429():
    """A non-retryable 4xx (e.g. 401) fails immediately with no retry."""
    calls = {"n": 0}

    def handler(_request):
        calls["n"] += 1
        return httpx.Response(401, json={"message": "unauthorized"})

    client = _make_client(handler)
    with pytest.raises(TaostatsError) as excinfo:
        client.get_tao_price()
    assert excinfo.value.status == 401
    assert calls["n"] == 1  # no retry


def test_rate_limiter_acquires_before_every_attempt_including_retries():
    """The limiter is acquired once per attempt, retries included."""
    acquires = {"n": 0}

    class CountingLimiter(RateLimiter):
        def acquire(self) -> None:
            acquires["n"] += 1
            super().acquire()

    def handler(_request):
        return httpx.Response(500, json={"message": "err"})

    client = _make_client(handler, rate_limiter=CountingLimiter(1_000_000))
    with pytest.raises(TaostatsError):
        client.get_tao_price()
    # initial attempt + MAX_RETRIES retries, each preceded by an acquire.
    assert acquires["n"] == MAX_RETRIES + 1


# --------------------------------------------------------------------------- #
# Finding 18 / C6: pagination truncation warning at MAX_PAGES
# --------------------------------------------------------------------------- #


def test_pagination_truncation_logs_warning_naming_endpoint(caplog):
    """Hitting MAX_PAGES with next_page still set logs a named-endpoint warning."""
    def handler(_request):
        # Always reports a next page, so the loop runs the full cap.
        return httpx.Response(200, json={"pagination": {"next_page": 99},
                                          "data": [{"netuid": 1, "price": "0.1"}]})

    client = _make_client(handler)
    with caplog.at_level("WARNING", logger="tao_sentinel.api"):
        rows = client.get_pools()
    assert len(rows) == MAX_PAGES  # one row per page, capped
    truncation = [r for r in caplog.records if "Pagination truncated" in r.message]
    assert truncation, "expected a truncation warning"
    assert ENDPOINTS["pools"] in truncation[0].getMessage()


def test_pagination_no_warning_when_next_page_clears(caplog):
    """A finite list (next_page eventually None) logs no truncation warning."""
    pages = {"n": 0}

    def handler(_request):
        pages["n"] += 1
        next_page = 2 if pages["n"] == 1 else None
        return httpx.Response(200, json={"pagination": {"next_page": next_page},
                                          "data": [{"netuid": pages["n"], "price": "0.1"}]})

    client = _make_client(handler)
    with caplog.at_level("WARNING", logger="tao_sentinel.api"):
        rows = client.get_pools()
    assert len(rows) == 2
    assert not [r for r in caplog.records if "Pagination truncated" in r.message]


# --------------------------------------------------------------------------- #
# Finding 5 / C2: thread-safe RateLimiter never over-admits
# --------------------------------------------------------------------------- #


class _WaitSignalled(Exception):
    """Raised by the test sleep so a throttled acquire unwinds instead of hangs."""


def test_rate_limiter_threaded_never_over_admits():
    """Under heavy concurrency, total admissions never exceed available tokens.

    Many threads hammer a frozen-clock limiter at once. The clock never
    advances, so no tokens regenerate: exactly ``capacity`` acquires must
    succeed and every other thread must be forced into ``sleep``. The injected
    sleep raises immediately (rather than blocking), so a throttled thread
    unwinds deterministically and the test cannot hang. A racy (lockless)
    limiter lets several threads observe the same token and decrement past
    empty, admitting MORE than ``capacity``.
    """

    def frozen_clock() -> float:
        return 0.0

    def raising_sleep(_seconds: float) -> None:
        # No token will ever regenerate (clock frozen); rather than block, bail
        # so the worker thread terminates. Over-admission shows up as too many
        # successful acquires, never as a hang.
        raise _WaitSignalled

    capacity = 5
    limiter = RateLimiter(capacity, clock=frozen_clock, sleep=raising_sleep)

    admitted = 0
    admitted_lock = threading.Lock()
    n_threads = 40
    barrier = threading.Barrier(n_threads)

    def worker() -> None:
        barrier.wait()  # release all threads simultaneously for max contention
        try:
            limiter.acquire()
        except _WaitSignalled:
            return
        nonlocal admitted
        with admitted_lock:
            admitted += 1

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)
        assert not t.is_alive(), "rate limiter acquire hung a worker thread"

    assert admitted == capacity, (
        f"limiter admitted {admitted}, expected exactly {capacity} (over-admit)"
    )


def test_rate_limiter_concurrent_decrement_is_exact(fake_clock, fake_sleep):
    """Sequential capacity burst still admits exactly capacity without sleeping.

    Guards the lock refactor against a regression that double-counts or skips a
    token under the new loop structure.
    """
    limiter = RateLimiter(5, clock=fake_clock, sleep=fake_sleep)
    for _ in range(5):
        limiter.acquire()
    assert fake_sleep.calls == []


def test_parse_subnet_info_live_api_shape_june_2026():
    """Field mapping verified against the production API (June 2026).

    The live subnet/latest row has no ``name``, carries the REAL populations
    in ``active_validators``/``active_miners``, and ``max_validators`` is the
    slot cap (must NOT win when a real count is present).
    """
    item = {
        "netuid": 64,
        "emission": "1148000000",          # RAO
        "active_validators": 8,
        "active_miners": 27,
        "validators": 8,
        "max_validators": 64,
        "max_neurons": 64,
        "registration_cost": 0,            # zero -> fall through to min_burn
        "min_burn": "500000000",
    }
    info = parse_subnet_info(item)
    assert info.name is None               # filled later by the pool merge
    assert info.n_validators == 8          # real population, not the 64 cap
    assert info.n_miners == 27
    assert info.registration_cost_tao == pytest.approx(0.5)
    assert info.emission_pct == pytest.approx(1.148)  # pre-normalization TAO


def test_parse_subnet_info_falls_back_to_cap_without_population():
    """Older/fixture rows without active_* still map from max_validators."""
    info = parse_subnet_info({"netuid": 1, "max_validators": 64})
    assert info.n_validators == 64


# --------------------------------------------------------------------------- #
# C1 / C9: history parsing, units, and downsampling
# --------------------------------------------------------------------------- #


def test_parse_price_point_reads_value_and_timestamp():
    """A TAO price-history row parses ``price`` (USD) + ``created_at`` verbatim."""
    point = parse_price_point(
        {"created_at": "2026-06-03T22:00:00Z", "price": "229.881483505172"},
        value_key="price",
        time_key="created_at",
    )
    assert isinstance(point, PricePoint)
    assert point.value == pytest.approx(229.881483505172)
    assert point.timestamp == "2026-06-03T22:00:00Z"


def test_parse_price_point_pool_price_is_tao_not_rao():
    """Pool ``price`` (alpha-in-TAO decimal) is NOT divided by 1e9."""
    point = parse_price_point(
        {"timestamp": "2026-06-03T03:19:12Z", "price": "0.009833819"},
        value_key="price",
        time_key="timestamp",
    )
    assert point is not None
    assert point.value == pytest.approx(0.009833819)  # unscaled, not /1e9
    assert point.timestamp == "2026-06-03T03:19:12Z"


def test_parse_price_point_unparseable_value_returns_none():
    """A junk/missing value yields ``None`` so callers can skip the row."""
    assert parse_price_point({"price": ""}, value_key="price", time_key="created_at") is None
    assert parse_price_point({}, value_key="price", time_key="created_at") is None


def test_downsample_points_noop_within_cap():
    """A series at or under the cap is returned unchanged."""
    pts = [PricePoint(timestamp=f"t{i}", value=float(i)) for i in range(10)]
    out = downsample_points(pts, max_points=48)
    assert out == pts


def test_downsample_points_caps_and_keeps_last():
    """A long series is reduced to the cap and always retains the final point."""
    pts = [PricePoint(timestamp=f"t{i}", value=float(i)) for i in range(500)]
    out = downsample_points(pts, max_points=MAX_HISTORY_POINTS)
    assert len(out) <= MAX_HISTORY_POINTS
    assert out[0].value == pytest.approx(0.0)  # oldest preserved
    assert out[-1].value == pytest.approx(499.0)  # newest never dropped
    # Order preserved (strictly ascending values here).
    assert [p.value for p in out] == sorted(p.value for p in out)


def test_downsample_points_single_point_target():
    """max_points=1 keeps only the most recent point."""
    pts = [PricePoint(timestamp=f"t{i}", value=float(i)) for i in range(5)]
    out = downsample_points(pts, max_points=1)
    assert out == [pts[-1]]


# --------------------------------------------------------------------------- #
# C1 / C9: live-client history methods (MockTransport, one call, ascending)
# --------------------------------------------------------------------------- #


def test_get_tao_price_history_parses_and_orders_ascending():
    """One descending page is parsed, reversed to ascending, capped at 48."""
    # API returns newest-first; build 120 candles descending in time.
    data = [
        {"created_at": f"2026-06-03T{i:02d}:00:00Z", "price": str(200.0 + i)}
        for i in range(120)
    ]  # i=0 is newest in this fake (value 200), i=119 oldest (value 319)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        assert "asset=tao" in str(request.url)
        return httpx.Response(200, json={"pagination": {"next_page": None}, "data": data})

    client = _make_client(handler)
    points = client.get_tao_price_history(hours=24)
    assert calls["n"] == 1  # exactly one API call (budget)
    assert len(points) <= MAX_HISTORY_POINTS
    # Reversed to chronological ascending: first emitted is the oldest row.
    assert points[0].value == pytest.approx(319.0)
    assert points[-1].value == pytest.approx(200.0)


def test_get_pool_history_uses_netuid_and_hourly_frequency():
    """get_pool_history sends netuid + by_hour, parses price as TAO, one call."""
    data = [
        {"timestamp": f"2026-06-03T{i:02d}:00:00Z", "price": str(0.01 + i / 10000.0)}
        for i in range(10)
    ]
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"pagination": {"next_page": None}, "data": data})

    client = _make_client(handler)
    points = client.get_pool_history(64, hours=24)
    assert "netuid=64" in seen["url"]
    assert "frequency=by_hour" in seen["url"]
    assert len(points) == 10
    # Endpoint returns newest-first; method reverses to ascending. The fake
    # rows are emitted index-ascending, so after reversal index 9 comes first.
    # Values are NOT divided by 1e9 (alpha price already in TAO).
    assert points[0].value == pytest.approx(0.01 + 9 / 10000.0)
    assert points[-1].value == pytest.approx(0.01)


def test_history_endpoint_unavailable_raises(monkeypatch):
    """If a history endpoint 4xxs, the client surfaces a TaostatsError."""
    def handler(_request):
        return httpx.Response(404, json={"message": "not found"})

    client = _make_client(handler)
    with pytest.raises(TaostatsError):
        client.get_tao_price_history()


# --------------------------------------------------------------------------- #
# C1 / C9: mock history fixtures (deterministic, smooth, <=48, graceful)
# --------------------------------------------------------------------------- #


def test_mock_tao_price_history_is_deterministic_and_capped():
    """Two calls return identical series, ascending, <= 48 points."""
    mock = MockTaostatsClient()
    a = mock.get_tao_price_history(hours=24)
    b = mock.get_tao_price_history(hours=24)
    assert [(p.timestamp, p.value) for p in a] == [(p.timestamp, p.value) for p in b]
    assert 0 < len(a) <= MAX_HISTORY_POINTS
    # Timestamps strictly ascending (chronological).
    assert [p.timestamp for p in a] == sorted(p.timestamp for p in a)
    # Smooth oscillation around the 350.0 fixture price (within +/- ~3%).
    assert all(330.0 <= p.value <= 370.0 for p in a)


def test_mock_pool_history_known_netuids_deterministic():
    """Each fixture netuid yields a stable smooth series around its price."""
    mock = MockTaostatsClient()
    bases = {1: 0.0254, 4: 0.0182, 8: 0.0410, 64: 0.0333}
    for netuid, base in bases.items():
        a = mock.get_pool_history(netuid, hours=24)
        b = mock.get_pool_history(netuid, hours=24)
        assert [(p.timestamp, p.value) for p in a] == [(p.timestamp, p.value) for p in b]
        assert 0 < len(a) <= MAX_HISTORY_POINTS
        # Oscillates within +/- ~5% of the fixture price.
        assert all(base * 0.94 <= p.value <= base * 1.06 for p in a)


def test_mock_pool_history_unknown_netuid_is_empty():
    """Unknown netuids degrade gracefully to an empty series (no sparkline)."""
    assert MockTaostatsClient().get_pool_history(999) == []


def test_mock_pool_history_distinct_phases_per_subnet():
    """Different subnets do not move in lockstep (distinct phases)."""
    mock = MockTaostatsClient()
    s1 = [p.value / 0.0254 for p in mock.get_pool_history(1, hours=24)]
    s4 = [p.value / 0.0182 for p in mock.get_pool_history(4, hours=24)]
    # Normalized series differ (phase offset), so they are not identical.
    assert s1 != s4


# --------------------------------------------------------------------------- #
# FileRateLimiter -- cross-process shared token bucket
# --------------------------------------------------------------------------- #


def _file_limiter_pair(tmp_path, rate=5):
    """Two limiters on ONE state file, as the watcher+dashboard processes."""
    from tao_sentinel.api import FileRateLimiter

    t = {"now": 1_000_000.0}
    slept: list[float] = []

    def clock():
        return t["now"]

    def sleep(s):
        slept.append(s)
        t["now"] += s  # advancing time lets blocked acquires terminate

    path = str(tmp_path / "ratelimit.json")
    a = FileRateLimiter(rate, path, clock=clock, sleep=sleep)
    b = FileRateLimiter(rate, path, clock=clock, sleep=sleep)
    return a, b, t, slept


def test_file_rate_limiter_shares_one_bucket_across_instances(tmp_path):
    """Five tokens TOTAL across both 'processes', not five each.

    This is the 429 fix: separate in-process buckets summed to 10/min on one
    API key; the shared file bucket keeps the pair at the real limit.
    """
    a, b, t, slept = _file_limiter_pair(tmp_path, rate=5)
    for _ in range(3):
        a.acquire()
    for _ in range(2):
        b.acquire()
    assert slept == []  # five tokens: full burst allowed across BOTH

    a.acquire()  # sixth call in the same instant must wait on either instance
    assert len(slept) >= 1
    assert sum(slept) > 0


def test_file_rate_limiter_refills_for_the_other_process(tmp_path):
    """Tokens regenerated by elapsed time are visible to the sibling."""
    a, b, t, slept = _file_limiter_pair(tmp_path, rate=5)
    for _ in range(5):
        a.acquire()
    t["now"] += 60.0  # a full minute: bucket refills to capacity
    for _ in range(5):
        b.acquire()  # the OTHER instance sees the refill
    assert slept == []


def test_file_rate_limiter_recovers_from_corrupt_state(tmp_path):
    """Garbage in the state file resets to a full bucket instead of crashing."""
    a, _, t, slept = _file_limiter_pair(tmp_path, rate=5)
    (tmp_path / "ratelimit.json").write_text("{not json")
    a.acquire()
    assert slept == []


def test_make_client_uses_shared_limiter_when_path_given(tmp_path):
    """A live client built with rate_limit_file carries a FileRateLimiter."""
    from tao_sentinel.api import FileRateLimiter

    client = make_client(
        "tao-key:x", mock=False, rate_limit_file=str(tmp_path / "rl.json")
    )
    try:
        assert isinstance(client._limiter, FileRateLimiter)
    finally:
        client.close()


def test_make_client_falls_back_when_path_unusable():
    """An unusable shared path degrades to the in-process limiter."""
    client = make_client(
        "tao-key:x", mock=False,
        rate_limit_file="/proc/definitely/not/writable/rl.json",
    )
    try:
        assert isinstance(client._limiter, RateLimiter)
    finally:
        client.close()


def test_429_retry_waits_at_least_a_token_interval(monkeypatch):
    """A 429 without Retry-After backs off >= 12s (one token), not 2s --
    a 2s retry lands inside the same closed rate window and is wasted."""
    waits: list[float] = []
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"message": "Rate Limited"})
        return httpx.Response(
            200, json={"pagination": None, "data": [{"price": "1.0",
                       "created_at": "t"}]}
        )

    client = _make_client(handler, retry_sleep=waits.append)
    try:
        client.get_tao_price()
    finally:
        client.close()
    assert waits and waits[0] >= 12.0


def test_5xx_retry_keeps_short_backoff(monkeypatch):
    """Transient 5xx still retries quickly (2s) -- only 429 gets the long wait."""
    waits: list[float] = []
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="upstream hiccup")
        return httpx.Response(
            200, json={"pagination": None, "data": [{"price": "1.0",
                       "created_at": "t"}]}
        )

    client = _make_client(handler, retry_sleep=waits.append)
    try:
        client.get_tao_price()
    finally:
        client.close()
    assert waits and waits[0] < 12.0
