"""SPEC §12: where in the book the opportunity sits, cut from the headline's own runs.

Not a per-customer listing and it must not become one. These customers are synthetic; a
row per customer reads as an operational action list for people who do not exist.

Three properties this module exists to preserve:

1. **Cut, never re-simulated.** Episodes arrive through `RunObserver` from the same paired
   runs that produce the headline, so the two cannot disagree.
2. **Segment assignment is strategy-invariant.** It is computed once from the scheduler
   reference run and reused, because a strategy that segmented on its own history would
   move mandates between segments and stop the comparison being within-segment.
3. **Only corrected verdicts are published.** With many segments some clear zero by
   chance. There is no uncorrected column, because if one exists it is what ends up in a
   deck.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from enum import StrEnum

import numpy as np
from pydantic import ConfigDict, Field

from rebound.config import Assumptions
from rebound.domain.entities import DomainModel, EpisodeOutcome, Mandate, RecoveryEpisode
from rebound.domain.reason_codes import ReasonCode
from rebound.seeding import stream

MONTHS_PER_YEAR = 12
PAISE_PER_RUPEE = 100
NO_FAILURES = "no failures"


class Dimension(StrEnum):
    RAIL = "rail"
    CAP_BAND = "mandate cap band"
    DOMINANT_REASON = "dominant reason code"
    FAILURE_FREQUENCY = "failure frequency"


# SPEC §12.5. Reported apart from the findings and never among them: caps are generated
# independently of everything that drives recovery, so this axis is known to be empty and
# exists to prove the significance procedure does not manufacture winners.
NEGATIVE_CONTROL = Dimension.CAP_BAND


class Verdict(StrEnum):
    INSUFFICIENT_DATA = "insufficient data"
    NO_WINNER = "no strategy beats the baseline"
    WINNER = "winner"
    LOST_TO_CORRECTION = "would win uncorrected, does not survive correction"


class _Tally:
    """Per (seed, strategy, segment) counters, accumulated as episodes arrive."""

    __slots__ = ("episodes", "recovered", "gross_paise", "at_risk_paise", "mandates")

    def __init__(self) -> None:
        self.episodes = 0
        self.recovered = 0
        self.gross_paise = 0
        self.at_risk_paise = 0
        self.mandates: set[str] = set()


class SegmentCollector:
    """A `RunObserver` that keeps only what segmentation needs.

    Deliberately not the `RecoveryEpisode` objects: a publication run produces hundreds of
    thousands of them, and the six numbers below are the whole of what §12 reports.
    """

    def __init__(self) -> None:
        self.mandates: dict[tuple[int, str], Mandate] = {}
        # (seed, strategy, mandate_id) -> episodes, for the reference-run assignment.
        self.opening_reasons: dict[tuple[int, str, str], list[ReasonCode]] = defaultdict(list)
        self.episodes: dict[tuple[int, str, str], list[tuple[bool, int]]] = defaultdict(list)

    def saw_mandate(self, strategy: str, seed: int, mandate: Mandate) -> None:
        self.mandates.setdefault((seed, mandate.id), mandate)

    def saw_episode(
        self, strategy: str, seed: int, mandate: Mandate, episode: RecoveryEpisode
    ) -> None:
        key = (seed, strategy, mandate.id)
        recovered = episode.outcome is EpisodeOutcome.RECOVERED
        self.episodes[key].append((recovered, episode.original_attempt.amount_paise))
        if episode.original_attempt.reason_code is not None:
            self.opening_reasons[key].append(episode.original_attempt.reason_code)

    def saw_cycle_guard_events(self, *_: object) -> None:
        """Not needed here. Segmentation reports how often compliance blocked a retry
        (the aggregate counters already carry that) and never which rule blocked which
        proposal, so the trace's per-verdict detail is deliberately discarded."""

    def seeds(self) -> tuple[int, ...]:
        return tuple(sorted({seed for seed, _ in self.mandates}))

    def strategies(self) -> tuple[str, ...]:
        return tuple(sorted({strategy for _, strategy, _ in self.episodes}))


