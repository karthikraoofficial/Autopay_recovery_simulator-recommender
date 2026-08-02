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

    The two are genuinely different operations. `INTERACTIVE` caps the book so that moving
    a control and seeing the answer move takes seconds; `PUBLICATION` simulates the
    merchant's actual book and is the number that goes in front of them. Neither is a
    different model — an interactive run is the same model measured less precisely, on a
    smaller population, and its intervals are correspondingly wider.

    **Interactive results are per simulated book of `SizingPlan.book_size` and are never
    scaled to the merchant's book.** Multiplying by `book_size / simulated` would be a
    one-line convenience and it is deliberately absent: it asserts that mandates are
    independent and identically distributed, which nothing here has tested, and real books
    concentrate on signup dates, verticals and a handful of banks. A fabricated figure that
    looks like the merchant's own is worse than an honest one that does not. If a merchant
    wants the number for their book, run `PUBLICATION`, which simulates it.
    """

    INTERACTIVE = "interactive"
    PUBLICATION = "publication"


class BookSizeTooLargeError(ValueError):
    """A publication run whose runtime would be measured in hours."""


class SizingPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sizing: Sizing
    book_size: int = Field(gt=0)
    requested_book_size: int = Field(gt=0)
    n_seeds: int = Field(gt=1)
    # Shown before the user commits to a run. Machine-dependent and approximate; the
    # dashboard displays elapsed time beside it so a bad estimate is visible as one.
    estimated_seconds: float = Field(ge=0.0)

    @property
    def is_capped(self) -> bool:
        """Whether the simulated book is smaller than the merchant asked for. The UI must
        say so rather than presenting the result as their book's."""
        return self.book_size < self.requested_book_size


def sizing_plan(assumptions: Assumptions, sizing: Sizing, book_size: int) -> SizingPlan:
    """Interactive caps the book for speed; publication simulates the one the merchant has.

    Publication seed count comes from the sensitivity keys, which is the setting at which
    the phase-7 sweep found the comparison significant. Its book size does not: precision
    comes from seeds, and using the merchant's real book is what makes the quoted rupee
    figure theirs rather than an extrapolation.
    """
    rate = float(assumptions.value("api.seconds_per_mandate_seed"))
    if sizing is Sizing.PUBLICATION:
        cap = int(assumptions.value("api.publication_book_size_cap"))
        seeds = int(assumptions.value("harness.sensitivity.seeds"))
        if book_size > cap:
            hours = book_size * seeds * rate / 3600
            raise BookSizeTooLargeError(
                f"a publication run on {book_size:,} mandates would take about "
                f"{hours:.1f} hours. The cap is {cap:,} "
                f"(api.publication_book_size_cap). Run the smaller interactive sizing, "
                f"or raise the cap deliberately."
            )
        return SizingPlan(
            sizing=sizing,
            book_size=book_size,
            requested_book_size=book_size,
            n_seeds=seeds,
            estimated_seconds=book_size * seeds * rate,
        )
    # A ceiling, not a replacement: a merchant smaller than the cap is simulated whole.
    size = min(book_size, int(assumptions.value("api.interactive_book_size")))
    seeds = int(assumptions.value("api.interactive_seeds"))
    return SizingPlan(
        sizing=sizing,
        book_size=size,
        requested_book_size=book_size,
        n_seeds=seeds,
        estimated_seconds=size * seeds * rate,
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
