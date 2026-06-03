"""Taostats API client for tao-sentinel.

This module implements the HTTP client (:class:`TaostatsClient`), a no-network
deterministic stand-in (:class:`MockTaostatsClient`), a blocking token-bucket
:class:`RateLimiter`, and the :func:`make_client` factory.

All amounts crossing the client boundary are normalized to whole TAO / whole
alpha units (floats). The Taostats API returns balance/stake/emission/reserve
fields as RAO strings (1 TAO = 1e9 RAO; 1 alpha = 1e9 alpha-RAO), so those are
divided by 1e9 here. The dTAO pool ``price`` field is the documented exception:
it is already expressed in TAO per alpha and is NOT divided.

Endpoint paths, the auth header convention, the response envelope, and the unit
conversions are all derived from live Taostats API research. Endpoint paths are
collected in the module-level :data:`ENDPOINTS` dict (one comment per entry
noting source confidence) so they can be patched easily if Taostats changes
them.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

import httpx

from .models import (
    Pool,
    StakePosition,
    SubnetInfo,
    TaoPrice,
    ValidatorInfo,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

#: Base URL for the Taostats REST API. All REST paths live under ``/api`` and
#: are versioned per-endpoint (e.g. ``.../v1``).
DEFAULT_BASE_URL = "https://api.taostats.io"

#: 1 TAO = 1e9 RAO (a.k.a. nanoTAO). Alpha tokens use the same 1e9 base unit.
RAO_PER_TAO = 1_000_000_000.0

#: Free-tier rate limit reported consistently across docs and community code.
DEFAULT_RATE_LIMIT_PER_MIN = 5

#: Default request timeout in seconds (mirrors the official ts-sdk default).
DEFAULT_TIMEOUT_SECONDS = 30.0

#: Page size requested for list endpoints. The SDK caps page size at 100; some
#: endpoints advertise a max of 200. 100 is safe everywhere.
DEFAULT_PAGE_LIMIT = 100

#: Hard cap on pages fetched per list call, a safety valve against runaway
#: pagination if ``next_page`` never becomes ``null``.
MAX_PAGES = 50

#: Maximum number of retries (on top of the first attempt) for a request that
#: fails with 429 or a 5xx response.
MAX_RETRIES = 2

#: Fallback backoff (seconds) per retry attempt when the server gives no usable
#: ``Retry-After`` header: 2s before the first retry, 4s before the second.
RETRY_BACKOFF_SECONDS = (2.0, 4.0)

#: Cap (seconds) applied to a server-provided integer ``Retry-After`` so a
#: hostile/large value cannot stall a command indefinitely.
MAX_RETRY_AFTER_SECONDS = 30

#: Module-level registry of endpoint paths. Populated from the API SPEC,
#: picking the highest-confidence path for each capability. Patch here if
#: Taostats changes a route.
ENDPOINTS: dict[str, str] = {
    # confidence: high - /api/price/latest/v1?asset=tao, CoinMarketCap-style payload.
    "tao_price": "/api/price/latest/v1",
    # confidence: high - /api/dtao/pool/latest/v1, primary per-subnet pool state.
    "pools": "/api/dtao/pool/latest/v1",
    # confidence: high - /api/dtao/stake_balance/latest/v1, per (hotkey, netuid) alpha stake.
    "stake_balances": "/api/dtao/stake_balance/latest/v1",
    # confidence: high - /api/subnet/latest/v1, subnet list with hyperparams + emission.
    "subnets": "/api/subnet/latest/v1",
    # confidence: high - /api/metagraph/latest/v1, per-subnet neuron/validator list.
    "validators": "/api/metagraph/latest/v1",
}


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class TaostatsError(Exception):
    """Raised on a non-2xx Taostats API response.

    Branch on HTTP status (the API uses 401/429/4xx/5xx) rather than relying on
    a specific JSON error envelope, which Taostats does not document.

    Attributes:
        status: HTTP status code returned by the API.
        body: Best-effort parsed JSON body, or the raw text if not JSON.
    """

    def __init__(self, status: int, body: Any) -> None:
        self.status = status
        self.body = body
        message = self._extract_message(body)
        super().__init__(f"Taostats API error {status}: {message}")

    @staticmethod
    def _extract_message(body: Any) -> str:
        """Defensively pull a human message out of an arbitrary error body."""
        if isinstance(body, dict):
            for key in ("message", "error", "detail"):
                value = body.get(key)
                if isinstance(value, str) and value:
                    return value
        if isinstance(body, str) and body:
            return body
        return str(body)


# --------------------------------------------------------------------------- #
# Unit / parsing helpers
# --------------------------------------------------------------------------- #


def _to_float(value: Any) -> Optional[float]:
    """Coerce a Taostats numeric field (often a string) to ``float``.

    Returns ``None`` for ``None`` / empty / unparseable values so that optional
    model fields degrade gracefully rather than raising.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        # Avoid treating bools as 0/1 numbers by accident.
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def rao_to_tao(value: Any) -> Optional[float]:
    """Convert a RAO (or alpha-RAO) amount to whole TAO (or whole alpha).

    The Taostats API returns balance/stake/emission/reserve amounts as RAO
    strings; divide by 1e9. Returns ``None`` if the value cannot be parsed.
    """
    parsed = _to_float(value)
    if parsed is None:
        return None
    return parsed / RAO_PER_TAO


