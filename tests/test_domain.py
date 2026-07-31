from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest
from pydantic import ValidationError

from rebound.domain import (
    HARD_DECLINE_CODES,
    REASON_CODE_PROFILES,
    AttemptOutcome,
    BalanceProcessParams,
    Bank,
    BillingDayPolicy,
    Customer,
    DebitAttempt,
    DeclineSeverity,
    EpisodeOutcome,
    IncomeBand,
    Mandate,
    MandateStatus,
    Merchant,
    Rail,
    ReasonCode,
    RecoveryEpisode,
    is_hard_decline,
)

T0 = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)


def attempt(
    *,
    n: int = 1,
    outcome: AttemptOutcome = AttemptOutcome.FAILURE,
    reason: ReasonCode | None = ReasonCode.INSUFFICIENT_FUNDS,
    offset_days: int = 0,
    amount_paise: int = 49900,
) -> DebitAttempt:
    return DebitAttempt(
        id=f"a{n}",
        mandate_id="m1",
        scheduled_at=T0 + timedelta(days=offset_days),
        amount_paise=amount_paise,
        outcome=outcome,
        reason_code=reason,
        attempt_number=n,
        is_retry=n > 1,
    )


# --- reason codes (SPEC §1.3) -------------------------------------------------


def test_every_reason_code_has_a_profile() -> None:
    assert set(REASON_CODE_PROFILES) == set(ReasonCode)


def test_hard_decline_set_matches_the_spec_table() -> None:
    assert {
        ReasonCode.MANDATE_AMOUNT_EXCEEDED,
        ReasonCode.MANDATE_REVOKED,
        ReasonCode.MANDATE_EXPIRED,
        ReasonCode.ACCOUNT_FROZEN,
    } == HARD_DECLINE_CODES


def test_insufficient_funds_is_soft_and_the_core_opportunity() -> None:
    profile = REASON_CODE_PROFILES[ReasonCode.INSUFFICIENT_FUNDS]
    assert profile.severity is DeclineSeverity.SOFT
    assert not is_hard_decline(ReasonCode.INSUFFICIENT_FUNDS)


# --- attempts -----------------------------------------------------------------


def test_failure_without_a_reason_code_is_rejected() -> None:
    with pytest.raises(ValidationError, match="reason code"):
        attempt(reason=None)


def test_success_with_a_reason_code_is_rejected() -> None:
    with pytest.raises(ValidationError, match="must not carry a reason code"):
        attempt(outcome=AttemptOutcome.SUCCESS, reason=ReasonCode.TECHNICAL_DECLINE)


def test_is_retry_must_agree_with_attempt_number() -> None:
    with pytest.raises(ValidationError, match="is_retry"):
        DebitAttempt(
            id="a2",
            mandate_id="m1",
            scheduled_at=T0,
            amount_paise=1,
            outcome=AttemptOutcome.SUCCESS,
            attempt_number=2,
            is_retry=False,
        )


def test_naive_datetimes_are_rejected() -> None:
    with pytest.raises(ValidationError):
        DebitAttempt(
            id="a1",
            mandate_id="m1",
            scheduled_at=datetime(2026, 3, 1, 9, 0),
            amount_paise=1,
            outcome=AttemptOutcome.SUCCESS,
            attempt_number=1,
            is_retry=False,
        )


# --- episodes -----------------------------------------------------------------


def test_lapsed_episode_recovers_nothing() -> None:
    episode = RecoveryEpisode(
        mandate_id="m1",
        cycle_id="c1",
        original_attempt=attempt(),
        retry_attempts=(attempt(n=2, offset_days=3),),
        outcome=EpisodeOutcome.LAPSED,
        amount_recovered_paise=0,
    )
    assert len(episode.attempts) == 2


def test_recovered_episode_amount_must_match_the_successful_attempt() -> None:
    with pytest.raises(ValidationError, match="amount_recovered"):
        RecoveryEpisode(
            mandate_id="m1",
            cycle_id="c1",
            original_attempt=attempt(),
            retry_attempts=(
                attempt(n=2, outcome=AttemptOutcome.SUCCESS, reason=None, offset_days=3),
            ),
            outcome=EpisodeOutcome.RECOVERED,
            days_to_recovery=3,
            amount_recovered_paise=99900,
        )


def test_lapsed_episode_containing_a_success_is_rejected() -> None:
    with pytest.raises(ValidationError, match="successful attempt"):
        RecoveryEpisode(
            mandate_id="m1",
            cycle_id="c1",
            original_attempt=attempt(),
            retry_attempts=(
                attempt(n=2, outcome=AttemptOutcome.SUCCESS, reason=None, offset_days=1),
            ),
            outcome=EpisodeOutcome.LAPSED,
            amount_recovered_paise=0,
        )


