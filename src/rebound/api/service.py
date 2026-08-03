"""What the dashboard is told, and what it is not allowed to be told.

The response models here exist to make one thing impossible: reporting the headline as a
single number. `SimulationResult` carries `rescheduling` and `strategy` as two separate
`LiftLine` objects and has no combined field, no total, and no way to derive one without
the caller writing the addition itself. SPEC §6.2 asks for one headline number; phase 7
measured that the larger part of it is scheduler plumbing rather than retry logic, so the
honest headline is two line items and this module will not serve any other shape.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, computed_field

from rebound.api.inputs import MerchantProfile, Sizing, sizing_plan
from rebound.config import Assumption, Assumptions, load_assumptions
from rebound.harness.bootstrap import Interval
from rebound.harness.runner import (
    HarnessReport,
    ProgressCallback,
    default_strategies,
    run_experiment,
)
from rebound.harness.segments import SegmentCollector, SegmentReport, build_report

PAISE_PER_RUPEE = 100

# Fixed at 12 by SPEC §6.2's chart. Not a dashboard input: a merchant comparing two
# strategies over different horizons is not comparing them at all.
CHART_MONTHS_KEY = "book.months"


class LiftLine(BaseModel):
    """One line item of the headline, always with its interval.

    `low` and `high` are declared before `point` so that every rendering of this model —
    JSON key order included — puts the interval first, per CLAUDE.md.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(min_length=1)
    explanation: str = Field(min_length=1)
    gross_low_inr: float
    gross_high_inr: float
    gross_point_inr: float
    net_low_inr: float
    net_high_inr: float
    net_point_inr: float
    level: float = Field(gt=0.0, lt=1.0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def significant(self) -> bool:
        """Whether the net interval excludes zero. The dashboard must not render a bar
        that reads as a positive result when this is false.

        `computed_field`, not a bare `property`: Pydantic v2 omits plain properties from
        `model_dump`, so this was absent from the response body and the dashboard read it
        as `undefined` — falsy — and showed the not-significant warning on every result,
        including strictly positive ones. A predicate the UI depends on has to be in the
        payload, not merely on the Python object.
        """
        return self.net_low_inr > 0.0 or self.net_high_inr < 0.0


class MonthPoint(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    month: int = Field(ge=1)
    baseline_inr: float
    headline_inr: float


class StrategyLine(BaseModel):
    """One strategy's absolute result. Every figure carries an interval or is a counter
    with no interval, and counters say so rather than being shown as point estimates."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    role: str = Field(min_length=1)
    recovery_rate_low: float
    recovery_rate_high: float
    recovery_rate_point: float
    net_low_inr: float
    net_high_inr: float
    net_point_inr: float
    # SPEC §5.1's downside and cost columns. Shown beside recovery, never behind it: a
    # strategy that recovers more by attempting more has not been shown to be better.
    attempts_per_episode_point: float
    induced_revocations_point: float
    compliance_blocks: int
    compliance_reschedules: int


class SimulationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scheduler_reference: str
    baseline: str
    headline: str
    # The two line items. There is deliberately no field summing them.
    rescheduling: LiftLine
    strategy: LiftLine
    months: tuple[MonthPoint, ...]
    strategies: tuple[StrategyLine, ...]
    sizing: Sizing
    book_size_simulated: int
    book_size_requested: int
    # True when the simulated book is smaller than the merchant's. The rupee figures are
    # then per simulated book and are NOT theirs. Nothing here scales them up — see the
    # no-extrapolation note on `Sizing`.
    book_size_capped: bool
    n_seeds: int
    estimated_seconds: float
    confidence_level: float
    master_seed: int
    # The seeds actually run, so a caller offering a per-seed choice can only ever
    # offer one that was in this run. Derived client-side it would drift, and /trace
    # would answer with a valid trace of a population the headline never saw.
    seeds: tuple[int, ...] = Field(min_length=1)
    # The configuration this result was produced under. A derived export (trace,
    # segment report) must match it, and the export is refused if it does not.
    config_fingerprint: str = Field(min_length=1)
    output_hash: str
    retry_inherits_original_notice: bool


def _inr(paise: float) -> float:
    return paise / PAISE_PER_RUPEE


def _line(label: str, explanation: str, gross: Interval, net: Interval) -> LiftLine:
    return LiftLine(
        label=label,
        explanation=explanation,
        gross_low_inr=_inr(gross.low),
        gross_high_inr=_inr(gross.high),
        gross_point_inr=_inr(gross.point),
        net_low_inr=_inr(net.low),
        net_high_inr=_inr(net.high),
        net_point_inr=_inr(net.point),
        level=net.level,
    )


def _require(report: HarnessReport, strategy: str, metric: str, vs_baseline: bool) -> Interval:
    interval = report.interval(strategy, metric, vs_baseline=vs_baseline)
    if interval is None:
        raise KeyError(f"no {metric} interval for {strategy} (vs_baseline={vs_baseline})")
    return interval


def _lift_lines(report: HarnessReport, a: Assumptions) -> tuple[LiftLine, LiftLine]:
    """Rescheduling and strategy, apart. Built from `HarnessReport.decomposition` for the
    net figures and from the same intervals for gross, so the two cannot disagree."""
    net = report.decomposition("net_recovered_paise", a)
    gross = report.decomposition("gross_recovered_paise", a)
    if net is None or gross is None:
        raise KeyError("run did not include the strategies needed to decompose the headline")
    rescheduling = _line(
        f"Rescheduling ({net.scheduler_reference} to {net.baseline})",
        "Re-presenting a retry that a compliance timing rule blocked, instead of "
        "abandoning it. Scheduler plumbing: no reason codes, no inference, no model.",
        gross.rescheduling,
        net.rescheduling,
    )
    strategy = _line(
        f"Retry strategy ({net.baseline} to {net.headline})",
        "What the retry logic adds on top of a scheduler that already re-presents "
        "blocked retries. This is the only part that is intelligence rather than plumbing.",
        gross.strategy,
        net.strategy,
    )
    return rescheduling, strategy


def _months(report: HarnessReport, a: Assumptions) -> tuple[MonthPoint, ...]:
    """Mean ₹ recovered per book per billing month. Divided by the seed count because the
    aggregate counters are totals across seeds, and charting a total against a per-book
    headline would overstate the line by a factor of n_seeds."""
    baseline = report.for_strategy(str(a.value("harness.baseline_strategy")))
    headline = report.for_strategy(str(a.value("harness.headline_strategy")))
    seeds = len(report.seeds)
    return tuple(
        MonthPoint(month=i + 1, baseline_inr=_inr(b / seeds), headline_inr=_inr(h / seeds))
        for i, (b, h) in enumerate(
            zip(
                baseline.recovered_paise_by_cycle,
                headline.recovered_paise_by_cycle,
                strict=True,
            )
        )
    )


_ROLES: dict[str, str] = {
    "NoRetry": "floor: natural recovery with no retry at all",
    "NoReschedule": "what a merchant without a re-presenting scheduler runs today",
    "FixedSchedule": "baseline: T+1/T+3/T+7 with blocked retries re-presented",
}


def _strategy_lines(report: HarnessReport, a: Assumptions) -> tuple[StrategyLine, ...]:
    lines = []
    for name in report.strategies:
        metrics = report.for_strategy(name)
        recovery = _require(report, name, "recovery_rate", vs_baseline=False)
        net = _require(report, name, "net_recovered_paise", vs_baseline=False)
        attempts = _require(report, name, "attempts_per_episode", vs_baseline=False)
        revocations = _require(report, name, "induced_revocations", vs_baseline=False)
        lines.append(
            StrategyLine(
                name=name,
                role=_ROLES.get(name, "retry strategy"),
                recovery_rate_low=recovery.low,
                recovery_rate_high=recovery.high,
                recovery_rate_point=recovery.point,
                net_low_inr=_inr(net.low),
                net_high_inr=_inr(net.high),
                net_point_inr=_inr(net.point),
                attempts_per_episode_point=attempts.point,
                induced_revocations_point=revocations.point,
                compliance_blocks=metrics.compliance_blocks,
                compliance_reschedules=metrics.compliance_reschedules,
            )
        )
    return tuple(lines)


def simulate(
    profile: MerchantProfile,
    master_seed: int,
    sizing: Sizing = Sizing.INTERACTIVE,
    assumptions: Assumptions | None = None,
    progress: ProgressCallback | None = None,
) -> SimulationResult:
    """One paired experiment, reported as two line items and one chart.

    Deterministic given `master_seed` and the profile: `output_hash` is returned so a
    caller can check that two runs of the same inputs produced identical counters. The
    progress callback cannot reach the simulation, so passing one does not change the hash.
    """
    base = assumptions or load_assumptions()
    plan = sizing_plan(base, sizing, profile.book_size)
    configured = profile.configure(base, plan)
    report = run_experiment(
        default_strategies(configured),
        master_seed,
        configured,
        n_seeds=plan.n_seeds,
        progress=progress,
    )
    rescheduling, strategy = _lift_lines(report, configured)
    return SimulationResult(
        scheduler_reference=str(configured.value("harness.scheduler_reference_strategy")),
        baseline=str(configured.value("harness.baseline_strategy")),
        headline=str(configured.value("harness.headline_strategy")),
        rescheduling=rescheduling,
        strategy=strategy,
        months=_months(report, configured),
        strategies=_strategy_lines(report, configured),
        sizing=plan.sizing,
        book_size_simulated=plan.book_size,
        book_size_requested=plan.requested_book_size,
        book_size_capped=plan.is_capped,
        n_seeds=plan.n_seeds,
        estimated_seconds=plan.estimated_seconds,
        confidence_level=float(configured.value("harness.confidence_level")),
        master_seed=master_seed,
        seeds=report.seeds,
        config_fingerprint=configured.fingerprint(),
        output_hash=report.output_hash,
        retry_inherits_original_notice=bool(
            configured.value("compliance.pre_debit_notification.retry_inherits_original_notice")
        ),
    )


def segment_report(
    profile: MerchantProfile,
    master_seed: int,
    sizing: Sizing = Sizing.INTERACTIVE,
    assumptions: Assumptions | None = None,
) -> SegmentReport:
    """SPEC §12. One run, observed; the segments are cut from it rather than re-simulated.

    Deliberately not derived from a cached `SimulationResult`: the segment cut needs
    per-mandate episodes, which the aggregate counters have already discarded. Running once
    with an observer attached is what keeps the segments and the headline consistent.
    """
    base = assumptions or load_assumptions()
    plan = sizing_plan(base, sizing, profile.book_size)
    configured = profile.configure(base, plan)
    collector = SegmentCollector()
    run_experiment(
        default_strategies(configured),
        master_seed,
        configured,
        n_seeds=plan.n_seeds,
        observer=collector,
    )
    return build_report(
        collector, configured, int(configured.value("book.months")), master_seed
    )


class AssumptionLine(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    value: float | int | str | bool | list[int] | list[float]
    unit: str
    source: str
    confidence: str
    notes: str | None = None
    open_question: str | None = None


class AssumptionsView(BaseModel):
    """SPEC §6.3: the whole file, with sources and confidence, always visible."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int
    assumptions: tuple[AssumptionLine, ...]
    # §11 items with no key of their own, so nothing on the list is dropped for lacking
    # somewhere to hang.
    unkeyed_open_questions: tuple[str, ...]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def estimate_count(self) -> int:
        return sum(1 for a in self.assumptions if a.confidence == "estimate")


def assumptions_view(assumptions: Assumptions | None = None) -> AssumptionsView:
    a = assumptions or load_assumptions()
    questions = {q.key: q.question for q in a.open_questions if q.key is not None}
    return AssumptionsView(
        version=a.version,
        assumptions=tuple(
            _assumption_line(key, entry, questions.get(key))
            for key, entry in a.assumptions.items()
        ),
        unkeyed_open_questions=tuple(
            q.question for q in a.open_questions if q.key is None
        ),
    )


def _assumption_line(key: str, entry: Assumption, question: str | None) -> AssumptionLine:
    return AssumptionLine(
        key=key,
        value=entry.value,
        unit=entry.unit,
        source=entry.source,
        confidence=str(entry.confidence),
        notes=entry.notes,
        open_question=question,
    )


class StrategyDescription(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    role: str


def strategy_descriptions(
    assumptions: Assumptions | None = None,
) -> tuple[StrategyDescription, ...]:
    a = assumptions or load_assumptions()
    return tuple(
        StrategyDescription(name=s.name, role=_ROLES.get(s.name, "retry strategy"))
        for s in default_strategies(a)
    )
