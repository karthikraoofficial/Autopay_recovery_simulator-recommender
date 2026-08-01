from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from pydantic import AwareDatetime, ConfigDict, Field

from rebound.compliance.errors import HardDeclineViolation, RevokedMandateViolation
from rebound.compliance.rules import (
    AMOUNT_INTEGRITY,
    ATTEMPT_CAP,
    ATTEMPT_CAP_KEYS,
    BATCH_CLEARED_RAILS,
    HARD_DECLINE_STOP,
    PRE_DEBIT_NOTIFICATION,
    PRESENTATION_WINDOW,
    RESCHEDULABLE_RULES,
    REVOCATION_RESPECT,
    RULE_SOURCES,
)
from rebound.config import Assumptions
from rebound.domain.entities import (
    Bank,
    DebitAttempt,
    DomainModel,
    Mandate,
    MandateStatus,
)
from rebound.strategies.base import ProposedRetry

_ABSOLUTE = "absolute"


class RetryContext(DomainModel):
    """Everything the guard needs to judge a proposal. Carries no customer ground truth:
    the guard rules on legality, and legality does not depend on the balance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mandate: Mandate
    bank: Bank
    original_attempt: DebitAttempt
    history: tuple[DebitAttempt, ...] = Field(min_length=1)
    notified_at: AwareDatetime | None = None

    @property
    def retries_so_far(self) -> int:
        return sum(1 for a in self.history if a.is_retry)


class ComplianceBlock(DomainModel):
    """A retry that was legal to want and illegal to make.

    SPEC §3 surfaces these on the dashboard as opportunity forgone for compliance. That
    transparency is a selling point: a merchant who can see what we declined to do has
    reason to believe what we claim we did do.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    rule: str = Field(min_length=1)
    source_key: str = Field(min_length=1)
    mandate_id: str = Field(min_length=1)
    scheduled_at: AwareDatetime
    detail: str = Field(min_length=1)


