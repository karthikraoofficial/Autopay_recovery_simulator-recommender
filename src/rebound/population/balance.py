from __future__ import annotations

import calendar
from datetime import date, timedelta

import numpy as np

from rebound.config import Assumptions
from rebound.domain.entities import Customer, Paise
from rebound.harness.seeding import stream

_SATURDAY = 5
_FRIDAY = 4
_DAYS_IN_LONGEST_MONTH = 31

# A credit can be pulled back across a month boundary by the weekend shift, so the
# most recent credit on or before a given day is not necessarily in that day's month
# or the one before it. Three months back is far more than the shift can ever reach.
_MONTHS_TO_SEARCH_BACK = 3


def _month_before(year: int, month: int, back: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) - back
    return index // 12, index % 12 + 1


class BalanceProcess:
    """SPEC §2.1. A customer's daily account balance, as a pure function of
    (seed, customer, date) — never of call order, so two strategies probing the same
    customer on the same day see the same balance.

    The emergent property the whole project rests on: because the balance decays from
    the salary credit and the daily noise is multiplicative, P(balance < amount) rises
    monotonically with days since credit. Nothing enforces that; it falls out of the
    decay, and `tests/test_population.py` asserts it holds for every income band.
    """

    def __init__(self, assumptions: Assumptions, seed: int) -> None:
        self._seed = seed
        self._jitter_max = int(assumptions.value("population.balance.salary_jitter_max_days"))
        self._shift_weekend = bool(
            assumptions.value("population.balance.salary_shifts_before_weekend")
        )
        self._carryover_mean = float(
            assumptions.value("population.balance.carryover_fraction_mean")
        )
        self._carryover_sigma = float(
            assumptions.value("population.balance.carryover_lognormal_sigma")
        )
        self._first_week_days = int(assumptions.value("population.balance.first_week_days"))
        self._first_week_multiplier = float(
            assumptions.value("population.balance.first_week_spend_multiplier")
        )
        self._cushion_sigma = float(assumptions.value("population.balance.cushion_lognormal_sigma"))
        self._noise_cache: dict[tuple[str, int, int], np.ndarray] = {}

    def salary_credit_date(self, customer: Customer, year: int, month: int) -> date:
        """The nominal payroll day, slipped by 0-2 days, then pulled back off a weekend.

        The pull-back can cross into the previous month — a credit nominally on the 1st
        of a month starting on a Sunday is made on the preceding Friday. That is real,
        and `days_since_salary_credit` searches back far enough to see it.
        """
        last_day = calendar.monthrange(year, month)[1]
        nominal = date(year, month, min(customer.salary_credit_day, last_day))
        rng = stream(self._seed, "salary-jitter", customer.id, f"{year:04d}-{month:02d}")
        credited = nominal + timedelta(days=int(rng.integers(0, self._jitter_max + 1)))
        if credited.month != month:
            credited = date(year, month, last_day)
        if self._shift_weekend and credited.weekday() >= _SATURDAY:
            credited -= timedelta(days=credited.weekday() - _FRIDAY)
        return credited

    def last_credit_on_or_before(self, customer: Customer, on: date) -> date:
        """Next month is searched too: a credit shifted back off a weekend can land in
        the month before its own."""
        candidates = [
            self.salary_credit_date(customer, *_month_before(on.year, on.month, back))
            for back in range(-1, _MONTHS_TO_SEARCH_BACK + 1)
        ]
        past = [c for c in candidates if c <= on]
        if not past:
            raise ValueError(f"no salary credit on or before {on} for {customer.id}")
        return max(past)

    def days_since_salary_credit(self, customer: Customer, on: date) -> int:
        return (on - self.last_credit_on_or_before(customer, on)).days

    def balance_paise(self, customer: Customer, on: date) -> Paise:
        credit = self.last_credit_on_or_before(customer, on)
        params = customer.balance_process
        opening = params.monthly_income_paise * (1.0 + self._carryover_fraction(customer, credit))
        spent = self._cumulative_spend(params.spend_decay_rate, (on - credit).days)
        balance = (
            opening * self._cushion(customer) * float(np.exp(-spent)) * self._noise(customer, on)
        )
        return max(0, int(balance))

    def _cushion(self, customer: Customer) -> float:
        """A per-customer, time-invariant multiplier on the whole balance level.

        Without it, every customer holds the same balance *relative to their income*, and
        nobody in the book lives near zero — a small-ticket debit then never fails for
        want of funds at any point in the cycle, which is not what a real mandate book
        looks like. Income does not determine what actually sits in the account: other
        accounts, dependants, rent timing and existing obligations do, and none of those
        are modelled. This is the one parameter standing in for all of them.

        Being constant per customer, it changes the level of insufficient-funds risk and
        who carries it, but not its monotonicity in days since credit.
        """
        rng = stream(self._seed, "cushion", customer.id)
        median = float(np.exp(-(self._cushion_sigma**2) / 2.0))
        return float(rng.lognormal(mean=np.log(median), sigma=self._cushion_sigma))

    def _cumulative_spend(self, decay_rate: float, days: int) -> float:
        """Spend velocity is higher in the first week (SPEC §2.1), so the decay exponent
        accumulates faster there. Strictly increasing in `days`, which is what makes
        P(insufficient funds) monotone."""
        front = min(days, self._first_week_days)
        return decay_rate * (self._first_week_multiplier * front + (days - front))

    def _carryover_fraction(self, customer: Customer, credit: date) -> float:
        """Drawn per credit, not per day, so the balance does not jump discontinuously
        within a salary cycle."""
        rng = stream(self._seed, "carryover", customer.id, f"{credit.year:04d}-{credit.month:02d}")
        median = self._carryover_mean * float(np.exp(-(self._carryover_sigma**2) / 2.0))
        return float(rng.lognormal(mean=np.log(median), sigma=self._carryover_sigma))

    def _noise(self, customer: Customer, on: date) -> float:
        """One draw per calendar day, taken from a month-at-a-time stream keyed by
        (customer, month) so the cost is one seeded generator per customer-month rather
        than one per day. Indexing by day-of-month keeps it independent of call order."""
        key = (customer.id, on.year, on.month)
        month_draws = self._noise_cache.get(key)
        if month_draws is None:
            sigma = customer.balance_process.lognormal_sigma
            rng = stream(self._seed, "balance", customer.id, f"{on.year:04d}-{on.month:02d}")
            # Median chosen so the noise has mean 1.0: it perturbs the balance without
            # shifting its expected level.
            month_draws = rng.lognormal(
                mean=-(sigma**2) / 2.0, sigma=sigma, size=_DAYS_IN_LONGEST_MONTH
            )
            self._noise_cache[key] = month_draws
        return float(month_draws[on.day - 1])