def _to_int(value: Any) -> Optional[int]:
    """Coerce a Taostats integer-ish field to ``int``, or ``None``."""
    parsed = _to_float(value)
    if parsed is None:
        return None
    return int(parsed)


# Any TAO-denominated amount above this must actually be RAO: total TAO supply
# is capped at 21M (2.1e7), so a genuine TAO figure can never reach 3e7. The
# threshold sits just above the supply bound to catch as many RAO values as the
# magnitude heuristic possibly can.
_RAO_HEURISTIC_THRESHOLD = 3.0e7


def _amount_maybe_rao(value: Any) -> Optional[float]:
    """Parse an amount whose denomination (TAO vs RAO) is ambiguous in docs.

    Taostats documents most amounts as RAO strings but some aggregate figures
    (e.g. pool ``market_cap``) are ambiguous. Use the supply bound: values
    above :data:`_RAO_HEURISTIC_THRESHOLD` (just above the 21M-TAO supply cap)
    cannot be TAO, so treat them as RAO and divide by 1e9; smaller values are
    passed through as TAO.

    Remaining blind spot: a genuine RAO value *below* the threshold (i.e. below
    3e7 RAO = 0.03 TAO of dust) cannot be distinguished from a legitimate small
    TAO amount and is passed through unconverted. The resulting error is
    therefore bounded at 0.03 TAO -- a magnitude heuristic cannot disambiguate
    sub-dust RAO from real TAO; authoritative resolution requires the API to
    document the field's unit.
    """
    parsed = _to_float(value)
    if parsed is None:
        return None
    if abs(parsed) > _RAO_HEURISTIC_THRESHOLD:
        return parsed / RAO_PER_TAO
    return parsed


def _to_bool(value: Any) -> Optional[bool]:
    """Coerce a Taostats boolean-ish field to ``bool``, or ``None``."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "1", "yes"):
            return True
        if text in ("false", "0", "no"):
            return False
        return None
    if isinstance(value, (int, float)):
        return bool(value)
    return None


def _ss58(value: Any) -> Optional[str]:
    """Extract an ss58 address from a Taostats account field.

    Account/coldkey/hotkey fields come back either as a bare ss58 string or as
    a nested object ``{"ss58": "...", "hex": "..."}``. Normalize to the ss58
    string.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        ss58 = value.get("ss58")
        if isinstance(ss58, str) and ss58:
            return ss58
    return None


def _name_field(value: Any) -> Optional[str]:
    """Normalize an optional string label, treating empty strings as ``None``."""
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return None


# --- per-response-type parse helpers (unit-testable without HTTP) ----------- #


def parse_tao_price(item: dict) -> TaoPrice:
    """Parse one item from ``/api/price/latest/v1`` into a :class:`TaoPrice`.

    ``price`` is TAO/USD (whole-token units, NOT RAO).
    """
    usd = _to_float(item.get("price"))
    timestamp = (
        item.get("last_updated")
        or item.get("updated_at")
        or item.get("timestamp")
        or ""
    )
    return TaoPrice(usd=usd if usd is not None else 0.0, timestamp=str(timestamp))


