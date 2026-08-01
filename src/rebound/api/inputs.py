"""The six dashboard inputs, and how each one reaches the simulation.

SPEC §6.1 originally named book size, avg ticket, rail mix, vertical and billing day
policy. Two of those are absent here on purpose:

- `vertical` is a label. Nothing in `population/` or `engine/` reads `Merchant.vertical`,
  so a vertical selector would change the caption above the chart and nothing below it.
  What a vertical is really a proxy for is the failure mix, and that is offered directly
  as `failure_mix` instead.
- `billing_day_policy` has a second member, `SIGNUP_ANNIVERSARY`, that no generator
  implements: `generate_mandates` always draws a fixed calendar day. Offering the control
  would imply a modelled alternative that does not exist.

Shipping either as a live control would put an input on the dashboard that moves no
number, which is exactly what a sceptical reader tests first. They are replaced by
`failure_mix` and `performance_fee_rate` — the mix because SPEC §11 names it the largest
uncertainty in the model and phase 8 measured strategy lift to be fragile across it, the
fee because it converts gross recovery into the net figure the merchant is actually
quoted. SPEC §6.1 has been updated to this list.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rebound.config import Assumptions

PAISE_PER_RUPEE = 100

# A rail mix must sum to 1. The form supplies two shares and the third absorbs the
# remainder, so the constraint cannot be violated by the caller at all.
_RAIL_MIX_TOLERANCE = 1e-9


class FailureMix(StrEnum):
    """Which shape of failure book to simulate.

    Not a target distribution — see the preset.failure_mix.* block in assumptions.yaml.
    Each member selects engine settings that *produce* a mix; the mix each produces is
    measured, never asserted.
    """

    CURRENT = "current"
    INSUFFICIENT_FUNDS_DOMINANT = "insufficient_funds_dominant"
    TECHNICAL_DOMINANT = "technical_dominant"


# Preset name -> {assumption key it overrides: key holding the preset value}. Indirect
# rather than literal so no number lives in Python (CLAUDE.md), and so the dashboard's
# assumptions panel shows both the shipped value and the preset that replaced it.
_PRESET_OVERRIDES: dict[FailureMix, dict[str, str]] = {
    FailureMix.CURRENT: {},
    FailureMix.INSUFFICIENT_FUNDS_DOMINANT: {
        "population.balance.cushion_lognormal_sigma": (
            "preset.failure_mix.insufficient_funds_dominant.cushion_lognormal_sigma"
        ),
        "population.bank.td_rate_max": (
            "preset.failure_mix.insufficient_funds_dominant.td_rate_max"
        ),
    },
    FailureMix.TECHNICAL_DOMINANT: {
        "population.balance.cushion_lognormal_sigma": (
            "preset.failure_mix.technical_dominant.cushion_lognormal_sigma"
        ),
        "population.bank.td_rate_min": "preset.failure_mix.technical_dominant.td_rate_min",
        "population.bank.td_rate_max": "preset.failure_mix.technical_dominant.td_rate_max",
    },
}


class Sizing(StrEnum):
    """How much compute the run is given. Not a modelling choice, and kept separate from
    `MerchantProfile` for that reason.

    The two are genuinely different operations. `INTERACTIVE` is for moving a slider and
    watching the answer move; `PUBLICATION` is the number that goes in front of a
    merchant. They differ only in book size and seed count, so an interactive run is not
    a different model — it is the same model measured less precisely, and its intervals
    are correspondingly wider. The API returns the seed count and the interval with every
    result so that the difference is visible rather than implied.
    """

    INTERACTIVE = "interactive"
    PUBLICATION = "publication"


class SizingPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sizing: Sizing
    book_size: int = Field(gt=0)
    n_seeds: int = Field(gt=1)


def sizing_plan(assumptions: Assumptions, sizing: Sizing, book_size: int) -> SizingPlan:
    """Publication sizing is the setting at which the phase-7 sweep found the comparison
    significant, so it is read from the sensitivity keys rather than invented here. It is
    smaller than the merchant's stated book and runs more seeds: precision on this
    comparison comes from seeds, not from book size."""
    if sizing is Sizing.PUBLICATION:
        return SizingPlan(
            sizing=sizing,
            book_size=int(assumptions.value("harness.sensitivity.book_size")),
            n_seeds=int(assumptions.value("harness.sensitivity.seeds")),
        )
    return SizingPlan(
        sizing=sizing,
        book_size=book_size,
        n_seeds=int(assumptions.value("api.interactive_seeds")),
    )


class MerchantProfile(BaseModel):
    """The six fields on the dashboard. Everything else comes from assumptions.yaml."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    book_size: int = Field(gt=0, le=100_000)
    avg_ticket_inr: float = Field(gt=0.0)
    upi_autopay_share: float = Field(ge=0.0, le=1.0)
    enach_share: float = Field(ge=0.0, le=1.0)
    failure_mix: FailureMix = FailureMix.CURRENT
    performance_fee_rate: float = Field(ge=0.0, lt=1.0)

    @model_validator(mode="after")
    def rail_shares_leave_room_for_cards(self) -> MerchantProfile:
        if self.upi_autopay_share + self.enach_share > 1.0 + _RAIL_MIX_TOLERANCE:
            raise ValueError("upi_autopay_share + enach_share must not exceed 1.0")
        return self

    @property
    def card_emandate_share(self) -> float:
        """The remainder, so the mix sums to 1 by construction rather than by validation."""
        return max(0.0, 1.0 - self.upi_autopay_share - self.enach_share)

    def overrides(self, assumptions: Assumptions) -> dict[str, Any]:
        """Every assumption key this profile sets, and to what."""
        values: dict[str, Any] = {
            "book.size": self.book_size,
            "book.avg_ticket_paise": int(round(self.avg_ticket_inr * PAISE_PER_RUPEE)),
            "population.mandate.rail_mix.upi_autopay": self.upi_autopay_share,
            "population.mandate.rail_mix.enach": self.enach_share,
            "population.mandate.rail_mix.card_emandate": self.card_emandate_share,
            "harness.performance_fee_rate": self.performance_fee_rate,
        }
        for target, source in _PRESET_OVERRIDES[self.failure_mix].items():
            values[target] = assumptions.value(source)
        return values

    def configure(self, assumptions: Assumptions, sizing: SizingPlan) -> Assumptions:
        """The assumptions this run is measured under.

        Sizing is applied after the profile, so an interactive run at a reduced book size
        cannot be silently overridden by the merchant's stated one.
        """
        values = self.overrides(assumptions)
        values["book.size"] = sizing.book_size
        return assumptions.with_values(values)
