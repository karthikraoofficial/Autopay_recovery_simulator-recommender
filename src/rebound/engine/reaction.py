from __future__ import annotations

from rebound.config import Assumptions


class CustomerReaction:
    """SPEC §2.3. How a customer responds to being retried.

    Pure functions of the config: this decides probabilities, the engine does the
    drawing. Without this model every strategy converges on retrying infinitely, which
    would be both wrong and destructive to a real merchant's customer relationships.
    The downside of aggression has to be in the simulation or the simulation is a
    sales lie.
    """

    def __init__(self, assumptions: Assumptions) -> None:
        self._intent_decay = float(
            assumptions.value("engine.reaction.intent_decay_per_failed_attempt")
        )
        self._revocation_base = float(assumptions.value("engine.reaction.revocation_base_hazard"))
        self._revocation_exponent = float(
            assumptions.value("engine.reaction.revocation_attempt_exponent")
        )
        self._revocation_intent_weight = float(
            assumptions.value("engine.reaction.revocation_intent_weight")
        )
        self._topup_notified = float(
            assumptions.value("engine.reaction.topup_probability_notified")
        )
        self._topup_silent = float(
            assumptions.value("engine.reaction.topup_probability_not_notified")
        )

    def decayed_intent(self, initial_intent: float, failed_attempts: int) -> float:
        """Intent erodes over an episode: each failure is a reminder that the customer
        could just cancel instead."""
        return initial_intent * (1.0 - self._intent_decay) ** max(0, failed_attempts)

    def revocation_probability(self, failed_attempts: int, intent: float) -> float:
        """Rises with the number of failures already suffered, super-linearly by default:
        the fourth retry annoys more than the first did. A customer whose intent has
        decayed is likelier to walk, so the two mechanisms compound.

        This is the cost side of retry aggression. It is the reason an unbounded retry
        strategy loses money rather than merely costing attempts.
        """
        if failed_attempts <= 0:
            return 0.0
        pressure = float(failed_attempts) ** self._revocation_exponent
        reluctance = 1.0 + self._revocation_intent_weight * (1.0 - intent)
        return min(1.0, self._revocation_base * pressure * reluctance)

    def topup_probability(self, notified: bool, intent: float) -> float:
        """A customer who still wants the service and knows a debit is coming may move
        money in to cover it. Scaled by intent, because someone who has given up on the
        service has no reason to."""
        base = self._topup_notified if notified else self._topup_silent
        return min(1.0, base * intent)
