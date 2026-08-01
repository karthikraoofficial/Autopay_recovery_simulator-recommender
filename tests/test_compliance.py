"""Phase-5 compliance guard tests, written before the guard exists.

SPEC §0.3: the guard is a hard gate, not a warning. A strategy that would violate a
rule must be *unable* to execute it, not flagged afterwards. Simulated lift that could
not be legally realised is fraud in a sales deck, so every rule in §3 gets a test that
a violating retry is blocked and that the block names the rule that stopped it.

Two of the six rules are absolute (SPEC §1.3, §3) and raise rather than block: there is
no policy tradeoff to log about retrying a dead mandate. The other four are policy and
produce a logged block, which the dashboard surfaces as opportunity forgone.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest

from rebound.compliance.errors import (
    ComplianceViolation,
    HardDeclineViolation,
    RevokedMandateViolation,
)
from rebound.compliance.guard import ComplianceGuard, RetryContext
from rebound.compliance.rules import (
    AMOUNT_INTEGRITY,
    ATTEMPT_CAP,
    HARD_DECLINE_STOP,
    PRE_DEBIT_NOTIFICATION,
    PRESENTATION_WINDOW,
    REVOCATION_RESPECT,
)
from rebound.config import Assumptions
from rebound.domain.entities import (
    AttemptOutcome,
    Bank,
    DebitAttempt,
    Mandate,
    MandateStatus,
    Rail,
)
from rebound.domain.reason_codes import ReasonCode
from rebound.strategies.base import ProposedRetry

# A Wednesday, chosen so weekday arithmetic in the eNACH presentation tests is legible.
BILLED_AT = datetime(2026, 4, 15, 10, 0, tzinfo=UTC)
AMOUNT = 49900


def _bank() -> Bank:
    return Bank(
        id="bank-000",
        name="Bank 000",
        td_rate=0.01,
        uptime_profile=dict.fromkeys(range(24), 0.99),
        batch_cutoff_time=time(hour=17),
        return_charge_paise=59000,
    )


def _mandate(
    rail: Rail = Rail.UPI_AUTOPAY, status: MandateStatus = MandateStatus.ACTIVE
) -> Mandate:
    return Mandate(
        id="mandate-000000",
        merchant_id="merchant-000",
        customer_id="cust-000000",
        rail=rail,
        max_amount_paise=1_000_000,
        created_at=datetime(2026, 1, 15, tzinfo=UTC),
        status=status,
    )


def _attempt(
    number: int = 1,
    reason: ReasonCode = ReasonCode.INSUFFICIENT_FUNDS,
    when: datetime = BILLED_AT,
) -> DebitAttempt:
    return DebitAttempt(
        id=f"a{number}",
        mandate_id="mandate-000000",
        scheduled_at=when,
        executed_at=when,
        amount_paise=AMOUNT,
        outcome=AttemptOutcome.FAILURE,
        reason_code=reason,
        attempt_number=number,
        is_retry=number > 1,
    )


def _context(
    rail: Rail = Rail.UPI_AUTOPAY,
    status: MandateStatus = MandateStatus.ACTIVE,
    history: tuple[DebitAttempt, ...] | None = None,
) -> RetryContext:
    history = history or (_attempt(),)
    return RetryContext(
        mandate=_mandate(rail, status),
        bank=_bank(),
        original_attempt=history[0],
        history=history,
        notified_at=BILLED_AT - timedelta(hours=24),
    )


def _retry(when: datetime, amount: int = AMOUNT) -> ProposedRetry:
    return ProposedRetry(mandate_id="mandate-000000", scheduled_at=when, amount_paise=amount)


# --- SPEC §3 rule 3: hard-decline stop (absolute) -----------------------------------


@pytest.mark.parametrize(
    "code",
    [
        ReasonCode.MANDATE_REVOKED,
        ReasonCode.MANDATE_EXPIRED,
        ReasonCode.ACCOUNT_FROZEN,
        ReasonCode.MANDATE_AMOUNT_EXCEEDED,
    ],
)
def test_a_retry_after_any_hard_decline_raises(shipped: Assumptions, code: ReasonCode) -> None:
    """Absolute, and deliberately not a logged block: there is no policy tradeoff to
    weigh about retrying a mandate that is legally dead."""
    guard = ComplianceGuard(shipped)
    context = _context(history=(_attempt(reason=code),))
    with pytest.raises(HardDeclineViolation) as caught:
        guard.review(_retry(BILLED_AT + timedelta(days=1)), context)
    assert HARD_DECLINE_STOP in str(caught.value)


def test_hard_decline_stop_cannot_be_disabled_by_config(shipped: Assumptions) -> None:
    """SPEC §1.3 and CLAUDE.md: no exceptions, no config flag. The assumptions entry is a
    citation, not a switch, and the guard refuses to start if it has been turned into
    one."""
    entries = dict(shipped.assumptions)
    entries["compliance.hard_decline_stop.rule"] = entries[
        "compliance.hard_decline_stop.rule"
    ].model_copy(update={"value": "advisory"})
    weakened = shipped.model_copy(update={"assumptions": entries})
    with pytest.raises(ValueError, match="absolute"):
        ComplianceGuard(weakened)


# --- SPEC §3 rule 4: revocation respect (absolute) ----------------------------------


def test_a_retry_against_a_revoked_mandate_raises(shipped: Assumptions) -> None:
    """SPEC §3: no attempt against a REVOKED mandate under any circumstance."""
    guard = ComplianceGuard(shipped)
    context = _context(status=MandateStatus.REVOKED)
    with pytest.raises(RevokedMandateViolation) as caught:
        guard.review(_retry(BILLED_AT + timedelta(days=1)), context)
    assert REVOCATION_RESPECT in str(caught.value)


def test_revocation_outranks_every_policy_rule(shipped: Assumptions) -> None:
    """A retry that is revoked *and* wrong-amount *and* past its cap must raise, not
    return whichever policy block happened to be checked first."""
    guard = ComplianceGuard(shipped)
    context = _context(
        status=MandateStatus.REVOKED,
        history=(_attempt(), _attempt(2), _attempt(3), _attempt(4)),
    )
    with pytest.raises(RevokedMandateViolation):
        guard.review(_retry(BILLED_AT + timedelta(minutes=1), amount=1), context)


def test_absolute_violations_share_a_base_class(shipped: Assumptions) -> None:
    assert issubclass(HardDeclineViolation, ComplianceViolation)
    assert issubclass(RevokedMandateViolation, ComplianceViolation)


# --- SPEC §3 rule 6: amount integrity -----------------------------------------------


@pytest.mark.parametrize("amount", [AMOUNT - 1, AMOUNT + 1, 1])
def test_a_retry_for_a_different_amount_is_blocked(shipped: Assumptions, amount: int) -> None:
    """SPEC §3: retry amount must equal the original. Partial debits are a separate
    feature needing their own compliance analysis, not a silent recovery tactic."""
    guard = ComplianceGuard(shipped)
    block = guard.review(_retry(BILLED_AT + timedelta(days=1), amount=amount), _context())
    assert block is not None
    assert block.rule == AMOUNT_INTEGRITY
    assert block.source_key in shipped.assumptions


def test_a_retry_for_the_original_amount_passes_amount_integrity(shipped: Assumptions) -> None:
    guard = ComplianceGuard(shipped)
    assert guard.review(_retry(BILLED_AT + timedelta(days=1)), _context()) is None


# --- SPEC §3 rule 2: attempt caps ---------------------------------------------------


def test_a_retry_beyond_the_per_rail_cap_is_blocked(shipped: Assumptions) -> None:
    guard = ComplianceGuard(shipped)
    cap = int(shipped.value("compliance.max_retries_per_cycle.upi_autopay"))
    history = (_attempt(),) + tuple(_attempt(n + 2) for n in range(cap))
    block = guard.review(_retry(BILLED_AT + timedelta(days=10)), _context(history=history))
    assert block is not None
    assert block.rule == ATTEMPT_CAP


def test_the_retry_exactly_at_the_cap_is_still_allowed(shipped: Assumptions) -> None:
    """Off-by-one here silently costs a merchant a legal retry, or grants an illegal
    one."""
    guard = ComplianceGuard(shipped)
    cap = int(shipped.value("compliance.max_retries_per_cycle.upi_autopay"))
    history = (_attempt(),) + tuple(_attempt(n + 2) for n in range(cap - 1))
    assert guard.review(_retry(BILLED_AT + timedelta(days=10)), _context(history=history)) is None


def test_attempt_caps_are_enforced_per_rail(shipped: Assumptions) -> None:
    """SPEC §1.1 makes the rails behave differently, so their caps are separate numbers
    and the guard must read the one belonging to the mandate's rail."""
    guard = ComplianceGuard(shipped)
    for rail, key in (
        (Rail.UPI_AUTOPAY, "compliance.max_retries_per_cycle.upi_autopay"),
        (Rail.ENACH, "compliance.max_retries_per_cycle.enach"),
        (Rail.CARD_EMANDATE, "compliance.max_retries_per_cycle.card_emandate"),
    ):
        cap = int(shipped.value(key))
        history = (_attempt(),) + tuple(_attempt(n + 2) for n in range(cap))
        # A Thursday well inside the clearing week, so eNACH's presentation rule is not
        # what does the blocking here.
        block = guard.review(
            _retry(datetime(2026, 4, 16, 10, 0, tzinfo=UTC)), _context(rail=rail, history=history)
        )
        assert block is not None, f"{rail} allowed a retry past its cap of {cap}"
        assert block.rule == ATTEMPT_CAP
        assert block.source_key == key


