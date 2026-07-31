from __future__ import annotations

from datetime import time

import numpy as np

from rebound.config import Assumptions
from rebound.domain.entities import Bank, DowntimeWindow
from rebound.seeding import stream

HOURS_IN_DAY = 24
DAYS_IN_WEEK = 7


def market_shares(assumptions: Assumptions, count: int) -> np.ndarray:
    """Zipf-shaped bank market share, largest first.

    A real merchant's book is not spread evenly across banks; it piles into a few. That
    concentration is what decides whether knowing a bank's downtime is worth anything,
    so it is a parameter rather than an implicit uniform.
    """
    alpha = float(assumptions.value("population.bank.share_concentration"))
    weights = np.power(np.arange(1, count + 1, dtype=float), -alpha)
    return weights / weights.sum()


def _uptime_profile(rng: np.random.Generator, assumptions: Assumptions) -> dict[int, float]:
    """A diurnal availability curve: worst at `uptime_trough_hour`, best twelve hours
    later. Each bank scales the whole shortfall from 1.0, so a bad bank is bad at every
    hour rather than bad at a different hour, and `BankAware` has a stable signal."""
    daytime = float(assumptions.value("population.bank.uptime_daytime"))
    trough = float(assumptions.value("population.bank.uptime_night_trough"))
    trough_hour = int(assumptions.value("population.bank.uptime_trough_hour"))
    factor = float(
        rng.uniform(
            float(assumptions.value("population.bank.unavailability_factor_min")),
            float(assumptions.value("population.bank.unavailability_factor_max")),
        )
    )
    hours = np.arange(HOURS_IN_DAY, dtype=float)
    diurnal = 0.5 * (1.0 + np.cos(2.0 * np.pi * (hours - trough_hour) / HOURS_IN_DAY))
    median_uptime = daytime - (daytime - trough) * diurnal
    scaled = 1.0 - (1.0 - median_uptime) * factor
    return {h: float(np.clip(scaled[h], 0.0, 1.0)) for h in range(HOURS_IN_DAY)}


def _downtime_windows(
    rng: np.random.Generator, assumptions: Assumptions
) -> tuple[DowntimeWindow, ...]:
    mean = float(assumptions.value("population.bank.downtime_windows_mean"))
    maximum = int(assumptions.value("population.bank.downtime_windows_max"))
    length = int(assumptions.value("population.bank.downtime_window_hours"))
    earliest = int(assumptions.value("population.bank.downtime_window_earliest_hour"))
    latest = int(assumptions.value("population.bank.downtime_window_latest_hour"))
    daily_share = float(assumptions.value("population.bank.downtime_window_daily_share"))
    count = min(int(rng.poisson(mean)), maximum)
    windows = []
    for _ in range(count):
        start = int(rng.integers(earliest, latest + 1))
        weekday = None if float(rng.random()) < daily_share else int(rng.integers(0, DAYS_IN_WEEK))
        windows.append(
            DowntimeWindow(
                weekday=weekday,
                start=time(hour=start),
                end=time(hour=min(start + length, HOURS_IN_DAY - 1)),
            )
        )
    return tuple(windows)


def generate_banks(assumptions: Assumptions, seed: int) -> tuple[Bank, ...]:
    """Banks are ordered by market share, largest first. `market_shares` is the
    matching weight vector; customers are assigned against it."""
    rng = stream(seed, "population", "banks")
    count = int(assumptions.value("population.bank.count"))
    td_min = float(assumptions.value("population.bank.td_rate_min"))
    td_max = float(assumptions.value("population.bank.td_rate_max"))
    cutoff_hour = int(assumptions.value("population.bank.batch_cutoff_hour"))
    return_charge = int(assumptions.value("population.bank.return_charge_paise"))
    return tuple(
        Bank(
            id=f"bank-{i:03d}",
            name=f"Bank {i:03d}",
            td_rate=float(rng.uniform(td_min, td_max)),
            uptime_profile=_uptime_profile(rng, assumptions),
            downtime_windows=_downtime_windows(rng, assumptions),
            batch_cutoff_time=time(hour=cutoff_hour),
            return_charge_paise=return_charge,
        )
        for i in range(count)
    )
