from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from enum import StrEnum

from pydantic import ConfigDict, Field

from rebound.config import Assumptions
from rebound.domain.entities import AttemptOutcome, DomainModel
from rebound.domain.reason_codes import ReasonCode
from rebound.engine.failure import FailureEngine
from rebound.engine.protocol import AttemptRequest
from rebound.population.book import generate_book

# Which assumption keys move which reason code. Printed alongside a gap so the report
# says what to turn, not merely that something is off.
KNOBS: Mapping[ReasonCode, tuple[str, ...]] = {
    ReasonCode.INSUFFICIENT_FUNDS: (
        "population.balance.cushion_lognormal_sigma",
        "population.balance.spend_decay_rate.low",
        "population.balance.spend_decay_rate.mid",
        "population.balance.spend_decay_rate.high",
        "population.balance.carryover_fraction_mean",
    ),
    ReasonCode.TECHNICAL_DECLINE: (
        "population.bank.td_rate_min",
        "population.bank.td_rate_max",
        "engine.rail_technical_multiplier.upi_autopay",
    ),
    ReasonCode.BANK_UNAVAILABLE: (
        "population.bank.uptime_daytime",
        "population.bank.uptime_night_trough",
        "population.bank.downtime_windows_mean",
    ),
    ReasonCode.LIMIT_EXCEEDED: (
        "engine.daily_limit_consumption_share_mean",
        "engine.daily_limit_consumption_lognormal_sigma",
        "engine.daily_limit_paise.upi_autopay",
    ),
    ReasonCode.MANDATE_REVOKED: (
        "engine.reaction.revocation_base_hazard",
        "engine.reaction.revocation_attempt_exponent",
    ),
    ReasonCode.MANDATE_EXPIRED: ("engine.mandate_expiry_hazard_per_cycle",),
    ReasonCode.ACCOUNT_FROZEN: ("engine.account_frozen_hazard_per_month",),
    ReasonCode.MANDATE_AMOUNT_EXCEEDED: ("population.mandate.cap_multiple_of_ticket",),
    ReasonCode.INVALID_PIN: (),
}


class Reachability(StrEnum):
    """Whether a code can appear in this measurement at all.

    Without this, a code that is absent by construction reads identically to a code the
    config simply fails to produce, and someone would go tuning knobs that cannot move
    it.
    """

    MEASURED = "measured"
    RETRY_ONLY = "retry_only"
    NOT_MODELLED = "not_modelled"


REACHABILITY: Mapping[ReasonCode, Reachability] = {
    ReasonCode.INSUFFICIENT_FUNDS: Reachability.MEASURED,
    ReasonCode.LIMIT_EXCEEDED: Reachability.MEASURED,
    ReasonCode.TECHNICAL_DECLINE: Reachability.MEASURED,
    ReasonCode.BANK_UNAVAILABLE: Reachability.MEASURED,
    ReasonCode.MANDATE_EXPIRED: Reachability.MEASURED,
    ReasonCode.ACCOUNT_FROZEN: Reachability.MEASURED,
    ReasonCode.MANDATE_AMOUNT_EXCEEDED: Reachability.MEASURED,
    # Induced by retry aggression (SPEC §2.3), so it cannot occur on a first attempt.
    ReasonCode.MANDATE_REVOKED: Reachability.RETRY_ONLY,
    # SPEC §1.3 says INVALID_PIN is not applicable to autopay execution and is listed
    # only for completeness. The engine therefore never emits it, and no knob will.
    ReasonCode.INVALID_PIN: Reachability.NOT_MODELLED,
}