# --- SPEC §3 rule 1: pre-debit notification -----------------------------------------


def test_a_retry_inside_the_notice_window_is_blocked_when_notice_is_not_inherited(
    shipped: Assumptions,
) -> None:
    """The pessimistic reading of §3: every retry needs its own 24h notice. Under this
    setting a fast technical retry is illegal, which is why the flag is the single
    largest regulatory lever in the model."""
    entries = dict(shipped.assumptions)
    key = "compliance.pre_debit_notification.retry_inherits_original_notice"
    entries[key] = entries[key].model_copy(update={"value": False})
    strict = shipped.model_copy(update={"assumptions": entries})
    guard = ComplianceGuard(strict)
    block = guard.review(_retry(BILLED_AT + timedelta(hours=2)), _context())
    assert block is not None
    assert block.rule == PRE_DEBIT_NOTIFICATION
    assert block.source_key in strict.assumptions


def test_a_fast_retry_is_allowed_when_notice_is_inherited(shipped: Assumptions) -> None:
    """The shipped reading: the original debit was notified 24h ahead and a retry of that
    same charge carries it."""
    guard = ComplianceGuard(shipped)
    assert guard.review(_retry(BILLED_AT + timedelta(hours=2)), _context()) is None


def test_an_inherited_notice_must_actually_have_existed(shipped: Assumptions) -> None:
    """Inheritance is not a loophole. If the original charge was never notified 24h
    ahead, there is no notice for the retry to inherit, and allowing one would let the
    exemption launder a violation into a compliant-looking retry."""
    guard = ComplianceGuard(shipped)
    context = RetryContext(
        mandate=_mandate(),
        bank=_bank(),
        original_attempt=_attempt(),
        history=(_attempt(),),
        notified_at=BILLED_AT - timedelta(hours=1),
    )
    block = guard.review(_retry(BILLED_AT + timedelta(days=1)), context)
    assert block is not None
    assert block.rule == PRE_DEBIT_NOTIFICATION


