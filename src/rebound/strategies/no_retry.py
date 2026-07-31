from __future__ import annotations

from datetime import datetime

from rebound.domain.entities import DebitAttempt, Mandate
from rebound.strategies.base import CustomerObservable, ProposedRetry


class NoRetry:
    """SPEC §4.2, the floor. Establishes the natural recovery rate, which is zero by
    construction here: nothing is retried, so nothing is recovered after a failure.
    Its value is as the denominator, not as a candidate."""

    name = "NoRetry"

    def propose_retries(
        self,
        failed_attempt: DebitAttempt,
        mandate: Mandate,
        customer_view: CustomerObservable,  # unread by design; the boundary is mandatory
        history: list[DebitAttempt],
        clock: datetime,
    ) -> list[ProposedRetry]:
        return []
