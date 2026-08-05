"""SPEC §14: one observed failure in, one recommended retry time out.

Three things this module exists to hold in place:

1. **A time, never a probability** (§14.1). There is no oracle outside the simulator: in
   it, P(success) is knowable because the balance process generated the answer; out here
   nothing in this repo has ever seen a real payment succeed.
2. **The guard applies** (§14.5). A simulated illegal retry costs a wrong number; a
   recommended illegal retry gets executed against a real customer.
3. **The quoted lift belongs to the segment, not to the rule** (§14.6). No experiment here
   isolates one branch of one strategy, so the response names both.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from rebound.compliance.guard import ComplianceGuard, ComplianceReschedule
from rebound.config import Assumptions, load_assumptions
from rebound.domain.entities import DebitAttempt
from rebound.domain.reason_codes import terminates_retry_chain
from rebound.harness.segments import Dimension, Segment, SegmentReport, Verdict
from rebound.recommend import evidence as ev
from rebound.recommend.adapter import (
    StrategyAvailability,
    build_context,
    build_history,
    build_mandate,
    build_observable,
)
from rebound.recommend.catalogue import (
    BASELINE,
    availability,
    available_names,
    describe_rule,
)
from rebound.recommend.inputs import ObservedFailure, Tier
from rebound.strategies.bank_aware import BankAware
from rebound.strategies.base import ProposedRetry, RetryStrategy
from rebound.strategies.blended import Blended
from rebound.strategies.fixed_schedule import FixedSchedule
from rebound.strategies.reason_aware import ReasonAware
from rebound.strategies.salary_aware import SalaryAware

_STRATEGIES: dict[str, type] = {
    "FixedSchedule": FixedSchedule,
    "ReasonAware": ReasonAware,
    "SalaryAware": SalaryAware,
    "BankAware": BankAware,
    "Blended": Blended,
}

# SPEC §14.7. Carried in the response rather than in documentation, because the response
# is what gets forwarded.
CAVEAT_SIMULATED = (
    "Measured on a simulated book of synthetic mandates. It is not a measurement of your "
    "book and does not become one by being quoted at you."
)
CAVEAT_BALANCE = (
    "Two of the three assumptions the headline is most fragile to are population.balance.* "
    "- the part of the model with no empirical grounding at all (SPEC 11). Every lift "
    "figure here inherits that fragility."
)
CAVEAT_BLENDED = (
    "Blended's three strategy.blended.weight_* keys have no corrected sensitivity sweep: "
    "the first run of them was void and the corrected run is outstanding (SPEC 11)."
)
CAVEAT_ONE_CYCLE = (
    "The dominant-reason segment is assigned from this one cycle. SPEC 12.1 defines it as "
    "the mode over a mandate's opening attempts across a 12-month horizon; one failure is "
    "not a mode, so this is an approximation of that assignment."
)
CAVEAT_NOTICE = (
    "This retry lands less than the notification lead time after the failure. It is legal "
    "under the shipped reading of the pre-debit rule "
    "(compliance.pre_debit_notification.retry_inherits_original_notice: true), which SPEC "
    "11 records as unverified and the largest single regulatory risk in the model. Under "
    "the strict reading it would be illegal."
)


class Status(StrEnum):
    RECOMMENDED = "recommended"
    STOP_HARD_DECLINE = "stop, hard decline"
    BLOCKED = "blocked by compliance"
    NO_PROPOSAL = "no retry proposed"


class ComplianceNote(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    rule: str = Field(min_length=1)
    source_key: str = Field(min_length=1)
    detail: str = Field(min_length=1)
    # Set when a timing rule moved the retry rather than killing it. The time originally
    # proposed is never returned in its place: it is not a time we are willing to advise.
    moved_from: AwareDatetime | None = None


class EvidenceQuote(BaseModel):
    """The measured lift in the segment this failure belongs to (SPEC §14.6).

    Never the rule's own effect — no experiment in this repo isolates one. Interval before
    point estimate, as everywhere else in this project.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    dimension: str
    segment_label: str
    verdict: str
    sentence: str = Field(min_length=1)
    measured_strategy: str | None = None
    against: str | None = None
    low_inr: float | None = None
    high_inr: float | None = None
    point_inr: float | None = None
    level: float | None = None
    survives_correction: bool | None = None
    seeds: int = Field(ge=1)
    tests_performed: int = Field(ge=0)