def test_an_unnotified_original_charge_blocks_its_retries(shipped: Assumptions) -> None:
    guard = ComplianceGuard(shipped)
    context = RetryContext(
        mandate=_mandate(),
        bank=_bank(),
        original_attempt=_attempt(),
        history=(_attempt(),),
        notified_at=None,
    )
    block = guard.review(_retry(BILLED_AT + timedelta(days=1)), context)
    assert block is not None
    assert block.rule == PRE_DEBIT_NOTIFICATION


def test_the_notice_window_boundary_is_inclusive(shipped: Assumptions) -> None:
    """A retry exactly `lead_hours` after notice is compliant; one second earlier is
    not."""
    entries = dict(shipped.assumptions)
    key = "compliance.pre_debit_notification.retry_inherits_original_notice"
    entries[key] = entries[key].model_copy(update={"value": False})
    strict = shipped.model_copy(update={"assumptions": entries})
    guard = ComplianceGuard(strict)
    lead = timedelta(hours=float(strict.value("compliance.pre_debit_notification.lead_hours")))
    assert guard.review(_retry(BILLED_AT + lead), _context()) is None
    late = guard.review(_retry(BILLED_AT + lead - timedelta(seconds=1)), _context())
    assert late is not None
    assert late.rule == PRE_DEBIT_NOTIFICATION


