"""Tests for :class:`~tao_sentinel.scanner.SubnetScanner`.

Covers:

* All scores landing within the documented 0..100 range.
* A concentrated-validator subnet scoring strictly worse than a distributed
  one (the mock client makes netuid 1 concentrated and netuid 4 distributed).
* Grade boundary mapping (A>=85, B>=70, C>=55, D>=40, else F).
* The rate-frugal all-subnets path scoring from the subnet list alone (no
  per-subnet validator calls) and recording that in ``metrics``.

Everything runs against the deterministic mock client; no network.
"""

from __future__ import annotations

import pytest

from tao_sentinel.api import TaostatsError
from tao_sentinel.models import HealthReport, SubnetInfo
from tao_sentinel.scanner import SubnetScanner, _grade


# --------------------------------------------------------------------------- #
# Grade boundaries
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "score, expected",
    [
        (100.0, "A"),
        (85.0, "A"),  # lower edge of A is inclusive
        (84.99, "B"),
        (70.0, "B"),  # lower edge of B
        (69.99, "C"),
        (55.0, "C"),  # lower edge of C
        (54.99, "D"),
        (40.0, "D"),  # lower edge of D
        (39.99, "F"),
        (0.0, "F"),
    ],
)
def test_grade_boundaries(score, expected):
    """Letter grades follow the A/B/C/D/F cutoffs exactly."""
    assert _grade(score) == expected


# --------------------------------------------------------------------------- #
# Single-subnet scan
# --------------------------------------------------------------------------- #


def test_scan_single_subnet_returns_one_report(mock_client):
    """Scanning a specific netuid returns exactly one report for it."""
    reports = SubnetScanner(mock_client).scan(1)
    assert len(reports) == 1
    report = reports[0]
    assert isinstance(report, HealthReport)
    assert report.netuid == 1
    assert report.metrics["validator_data"] is True  # validators were fetched


def test_scan_unknown_netuid_returns_empty(mock_client):
    """Scanning a netuid the client does not know about yields no report."""
    assert SubnetScanner(mock_client).scan(999) == []


def test_all_scores_within_range(mock_client):
    """Every produced score is clamped to the inclusive 0..100 range."""
    scanner = SubnetScanner(mock_client)
    reports = list(scanner.scan())
    for netuid in (1, 4, 8, 64):
        reports.extend(scanner.scan(netuid))
    assert reports
    for report in reports:
        assert 0.0 <= report.score <= 100.0
        assert report.grade in {"A", "B", "C", "D", "F"}


# --------------------------------------------------------------------------- #
# Concentration: concentrated scores worse than distributed
# --------------------------------------------------------------------------- #


def test_concentrated_subnet_scores_worse_than_distributed(mock_client):
    """netuid 1 (one dominant validator) scores below netuid 4 (distributed)."""
    scanner = SubnetScanner(mock_client)
    concentrated = scanner.scan(1)[0]
    distributed = scanner.scan(4)[0]

    assert concentrated.score < distributed.score


def test_concentrated_subnet_raises_concentration_warning(mock_client):
    """The concentrated subnet flags its top-1/top-5 stake dominance."""
    report = SubnetScanner(mock_client).scan(1)[0]

    conc = report.metrics["concentration"]
    assert conc["top1_share"] > 0.30
    assert conc["top5_share"] > 0.70
    # Both threshold breaches surface as human-readable warnings.
    assert any("Top validator" in w for w in report.warnings)
    assert any("Top 5" in w for w in report.warnings)


def test_distributed_subnet_has_no_concentration_warning(mock_client):
    """The distributed subnet's top-1 share stays under the 30% threshold."""
    report = SubnetScanner(mock_client).scan(4)[0]

    assert report.metrics["concentration"]["top1_share"] <= 0.30
    assert not any("Top validator holds" in w for w in report.warnings)


# --------------------------------------------------------------------------- #
# All-subnets (rate-frugal) scan
# --------------------------------------------------------------------------- #


def test_scan_all_returns_report_per_subnet(mock_client):
    """A bare scan() returns one report per subnet the client knows."""
    reports = SubnetScanner(mock_client).scan()
    assert {r.netuid for r in reports} == {1, 4, 8, 64}


