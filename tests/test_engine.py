"""Phase-4 failure engine tests.

The load-bearing ones are the check-order tests: SPEC §2.2 fixes the order, and the
order is what decides the reason-code mix. A model that produced the right overall
failure rate through the wrong checks would be tuned to look right and would respond
to strategies in the wrong way.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest

from rebound.config import Assumptions
from rebound.domain.entities import (
    AttemptOutcome,
    BalanceProcessParams,
    Bank,
    Customer,
    DowntimeWindow,
    IncomeBand,
    Mandate,
    MandateStatus,
    Rail,
)
from rebound.domain.reason_codes import ReasonCode
from rebound.engine.failure import FailureEngine
from rebound.engine.protocol import AttemptRequest
from rebound.engine.reaction import CustomerReaction

SEED = 20260731
WHEN = datetime(2026, 4, 20, 17, 0, tzinfo=UTC)


def _bank(
    uptime: float = 1.0,
    td_rate: float = 0.0,
    windows: tuple[DowntimeWindow, ...] = (),
) -> Bank:
    return Bank(
        id="bank-000",
        name="Bank 000",
        td_rate=td_rate,
        uptime_profile=dict.fromkeys(range(24), uptime),
        downtime_windows=windows,
        batch_cutoff_time=time(hour=17),
        return_charge_paise=59000,
    )


def _customer(income: int = 5_000_000, intent: float = 0.9) -> Customer:
    return Customer(
        id="cust-000000",
        bank_id="bank-000",
        salary_credit_day=1,
        income_band=IncomeBand.MID,
        balance_process=BalanceProcessParams(
            monthly_income_paise=income, spend_decay_rate=0.06, lognormal_sigma=0.45
        ),
        intent_score=intent,
    )


def _mandate(status: MandateStatus = MandateStatus.ACTIVE, cap: int = 1_000_000) -> Mandate:
    return Mandate(
        id="mandate-000000",
        merchant_id="merchant-000",
        customer_id="cust-000000",
        rail=Rail.UPI_AUTOPAY,
        max_amount_paise=cap,
        created_at=datetime(2026, 1, 20, tzinfo=UTC),
        status=status,
    )


def _request(
    mandate: Mandate | None = None,
    customer: Customer | None = None,
    bank: Bank | None = None,
    amount: int = 49900,
    attempt: int = 1,
    when: datetime = WHEN,
    notified: bool = False,
) -> AttemptRequest:
    return AttemptRequest(
        mandate=mandate or _mandate(),
        customer=customer or _customer(),
        bank=bank or _bank(),
        cycle_id="mandate-000000:c03",
        attempt_number=attempt,
        scheduled_at=when,
        amount_paise=amount,
        original_reason_code=ReasonCode.INSUFFICIENT_FUNDS if attempt > 1 else None,
        days_since_original=attempt - 1,
        pre_debit_notified=notified,
    )


# --- SPEC §2.2 check order ----------------------------------------------------------


def test_revoked_mandate_declines_before_anything_else_is_checked(shipped: Assumptions) -> None:
    """Step 1 wins over every later check. A mandate that is revoked and also out of
    funds and on a dead bank must report MANDATE_REVOKED: reporting the balance problem
    would invite a retry against a mandate that must never be attempted again."""
    engine = FailureEngine(shipped, SEED)
    request = _request(
        mandate=_mandate(status=MandateStatus.REVOKED),
        bank=_bank(uptime=0.0, td_rate=1.0),
        customer=_customer(income=1),
        amount=99_999_999,
    )
    result = engine.execute(request)
    assert result.reason_code is ReasonCode.MANDATE_REVOKED


def test_expired_mandate_reports_expiry_not_a_downstream_failure(shipped: Assumptions) -> None:
    engine = FailureEngine(shipped, SEED)
    result = engine.execute(
        _request(mandate=_mandate(status=MandateStatus.EXPIRED), bank=_bank(uptime=0.0))
    )
    assert result.reason_code is ReasonCode.MANDATE_EXPIRED


def test_bank_downtime_wins_over_insufficient_funds(shipped: Assumptions) -> None:
    """Step 2 before step 5. Getting this backwards would relabel every outage during a
    thin-balance window as a funding problem, and a reason-aware strategy would then
    wait for a salary credit instead of retrying in minutes."""
    engine = FailureEngine(shipped, SEED)
    window = DowntimeWindow(weekday=None, start=time(16), end=time(18))
    result = engine.execute(_request(bank=_bank(windows=(window,)), customer=_customer(income=1)))
    assert result.reason_code is ReasonCode.BANK_UNAVAILABLE


def test_mandate_cap_wins_over_insufficient_funds(shipped: Assumptions) -> None:
    """Step 3 before step 5, and it is a hard decline: a debit above the mandate cap is
    not a timing problem and must not start a retry chain."""
    engine = FailureEngine(shipped, SEED)
    result = engine.execute(_request(mandate=_mandate(cap=10_000), amount=50_000))
    assert result.reason_code is ReasonCode.MANDATE_AMOUNT_EXCEEDED


def test_daily_limit_wins_over_insufficient_funds(shipped: Assumptions) -> None:
    """Step 4 before step 5."""
    engine = FailureEngine(shipped, SEED)
    limit = engine.daily_limit_for(Rail.UPI_AUTOPAY)
    result = engine.execute(
        _request(mandate=_mandate(cap=limit * 10), amount=limit, customer=_customer(income=1))
    )
    assert result.reason_code is ReasonCode.LIMIT_EXCEEDED


def test_a_healthy_attempt_with_funds_succeeds(shipped: Assumptions) -> None:
    engine = FailureEngine(shipped, SEED)
    result = engine.execute(_request(customer=_customer(income=500_000_000)))
    assert result.outcome is AttemptOutcome.SUCCESS
    assert result.reason_code is None


def test_insufficient_funds_fires_when_only_the_balance_is_short(shipped: Assumptions) -> None:
    """Intent is zeroed to switch off the §2.3 top-up, which can otherwise rescue an
    empty account and would make this a test of two mechanisms at once."""
    engine = FailureEngine(shipped, SEED)
    result = engine.execute(_request(customer=_customer(income=1, intent=0.0)))
    assert result.reason_code is ReasonCode.INSUFFICIENT_FUNDS


def test_paused_mandates_raise_rather_than_being_silently_mapped(shipped: Assumptions) -> None:
    """No SPEC §1.3 reason code describes PAUSED. Mapping it to the nearest code would
    put a fabricated reason into the mix the whole thesis rests on."""
    engine = FailureEngine(shipped, SEED)
    with pytest.raises(ValueError, match="PAUSED"):
        engine.execute(_request(mandate=_mandate(status=MandateStatus.PAUSED)))


def test_technical_decline_is_reported_when_the_bank_rejects(shipped: Assumptions) -> None:
    engine = FailureEngine(shipped, SEED)
    result = engine.execute(
        _request(bank=_bank(uptime=1.0, td_rate=1.0), customer=_customer(income=500_000_000))
    )
    assert result.reason_code is ReasonCode.TECHNICAL_DECLINE


def test_rail_multiplier_orders_technical_failure_across_rails(shipped: Assumptions) -> None:
    """SPEC §1.1: UPI fails most, card least. Asserted on the configured multipliers so
    a change that inverts the rail ordering cannot pass silently."""
    upi = float(shipped.value("engine.rail_technical_multiplier.upi_autopay"))
    enach = float(shipped.value("engine.rail_technical_multiplier.enach"))
    card = float(shipped.value("engine.rail_technical_multiplier.card_emandate"))
    assert upi > enach > card


# --- SPEC §2.3 customer reaction ----------------------------------------------------


def test_revocation_risk_rises_strictly_with_failed_attempts(shipped: Assumptions) -> None:
    """The cost of retry aggression. If this is ever flat, every strategy converges on
    retrying to the compliance cap and the simulator recommends something that would
    damage a real merchant's book."""
    reaction = CustomerReaction(shipped)
    risks = [reaction.revocation_probability(n, intent=0.9) for n in range(1, 6)]
    assert risks == sorted(risks)
    assert len(set(risks)) == len(risks), f"revocation risk is flat somewhere: {risks}"
    assert reaction.revocation_probability(0, intent=0.9) == 0.0


