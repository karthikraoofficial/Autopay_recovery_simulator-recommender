from __future__ import annotations


# N818 wants an `Error` suffix. Not renamed: `HardDeclineViolation` is the name the
# SPEC §5.2 guardrail test fixes as the API, and "violation" is the compliance term for
# what these are. Renaming to satisfy a lint would change a frozen contract.
class ComplianceViolation(Exception):  # noqa: N818
    """An absolute rule was broken (SPEC §1.3, §3).

    Raised rather than logged, and deliberately so. SPEC §0.3 requires a strategy that
    would violate a rule to be *unable* to execute it: the policy rules in §3 involve a
    tradeoff worth recording as opportunity forgone, but retrying a legally dead mandate
    does not. There is nothing to weigh and nothing to surface on a dashboard — it is a
    bug in whatever proposed it.
    """


class HardDeclineViolation(ComplianceViolation):
    """A retry was proposed after a hard decline. SPEC §1.3: no exceptions, no config
    flag to disable."""


class RevokedMandateViolation(ComplianceViolation):
    """An attempt was proposed against a REVOKED mandate. SPEC §3: under any
    circumstance."""