def _dominant_reason(codes: Sequence[ReasonCode]) -> str:
    """Modal opening reason. Ties broken by `ReasonCode` declaration order, never by
    `Counter.most_common`, whose tie order depends on insertion and would make the label
    depend on the order episodes happened to occur."""
    if not codes:
        return NO_FAILURES
    counts = Counter(codes)
    best = max(counts.values())
    return next(code.value for code in ReasonCode if counts.get(code, 0) == best)


def _frequency_band(count: int, edges: Sequence[int]) -> str:
    if count == 0:
        return NO_FAILURES
    low = 1
    for edge in edges:
        if count < edge:
            return str(low) if low == edge - 1 else f"{low}-{edge - 1}"
        low = edge
    return f"{low}+"


def _cap_bands(
    collector: SegmentCollector, quantiles: Sequence[float]
) -> tuple[np.ndarray, tuple[str, ...], tuple[str, ...]]:
    """One set of band edges, pooled over every seed.

    Not per seed. Each seed draws its own book, so per-seed quantiles land on slightly
    different ₹ boundaries; labelling a band with them makes seed 1's "Q4" a different
    segment from seed 2's "Q4". That fragments the axis into one segment per seed, starves
    each of data, and multiplies the test count by the seed count. Pooling is also the
    honest cut: the caps are draws from one distribution, and the bands describe that
    distribution rather than any single book.

    Labels stay bare ("Q1"). The realised ₹ range is returned separately so it can be
    shown without becoming part of the segment's identity.
    """
    caps = np.array([m.max_amount_paise for m in collector.mandates.values()], dtype=float)
    edges = np.quantile(caps, quantiles)
    bounds = [float(caps.min()), *edges.tolist(), float(caps.max())]
    labels = tuple(f"Q{i + 1}" for i in range(len(bounds) - 1))
    ranges = tuple(
        f"Rs {bounds[i] / PAISE_PER_RUPEE:,.0f}-{bounds[i + 1] / PAISE_PER_RUPEE:,.0f}"
        for i in range(len(bounds) - 1)
    )
    return edges, labels, ranges


def _assign(
    collector: SegmentCollector,
    seed: int,
    reference: str,
    assumptions: Assumptions,
    cap_edges: np.ndarray,
    cap_labels: Sequence[str],
) -> dict[str, dict[Dimension, str]]:
    """Segment labels for every mandate in one seed, from the reference run only."""
    edges = [int(e) for e in assumptions.value("segment.failure_frequency_band_edges")]
    mandates = [m for (s, _), m in collector.mandates.items() if s == seed]
    assignment: dict[str, dict[Dimension, str]] = {}
    for mandate in mandates:
        key = (seed, reference, mandate.id)
        band = int(np.searchsorted(cap_edges, mandate.max_amount_paise))
        assignment[mandate.id] = {
            Dimension.RAIL: mandate.rail.value,
            Dimension.CAP_BAND: cap_labels[min(band, len(cap_labels) - 1)],
            Dimension.DOMINANT_REASON: _dominant_reason(collector.opening_reasons.get(key, [])),
            Dimension.FAILURE_FREQUENCY: _frequency_band(
                len(collector.episodes.get(key, [])), edges
            ),
        }
    return assignment


def _tally(
    collector: SegmentCollector,
    assumptions: Assumptions,
    reference: str,
    cap_edges: np.ndarray,
    cap_labels: Sequence[str],
) -> dict[tuple[int, str, Dimension, str], _Tally]:
    """Every (seed, strategy, dimension, segment) cell. One pass over the collected runs."""
    cells: dict[tuple[int, str, Dimension, str], _Tally] = defaultdict(_Tally)
    strategies = collector.strategies()
    for seed in collector.seeds():
        assignment = _assign(collector, seed, reference, assumptions, cap_edges, cap_labels)
        for mandate_id, labels in assignment.items():
            for dimension, label in labels.items():
                for strategy in strategies:
                    cell = cells[(seed, strategy, dimension, label)]
                    cell.mandates.add(mandate_id)
                    for recovered, amount in collector.episodes.get(
                        (seed, strategy, mandate_id), []
                    ):
                        cell.episodes += 1
                        cell.at_risk_paise += amount
                        if recovered:
                            cell.recovered += 1
                            cell.gross_paise += amount
    return cells