def test_attempt_after_a_hard_decline_is_rejected() -> None:
    with pytest.raises(ValidationError, match="after hard decline"):
        RecoveryEpisode(
            mandate_id="m1",
            cycle_id="c1",
            original_attempt=attempt(reason=ReasonCode.MANDATE_REVOKED),
            retry_attempts=(attempt(n=2, offset_days=1),),
            outcome=EpisodeOutcome.LAPSED,
            amount_recovered_paise=0,
        )


def test_hard_decline_as_the_last_attempt_is_allowed() -> None:
    episode = RecoveryEpisode(
        mandate_id="m1",
        cycle_id="c1",
        original_attempt=attempt(),
        retry_attempts=(attempt(n=2, reason=ReasonCode.MANDATE_REVOKED, offset_days=1),),
        outcome=EpisodeOutcome.LAPSED,
        amount_recovered_paise=0,
    )
    assert episode.attempts[-1].is_hard_decline


def test_retries_must_be_ordered_by_scheduled_at() -> None:
    with pytest.raises(ValidationError, match="ordered"):
        RecoveryEpisode(
            mandate_id="m1",
            cycle_id="c1",
            original_attempt=attempt(offset_days=5),
            retry_attempts=(attempt(n=2, offset_days=1),),
            outcome=EpisodeOutcome.LAPSED,
            amount_recovered_paise=0,
        )


def test_episode_must_start_from_a_failure() -> None:
    with pytest.raises(ValidationError, match="failed attempt"):
        RecoveryEpisode(
            mandate_id="m1",
            cycle_id="c1",
            original_attempt=attempt(outcome=AttemptOutcome.SUCCESS, reason=None),
            outcome=EpisodeOutcome.LAPSED,
            amount_recovered_paise=0,
        )


# --- book entities ------------------------------------------------------------


def test_rails_enabled_is_canonically_ordered_regardless_of_input_order() -> None:
    kwargs = dict(
        id="mer1",
        name="Acme",
        vertical="ott",
        book_size=1000,
        avg_ticket_paise=49900,
        billing_day_policy=BillingDayPolicy.FIXED_CALENDAR_DAY,
    )
    a = Merchant(**kwargs, rails_enabled=(Rail.ENACH, Rail.UPI_AUTOPAY))
    b = Merchant(**kwargs, rails_enabled=(Rail.UPI_AUTOPAY, Rail.ENACH))
    assert a.rails_enabled == b.rails_enabled == (Rail.UPI_AUTOPAY, Rail.ENACH)


def test_duplicate_rails_are_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicates"):
        Merchant(
            id="mer1",
            name="Acme",
            vertical="ott",
            book_size=1,
            avg_ticket_paise=1,
            billing_day_policy=BillingDayPolicy.FIXED_CALENDAR_DAY,
            rails_enabled=(Rail.ENACH, Rail.ENACH),
        )


def test_mandate_is_debitable_only_when_active() -> None:
    mandate = Mandate(
        id="m1",
        merchant_id="mer1",
        customer_id="c1",
        rail=Rail.UPI_AUTOPAY,
        max_amount_paise=1500000,
        created_at=T0,
    )
    assert mandate.status is MandateStatus.ACTIVE
    assert mandate.is_debitable
    mandate.status = MandateStatus.REVOKED
    assert not mandate.is_debitable


def test_customer_intent_score_is_bounded() -> None:
    with pytest.raises(ValidationError):
        Customer(
            id="c1",
            bank_id="b1",
            salary_credit_day=1,
            income_band=IncomeBand.MID,
            balance_process=BalanceProcessParams(
                monthly_income_paise=6000000, spend_decay_rate=0.1, lognormal_sigma=0.4
            ),
            intent_score=1.5,
        )


def test_bank_uptime_profile_must_cover_all_24_hours() -> None:
    with pytest.raises(ValidationError, match="one entry per hour"):
        Bank(
            id="b1",
            name="Bank",
            td_rate=0.02,
            uptime_profile={h: 0.99 for h in range(23)},
            batch_cutoff_time=time(17, 0),
            return_charge_paise=59000,
        )


def test_unknown_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Mandate(
            id="m1",
            merchant_id="mer1",
            customer_id="c1",
            rail=Rail.ENACH,
            max_amount_paise=1,
            created_at=T0,
            sauce="typo",
        )