# --- SPEC §3 rule 5: presentation windows (eNACH) -----------------------------------


@pytest.mark.parametrize("day", [18, 19])  # Saturday, Sunday
def test_an_enach_retry_on_a_non_clearing_day_is_blocked(shipped: Assumptions, day: int) -> None:
    guard = ComplianceGuard(shipped)
    block = guard.review(
        _retry(datetime(2026, 4, day, 10, 0, tzinfo=UTC)), _context(rail=Rail.ENACH)
    )
    assert block is not None
    assert block.rule == PRESENTATION_WINDOW


def test_an_enach_retry_after_the_batch_cutoff_is_blocked(shipped: Assumptions) -> None:
    """SPEC §3: eNACH respects batch cutoff times. Presenting after cutoff is not a late
    debit, it is no debit."""
    guard = ComplianceGuard(shipped)
    after_cutoff = datetime(2026, 4, 16, 18, 30, tzinfo=UTC)
    block = guard.review(_retry(after_cutoff), _context(rail=Rail.ENACH))
    assert block is not None
    assert block.rule == PRESENTATION_WINDOW


def test_an_enach_retry_on_a_clearing_day_before_cutoff_passes(shipped: Assumptions) -> None:
    guard = ComplianceGuard(shipped)
    assert (
        guard.review(_retry(datetime(2026, 4, 16, 10, 0, tzinfo=UTC)), _context(rail=Rail.ENACH))
        is None
    )


@pytest.mark.parametrize("rail", [Rail.UPI_AUTOPAY, Rail.CARD_EMANDATE])
def test_real_time_rails_are_not_bound_by_presentation_windows(
    shipped: Assumptions, rail: Rail
) -> None:
    """SPEC §1.1: UPI Autopay is real-time and stateless. Applying a batch clearing
    calendar to it would invent compliance blocks that do not exist and understate what
    a merchant can legally recover."""
    guard = ComplianceGuard(shipped)
    weekend_evening = datetime(2026, 4, 19, 22, 0, tzinfo=UTC)
    assert guard.review(_retry(weekend_evening), _context(rail=rail)) is None


# --- Logging and sourcing -----------------------------------------------------------


def test_every_block_names_its_rule_and_a_real_assumption_key(shipped: Assumptions) -> None:
    """SPEC §3: blocked retries are logged with the rule that blocked them and surfaced
    as opportunity forgone. A block that cannot say why is not evidence of anything."""
    guard = ComplianceGuard(shipped)
    block = guard.review(_retry(BILLED_AT + timedelta(days=1), amount=123), _context())
    assert block is not None
    assert block.rule
    assert block.source_key in shipped.assumptions
    assert block.mandate_id == "mandate-000000"
    assert block.detail


def test_the_guard_records_every_block_it_issues(shipped: Assumptions) -> None:
    guard = ComplianceGuard(shipped)
    guard.review(_retry(BILLED_AT + timedelta(days=1), amount=1), _context())
    guard.review(_retry(BILLED_AT + timedelta(days=2), amount=2), _context())
    assert len(guard.blocks) == 2
    assert {b.rule for b in guard.blocks} == {AMOUNT_INTEGRITY}


def test_filter_keeps_the_earliest_compliant_proposal(shipped: Assumptions) -> None:
    """A strategy may offer alternatives. The gate rejects the illegal ones and the
    harness takes the soonest survivor, rather than the whole chain dying because the
    strategy's first choice was non-compliant."""
    guard = ComplianceGuard(shipped)
    proposals = [
        _retry(BILLED_AT + timedelta(days=1), amount=1),  # blocked: wrong amount
        _retry(BILLED_AT + timedelta(days=5)),
        _retry(BILLED_AT + timedelta(days=9)),
    ]
    allowed = guard.filter(proposals, _context())
    assert allowed is not None
    assert allowed.scheduled_at == BILLED_AT + timedelta(days=5)
    assert len(guard.blocks) == 1


