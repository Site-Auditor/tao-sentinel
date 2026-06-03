"""Tests for :class:`~tao_sentinel.portfolio.PortfolioTracker`.

Covers the valuation join (alpha * pool price), the USD total via the TAO/USD
spot price, and the contract that a position whose netuid has no pool price is
left unvalued (``value_tao is None``) and excluded from the total. The mock
client supplies the happy path; a tiny in-test stub client supplies the
missing-price and empty cases.
"""

from __future__ import annotations

import pytest

from tao_sentinel.models import Pool, StakePosition, TaoPrice
from tao_sentinel.portfolio import PortfolioTracker


class StubClient:
    """Minimal client exposing only what PortfolioTracker needs.

    Lets a test pin exactly which positions, pools and TAO price are returned,
    so the valuation arithmetic (including the missing-pool branch) is isolated.
    """

    def __init__(
        self,
        positions: list[StakePosition],
        pools: list[Pool],
        tao_price: TaoPrice | None = TaoPrice(usd=350.0, timestamp="t"),
    ) -> None:
        self._positions = positions
        self._pools = pools
        self._tao_price = tao_price

    def get_stake_balances(self, coldkey: str) -> list[StakePosition]:
        return list(self._positions)

    def get_pools(self) -> list[Pool]:
        return list(self._pools)

    def get_tao_price(self) -> TaoPrice:
        return self._tao_price


# --------------------------------------------------------------------------- #
# Happy path via the mock client
# --------------------------------------------------------------------------- #


def test_portfolio_values_positions_against_mock_pools(mock_client, mock_coldkey):
    """Each position is valued at alpha * pool price and summed for the total."""
    tracker = PortfolioTracker(mock_client)
    portfolio = tracker.get_portfolio(mock_coldkey)

    assert portfolio.coldkey == mock_coldkey
    assert len(portfolio.positions) == 3

    prices = {p.netuid: p.price_tao for p in mock_client.get_pools()}
    expected_total = 0.0
    for position in portfolio.positions:
        expected = position.alpha_staked * prices[position.netuid]
        assert position.value_tao == pytest.approx(expected)
        expected_total += expected

    assert portfolio.total_value_tao == pytest.approx(expected_total)


def test_portfolio_usd_total_uses_tao_price(mock_client, mock_coldkey):
    """USD total is the TAO total scaled by the TAO/USD spot price (350.0)."""
    tracker = PortfolioTracker(mock_client)
    portfolio = tracker.get_portfolio(mock_coldkey)

    assert portfolio.tao_price_usd == pytest.approx(350.0)
    assert portfolio.total_value_usd == pytest.approx(
        portfolio.total_value_tao * 350.0
    )


def test_portfolio_unknown_coldkey_is_empty(mock_client):
    """A coldkey with no positions yields an empty, zero-valued portfolio."""
    tracker = PortfolioTracker(mock_client)
    portfolio = tracker.get_portfolio("5SomeOtherColdkey")

    assert portfolio.positions == []
    assert portfolio.total_value_tao == pytest.approx(0.0)
    assert portfolio.total_value_usd == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# Missing pool price
# --------------------------------------------------------------------------- #


def test_portfolio_excludes_position_with_missing_pool_price():
    """A position whose netuid has no pool price stays unvalued and untotaled."""
    positions = [
        StakePosition(coldkey="5C", hotkey="5H1", netuid=1, alpha_staked=100.0),
        StakePosition(coldkey="5C", hotkey="5H2", netuid=99, alpha_staked=500.0),
    ]
    pools = [Pool(netuid=1, name="apex", price_tao=0.02)]  # no pool for netuid 99
    tracker = PortfolioTracker(StubClient(positions, pools))

    portfolio = tracker.get_portfolio("5C")

    by_netuid = {p.netuid: p for p in portfolio.positions}
    assert by_netuid[1].value_tao == pytest.approx(2.0)  # 100 * 0.02
    assert by_netuid[99].value_tao is None  # excluded
    # Total reflects only the valued position.
    assert portfolio.total_value_tao == pytest.approx(2.0)
    assert portfolio.total_value_usd == pytest.approx(2.0 * 350.0)


def test_portfolio_all_positions_unpriced_totals_zero():
    """If no position can be priced, the TAO total is zero."""
    positions = [
        StakePosition(coldkey="5C", hotkey="5H", netuid=42, alpha_staked=10.0),
    ]
    tracker = PortfolioTracker(StubClient(positions, pools=[]))

    portfolio = tracker.get_portfolio("5C")

    assert portfolio.positions[0].value_tao is None
    assert portfolio.total_value_tao == pytest.approx(0.0)


