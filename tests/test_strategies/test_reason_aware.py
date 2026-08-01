"""SPEC §4.3 ReasonAware: branch on the reason code, and nothing else."""

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
from rebound.strategies.reason_aware import ReasonAware

FAILED_AT = datetime(2026, 4, 15, 10, 0, tzinfo=UTC)

SOFT_CODES = tuple(c for c in ReasonCode if c not in HARD_DECLINE_CODES)


def _mandate() -> Mandate:
    return Mandate(
        id="mandate-000000",
        merchant_id="merchant-000",
        customer_id="cust-000000",
        rail=Rail.UPI_AUTOPAY,
        max_amount_paise=1_000_000,
        created_at=datetime(2026, 1, 15, tzinfo=UTC),
    )


def _view() -> CustomerObservable:
    return CustomerObservable(customer_id="cust-000000", bank_id="bank-000", rail=Rail.UPI_AUTOPAY)


def _attempt(code: ReasonCode, number: int = 1, when: datetime = FAILED_AT) -> DebitAttempt:
    return DebitAttempt(
        id=f"a{number}",
        mandate_id="mandate-000000",
        scheduled_at=when,
        executed_at=when,
        amount_paise=49900,
        outcome=AttemptOutcome.FAILURE,
        reason_code=code,
        attempt_number=number,
        is_retry=number > 1,
    )


def _propose(
    strategy: ReasonAware, attempt: DebitAttempt, history: list[DebitAttempt] | None = None
):
    return strategy.propose_retries(
        failed_attempt=attempt,
        mandate=_mandate(),
        customer_view=_view(),
        history=history if history is not None else [attempt],
        clock=attempt.scheduled_at,
    )


@pytest.mark.parametrize("code", sorted(HARD_DECLINE_CODES))
def test_a_hard_decline_stops_the_chain(shipped: Assumptions, code: ReasonCode) -> None:
    """SPEC §1.3, and the single biggest source of fake lift in a naive simulator."""
    assert _propose(ReasonAware(shipped), _attempt(code)) == []


def test_a_technical_decline_is_retried_within_hours(shipped: Assumptions) -> None:
    """SPEC §4.3 states this one directly: technical decline, retry in 2 hours."""
    strategy = ReasonAware(shipped)
    [retry] = _propose(strategy, _attempt(ReasonCode.TECHNICAL_DECLINE))
    delay = retry.scheduled_at - FAILED_AT
    assert delay == timedelta(
        hours=float(shipped.value("strategy.reason_aware.technical_decline_delay_hours"))
    )
    assert delay < timedelta(days=1)


def test_insufficient_funds_waits_days_not_hours(shipped: Assumptions) -> None:
    """The branch that carries the thesis. Retrying an empty account in two hours cannot
    work, and the whole point of reading the reason code is to tell the two cases apart."""
    strategy = ReasonAware(shipped)
    [funds] = _propose(strategy, _attempt(ReasonCode.INSUFFICIENT_FUNDS))
    [technical] = _propose(strategy, _attempt(ReasonCode.TECHNICAL_DECLINE))
    assert funds.scheduled_at - FAILED_AT >= timedelta(days=1)
    assert funds.scheduled_at > technical.scheduled_at


def test_every_soft_code_is_handled_and_ordered_sensibly(shipped: Assumptions) -> None:
    """SPEC §1.3 ranks the codes by recoverability. A code that fell through unhandled
    would silently become a no-retry, which is a strategy decision made by omission."""
    strategy = ReasonAware(shipped)
    delays = {}
    for code in SOFT_CODES:
        proposals = _propose(strategy, _attempt(code))
        assert proposals, f"{code} produced no retry and no explicit stop"
        delays[code] = proposals[0].scheduled_at - FAILED_AT
    assert delays[ReasonCode.TECHNICAL_DECLINE] < delays[ReasonCode.BANK_UNAVAILABLE]
    assert delays[ReasonCode.BANK_UNAVAILABLE] < delays[ReasonCode.INSUFFICIENT_FUNDS]
    assert delays[ReasonCode.LIMIT_EXCEEDED] >= timedelta(days=1)


def test_the_delay_is_measured_from_the_failure_being_reacted_to(
    shipped: Assumptions,
) -> None:
    """A technical decline two days into an episode deserves a retry two hours later, not
    two hours after the original charge — which would be in the past."""
    strategy = ReasonAware(shipped)
    original = _attempt(ReasonCode.INSUFFICIENT_FUNDS)
    later = _attempt(ReasonCode.TECHNICAL_DECLINE, number=2, when=FAILED_AT + timedelta(days=2))
    [retry] = _propose(strategy, later, history=[original, later])
    assert retry.scheduled_at > later.scheduled_at


def test_the_strategy_stops_at_its_own_retry_limit(shipped: Assumptions) -> None:
    strategy = ReasonAware(shipped)
    cap = int(shipped.value("strategy.reason_aware.max_retries"))
    history = [_attempt(ReasonCode.INSUFFICIENT_FUNDS)]
    history += [_attempt(ReasonCode.INSUFFICIENT_FUNDS, n + 2) for n in range(cap)]
    assert _propose(strategy, history[-1], history=history) == []


def test_the_retry_amount_always_equals_the_original(shipped: Assumptions) -> None:
    """SPEC §3 amount integrity. A strategy proposing a different amount would be blocked
    by the guard, so getting this wrong costs recovery rather than breaking loudly."""
    strategy = ReasonAware(shipped)
    original = _attempt(ReasonCode.INSUFFICIENT_FUNDS)
    for code in SOFT_CODES:
        later = _attempt(code, number=2, when=FAILED_AT + timedelta(days=1))
        [retry] = _propose(strategy, later, history=[original, later])
        assert retry.amount_paise == original.amount_paise


def test_the_strategy_reads_nothing_from_the_customer_view(shipped: Assumptions) -> None:
    """SPEC §4.3 scopes ReasonAware to the reason code. If it started reading bank or
    salary signal its lift would no longer be attributable to reason-code branching, and
    BankAware and SalaryAware would have nothing left to show."""
    strategy = ReasonAware(shipped)
    baseline = _propose(strategy, _attempt(ReasonCode.INSUFFICIENT_FUNDS))[0]
    other_view = CustomerObservable(
        customer_id="cust-999999",
        bank_id="bank-011",
        rail=Rail.ENACH,
        upi_app="app_d",
        has_alt_rail=True,
        inferred_salary_day=1,
    )
    moved = strategy.propose_retries(
        failed_attempt=_attempt(ReasonCode.INSUFFICIENT_FUNDS),
        mandate=_mandate(),
        customer_view=other_view,
        history=[_attempt(ReasonCode.INSUFFICIENT_FUNDS)],
        clock=FAILED_AT,
    )[0]
    assert moved.scheduled_at == baseline.scheduled_at
