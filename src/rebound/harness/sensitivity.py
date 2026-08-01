from __future__ import annotations

from collections.abc import Callable, Sequence

from pydantic import ConfigDict, Field

from rebound.config import Assumption, Assumptions
from rebound.domain.entities import DomainModel
from rebound.harness.runner import run_experiment
from rebound.strategies.base import RetryStrategy

StrategyFactory = Callable[[Assumptions], Sequence[RetryStrategy]]

# Keys that describe how the measurement is run rather than what is being modelled.
# Sweeping them changes the experiment, not the world, so a "result" from doing so would
# be a statement about our own runtime budget.
_MEASUREMENT_PREFIXES: tuple[str, ...] = (
    "harness.sensitivity.",
    "harness.bootstrap_",
    "harness.confidence_level",
    "book.size",
    "book.months",
    "book.start_date",
    # Replaced by the phase-4 engine and read by nothing on this path.
    "stub_engine.",
    "engine.calibration.",
)

# Units whose values are probabilities. A ±50% sweep can push one outside [0, 1], where
# the domain model rejects it; those are clamped and the clamp is recorded, because a
# silently clamped sweep understates sensitivity without saying so.
_PROBABILITY_UNITS: frozenset[str] = frozenset({"ratio"})

_PROBABILITY_KEY_HINTS: tuple[str, ...] = (
    "uptime",
    "probability",
    "share",
    "rate",
    "hazard",
    "intent_score_mean",
)

# Units whose values live on a calendar and cannot be scaled out of it.
_DOMAIN_BY_UNIT: dict[str, tuple[int, int]] = {
    "hour_of_day": (0, 23),
    "day_of_month": (1, 31),
    "weekday_index": (0, 6),
}

# Groups whose members are shares of one thing and are validated to sum to 1. Scaling a
# member on its own is not a configuration that exists, so the others absorb the change
# proportionally: the swept key's share of the whole moves by the sweep factor, which is
# the only reading of "this share is 50% larger" that leaves a valid book behind.
_MIX_GROUPS: tuple[tuple[str, ...], ...] = (
    (
        "population.customer.band_share.low",
        "population.customer.band_share.mid",
        "population.customer.band_share.high",
    ),
    (
        "population.customer.upi_app_share.app_a",
        "population.customer.upi_app_share.app_b",
        "population.customer.upi_app_share.app_c",
        "population.customer.upi_app_share.app_d",
    ),
    (
        "population.mandate.rail_mix.upi_autopay",
        "population.mandate.rail_mix.enach",
        "population.mandate.rail_mix.card_emandate",
    ),
)

# Keys whose ±50% sweep has no valid representation at all, with the reason. Each is an
# ordered pair: moving one end past the other describes no population, and clamping it
# back to the boundary would silently report a sweep that never happened.
UNSWEEPABLE: dict[str, str] = {
    "population.customer.salary_day.month_end_min": (
        "a 50% rise pushes the start of the month-end payroll cluster past its end"
    ),
    "population.customer.salary_day.month_end_max": (
        "a 50% fall pushes the end of the month-end payroll cluster below its start"
    ),
}


def sweepable_keys(assumptions: Assumptions) -> tuple[str, ...]:
    """Every numeric modelling assumption, in file order.

    Booleans are excluded because a ±50% sweep is meaningless on them — the binary flags
    are reported as an explicit pair instead (see `binary_flag_pair`), which SPEC §11
    requires permanently for the pre-debit notification question.
    """
    return tuple(
        key
        for key, entry in assumptions.assumptions.items()
        if isinstance(entry.value, int | float)
        and not isinstance(entry.value, bool)
        and not key.startswith(_MEASUREMENT_PREFIXES)
        and key not in UNSWEEPABLE
    )


def _is_probability(key: str, entry: Assumption) -> bool:
    if entry.unit not in _PROBABILITY_UNITS:
        return False
    return any(hint in key for hint in _PROBABILITY_KEY_HINTS)


def _mix_group(key: str) -> tuple[str, ...] | None:
    for group in _MIX_GROUPS:
        if key in group:
            return group
    return None


