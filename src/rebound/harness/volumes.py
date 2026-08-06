"""How many opening debits were attempted, how many failed, per rail and overall.

The question a merchant asks first — *how many of my debits fail?* — and the one the lift
figures could not answer, because their counters are about episodes and rupees rather than
about the denominator underneath.

**Context for the headline, not a third line item.** It is not lift and must never be
rendered beside the two lift figures as though it were: it says how big the problem is,
where they already are, before anything is claimed about what fixing it is worth.

Cut from the headline's own run through `RunObserver`, the seam SPEC §12.3 established, so
these figures and the rupee figures describe the same simulated book.

**The denominator was never recorded anywhere.** `StrategyMetrics.total_attempts` sums the
attempts *inside episodes*, and an episode exists only for a failed opening — so
`total_attempts - retry_attempts` is identically `episodes`, and a successful opening debit
is counted nowhere in the hashed counters. That is why this cannot be derived from the
existing output and needs an observer: the numerator was always there, the denominator
never was. The numerator still reconciles — a test asserts the observed failures equal
`episodes` per seed, so if the two ever disagree one of them is wrong.
"""

from __future__ import annotations

from collections import defaultdict

import numpy as np
from pydantic import ConfigDict, Field

from rebound.config import Assumptions
from rebound.domain.entities import DomainModel, Mandate, Rail
from rebound.harness.bootstrap import Interval, _resample
from rebound.seeding import stream

# The label the overall line carries. Not a `Rail`, deliberately: it is the sum over rails
# and would otherwise be mistaken for a fourth one.
ALL_RAILS = "all rails"

ATTEMPTED = "opening_debits_attempted"
FAILED = "opening_debits_failed"
FAILURE_RATE = "opening_debit_failure_rate"


class VolumeCollector:
    """A `RunObserver` that tallies opening debits for one strategy's run.

    One strategy, because opening debits are *not* strategy-invariant: retry aggression
    induces revocations, and a revoked mandate has no further opening debits. Tallying
    every strategy and adding them would produce a number belonging to no run at all.
    """

    def __init__(self, strategy: str) -> None:
        self.strategy = strategy
        self._attempted: dict[tuple[int, Rail], int] = defaultdict(int)
        self._failed: dict[tuple[int, Rail], int] = defaultdict(int)
        self._seeds: set[int] = set()

    def saw_opening_attempt(
        self, strategy: str, seed: int, mandate: Mandate, cycle_index: int, failed: bool
    ) -> None:
        if strategy != self.strategy:
            return
        self._seeds.add(seed)
        self._attempted[(seed, mandate.rail)] += 1
        if failed:
            self._failed[(seed, mandate.rail)] += 1

    def saw_mandate(self, *_: object) -> None:
        """Not needed: a mandate with no debitable cycle contributes no opening debit."""

    def saw_episode(self, *_: object) -> None:
        """Not needed: an episode is a failed opening, which `saw_opening_attempt` already
        reported. Counting it here as well would double the numerator."""

    def saw_cycle_guard_events(self, *_: object) -> None:
        """Not needed: compliance verdicts are about retries, and these are openings."""

    def seeds(self) -> tuple[int, ...]:
        return tuple(sorted(self._seeds))

    def for_rail(self, seed: int, rail: Rail) -> tuple[int, int]:
        return self._attempted[(seed, rail)], self._failed[(seed, rail)]

    def totals(self, seed: int) -> tuple[int, int]:
        attempted = sum(v for (s, _), v in self._attempted.items() if s == seed)
        failed = sum(v for (s, _), v in self._failed.items() if s == seed)
        return attempted, failed

    def rails_seen(self) -> tuple[Rail, ...]:
        """Rails with at least one opening debit, in canonical enum order.

        A rail the merchant does not use is absent rather than reported as zero: a failure
        rate over zero debits is undefined, not 0%, and rendering it as 0% would put a
        reassuring number against a rail that was never tried.
        """
        used = {rail for (_, rail), count in self._attempted.items() if count}
        return tuple(rail for rail in Rail if rail in used)


