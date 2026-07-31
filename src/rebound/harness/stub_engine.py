from __future__ import annotations

from rebound.config import Assumptions
from rebound.domain.entities import AttemptOutcome
from rebound.domain.reason_codes import ReasonCode, is_hard_decline
from rebound.engine.protocol import AttemptRequest, AttemptResult
from rebound.seeding import stream

_REASON_MIX_KEY = "stub_engine.reason_mix.{code}"
_RETRY_BASE_KEY = "stub_engine.retry_success_base.{code}"


class ScriptedEngine:
    """Phase-2 stand-in for the failure engine, so the harness can be tested before the
    thing it measures exists. It has no balance process and no customer reaction model;
    outcomes are scripted from seeded draws. Phase 4 replaces it behind `PaymentEngine`.

    The draws are addressed by (mandate, cycle, calendar day), never by call order, so
    two strategies retrying on the same day face identical luck. That is what makes the
    comparison paired: any difference between strategies is timing, not sampling noise.
    """

    def __init__(self, assumptions: Assumptions, seed: int) -> None:
        self._seed = seed
        self._first_failure_rate = float(
            assumptions.value("stub_engine.first_attempt_failure_rate")
        )
        self._decay = float(assumptions.value("stub_engine.retry_success_decay_per_day"))
        self._revocation_prob = float(
            assumptions.value("stub_engine.induced_revocation_prob_per_retry")
        )
        self._mix = self._load_mix(assumptions)
        self._retry_base = {
            code: float(assumptions.value(_RETRY_BASE_KEY.format(code=code.value.lower())))
            for code in ReasonCode
            if not is_hard_decline(code)
        }

    @staticmethod
    def _load_mix(assumptions: Assumptions) -> tuple[tuple[ReasonCode, float], ...]:
        """Cumulative, in ReasonCode declaration order — never dict iteration order."""
        weights = [
            (code, float(assumptions.value(_REASON_MIX_KEY.format(code=code.value.lower()))))
            for code in ReasonCode
        ]
        total = sum(w for _, w in weights)
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"stub_engine.reason_mix.* must sum to 1.0, got {total}")
        cumulative: list[tuple[ReasonCode, float]] = []
        running = 0.0
        for code, weight in weights:
            running += weight
            cumulative.append((code, running))
        return tuple(cumulative)

    def execute(self, request: AttemptRequest) -> AttemptResult:
        if request.attempt_number == 1:
            return self._first_attempt(request)
        return self._retry(request)

    def _first_attempt(self, request: AttemptRequest) -> AttemptResult:
        rng = stream(self._seed, "first", request.mandate.id, request.cycle_id)
        if float(rng.random()) >= self._first_failure_rate:
            return AttemptResult(outcome=AttemptOutcome.SUCCESS, executed_at=request.scheduled_at)
        return AttemptResult(
            outcome=AttemptOutcome.FAILURE,
            reason_code=self._draw_reason(float(rng.random())),
            executed_at=request.scheduled_at,
        )

    def _draw_reason(self, u: float) -> ReasonCode:
        for code, ceiling in self._mix:
            if u < ceiling:
                return code
        return self._mix[-1][0]

    def _retry(self, request: AttemptRequest) -> AttemptResult:
        original = request.original_reason_code
        if original is None or is_hard_decline(original):
            raise ValueError(f"stub engine asked to retry after {original}")
        rng = stream(
            self._seed,
            "retry",
            request.mandate.id,
            request.cycle_id,
            request.scheduled_at.date().isoformat(),
        )
        u, v = float(rng.random()), float(rng.random())
        decayed = (1.0 - self._decay) ** request.days_since_original
        probability = self._retry_base[original] * decayed
        if u < probability:
            return AttemptResult(outcome=AttemptOutcome.SUCCESS, executed_at=request.scheduled_at)
        if v < self._revocation_prob * (request.attempt_number - 1):
            # SPEC §2.3: retry aggression has a cost. The customer walks, and the hard
            # decline that follows terminates the chain on its own.
            return AttemptResult(
                outcome=AttemptOutcome.FAILURE,
                reason_code=ReasonCode.MANDATE_REVOKED,
                executed_at=request.scheduled_at,
                induced_revocation=True,
            )
        return AttemptResult(
            outcome=AttemptOutcome.FAILURE,
            reason_code=original,
            executed_at=request.scheduled_at,
        )
