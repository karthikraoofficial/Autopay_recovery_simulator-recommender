from __future__ import annotations

from datetime import time
from enum import StrEnum

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from rebound.domain.reason_codes import ReasonCode, is_hard_decline

# Money is integer paise everywhere. Floats are not exact and this project promises
# byte-identical output for a given seed; ₹ conversion happens at the presentation edge.
Paise = int


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Rail(StrEnum):
    UPI_AUTOPAY = "UPI_AUTOPAY"
    ENACH = "ENACH"
    CARD_EMANDATE = "CARD_EMANDATE"


class MandateStatus(StrEnum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"
    EXPIRED = "EXPIRED"
    PAUSED = "PAUSED"


class AttemptOutcome(StrEnum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"


class EpisodeOutcome(StrEnum):
    RECOVERED = "RECOVERED"
    LAPSED = "LAPSED"


class IncomeBand(StrEnum):
    """Labels only. The ₹ boundaries between bands live in assumptions.yaml."""

    LOW = "low"
    MID = "mid"
    HIGH = "high"


class BillingDayPolicy(StrEnum):
    FIXED_CALENDAR_DAY = "fixed_calendar_day"
    SIGNUP_ANNIVERSARY = "signup_anniversary"


class BalanceProcessParams(DomainModel):
    """Parameters of SPEC §2.1's per-customer balance process. Values come from
    generators driven by assumptions.yaml, never from defaults here."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    monthly_income_paise: Paise = Field(gt=0)
    spend_decay_rate: float = Field(gt=0.0)
    lognormal_sigma: float = Field(gt=0.0)


class DowntimeWindow(DomainModel):
    """Recurring bank downtime. `weekday` is None for a window that recurs daily."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    weekday: int | None = Field(default=None, ge=0, le=6)
    start: time
    end: time


class Merchant(DomainModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    vertical: str = Field(min_length=1)
    book_size: int = Field(gt=0)
    avg_ticket_paise: Paise = Field(gt=0)
    billing_day_policy: BillingDayPolicy
    rails_enabled: tuple[Rail, ...] = Field(min_length=1)

    @field_validator("rails_enabled", mode="after")
    @classmethod
    def canonical_rail_order(cls, rails: tuple[Rail, ...]) -> tuple[Rail, ...]:
        """Ordered tuple, not a set: set iteration order is not stable across runs."""
        if len(set(rails)) != len(rails):
            raise ValueError("rails_enabled contains duplicates")
        return tuple(r for r in Rail if r in rails)


class Mandate(DomainModel):
    id: str = Field(min_length=1)
    merchant_id: str = Field(min_length=1)
    customer_id: str = Field(min_length=1)
    rail: Rail
    max_amount_paise: Paise = Field(gt=0)
    created_at: AwareDatetime
    status: MandateStatus = MandateStatus.ACTIVE

    @property
    def is_debitable(self) -> bool:
        return self.status is MandateStatus.ACTIVE


class Customer(DomainModel):
    id: str = Field(min_length=1)
    bank_id: str = Field(min_length=1)
    salary_credit_day: int = Field(ge=1, le=31)
    income_band: IncomeBand
    balance_process: BalanceProcessParams
    intent_score: float = Field(ge=0.0, le=1.0)
    upi_app: str | None = None
    has_alt_rail: bool = False


class Bank(DomainModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    td_rate: float = Field(ge=0.0, le=1.0)
    uptime_profile: dict[int, float]
    downtime_windows: tuple[DowntimeWindow, ...] = ()
    batch_cutoff_time: time
    return_charge_paise: Paise = Field(ge=0)

    @model_validator(mode="after")
    def uptime_profile_covers_every_hour(self) -> Bank:
        if sorted(self.uptime_profile) != list(range(24)):
            raise ValueError("uptime_profile must have one entry per hour 0..23")
        if any(not 0.0 <= v <= 1.0 for v in self.uptime_profile.values()):
            raise ValueError("uptime_profile values must be in [0, 1]")
        return self


class DebitAttempt(DomainModel):
    id: str = Field(min_length=1)
    mandate_id: str = Field(min_length=1)
    scheduled_at: AwareDatetime
    executed_at: AwareDatetime | None = None
    amount_paise: Paise = Field(gt=0)
    outcome: AttemptOutcome
    reason_code: ReasonCode | None = None
    attempt_number: int = Field(ge=1)
    is_retry: bool

    @model_validator(mode="after")
    def outcome_and_reason_code_agree(self) -> DebitAttempt:
        if self.outcome is AttemptOutcome.FAILURE and self.reason_code is None:
            raise ValueError("a FAILURE must carry a reason code (SPEC §0.4)")
        if self.outcome is AttemptOutcome.SUCCESS and self.reason_code is not None:
            raise ValueError("a SUCCESS must not carry a reason code")
        return self

    @model_validator(mode="after")
    def retry_flag_matches_attempt_number(self) -> DebitAttempt:
        if self.is_retry != (self.attempt_number > 1):
            raise ValueError("is_retry must be true exactly when attempt_number > 1")
        return self

    @property
    def is_hard_decline(self) -> bool:
        return self.reason_code is not None and is_hard_decline(self.reason_code)


class RecoveryEpisode(DomainModel):
    mandate_id: str = Field(min_length=1)
    cycle_id: str = Field(min_length=1)
    # Which billing month of the run this episode belongs to, zero-based. Carried
    # explicitly rather than parsed back out of cycle_id, because SPEC §6 renders ₹
    # recovered over 12 months and a string is not a place to keep a number.
    cycle_index: int = Field(ge=0)
    original_attempt: DebitAttempt
    retry_attempts: tuple[DebitAttempt, ...] = ()
    outcome: EpisodeOutcome
    days_to_recovery: int | None = Field(default=None, ge=0)
    amount_recovered_paise: Paise = Field(ge=0)

    @property
    def attempts(self) -> tuple[DebitAttempt, ...]:
        return (self.original_attempt, *self.retry_attempts)

    @model_validator(mode="after")
    def episode_starts_from_a_failure(self) -> RecoveryEpisode:
        if self.original_attempt.outcome is not AttemptOutcome.FAILURE:
            raise ValueError("a recovery episode exists only for a failed attempt")
        if self.original_attempt.is_retry:
            raise ValueError("original_attempt must not itself be a retry")
        return self

    @model_validator(mode="after")
    def retries_are_ordered_and_stop_at_a_hard_decline(self) -> RecoveryEpisode:
        """SPEC §1.3: hard declines terminate the chain. No exceptions."""
        attempts = self.attempts
        scheduled = [a.scheduled_at for a in attempts]
        if scheduled != sorted(scheduled):
            raise ValueError("retry_attempts must be ordered by scheduled_at")
        for i, attempt in enumerate(attempts):
            if attempt.is_hard_decline and i != len(attempts) - 1:
                raise ValueError(
                    f"attempt after hard decline {attempt.reason_code} on mandate {self.mandate_id}"
                )
        return self

    @model_validator(mode="after")
    def recovered_amount_matches_outcome(self) -> RecoveryEpisode:
        succeeded = [a for a in self.attempts if a.outcome is AttemptOutcome.SUCCESS]
        if self.outcome is EpisodeOutcome.RECOVERED:
            if not succeeded:
                raise ValueError("RECOVERED requires a successful attempt")
            if self.amount_recovered_paise != succeeded[-1].amount_paise:
                raise ValueError("amount_recovered must equal the successful attempt amount")
            if self.days_to_recovery is None:
                raise ValueError("RECOVERED requires days_to_recovery")
        else:
            if succeeded:
                raise ValueError("LAPSED episode contains a successful attempt")
            if self.amount_recovered_paise != 0 or self.days_to_recovery is not None:
                raise ValueError("LAPSED episode recovered nothing")
        return self