class SegmentLift(DomainModel):
    """One paired comparison inside one segment, with its corrected verdict.

    The interval is at the headline confidence level so it can be read normally; the
    verdict is corrected for the whole family of tests. Those are different questions and
    conflating them is how a segment report becomes a winner-finding machine.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy: str = Field(min_length=1)
    against: str = Field(min_length=1)
    low_inr: float
    high_inr: float
    point_inr: float
    level: float = Field(gt=0.0, lt=1.0)
    # Two-sided bootstrap p-value: the share of resamples on the far side of zero,
    # doubled. Used instead of an extreme interval quantile because a Bonferroni threshold
    # sits far into the tail, where a quantile estimated from finite resamples is unstable
    # but a tail proportion is not.
    p_value: float = Field(ge=0.0, le=1.0)
    survives_correction: bool

    @property
    def clears_zero_uncorrected(self) -> bool:
        return self.low_inr > 0.0 or self.high_inr < 0.0


class Segment(DomainModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    dimension: Dimension
    label: str = Field(min_length=1)
    # The realised range a banded label stands for, pooled across seeds. Kept out of
    # `label` so that two seeds' Q4 remain the same segment.
    label_range: str | None = None
    mandates: int = Field(ge=0)
    episodes: int = Field(ge=0)
    share_of_mandates: float = Field(ge=0.0, le=1.0)
    share_of_episodes: float = Field(ge=0.0, le=1.0)
    share_of_at_risk: float = Field(ge=0.0, le=1.0)
    at_risk_inr: float = Field(ge=0.0)
    verdict: Verdict
    # Absent when the segment is too small to support one, rather than reported wide.
    reference_recovery_rate: float | None = None
    baseline_recovery_rate: float | None = None
    rescheduling: SegmentLift | None = None
    strategy_lift: SegmentLift | None = None
    net_inr_per_mandate_year: float | None = None
    recommendation: str = Field(min_length=1)

    @property
    def is_negative_control(self) -> bool:
        return self.dimension is NEGATIVE_CONTROL


class SegmentReport(DomainModel):
    """SPEC §12. Findings and the negative control are held apart on purpose."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seeds: tuple[int, ...] = Field(min_length=1)
    months: int = Field(gt=0)
    # The configuration this report was built under. Symmetric with TraceHeader: any
    # export derived from a headline run has to be checkable against it, because a re-run
    # under changed assumptions is otherwise indistinguishable from the real thing.
    config_fingerprint: str = Field(min_length=1)
    reference_strategy: str
    baseline_strategy: str
    candidate_strategies: tuple[str, ...]
    level: float = Field(gt=0.0, lt=1.0)
    min_episodes: int = Field(ge=0)
    # SPEC §12.4: stated next to the results, not in a footnote.
    tests_performed: int = Field(ge=0)
    corrected_alpha: float = Field(ge=0.0)
    resolvable: bool
    total_episodes: int = Field(ge=0)
    total_at_risk_inr: float = Field(ge=0.0)
    segments: tuple[Segment, ...] = ()

    def for_dimension(self, dimension: Dimension) -> tuple[Segment, ...]:
        return tuple(s for s in self.segments if s.dimension is dimension)

    @property
    def findings(self) -> tuple[Segment, ...]:
        return tuple(s for s in self.segments if not s.is_negative_control)

    @property
    def negative_control(self) -> tuple[Segment, ...]:
        return tuple(s for s in self.segments if s.is_negative_control)

    @property
    def winners(self) -> tuple[Segment, ...]:
        return tuple(s for s in self.findings if s.verdict is Verdict.WINNER)


