"""SPEC §4.5 BankAware: prefer time-of-day slots the bank has been seen to succeed at."""

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
from rebound.strategies.bank_aware import BankAware
from rebound.strategies.base import CustomerObservable

BILLED_AT = datetime(2026, 4, 20, 17, 0, tzinfo=UTC)


def _mandate(rail: Rail = Rail.UPI_AUTOPAY) -> Mandate:
    return Mandate(
        id="mandate-000000",
        merchant_id="merchant-000",
        customer_id="cust-000000",
        rail=rail,
        max_amount_paise=1_000_000,
        created_at=datetime(2026, 1, 20, tzinfo=UTC),
    )


def _attempt(
    when: datetime,
    outcome: AttemptOutcome = AttemptOutcome.FAILURE,
    code: ReasonCode | None = ReasonCode.INSUFFICIENT_FUNDS,
    number: int = 1,
    suffix: str = "",
) -> DebitAttempt:
    return DebitAttempt(
        id=f"a-{when.isoformat()}-{number}{suffix}",
        mandate_id="mandate-000000",
        scheduled_at=when,
        executed_at=when,
        amount_paise=49900,
        outcome=outcome,
        reason_code=code if outcome is AttemptOutcome.FAILURE else None,
        attempt_number=number,
        is_retry=number > 1,
    )


def _view(past: tuple[DebitAttempt, ...] = (), bank_id: str = "bank-000") -> CustomerObservable:
    return CustomerObservable(
        customer_id="cust-000000", bank_id=bank_id, rail=Rail.UPI_AUTOPAY, past_attempts=past
    )


def _propose(strategy: BankAware, failed: DebitAttempt, past: tuple[DebitAttempt, ...] = ()):
    return strategy.propose_retries(
        failed_attempt=failed,
        mandate=_mandate(),
        customer_view=_view(past),
        history=[failed],
        clock=failed.scheduled_at,
    )


@pytest.mark.parametrize("code", sorted(HARD_DECLINE_CODES))
def test_a_hard_decline_stops_the_chain(shipped: Assumptions, code: ReasonCode) -> None:
    assert _propose(BankAware(shipped), _attempt(BILLED_AT, code=code)) == []


def test_the_retry_lands_inside_the_schedulable_hours(shipped: Assumptions) -> None:
    """The bank's real batch cutoff is not on CustomerObservable, so the bounds stand in
    for it. Outside them an eNACH retry would be blocked rather than executed."""
    low = int(shipped.value("strategy.bank_aware.earliest_hour"))
    high = int(shipped.value("strategy.bank_aware.latest_hour"))
    [retry] = _propose(BankAware(shipped), _attempt(BILLED_AT))
    assert low <= retry.scheduled_at.hour <= high


def test_it_keeps_the_baseline_retry_date(shipped: Assumptions) -> None:
    """It moves the hour, not the day. Moving the date too would confound this
    strategy's lift with SalaryAware's."""
    offsets = list(shipped.value("strategy.fixed_schedule.retry_offsets_days"))
    [retry] = _propose(BankAware(shipped), _attempt(BILLED_AT))
    assert retry.scheduled_at.date() == (BILLED_AT + timedelta(days=offsets[0])).date()


def test_it_learns_away_from_an_hour_it_has_seen_fail(shipped: Assumptions) -> None:
    strategy = BankAware(shipped)
    bad_hour = strategy.best_hour("bank-000")
    failures = tuple(
        _attempt(
            datetime(2026, 3, day, bad_hour, tzinfo=UTC),
            code=ReasonCode.BANK_UNAVAILABLE,
            suffix=f"-{day}",
        )
        for day in range(1, 21)
    )
    strategy.observe("bank-000", failures)
    assert strategy.best_hour("bank-000") != bad_hour


def test_evidence_is_counted_once_however_often_it_is_shown(shipped: Assumptions) -> None:
    """`past_attempts` is re-presented in full on every call. Counted naively, confidence
    would grow with how often the strategy was asked rather than with what it has seen."""
    strategy = BankAware(shipped)
    past = (_attempt(datetime(2026, 3, 2, 9, tzinfo=UTC), code=ReasonCode.BANK_UNAVAILABLE),)
    strategy.observe("bank-000", past)
    once = strategy.success_rate("bank-000", 9)
    for _ in range(5):
        strategy.observe("bank-000", past)
    assert strategy.success_rate("bank-000", 9) == once


def test_a_banks_history_does_not_move_another_banks_slots(shipped: Assumptions) -> None:
    strategy = BankAware(shipped)
    hour = strategy.best_hour("bank-001")
    strategy.observe(
        "bank-000",
        tuple(
            _attempt(
                datetime(2026, 3, day, hour, tzinfo=UTC),
                code=ReasonCode.BANK_UNAVAILABLE,
                suffix=f"-{day}",
            )
            for day in range(1, 21)
        ),
    )
    assert strategy.best_hour("bank-001") == hour


def test_reset_clears_what_it_learned(shipped: Assumptions) -> None:
    """SPEC §5.1 pairs strategies on identical populations. State carried from one book
    into the next would make seed N a different experiment from seed 1."""
    strategy = BankAware(shipped)
    before = strategy.success_rate("bank-000", 9)
    strategy.observe(
        "bank-000",
        (_attempt(datetime(2026, 3, 2, 9, tzinfo=UTC), code=ReasonCode.BANK_UNAVAILABLE),),
    )
    assert strategy.success_rate("bank-000", 9) != before
    strategy.reset()
    assert strategy.success_rate("bank-000", 9) == before


def test_the_retry_amount_always_equals_the_original(shipped: Assumptions) -> None:
    [retry] = _propose(BankAware(shipped), _attempt(BILLED_AT))
    assert retry.amount_paise == 49900


def test_no_reschedule_proposes_exactly_what_the_baseline_proposes(
    shipped: Assumptions,
) -> None:
    """SPEC §4.1 freezes the baseline's proposals. `NoReschedule` differs only in what
    the guard does with a blocked one, so if it ever proposes a different time the two
    references stop being comparable and the rescheduling lift becomes meaningless."""
    from rebound.strategies.fixed_schedule import FixedSchedule
    from rebound.strategies.no_reschedule import NoReschedule

    baseline, variant = FixedSchedule(shipped), NoReschedule(shipped)
    assert variant.reschedules_blocked_retries is False
    assert getattr(baseline, "reschedules_blocked_retries", True) is True
    for n in range(4):
        history = [_attempt(BILLED_AT)] + [
            _attempt(BILLED_AT, number=i + 2, suffix=f"-{i}") for i in range(n)
        ]
        args = dict(
            failed_attempt=history[-1],
            mandate=_mandate(),
            customer_view=_view(),
            history=history,
            clock=BILLED_AT,
        )
        assert variant.propose_retries(**args) == baseline.propose_retries(**args)
