from __future__ import annotations

import calendar
from datetime import datetime

import numpy as np

from rebound.config import Assumptions
from rebound.domain.entities import Customer, Mandate, Merchant, Rail
from rebound.population.mix import share_vector
from rebound.seeding import stream

PAISE_PER_RUPEE = 100

# Ordered, not a set: the rail mix vector below is indexed positionally.
RAILS: tuple[Rail, ...] = (Rail.UPI_AUTOPAY, Rail.ENACH, Rail.CARD_EMANDATE)

_RAIL_MIX_KEYS = tuple(f"population.mandate.rail_mix.{r.value.lower()}" for r in RAILS)
_RAIL_CAP_KEYS: tuple[tuple[Rail, str], ...] = (
    (Rail.UPI_AUTOPAY, "rail.upi_autopay.mandate_cap_default_inr"),
    (Rail.ENACH, "rail.enach.mandate_cap_inr"),
    (Rail.CARD_EMANDATE, "rail.card_emandate.mandate_cap_inr"),
)


def rail_caps_paise(assumptions: Assumptions) -> dict[Rail, int]:
    """The ceiling a mandate on each rail may be registered for."""
    return {rail: int(assumptions.value(key)) * PAISE_PER_RUPEE for rail, key in _RAIL_CAP_KEYS}


def _created_at(start_at: datetime, months_back: int, billing_day: int) -> datetime:
    index = start_at.year * 12 + (start_at.month - 1) - months_back
    year, month = index // 12, index % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return start_at.replace(year=year, month=month, day=min(billing_day, last_day))


def generate_mandates(
    assumptions: Assumptions,
    seed: int,
    customers: tuple[Customer, ...],
    merchant: Merchant,
    start_at: datetime,
) -> tuple[Mandate, ...]:
    """One mandate per customer. `created_at.day` is the billing day, so spreading
    creation over the month is what makes days-since-salary vary at debit time — with
    every mandate billing on the same day the salary effect would be invisible."""
    rng = stream(seed, "population", "mandates")
    caps = rail_caps_paise(assumptions)
    avg_ticket = int(assumptions.value("book.avg_ticket_paise"))
    ticket_sigma = float(assumptions.value("population.mandate.ticket_lognormal_sigma"))
    cap_multiple = float(assumptions.value("population.mandate.cap_multiple_of_ticket"))
    billing_day_max = int(assumptions.value("population.mandate.billing_day_max"))
    max_age = int(assumptions.value("population.mandate.max_age_months"))
    n = len(customers)
    rails = rng.choice(len(RAILS), size=n, p=share_vector(assumptions, _RAIL_MIX_KEYS))
    tickets = rng.lognormal(mean=np.log(avg_ticket), sigma=ticket_sigma, size=n)
    billing_days = rng.integers(1, billing_day_max + 1, size=n)
    ages = rng.integers(0, max_age + 1, size=n)
    mandates = []
    for i, customer in enumerate(customers):
        rail = RAILS[int(rails[i])]
        mandates.append(
            Mandate(
                id=f"mandate-{i:06d}",
                merchant_id=merchant.id,
                customer_id=customer.id,
                rail=rail,
                # Clipped to the rail ceiling: a merchant cannot register headroom the
                # rail does not permit, and a book that ignored this would understate
                # MANDATE_AMOUNT_EXCEEDED once the phase-4 engine checks caps.
                max_amount_paise=max(1, min(int(tickets[i] * cap_multiple), caps[rail])),
                created_at=_created_at(start_at, int(ages[i]), int(billing_days[i])),
            )
        )
    return tuple(mandates)