def _paired(
    values: np.ndarray, draws: np.ndarray, level: float
) -> tuple[float, float, float, float]:
    """Point, interval bounds, and the two-sided bootstrap p-value for one difference."""
    means = values[draws].mean(axis=1)
    tail = (1.0 - level) / 2.0
    low, high = np.quantile(means, [tail, 1.0 - tail])
    wrong_side = min(float((means <= 0.0).mean()), float((means >= 0.0).mean()))
    return float(values.mean()), float(low), float(high), min(1.0, 2.0 * wrong_side)


def _net_per_mandate_year(
    gross: np.ndarray, mandates: np.ndarray, fee: float, months: int
) -> float:
    """Net ₹ per mandate per year, so segments of different sizes are comparable.

    Divided by mandates and annualised, because a large segment recovering more in total
    than a small one says nothing about which is worth acting on.
    """
    with np.errstate(divide="ignore", invalid="ignore"):
        per = np.where(mandates > 0, gross * (1.0 - fee) / np.maximum(mandates, 1), 0.0)
    return float(per.mean()) * (MONTHS_PER_YEAR / months) / PAISE_PER_RUPEE


def _recommend(segment_label: str, verdict: Verdict, lift: SegmentLift | None,
               rescheduling: SegmentLift | None, episodes: int, minimum: int) -> str:
    """Derived from the measured result. Each verdict says something materially different;
    none of these is the same sentence with a different number dropped into it."""
    if verdict is Verdict.INSUFFICIENT_DATA:
        return (
            f"Only {episodes} episodes fell in this segment, below the {minimum} needed to "
            f"support an interval. Nothing is claimed about it. Widen the band or run more "
            f"seeds if this segment matters."
        )
    if verdict is Verdict.LOST_TO_CORRECTION and lift is not None:
        return (
            f"{lift.strategy} looks ahead of {lift.against} here on its own, but the "
            f"comparison does not survive correction for the number of segments tested. "
            f"Treat it as untested rather than as a small win: this is exactly the pattern "
            f"a large enough segment report produces by chance."
        )
    if verdict is Verdict.NO_WINNER:
        if rescheduling is not None and rescheduling.survives_correction:
            return (
                "No retry strategy beats the baseline in this segment. Rescheduling blocked "
                "retries does pay here, and it needs no strategy at all - it is the whole of "
                "what this segment is worth."
            )
        return (
            "Nothing measurable is available in this segment. Neither rescheduling nor any "
            "retry strategy separates from the baseline. Spending effort here is not "
            "supported by the run."
        )
    if lift is not None:
        return (
            f"{lift.strategy} beats {lift.against} in this segment, and the comparison "
            f"survives correction for every test in the report. This is the strongest kind "
            f"of result the segment analysis can produce; it remains a claim about a "
            f"simulated book, not a measurement of a real one."
        )
    return "No result."


def _series(cells: dict, seeds: Sequence[int], strategy: str, dimension: Dimension,
            label: str, field: str) -> np.ndarray:
    return np.array(
        [getattr(cells[(s, strategy, dimension, label)], field, 0) for s in seeds], dtype=float
    )


def _mandate_counts(cells: dict, seeds: Sequence[int], strategy: str, dimension: Dimension,
                    label: str) -> np.ndarray:
    return np.array([len(cells[(s, strategy, dimension, label)].mandates) for s in seeds], float)


def _lift(cells: dict, seeds: Sequence[int], subject: str, against: str, dimension: Dimension,
          label: str, draws: np.ndarray, level: float, fee: float, months: int,
          alpha: float) -> SegmentLift:
    """One paired difference in net ₹ per mandate per year, inside one segment."""
    mandates = _mandate_counts(cells, seeds, subject, dimension, label)
    scale = (1.0 - fee) * (MONTHS_PER_YEAR / months) / PAISE_PER_RUPEE
    per = lambda name: np.where(  # noqa: E731 - a local, used twice, named for the reader
        mandates > 0,
        _series(cells, seeds, name, dimension, label, "gross_paise") / np.maximum(mandates, 1),
        0.0,
    ) * scale
    point, low, high, p = _paired(per(subject) - per(against), draws, level)
    return SegmentLift(
        strategy=subject,
        against=against,
        low_inr=low,
        high_inr=high,
        point_inr=point,
        level=level,
        p_value=p,
        survives_correction=p <= alpha,
    )


