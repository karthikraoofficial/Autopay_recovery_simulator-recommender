from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import AwareDatetime, ConfigDict, Field

from rebound.domain.entities import AttemptOutcome, Bank, Customer, DomainModel, Mandate, Paise
from rebound.domain.reason_codes import ReasonCode


class AttemptRequest(DomainModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    mandate: Mandate
    customer: Customer
    bank: Bank
    cycle_id: str = Field(min_length=1)
    attempt_number: int = Field(ge=1)
    scheduled_at: AwareDatetime
    amount_paise: Paise = Field(gt=0)
    original_reason_code: ReasonCode | None = None
    days_since_original: int = Field(default=0, ge=0)
    # SPEC §2.3: a notified customer is likelier to top up before the debit lands.
    # Nothing sets this yet — the phase-5 compliance guard owns pre-debit notification —
    # so every phase-4 number is the un-notified floor, not a full-system estimate.
    pre_debit_notified: bool = False


class AttemptResult(DomainModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: AttemptOutcome
    reason_code: ReasonCode | None = None
    executed_at: AwareDatetime
    induced_revocation: bool = False


@runtime_checkable
class PaymentEngine(Protocol):
    """Phase 4's failure engine replaces the phase-2 stub behind this."""

    def execute(self, request: AttemptRequest) -> AttemptResult: ...
