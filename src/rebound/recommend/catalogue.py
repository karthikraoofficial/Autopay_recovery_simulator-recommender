"""Which strategies this service can run on which input, and how each explains itself.

SPEC §14.4: availability is reported, never a silent downgrade. A merchant who sends the
minimum set gets the best available strategy *and* the list of what better data would buy
them, because answering with `FixedSchedule` and saying nothing reads as "the system
recommends the baseline", which is a different and false claim.
"""

from __future__ import annotations

from datetime import datetime

from rebound.config import Assumptions
from rebound.recommend.adapter import StrategyAvailability
from rebound.recommend.inputs import ObservedFailure

BASELINE = "FixedSchedule"

# Strategy -> the input fields it reads beyond the minimum set. Derived from what each
# class actually touches: `BankAware` and `Blended` tally by (bank, hour) and so need both
# a bank identity and outcomes to tally; `SalaryAware` infers from attempt history alone.
EXTENDED_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "FixedSchedule": (),
    "ReasonAware": (),
    "SalaryAware": ("attempt_history",),
    "BankAware": ("bank_id", "attempt_history"),
    "Blended": ("bank_id", "attempt_history"),
}

_NOTES = {
    "FixedSchedule": "The baseline: T+1, T+3, T+7 from the original attempt. Always available.",
    "ReasonAware": "Branches on the reason code. Served by the minimum set.",
    "BankAware": "Prefers the hours that have worked for this bank.",
    "Blended": "Scores the reason-code wait, the baseline offset and the bank's best hour.",
}

# SPEC §14.4 and §11. Said in both directions on purpose: reporting the weakness only when
# the strategy runs would let its absence read as an endorsement by silence.
_SALARY_AVAILABLE = (
    "Ran, and is not selected on the strength of its inference. Phase 7 measured "
    "per-mandate salary-day inference to be unidentifiable from payment telemetry — worse "
    "than a uniform guess — because a mandate bills on one calendar day and the history is "
    "that day repeated (SPEC §11). Salary timing needs an external source, not more history."
)
_SALARY_MISSING = (
    "Unavailable for want of attempt_history. Supplying it would let the strategy run, and "
    "phase 7 measured its inference to be unidentifiable from payment telemetry anyway "
    "(SPEC §11), so it is named here for completeness rather than as an opportunity."
)


def _missing_for(strategy: str, failure: ObservedFailure) -> tuple[str, ...]:
    supplied = {
        "bank_id": failure.bank_id is not None,
        "attempt_history": failure.attempt_history is not None,
    }
    return tuple(f for f in EXTENDED_REQUIREMENTS[strategy] if not supplied[f])


def availability(failure: ObservedFailure) -> tuple[StrategyAvailability, ...]:
    """Every strategy, whether it could run, and the field that would unlock it."""
    rows = []
    for strategy in EXTENDED_REQUIREMENTS:
        missing = _missing_for(strategy, failure)
        if strategy == "SalaryAware":
            note = _SALARY_MISSING if missing else _SALARY_AVAILABLE
        elif missing:
            note = f"{_NOTES[strategy]} Unavailable for want of {', '.join(missing)}."
        else:
            note = _NOTES[strategy]
        rows.append(
            StrategyAvailability(
                strategy=strategy,
                available=not missing,
                missing_fields=missing,
                note=note,
            )
        )
    return tuple(rows)


def available_names(failure: ObservedFailure) -> frozenset[str]:
    return frozenset(row.strategy for row in availability(failure) if row.available)


def describe_rule(
    strategy: str,
    failure: ObservedFailure,
    retries_so_far: int,
    proposed_at: datetime,
    assumptions: Assumptions,
) -> str:
    """The rule that produced this time, in the terms the strategy actually reasons in.

    Written from the strategy's own configuration rather than from a template with the
    answer substituted in, so a reader can check the sentence against `assumptions.yaml`.
    """
    offsets = list(assumptions.value("strategy.fixed_schedule.retry_offsets_days"))
    offset = offsets[retries_so_far] if retries_so_far < len(offsets) else offsets[-1]
    code = failure.reason_code.value
    sentences = {
        "FixedSchedule": (
            f"retry {offset} days after the original attempt "
            f"(strategy.fixed_schedule.retry_offsets_days, position {retries_so_far + 1})"
        ),
        "ReasonAware": (
            f"{code} prescribes a wait of {proposed_at - failure.failed_at} from the "
            "failure (strategy.reason_aware.*_delay_*), independent of the baseline offset"
        ),
        "BankAware": (
            f"the {offset}-day baseline offset, moved to {proposed_at.hour:02d}:00 - the "
            "hour with the best observed success rate for this bank "
            "(strategy.bank_aware.earliest_hour..latest_hour)"
        ),
        "SalaryAware": (
            f"the {offset}-day baseline offset, shifted to the first day inside the "
            "inferred post-credit window (strategy.salary_aware.target_window_days); the "
            "inference is the one SPEC 11 measured as unidentifiable"
        ),
        "Blended": (
            f"the highest-scoring candidate among the {code} wait, the {offset}-day "
            "baseline offset and this bank's preferred hour, weighted by "
            "strategy.blended.weight_reason, .weight_salary and .weight_bank"
        ),
    }
    return sentences[strategy]