def test_scan_all_does_not_fetch_validators(mock_client):
    """All-subnets mode scores from the subnet list alone (rate-frugal)."""

    class _NoValidatorClient:
        """Wraps the mock client but forbids get_validators calls."""

        def __init__(self, inner):
            self._inner = inner

        def get_subnets(self):
            return self._inner.get_subnets()

        def get_pools(self):
            return self._inner.get_pools()

        def get_validators(self, netuid):  # pragma: no cover - must not run
            raise AssertionError("scan() must not fetch validators")

    scanner = SubnetScanner(_NoValidatorClient(mock_client))
    reports = scanner.scan()  # would raise if validators were fetched

    assert reports
    for report in reports:
        assert report.metrics["validator_data"] is False
        assert "note" in report.metrics  # records the rate-frugal omission
        # Concentration is excluded (not faked from a slot cap), not scored.
        assert report.metrics["concentration"] == {"source": "unavailable"}


# --------------------------------------------------------------------------- #
# Finding 9: all-subnets scan excludes concentration & renormalizes weights
# --------------------------------------------------------------------------- #


class _CountingPoolClient:
    """Mock-backed client that records get_pools/get_validators call counts."""

    def __init__(self, inner, pools_override=None):
        self._inner = inner
        self._pools_override = pools_override
        self.pool_calls = 0
        self.validator_calls = 0

    def get_subnets(self):
        return self._inner.get_subnets()

    def get_pools(self):
        self.pool_calls += 1
        if self._pools_override is not None:
            return list(self._pools_override)
        return self._inner.get_pools()

    def get_validators(self, netuid):
        self.validator_calls += 1
        return self._inner.get_validators(netuid)


def test_scan_all_excludes_concentration_and_renormalizes():
    """Scan-all renormalizes over emission/neuron/market only (no concentration).

    A single subnet whose three contributing components each score full marks
    must yield exactly 100, proving the remaining weights are renormalized to
    sum to 100 rather than leaving the discarded 35-point concentration slot
    as dead weight (which would cap the score at 65).
    """

    class _PerfectSubnetClient:
        def get_subnets(self):
            # emission == median -> full marks; >=64 validators and >=200
            # miners -> full neuron marks; positive price + market cap -> full
            # market marks.
            return [
                SubnetInfo(
                    netuid=1, name="apex", emission_pct=10.0, price_tao=0.02,
                    market_cap_tao=100000.0, n_validators=64, n_miners=200,
                ),
            ]

        def get_pools(self):
            return []

    report = SubnetScanner(_PerfectSubnetClient()).scan()[0]

    assert report.metrics["concentration"] == {"source": "unavailable"}
    assert report.metrics["validator_data"] is False
    assert "EXCLUDED" in report.metrics["note"]
    # Renormalized: 65 weighted points / 65 total * 100 == 100, not 65.
    assert report.score == pytest.approx(100.0)


def test_scan_all_score_independent_of_validator_slot_cap():
    """Two subnets identical except for max_validators score IDENTICALLY.

    The slot cap (``max_validators``) is not a population or concentration
    signal, so in the all-subnets scan it must not move the headline score at
    all: the concentration component is excluded AND the validator sub-score of
    the neuron component is skipped (only the slot cap is known, never the live
    count). The two subnets therefore differ only in their (ignored) cap and
    must produce exactly the same score and grade -- the old proxy made the
    higher-cap subnet score higher, which was the bug.
    """

    class _TwoSubnetClient:
        def __init__(self, cap_a, cap_b):
            self._cap_a = cap_a
            self._cap_b = cap_b

        def get_subnets(self):
            return [
                SubnetInfo(
                    netuid=1, name="a", emission_pct=8.0, price_tao=0.02,
                    market_cap_tao=100000.0, n_validators=self._cap_a,
                    n_miners=200,
                ),
                SubnetInfo(
                    netuid=2, name="b", emission_pct=8.0, price_tao=0.02,
                    market_cap_tao=100000.0, n_validators=self._cap_b,
                    n_miners=200,
                ),
            ]

        def get_pools(self):
            return []

    reports = {r.netuid: r for r in SubnetScanner(_TwoSubnetClient(8, 256)).scan()}
    assert reports[1].metrics["concentration"] == {"source": "unavailable"}
    assert reports[2].metrics["concentration"] == {"source": "unavailable"}
    # The slot cap is recorded but flagged as a cap, not a live count, and does
    # not contribute to the score.
    assert reports[1].metrics["n_validators_is_cap"] is True
    assert reports[2].metrics["n_validators_is_cap"] is True
    assert reports[1].metrics["n_active_validators"] is None
    assert reports[2].metrics["n_active_validators"] is None
    # Identical in everything that scores -> identical score AND grade.
    assert reports[2].score == reports[1].score
    assert reports[2].grade == reports[1].grade