def test_revocation_risk_compounds_rather_than_merely_accumulating(
    shipped: Assumptions,
) -> None:
    """Super-linear in attempts, so the marginal cost of one more retry grows. Under a
    linear model the optimal retry count would always be the compliance cap."""
    reaction = CustomerReaction(shipped)
    first_step = reaction.revocation_probability(2, 0.9) - reaction.revocation_probability(1, 0.9)
    later_step = reaction.revocation_probability(5, 0.9) - reaction.revocation_probability(4, 0.9)
    assert later_step > first_step


def test_low_intent_customers_are_likelier_to_revoke(shipped: Assumptions) -> None:
    reaction = CustomerReaction(shipped)
    assert reaction.revocation_probability(3, intent=0.2) > reaction.revocation_probability(
        3, intent=0.95
    )


def test_intent_decays_over_an_episode(shipped: Assumptions) -> None:
    reaction = CustomerReaction(shipped)
    path = [reaction.decayed_intent(0.9, n) for n in range(5)]
    assert path == sorted(path, reverse=True)
    assert path[0] == pytest.approx(0.9)


def test_notification_raises_the_top_up_probability(shipped: Assumptions) -> None:
    """SPEC §2.3. Nothing sets pre_debit_notified until phase 5, so this ratio is the
    lever the whole notification story will rest on."""
    reaction = CustomerReaction(shipped)
    assert reaction.topup_probability(notified=True, intent=0.9) > reaction.topup_probability(
        notified=False, intent=0.9
    )


def test_a_customer_with_no_intent_never_tops_up(shipped: Assumptions) -> None:
    reaction = CustomerReaction(shipped)
    assert reaction.topup_probability(notified=True, intent=0.0) == 0.0


