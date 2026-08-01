"""SPEC §4.6 Blended: strategies 3-5 combined by a scoring function."""

from __future__ import annotations

from datetime import UTC, datetime

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
from rebound.strategies.blended import Blended

BILLED_AT = datetime(2026, 4, 20, 17, 0, tzinfo=UTC)

SOFT_CODES = tuple(c for c in ReasonCode if c not in HARD_DECLINE_CODES)


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
    when: datetime = BILLED_AT,
    code: ReasonCode | None = ReasonCode.INSUFFICIENT_FUNDS,
    number: int = 1,
) -> DebitAttempt:
    return DebitAttempt(
        id=f"a-{when.isoformat()}-{number}",
        mandate_id="mandate-000000",
        scheduled_at=when,
        executed_at=when,
        amount_paise=49900,
        outcome=AttemptOutcome.FAILURE,
        reason_code=code,
        attempt_number=number,
        is_retry=number > 1,
    )


def _propose(strategy: Blended, failed: DebitAttempt, past: tuple[DebitAttempt, ...] = ()):
    return strategy.propose_retries(
        failed_attempt=failed,
        mandate=_mandate(),
        customer_view=CustomerObservable(
            customer_id="cust-000000",
            bank_id="bank-000",
            rail=Rail.UPI_AUTOPAY,
            past_attempts=past,
        ),
        history=[failed],
        clock=failed.scheduled_at,
    )


@pytest.mark.parametrize("code", sorted(HARD_DECLINE_CODES))
def test_a_hard_decline_stops_the_chain(shipped: Assumptions, code: ReasonCode) -> None:
    assert _propose(Blended(shipped), _attempt(code=code)) == []


def test_it_proposes_exactly_one_retry(shipped: Assumptions) -> None:
    """A menu of candidates would have its rejects logged as compliance blocks, and the
    'opportunity forgone' column would stop being comparable with the baseline's."""
    strategy = Blended(shipped)
    for code in SOFT_CODES:
        assert len(_propose(strategy, _attempt(code=code))) == 1


def test_every_proposal_is_in_the_future(shipped: Assumptions) -> None:
    strategy = Blended(shipped)
    for code in SOFT_CODES:
        [retry] = _propose(strategy, _attempt(code=code))
        assert retry.scheduled_at > BILLED_AT


def test_a_technical_decline_is_retried_sooner_than_insufficient_funds(
    shipped: Assumptions,
) -> None:
    """The reason component has to survive the blend. If it does not, Blended has lost
    the one signal in it with a stated basis in SPEC.md."""
    strategy = Blended(shipped)
    [technical] = _propose(strategy, _attempt(code=ReasonCode.TECHNICAL_DECLINE))
    [funds] = _propose(strategy, _attempt(code=ReasonCode.INSUFFICIENT_FUNDS))
    assert technical.scheduled_at < funds.scheduled_at


def test_the_retry_amount_always_equals_the_original(shipped: Assumptions) -> None:
    strategy = Blended(shipped)
    for code in SOFT_CODES:
        [retry] = _propose(strategy, _attempt(code=code))
        assert retry.amount_paise == 49900


def test_it_stops_at_its_own_retry_limit(shipped: Assumptions) -> None:
    strategy = Blended(shipped)
    cap = int(shipped.value("strategy.blended.max_retries"))
    failed = _attempt()
    history = [failed] + [_attempt(number=n + 2) for n in range(cap)]
    assert (
        strategy.propose_retries(
            failed_attempt=history[-1],
            mandate=_mandate(),
            customer_view=CustomerObservable(
                customer_id="cust-000000", bank_id="bank-000", rail=Rail.UPI_AUTOPAY
            ),
            history=history,
            clock=BILLED_AT,
        )
        == []
    )


def test_reset_clears_the_bank_tally(shipped: Assumptions) -> None:
    strategy = Blended(shipped)
    [before] = _propose(strategy, _attempt(code=ReasonCode.TECHNICAL_DECLINE))
    failures = tuple(
        DebitAttempt(
            id=f"seen-{day}",
            mandate_id="mandate-000000",
            scheduled_at=datetime(2026, 3, day, before.scheduled_at.hour, tzinfo=UTC),
            executed_at=datetime(2026, 3, day, before.scheduled_at.hour, tzinfo=UTC),
            amount_paise=49900,
            outcome=AttemptOutcome.FAILURE,
            reason_code=ReasonCode.BANK_UNAVAILABLE,
            attempt_number=1,
            is_retry=False,
        )
        for day in range(1, 21)
    )
    [moved] = _propose(strategy, _attempt(code=ReasonCode.TECHNICAL_DECLINE), failures)
    assert moved.scheduled_at != before.scheduled_at
    strategy.reset()
    [again] = _propose(strategy, _attempt(code=ReasonCode.TECHNICAL_DECLINE))
    assert again.scheduled_at == before.scheduled_at