def _pick_winner(lifts: Sequence[SegmentLift]) -> tuple[SegmentLift | None, Verdict]:
    """The best candidate, and what may honestly be said about it.

    Ranked on p-value rather than on point estimate: with segments of very different sizes
    the largest point estimate is routinely the noisiest one, and picking it would be the
    winner-finding behaviour §12.4 exists to prevent.
    """
    if not lifts:
        return None, Verdict.NO_WINNER
    survivors = [line for line in lifts if line.survives_correction and line.point_inr > 0]
    if survivors:
        return min(survivors, key=lambda line: line.p_value), Verdict.WINNER
    raw = [line for line in lifts if line.clears_zero_uncorrected and line.point_inr > 0]
    if raw:
        return min(raw, key=lambda line: line.p_value), Verdict.LOST_TO_CORRECTION
    return None, Verdict.NO_WINNER


def _labels(cells: dict) -> dict[Dimension, list[str]]:
    found: dict[Dimension, set[str]] = defaultdict(set)
    for _, _, dimension, label in cells:
        found[dimension].add(label)
    return {dimension: sorted(labels) for dimension, labels in found.items()}


def build_report(
    collector: SegmentCollector, assumptions: Assumptions, months: int, master_seed: int
) -> SegmentReport:
    """SPEC §12, from an observed run. Does not simulate anything.

    The Bonferroni family is every segment × every candidate strategy across all
    dimensions at once, including the negative control: a reader scanning the page is
    running all of them, whatever the dimensions are nominally called.
    """
    reference = str(assumptions.value("harness.scheduler_reference_strategy"))
    baseline = str(assumptions.value("harness.baseline_strategy"))
    floor = str(assumptions.value("harness.floor_strategy"))
    level = float(assumptions.value("harness.confidence_level"))
    fee = float(assumptions.value("harness.performance_fee_rate"))
    minimum = int(assumptions.value("segment.min_episodes"))
    resamples = int(assumptions.value("segment.bootstrap_resamples"))
    seeds = collector.seeds()
    quantiles = [float(q) for q in assumptions.value("segment.cap_band_quantiles")]
    cap_edges, cap_labels, cap_ranges = _cap_bands(collector, quantiles)
    ranges = dict(zip(cap_labels, cap_ranges, strict=True))
    cells = _tally(collector, assumptions, reference, cap_edges, cap_labels)
    labels = _labels(cells)
    # References are excluded, not just the baseline. Asking whether the no-retry floor
    # beats the baseline has a known answer, and spending a Bonferroni test on it makes
    # every real comparison in the report stricter for nothing.
    references = {reference, baseline, floor}
    candidates = tuple(s for s in collector.strategies() if s not in references)

    tests = sum(len(names) for names in labels.values()) * max(1, len(candidates))
    alpha = (1.0 - level) / tests if tests else (1.0 - level)
    rng = stream(master_seed, "segments", "bootstrap")
    draws = rng.integers(0, len(seeds), size=(resamples, len(seeds)))

    totals = _totals(cells, seeds, labels, baseline)
    segments = [
        _segment(cells, seeds, dimension, label, baseline, reference, candidates, draws,
                 level, fee, months, alpha, minimum, totals, ranges.get(label))
        for dimension in sorted(labels, key=lambda d: d.value)
        for label in labels[dimension]
    ]
    return SegmentReport(
        seeds=seeds,
        months=months,
        config_fingerprint=assumptions.fingerprint(),
        reference_strategy=reference,
        baseline_strategy=baseline,
        candidate_strategies=candidates,
        level=level,
        min_episodes=minimum,
        tests_performed=tests,
        corrected_alpha=alpha,
        # A bootstrap p-value cannot go below 1/resamples, so a threshold under that
        # cannot be tested at all. Reported rather than silently rounded to "significant".
        resolvable=alpha >= 1.0 / resamples,
        total_episodes=totals["episodes"],
        total_at_risk_inr=totals["at_risk_paise"] / PAISE_PER_RUPEE,
        segments=tuple(segments),
    )


