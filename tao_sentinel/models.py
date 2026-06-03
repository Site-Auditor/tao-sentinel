"""Pydantic data models for tao-sentinel.

All monetary amounts in these models are expressed in human-readable units
(whole TAO or whole alpha tokens) as ``float`` values. They are NEVER raw RAO:
the Taostats API returns RAO strings, and the API client (``tao_sentinel.api``)
is responsible for dividing by ``1e9`` at its boundary before populating these
models. Optional fields default to ``None`` so that partial/sparse API
responses can still be represented.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class TaoPrice(BaseModel):
    """Spot price of TAO in USD.

    Attributes:
        usd: Price of one TAO in US dollars.
        timestamp: ISO-8601 timestamp string for when the price was observed.
    """

    usd: float
    timestamp: str


class Pool(BaseModel):
    """A dTAO liquidity-pool snapshot for a single subnet.

    All amounts are in whole TAO / whole alpha units (the client converts the
    RAO reserve fields from the API). ``price_tao`` is the alpha price denoted
    in TAO (the Taostats ``price`` field, which is already TAO-denominated).
    """

    netuid: int
    name: Optional[str] = None
    price_tao: float
    market_cap_tao: Optional[float] = None
    tao_in: Optional[float] = None
    alpha_in: Optional[float] = None


class StakePosition(BaseModel):
    """A single alpha stake position held by a coldkey on a hotkey/subnet.

    Attributes:
        coldkey: Owning coldkey ss58 address.
        hotkey: Hotkey ss58 address the alpha is staked to.
        netuid: Subnet id the alpha belongs to (0 == root / TAO).
        alpha_staked: Amount of alpha staked, in whole alpha units.
        value_tao: TAO-equivalent value of the position, if known.
    """

    coldkey: str
    hotkey: str
    netuid: int
    alpha_staked: float
    value_tao: Optional[float] = None


class SubnetInfo(BaseModel):
    """Metadata and headline economics for a subnet."""

    netuid: int
    name: Optional[str] = None
    emission_pct: Optional[float] = None
    price_tao: Optional[float] = None
    market_cap_tao: Optional[float] = None
    n_validators: Optional[int] = None
    n_miners: Optional[int] = None
    registration_cost_tao: Optional[float] = None


class ValidatorInfo(BaseModel):
    """A validator/neuron entry within a subnet metagraph.

    Attributes:
        hotkey: Validator hotkey ss58 address.
        netuid: Subnet id the validator is registered on.
        stake_tao: Stake on this subnet in whole TAO/alpha units.
        vtrust: Validator trust score (0..1), if available.
        active: Whether the validator is currently active, if known.
    """

    hotkey: str
    netuid: int
    stake_tao: float
    vtrust: Optional[float] = None
    active: Optional[bool] = None


class Alert(BaseModel):
    """A single alert produced by the watch engine.

    Attributes:
        rule_type: Watch type that produced the alert
            (e.g. ``price_change``, ``stake_change``).
        severity: One of ``info``, ``warning`` or ``critical``.
        title: Short human-readable headline.
        message: Detailed human-readable description.
        netuid: Subnet id the alert concerns, if applicable.
        timestamp: ISO-8601 timestamp string for when the alert fired.
    """

    rule_type: str
    severity: str
    title: str
    message: str
    netuid: Optional[int] = None
    timestamp: str


class Portfolio(BaseModel):
    """Valued portfolio for a single coldkey.

    Attributes:
        coldkey: The coldkey ss58 address.
        positions: All known stake positions for the coldkey.
        total_value_tao: Sum of position values in TAO (positions missing a
            pool price are excluded from the total).
        total_value_usd: Total value in USD, if a TAO/USD price was available.
        tao_price_usd: TAO/USD price used for the USD valuation, if any.
    """

    coldkey: str
    positions: list[StakePosition] = Field(default_factory=list)
    total_value_tao: float
    total_value_usd: Optional[float] = None
    tao_price_usd: Optional[float] = None


class HealthReport(BaseModel):
    """A subnet health scan result.

    Attributes:
        netuid: Subnet id scored.
        name: Subnet name, if known.
        score: Overall health score in the range 0..100.
        grade: Letter grade derived from ``score`` (A..F).
        metrics: Arbitrary metric name -> value mapping used in scoring.
        warnings: Human-readable flags raised during scoring.
    """

    netuid: int
    name: Optional[str] = None
    score: float
    grade: str
    metrics: dict = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
