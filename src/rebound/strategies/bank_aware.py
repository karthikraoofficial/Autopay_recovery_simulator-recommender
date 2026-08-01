from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta

from rebound.config import Assumptions, load_assumptions
from rebound.domain.entities import AttemptOutcome, DebitAttempt, Mandate
from rebound.domain.reason_codes import terminates_retry_chain
from rebound.strategies.base import CustomerObservable, ProposedRetry


class _Slot:
    """Observed attempts and successes for one (bank, hour)."""

    __slots__ = ("attempts", "successes")

    def __init__(self) -> None:
        self.attempts = 0
        self.successes = 0


class BankAware:
    """SPEC §4.5: avoid a bank's downtime and low-uptime hours, prefer good slots.

    The bank's downtime windows and uptime profile are *not* on `CustomerObservable`,
    and putting them there would be handing the strategy ground truth. So this strategy
    does what a merchant does: it tallies its own attempts by (bank, hour of day) and
    prefers the hours that have worked. The tally starts empty and is built only from
    outcomes the harness has already shown it.

    Two consequences worth stating rather than hiding. The tally learns fastest for the
    banks holding most of the book, so the concentration in
    `population.bank.share_concentration` drives how much this can be worth. And with an
    optimistic prior the strategy settles on an early-morning slot quickly and rarely
    leaves it, which makes it close to a fixed "retry in the morning" policy — most of
    what it earns is avoiding overnight hours, not fine-grained slot selection.
    """

    name = "BankAware"

    def __init__(self, assumptions: Assumptions | None = None) -> None:
        assumptions = assumptions or load_assumptions()
        self._offsets_days: list[int] = list(
            assumptions.value("strategy.fixed_schedule.retry_offsets_days")
        )
        self._hours = range(
            int(assumptions.value("strategy.bank_aware.earliest_hour")),
            int(assumptions.value("strategy.bank_aware.latest_hour")) + 1,
        )
        self._prior_attempts = float(assumptions.value("strategy.bank_aware.prior_attempts"))
        self._prior_rate = float(assumptions.value("strategy.bank_aware.prior_success_rate"))
        self._max_retries = int(assumptions.value("strategy.bank_aware.max_retries"))
        self._slots: dict[tuple[str, int], _Slot] = {}
        self._seen: set[str] = set()

    def reset(self) -> None:
        self._slots = {}
        self._seen = set()

    def observe(self, bank_id: str, attempts: Iterable[DebitAttempt]) -> None:
        """Fold outcomes into the tally, once each.

        `past_attempts` grows and is re-presented on every call, so attempts are counted
        by id. Double-counting would make the strategy's confidence a function of how
        often it was asked rather than of how much it has seen.
        """
        for attempt in attempts:
            if attempt.id in self._seen:
                continue
            self._seen.add(attempt.id)
            slot = self._slots.setdefault((bank_id, attempt.scheduled_at.hour), _Slot())
            slot.attempts += 1
            slot.successes += int(attempt.outcome is AttemptOutcome.SUCCESS)

    def success_rate(self, bank_id: str, hour: int) -> float:
        slot = self._slots.get((bank_id, hour))
        successes = self._prior_attempts * self._prior_rate
        attempts = self._prior_attempts
        if slot is not None:
            successes += slot.successes
            attempts += slot.attempts
        return successes / attempts

    def best_hour(self, bank_id: str) -> int:
        return max(self._hours, key=lambda h: (self.success_rate(bank_id, h), -h))

    def propose_retries(
        self,
        failed_attempt: DebitAttempt,
        mandate: Mandate,
        customer_view: CustomerObservable,
        history: list[DebitAttempt],
        clock: datetime,
    ) -> list[ProposedRetry]:
        self.observe(customer_view.bank_id, customer_view.past_attempts)
        code = failed_attempt.reason_code
        if code is None or terminates_retry_chain(code):
            return []
        retries_so_far = sum(1 for a in history if a.is_retry)
        if retries_so_far >= min(self._max_retries, len(self._offsets_days)):
            return []
        original = history[0] if history else failed_attempt
        day = original.scheduled_at + timedelta(days=self._offsets_days[retries_so_far])
        when = day.replace(hour=self.best_hour(customer_view.bank_id), minute=0)
        if when <= failed_attempt.scheduled_at:
            return []
        return [
            ProposedRetry(
                mandate_id=mandate.id,
                scheduled_at=when,
                amount_paise=original.amount_paise,
            )
        ]