class VolumeLine(DomainModel):
    """One rail, or the overall total. Intervals, each carrying its own point estimate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rail: str = Field(min_length=1)
    attempted: Interval
    failed: Interval
    failure_rate: Interval


class VolumeSummary(DomainModel):
    """SPEC §6: context above the two line items, never summed with them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Which run these came from. Opening debits are nearly, but not exactly,
    # strategy-invariant, so the figure has to name its run rather than read as a property
    # of the book.
    strategy: str = Field(min_length=1)
    seeds: tuple[int, ...] = Field(min_length=1)
    level: float = Field(gt=0.0, lt=1.0)
    # Per rail, then the overall line last.
    lines: tuple[VolumeLine, ...] = ()


def _rate_interval(
    strategy: str,
    attempted: np.ndarray,
    failed: np.ndarray,
    draws: np.ndarray,
    level: float,
) -> Interval:
    """A pooled rate: total failures over total attempts, bootstrapped over whole seeds.

    A ratio of sums rather than a mean of per-seed ratios. A seed that happened to produce
    few debits must not weigh as much as one that produced many, and a resample containing
    a seed with no debits on this rail has a defined pooled rate where a mean of ratios
    would have to divide by zero.
    """
    totals = attempted[draws].sum(axis=1)
    failures = failed[draws].sum(axis=1)
    usable = totals > 0
    ratios = failures[usable] / totals[usable]
    tail = (1.0 - level) / 2.0
    low, high = np.quantile(ratios, [tail, 1.0 - tail]) if ratios.size else (0.0, 0.0)
    denominator = attempted.sum()
    point = float(failed.sum() / denominator) if denominator else 0.0
    return Interval(
        strategy=strategy,
        metric=FAILURE_RATE,
        point=point,
        low=float(low),
        high=float(high),
        level=level,
    )


def _line(
    strategy: str,
    label: str,
    attempted: np.ndarray,
    failed: np.ndarray,
    draws: np.ndarray,
    level: float,
) -> VolumeLine:
    counts = []
    for metric, values in ((ATTEMPTED, attempted), (FAILED, failed)):
        point, low, high = _resample(values.astype(float), draws, level)
        counts.append(
            Interval(
                strategy=strategy, metric=metric, point=point, low=low, high=high, level=level
            )
        )
    return VolumeLine(
        rail=label,
        attempted=counts[0],
        failed=counts[1],
        failure_rate=_rate_interval(strategy, attempted, failed, draws, level),
    )


def volume_summary(
    collector: VolumeCollector, master_seed: int, assumptions: Assumptions
) -> VolumeSummary:
    """Per-rail and overall volumes, with intervals bootstrapped across whole seeds.

    Counts are per simulated book, as every other figure in a run is. Where the book was
    capped they are not the merchant's own totals, and the same no-extrapolation rule
    applies: nothing here multiplies them up.
    """
    seeds = collector.seeds()
    if not seeds:
        raise ValueError(
            f"no opening debits were observed for {collector.strategy!r}. The collector was "
            "either passed to a run that did not include it, or not passed to a run at all."
        )
    level = float(assumptions.value("harness.confidence_level"))
    resamples = int(assumptions.value("harness.bootstrap_resamples"))
    # Its own stream, so adding this report cannot shift the draws the lift intervals use.
    rng = stream(master_seed, "bootstrap", "volumes")
    draws = rng.integers(0, len(seeds), size=(resamples, len(seeds)))

    lines = []
    for rail in collector.rails_seen():
        attempted = np.array([collector.for_rail(seed, rail)[0] for seed in seeds])
        failed = np.array([collector.for_rail(seed, rail)[1] for seed in seeds])
        lines.append(_line(collector.strategy, rail.value, attempted, failed, draws, level))
    attempted = np.array([collector.totals(seed)[0] for seed in seeds])
    failed = np.array([collector.totals(seed)[1] for seed in seeds])
    lines.append(_line(collector.strategy, ALL_RAILS, attempted, failed, draws, level))
    return VolumeSummary(
        strategy=collector.strategy, seeds=seeds, level=level, lines=tuple(lines)
    )