def _clamp(key: str, entry: Assumption, raw: float, ceiling: float) -> tuple[float, bool]:
    if _is_probability(key, entry) and raw > ceiling:
        return ceiling, True
    domain = _DOMAIN_BY_UNIT.get(entry.unit)
    if domain is not None and not domain[0] <= raw <= domain[1]:
        return float(min(max(raw, domain[0]), domain[1])), True
    return raw, False


def scaled(assumptions: Assumptions, key: str, factor: float) -> tuple[Assumptions, bool]:
    """A copy with one key multiplied. Returns whether the value had to be clamped.

    Raises on a key the sweep cannot represent, rather than quietly returning the
    unswept config — which would be reported as a robust key when nothing was tested.
    """
    if key in UNSWEEPABLE:
        raise ValueError(f"{key} cannot be swept: {UNSWEEPABLE[key]}")
    entry = assumptions.assumptions[key]
    ceiling = float(assumptions.value("harness.sensitivity.probability_ceiling"))
    raw, clamped = _clamp(key, entry, float(entry.value) * factor, ceiling)
    value: float | int = raw
    if isinstance(entry.value, int):
        # A calendar value may legitimately be 0 (midnight, Monday); every other count
        # is a mechanism that must not be rounded out of existence.
        low = _DOMAIN_BY_UNIT.get(entry.unit, (1, None))[0]
        value = max(low, round(raw))
    entries = dict(assumptions.assumptions)
    entries[key] = entry.model_copy(update={"value": value})
    group = _mix_group(key)
    if group is not None:
        _renormalise(entries, group, key, float(value))
    return assumptions.model_copy(update={"assumptions": entries}), clamped


def _renormalise(
    entries: dict[str, Assumption], group: tuple[str, ...], key: str, value: float
) -> None:
    """Spread the change across the rest of the group so the shares still sum to 1."""
    others = [k for k in group if k != key]
    remaining = 1.0 - value
    total = sum(float(entries[k].value) for k in others)
    for other in others:
        share = float(entries[other].value) / total if total else 1.0 / len(others)
        entries[other] = entries[other].model_copy(update={"value": remaining * share})


class SweepPoint(DomainModel):
    """One assumption at one setting, and what the headline became."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(min_length=1)
    factor: float
    clamped: bool = False
    lift_point: float
    lift_low: float
    lift_high: float
    metric: str = Field(min_length=1)

    @property
    def interval_excludes_zero(self) -> bool:
        return self.lift_low > 0.0 or self.lift_high < 0.0


class SweepResult(DomainModel):
    """One assumption, swept both ways, against the unswept baseline."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(min_length=1)
    base: SweepPoint
    low: SweepPoint
    high: SweepPoint

    @property
    def points(self) -> tuple[SweepPoint, SweepPoint]:
        return (self.low, self.high)

    @property
    def sign_flips(self) -> bool:
        """The conclusion reverses: the strategy that was ahead falls behind."""
        return any((p.lift_point > 0) != (self.base.lift_point > 0) for p in self.points)

    @property
    def loses_significance(self) -> bool:
        """The lift stops being distinguishable from zero at this level."""
        if not self.base.interval_excludes_zero:
            return False
        return any(not p.interval_excludes_zero for p in self.points)

    @property
    def max_relative_swing(self) -> float:
        if self.base.lift_point == 0.0:
            return float("inf")
        return max(
            abs(p.lift_point - self.base.lift_point) / abs(self.base.lift_point)
            for p in self.points
        )

    @property
    def base_is_significant(self) -> bool:
        return self.base.interval_excludes_zero

    @property
    def is_fragile(self) -> bool:
        """Undefined unless the unswept conclusion is itself significant.

        With a base interval straddling zero there is no conclusion to be fragile about:
        every sweep 'flips the sign' of a point estimate that is indistinguishable from
        no effect, and `max_relative_swing` divides by something near zero and reports
        swings in the tens of thousands of percent. A sweep run in that state produces a
        long, confident-looking fragility list made entirely of noise, which is worse
        than no sweep at all. `run_sweep` refuses to run rather than emit one.
        """
        return self.base_is_significant and (self.sign_flips or self.loses_significance)


