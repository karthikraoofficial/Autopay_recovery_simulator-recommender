from __future__ import annotations

from datetime import datetime, timedelta

from rebound.config import Assumptions, load_assumptions
from rebound.domain.entities import DebitAttempt, Mandate
from rebound.domain.reason_codes import ReasonCode, terminates_retry_chain
from rebound.strategies.base import CustomerObservable, ProposedRetry


class ReasonAware:
    """SPEC §4.3: branch on the reason code.

    The first strategy that treats an insufficient-balance failure differently from a
    revoked mandate, which SPEC §0.4 calls the thesis of the whole project. Everything it
    knows comes from the reason code on the last attempt — no bank downtime windows (that
    is `BankAware`) and no salary inference (that is `SalaryAware`), so its lift over the
    baseline is attributable to reason-code branching alone.

    It proposes one time, not a menu of candidates. A strategy offering fallbacks would
    have its rejected candidates counted as compliance blocks and its "opportunity
    forgone" column would stop being comparable with the baseline's.
    """

    name = "ReasonAware"

    def __init__(self, assumptions: Assumptions | None = None) -> None:
        assumptions = assumptions or load_assumptions()
        self._max_retries = int(assumptions.value("strategy.reason_aware.max_retries"))
        hours = {
            ReasonCode.TECHNICAL_DECLINE: float(
                assumptions.value("strategy.reason_aware.technical_decline_delay_hours")
            ),
            ReasonCode.BANK_UNAVAILABLE: float(
                assumptions.value("strategy.reason_aware.bank_unavailable_delay_hours")
            ),
        }
        days = {
            ReasonCode.INSUFFICIENT_FUNDS: float(
                assumptions.value("strategy.reason_aware.insufficient_funds_delay_days")
            ),
            ReasonCode.LIMIT_EXCEEDED: float(
                assumptions.value("strategy.reason_aware.limit_exceeded_delay_days")
            ),
            ReasonCode.INVALID_PIN: float(
                assumptions.value("strategy.reason_aware.invalid_pin_delay_days")
            ),
        }
        self._delays: dict[ReasonCode, timedelta] = {
            **{code: timedelta(hours=h) for code, h in hours.items()},
            **{code: timedelta(days=d) for code, d in days.items()},
        }

    def delay_for(self, code: ReasonCode) -> timedelta | None:
        """The wait this strategy prescribes for a code, or None if it proposes nothing.

        Public so `Blended` can score against the same six numbers rather than keeping a
        second copy of them that could drift out of step with this one.
        """
        return self._delays.get(code)

    def propose_retries(
        self,
        failed_attempt: DebitAttempt,
        mandate: Mandate,
        customer_view: CustomerObservable,  # read for nothing but its type; see SPEC §4
        history: list[DebitAttempt],
        clock: datetime,
    ) -> list[ProposedRetry]:
        code = failed_attempt.reason_code
        if code is None or terminates_retry_chain(code):
            # SPEC §1.3. The harness also stops here and the guard raises on it, but a
            # strategy that would propose against a dead mandate is broken regardless of
            # what catches it.
            return []
        retries_so_far = sum(1 for a in history if a.is_retry)
        if retries_so_far >= self._max_retries:
            return []
        delay = self._delays.get(code)
        if delay is None:
            return []
        original = history[0] if history else failed_attempt
        return [
            ProposedRetry(
                mandate_id=mandate.id,
                # Measured from the failure being reacted to, not from the original
                # attempt: a technical decline two days into an episode still deserves a
                # retry two hours later, not two hours after the original charge.
                scheduled_at=failed_attempt.scheduled_at + delay,
                amount_paise=original.amount_paise,
            )
        ]
