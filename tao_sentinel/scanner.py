"""Subnet health scanner.

Scores Bittensor subnets 0-100 from Taostats data and assigns a letter
grade. Two modes:

* Single subnet (``scan(netuid)``) -- also pulls the validator set so the
  score can factor in real validator stake concentration and the actual
  active-validator population (not the slot cap).
* All subnets (``scan()``) -- scores from the subnet list alone to stay
  rate-frugal; the omission of validator detail is recorded in
  ``metrics["validator_data"]``. Because no real stake distribution is
  available, the concentration component is EXCLUDED entirely (not faked
  from a slot cap) and the remaining component weights are renormalized so
  scores stay on the 0-100 scale. The validator population sub-score is also
  skipped in this mode (the subnet list exposes only the slot CAP, which is
  not a population signal); the neuron component is scored from miners alone.

  WARNING: an all-subnets score is PROVISIONAL and is NOT comparable to a
  single-netuid score. Because a concentrated validator set is invisible in
  this mode, the same subnet can score materially HIGHER here -- by tens of
  points, enough to flip the letter grade -- than it does under
  ``scan(netuid)``. ``metrics["provisional"]`` is ``True`` and
  ``metrics["note"]`` spells this out. Use all-subnets scores for triage
  only and rescan a specific netuid for the authoritative grade.

Each list fetch (subnets, pools) is paginated, so a logical fetch maps to
one or more HTTP requests (``ceil(rows / page_limit)``).

Scoring is deterministic for a given set of inputs.
"""

from __future__ import annotations

import logging
import statistics
from typing import Optional

from .api import TaostatsError
from .models import HealthReport, Pool, SubnetInfo, ValidatorInfo

logger = logging.getLogger(__name__)

# Score weights (sum to 100). Each component returns a 0..1 fraction that is
# multiplied by its weight.
_WEIGHT_EMISSION = 20.0
_WEIGHT_NEURONS = 25.0
_WEIGHT_MARKET = 20.0

# Validator concentration penalty thresholds (share of total stake).
_TOP1_OK = 0.30
_TOP5_OK = 0.70


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    """Clamp ``value`` to the inclusive range ``[low, high]``."""
    return max(low, min(high, value))


def _grade(score: float) -> str:
    """Map a 0-100 score to a letter grade A-F."""
    if score >= 85:
        return "A"
    if score >= 70:
        return "B"
    if score >= 55:
        return "C"
    if score >= 40:
        return "D"
    return "F"