def test_single_netuid_scan_uses_real_stake_distribution(mock_client):
    """A single-netuid scan still scores real concentration (top1/top5 shares)."""
    report = SubnetScanner(mock_client).scan(1)[0]
    conc = report.metrics["concentration"]
    assert report.metrics["validator_data"] is True
    assert "source" not in conc  # real distribution, not the unavailable flag
    assert conc["top1_share"] is not None
    assert conc["n_active_validators"] > 0


def test_neuron_component_uses_real_active_count_not_slot_cap(mock_client):
    """Single-netuid neuron scoring uses the live active count, not the cap.

    Regression for finding 9's second sub-concern: ``subnet.n_validators`` is
    the slot CAP (``max_validators``, 64 in the fixtures) so scoring it would
    peg the validator sub-score at full marks and suppress the "only N
    validators" warning. The single scan must instead use the true active
    count from the fetched validator set.
    """
    report = SubnetScanner(mock_client).scan(1)[0]
    # The cap is recorded but flagged as a cap; the live count is also recorded.
    assert report.metrics["n_validators"] == 64  # the cap, for reference
    assert report.metrics["n_validators_is_cap"] is False  # live data present
    active = report.metrics["n_active_validators"]
    assert active is not None and active < 8  # netuid 1 truly has < 8 active
    # The warning now fires on the REAL count (it never did under the cap).
    assert any("active validators registered" in w for w in report.warnings)


def test_low_validator_warning_fires_on_real_count_for_sparse_subnets(mock_client):
    """The "only N validators" warning fires for every truly-sparse subnet.

    Under the old slot-cap logic the warning never fired (every fixture caps at
    64). netuid 1, 4 and 64 each have fewer than 8 ACTIVE validators, so the
    warning must now fire for them, and not for netuid 8 (exactly 8).
    """
    scanner = SubnetScanner(mock_client)
    for netuid in (1, 4, 64):
        report = scanner.scan(netuid)[0]
        assert any(
            "active validators registered" in w for w in report.warnings
        ), f"expected sparse-validator warning for netuid {netuid}"
    report8 = scanner.scan(8)[0]
    assert report8.metrics["n_active_validators"] == 8
    assert not any(
        "active validators registered" in w for w in report8.warnings
    )


def test_scan_all_skips_validator_subscore_and_does_not_saturate():
    """All-subnets neuron scoring ignores the slot cap entirely.

    With only the slot cap available, the validator sub-score must be SKIPPED
    (not a saturated 1.0), so the neuron component is driven by miners alone.
    A subnet with a huge cap but a tiny miner count must NOT score near full on
    the neuron component.
    """

    class _BigCapTinyMinersClient:
        def get_subnets(self):
            return [
                SubnetInfo(
                    netuid=1, name="a", emission_pct=10.0, price_tao=0.02,
                    market_cap_tao=100000.0, n_validators=4096, n_miners=20,
                ),
            ]

        def get_pools(self):
            return []

    report = SubnetScanner(_BigCapTinyMinersClient()).scan()[0]
    # Cap recorded but flagged; no live count in all-subnets mode.
    assert report.metrics["n_validators"] == 4096
    assert report.metrics["n_validators_is_cap"] is True
    assert report.metrics["n_active_validators"] is None
    # Emission + market are full; the neuron component is miner-only (20/200 =
    # 0.1). Renormalized over emission(20)+neuron(25)+market(20)=65:
    # (1*20 + 0.1*25 + 1*20)/65 * 100 ~= 65.4 -- nowhere near the ~96 it would
    # be if the giant slot cap had saturated the validator sub-score.
    assert report.score < 70.0


