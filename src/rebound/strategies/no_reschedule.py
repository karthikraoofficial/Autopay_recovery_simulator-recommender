from __future__ import annotations

from rebound.strategies.fixed_schedule import FixedSchedule


class NoReschedule(FixedSchedule):
    """T+1/T+3/T+7, and a blocked retry is abandoned rather than moved.

    The fourth reference point, and the one a merchant actually recognises. `NoRetry` is
    a floor nobody runs and `FixedSchedule` assumes a scheduler that re-presents a retry
    the eNACH window rejected — which is a capability, not a given. This strategy assumes
    it does not exist: a retry landing on a weekend or past the batch cutoff is simply
    lost, and the cycle ends.

    Two lifts therefore have to be quoted separately and permanently, because they are
    bought in different ways:

      NoReschedule -> FixedSchedule   the value of re-presenting blocked retries at all.
                                      Pure scheduling plumbing. No reason codes, no
                                      inference, no model.
      FixedSchedule -> Blended        the value of the retry logic itself.

    Reporting only the total would sell plumbing as intelligence. It also flatters the
    strategies: before phase 7 this abandonment was a bug in the harness rather than a
    modelled behaviour, and it alone made every phase-7 strategy look worse than the
    baseline it was measured against.

    It inherits `FixedSchedule` and adds no scheduling logic of its own — the difference
    lives entirely in the guard, so the baseline's proposals stay frozen as SPEC §4.1
    requires.
    """

    name = "NoReschedule"

    # Read by the harness when it constructs this strategy's ComplianceGuard.
    reschedules_blocked_retries = False