def _totals(cells: dict, seeds: Sequence[int], labels: dict, baseline: str) -> dict[str, int]:
    """Run totals, taken from one dimension. Every dimension partitions the same mandates,
    so any of them sums to the same answer — which is what the reconciliation test checks."""
    dimension = Dimension.RAIL
    return {
        "episodes": int(
            sum(
                _series(cells, seeds, baseline, dimension, x, "episodes").sum()
                for x in labels[dimension]
            )
        ),
        "at_risk_paise": int(
            sum(
                _series(cells, seeds, baseline, dimension, x, "at_risk_paise").sum()
                for x in labels[dimension]
            )
        ),
        "mandates": int(
            sum(
                _mandate_counts(cells, seeds, baseline, dimension, x).sum()
                for x in labels[dimension]
            )
        ),
    }


def _rate(cells: dict, seeds: Sequence[int], strategy: str, dimension: Dimension,
          label: str) -> float | None:
    episodes = _series(cells, seeds, strategy, dimension, label, "episodes").sum()
    if episodes == 0:
        return None
    return float(_series(cells, seeds, strategy, dimension, label, "recovered").sum() / episodes)


def _segment(cells: dict, seeds: Sequence[int], dimension: Dimension, label: str,
             baseline: str, reference: str, candidates: Sequence[str], draws: np.ndarray,
             level: float, fee: float, months: int, alpha: float, minimum: int,
             totals: dict[str, int], label_range: str | None) -> Segment:
    episodes = int(_series(cells, seeds, baseline, dimension, label, "episodes").sum())
    at_risk = float(_series(cells, seeds, baseline, dimension, label, "at_risk_paise").sum())
    mandates = int(_mandate_counts(cells, seeds, baseline, dimension, label).sum())
    common = dict(
        dimension=dimension,
        label=label,
        label_range=label_range if dimension is NEGATIVE_CONTROL else None,
        mandates=mandates // max(1, len(seeds)),
        episodes=episodes,
        share_of_mandates=mandates / totals["mandates"] if totals["mandates"] else 0.0,
        share_of_episodes=episodes / totals["episodes"] if totals["episodes"] else 0.0,
        share_of_at_risk=at_risk / totals["at_risk_paise"] if totals["at_risk_paise"] else 0.0,
        at_risk_inr=at_risk / PAISE_PER_RUPEE,
    )
    if episodes < minimum:
        return Segment(
            **common,
            verdict=Verdict.INSUFFICIENT_DATA,
            recommendation=_recommend(
                label, Verdict.INSUFFICIENT_DATA, None, None, episodes, minimum
            ),
        )

    rescheduling = _lift(cells, seeds, baseline, reference, dimension, label, draws,
                         level, fee, months, alpha)
    lifts = [
        _lift(cells, seeds, name, baseline, dimension, label, draws, level, fee, months, alpha)
        for name in candidates
    ]
    winner, verdict = _pick_winner(lifts)
    gross = _series(cells, seeds, baseline, dimension, label, "gross_paise")
    return Segment(
        **common,
        verdict=verdict,
        reference_recovery_rate=_rate(cells, seeds, reference, dimension, label),
        baseline_recovery_rate=_rate(cells, seeds, baseline, dimension, label),
        rescheduling=rescheduling,
        strategy_lift=winner,
        net_inr_per_mandate_year=_net_per_mandate_year(
            gross, _mandate_counts(cells, seeds, baseline, dimension, label), fee, months
        ),
        recommendation=_recommend(label, verdict, winner, rescheduling, episodes, minimum),
    )


# SPEC §12.1: the cap axis is a stand-in for ticket size, and a reader who does not know
# that will read it as one. Printed with the axis, every time, not once in a preamble.
CAP_BAND_NOTE = (
    "mandate cap band is a PROXY for ticket size: debit amount is constant at "
    "book.avg_ticket_paise in the current population model, so it cannot be banded. "
    "See SPEC.md section 11."
)


