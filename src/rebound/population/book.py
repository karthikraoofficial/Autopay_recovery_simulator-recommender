from __future__ import annotations

from datetime import UTC, date, datetime, time

from pydantic import AwareDatetime, ConfigDict, Field

from rebound.config import Assumptions
from rebound.domain.entities import (
    Bank,
    BillingDayPolicy,
    Customer,
    DomainModel,
    Mandate,
    Merchant,
)
from rebound.population.banks import generate_banks
from rebound.population.customers import generate_customers
from rebound.population.mandates import RAILS, generate_mandates

# Calendar fact, not an assumption: only days 1-28 exist in every month, so a fixed
# billing day above 28 has no well-defined meaning.
LAST_UNIVERSAL_DAY_OF_MONTH = 28


class Book(DomainModel):
    """A seeded population, generated once and deep-copied per strategy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seed: int
    start_at: AwareDatetime
    months: int = Field(gt=0)
    merchant: Merchant
    banks: tuple[Bank, ...] = Field(min_length=1)
    customers: tuple[Customer, ...] = Field(min_length=1)
    mandates: tuple[Mandate, ...] = Field(min_length=1)


def generate_book(assumptions: Assumptions, seed: int) -> Book:
    """Deterministic given `seed`. Every strategy is measured on a deep copy of this."""
    size = int(assumptions.value("book.size"))
    start_at = datetime.combine(
        date.fromisoformat(str(assumptions.value("book.start_date"))), time(), tzinfo=UTC
    )
    banks = generate_banks(assumptions, seed)
    customers = generate_customers(assumptions, seed, banks, size)
    merchant = Merchant(
        id="merchant-000",
        name="Unnamed merchant",
        vertical="unspecified",
        book_size=size,
        avg_ticket_paise=int(assumptions.value("book.avg_ticket_paise")),
        billing_day_policy=BillingDayPolicy.FIXED_CALENDAR_DAY,
        rails_enabled=RAILS,
    )
    return Book(
        seed=seed,
        start_at=start_at,
        months=int(assumptions.value("book.months")),
        merchant=merchant,
        banks=banks,
        customers=customers,
        mandates=generate_mandates(assumptions, seed, customers, merchant, start_at),
    )
