from __future__ import annotations

from datetime import UTC, date, datetime, time

import numpy as np
from pydantic import AwareDatetime, ConfigDict, Field

from rebound.config import Assumptions
from rebound.domain.entities import (
    BalanceProcessParams,
    Bank,
    BillingDayPolicy,
    Customer,
    DomainModel,
    IncomeBand,
    Mandate,
    Merchant,
    Rail,
)
from rebound.harness.seeding import stream

# Calendar fact, not an assumption: only days 1-28 exist in every month, so a fixed
# billing day above 28 has no well-defined meaning.
LAST_UNIVERSAL_DAY_OF_MONTH = 28

_RAIL_MIX_KEYS: tuple[tuple[Rail, str], ...] = (
    (Rail.UPI_AUTOPAY, "book.rail_mix.upi_autopay"),
    (Rail.ENACH, "book.rail_mix.enach"),
    (Rail.CARD_EMANDATE, "book.rail_mix.card_emandate"),
)


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


def _rail_mix(assumptions: Assumptions) -> tuple[tuple[Rail, ...], np.ndarray]:
    weights = np.array([float(assumptions.value(key)) for _, key in _RAIL_MIX_KEYS])
    total = weights.sum()
    if not np.isclose(total, 1.0):
        raise ValueError(f"book.rail_mix.* must sum to 1.0, got {total}")
    return tuple(rail for rail, _ in _RAIL_MIX_KEYS), weights / total


def _generate_banks(assumptions: Assumptions, seed: int) -> tuple[Bank, ...]:
    rng = stream(seed, "book", "banks")
    count = int(assumptions.value("book.bank_count"))
    td_min = float(assumptions.value("book.bank.td_rate_min"))
    td_max = float(assumptions.value("book.bank.td_rate_max"))
    up_min = float(assumptions.value("book.bank.uptime_min"))
    up_max = float(assumptions.value("book.bank.uptime_max"))
    cutoff_hour = int(assumptions.value("book.bank.batch_cutoff_hour"))
    return_charge = int(assumptions.value("book.bank.return_charge_paise"))
    banks = []
    for i in range(count):
        hourly = rng.uniform(up_min, up_max, size=24)
        banks.append(
            Bank(
                id=f"bank-{i:03d}",
                name=f"Bank {i:03d}",
                td_rate=float(rng.uniform(td_min, td_max)),
                uptime_profile={h: float(hourly[h]) for h in range(24)},
                batch_cutoff_time=time(hour=cutoff_hour),
                return_charge_paise=return_charge,
            )
        )
    return tuple(banks)


def _income_band(income: float, low_cut: float, high_cut: float) -> IncomeBand:
    if income < low_cut:
        return IncomeBand.LOW
    if income > high_cut:
        return IncomeBand.HIGH
    return IncomeBand.MID


def _generate_customers(
    assumptions: Assumptions, seed: int, banks: tuple[Bank, ...], size: int
) -> tuple[Customer, ...]:
    rng = stream(seed, "book", "customers")
    centre = int(assumptions.value("book.avg_ticket_paise")) * float(
        assumptions.value("book.customer.income_multiple_of_ticket")
    )
    decay = float(assumptions.value("book.customer.spend_decay_rate"))
    sigma = float(assumptions.value("book.customer.balance_lognormal_sigma"))
    intent = float(assumptions.value("book.customer.intent_score_initial"))
    low_cut = centre * float(assumptions.value("book.customer.income_band_low_multiple"))
    high_cut = centre * float(assumptions.value("book.customer.income_band_high_multiple"))
    day_min = int(assumptions.value("book.customer.salary_day_min"))
    day_max = int(assumptions.value("book.customer.salary_day_max"))
    bank_ids = [b.id for b in banks]
    incomes = rng.lognormal(mean=np.log(centre), sigma=sigma, size=size)
    bank_choice = rng.integers(0, len(bank_ids), size=size)
    salary_days = rng.integers(day_min, day_max + 1, size=size)
    return tuple(
        Customer(
            id=f"cust-{i:06d}",
            bank_id=bank_ids[int(bank_choice[i])],
            salary_credit_day=int(salary_days[i]),
            income_band=_income_band(float(incomes[i]), low_cut, high_cut),
            balance_process=BalanceProcessParams(
                monthly_income_paise=max(1, int(incomes[i])),
                spend_decay_rate=decay,
                lognormal_sigma=sigma,
            ),
            intent_score=intent,
        )
        for i in range(size)
    )


def _generate_mandates(
    assumptions: Assumptions,
    seed: int,
    customers: tuple[Customer, ...],
    merchant: Merchant,
    start_at: datetime,
) -> tuple[Mandate, ...]:
    rng = stream(seed, "book", "mandates")
    rails, weights = _rail_mix(assumptions)
    avg_ticket = int(assumptions.value("book.avg_ticket_paise"))
    ticket_sigma = float(assumptions.value("book.ticket_lognormal_sigma"))
    cap_multiple = float(assumptions.value("book.mandate_cap_multiple_of_ticket"))
    n = len(customers)
    rail_choice = rng.choice(len(rails), size=n, p=weights)
    tickets = rng.lognormal(mean=np.log(avg_ticket), sigma=ticket_sigma, size=n)
    return tuple(
        Mandate(
            id=f"mandate-{i:06d}",
            merchant_id=merchant.id,
            customer_id=customer.id,
            rail=rails[int(rail_choice[i])],
            max_amount_paise=max(1, int(tickets[i] * cap_multiple)),
            created_at=start_at,
        )
        for i, customer in enumerate(customers)
    )


def generate_book(assumptions: Assumptions, seed: int) -> Book:
    """Deterministic given `seed`. Every strategy is measured on a deep copy of this."""
    size = int(assumptions.value("book.size"))
    start_at = datetime.combine(
        date.fromisoformat(str(assumptions.value("book.start_date"))), time(), tzinfo=UTC
    )
    banks = _generate_banks(assumptions, seed)
    customers = _generate_customers(assumptions, seed, banks, size)
    merchant = Merchant(
        id="merchant-000",
        name="Phase-2 placeholder merchant",
        vertical="unspecified",
        book_size=size,
        avg_ticket_paise=int(assumptions.value("book.avg_ticket_paise")),
        billing_day_policy=BillingDayPolicy.FIXED_CALENDAR_DAY,
        rails_enabled=tuple(rail for rail, _ in _RAIL_MIX_KEYS),
    )
    return Book(
        seed=seed,
        start_at=start_at,
        months=int(assumptions.value("book.months")),
        merchant=merchant,
        banks=banks,
        customers=customers,
        mandates=_generate_mandates(assumptions, seed, customers, merchant, start_at),
    )
