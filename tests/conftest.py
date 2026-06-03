"""Shared pytest fixtures for the tao-sentinel test suite.

These fixtures provide the deterministic, no-network building blocks the rest of
the suite relies on: the :class:`~tao_sentinel.api.MockTaostatsClient` (and its
fixture coldkey) plus a deterministic fake clock/sleep pair for exercising the
rate limiter without any real wall-clock delay.

No test in this suite performs any network I/O; everything is driven through the
mock client or hand-built snapshot dicts.
"""

from __future__ import annotations

import pytest

from tao_sentinel.api import MockTaostatsClient


@pytest.fixture()
def mock_client() -> MockTaostatsClient:
    """Return a fresh deterministic mock Taostats client (no network)."""
    return MockTaostatsClient()


@pytest.fixture()
def mock_coldkey() -> str:
    """Return the fixture coldkey that the mock client holds positions for."""
    return MockTaostatsClient.COLDKEY


class FakeClock:
    """A manually-advanced monotonic clock for deterministic timing tests.

    The clock only moves when :meth:`advance` is called (or when the paired
    :class:`FakeSleep` is invoked), so the rate limiter can be driven through its
    blocking path without any real wall-clock delay.
    """

    def __init__(self, start: float = 0.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        """Return the current fake time in seconds."""
        return self.now

    def advance(self, seconds: float) -> None:
        """Advance the fake clock by ``seconds``."""
        self.now += float(seconds)


class FakeSleep:
    """A sleep stand-in that advances a :class:`FakeClock` instead of blocking.

    Records every requested sleep duration in :attr:`calls` so tests can assert
    that (and for how long) the rate limiter blocked.
    """

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        """Record the sleep and advance the fake clock by ``seconds``."""
        self.calls.append(seconds)
        self._clock.advance(seconds)

    @property
    def total_slept(self) -> float:
        """Total fake seconds slept across all calls."""
        return sum(self.calls)


@pytest.fixture()
def fake_clock() -> FakeClock:
    """Return a manually-advanced fake monotonic clock starting at 0."""
    return FakeClock()


@pytest.fixture()
def fake_sleep(fake_clock: FakeClock) -> FakeSleep:
    """Return a fake sleep that advances ``fake_clock`` and records durations."""
    return FakeSleep(fake_clock)
