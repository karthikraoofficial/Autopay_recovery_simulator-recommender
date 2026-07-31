from __future__ import annotations

import numpy as np

from rebound.config import Assumptions
from rebound.domain.entities import BalanceProcessParams, Bank, Customer, IncomeBand
from rebound.population.banks import market_shares
from rebound.population.mix import share_vector
from rebound.seeding import stream

# Declaration order is the index order of every per-band array below. It is fixed here
# rather than taken from IncomeBand so that adding a band cannot silently reshuffle
# which customer gets which income for a fixed seed.
BANDS: tuple[IncomeBand, ...] = (IncomeBand.LOW, IncomeBand.MID, IncomeBand.HIGH)

_BAND_SHARE_KEYS = tuple(f"population.customer.band_share.{b.value}" for b in BANDS)
_UPI_APPS = ("app_a", "app_b", "app_c", "app_d")
_UPI_APP_KEYS = tuple(f"population.customer.upi_app_share.{a}" for a in _UPI_APPS)


def _per_band(assumptions: Assumptions, template: str) -> np.ndarray:
    return np.array([float(assumptions.value(template.format(band=b.value))) for b in BANDS])


def _incomes(rng: np.random.Generator, assumptions: Assumptions, bands: np.ndarray) -> np.ndarray:
    """Log-normal within a band, so the bands overlap at their edges the way real income
    does — a band is a segment we assign, not a hard bracket we measured."""
    medians = _per_band(assumptions, "population.customer.income_median_paise.{band}")
    sigma = float(assumptions.value("population.customer.income_lognormal_sigma"))
    return rng.lognormal(mean=np.log(medians[bands]), sigma=sigma)


def _salary_days(rng: np.random.Generator, assumptions: Assumptions, size: int) -> np.ndarray:
    """A two-cluster payroll calendar: most of the book paid at month end, the rest in
    the first week. The bimodality is the point — a unimodal salary date would make
    `SalaryAware` a trivial constant-offset rule and prove nothing."""
    end_share = float(assumptions.value("population.customer.salary_day.month_end_share"))
    end_min = int(assumptions.value("population.customer.salary_day.month_end_min"))
    end_max = int(assumptions.value("population.customer.salary_day.month_end_max"))
    start_min = int(assumptions.value("population.customer.salary_day.month_start_min"))
    start_max = int(assumptions.value("population.customer.salary_day.month_start_max"))
    at_month_end = rng.random(size) < end_share
    return np.where(
        at_month_end,
        rng.integers(end_min, end_max + 1, size=size),
        rng.integers(start_min, start_max + 1, size=size),
    )


def _intent_scores(rng: np.random.Generator, assumptions: Assumptions, size: int) -> np.ndarray:
    mean = float(assumptions.value("population.customer.intent_score_mean"))
    concentration = float(assumptions.value("population.customer.intent_score_concentration"))
    return rng.beta(mean * concentration, (1.0 - mean) * concentration, size=size)


def generate_customers(
    assumptions: Assumptions, seed: int, banks: tuple[Bank, ...], size: int
) -> tuple[Customer, ...]:
    rng = stream(seed, "population", "customers")
    bands = rng.choice(len(BANDS), size=size, p=share_vector(assumptions, _BAND_SHARE_KEYS))
    incomes = _incomes(rng, assumptions, bands)
    bank_index = rng.choice(len(banks), size=size, p=market_shares(assumptions, len(banks)))
    salary_days = _salary_days(rng, assumptions, size)
    intents = _intent_scores(rng, assumptions, size)
    apps = rng.choice(len(_UPI_APPS), size=size, p=share_vector(assumptions, _UPI_APP_KEYS))
    alt_rail = rng.random(size) < float(assumptions.value("population.customer.has_alt_rail_share"))
    decay = _per_band(assumptions, "population.balance.spend_decay_rate.{band}")
    sigma = _per_band(assumptions, "population.balance.daily_lognormal_sigma.{band}")
    return tuple(
        Customer(
            id=f"cust-{i:06d}",
            bank_id=banks[int(bank_index[i])].id,
            salary_credit_day=int(salary_days[i]),
            income_band=BANDS[int(bands[i])],
            balance_process=BalanceProcessParams(
                monthly_income_paise=max(1, int(incomes[i])),
                spend_decay_rate=float(decay[bands[i]]),
                lognormal_sigma=float(sigma[bands[i]]),
            ),
            intent_score=float(intents[i]),
            upi_app=_UPI_APPS[int(apps[i])],
            has_alt_rail=bool(alt_rail[i]),
        )
        for i in range(size)
    )