class Recommendation(BaseModel):
    """SPEC §14.1: a time and the rule behind it. No field here is a prediction."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mandate_ref: str = Field(min_length=1)
    status: Status
    retry_at: AwareDatetime | None = None
    strategy: str | None = None
    rule: str = Field(min_length=1)
    tier: Tier
    compliance: ComplianceNote | None = None
    evidence: EvidenceQuote | None = None
    availability: tuple[StrategyAvailability, ...]
    selection_notes: tuple[str, ...] = ()
    caveats: tuple[str, ...]
    evidence_fingerprint: str = Field(min_length=1)
    # SPEC §15.4. Echoed beside the evidence fingerprint so a recommendation can be traced
    # to both of the things that decide it: the measurement that justified it, and the
    # vocabulary the input was read through. `None` means no mapping was applied and the
    # values arrived already canonical, which is the case for POST /recommend.
    mapping_fingerprint: str | None = None


class _Selection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy: str
    segment: Segment | None = None
    dimension: Dimension | None = None
    notes: tuple[str, ...] = ()


def _verdict_sentence(segment: Segment, report: SegmentReport) -> str:
    tests = report.tests_performed
    lift = segment.strategy_lift
    if segment.verdict is Verdict.WINNER and lift is not None:
        return (
            f"{lift.strategy} beat {BASELINE} in this segment and survived correction "
            f"across all {tests} tests in the report."
        )
    if segment.verdict is Verdict.LOST_TO_CORRECTION and lift is not None:
        return (
            f"{lift.strategy}'s interval clears zero on its own but does not survive "
            f"correction across all {tests} tests, so no strategy beats {BASELINE} here."
        )
    if segment.verdict is Verdict.INSUFFICIENT_DATA:
        return (
            f"Insufficient data: only {segment.episodes} episodes fell in this segment, "
            f"below the {report.min_episodes} needed to support an interval. Nothing is "
            "claimed about it."
        )
    return f"No strategy beats {BASELINE} here."


def _quote(segment: Segment, dimension: Dimension, report: SegmentReport) -> EvidenceQuote:
    lift = segment.strategy_lift if segment.verdict is Verdict.WINNER else None
    return EvidenceQuote(
        dimension=dimension.value,
        segment_label=segment.label,
        verdict=segment.verdict.value,
        sentence=_verdict_sentence(segment, report),
        measured_strategy=lift.strategy if lift else None,
        against=lift.against if lift else None,
        low_inr=lift.low_inr if lift else None,
        high_inr=lift.high_inr if lift else None,
        point_inr=lift.point_inr if lift else None,
        level=lift.level if lift else None,
        survives_correction=lift.survives_correction if lift else None,
        seeds=len(report.seeds),
        tests_performed=report.tests_performed,
    )


def _unassigned_note() -> str:
    return (
        "The dominant-reason segment could not be assigned: this failure is a retry and "
        "the opening attempt's reason code was not supplied. Send attempt_history to place "
        "it on that dimension."
    )


def _unavailable_winner_note(
    winner: str, dimension: Dimension, segment: Segment, missing: tuple[str, ...]
) -> str:
    return (
        f"{winner} is the measured winner in the {dimension.value} segment "
        f"'{segment.label}' and cannot be run on this input. Supplying "
        f"{', '.join(missing)} would unlock it."
    )


def _select(
    failure: ObservedFailure, report: SegmentReport, assumptions: Assumptions
) -> _Selection:
    """The first segment on SPEC §14.6's precedence whose measured winner we can run.

    Falls through to the baseline, which is what the merchant is already doing. A winner we
    cannot run is reported rather than skipped, because *which field would unlock it* is
    the most useful thing this service can tell a merchant on a minimum-set input.
    """
    labels = ev.segment_labels(failure, assumptions)
    runnable = available_names(failure)
    rows = availability(failure)
    notes: list[str] = []
    fallback: tuple[Segment, Dimension] | None = None
    if Dimension.DOMINANT_REASON not in labels:
        notes.append(_unassigned_note())
    for dimension in ev.PRECEDENCE:
        label = labels.get(dimension)
        segment = ev.find_segment(report, dimension, label) if label else None
        if segment is None:
            continue
        fallback = fallback or (segment, dimension)
        winner = ev.winning_strategy(segment)
        if winner is None or winner == BASELINE:
            continue
        if winner in runnable:
            return _Selection(
                strategy=winner, segment=segment, dimension=dimension, notes=tuple(notes)
            )
        missing = next(r.missing_fields for r in rows if r.strategy == winner)
        notes.append(_unavailable_winner_note(winner, dimension, segment, missing))
    segment, dimension = fallback if fallback else (None, None)
    return _Selection(strategy=BASELINE, segment=segment, dimension=dimension, notes=tuple(notes))


def _caveats(
    failure: ObservedFailure,
    selection: _Selection,
    retry_at: datetime | None,
    assumptions: Assumptions,
) -> tuple[str, ...]:
    caveats = [CAVEAT_SIMULATED, CAVEAT_BALANCE]
    named = {selection.strategy}
    if selection.segment is not None and selection.segment.strategy_lift is not None:
        named.add(selection.segment.strategy_lift.strategy)
    if "Blended" in named:
        caveats.append(CAVEAT_BLENDED)
    if selection.dimension is Dimension.DOMINANT_REASON:
        caveats.append(CAVEAT_ONE_CYCLE)
    lead_hours = float(assumptions.value("compliance.pre_debit_notification.lead_hours"))
    if retry_at is not None and retry_at - failure.failed_at < timedelta(hours=lead_hours):
        caveats.append(CAVEAT_NOTICE)
    return tuple(caveats)


def _hard_decline(
    failure: ObservedFailure, fingerprint: str, mapping_fingerprint: str | None = None
) -> Recommendation:
    """SPEC §1.3/§14.5: no time at all, and no lift quoted beside a chain that has ended."""
    return Recommendation(
        mandate_ref=failure.mandate_ref,
        status=Status.STOP_HARD_DECLINE,
        rule=(
            f"{failure.reason_code.value} is a hard decline and terminates the retry chain "
            "(compliance.hard_decline_stop.rule, absolute - SPEC 1.3). Route to dunning or "
            "re-authorisation, not to a retry."
        ),
        tier=failure.tier,
        availability=availability(failure),
        caveats=(CAVEAT_SIMULATED,),
        evidence_fingerprint=fingerprint,
        mapping_fingerprint=mapping_fingerprint,
    )


def recommend(
    failure: ObservedFailure,
    report: SegmentReport,
    assumptions: Assumptions | None = None,
    mapping_fingerprint: str | None = None,
) -> Recommendation:
    """One failure in, one time out. Deterministic: nothing here reads the wall clock."""
    assumptions = assumptions or load_assumptions()
    if terminates_retry_chain(failure.reason_code):
        return _hard_decline(failure, report.config_fingerprint, mapping_fingerprint)
    selection = _select(failure, report, assumptions)
    history = build_history(failure)
    strategy: RetryStrategy = _STRATEGIES[selection.strategy](assumptions)
    proposals = strategy.propose_retries(
        failed_attempt=history[-1],
        mandate=build_mandate(failure),
        customer_view=build_observable(failure, history),
        history=history,
        # SPEC §14.10: bound to the failure, never to the wall clock.
        clock=failure.failed_at,
    )
    return _answer(
        failure, selection, proposals, history, report, assumptions, mapping_fingerprint
    )


def _answer(
    failure: ObservedFailure,
    selection: _Selection,
    proposals: list[ProposedRetry],
    history: list[DebitAttempt],
    report: SegmentReport,
    assumptions: Assumptions,
    mapping_fingerprint: str | None = None,
) -> Recommendation:
    """Put the proposal through the guard (SPEC §14.5) and assemble the response."""
    common = _common_fields(failure, selection, report, mapping_fingerprint)
    if not proposals:
        return Recommendation(
            status=Status.NO_PROPOSAL,
            rule=(
                f"{selection.strategy} proposes no further retry after attempt "
                f"{failure.attempt_number}: its retry limit for this cycle is reached, or "
                f"{failure.reason_code.value} prescribes no retry."
            ),
            caveats=_caveats(failure, selection, None, assumptions),
            **common,
        )
    guard = ComplianceGuard(assumptions)
    allowed = guard.filter(proposals, build_context(failure, history))
    retries_so_far = failure.attempt_number - 1
    when = allowed.scheduled_at if allowed is not None else proposals[0].scheduled_at
    rule = describe_rule(selection.strategy, failure, retries_so_far, when, assumptions)
    if allowed is None:
        block = guard.blocks[-1]
        return Recommendation(
            status=Status.BLOCKED,
            rule=rule,
            compliance=ComplianceNote(
                rule=block.rule, source_key=block.source_key, detail=block.detail
            ),
            caveats=_caveats(failure, selection, None, assumptions),
            **common,
        )
    moved = next((r for r in guard.reschedules if r.rescheduled_at == when), None)
    return Recommendation(
        status=Status.RECOMMENDED,
        retry_at=when,
        rule=rule,
        compliance=_moved_note(moved, guard),
        caveats=_caveats(failure, selection, when, assumptions),
        **common,
    )


def _common_fields(
    failure: ObservedFailure,
    selection: _Selection,
    report: SegmentReport,
    mapping_fingerprint: str | None,
) -> dict[str, object]:
    """The fields every answer carries, whatever the outcome — including the refusals."""
    return {
        "mandate_ref": failure.mandate_ref,
        "strategy": selection.strategy,
        "tier": failure.tier,
        "evidence": (
            _quote(selection.segment, selection.dimension, report)
            if selection.segment is not None and selection.dimension is not None
            else None
        ),
        "availability": availability(failure),
        "selection_notes": selection.notes,
        "evidence_fingerprint": report.config_fingerprint,
        "mapping_fingerprint": mapping_fingerprint,
    }


def _moved_note(
    moved: ComplianceReschedule | None, guard: ComplianceGuard
) -> ComplianceNote | None:
    """A reschedule reported as what it is: revenue still collected, later (SPEC §3)."""
    if moved is None:
        return None
    return ComplianceNote(
        rule=moved.rule,
        source_key=guard.rule_sources[moved.rule],
        detail=f"moved from {moved.proposed_at.isoformat()} to the next presentable slot",
        moved_from=moved.proposed_at,
    )