def test_filter_returns_nothing_when_every_proposal_is_blocked(shipped: Assumptions) -> None:
    guard = ComplianceGuard(shipped)
    proposals = [_retry(BILLED_AT + timedelta(days=1), amount=1), _retry(BILLED_AT, amount=2)]
    assert guard.filter(proposals, _context()) is None
    assert len(guard.blocks) == 2


def test_every_rule_cites_a_source_that_exists(shipped: Assumptions) -> None:
    """SPEC §0.1 applied to the guard: a rule whose source is not in assumptions.yaml
    cannot be rendered on the dashboard and cannot be audited."""
    guard = ComplianceGuard(shipped)
    assert guard.rule_sources
    for rule, key in guard.rule_sources.items():
        assert key in shipped.assumptions, f"rule {rule} cites missing key {key}"


def test_all_six_spec_rules_are_present(shipped: Assumptions) -> None:
    guard = ComplianceGuard(shipped)
    assert set(guard.rule_sources) == {
        PRE_DEBIT_NOTIFICATION,
        ATTEMPT_CAP,
        HARD_DECLINE_STOP,
        REVOCATION_RESPECT,
        PRESENTATION_WINDOW,
        AMOUNT_INTEGRITY,
    }


# --- Rescheduling: a timing rule moves a retry, it does not end the chain ------------
#
# Before this existed, `filter` returned None whenever every proposal was blocked and the
# harness ended the cycle. Because every strategy proposes exactly one candidate, a
# single presentation-window block killed the whole retry chain — a timing rule acting as
# a termination rule. That made a strategy's measured quality depend on whether it
# happened to schedule inside the eNACH batch window, and it understated by construction
# every strategy that did not.


def test_a_retry_past_the_batch_cutoff_moves_to_the_next_batch(shipped: Assumptions) -> None:
    guard = ComplianceGuard(shipped)
    context = _context(rail=Rail.ENACH)
    late = BILLED_AT.replace(hour=23)  # Wednesday, past the 17:00 cutoff
    slot = guard.next_valid_slot(_retry(late), context)
    assert slot is not None
    assert slot.scheduled_at > late
    assert slot.scheduled_at.timetz().replace(tzinfo=None) <= _bank().batch_cutoff_time
    assert not guard.blocks


def test_a_retry_on_a_non_clearing_day_moves_to_the_next_clearing_day(
    shipped: Assumptions,
) -> None:
    guard = ComplianceGuard(shipped)
    context = _context(rail=Rail.ENACH)
    saturday = datetime(2026, 4, 18, 10, 0, tzinfo=UTC)
    assert saturday.weekday() == 5
    slot = guard.next_valid_slot(_retry(saturday), context)
    assert slot is not None
    assert slot.scheduled_at.weekday() in {0, 1, 2, 3, 4}


def test_a_reschedule_is_counted_apart_from_a_block(shipped: Assumptions) -> None:
    """A block is revenue forgone; a reschedule is the same revenue collected later.
    Summing them would overstate the cost of compliance."""
    guard = ComplianceGuard(shipped)
    guard.next_valid_slot(_retry(BILLED_AT.replace(hour=23)), _context(rail=Rail.ENACH))
    assert len(guard.reschedules) == 1
    assert not guard.blocks
    assert guard.reschedules[0].rule == PRESENTATION_WINDOW
    assert guard.reschedules[0].delay > timedelta(0)


def test_a_legal_retry_is_not_recorded_as_rescheduled(shipped: Assumptions) -> None:
    guard = ComplianceGuard(shipped)
    slot = guard.next_valid_slot(_retry(BILLED_AT + timedelta(days=1)), _context())
    assert slot is not None
    assert not guard.reschedules
    assert not guard.blocks