def _interval(lift: SegmentLift | None) -> str:
    if lift is None:
        return "n/a"
    return f"[Rs {lift.low_inr:,.0f}, Rs {lift.high_inr:,.0f}]"


def _wrap(text: str, width: int, indent: str) -> list[str]:
    lines, current = [], indent
    for word in text.split():
        if len(current) + len(word) + 1 > width and current != indent:
            lines.append(current)
            current = indent
        current += ("" if current == indent else " ") + word
    return [*lines, current]


def render(report: SegmentReport, width: int = 96) -> str:
    """Plain text. No dependency, and readable in a terminal or pasted into an email."""
    out: list[str] = ["SEGMENT REPORT (SPEC section 12)", "=" * width, ""]
    out += [
        f"seeds {len(report.seeds)}  horizon {report.months} months  "
        f"CI level {report.level:.0%}  min episodes {report.min_episodes}",
        f"reference {report.reference_strategy}  baseline {report.baseline_strategy}  "
        f"candidates {', '.join(report.candidate_strategies)}",
        f"total episodes {report.total_episodes:,}  at risk Rs {report.total_at_risk_inr:,.0f}",
        "",
        f"config_fingerprint: {report.config_fingerprint}",
        "",
        f"*** {report.tests_performed} TESTS PERFORMED. Every verdict below is corrected for "
        f"all of them (Bonferroni, alpha {report.corrected_alpha:.5f}). ***",
    ]
    if not report.resolvable:
        out.append(
            "*** WARNING: segment.bootstrap_resamples is too low to resolve that threshold. "
            "Verdicts are not trustworthy. Raise it. ***"
        )
    out += ["", "-" * width, "FINDINGS", "-" * width]
    out += _render_dimensions(report, report.findings, width)
    out += ["", "-" * width, "NEGATIVE CONTROL - EXPECTED NULL", "-" * width]
    out += _wrap(
        "This axis carries no signal by construction and is not a finding. It is here to "
        "check the statistics: if anything below wins, the correction is broken.", width, ""
    )
    out += _render_dimensions(report, report.negative_control, width)
    return "\n".join(out)


def _render_dimensions(report: SegmentReport, segments: Sequence[Segment], width: int) -> list[str]:
    out: list[str] = []
    for dimension in sorted({s.dimension for s in segments}, key=lambda d: d.value):
        out += ["", f"## by {dimension.value}"]
        if dimension is NEGATIVE_CONTROL:
            out += _wrap(CAP_BAND_NOTE, width, "   ")
        for segment in [s for s in segments if s.dimension is dimension]:
            out += _render_segment(segment, width)
    return out


def _render_segment(segment: Segment, width: int) -> list[str]:
    out = [
        "",
        f"  {segment.label}" + (f"  ({segment.label_range})" if segment.label_range else ""),
        f"    {segment.share_of_mandates:6.1%} of mandates | "
        f"{segment.share_of_episodes:6.1%} of episodes | "
        f"{segment.share_of_at_risk:6.1%} of Rs at risk (Rs {segment.at_risk_inr:,.0f})",
    ]
    if segment.verdict is not Verdict.INSUFFICIENT_DATA:
        out += [
            f"    recovery rate: {segment.reference_recovery_rate:.1%} under reference -> "
            f"{segment.baseline_recovery_rate:.1%} under baseline",
            f"    rescheduling lift {_interval(segment.rescheduling)} per mandate/year",
            f"    strategy lift    {_interval(segment.strategy_lift)} per mandate/year"
            + (f"  [{segment.strategy_lift.strategy}]" if segment.strategy_lift else ""),
            f"    net to merchant  Rs {segment.net_inr_per_mandate_year:,.0f} per mandate/year",
        ]
    out += [f"    VERDICT: {segment.verdict.value}"]
    out += _wrap(segment.recommendation, width, "      ")
    return out
