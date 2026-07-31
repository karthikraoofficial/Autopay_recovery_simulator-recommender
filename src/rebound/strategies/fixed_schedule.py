from __future__ import annotations

from datetime import datetime, timedelta

from rebound.config import Assumptions, load_assumptions
from rebound.domain.entities import DebitAttempt, Mandate
from rebound.strategies.base import CustomerObservable, ProposedRetry


class FixedSchedule:
    """SPEC §4.1, the baseline: retry at T+1, T+3, T+7 from the original attempt.

    This strategy must never gain reason-code logic, bank awareness, salary inference,
    or any other conditioning. It exists to represent what merchants do today, and every
    reported lift number is a difference against it. The moment it gets smarter the
    baseline moves, previously reported lift silently changes, and no result in this
    repo is comparable to any earlier one. If it looks like it needs a branch, the
    branch belongs in ReasonAware.
    """

    name = "FixedSchedule"

    def __init__(self, assumptions: Assumptions | None = None) -> None:
        assumptions = assumptions or load_assumptions()
        self._offsets_days: list[int] = list(
            assumptions.value("strategy.fixed_schedule.retry_offsets_days")
        )

    def propose_retries(
        self,
        failed_attempt: DebitAttempt,
        mandate: Mandate,
        customer_view: CustomerObservable,  # unread by design; the boundary is mandatory
        history: list[DebitAttempt],
        clock: datetime,
    ) -> list[ProposedRetry]:
        retries_so_far = sum(1 for a in history if a.is_retry)
        if retries_so_far >= len(self._offsets_days):
            return []
        original = history[0] if history else failed_attempt
        offset = self._offsets_days[retries_so_far]
        return [
            ProposedRetry(
                mandate_id=mandate.id,
                scheduled_at=original.scheduled_at + timedelta(days=offset),
                amount_paise=original.amount_paise,
            )
        ]
