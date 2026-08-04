"""SPEC §14.3: merchant-supplied fields expressed in the shapes the strategies demand.

The strategies and the guard are used unchanged, so this module has to hand them whole
objects — a `Mandate`, a `Bank`, a list of `DebitAttempt` — where the merchant supplied
facts. Fields no consumer reads are placeholders, and every one of them is covered by a
test that varies it and asserts the recommendation does not move (SPEC §14.3). Fields a
consumer *does* read are never invented: their absence makes a strategy unavailable, and
`availability` says which field would unlock it.
"""

from __future__ import annotations

from datetime import time, timedelta

from pydantic import BaseModel, ConfigDict, Field

from rebound.compliance.guard import RetryContext
from rebound.domain.entities import (
    AttemptOutcome,
    Bank,
    DebitAttempt,
    Mandate,
    MandateStatus,
)
from rebound.recommend.inputs import ObservedFailure
from rebound.strategies.base import CustomerObservable

# --- placeholders ---------------------------------------------------------------------
# None of these is read by any strategy this service can run, nor by any compliance rule
# except the eNACH cutoff, which is a required input. `test_placeholders.py` varies each
# and asserts the recommendation is unchanged; if one of these ever starts to matter, that
# test fails and the field has to move into the input tiers of SPEC §14.2.

PLACEHOLDER_BANK_ID = "unattested-bank"
PLACEHOLDER_MERCHANT_ID = "unattested-merchant"
PLACEHOLDER_CUSTOMER_ID = "unattested-customer"
# Non-batch rails never reach the cutoff check; eNACH refuses the input without a real one.
PLACEHOLDER_CUTOFF = time(23, 59)


class StrategyAvailability(BaseModel):
    """Whether a strategy could be run on this input, and what would change that."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy: str = Field(min_length=1)
    available: bool
    missing_fields: tuple[str, ...] = ()
    note: str = Field(min_length=1)


def build_mandate(failure: ObservedFailure) -> Mandate:
    """SPEC §14.3: status is always ACTIVE and is not an input.

    A dead mandate announces itself through its reason code — `MANDATE_REVOKED` and
    `MANDATE_EXPIRED` are hard declines that stop the chain before this is built — so a
    merchant-reported status would be a second, weaker path to the same decision.
    """
    return Mandate(
        id=failure.mandate_ref,
        merchant_id=PLACEHOLDER_MERCHANT_ID,
        customer_id=PLACEHOLDER_CUSTOMER_ID,
        rail=failure.rail,
        max_amount_paise=failure.mandate_cap_paise,
        created_at=failure.original_at,
        status=MandateStatus.ACTIVE,
    )


def build_bank(failure: ObservedFailure) -> Bank:
    """The guard reads `batch_cutoff_time` and nothing else. Everything else is placeholder."""
    return Bank(
        id=failure.bank_id or PLACEHOLDER_BANK_ID,
        name=failure.bank_id or PLACEHOLDER_BANK_ID,
        td_rate=0.0,
        uptime_profile={hour: 1.0 for hour in range(24)},
        downtime_windows=(),
        batch_cutoff_time=failure.bank_batch_cutoff_time or PLACEHOLDER_CUTOFF,
        return_charge_paise=0,
    )


def _observed_attempt(failure: ObservedFailure) -> DebitAttempt:
    return DebitAttempt(
        id=f"{failure.mandate_ref}-{failure.attempt_number}",
        mandate_id=failure.mandate_ref,
        scheduled_at=failure.failed_at,
        executed_at=failure.failed_at,
        amount_paise=failure.amount_paise,
        outcome=AttemptOutcome.FAILURE,
        reason_code=failure.reason_code,
        attempt_number=failure.attempt_number,
        is_retry=failure.attempt_number > 1,
    )


def _placeholder_priors(failure: ObservedFailure) -> list[DebitAttempt]:
    """Attempts 1..N-1 when the merchant did not itemise them.

    The merchant did tell us these attempts happened — `attempt_number` says so — and the
    strategies read only how many there were and when attempt 1 ran, both of which are
    supplied. The reason codes and the intermediate times are placeholders. The code used
    is the observed failure's own, which is soft by construction: a hard decline stops the
    chain in `service.py` before anything here is built.
    """
    count = failure.attempt_number - 1
    if count <= 0:
        return []
    span = failure.failed_at - failure.original_at
    priors = []
    for index in range(count):
        # Attempt 1 lands exactly on the supplied original time; the rest are spread
        # evenly, which is placeholder timing and nothing reads it.
        offset = timedelta() if index == 0 else span * index / count
        priors.append(
            DebitAttempt(
                id=f"{failure.mandate_ref}-{index + 1}",
                mandate_id=failure.mandate_ref,
                scheduled_at=failure.original_at + offset,
                executed_at=failure.original_at + offset,
                amount_paise=failure.amount_paise,
                outcome=AttemptOutcome.FAILURE,
                reason_code=failure.reason_code,
                attempt_number=index + 1,
                is_retry=index > 0,
            )
        )
    return priors


def _attested_priors(failure: ObservedFailure) -> list[DebitAttempt]:
    records = failure.attempt_history or ()
    return [
        DebitAttempt(
            id=f"{failure.mandate_ref}-{record.attempt_number}",
            mandate_id=failure.mandate_ref,
            scheduled_at=record.scheduled_at,
            executed_at=record.scheduled_at,
            amount_paise=record.amount_paise,
            outcome=record.outcome,
            reason_code=record.reason_code,
            attempt_number=record.attempt_number,
            is_retry=record.attempt_number > 1,
        )
        for record in records
    ]


def build_history(failure: ObservedFailure) -> list[DebitAttempt]:
    """Attempts 1..N of this cycle, the last being the failure being reacted to."""
    priors = (
        _attested_priors(failure)
        if failure.attempt_history is not None
        else _placeholder_priors(failure)
    )
    return [*priors, _observed_attempt(failure)]


def build_observable(failure: ObservedFailure, history: list[DebitAttempt]) -> CustomerObservable:
    """SPEC §14.3: `past_attempts` is empty unless the merchant attested to the attempts.

    It is the one place a placeholder would be read — `BankAware` tallies outcomes from it
    — so nothing goes in it that we made up. `inferred_salary_day` stays None here: it is a
    strategy's own inference over `past_attempts`, never something the caller supplies.
    """
    attested = failure.attempt_history is not None
    past = tuple(history) if attested else ()
    return CustomerObservable(
        customer_id=PLACEHOLDER_CUSTOMER_ID,
        bank_id=failure.bank_id or PLACEHOLDER_BANK_ID,
        rail=failure.rail,
        upi_app=None,
        has_alt_rail=False,
        past_attempts=past,
        inferred_salary_day=None,
    )


def build_context(failure: ObservedFailure, history: list[DebitAttempt]) -> RetryContext:
    return RetryContext(
        mandate=build_mandate(failure),
        bank=build_bank(failure),
        original_attempt=history[0],
        history=tuple(history),
        notified_at=failure.notified_at,
    )
