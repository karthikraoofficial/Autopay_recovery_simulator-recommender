from __future__ import annotations

from datetime import date

import numpy as np

from rebound.config import Assumptions
from rebound.domain.entities import Customer, Paise, Rail
from rebound.seeding import stream

_RAIL_LIMIT_KEYS: tuple[tuple[Rail, str], ...] = (
    (Rail.UPI_AUTOPAY, "engine.daily_limit_paise.upi_autopay"),
    (Rail.ENACH, "engine.daily_limit_paise.enach"),
    (Rail.CARD_EMANDATE, "engine.daily_limit_paise.card_emandate"),
)


class DailyLimits:
    """SPEC §2.2 step 4. A customer's own spending eats into the same per-rail daily
    limit the mandate debit has to fit inside.

    The merchant's debit is not the only thing on the rail that day, so the consumption
    already booked against the limit is drawn rather than assumed to be zero. Without it
    `LIMIT_EXCEEDED` could only ever fire for a debit larger than the whole daily limit,
    which is a different failure and already caught upstream by the mandate cap.
    """

    def __init__(self, assumptions: Assumptions, seed: int) -> None:
        self._seed = seed
        self._limits = {rail: int(assumptions.value(key)) for rail, key in _RAIL_LIMIT_KEYS}
        self._share_mean = float(assumptions.value("engine.daily_limit_consumption_share_mean"))
        self._share_sigma = float(
            assumptions.value("engine.daily_limit_consumption_lognormal_sigma")
        )

    def limit_paise(self, rail: Rail) -> Paise:
        return self._limits[rail]

    def consumed_paise(self, customer: Customer, rail: Rail, on: date) -> Paise:
        """Drawn per (customer, rail, day), so two strategies attempting on the same day
        face the same already-consumed limit."""
        rng = stream(self._seed, "daily-limit", customer.id, rail.value, on.isoformat())
        median = self._share_mean * float(np.exp(-(self._share_sigma**2) / 2.0))
        share = float(rng.lognormal(mean=np.log(median), sigma=self._share_sigma))
        return int(min(1.0, share) * self._limits[rail])

    def would_exceed(self, customer: Customer, rail: Rail, on: date, amount: Paise) -> bool:
        return self.consumed_paise(customer, rail, on) + amount > self._limits[rail]