def parse_pool(item: dict) -> Pool:
    """Parse one item from ``/api/dtao/pool/latest/v1`` into a :class:`Pool`.

    ``price`` is alpha price in TAO (already human-readable, NOT divided).
    Reserve fields (``total_tao``, ``alpha_in_pool``) are RAO -> divide by 1e9.
    ``market_cap`` denomination is ambiguous in the docs (TAO vs RAO), so it
    goes through the supply-bound heuristic in :func:`_amount_maybe_rao`.
    """
    return Pool(
        netuid=_to_int(item.get("netuid")) or 0,
        name=_name_field(item.get("name")),
        price_tao=_to_float(item.get("price")) or 0.0,
        market_cap_tao=_amount_maybe_rao(item.get("market_cap")),
        tao_in=rao_to_tao(item.get("total_tao")),
        alpha_in=rao_to_tao(item.get("alpha_in_pool")),
    )


def parse_stake_position(item: dict) -> StakePosition:
    """Parse one item from ``/api/dtao/stake_balance/latest/v1``.

    ``balance`` is the alpha amount in RAO -> divide by 1e9 for whole alpha.
    ``balance_as_tao`` is the TAO-equivalent value of that alpha (already in TAO
    terms per docs).
    """
    return StakePosition(
        coldkey=_ss58(item.get("coldkey")) or "",
        hotkey=_ss58(item.get("hotkey")) or "",
        netuid=_to_int(item.get("netuid")) or 0,
        alpha_staked=rao_to_tao(item.get("balance")) or 0.0,
        value_tao=_to_float(item.get("balance_as_tao")),
    )


def parse_subnet_info(item: dict) -> SubnetInfo:
    """Parse one item from ``/api/subnet/latest/v1`` into a :class:`SubnetInfo`.

    ``emission`` is RAO; when the API does not provide a ready percentage the
    raw TAO emission is parsed here and normalized to a share-of-total
    percentage by :meth:`TaostatsClient.get_subnets` (per-item parsing cannot
    see the network total). The subnet/latest endpoint carries hyperparams
    (``max_validators``, burn bounds) but typically not price/market cap —
    those live in the pool endpoint and are merged in by the scanner.
    ``min_burn`` (RAO) is used as the registration-cost proxy when present.
    """
    emission_pct = _to_float(item.get("emission_pct"))
    if emission_pct is None:
        # Raw RAO emission -> TAO; normalized to a percentage in get_subnets.
        emission_pct = rao_to_tao(item.get("emission"))

    registration_cost = rao_to_tao(item.get("registration_cost"))
    if registration_cost is None:
        registration_cost = rao_to_tao(item.get("min_burn"))

    return SubnetInfo(
        netuid=_to_int(item.get("netuid")) or 0,
        name=_name_field(item.get("name")),
        emission_pct=emission_pct,
        price_tao=_to_float(item.get("price")),
        market_cap_tao=_amount_maybe_rao(item.get("market_cap")),
        n_validators=_to_int(item.get("max_validators")),
        n_miners=_to_int(item.get("n_miners")),
        registration_cost_tao=registration_cost,
    )


def normalize_emission_shares(subnets: list[SubnetInfo]) -> list[SubnetInfo]:
    """Rescale raw per-subnet emission *amounts* into share-of-total percentages.

    Call this ONLY when the API did not expose a native ``emission_pct`` and the
    values therefore came from the raw RAO ``emission`` field (parsed as TAO by
    :func:`parse_subnet_info`). Each non-``None`` value is divided by the sum
    across the supplied list and multiplied by 100, so the returned list sums to
    100. No-op when no emission data is present (sum <= 0).

    Important: the shares are relative to the FETCHED set, not the whole network.
    A truncated or partial list (pagination cap, a transient short page, a single
    extra/missing subnet) yields shares of that subset only -- the percentages
    describe the rows you actually have, not the global emission distribution.
    Do NOT pass values the API already delivered as true percentages: rescaling
    those would corrupt correct data (see :meth:`TaostatsClient.get_subnets`,
    which gates this call on emission provenance).
    """
    total = sum(s.emission_pct for s in subnets if s.emission_pct is not None)
    if total <= 0:
        return subnets
    return [
        s if s.emission_pct is None
        else s.model_copy(update={"emission_pct": s.emission_pct / total * 100.0})
        for s in subnets
    ]


