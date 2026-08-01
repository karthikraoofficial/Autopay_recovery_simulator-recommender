from __future__ import annotations

from datetime import datetime, timedelta

from rebound.config import Assumptions, load_assumptions
from rebound.domain.entities import DebitAttempt, Mandate
from rebound.domain.reason_codes import terminates_retry_chain
from rebound.strategies.base import CustomerObservable, ProposedRetry
from rebound.strategies.salary_inference import (
    SalaryInferenceModel,
    infer_salary_day,
)


class SalaryAware:
    """SPEC §4.4: infer the salary credit day, land the retry 0-2 days after it.

    The inference runs on `customer_view.past_attempts` and nothing else. The true
    `salary_credit_day` is not on `CustomerObservable` and this strategy never sees it,
    so its lift is whatever an imperfect estimate of payroll timing is worth — which is
    the number worth having, since a perfect one is not purchasable.

    Where it cannot identify a credit day it falls back to the baseline offsets, so its
    floor is FixedSchedule and any difference is attributable to the inference alone.
    """

    name = "SalaryAware"

    def __init__(self, assumptions: Assumptions | None = None) -> None:
        assumptions = assumptions or load_assumptions()
        self._offsets_days: list[int] = list(
            assumptions.value("strategy.fixed_schedule.retry_offsets_days")
        )
        self._model = SalaryInferenceModel.from_assumptions(assumptions)
        self._window_days = int(assumptions.value("strategy.salary_aware.target_window_days"))
        self._max_wait_days = int(assumptions.value("strategy.salary_aware.max_wait_days"))
        self._max_retries = int(assumptions.value("strategy.salary_aware.max_retries"))

    def propose_retries(
        self,
        failed_attempt: DebitAttempt,
        mandate: Mandate,
        customer_view: CustomerObservable,
        history: list[DebitAttempt],
        clock: datetime,
    ) -> list[ProposedRetry]:
        code = failed_attempt.reason_code
        if code is None or terminates_retry_chain(code):
            return []
        retries_so_far = sum(1 for a in history if a.is_retry)
        if retries_so_far >= min(self._max_retries, len(self._offsets_days)):
            return []
        original = history[0] if history else failed_attempt
        earliest = original.scheduled_at + timedelta(days=self._offsets_days[retries_so_far])
        salary_day = infer_salary_day(customer_view.past_attempts, self._model)
        when = self._after_credit(earliest, salary_day) if salary_day is not None else earliest
        if when <= failed_attempt.scheduled_at:
            return []
        return [
            ProposedRetry(
                mandate_id=mandate.id,
                scheduled_at=when,
                amount_paise=original.amount_paise,
            )
        ]

    def _after_credit(self, earliest: datetime, salary_day: int) -> datetime:
        """The first day at or after `earliest` that sits inside the post-credit window.

        Never earlier than the baseline would have retried: pulling a retry forward to
        catch a credit that has already passed would be reading the past, and the money
        is gone by then anyway. The wait is capped, because deferring a retry three weeks
        to reach the next payroll costs intent decay for a timing gain that arrives after
        the customer has stopped caring.
        """
        for offset in range(self._max_wait_days + 1):
            candidate = earliest + timedelta(days=offset)
            since = (candidate.day - salary_day) % 30
            if since <= self._window_days:
                return candidate
        return earliest