def test_rescheduling_never_moves_a_retry_earlier(shipped: Assumptions) -> None:
    guard = ComplianceGuard(shipped)
    for hour in range(24):
        for day in range(7):
            when = datetime(2026, 4, 13 + day, hour, tzinfo=UTC)
            slot = guard.next_valid_slot(_retry(when), _context(rail=Rail.ENACH))
            if slot is not None:
                assert slot.scheduled_at >= when


def test_rescheduling_never_changes_the_amount(shipped: Assumptions) -> None:
    """Amount integrity survives the search. A slot found by altering the debit would be
    the partial-debit feature SPEC §3 puts out of scope, arriving by the back door."""
    guard = ComplianceGuard(shipped)
    slot = guard.next_valid_slot(_retry(BILLED_AT.replace(hour=23)), _context(rail=Rail.ENACH))
    assert slot is not None
    assert slot.amount_paise == AMOUNT


def test_an_exhausted_attempt_cap_is_not_rescheduled(shipped: Assumptions) -> None:
    """No later slot restores a cap that is spent for the cycle. Rescheduling this would
    manufacture an attempt the merchant may not make."""
    cap = int(shipped.value("compliance.max_retries_per_cycle.upi_autopay"))
    history = (_attempt(),) + tuple(_attempt(number=n + 2) for n in range(cap))
    guard = ComplianceGuard(shipped)
    context = _context(history=history)
    assert guard.next_valid_slot(_retry(BILLED_AT + timedelta(days=1)), context) is None
    assert len(guard.blocks) == 1
    assert guard.blocks[0].rule == ATTEMPT_CAP
    assert not guard.reschedules


def test_a_wrong_amount_is_not_rescheduled(shipped: Assumptions) -> None:
    guard = ComplianceGuard(shipped)
    bad = _retry(BILLED_AT + timedelta(days=1), amount=AMOUNT + 1)
    assert guard.next_valid_slot(bad, _context()) is None
    assert [b.rule for b in guard.blocks] == [AMOUNT_INTEGRITY]


def test_the_search_gives_up_at_the_configured_horizon(shipped: Assumptions) -> None:
    """A merchant does not chase a slot forever, and an unbounded search would let a
    retry drift into the next billing cycle where it is a different charge."""
    from tests.conftest import override

    narrow = override(shipped, {"compliance.enach.clearing_weekdays": [0]})
    guard = ComplianceGuard(override(narrow, {"compliance.reschedule_horizon_days": 1}))
    tuesday = datetime(2026, 4, 14, 10, 0, tzinfo=UTC)
    assert guard.next_valid_slot(_retry(tuesday), _context(rail=Rail.ENACH)) is None
    assert len(guard.blocks) == 1
    assert not guard.reschedules


def test_searching_does_not_inflate_the_block_log(shipped: Assumptions) -> None:
    """Each probe evaluates the rules. Counting probes as blocks would make 'opportunity
    forgone' a function of how hard the guard searched."""
    guard = ComplianceGuard(shipped)
    saturday = datetime(2026, 4, 18, 23, 0, tzinfo=UTC)
    assert guard.next_valid_slot(_retry(saturday), _context(rail=Rail.ENACH)) is not None
    assert len(guard.blocks) == 0
    assert len(guard.reschedules) == 1


def test_the_guard_can_be_built_to_abandon_blocked_retries(shipped: Assumptions) -> None:
    """The pre-phase-7 behaviour, kept as a measurable reference rather than deleted.
    `NoReschedule` runs on this so a merchant without a re-presenting scheduler has a
    baseline that describes what they actually run."""
    guard = ComplianceGuard(shipped, reschedule=False)
    late = BILLED_AT.replace(hour=23)
    assert guard.next_valid_slot(_retry(late), _context(rail=Rail.ENACH)) is None
    assert [b.rule for b in guard.blocks] == [PRESENTATION_WINDOW]
    assert not guard.reschedules


def test_abandoning_still_lets_a_legal_retry_through(shipped: Assumptions) -> None:
    guard = ComplianceGuard(shipped, reschedule=False)
    slot = guard.next_valid_slot(_retry(BILLED_AT + timedelta(days=1)), _context())
    assert slot is not None
    assert not guard.blocks