def parse_validator_info(item: dict) -> ValidatorInfo:
    """Parse one item from ``/api/metagraph/latest/v1`` into a
    :class:`ValidatorInfo`.

    In the dTAO era a neuron's per-subnet ``stake`` is that subnet's alpha in
    alpha-RAO -> divide by 1e9. ``validator_trust`` is the vtrust decimal [0,1].
    """
    return ValidatorInfo(
        hotkey=_ss58(item.get("hotkey")) or "",
        netuid=_to_int(item.get("netuid")) or 0,
        stake_tao=rao_to_tao(item.get("stake")) or 0.0,
        vtrust=_to_float(item.get("validator_trust")),
        active=_to_bool(item.get("active")),
    )


# --------------------------------------------------------------------------- #
# Rate limiter
# --------------------------------------------------------------------------- #


class RateLimiter:
    """Thread-safe blocking token-bucket rate limiter.

    Allows up to ``calls_per_min`` acquisitions within any rolling 60-second
    window; once the bucket is empty, :meth:`acquire` blocks (sleeps) until a
    token regenerates. The clock and sleep functions are injectable so tests can
    drive the limiter with a fake clock and assert blocking behavior without
    real wall-clock delays.

    A :class:`threading.Lock` guards the refill / check / decrement sequence so
    the limiter never over-admits when shared across threads (e.g. the web
    dashboard runs its synchronous route handlers in a threadpool and shares one
    client, hence one limiter). The lock is held only while inspecting/updating
    the bucket; the blocking sleep happens OUTSIDE the lock so waiters do not
    serialize, and the bucket is re-checked under the lock after each sleep.

    Args:
        calls_per_min: Maximum number of calls permitted per 60-second window.
        clock: Monotonic time source returning seconds (default
            :func:`time.monotonic`).
        sleep: Sleep function taking seconds (default :func:`time.sleep`).
    """

    def __init__(
        self,
        calls_per_min: int,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if calls_per_min <= 0:
            raise ValueError("calls_per_min must be positive")
        self.calls_per_min = calls_per_min
        self._clock = clock
        self._sleep = sleep
        self._capacity = float(calls_per_min)
        #: Tokens regenerated per second.
        self._refill_rate = calls_per_min / 60.0
        self._tokens = float(calls_per_min)
        self._last = clock()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        """Add tokens accrued since the last update, capped at capacity.

        Callers must hold :attr:`_lock`.
        """
        now = self._clock()
        elapsed = now - self._last
        if elapsed > 0:
            self._tokens = min(self._capacity, self._tokens + elapsed * self._refill_rate)
            self._last = now

    def acquire(self) -> None:
        """Acquire one token, blocking (sleeping) until one is available.

        Thread-safe: the refill / check / decrement is atomic under the lock,
        and the sleep is performed without the lock held so a waiting thread
        does not block other threads from making progress. After sleeping the
        bucket is re-checked under the lock, so two threads can never both see a
        token and decrement past empty.
        """
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                needed = 1.0 - self._tokens
                wait = needed / self._refill_rate
            # Sleep OUTSIDE the lock, then loop to re-check under the lock.
            if wait > 0:
                logger.debug("Rate limit reached; sleeping %.3fs", wait)
                self._sleep(wait)


# --------------------------------------------------------------------------- #
# HTTP client
# --------------------------------------------------------------------------- #


class TaostatsClient:
    """HTTP client for the Taostats API.

    All requests flow through :meth:`_get`, which attaches the auth header,
    throttles via the :class:`RateLimiter`, parses the standard
    ``{pagination, data}`` envelope, and raises :class:`TaostatsError` on
    non-2xx responses. Amounts are normalized to whole TAO / alpha at this
    boundary.

    Args:
        api_key: Raw Taostats API key. Sent verbatim in the ``Authorization``
            header with NO ``Bearer`` prefix (per the official SDK).
        base_url: API base URL.
        rate_limit_per_min: Calls permitted per minute (free tier is 5).
        timeout: Per-request timeout in seconds.
        client: Optional preconfigured :class:`httpx.Client` (mainly for tests).
        rate_limiter: Optional preconfigured :class:`RateLimiter` (for tests).
        retry_sleep: Sleep function used between retries (default
            :func:`time.sleep`); injectable so tests can assert backoff without
            real delay.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        rate_limit_per_min: int = DEFAULT_RATE_LIMIT_PER_MIN,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        client: Optional[httpx.Client] = None,
        rate_limiter: Optional[RateLimiter] = None,
        retry_sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.rate_limit_per_min = rate_limit_per_min
        self._limiter = rate_limiter or RateLimiter(rate_limit_per_min)
        self._retry_sleep = retry_sleep
        # Auth: raw key, NO 'Bearer' prefix (confirmed authoritative by the
        # official ts-sdk and the 401 troubleshooting note). accept: json.
        headers = {
            "Authorization": api_key,
            "accept": "application/json",
        }
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=self.base_url,
            headers=headers,
            timeout=timeout,
        )

    # -- low-level request ---------------------------------------------------- #

    def _get(self, path: str, params: dict) -> dict:
        """Perform a throttled GET (with retries) and return the parsed body.

        Transport/timeout errors are normalized into ``TaostatsError(0, ...)``
        so callers only ever have to catch a single exception type. A 429 or a
        5xx response is retried up to :data:`MAX_RETRIES` times: the wait honors
        an integer ``Retry-After`` header (capped at
        :data:`MAX_RETRY_AFTER_SECONDS`) and otherwise falls back to
        :data:`RETRY_BACKOFF_SECONDS` (2s then 4s). The rate limiter is acquired
        before EVERY attempt, including retries.

        Args:
            path: API path beginning with ``/`` (e.g. ``/api/price/latest/v1``).
            params: Query parameters.

        Returns:
            The parsed JSON response body (the full envelope, including
            ``pagination`` and ``data`` where present).

        Raises:
            TaostatsError: On a non-2xx response (after exhausting retries) or a
                transport/timeout failure (status 0).
        """
        for attempt in range(MAX_RETRIES + 1):
            # Throttle before every attempt, retries included, so a retry never
            # bypasses the rate limit.
            self._limiter.acquire()
            logger.debug("GET %s params=%s (attempt %d)", path, params, attempt + 1)
            try:
                response = self._client.get(path, params=params)
            except httpx.HTTPError as exc:
                # Transport/timeout/connection error: normalize to status 0 so
                # callers catch one type. Not retried (the rate limiter is the
                # primary guard and these are not the documented retry cases).
                logger.warning("Taostats API %s transport error: %s", path, exc)
                raise TaostatsError(0, str(exc)) from exc

            status = response.status_code
            if 200 <= status < 300:
                return self._safe_body(response) or {}

            body = self._safe_body(response)
            retryable = status == 429 or 500 <= status < 600
            if retryable and attempt < MAX_RETRIES:
                wait = self._retry_wait(response, attempt)
                logger.warning(
                    "Taostats API %s -> %s; retrying in %.1fs (attempt %d/%d)",
                    path, status, wait, attempt + 1, MAX_RETRIES,
                )
                if wait > 0:
                    self._retry_sleep(wait)
                continue

            logger.warning("Taostats API %s -> %s", path, status)
            raise TaostatsError(status, body)

        # Unreachable: the loop either returns, raises, or continues to the last
        # attempt which raises. Present only to satisfy static analysis.
        raise TaostatsError(0, "request retries exhausted")  # pragma: no cover

    @staticmethod
    def _retry_wait(response: httpx.Response, attempt: int) -> float:
        """Compute the wait before a retry, honoring ``Retry-After``.

        A valid integer ``Retry-After`` (seconds) wins, capped at
        :data:`MAX_RETRY_AFTER_SECONDS`; otherwise fall back to the per-attempt
        :data:`RETRY_BACKOFF_SECONDS`.
        """
        retry_after = response.headers.get("Retry-After")
        if retry_after is not None:
            text = retry_after.strip()
            if text.isdigit():
                return float(min(int(text), MAX_RETRY_AFTER_SECONDS))
        index = min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)
        return RETRY_BACKOFF_SECONDS[index]

    @staticmethod
    def _safe_body(response: httpx.Response) -> Any:
        """Parse the response body as JSON, falling back to raw text."""
        try:
            return response.json()
        except (ValueError, httpx.DecodingError):
            return response.text

    def _get_data_list(self, path: str, params: dict) -> list[dict]:
        """GET a list endpoint and follow pagination, returning all data rows.

        The standard envelope is ``{"pagination": {...}, "data": [...]}``. Pages
        are followed via ``pagination.next_page`` until it is ``null`` (or the
        :data:`MAX_PAGES` safety cap is hit). A bare list response (no envelope)
        and a single ``{"data": {...}}`` object are both handled defensively.
        """
        results: list[dict] = []
        query = dict(params)
        query.setdefault("limit", DEFAULT_PAGE_LIMIT)
        query.setdefault("page", 1)

        next_page: Optional[int] = None
        for _ in range(MAX_PAGES):
            body = self._get(path, query)
            results.extend(self._extract_rows(body))

            next_page = self._next_page(body)
            if next_page is None:
                break
            query["page"] = next_page
        else:
            # Loop ran the full MAX_PAGES without next_page becoming None: the
            # remaining rows are silently dropped, so make the truncation
            # visible and name the endpoint.
            if next_page is not None:
                logger.warning(
                    "Pagination truncated at MAX_PAGES=%d for %s "
                    "(next_page=%s still pending); results may be incomplete.",
                    MAX_PAGES, path, next_page,
                )

        return results

    @staticmethod
    def _extract_rows(body: Any) -> list[dict]:
        """Pull the list of row dicts out of a (possibly enveloped) body."""
        if isinstance(body, list):
            return [row for row in body if isinstance(row, dict)]
        if isinstance(body, dict):
            data = body.get("data")
            if isinstance(data, list):
                return [row for row in data if isinstance(row, dict)]
            if isinstance(data, dict):
                return [data]
        return []

    @staticmethod
    def _next_page(body: Any) -> Optional[int]:
        """Return the next page number from the envelope, or ``None`` at the end.

        Wire pagination fields are ``current_page``, ``per_page``,
        ``total_items``, ``total_pages``, ``next_page``, ``prev_page``. We follow
        ``next_page`` until it is ``null``.
        """
        if not isinstance(body, dict):
            return None
        pagination = body.get("pagination")
        if not isinstance(pagination, dict):
            return None
        next_page = pagination.get("next_page")
        if isinstance(next_page, bool):
            return None
        if isinstance(next_page, (int, float)):
            return int(next_page)
        if isinstance(next_page, str) and next_page.strip().isdigit():
            return int(next_page)
        return None

    # -- public API ----------------------------------------------------------- #

    def get_tao_price(self) -> TaoPrice:
        """Return the current TAO/USD price."""
        body = self._get(ENDPOINTS["tao_price"], {"asset": "tao"})
        rows = self._extract_rows(body)
        if not rows:
            raise TaostatsError(502, "empty TAO price response")
        return parse_tao_price(rows[0])

    def get_pools(self) -> list[Pool]:
        """Return the latest dTAO pool state for all subnets.

        Sorted client-side by netuid; the ``order`` query param is not sent
        because its accepted values are not consistently documented.
        """
        rows = self._get_data_list(ENDPOINTS["pools"], {})
        return sorted((parse_pool(row) for row in rows), key=lambda p: p.netuid)

    def get_stake_balances(self, coldkey: str) -> list[StakePosition]:
        """Return the per-(hotkey, netuid) alpha stake positions for a coldkey.

        Args:
            coldkey: ss58 coldkey address.
        """
        rows = self._get_data_list(
            ENDPOINTS["stake_balances"], {"coldkey": coldkey}
        )
        return [parse_stake_position(row) for row in rows]

    def get_subnets(self) -> list[SubnetInfo]:
        """Return the list of subnets with hyperparameter metadata.

        Sorted client-side by netuid (the ``order`` query param is not sent
        because its accepted values are not consistently documented).

        Emission normalization is provenance-gated: if ANY raw row carries a
        native ``emission_pct`` field, the API already speaks percentages and we
        leave them untouched (rescaling true percentages would corrupt them,
        and would also drift a subnet's reported share whenever the fetched set
        changes). Only when NO row provides a native percentage -- i.e.
        :func:`parse_subnet_info` fell back to the raw RAO ``emission`` amount --
        do we rescale via :func:`normalize_emission_shares`.
        """
        rows = self._get_data_list(ENDPOINTS["subnets"], {})
        has_native_pct = any(row.get("emission_pct") is not None for row in rows)
        subnets = sorted(
            (parse_subnet_info(row) for row in rows), key=lambda s: s.netuid
        )
        if has_native_pct:
            # API delivered true percentages; do not rescale.
            return subnets
        # Raw-amount fallback: rescale amounts into shares of the fetched set.
        return normalize_emission_shares(subnets)

    def get_validators(self, netuid: int) -> list[ValidatorInfo]:
        """Return the validators (permitted neurons) for a single subnet.

        Args:
            netuid: Subnet id (required by the metagraph endpoint).
        """
        rows = self._get_data_list(
            ENDPOINTS["validators"],
            {"netuid": netuid, "validator_permit": "true"},
        )
        return [parse_validator_info(row) for row in rows]

    # -- lifecycle ------------------------------------------------------------ #

    def close(self) -> None:
        """Close the underlying HTTP client if this instance owns it."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "TaostatsClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# --------------------------------------------------------------------------- #
# Mock client (no network)
# --------------------------------------------------------------------------- #


class MockTaostatsClient:
    """Deterministic, no-network stand-in for :class:`TaostatsClient`.

    Exposes the same five public methods backed by static fixtures covering
    netuids 1 (apex), 4 (targon), 8 (ptn), and 64 (chutes). Provides one fixture
    coldkey (``5MockColdkey...``) with three stake positions and a TAO price of
    350.0 USD. Used by ``--mock`` and the test suite so that the entire tool
    works without an API key or network access.
    """

    #: The single fixture coldkey holding stake positions.
    COLDKEY = "5MockColdkey0000000000000000000000000000000000000000000"

    _HOTKEY_1 = "5MockHotkeyApex000000000000000000000000000000000000000"
    _HOTKEY_4 = "5MockHotkeyTargon0000000000000000000000000000000000000"
    _HOTKEY_64 = "5MockHotkeyChutes0000000000000000000000000000000000000"

    # Subnet fixtures: (netuid, name, price_tao, market_cap_tao, emission_pct).
    _SUBNETS: list[dict] = [
        {"netuid": 1, "name": "apex", "price": 0.0254, "market_cap": 124000.0,
         "emission_pct": 8.5, "max_validators": 64, "n_miners": 192,
         "registration_cost": 0.85},
        {"netuid": 4, "name": "targon", "price": 0.0182, "market_cap": 98000.0,
         "emission_pct": 6.2, "max_validators": 64, "n_miners": 200,
         "registration_cost": 0.72},
        {"netuid": 8, "name": "ptn", "price": 0.0410, "market_cap": 210000.0,
         "emission_pct": 11.0, "max_validators": 64, "n_miners": 256,
         "registration_cost": 1.10},
        {"netuid": 64, "name": "chutes", "price": 0.0333, "market_cap": 175000.0,
         "emission_pct": 9.4, "max_validators": 64, "n_miners": 220,
         "registration_cost": 0.95},
    ]

    def get_tao_price(self) -> TaoPrice:
        """Return a fixed TAO price of 350.0 USD."""
        return TaoPrice(usd=350.0, timestamp="2026-06-03T00:00:00Z")

    def get_pools(self) -> list[Pool]:
        """Return fixture pools for netuids 1, 4, 8, 64."""
        pools: list[Pool] = []
        for sub in self._SUBNETS:
            # Derive plausible reserves from price so portfolio math is stable.
            tao_in = round(float(sub["market_cap"]) * 0.1, 4)
            alpha_in = round(tao_in / float(sub["price"]), 4)
            pools.append(
                Pool(
                    netuid=int(sub["netuid"]),
                    name=str(sub["name"]),
                    price_tao=float(sub["price"]),
                    market_cap_tao=float(sub["market_cap"]),
                    tao_in=tao_in,
                    alpha_in=alpha_in,
                )
            )
        return pools

    def get_stake_balances(self, coldkey: str) -> list[StakePosition]:
        """Return three fixture positions for the fixture coldkey, else empty.

        Args:
            coldkey: ss58 coldkey address. Only :data:`COLDKEY` has positions.
        """
        if coldkey != self.COLDKEY:
            return []
        prices = {sub["netuid"]: float(sub["price"]) for sub in self._SUBNETS}
        specs = [
            (self._HOTKEY_1, 1, 1000.0),
            (self._HOTKEY_4, 4, 2500.0),
            (self._HOTKEY_64, 64, 500.0),
        ]
        positions: list[StakePosition] = []
        for hotkey, netuid, alpha in specs:
            positions.append(
                StakePosition(
                    coldkey=coldkey,
                    hotkey=hotkey,
                    netuid=netuid,
                    alpha_staked=alpha,
                    value_tao=round(alpha * prices[netuid], 6),
                )
            )
        return positions

    def get_subnets(self) -> list[SubnetInfo]:
        """Return fixture subnet metadata for netuids 1, 4, 8, 64.

        The fixtures carry native ``emission_pct`` values, so this mirrors the
        real client's native-percentage case: provenance-gated normalization in
        :meth:`TaostatsClient.get_subnets` leaves native percentages untouched,
        and so these fixtures stay un-normalized (they intentionally do NOT sum
        to 100).
        """
        subnets: list[SubnetInfo] = []
        for sub in self._SUBNETS:
            subnets.append(
                SubnetInfo(
                    netuid=int(sub["netuid"]),
                    name=str(sub["name"]),
                    emission_pct=float(sub["emission_pct"]),
                    price_tao=float(sub["price"]),
                    market_cap_tao=float(sub["market_cap"]),
                    n_validators=int(sub["max_validators"]),
                    n_miners=int(sub["n_miners"]),
                    registration_cost_tao=float(sub["registration_cost"]),
                )
            )
        return subnets

    def get_validators(self, netuid: int) -> list[ValidatorInfo]:
        """Return deterministic fixture validators for the given subnet.

        netuid 1 is intentionally concentrated (one dominant validator) while
        the other subnets are more evenly distributed, so the scanner's
        concentration penalty has a clear test target.
        """
        if netuid == 1:
            # Concentrated: top validator dwarfs the rest.
            stakes = [50000.0, 3000.0, 2500.0, 2000.0, 1500.0, 1000.0]
        elif netuid == 4:
            stakes = [12000.0, 11000.0, 10000.0, 9500.0, 9000.0, 8500.0, 8000.0]
        elif netuid == 8:
            stakes = [9000.0, 8800.0, 8600.0, 8400.0, 8200.0, 8000.0, 7800.0, 7600.0]
        elif netuid == 64:
            stakes = [7000.0, 6800.0, 6600.0, 6400.0, 6200.0, 6000.0]
        else:
            stakes = []

        validators: list[ValidatorInfo] = []
        for index, stake in enumerate(stakes):
            hotkey = f"5MockValidator{netuid:02d}_{index:02d}".ljust(48, "0")
            validators.append(
                ValidatorInfo(
                    hotkey=hotkey,
                    netuid=netuid,
                    stake_tao=stake,
                    vtrust=round(0.99 - index * 0.01, 4),
                    active=True,
                )
            )
        return validators

    def close(self) -> None:
        """No-op; the mock holds no network resources."""

    def __enter__(self) -> "MockTaostatsClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #


def make_client(api_key: Optional[str], mock: bool):
    """Build a Taostats client.

    Returns a :class:`MockTaostatsClient` when ``mock`` is set or when no API
    key is available (so every command works offline). Otherwise returns a live
    :class:`TaostatsClient`.

    Args:
        api_key: Raw Taostats API key, or ``None``.
        mock: Force the mock client.

    Returns:
        A :class:`MockTaostatsClient` or :class:`TaostatsClient`.
    """
    if mock or not api_key:
        if not mock:
            logger.info("No API key provided; falling back to MockTaostatsClient.")
        return MockTaostatsClient()
    return TaostatsClient(api_key)
