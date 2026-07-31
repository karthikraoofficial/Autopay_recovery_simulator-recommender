from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

import numpy as np
from pydantic import ConfigDict, Field

from rebound.config import Assumptions
from rebound.domain.entities import DomainModel
from rebound.harness.metrics import StrategyMetrics
from rebound.seeding import stream

if TYPE_CHECKING:
    from rebound.harness.runner import SeedResult

# SPEC §5.1's metric set, as functions of one seed's counters. Ordered, because the
# report is hashed and rendered in this order.
METRICS: tuple[tuple[str, Callable[[StrategyMetrics], float]], ...] = (
    ("recovery_rate", lambda m: m.recovery_rate),
    ("gross_recovered_paise", lambda m: float(m.gross_recovered_paise)),
    ("net_recovered_paise", lambda m: float(m.net_recovered_paise)),
    ("attempts_per_recovery", lambda m: m.attempts_per_recovery or 0.0),
    ("induced_revocations", lambda m: float(m.induced_revocations)),
    ("median_days_to_recovery", lambda m: float(m.median_days_to_recovery or 0)),
    ("p90_days_to_recovery", lambda m: float(m.p90_days_to_recovery or 0)),
)


class Interval(DomainModel):
    """SPEC: never a point estimate without an interval. The point is carried alongside
    the bounds so the two cannot be reported apart."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy: str = Field(min_length=1)
    metric: str = Field(min_length=1)
    point: float
    low: float
    high: float
    level: float = Field(gt=0.0, lt=1.0)
    vs_baseline: str | None = None

    def __str__(self) -> str:
        span = f"[{self.low:,.4g}, {self.high:,.4g}]"
        tail = f" vs {self.vs_baseline}" if self.vs_baseline else ""
        return f"{self.strategy}.{self.metric}{tail}: {span} (point {self.point:,.4g})"


def _resample(values: np.ndarray, draws: np.ndarray, level: float) -> tuple[float, float, float]:
    means = values[draws].mean(axis=1)
    tail = (1.0 - level) / 2.0
    low, high = np.quantile(means, [tail, 1.0 - tail])
    return float(values.mean()), float(low), float(high)


def paired_intervals(
    per_seed: Sequence[SeedResult],
    names: Sequence[str],
    master_seed: int,
    assumptions: Assumptions,
) -> tuple[Interval, ...]:
    """Bootstrap over seeds, resampling whole seeds so the pairing survives: a resample
    takes the same seed's result for every strategy, which is what makes the difference
    interval a paired one rather than a difference of two independent intervals."""
    level = float(assumptions.value("harness.confidence_level"))
    resamples = int(assumptions.value("harness.bootstrap_resamples"))
    baseline = str(assumptions.value("harness.baseline_strategy"))
    rng = stream(master_seed, "bootstrap", "seeds")
    draws = rng.integers(0, len(per_seed), size=(resamples, len(per_seed)))
    intervals: list[Interval] = []
    for metric_name, extract in METRICS:
        columns = {
            name: np.array([extract(s.for_strategy(name)) for s in per_seed]) for name in names
        }
        for name in names:
            point, low, high = _resample(columns[name], draws, level)
            intervals.append(
                Interval(
                    strategy=name,
                    metric=metric_name,
                    point=point,
                    low=low,
                    high=high,
                    level=level,
                )
            )
        if baseline not in columns:
            continue
        for name in names:
            if name == baseline:
                continue
            point, low, high = _resample(columns[name] - columns[baseline], draws, level)
            intervals.append(
                Interval(
                    strategy=name,
                    metric=metric_name,
                    point=point,
                    low=low,
                    high=high,
                    level=level,
                    vs_baseline=baseline,
                )
            )
    return tuple(intervals)