class Sweep(DomainModel):
    """A configured sensitivity run. SPEC §5.1: sweep each assumption ±50% and report
    which ones the conclusion is fragile to."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    assumptions: Assumptions
    # Strategies are rebuilt per setting on purpose: they read their constants at
    # construction, so a strategy object made before the sweep would carry the unswept
    # value and the sweep would silently measure nothing.
    build: StrategyFactory
    subject: str = Field(min_length=1)
    metric: str = "net_recovered_paise"
    master_seed: int = 0

    @classmethod
    def sized_for_runtime(
        cls, assumptions: Assumptions, build: StrategyFactory, subject: str, master_seed: int = 0
    ) -> Sweep:
        """The sweep reruns the whole paired experiment twice per assumption, so it runs
        on a smaller book over fewer months than the headline. That is a runtime
        compromise and it costs precision: intervals here are wider than the headline's
        and the two are not interchangeable."""
        entries = dict(assumptions.assumptions)
        for target, source in (
            ("book.size", "harness.sensitivity.book_size"),
            ("book.months", "harness.sensitivity.months"),
        ):
            entries[target] = entries[target].model_copy(
                update={"value": assumptions.value(source)}
            )
        return cls(
            assumptions=assumptions.model_copy(update={"assumptions": entries}),
            build=build,
            subject=subject,
            master_seed=master_seed,
        )

    def measure(
        self, assumptions: Assumptions, key: str, factor: float, clamped: bool = False
    ) -> SweepPoint:
        n_seeds = int(assumptions.value("harness.sensitivity.seeds"))
        report = run_experiment(self.build(assumptions), self.master_seed, assumptions, n_seeds)
        baseline = str(assumptions.value("harness.baseline_strategy"))
        [interval] = [
            i
            for i in report.intervals_for(self.subject)
            if i.metric == self.metric and i.vs_baseline == baseline
        ]
        return SweepPoint(
            key=key,
            factor=factor,
            clamped=clamped,
            lift_point=interval.point,
            lift_low=interval.low,
            lift_high=interval.high,
            metric=self.metric,
        )

    def base_point(self) -> SweepPoint:
        return self.measure(self.assumptions, "<unswept>", 1.0)

    def sweep_key(self, key: str, base: SweepPoint) -> SweepResult:
        factor = float(self.assumptions.value("harness.sensitivity.sweep_factor"))
        points = []
        for direction in (1.0 - factor, 1.0 + factor):
            swept, clamped = scaled(self.assumptions, key, direction)
            points.append(self.measure(swept, key, direction, clamped))
        return SweepResult(key=key, base=base, low=points[0], high=points[1])

    def binary_flag_pair(self, key: str) -> tuple[SweepPoint, SweepPoint]:
        """SPEC §11: the pre-debit-notification question is binary, so it gets both
        settings side by side permanently rather than a ±50% sweep it cannot have."""
        results = []
        for setting in (False, True):
            entries = dict(self.assumptions.assumptions)
            entries[key] = entries[key].model_copy(update={"value": setting})
            swept = self.assumptions.model_copy(update={"assumptions": entries})
            results.append(self.measure(swept, key, float(setting)))
        return results[0], results[1]


class UnderpoweredSweepError(RuntimeError):
    """The unswept conclusion is not significant, so there is nothing to sweep."""


def run_sweep(sweep: Sweep, keys: Sequence[str] | None = None) -> tuple[SweepResult, ...]:
    base = sweep.base_point()
    if not base.interval_excludes_zero:
        raise UnderpoweredSweepError(
            f"base lift {base.lift_point:,.0f} has interval "
            f"[{base.lift_low:,.0f}, {base.lift_high:,.0f}], which contains zero. "
            "A sensitivity analysis around a null result reports the sign flips of noise "
            "as fragility. Raise harness.sensitivity.seeds or book_size until the "
            "unswept comparison is significant, then sweep."
        )
    keys = keys if keys is not None else sweepable_keys(sweep.assumptions)
    return tuple(sweep.sweep_key(key, base) for key in keys)


def fragile_first(results: Sequence[SweepResult]) -> tuple[SweepResult, ...]:
    """Fragile keys first, then by how far the headline moves. The ranking is the
    deliverable: a CFO asks which number, if wrong, changes the answer."""
    return tuple(
        sorted(results, key=lambda r: (not r.is_fragile, -r.max_relative_swing, r.key))
    )
