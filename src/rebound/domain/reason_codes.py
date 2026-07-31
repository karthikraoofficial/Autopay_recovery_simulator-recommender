from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict


class DeclineCategory(StrEnum):
    BUSINESS = "business_decline"
    TECHNICAL = "technical_decline"


class DeclineSeverity(StrEnum):
    SOFT = "soft"
    HARD = "hard"


class Recoverability(StrEnum):
    """Qualitative only. Any numeric recovery probability belongs in assumptions.yaml."""

    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    VERY_HIGH = "very_high"


class ReasonCode(StrEnum):
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
    MANDATE_AMOUNT_EXCEEDED = "MANDATE_AMOUNT_EXCEEDED"
    TECHNICAL_DECLINE = "TECHNICAL_DECLINE"
    BANK_UNAVAILABLE = "BANK_UNAVAILABLE"
    MANDATE_REVOKED = "MANDATE_REVOKED"
    MANDATE_EXPIRED = "MANDATE_EXPIRED"
    ACCOUNT_FROZEN = "ACCOUNT_FROZEN"
    INVALID_PIN = "INVALID_PIN"


class ReasonCodeProfile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: ReasonCode
    category: DeclineCategory
    severity: DeclineSeverity
    recoverability: Recoverability
    note: str


_PROFILES: tuple[ReasonCodeProfile, ...] = (
    ReasonCodeProfile(
        code=ReasonCode.INSUFFICIENT_FUNDS,
        category=DeclineCategory.BUSINESS,
        severity=DeclineSeverity.SOFT,
        recoverability=Recoverability.HIGH,
        note="The core opportunity. Purely a timing problem.",
    ),
    ReasonCodeProfile(
        code=ReasonCode.LIMIT_EXCEEDED,
        category=DeclineCategory.BUSINESS,
        severity=DeclineSeverity.SOFT,
        recoverability=Recoverability.MEDIUM,
        note="Daily/per-txn cap hit. Retry next day or split.",
    ),
    ReasonCodeProfile(
        code=ReasonCode.MANDATE_AMOUNT_EXCEEDED,
        category=DeclineCategory.BUSINESS,
        severity=DeclineSeverity.HARD,
        recoverability=Recoverability.LOW,
        note="Debit exceeds mandate cap. Needs a new mandate.",
    ),
    ReasonCodeProfile(
        code=ReasonCode.TECHNICAL_DECLINE,
        category=DeclineCategory.TECHNICAL,
        severity=DeclineSeverity.SOFT,
        recoverability=Recoverability.VERY_HIGH,
        note="Bank/NPCI side. Retry shortly.",
    ),
    ReasonCodeProfile(
        code=ReasonCode.BANK_UNAVAILABLE,
        category=DeclineCategory.TECHNICAL,
        severity=DeclineSeverity.SOFT,
        recoverability=Recoverability.VERY_HIGH,
        note="Downtime window. Retry after the window closes.",
    ),
    ReasonCodeProfile(
        code=ReasonCode.MANDATE_REVOKED,
        category=DeclineCategory.BUSINESS,
        severity=DeclineSeverity.HARD,
        recoverability=Recoverability.NONE,
        note="Customer cancelled. Stop immediately.",
    ),
    ReasonCodeProfile(
        code=ReasonCode.MANDATE_EXPIRED,
        category=DeclineCategory.BUSINESS,
        severity=DeclineSeverity.HARD,
        recoverability=Recoverability.NONE,
        note="Requires re-auth. Route to dunning, not retry.",
    ),
    ReasonCodeProfile(
        code=ReasonCode.ACCOUNT_FROZEN,
        category=DeclineCategory.BUSINESS,
        severity=DeclineSeverity.HARD,
        recoverability=Recoverability.NONE,
        note="Stop.",
    ),
    ReasonCodeProfile(
        code=ReasonCode.INVALID_PIN,
        category=DeclineCategory.BUSINESS,
        severity=DeclineSeverity.SOFT,
        recoverability=Recoverability.LOW,
        note="Not applicable to autopay execution; included for completeness.",
    ),
)

REASON_CODE_PROFILES: Mapping[ReasonCode, ReasonCodeProfile] = MappingProxyType(
    {p.code: p for p in _PROFILES}
)

_missing = sorted(c for c in ReasonCode if c not in REASON_CODE_PROFILES)
if _missing:
    raise RuntimeError(f"reason codes without a profile: {_missing}")


def profile_for(code: ReasonCode) -> ReasonCodeProfile:
    return REASON_CODE_PROFILES[code]


def is_hard_decline(code: ReasonCode) -> bool:
    return REASON_CODE_PROFILES[code].severity is DeclineSeverity.HARD


def terminates_retry_chain(code: ReasonCode) -> bool:
    """SPEC §1.3/§3: absolute, and deliberately has no config flag to disable it."""
    return is_hard_decline(code)


HARD_DECLINE_CODES: frozenset[ReasonCode] = frozenset(c for c in ReasonCode if is_hard_decline(c))