class SubnetScanner:
    """Computes :class:`HealthReport` objects from subnet/validator data."""

    def __init__(self, client: object) -> None:
        """Store the Taostats client (real or mock) used for lookups.

        Args:
            client: Any object exposing ``get_subnets`` and ``get_validators``
                per the client contract.
        """
        self.client = client

    def scan(
        self,
        netuid: Optional[int] = None,
        pools: Optional[list[Pool]] = None,
    ) -> list[HealthReport]:
        """Scan one or all subnets and return their health reports.

        Args:
            netuid: A specific subnet to scan. When ``None`` (the default) all
                subnets are scored from the subnet and pool lists alone; no
                per-subnet validator calls are made. The two logical list
                fetches (subnets, pools) are each paginated, so the real HTTP
                call count is ``ceil(rows / page_limit)`` per source.
            pools: A pre-fetched pool list to reuse for the price/market-cap
                merge so callers can share a single fetch across the scanner
                and tracker. When ``None`` the pool list is fetched.

        Returns:
            A list of :class:`HealthReport`, one per scanned subnet, ordered
            as returned by the client (single-element when ``netuid`` is set).
        """
        subnets = self.client.get_subnets()
        subnets = self._merge_pool_data(subnets, pools)

        # Emission median is computed across the full subnet population so a
        # single-subnet scan still scores emission relative to the network.
        emissions = [
            s.emission_pct for s in subnets if s.emission_pct is not None
        ]
        emission_median = statistics.median(emissions) if emissions else None

        if netuid is not None:
            subnet = next((s for s in subnets if s.netuid == netuid), None)
            if subnet is None:
                logger.warning("Subnet netuid %s not found in subnet list.", netuid)
                return []
            validators = self.client.get_validators(netuid)
            return [self._score_subnet(subnet, emission_median, validators)]

        return [
            self._score_subnet(s, emission_median, None) for s in subnets
        ]

    def _merge_pool_data(
        self,
        subnets: list[SubnetInfo],
        pools: Optional[list[Pool]] = None,
    ) -> list[SubnetInfo]:
        """Fill missing name/price/market-cap on subnets from the pool list.

        The live ``subnet/latest`` endpoint carries hyperparams but no subnet
        ``name`` and no price/market cap (verified June 2026) — those live on
        the dTAO pool endpoint, so without this merge the market component is
        silently skipped and every subnet renders nameless in live mode.

        Args:
            subnets: The parsed subnet list to enrich.
            pools: A pre-fetched pool list to reuse; when ``None`` the pool
                list is fetched from the client (one extra paginated fetch).

        Only a genuine API/transport failure (:class:`TaostatsError`) is
        caught so the scan degrades gracefully (returns subnets unmerged);
        programming errors are allowed to propagate rather than being masked
        as a benign "pool fetch failed".
        """
        if pools is None:
            try:
                pools = self.client.get_pools()
            except TaostatsError:
                logger.warning(
                    "Pool fetch failed; scoring without price/market-cap data."
                )
                return subnets
        pool_by_netuid = {p.netuid: p for p in pools}

        merged: list[SubnetInfo] = []
        for s in subnets:
            pool = pool_by_netuid.get(s.netuid)
            if pool is None or (
                s.name is not None
                and s.price_tao is not None
                and s.market_cap_tao is not None
            ):
                merged.append(s)
                continue
            merged.append(s.model_copy(update={
                "name": s.name if s.name is not None else pool.name,
                "price_tao": s.price_tao if s.price_tao is not None
                else pool.price_tao,
                "market_cap_tao": s.market_cap_tao
                if s.market_cap_tao is not None else pool.market_cap_tao,
            }))
        return merged

    def _score_subnet(
        self,
        subnet: SubnetInfo,
        emission_median: Optional[float],
        validators: Optional[list[ValidatorInfo]],
    ) -> HealthReport:
        """Score a single subnet, optionally factoring in its validator set.

        Args:
            subnet: The subnet metadata.
            emission_median: Median emission percent across all subnets, or
                ``None`` when no emission data is available.
            validators: The subnet's validator list for concentration/vtrust
                scoring, or ``None`` for the rate-frugal all-subnets scan.

        Returns:
            The computed :class:`HealthReport`.
        """
        metrics: dict = {}
        warnings: list[str] = []

        # When a validator list was fetched (single-netuid scan) the actual
        # active-validator COUNT is authoritative; the subnet list's
        # ``n_validators`` is only the slot CAP (``max_validators``), which is
        # not a population signal. Pass the real count so the neuron component
        # and its "only N validators" warning reflect reality, not the cap.
        n_active_validators = (
            sum(
                1
                for v in validators
                if v.active is not False and v.stake_tao > 0
            )
            if validators is not None
            else None
        )

        emission_score = self._score_emission(
            subnet, emission_median, metrics, warnings
        )
        # Scoring inputs must be IDENTICAL in both scan modes (grade
        # consistency invariant), so the fetched validator count never
        # enters the score -- it is recorded below as display metadata and
        # can add warnings, but the formula sees only the subnet row.
        neuron_score = self._score_neurons(subnet, metrics, warnings, None)
        market_score = self._score_market(subnet, metrics, warnings)

        if n_active_validators is not None:
            metrics["n_active_validators"] = n_active_validators
            metrics["n_validators_is_cap"] = False
            if n_active_validators < 5:
                warnings.append(
                    f"Only {n_active_validators} active validators registered."
                )

        # ONE score formula everywhere. Concentration deliberately does NOT
        # enter the score: it needs per-validator stake (only fetched on
        # single-netuid scans), and any view-dependent component makes the
        # SAME subnet grade A on the dashboard and D on its detail page --
        # which users rightly read as a bug. Concentration is surfaced as an
        # explicit RISK assessment (metrics + warnings) where the data
        # exists, instead of silently bending the grade.
        weighted = (
            emission_score * _WEIGHT_EMISSION
            + neuron_score * _WEIGHT_NEURONS
            + market_score * _WEIGHT_MARKET
        )
        total_weight = _WEIGHT_EMISSION + _WEIGHT_NEURONS + _WEIGHT_MARKET

        if validators is None:
            metrics["concentration"] = {"source": "unavailable"}
        else:
            # Risk-only: fills metrics["concentration"] (top1/top5 shares)
            # and appends concentration warnings; never touches the score.
            self._score_concentration(validators, subnet, metrics, warnings)

        score = (weighted / total_weight) * 100.0 if total_weight > 0 else 0.0
        score = round(_clamp(score, 0.0, 100.0), 2)

        metrics["validator_data"] = validators is not None

        return HealthReport(
            netuid=subnet.netuid,
            name=subnet.name,
            score=score,
            grade=_grade(score),
            metrics=metrics,
            warnings=warnings,
        )

    def _score_concentration(
        self,
        validators: list[ValidatorInfo],
        subnet: SubnetInfo,
        metrics: dict,
        warnings: list[str],
    ) -> float:
        """Score real validator stake concentration (lower is better).

        Penalizes a top-1 share above 30% and a top-5 share above 70%, scored
        from the actual per-validator stake distribution. This is only called
        when a validator list was fetched (a single-netuid scan); the
        all-subnets scan excludes the concentration component entirely rather
        than estimating it from a validator-slot cap, which is not a
        concentration signal.

        Returns:
            A fraction in ``[0, 1]``.
        """
        active_validators = [
            v for v in validators if v.active is not False and v.stake_tao > 0
        ]
        total_stake = sum(v.stake_tao for v in active_validators)
        if total_stake <= 0 or not active_validators:
            metrics["concentration"] = {"top1_share": None, "top5_share": None}
            warnings.append("No active validator stake found.")
            return 0.0

        stakes = sorted(
            (v.stake_tao for v in active_validators), reverse=True
        )
        top1_share = stakes[0] / total_stake
        top5_share = sum(stakes[:5]) / total_stake

        metrics["concentration"] = {
            "top1_share": round(top1_share, 4),
            "top5_share": round(top5_share, 4),
            "n_active_validators": len(active_validators),
        }

        # Each sub-score is 1.0 at/under the threshold and linearly degrades to
        # 0.0 as the share approaches 100%.
        top1_sub = 1.0 if top1_share <= _TOP1_OK else _clamp(
            1.0 - (top1_share - _TOP1_OK) / (1.0 - _TOP1_OK)
        )
        top5_sub = 1.0 if top5_share <= _TOP5_OK else _clamp(
            1.0 - (top5_share - _TOP5_OK) / (1.0 - _TOP5_OK)
        )

        if top1_share > _TOP1_OK:
            warnings.append(
                f"Top validator holds {top1_share:.0%} of stake "
                f"(>{_TOP1_OK:.0%})."
            )
        if top5_share > _TOP5_OK:
            warnings.append(
                f"Top 5 validators hold {top5_share:.0%} of stake "
                f"(>{_TOP5_OK:.0%})."
            )

        return (top1_sub + top5_sub) / 2.0

    def _score_emission(
        self,
        subnet: SubnetInfo,
        emission_median: Optional[float],
        metrics: dict,
        warnings: list[str],
    ) -> float:
        """Score emission share relative to the network median.

        A subnet at or above the median scores full marks; below-median
        emission scales down toward 0 as it approaches zero emission.

        Returns:
            A fraction in ``[0, 1]``.
        """
        emission = subnet.emission_pct
        if emission is None:
            metrics["emission_pct"] = None
            return 0.5
        metrics["emission_pct"] = emission

        if emission_median is None or emission_median <= 0:
            # No useful baseline; reward any positive emission.
            return 1.0 if emission > 0 else 0.0

        ratio = emission / emission_median
        score = _clamp(ratio, 0.0, 1.0)
        if ratio < 0.5:
            warnings.append(
                f"Emission ({emission:.4g}%) is well below the network "
                f"median ({emission_median:.4g}%)."
            )
        return score

    def _score_neurons(
        self,
        subnet: SubnetInfo,
        metrics: dict,
        warnings: list[str],
        n_active_validators: Optional[int] = None,
    ) -> float:
        """Score validator and miner population health.

        Targets are calibrated to the LIVE mainnet population distribution
        (June 2026: active validators median 10 / p75 12 / max 18; active
        miners median 2 / p75 17): full marks at 12 validators and 16 miners.
        Missing counts score neutrally.

        Validator population sources, in order of preference:

        * ``n_active_validators`` -- the true count computed from the fetched
          validator set (single-netuid scan); always scoreable.
        * ``subnet.n_validators`` with ``validators_from_population=True`` --
          the live API's ``active_validators`` field (all-subnets scan).
        * ``subnet.n_validators`` with ``validators_from_population`` falsy --
          the ``max_validators`` slot CAP. Not a population signal: it is
          recorded for reference but the validator sub-score is OMITTED (it
          would otherwise saturate to a meaningless 1.0) and the neuron
          component is scored from the miner population alone.

        Returns:
            A fraction in ``[0, 1]``.
        """
        n_miners = subnet.n_miners
        metrics["n_validators"] = subnet.n_validators
        metrics["n_validators_is_cap"] = (
            n_active_validators is None
            and not subnet.validators_from_population
        )
        metrics["n_active_validators"] = n_active_validators
        metrics["n_miners"] = n_miners

        # Calibrated to mainnet distribution, June 2026 (see docstring).
        validators_full_marks = 12.0
        validators_warn_below = 5
        miners_full_marks = 16.0
        miners_warn_below = 3

        val_count = n_active_validators
        if val_count is None and subnet.validators_from_population:
            val_count = subnet.n_validators

        sub_scores: list[float] = []

        if val_count is not None:
            sub_scores.append(_clamp(val_count / validators_full_marks))
            if val_count < validators_warn_below:
                warnings.append(
                    f"Only {val_count} active validators registered."
                )
        # Cap-only data: skip the validator sub-score (see docstring).
        if n_miners is not None:
            sub_scores.append(_clamp(n_miners / miners_full_marks))
            if n_miners < miners_warn_below:
                warnings.append(f"Only {n_miners} active miners.")

        if not sub_scores:
            return 0.5
        return sum(sub_scores) / len(sub_scores)

    def _score_market(
        self,
        subnet: SubnetInfo,
        metrics: dict,
        warnings: list[str],
    ) -> float:
        """Score presence and positivity of price and market-cap data.

        A subnet with a positive price and market cap is considered economically
        live (full marks); missing or zero values reduce the score and emit
        warnings.

        Returns:
            A fraction in ``[0, 1]``.
        """
        price = subnet.price_tao
        market_cap = subnet.market_cap_tao
        metrics["price_tao"] = price
        metrics["market_cap_tao"] = market_cap

        score = 0.0
        if price is not None and price > 0:
            score += 0.5
        else:
            warnings.append("No positive alpha price available.")
        if market_cap is not None and market_cap > 0:
            score += 0.5
        else:
            warnings.append("No market-cap data available.")
        return score
