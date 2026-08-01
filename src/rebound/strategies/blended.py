from __future__ import annotations

from datetime import datetime, timedelta

from rebound.config import Assumptions, load_assumptions
from rebound.domain.entities import DebitAttempt, Mandate
from rebound.domain.reason_codes import terminates_retry_chain
from rebound.strategies.bank_aware import BankAware
from rebound.strategies.base import CustomerObservable, ProposedRetry
from rebound.strategies.reason_aware import ReasonAware
from rebound.strategies.salary_inference import SalaryInferenceModel, infer_salary_day

_DAYS_IN_CYCLE = 30


class Blended:
    """SPEC §4.6: strategies 3-5 combined by a scoring function.

    It builds a handful of candidate retry times — the reason code's prescribed wait, the
    baseline offset, the next predicted post-salary day — scores each on all three
    signals, and proposes the winner. One proposal, not a menu: the guard takes the
    earliest survivor, so offering fallbacks would log the rejects as compliance blocks
    and make this strategy's opportunity-forgone column incomparable with the baseline's.

    The three weights decide what happens when the signals disagree, and none of them is
    fitted. A Blended result is therefore a claim about `strategy.blended.weight_*` as
    much as about the components, which is why the sensitivity sweep covers them.
    """

    name = "Blended"

    def __init__(self, assumptions: Assumptions | None = None) -> None:
        assumptions = assumptions or load_assumptions()
        self._reason = ReasonAware(assumptions)
        self._bank = BankAware(assumptions)
        self._salary = SalaryInferenceModel.from_assumptions(assumptions)
        self._offsets_days: list[int] = list(
            assumptions.value("strategy.fixed_schedule.retry_offsets_days")
        )
        self._window_days = int(assumptions.value("strategy.salary_aware.target_window_days"))
        self._max_wait_days = int(assumptions.value("strategy.salary_aware.max_wait_days"))
        self._w_reason = float(assumptions.value("strategy.blended.weight_reason"))
        self._w_salary = float(assumptions.value("strategy.blended.weight_salary"))
        self._w_bank = float(assumptions.value("strategy.blended.weight_bank"))
        self._max_retries = int(assumptions.value("strategy.blended.max_retries"))

    def reset(self) -> None:
        self._bank.reset()

    def propose_retries(
        self,
        failed_attempt: DebitAttempt,
        mandate: Mandate,
        customer_view: CustomerObservable,
        history: list[DebitAttempt],
        clock: datetime,
    ) -> list[ProposedRetry]:
        self._bank.observe(customer_view.bank_id, customer_view.past_attempts)
        code = failed_attempt.reason_code
        if code is None or terminates_retry_chain(code):
            return []
        retries_so_far = sum(1 for a in history if a.is_retry)
        if retries_so_far >= min(self._max_retries, len(self._offsets_days)):
            return []
        original = history[0] if history else failed_attempt
        candidates = self._candidates(failed_attempt, original, retries_so_far, customer_view)
        if not candidates:
            return []
        salary_day = infer_salary_day(customer_view.past_attempts, self._salary)
        best = max(
            candidates,
            key=lambda when: (
                self._score(when, failed_attempt, customer_view, salary_day),
                -when.timestamp(),
            ),
        )
        return [
            ProposedRetry(
                mandate_id=mandate.id,
                scheduled_at=best,
                amount_paise=original.amount_paise,
            )
        ]

    def _candidates(
        self,
        failed_attempt: DebitAttempt,
        original: DebitAttempt,
        retries_so_far: int,
        view: CustomerObservable,
    ) -> list[datetime]:
        """Times worth considering, each at its own hour and at the bank's preferred one.

        Deduplicated and sorted so the candidate set — and therefore the argmax — does
        not depend on the order the components were consulted in.
        """
        baseline = original.scheduled_at + timedelta(days=self._offsets_days[retries_so_far])
        times = [baseline]
        delay = self._reason.delay_for(failed_attempt.reason_code)
        if delay is not None:
            times.append(failed_attempt.scheduled_at + delay)
        hour = self._bank.best_hour(view.bank_id)
        times += [t.replace(hour=hour, minute=0) for t in list(times)]
        return sorted({t for t in times if t > failed_attempt.scheduled_at})

    def _score(
        self,
        when: datetime,
        failed_attempt: DebitAttempt,
        view: CustomerObservable,
        salary_day: int | None,
    ) -> float:
        total = self._w_reason * self._reason_fit(when, failed_attempt)
        total += self._w_bank * self._bank.success_rate(view.bank_id, when.hour)
        if salary_day is not None:
            since = (when.day - salary_day) % _DAYS_IN_CYCLE
            total += self._w_salary * self._salary.success_probability(since)
        return total

    def _reason_fit(self, when: datetime, failed_attempt: DebitAttempt) -> float:
        """1.0 once the reason code's prescribed wait has elapsed, proportional before it.

        Scoring "has it waited long enough" rather than "how close to exactly the
        prescribed moment" keeps the component one-sided: a code that wants a long wait
        is never penalised for waiting longer, which is the salary signal's job to
        arbitrate.
        """
        delay = self._reason.delay_for(failed_attempt.reason_code)
        if delay is None or delay <= timedelta(0):
            return 1.0
        elapsed = when - failed_attempt.scheduled_at
        return min(1.0, elapsed / delay)
