from __future__ import annotations

from rebound.domain.entities import Rail

# Rule names. Constants rather than string literals because every block carries one and
# the dashboard groups on them: a typo would silently split one rule into two categories.
PRE_DEBIT_NOTIFICATION = "pre_debit_notification"
ATTEMPT_CAP = "attempt_cap"
HARD_DECLINE_STOP = "hard_decline_stop"
REVOCATION_RESPECT = "revocation_respect"
PRESENTATION_WINDOW = "presentation_window"
AMOUNT_INTEGRITY = "amount_integrity"

# SPEC §0.1 applied to the guard: every rule points at the assumptions entry that
# justifies it, so the dashboard can render the rule beside its source and confidence.
RULE_SOURCES: dict[str, str] = {
    HARD_DECLINE_STOP: "compliance.hard_decline_stop.rule",
    REVOCATION_RESPECT: "compliance.revocation_respect.rule",
    AMOUNT_INTEGRITY: "compliance.amount_integrity.rule",
    ATTEMPT_CAP: "compliance.max_retries_per_cycle.upi_autopay",
    PRE_DEBIT_NOTIFICATION: "compliance.pre_debit_notification.lead_hours",
    PRESENTATION_WINDOW: "compliance.enach.clearing_weekdays",
}

ATTEMPT_CAP_KEYS: dict[Rail, str] = {
    Rail.UPI_AUTOPAY: "compliance.max_retries_per_cycle.upi_autopay",
    Rail.ENACH: "compliance.max_retries_per_cycle.enach",
    Rail.CARD_EMANDATE: "compliance.max_retries_per_cycle.card_emandate",
}

# SPEC §1.1: UPI Autopay is real-time and card e-mandate is bank-side pre-authorised.
# Only eNACH clears in batches, so only eNACH has a presentation window. Applying a
# clearing calendar to the real-time rails would invent blocks that do not exist and
# understate what a merchant can legally recover.
BATCH_CLEARED_RAILS: frozenset[Rail] = frozenset({Rail.ENACH})