class ComplianceReschedule(DomainModel):
    """A retry that a timing rule moved rather than killed.

    Reported apart from `ComplianceBlock` deliberately. A block is revenue the merchant
    forgoes to stay compliant; a reschedule is revenue it still collects, later. Adding
    them together would overstate the cost of compliance, and reporting only the total
    would hide which rules actually cost money and which merely cost days.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    rule: str = Field(min_length=1)
    mandate_id: str = Field(min_length=1)
    proposed_at: AwareDatetime
    rescheduled_at: AwareDatetime

    @property
    def delay(self) -> timedelta:
        return self.rescheduled_at - self.proposed_at


class ComplianceGuard:
    """SPEC §3. Every proposed retry passes through this before it can execute.

    A hard gate, not a warning (SPEC §0.3). Rules are checked in a fixed order with the
    absolute ones first, so a proposal that breaks several is reported by the most
    serious rather than by whichever happened to run first.
    """

    def __init__(self, assumptions: Assumptions, reschedule: bool = True) -> None:
        self._assert_absolute(assumptions, "compliance.hard_decline_stop.rule")
        self._assert_absolute(assumptions, "compliance.revocation_respect.rule")
        self._lead = timedelta(
            hours=float(assumptions.value("compliance.pre_debit_notification.lead_hours"))
        )
        self._notice_inherited = bool(
            assumptions.value("compliance.pre_debit_notification.retry_inherits_original_notice")
        )
        self._caps = {rail: int(assumptions.value(key)) for rail, key in ATTEMPT_CAP_KEYS.items()}
        self._clearing_weekdays = frozenset(
            int(d) for d in assumptions.value("compliance.enach.clearing_weekdays")
        )
        self._reschedule_horizon = timedelta(
            days=float(assumptions.value("compliance.reschedule_horizon_days"))
        )
        # False reproduces the behaviour this project shipped before phase 7: a blocked
        # proposal is abandoned and the retry chain ends. That is not a correct engine,
        # but it is what an unimproved merchant's scheduler does, so it stays available
        # as a measurable reference rather than being deleted as a fixed bug.
        self._reschedule = reschedule
        self.blocks: list[ComplianceBlock] = []
        self.reschedules: list[ComplianceReschedule] = []

    @staticmethod
    def _assert_absolute(assumptions: Assumptions, key: str) -> None:
        """SPEC §1.3 and CLAUDE.md: no exceptions, no config flag to disable. The entry
        exists to cite a source, so weakening it must stop the guard rather than quietly
        turn the rule off."""
        value = assumptions.value(key)
        if value != _ABSOLUTE:
            raise ValueError(
                f"{key} must be {_ABSOLUTE!r}, got {value!r}; this rule has no off switch"
            )

    @property
    def rule_sources(self) -> dict[str, str]:
        return dict(RULE_SOURCES)

    def _evaluate(self, proposal: ProposedRetry, context: RetryContext) -> ComplianceBlock | None:
        """Pure: judges the proposal and records nothing.

        Separate from `review` so `next_valid_slot` can probe candidate times without
        each probe appearing in the block log. Counting probes as blocks would make
        'opportunity forgone' a function of how hard we searched.
        """
        self._check_absolute(context)
        for check in (
            self._amount_integrity,
            self._attempt_cap,
            self._pre_debit_notification,
            self._presentation_window,
        ):
            block = check(proposal, context)
            if block is not None:
                return block
        return None

    def review(self, proposal: ProposedRetry, context: RetryContext) -> ComplianceBlock | None:
        """None means the retry may execute. Absolute violations raise instead."""
        block = self._evaluate(proposal, context)
        if block is not None:
            self.blocks.append(block)
        return block

    def next_valid_slot(
        self, proposal: ProposedRetry, context: RetryContext
    ) -> ProposedRetry | None:
        """The same retry moved to the next time it is legal, or None if no time is.

        A real merchant whose retry lands past the eNACH cutoff presents it in the next
        batch; it does not abandon the customer for the cycle. Dropping the proposal
        instead made a timing rule act as a termination rule, which understated every
        strategy that schedules outside the presentation window and made the compliance
        cost of a rule indistinguishable from the strategy's own quality.

        The search only ever moves a retry later and never changes its amount, so it
        cannot invent an attempt the merchant was not already entitled to make.
        """
        if not self._reschedule:
            block = self._evaluate(proposal, context)
            if block is None:
                return proposal
            self.blocks.append(block)
            return None
        candidate = proposal
        deadline = proposal.scheduled_at + self._reschedule_horizon
        while candidate.scheduled_at <= deadline:
            block = self._evaluate(candidate, context)
            if block is None:
                if candidate.scheduled_at != proposal.scheduled_at:
                    self.reschedules.append(
                        ComplianceReschedule(
                            rule=self._blocking_rule(proposal, context),
                            mandate_id=context.mandate.id,
                            proposed_at=proposal.scheduled_at,
                            rescheduled_at=candidate.scheduled_at,
                        )
                    )
                return candidate
            if block.rule not in RESCHEDULABLE_RULES:
                self.blocks.append(block)
                return None
            moved = self._advance(candidate, context, block.rule)
            if moved is None or moved <= candidate.scheduled_at:
                break
            candidate = candidate.model_copy(update={"scheduled_at": moved})
        # Ran out of horizon. Logged as a block against the rule that was still binding,
        # so the abandonment is attributed to a rule rather than to nothing.
        exhausted = self._evaluate(proposal, context)
        self.blocks.append(exhausted or self._horizon_block(proposal, context))
        return None

    def _blocking_rule(self, proposal: ProposedRetry, context: RetryContext) -> str:
        block = self._evaluate(proposal, context)
        return block.rule if block is not None else PRESENTATION_WINDOW

    def _horizon_block(self, proposal: ProposedRetry, context: RetryContext) -> ComplianceBlock:
        return self._block(
            PRESENTATION_WINDOW,
            RULE_SOURCES[PRESENTATION_WINDOW],
            proposal,
            context,
            f"no legal slot within {self._reschedule_horizon} of the proposed time",
        )

    def _advance(
        self, proposal: ProposedRetry, context: RetryContext, rule: str
    ) -> datetime | None:
        if rule == PRE_DEBIT_NOTIFICATION:
            # Only reachable under the strict reading, where each retry needs its own
            # notice. The earliest legal time is one lead period after the notice.
            return context.history[-1].scheduled_at + self._lead
        when = proposal.scheduled_at
        cutoff = context.bank.batch_cutoff_time
        if when.timetz().replace(tzinfo=None) > cutoff:
            when = (when + timedelta(days=1)).replace(
                hour=cutoff.hour, minute=cutoff.minute, second=0, microsecond=0
            )
        while when.weekday() not in self._clearing_weekdays:
            when += timedelta(days=1)
        return when

    def filter(
        self, proposals: Sequence[ProposedRetry], context: RetryContext
    ) -> ProposedRetry | None:
        """The earliest proposal that can legally run, rescheduled if need be.

        A strategy may offer alternatives; rejecting its first choice should cost it that
        choice, not the whole retry chain.
        """
        allowed = [
            slot for p in proposals if (slot := self.next_valid_slot(p, context)) is not None
        ]
        return min(allowed, key=lambda p: p.scheduled_at) if allowed else None

    def _check_absolute(self, context: RetryContext) -> None:
        if context.mandate.status is MandateStatus.REVOKED:
            raise RevokedMandateViolation(
                f"{REVOCATION_RESPECT}: mandate {context.mandate.id} is REVOKED; "
                "SPEC §3 forbids an attempt under any circumstance"
            )
        last = context.history[-1]
        if last.is_hard_decline:
            raise HardDeclineViolation(
                f"{HARD_DECLINE_STOP}: mandate {context.mandate.id} last failed with "
                f"{last.reason_code}, which terminates the retry chain (SPEC §1.3)"
            )

    def _amount_integrity(
        self, proposal: ProposedRetry, context: RetryContext
    ) -> ComplianceBlock | None:
        original = context.original_attempt.amount_paise
        if proposal.amount_paise == original:
            return None
        return self._block(
            AMOUNT_INTEGRITY,
            RULE_SOURCES[AMOUNT_INTEGRITY],
            proposal,
            context,
            f"retry amount {proposal.amount_paise} != original {original}",
        )

    def _attempt_cap(
        self, proposal: ProposedRetry, context: RetryContext
    ) -> ComplianceBlock | None:
        rail = context.mandate.rail
        cap = self._caps[rail]
        if context.retries_so_far < cap:
            return None
        return self._block(
            ATTEMPT_CAP,
            ATTEMPT_CAP_KEYS[rail],
            proposal,
            context,
            f"{context.retries_so_far} retries already made on {rail.value}, cap is {cap}",
        )

    def _pre_debit_notification(
        self, proposal: ProposedRetry, context: RetryContext
    ) -> ComplianceBlock | None:
        """SPEC §3, and the rule the whole model is most exposed on.

        Under the shipped reading a retry inherits the notice given for the original
        charge — same mandate, same amount, same billing cycle — so the rule binds on
        the original debit rather than on each retry. Under the strict reading every
        retry needs its own notice, which makes any sub-24h retry illegal. SPEC §11 does
        not settle this and no circular has been read.
        """
        if self._notice_inherited:
            # Inheritance is not a loophole: there must have been a valid notice on the
            # original charge for the retry to inherit. Otherwise the exemption would
            # launder an un-notified debit into a compliant-looking retry.
            notified_at = context.notified_at
            if notified_at is not None and (
                context.original_attempt.scheduled_at - notified_at >= self._lead
            ):
                return None
            return self._block(
                PRE_DEBIT_NOTIFICATION,
                RULE_SOURCES[PRE_DEBIT_NOTIFICATION],
                proposal,
                context,
                "no valid notice on the original charge for this retry to inherit",
            )
        notice_at = context.history[-1].scheduled_at
        if proposal.scheduled_at - notice_at >= self._lead:
            return None
        return self._block(
            PRE_DEBIT_NOTIFICATION,
            RULE_SOURCES[PRE_DEBIT_NOTIFICATION],
            proposal,
            context,
            f"retry is {proposal.scheduled_at - notice_at} after notice, needs {self._lead}",
        )

    def _presentation_window(
        self, proposal: ProposedRetry, context: RetryContext
    ) -> ComplianceBlock | None:
        if context.mandate.rail not in BATCH_CLEARED_RAILS:
            return None
        when = proposal.scheduled_at
        if when.weekday() not in self._clearing_weekdays:
            return self._block(
                PRESENTATION_WINDOW,
                RULE_SOURCES[PRESENTATION_WINDOW],
                proposal,
                context,
                f"{when.date()} is not a clearing day",
            )
        if when.timetz().replace(tzinfo=None) > context.bank.batch_cutoff_time:
            return self._block(
                PRESENTATION_WINDOW,
                "compliance.enach.presentation_cutoff.rule",
                proposal,
                context,
                f"{when.time()} is past the {context.bank.batch_cutoff_time} batch cutoff",
            )
        return None

    @staticmethod
    def _block(
        rule: str,
        source_key: str,
        proposal: ProposedRetry,
        context: RetryContext,
        detail: str,
    ) -> ComplianceBlock:
        return ComplianceBlock(
            rule=rule,
            source_key=source_key,
            mandate_id=context.mandate.id,
            scheduled_at=proposal.scheduled_at,
            detail=detail,
        )
