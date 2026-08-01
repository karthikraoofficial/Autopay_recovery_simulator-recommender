from __future__ import annotations

from datetime import datetime
from typing import NamedTuple

from rebound.config import Assumptions
from rebound.domain.entities import (
    AttemptOutcome,
    DowntimeWindow,
    MandateStatus,
    Rail,
)
from rebound.domain.reason_codes import ReasonCode
from rebound.engine.limits import DailyLimits
from rebound.engine.protocol import AttemptRequest, AttemptResult
from rebound.engine.reaction import CustomerReaction
from rebound.population.balance import BalanceProcess
from rebound.seeding import stream

_RAIL_TECHNICAL_KEYS: tuple[tuple[Rail, str], ...] = (
    (Rail.UPI_AUTOPAY, "engine.rail_technical_multiplier.upi_autopay"),
    (Rail.ENACH, "engine.rail_technical_multiplier.enach"),
    (Rail.CARD_EMANDATE, "engine.rail_technical_multiplier.card_emandate"),
)

_STATUS_REASONS: dict[MandateStatus, ReasonCode] = {
    MandateStatus.REVOKED: ReasonCode.MANDATE_REVOKED,
    MandateStatus.EXPIRED: ReasonCode.MANDATE_EXPIRED,
}


class Decline(NamedTuple):
    code: ReasonCode
    induced_revocation: bool = False


def _in_window(window: DowntimeWindow, moment: datetime) -> bool:
    if window.weekday is not None and window.weekday != moment.weekday():
        return False
    return window.start <= moment.time() < window.end


