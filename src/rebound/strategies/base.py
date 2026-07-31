from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import AwareDatetime, ConfigDict, Field

from rebound.domain.entities import (
    Customer,
    DebitAttempt,
    DomainModel,
    Mandate,
    Paise,
    Rail,
)


class CustomerObservable(DomainModel):
    """Everything a real merchant could know about a customer, and nothing else.

    SPEC §4: the true balance and the true salary credit day are deliberately absent.
    This is a separate type rather than a convention because a strategy that reads
    ground truth produces lift that can never be delivered, and reviewing for that by
    discipline fails silently. `from_customer` is the only constructor the harness uses,
    and it is the single place where the ground-truth boundary is crossed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    customer_id: str = Field(min_length=1)
    bank_id: str = Field(min_length=1)
    rail: Rail
    upi_app: str | None = None
    has_alt_rail: bool = False
    past_attempts: tuple[DebitAttempt, ...] = ()
    inferred_salary_day: int | None = Field(default=None, ge=1, le=31)

    @classmethod
    def from_customer(
        cls,
        customer: Customer,
        mandate: Mandate,
        past_attempts: tuple[DebitAttempt, ...] = (),
        inferred_salary_day: int | None = None,
    ) -> CustomerObservable:
        # customer.salary_credit_day and customer.balance_process are read here and
        # deliberately not forwarded. `inferred_salary_day` must come from a strategy's
        # own inference over past_attempts, never from the customer.
        return cls(
            customer_id=customer.id,
            bank_id=customer.bank_id,
            rail=mandate.rail,
            upi_app=customer.upi_app,
            has_alt_rail=customer.has_alt_rail,
            past_attempts=past_attempts,
            inferred_salary_day=inferred_salary_day,
        )


class ProposedRetry(DomainModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    mandate_id: str = Field(min_length=1)
    scheduled_at: AwareDatetime
    amount_paise: Paise = Field(gt=0)


@runtime_checkable
class RetryStrategy(Protocol):
    name: str

    def propose_retries(
        self,
        failed_attempt: DebitAttempt,
        mandate: Mandate,
        customer_view: CustomerObservable,
        history: list[DebitAttempt],
        clock: datetime,
    ) -> list[ProposedRetry]: ...
