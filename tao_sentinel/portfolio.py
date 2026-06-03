"""Portfolio valuation for a Bittensor coldkey.

Joins a coldkey's per-(hotkey, netuid) alpha stake positions with current
dTAO pool prices to value each holding in TAO, then applies the TAO/USD
spot price for a USD total. Each position's value prefers the API-provided
TAO valuation when present, falling back to ``alpha_staked * pool price``
only when the API omitted it; a position the API did not value and whose
netuid has no known pool price is left unknown (``value_tao`` is ``None``)
and excluded from the totals.
"""

from __future__ import annotations

import logging
from typing import Optional

from .models import Pool, Portfolio, StakePosition

logger = logging.getLogger(__name__)


class PortfolioTracker:
    """Builds a valued :class:`Portfolio` for a coldkey from a Taostats client.

    The tracker is stateless apart from the injected client. Each
    :meth:`get_portfolio` issues up to three logical client calls: stake
    balances for the coldkey, the full pool list (for price lookup), and the
    TAO/USD price. Each list fetch is paginated, so a single logical call may
    map to one or more HTTP requests (``ceil(rows / page_limit)``); callers
    that already hold the pool list can pass it via ``pools`` to skip the
    pool fetch entirely.
    """

    def __init__(self, client: object) -> None:
        """Store the Taostats client (real or mock) used for lookups.

        Args:
            client: Any object exposing ``get_stake_balances``, ``get_pools``
                and ``get_tao_price`` per the client contract.
        """
        self.client = client

    def get_portfolio(
        self, coldkey: str, pools: Optional[list[Pool]] = None
    ) -> Portfolio:
        """Return a fully valued portfolio for ``coldkey``.

        Each position's ``value_tao`` follows this precedence:

        1. The API-provided value (``StakePosition.value_tao``, parsed from
           the Taostats ``balance_as_tao`` field) when it is not ``None``.
           This is the authoritative current valuation and is used even for
           netuids that have no dTAO pool entry -- most notably root /
           netuid 0, which carries the bulk of typical TAO stake. Such
           positions are therefore INCLUDED in the total.
        2. Otherwise ``alpha_staked * pool.price_tao`` when a pool price is
           known for the position's netuid.
        3. Otherwise ``None`` (unvalued) -- the position is excluded from
           ``total_value_tao``.

        ``total_value_tao`` sums every position with a non-``None`` value.
        The USD total is the TAO total multiplied by the current TAO/USD
        spot price.

        Args:
            coldkey: The ss58 coldkey to value.
            pools: A pre-fetched pool list to reuse so callers can share a
                single fetch across the scanner and tracker. When ``None``
                the pool list is fetched from the client.

        Returns:
            A :class:`Portfolio` with priced positions and aggregated totals.
        """
        positions = self.client.get_stake_balances(coldkey)
        if pools is None:
            pools = self.client.get_pools()

        # netuid -> price in TAO; only pools that actually carry a price.
        price_by_netuid: dict[int, float] = {
            pool.netuid: pool.price_tao
            for pool in pools
            if pool.price_tao is not None
        }

        valued_positions: list[StakePosition] = []
        total_value_tao = 0.0
        for position in positions:
            # Precedence: API-provided value, then pool-price recompute, then
            # unvalued. The API value wins so pool-less positions (root /
            # netuid 0) the API already valued are not dropped from the total.
            if position.value_tao is not None:
                value_tao = position.value_tao
            else:
                price_tao = price_by_netuid.get(position.netuid)
                if price_tao is None:
                    logger.warning(
                        "No API value and no pool price for netuid %s; "
                        "position on hotkey %s excluded from portfolio total.",
                        position.netuid,
                        position.hotkey,
                    )
                    value_tao = None
                else:
                    value_tao = position.alpha_staked * price_tao

            if value_tao is not None:
                total_value_tao += value_tao

            valued_positions.append(
                StakePosition(
                    coldkey=position.coldkey,
                    hotkey=position.hotkey,
                    netuid=position.netuid,
                    alpha_staked=position.alpha_staked,
                    value_tao=value_tao,
                )
            )

        tao_price = self.client.get_tao_price()
        tao_price_usd = tao_price.usd if tao_price is not None else None
        total_value_usd = (
            total_value_tao * tao_price_usd if tao_price_usd is not None else None
        )

        return Portfolio(
            coldkey=coldkey,
            positions=valued_positions,
            total_value_tao=total_value_tao,
            total_value_usd=total_value_usd,
            tao_price_usd=tao_price_usd,
        )