def test_portfolio_preserves_position_identity():
    """Valued positions keep their coldkey/hotkey/netuid/alpha intact."""
    positions = [
        StakePosition(coldkey="5C", hotkey="5Hk", netuid=4, alpha_staked=2500.0),
    ]
    pools = [Pool(netuid=4, name="targon", price_tao=0.0182)]
    tracker = PortfolioTracker(StubClient(positions, pools))

    portfolio = tracker.get_portfolio("5C")
    pos = portfolio.positions[0]

    assert pos.coldkey == "5C"
    assert pos.hotkey == "5Hk"
    assert pos.netuid == 4
    assert pos.alpha_staked == pytest.approx(2500.0)
    assert pos.value_tao == pytest.approx(2500.0 * 0.0182)


# --------------------------------------------------------------------------- #
# Finding 8: API value precedence + pool-less (root / netuid 0) positions
# --------------------------------------------------------------------------- #


def test_portfolio_includes_poolless_netuid0_via_api_value():
    """A netuid-0/root position the API valued is counted even with no pool.

    Root stake has no dTAO pool entry, so the old pool-price recompute marked
    it unvalued and dropped it from the total. The API-provided ``value_tao``
    must take precedence so the position is valued and summed.
    """
    positions = [
        # Root position the API valued; there is no pool for netuid 0.
        StakePosition(
            coldkey="5C", hotkey="5Root", netuid=0,
            alpha_staked=500.0, value_tao=500.0,
        ),
        # A normal subnet position the API also valued.
        StakePosition(
            coldkey="5C", hotkey="5H1", netuid=1,
            alpha_staked=1000.0, value_tao=20.0,
        ),
    ]
    pools = [Pool(netuid=1, name="apex", price_tao=0.02)]  # no pool for netuid 0
    tracker = PortfolioTracker(StubClient(positions, pools))

    portfolio = tracker.get_portfolio("5C")

    by_netuid = {p.netuid: p for p in portfolio.positions}
    assert by_netuid[0].value_tao == pytest.approx(500.0)  # included, not None
    assert by_netuid[1].value_tao == pytest.approx(20.0)
    # Total is the API-authoritative sum, not the pool-price recompute.
    assert portfolio.total_value_tao == pytest.approx(520.0)
    assert portfolio.total_value_usd == pytest.approx(520.0 * 350.0)


def test_portfolio_api_value_takes_precedence_over_pool_price():
    """When the API supplied a value, it wins over alpha * pool price."""
    positions = [
        StakePosition(
            coldkey="5C", hotkey="5H1", netuid=1,
            alpha_staked=1000.0, value_tao=999.0,  # API value, deliberately
        ),
    ]
    # A pool exists and would recompute to 1000 * 0.02 = 20.0, but the API
    # value (999.0) must be used instead.
    pools = [Pool(netuid=1, name="apex", price_tao=0.02)]
    tracker = PortfolioTracker(StubClient(positions, pools))

    portfolio = tracker.get_portfolio("5C")

    assert portfolio.positions[0].value_tao == pytest.approx(999.0)
    assert portfolio.total_value_tao == pytest.approx(999.0)


def test_portfolio_falls_back_to_pool_price_when_api_value_absent():
    """With no API value, value_tao falls back to alpha * pool price."""
    positions = [
        StakePosition(
            coldkey="5C", hotkey="5H1", netuid=1,
            alpha_staked=1000.0, value_tao=None,  # API omitted the value
        ),
    ]
    pools = [Pool(netuid=1, name="apex", price_tao=0.02)]
    tracker = PortfolioTracker(StubClient(positions, pools))

    portfolio = tracker.get_portfolio("5C")

    assert portfolio.positions[0].value_tao == pytest.approx(20.0)  # 1000 * 0.02
    assert portfolio.total_value_tao == pytest.approx(20.0)


def test_portfolio_unvalued_when_no_api_value_and_no_pool():
    """No API value and no pool price -> value_tao None, excluded from total."""
    positions = [
        StakePosition(
            coldkey="5C", hotkey="5H1", netuid=42,
            alpha_staked=10.0, value_tao=None,
        ),
    ]
    tracker = PortfolioTracker(StubClient(positions, pools=[]))

    portfolio = tracker.get_portfolio("5C")

    assert portfolio.positions[0].value_tao is None
    assert portfolio.total_value_tao == pytest.approx(0.0)


def test_portfolio_accepts_prefetched_pools_without_fetching():
    """get_portfolio(pools=...) reuses the caller's list and skips get_pools."""

    class _NoPoolFetchClient(StubClient):
        def get_pools(self):  # pragma: no cover - must not run
            raise AssertionError("get_portfolio(pools=...) must not fetch pools")

    positions = [
        StakePosition(
            coldkey="5C", hotkey="5H1", netuid=1,
            alpha_staked=1000.0, value_tao=None,
        ),
    ]
    # Stub's own pools are unused; the pre-fetched list drives valuation.
    tracker = PortfolioTracker(_NoPoolFetchClient(positions, pools=[]))
    prefetched = [Pool(netuid=1, name="apex", price_tao=0.02)]

    portfolio = tracker.get_portfolio("5C", pools=prefetched)

    assert portfolio.positions[0].value_tao == pytest.approx(20.0)
    assert portfolio.total_value_tao == pytest.approx(20.0)
