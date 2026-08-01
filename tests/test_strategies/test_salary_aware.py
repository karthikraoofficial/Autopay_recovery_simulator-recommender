"""SPEC §4.4 SalaryAware: infer the credit day from observables, retry just after it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from rebound.config import Assumptions
from rebound.domain.entities import (
    AttemptOutcome,
    DebitAttempt,
    Mandate,
    Rail,
)
from rebound.domain.reason_codes import HARD_DECLINE_CODES, ReasonCode
from rebound.strategies.base import CustomerObservable
from rebound.strategies.fixed_schedule import FixedSchedule
from rebound.strategies.salary_aware import SalaryAware
from rebound.strategies.salary_inference import SalaryInferenceModel, infer_salary_day

BILLED_AT = datetime(2026, 4, 20, 17, 0, tzinfo=UTC)


def _mandate() -> Mandate:
    return Mandate(
        id="mandate-000000",
        merchant_id="merchant-000",
        customer_id="cust-000000",
        rail=Rail.UPI_AUTOPAY,
        max_amount_paise=1_000_000,
        created_at=datetime(2026, 1, 20, tzinfo=UTC),
    )


def _attempt(
    when: datetime,
    outcome: AttemptOutcome = AttemptOutcome.FAILURE,
    code: ReasonCode | None = ReasonCode.INSUFFICIENT_FUNDS,
    number: int = 1,
) -> DebitAttempt:
    return DebitAttempt(
        id=f"a-{when.isoformat()}-{number}",
        mandate_id="mandate-000000",
        scheduled_at=when,
        executed_at=when,
        amount_paise=49900,
        outcome=outcome,
        reason_code=code if outcome is AttemptOutcome.FAILURE else None,
        attempt_number=number,
        is_retry=number > 1,
    )


def _view(past: tuple[DebitAttempt, ...] = ()) -> CustomerObservable:
    return CustomerObservable(
        customer_id="cust-000000",
        bank_id="bank-000",
        rail=Rail.UPI_AUTOPAY,
        past_attempts=past,
    )


def _propose(strategy: SalaryAware, failed: DebitAttempt, past: tuple[DebitAttempt, ...] = ()):
    return strategy.propose_retries(
        failed_attempt=failed,
        mandate=_mandate(),
        customer_view=_view(past),
        history=[failed],
        clock=failed.scheduled_at,
    )


def _history_for_salary_day(day: int, months: int = 6) -> tuple[DebitAttempt, ...]:
    """Attempts consistent with payroll on `day`: succeeds just after it, fails long
    after. This is the signal the inference is supposed to find."""
    attempts = []
    for i in range(months):
        month = 1 + i
        soon = (day % 28) + 1
        late = ((day + 16) % 28) + 1
        good = datetime(2026, month, soon, 17, tzinfo=UTC)
        attempts.append(_attempt(good, AttemptOutcome.SUCCESS))
        attempts.append(_attempt(datetime(2026, month, late, 17, tzinfo=UTC)))
    return tuple(attempts)


@pytest.mark.parametrize("code", sorted(HARD_DECLINE_CODES))
def test_a_hard_decline_stops_the_chain(shipped: Assumptions, code: ReasonCode) -> None:
    assert _propose(SalaryAware(shipped), _attempt(BILLED_AT, code=code)) == []


def test_with_no_history_it_falls_back_to_the_baseline_offset(shipped: Assumptions) -> None:
    """The floor is FixedSchedule, so any measured difference is the inference and not a
    different retry cadence."""
    offsets = list(shipped.value("strategy.fixed_schedule.retry_offsets_days"))
    [retry] = _propose(SalaryAware(shipped), _attempt(BILLED_AT))
    assert retry.scheduled_at == BILLED_AT + timedelta(days=offsets[0])


def test_a_clean_signal_moves_the_retry_into_the_post_credit_window(
    shipped: Assumptions,
) -> None:
    window = int(shipped.value("strategy.salary_aware.target_window_days"))
    model = SalaryInferenceModel.from_assumptions(shipped)
    # A credit day reachable from the baseline offset within max_wait_days. A credit that
    # has just passed is not reachable at all, which `test_the_wait_is_capped` covers.
    past = _history_for_salary_day(25)
    inferred = infer_salary_day(past, model)
    assert inferred is not None
    [retry] = _propose(SalaryAware(shipped), _attempt(BILLED_AT), past)
    assert (retry.scheduled_at.day - inferred) % 30 <= window


def test_it_never_retries_earlier_than_the_baseline_would(shipped: Assumptions) -> None:
    """Pulling a retry forward to catch a credit that has already passed would be reading
    the past; the money is gone by then."""
    offsets = list(shipped.value("strategy.fixed_schedule.retry_offsets_days"))
    strategy = SalaryAware(shipped)
    for day in range(1, 29):
        [retry] = _propose(strategy, _attempt(BILLED_AT), _history_for_salary_day(day))
        assert retry.scheduled_at >= BILLED_AT + timedelta(days=offsets[0])


def test_the_wait_is_capped(shipped: Assumptions) -> None:
    offsets = list(shipped.value("strategy.fixed_schedule.retry_offsets_days"))
    cap = int(shipped.value("strategy.salary_aware.max_wait_days"))
    strategy = SalaryAware(shipped)
    earliest = BILLED_AT + timedelta(days=offsets[0])
    for day in range(1, 29):
        [retry] = _propose(strategy, _attempt(BILLED_AT), _history_for_salary_day(day))
        assert retry.scheduled_at - earliest <= timedelta(days=cap)


def test_the_retry_amount_always_equals_the_original(shipped: Assumptions) -> None:
    [retry] = _propose(SalaryAware(shipped), _attempt(BILLED_AT), _history_for_salary_day(5))
    assert retry.amount_paise == 49900


def test_it_cannot_read_the_true_salary_day(shipped: Assumptions) -> None:
    """The boundary that matters. `CustomerObservable` has no salary_credit_day field, so
    a strategy reaching for one does not compile away quietly — it raises here."""
    with pytest.raises(ValueError):
        CustomerObservable(
            customer_id="c", bank_id="b", rail=Rail.UPI_AUTOPAY, salary_credit_day=15
        )


class TestInference:
    def test_it_refuses_to_guess_from_too_little_history(self, shipped: Assumptions) -> None:
        model = SalaryInferenceModel.from_assumptions(shipped)
        assert infer_salary_day((_attempt(BILLED_AT),), model) is None

    def test_a_single_billing_day_collapses_the_estimate_onto_that_day(
        self, shipped: Assumptions
    ) -> None:
        """A known, unfixable bias, asserted so it cannot be forgotten.

        Every mandate bills on one day of the month, so a book where debits mostly
        succeed gives the inference one day-of-month and one outcome. The likelihood is
        then maximised by placing payroll immediately before that day — because success
        is likeliest just after a credit — no matter when payroll actually is. This is
        identifiability, not a threshold that needs tuning: no amount of that history
        distinguishes the true credit day. It is why inferred timing is worth
        considerably less than true timing, and it caps what SalaryAware can deliver.
        """
        model = SalaryInferenceModel.from_assumptions(shipped)
        past = tuple(
            _attempt(datetime(2026, m, 20, 17, tzinfo=UTC), AttemptOutcome.SUCCESS)
            for m in range(1, 8)
        )
        assert infer_salary_day(past, model) == 20

    def test_uninformative_reason_codes_are_ignored(self, shipped: Assumptions) -> None:
        """A downtime window says nothing about payroll. Counting it would let bank noise
        masquerade as a salary signal."""
        model = SalaryInferenceModel.from_assumptions(shipped)
        past = tuple(
            _attempt(datetime(2026, m, d, 17, tzinfo=UTC), code=ReasonCode.BANK_UNAVAILABLE)
            for m in range(1, 8)
            for d in (3, 19)
        )
        assert infer_salary_day(past, model) is None

    def test_a_clean_signal_is_recovered(self, shipped: Assumptions) -> None:
        model = SalaryInferenceModel.from_assumptions(shipped)
        inferred = infer_salary_day(_history_for_salary_day(5), model)
        assert inferred is not None
        assert abs(inferred - 5) <= 2


def test_an_all_success_history_leaves_the_schedule_untouched(shipped: Assumptions) -> None:
    """Why SalaryAware measures exactly 0.000 lift under a technical-dominant book.

    Measured there: the strategy is consulted, the inference commits on 43 mandates, and
    the proposed time differs from the baseline on *zero* of them. This is the mechanism,
    not a dead code path. With few insufficient-funds failures the informative history is
    successes on one billing day, the estimate collapses onto that day (SPEC §11), and the
    0-2 day post-credit window already contains T+1 — so there is nothing to move.

    Asserted so the zero stays explained. If this ever starts moving retries, the reported
    zero was hiding something.
    """
    strategy = SalaryAware(shipped)
    baseline = FixedSchedule(shipped)
    past = tuple(
        _attempt(datetime(2026, m, 20, 17, tzinfo=UTC), AttemptOutcome.SUCCESS)
        for m in range(1, 8)
    )
    billed = datetime(2026, 8, 20, 17, 0, tzinfo=UTC)
    failed = _attempt(billed)
    assert infer_salary_day(past, SalaryInferenceModel.from_assumptions(shipped)) is not None
    args = dict(
        failed_attempt=failed,
        mandate=_mandate(),
        customer_view=_view(past),
        history=[failed],
        clock=billed,
    )
    assert strategy.propose_retries(**args) == baseline.propose_retries(**args)