def test_notification_rescues_debits_that_would_otherwise_lack_funds(
    shipped: Assumptions,
) -> None:
    """End to end: against an empty account, the only thing that can produce a success
    is the §2.3 top-up, so notified attempts must succeed more often than silent ones.

    This is the mechanism behind any lift phase 5 will attribute to notification, and
    it is measured here against the un-notified floor phase 4 otherwise runs on.
    """
    engine = FailureEngine(shipped, SEED)
    broke = _customer(income=1)

    def successes(notified: bool) -> int:
        return sum(
            engine.execute(
                _request(customer=broke, when=WHEN + timedelta(days=day), notified=notified)
            ).outcome
            is AttemptOutcome.SUCCESS
            for day in range(1, 400)
        )

    assert successes(notified=True) > successes(notified=False)


def test_retrying_an_aggrieved_mandate_eventually_induces_revocation(
    shipped: Assumptions,
) -> None:
    """End to end through the engine: hammering the same mandate produces revocations,
    flagged as induced so the harness can count them as the downside they are."""
    engine = FailureEngine(shipped, SEED)
    induced = 0
    for day in range(1, 200):
        result = engine.execute(
            _request(
                customer=_customer(income=1),
                attempt=4,
                when=WHEN + timedelta(days=day),
            )
        )
        if result.induced_revocation:
            induced += 1
            assert result.reason_code is ReasonCode.MANDATE_REVOKED
    assert induced > 0, "no revocation in 199 fourth attempts; aggression has no cost"


def test_first_attempts_never_induce_revocation(shipped: Assumptions) -> None:
    """A customer cannot be annoyed by a retry that has not happened yet."""
    engine = FailureEngine(shipped, SEED)
    for day in range(1, 120):
        result = engine.execute(
            _request(customer=_customer(income=1), attempt=1, when=WHEN + timedelta(days=day))
        )
        assert not result.induced_revocation


# --- Determinism and paired luck ----------------------------------------------------


def test_engine_is_deterministic_for_a_seed(shipped: Assumptions) -> None:
    request = _request(customer=_customer(income=60_000))
    first = FailureEngine(shipped, SEED).execute(request)
    second = FailureEngine(shipped, SEED).execute(request)
    assert first.model_dump() == second.model_dump()


def test_two_strategies_landing_on_the_same_day_face_the_same_luck(
    shipped: Assumptions,
) -> None:
    """SPEC §0.6. The draws are addressed by (mandate, cycle, date), so a retry on a
    given day gets the same outcome regardless of what else the strategy did first."""
    engine = FailureEngine(shipped, SEED)
    when = WHEN + timedelta(days=3)
    direct = engine.execute(_request(customer=_customer(income=60_000), attempt=2, when=when))
    engine.execute(_request(customer=_customer(income=60_000), attempt=2, when=WHEN))
    engine.execute(_request(customer=_customer(income=60_000), attempt=3, when=WHEN))
    after_other_work = engine.execute(
        _request(customer=_customer(income=60_000), attempt=2, when=when)
    )
    assert direct.model_dump() == after_other_work.model_dump()


def test_different_seeds_give_different_outcomes(shipped: Assumptions) -> None:
    request = _request(customer=_customer(income=60_000))
    outcomes = {
        FailureEngine(shipped, seed).execute(request).reason_code for seed in range(SEED, SEED + 40)
    }
    assert len(outcomes) > 1


def test_a_same_day_retry_does_not_inherit_the_original_attempt_s_luck(
    shipped: Assumptions,
) -> None:
    """Regression. The rolls were keyed by calendar date while the bank-availability
    threshold is hourly, so a sub-day retry drew the identical number as the attempt it
    followed and could only reproduce that outcome. SPEC §4.3's two-hour technical retry
    was therefore impossible to benefit from, and the zero would have read as a finding
    about retry timing rather than an artefact of the draw."""
    engine = FailureEngine(shipped, SEED)
    dead_bank = _bank(uptime=0.0, td_rate=0.0)
    rich = _customer(income=500_000_000)
    first = engine.execute(_request(bank=dead_bank, customer=rich, when=WHEN))
    assert first.reason_code is ReasonCode.BANK_UNAVAILABLE
    healthy = _bank(uptime=1.0, td_rate=0.0)
    later = engine.execute(
        _request(bank=healthy, customer=rich, attempt=2, when=WHEN + timedelta(hours=2))
    )
    assert later.outcome is AttemptOutcome.SUCCESS


def test_two_strategies_retrying_at_the_same_instant_still_share_luck(
    shipped: Assumptions,
) -> None:
    """The paired-comparison property survives the finer key: identical timing means
    identical draws, so a difference between strategies is still timing, not sampling."""
    engine = FailureEngine(shipped, SEED)
    when = WHEN + timedelta(days=2, hours=5)
    a = engine.execute(_request(customer=_customer(income=60_000), attempt=2, when=when))
    b = engine.execute(_request(customer=_customer(income=60_000), attempt=2, when=when))
    assert a.model_dump() == b.model_dump()