class CodeGap(DomainModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: ReasonCode
    target_share: float = Field(ge=0.0, le=1.0)
    observed_share: float = Field(ge=0.0, le=1.0)
    reachability: Reachability
    knobs: tuple[str, ...] = ()

    @property
    def gap(self) -> float:
        """Observed minus target. Positive means the model produces too much of it."""
        return self.observed_share - self.target_share

    @property
    def missing_mechanism(self) -> bool:
        """Wanted, reachable in principle, and never produced. This is the one case that
        is neither a tuning problem nor expected: something the model cannot do."""
        return (
            self.target_share > 0.0
            and self.observed_share == 0.0
            and self.reachability is Reachability.MEASURED
        )


class CalibrationReport(DomainModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    seeds: tuple[int, ...] = Field(min_length=1)
    attempts: int = Field(ge=1)
    failures: int = Field(ge=0)
    gaps: tuple[CodeGap, ...] = Field(min_length=1)

    @property
    def failure_rate(self) -> float:
        return self.failures / self.attempts

    @property
    def total_variation_distance(self) -> float:
        """Half the L1 distance between the observed and target mixes: the single number
        for how far the current config lands from the target. 0 is exact, 1 is disjoint.

        Computed over every code, including those `codes_outside_measurement` names. Part
        of the distance is therefore structural rather than a tuning error, and reporting
        this number without that list beside it would overstate how badly the config is
        set.
        """
        return 0.5 * sum(abs(g.gap) for g in self.gaps)

    @property
    def missing_mechanisms(self) -> tuple[ReasonCode, ...]:
        return tuple(g.code for g in self.gaps if g.missing_mechanism)

    @property
    def codes_outside_measurement(self) -> tuple[CodeGap, ...]:
        """Wanted by the target but not observable here: no knob will close these."""
        return tuple(
            g
            for g in self.gaps
            if g.target_share > 0.0 and g.reachability is not Reachability.MEASURED
        )

    def worst(self, limit: int = 3) -> tuple[CodeGap, ...]:
        tunable = [g for g in self.gaps if g.reachability is Reachability.MEASURED]
        return tuple(sorted(tunable, key=lambda g: abs(g.gap), reverse=True)[:limit])


def _observed_mix(assumptions: Assumptions, seed: int) -> tuple[Counter[ReasonCode], int]:
    """First attempts only. That is the mix a merchant reports and the mix a target
    distribution is quoted against; retry outcomes are conditional on a strategy and
    would make the number depend on which strategy happened to be running."""
    book = generate_book(assumptions, seed)
    engine = FailureEngine(assumptions, seed)
    banks = {b.id: b for b in book.banks}
    customers = {c.id: c for c in book.customers}
    codes: Counter[ReasonCode] = Counter()
    attempts = 0
    for mandate in book.mandates:
        customer = customers[mandate.customer_id]
        bank = banks[customer.bank_id]
        amount = min(book.merchant.avg_ticket_paise, mandate.max_amount_paise)
        for cycle in range(book.months):
            when = book.start_at.replace(
                month=(book.start_at.month - 1 + cycle) % 12 + 1,
                year=book.start_at.year + (book.start_at.month - 1 + cycle) // 12,
                day=mandate.created_at.day,
                hour=bank.batch_cutoff_time.hour,
            )
            result = engine.execute(
                AttemptRequest(
                    mandate=mandate,
                    customer=customer,
                    bank=bank,
                    cycle_id=f"{mandate.id}:c{cycle:02d}",
                    attempt_number=1,
                    scheduled_at=when,
                    amount_paise=amount,
                )
            )
            attempts += 1
            if result.outcome is AttemptOutcome.FAILURE and result.reason_code is not None:
                codes[result.reason_code] += 1
    return codes, attempts


def calibrate(
    assumptions: Assumptions,
    target: Mapping[ReasonCode, float],
    seeds: tuple[int, ...],
) -> CalibrationReport:
    """Measure how far the configured engine's reason-code mix lands from `target`.

    This reports, it does not fit. An optimiser over these knobs would be easy and
    dishonest: the target itself is a guess until a pilot merchant supplies real
    reason-code counts (SPEC §11 names this the largest uncertainty in the model), and
    a config tuned to fit an invented target would look calibrated while being no more
    grounded than it started.
    """
    total = sum(target.values())
    if abs(total - 1.0) > 1e-9:
        raise ValueError(f"target mix must sum to 1.0, got {total}")
    if not seeds:
        raise ValueError("calibration needs at least one seed")
    codes: Counter[ReasonCode] = Counter()
    attempts = 0
    for seed in seeds:
        seed_codes, seed_attempts = _observed_mix(assumptions, seed)
        codes.update(seed_codes)
        attempts += seed_attempts
    failures = sum(codes.values())
    gaps = tuple(
        CodeGap(
            code=code,
            target_share=float(target.get(code, 0.0)),
            observed_share=(codes[code] / failures) if failures else 0.0,
            reachability=REACHABILITY[code],
            knobs=KNOBS[code],
        )
        for code in ReasonCode
    )
    return CalibrationReport(seeds=seeds, attempts=attempts, failures=failures, gaps=gaps)
