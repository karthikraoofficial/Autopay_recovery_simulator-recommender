from __future__ import annotations

import math
from collections.abc import Sequence

from pydantic import ConfigDict, Field

from rebound.config import Assumptions
from rebound.domain.entities import AttemptOutcome, DebitAttempt, DomainModel
from rebound.domain.reason_codes import ReasonCode

# Candidate salary days are 1..28 for the same calendar reason billing days are: a day
# above 28 does not exist in February, so a predicted credit on the 30th has no meaning
# in a month that has no 30th.
LAST_UNIVERSAL_DAY_OF_MONTH = 28

_DAYS_IN_CYCLE = 30


class SalaryInferenceModel(DomainModel):
    """The likelihood a merchant would fit, not the process the engine runs.

    A merchant sees outcomes and dates. It does not see the balance process, so it has
    to assume a shape for P(success | days since credit) and pick the credit day that
    best explains what it saw. Every parameter here is that assumed shape. The gap
    between it and the engine's actual balance process is not a bug to be closed — it is
    the reason inferred timing is worth less than true timing, which is the finding.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    peak: float = Field(gt=0.0, lt=1.0)
    floor: float = Field(gt=0.0, lt=1.0)
    decay_per_day: float = Field(gt=0.0)
    min_observations: int = Field(ge=1)
    min_margin: float = Field(ge=0.0)

    @classmethod
    def from_assumptions(cls, assumptions: Assumptions) -> SalaryInferenceModel:
        return cls(
            peak=float(assumptions.value("strategy.salary_aware.success_peak_probability")),
            floor=float(assumptions.value("strategy.salary_aware.success_floor_probability")),
            decay_per_day=float(assumptions.value("strategy.salary_aware.success_decay_per_day")),
            min_observations=int(assumptions.value("strategy.salary_aware.min_observations")),
            min_margin=float(assumptions.value("strategy.salary_aware.min_log_likelihood_margin")),
        )

    def success_probability(self, days_since_credit: int) -> float:
        decayed = math.exp(-self.decay_per_day * days_since_credit)
        return self.floor + (self.peak - self.floor) * decayed


def _is_informative(attempt: DebitAttempt) -> bool:
    """Only two outcomes say anything about when money arrives.

    A success means there was money; an insufficient-funds decline means there was not.
    A technical decline, a downtime window or a revocation are all silent about the
    customer's balance, and counting them would let bank noise masquerade as payroll.
    """
    if attempt.outcome is AttemptOutcome.SUCCESS:
        return True
    return attempt.reason_code is ReasonCode.INSUFFICIENT_FUNDS


def _days_since(candidate_day: int, attempt: DebitAttempt) -> int:
    """Days from the most recent occurrence of `candidate_day` to the attempt date.

    Purely calendar arithmetic on the day-of-month, so it needs no salary jitter model
    and no holiday calendar — neither of which a merchant has.
    """
    gap = attempt.scheduled_at.day - candidate_day
    return gap if gap >= 0 else gap + _DAYS_IN_CYCLE


def _log_likelihood(
    candidate_day: int, attempts: Sequence[DebitAttempt], model: SalaryInferenceModel
) -> float:
    total = 0.0
    for attempt in attempts:
        p = model.success_probability(_days_since(candidate_day, attempt))
        total += math.log(p if attempt.outcome is AttemptOutcome.SUCCESS else 1.0 - p)
    return total


def infer_salary_day(
    past_attempts: Sequence[DebitAttempt], model: SalaryInferenceModel
) -> int | None:
    """The merchant's best guess at the payroll day, or None for "I do not know".

    None is a real answer and the common one. Returning an argmax over a flat likelihood
    would dress noise up as a prediction, and a strategy acting on it would move retries
    for no reason while still being charged the cost of waiting.
    """
    informative = [a for a in past_attempts if _is_informative(a)]
    if len(informative) < model.min_observations:
        return None
    scores = {
        day: _log_likelihood(day, informative, model)
        for day in range(1, LAST_UNIVERSAL_DAY_OF_MONTH + 1)
    }
    best_day = max(scores, key=lambda day: (scores[day], -day))
    mean = sum(scores.values()) / len(scores)
    if scores[best_day] - mean < model.min_margin:
        return None
    return best_day