class FailureEngine:
    """SPEC §2.2. The checks run in the order the spec fixes, and that order is the
    model: it decides the reason-code mix, which is the thing a pilot merchant will
    check us against first.

    Every draw is addressed by (mandate, cycle, date), never by call order, so two
    strategies retrying on the same day face identical luck and any difference between
    them is timing rather than sampling.
    """

    def __init__(
        self, assumptions: Assumptions, seed: int, balance: BalanceProcess | None = None
    ) -> None:
        self._seed = seed
        self._balance = balance or BalanceProcess(assumptions, seed)
        self._limits = DailyLimits(assumptions, seed)
        self._reaction = CustomerReaction(assumptions)
        self._expiry_hazard = float(assumptions.value("engine.mandate_expiry_hazard_per_cycle"))
        self._frozen_hazard = float(assumptions.value("engine.account_frozen_hazard_per_month"))
        self._rail_technical = {
            rail: float(assumptions.value(key)) for rail, key in _RAIL_TECHNICAL_KEYS
        }

    def execute(self, request: AttemptRequest) -> AttemptResult:
        checks = (
            self._mandate_status,
            self._bank_availability,
            self._mandate_cap,
            self._daily_limit,
            self._funds,
        )
        for check in checks:
            decline = check(request)
            if decline is not None:
                return AttemptResult(
                    outcome=AttemptOutcome.FAILURE,
                    reason_code=decline.code,
                    executed_at=request.scheduled_at,
                    induced_revocation=decline.induced_revocation,
                )
        return AttemptResult(outcome=AttemptOutcome.SUCCESS, executed_at=request.scheduled_at)

    def _mandate_status(self, request: AttemptRequest) -> Decline | None:
        """SPEC §2.2 step 1, plus the §2.3 reaction that changes status between attempts.

        Revocation is evaluated here rather than after the outcome because that is what
        it is: the customer cancelled in the gap since the last failure, so the debit
        never reaches their balance. Checking it first is also what makes the retry cost
        real — an over-retried mandate dies before it can succeed.
        """
        mandate = request.mandate
        if mandate.status is MandateStatus.PAUSED:
            raise ValueError(
                "PAUSED mandates are not modelled; no SPEC §1.3 reason code describes them"
            )
        if mandate.status is not MandateStatus.ACTIVE:
            return Decline(_STATUS_REASONS[mandate.status])
        revocation = self._induced_revocation(request)
        if revocation is not None:
            return revocation
        if self._expired(request):
            return Decline(ReasonCode.MANDATE_EXPIRED)
        if self._frozen(request):
            return Decline(ReasonCode.ACCOUNT_FROZEN)
        return None

    def _induced_revocation(self, request: AttemptRequest) -> Decline | None:
        failed = request.attempt_number - 1
        if failed <= 0:
            return None
        intent = self._reaction.decayed_intent(request.customer.intent_score, failed)
        probability = self._reaction.revocation_probability(failed, intent)
        return (
            Decline(ReasonCode.MANDATE_REVOKED, induced_revocation=True)
            if self._roll(request, "revocation") < probability
            else None
        )

    def _expired(self, request: AttemptRequest) -> bool:
        """Keyed by cycle, not by attempt: a mandate that has expired stays expired for
        every retry in that cycle rather than flickering back to life."""
        rng = stream(self._seed, "expiry", request.mandate.id, request.cycle_id)
        return float(rng.random()) < self._expiry_hazard

    def _frozen(self, request: AttemptRequest) -> bool:
        """Keyed by customer-month: an account freeze is a state that persists, not a
        per-attempt coin flip."""
        when = request.scheduled_at
        rng = stream(self._seed, "frozen", request.customer.id, f"{when.year:04d}-{when.month:02d}")
        return float(rng.random()) < self._frozen_hazard

    def _bank_availability(self, request: AttemptRequest) -> Decline | None:
        """SPEC §2.2 step 2. A scheduled downtime window is deterministic — it is the
        signal `BankAware` is meant to find — while the residual unavailability and the
        technical decline rate are rolled."""
        bank = request.bank
        moment = request.scheduled_at
        if any(_in_window(w, moment) for w in bank.downtime_windows):
            return Decline(ReasonCode.BANK_UNAVAILABLE)
        multiplier = self._rail_technical[request.mandate.rail]
        if self._roll(request, "uptime") < (1.0 - bank.uptime_profile[moment.hour]) * multiplier:
            return Decline(ReasonCode.BANK_UNAVAILABLE)
        if self._roll(request, "technical") < bank.td_rate * multiplier:
            return Decline(ReasonCode.TECHNICAL_DECLINE)
        return None

    def _mandate_cap(self, request: AttemptRequest) -> Decline | None:
        """SPEC §2.2 step 3. Deterministic, and a hard decline: no amount of retrying
        makes a debit fit inside a cap it exceeds."""
        if request.amount_paise > request.mandate.max_amount_paise:
            return Decline(ReasonCode.MANDATE_AMOUNT_EXCEEDED)
        return None

    def _daily_limit(self, request: AttemptRequest) -> Decline | None:
        exceeded = self._limits.would_exceed(
            request.customer,
            request.mandate.rail,
            request.scheduled_at.date(),
            request.amount_paise,
        )
        return Decline(ReasonCode.LIMIT_EXCEEDED) if exceeded else None

    def _funds(self, request: AttemptRequest) -> Decline | None:
        """SPEC §2.2 step 5, and the whole point of the project. The balance comes from
        the §2.1 process, so whether this fires depends on where the attempt lands
        relative to the customer's salary credit — which is what a timing strategy has
        to exploit."""
        on = request.scheduled_at.date()
        balance = self._balance.balance_paise(request.customer, on)
        if balance >= request.amount_paise:
            return None
        if self._tops_up(request):
            return None
        return Decline(ReasonCode.INSUFFICIENT_FUNDS)

    def _tops_up(self, request: AttemptRequest) -> bool:
        """SPEC §2.3. A customer who still wants the service can move money in to cover
        the debit, and is likelier to do so having been told it is coming."""
        failed = request.attempt_number - 1
        intent = self._reaction.decayed_intent(request.customer.intent_score, failed)
        probability = self._reaction.topup_probability(request.pre_debit_notified, intent)
        return self._roll(request, "topup") < probability

    def _roll(self, request: AttemptRequest, purpose: str) -> float:
        """One addressed stream per (purpose, mandate, cycle, hour).

        Keyed by the hour, not the calendar date. `_bank_availability` compares this draw
        against an hourly threshold (`uptime_profile[hour]`), so a date-keyed draw was
        comparing one random number against a threshold that moved through the day, and
        — worse — gave a same-day retry the identical draw to the attempt it followed.
        A fast retry after a technical decline could therefore only ever reproduce that
        decline, which silently made SPEC §4.3's two-hour retry impossible to benefit
        from and would have shown up as a real finding about retry timing.

        Keyed by attempt time rather than attempt number, so two strategies retrying at
        the same instant still face identical luck; where the outcome should differ by
        attempt number it is the threshold that moves, not the draw.
        """
        return float(
            stream(
                self._seed,
                purpose,
                request.mandate.id,
                request.cycle_id,
                request.scheduled_at.strftime("%Y-%m-%dT%H"),
            ).random()
        )

    def daily_limit_for(self, rail: Rail) -> int:
        return self._limits.limit_paise(rail)