def test_scan_all_score_is_flagged_provisional_with_swing_disclosure():
    """All-subnets reports are flagged provisional and disclose the grade swing.

    Regression for finding 9's note-disclosure complaint: the all-subnets note
    must warn that the score is PROVISIONAL and can be materially higher (enough
    to flip the grade) than the concentration-inclusive single-netuid score.
    """

    class _OneSubnetClient:
        def get_subnets(self):
            return [
                SubnetInfo(
                    netuid=1, name="apex", emission_pct=10.0, price_tao=0.02,
                    market_cap_tao=100000.0, n_validators=64, n_miners=200,
                ),
            ]

        def get_pools(self):
            return []

    report = SubnetScanner(_OneSubnetClient()).scan()[0]
    assert report.metrics["provisional"] is True
    note = report.metrics["note"]
    # The note must surface the unreliability, not just "missing precision".
    assert "PROVISIONAL" in note
    assert "NOT comparable" in note
    assert "flip the letter grade" in note


def test_same_subnet_scan_all_higher_and_grade_flips_is_disclosed(mock_client):
    """The all-subnets vs single divergence still exists BUT is now disclosed.

    The contract pins this mechanism (scan-all excludes concentration; single
    includes it), so the two modes legitimately disagree for a concentrated
    subnet. What finding 9 required is that the gap not be silent: the
    all-subnets report must carry provisional=True and a note that warns the
    score can flip the grade versus a single-netuid scan.
    """
    scanner = SubnetScanner(mock_client)
    all_report = next(r for r in scanner.scan() if r.netuid == 1)
    single_report = scanner.scan(1)[0]

    # netuid 1 is the concentrated fixture: scan-all (no concentration penalty)
    # scores materially higher and the grade flips relative to the single scan.
    assert all_report.score > single_report.score
    assert all_report.grade != single_report.grade
    # ... and that is explicitly disclosed, not silent.
    assert all_report.metrics["provisional"] is True
    assert "flip the letter grade" in all_report.metrics["note"]
    assert single_report.metrics["validator_data"] is True
    assert "provisional" not in single_report.metrics


def test_scan_passes_prefetched_pools_without_fetching(mock_client):
    """scan(pools=...) reuses the caller's pool list and never calls get_pools."""
    client = _CountingPoolClient(mock_client)
    pools = mock_client.get_pools()
    reports = SubnetScanner(client).scan(pools=pools)

    assert reports
    assert client.pool_calls == 0  # the pre-fetched list was reused


# --------------------------------------------------------------------------- #
# Finding 21: _merge_pool_data catches only TaostatsError
# --------------------------------------------------------------------------- #


def test_merge_pool_data_degrades_on_taostats_error(mock_client):
    """A genuine API failure during pool fetch degrades gracefully."""

    class _FailingPoolClient:
        def __init__(self, inner):
            self._inner = inner

        def get_subnets(self):
            return self._inner.get_subnets()

        def get_pools(self):
            raise TaostatsError(0, "transport down")

        def get_validators(self, netuid):
            return self._inner.get_validators(netuid)

    reports = SubnetScanner(_FailingPoolClient(mock_client)).scan()
    # Still produces reports, just without merged market data.
    assert {r.netuid for r in reports} == {1, 4, 8, 64}


def test_merge_pool_data_propagates_programming_error(mock_client):
    """A programming bug in get_pools is NOT masked as a benign pool failure."""

    class _BuggyPoolClient:
        def __init__(self, inner):
            self._inner = inner

        def get_subnets(self):
            return self._inner.get_subnets()

        def get_pools(self):
            raise AttributeError("typo: pool.pirce_tao")

        def get_validators(self, netuid):
            return self._inner.get_validators(netuid)

    with pytest.raises(AttributeError):
        SubnetScanner(_BuggyPoolClient(mock_client)).scan()
