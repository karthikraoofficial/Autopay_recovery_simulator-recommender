"""SPEC §5.2 guardrail tests.

These are written before the harness and the strategies exist and are expected to
FAIL until phase 2 (harness) and phase 6 (strategies) land. Do not stub anything to
make them pass — a green guardrail against a stub is worse than a red one.

Imports of not-yet-existing modules are done inside each test body on purpose, so a
missing module fails that one test rather than erroring collection for the whole file.

The names imported below are the API commitment for phase 2:

    rebound.population.book   -> generate_book(assumptions, seed) -> Book
    rebound.harness.runner    -> run_paired(book, strategies, seed) -> HarnessReport
    HarnessReport             -> .output_hash, .for_strategy(name) -> StrategyMetrics
    StrategyMetrics           -> .recovery_rate, .gross_recovered_paise,
                                 .total_failed_paise
    rebound.compliance.errors -> HardDeclineViolation
    rebound.strategies.base   -> RetryStrategy, ProposedRetry, CustomerObservable
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from rebound.config import load_assumptions
from rebound.domain import AttemptOutcome, DebitAttempt, Mandate, ReasonCode

SEED = 20260731


def _assumptions():
    return load_assumptions()


def test_retry_after_hard_decline_is_rejected_by_the_harness() -> None:
    """SPEC §1.3/§5.2: a strategy proposing a retry after a hard decline must not run."""
    from rebound.compliance.errors import HardDeclineViolation
    from rebound.compliance.guard import ComplianceGuard, RetryContext
    from rebound.population.book import generate_book
    from rebound.strategies.base import ProposedRetry

    class RetriesAnything:
        name = "retries_anything"

        def propose_retries(
            self,
            failed_attempt: DebitAttempt,
            mandate: Mandate,
            customer_view: object,
            history: list[DebitAttempt],
            clock: datetime,
        ) -> list[ProposedRetry]:
            return [
                ProposedRetry(
                    mandate_id=mandate.id,
                    scheduled_at=clock + timedelta(days=1),
                    amount_paise=failed_attempt.amount_paise,
                )
            ]

    # Driven through the guard rather than through run_paired: the runner terminates the
    # episode on a hard decline as a matter of lifecycle, so it never asks a strategy to
    # propose against a dead mandate. The guard is the defence-in-depth layer that
    # catches such a proposal if the runner's check ever regresses, so that is where a
    # strategy which retries anything has to be pointed.
    book = generate_book(_assumptions(), seed=SEED)
    mandate = book.mandates[0]
    customer = {c.id: c for c in book.customers}[mandate.customer_id]
    bank = {b.id: b for b in book.banks}[customer.bank_id]
    declined = DebitAttempt(
        id="a1",
        mandate_id=mandate.id,
        scheduled_at=book.start_at,
        executed_at=book.start_at,
        amount_paise=49900,
        outcome=AttemptOutcome.FAILURE,
        reason_code=ReasonCode.MANDATE_EXPIRED,
        attempt_number=1,
        is_retry=False,
    )
    proposals = RetriesAnything().propose_retries(
        failed_attempt=declined,
        mandate=mandate,
        customer_view=object(),
        history=[declined],
        clock=book.start_at,
    )
    context = RetryContext(
        mandate=mandate,
        bank=bank,
        original_attempt=declined,
        history=(declined,),
        notified_at=book.start_at - timedelta(hours=24),
    )
    with pytest.raises(HardDeclineViolation):
        ComplianceGuard(_assumptions()).review(proposals[0], context)


def test_strategy_cannot_read_true_balance_or_true_salary_day() -> None:
    """SPEC §4: CustomerObservable exposes only what a real merchant could know."""
    from rebound.harness.runner import run_paired
    from rebound.population.book import generate_book
    from rebound.strategies.base import CustomerObservable, ProposedRetry

    forbidden = ("balance_paise", "balance", "balance_process", "salary_credit_day")
    for field in forbidden:
        assert field not in CustomerObservable.model_fields, (
            f"CustomerObservable leaks ground truth via {field!r}"
        )

    class ReadsGroundTruth:
        name = "reads_ground_truth"

        def propose_retries(
            self,
            failed_attempt: DebitAttempt,
            mandate: Mandate,
            customer_view: CustomerObservable,
            history: list[DebitAttempt],
            clock: datetime,
        ) -> list[ProposedRetry]:
            _ = customer_view.balance_paise  # type: ignore[attr-defined]
            return []

    book = generate_book(_assumptions(), seed=SEED)
    with pytest.raises(AttributeError):
        run_paired(book, [ReadsGroundTruth()], seed=SEED)


def test_same_seed_gives_an_identical_output_hash() -> None:
    """SPEC §0.5: same seed + same config = byte-identical results."""
    from rebound.harness.runner import run_paired
    from rebound.population.book import generate_book
    from rebound.strategies.fixed_schedule import FixedSchedule

    assumptions = _assumptions()
    first = run_paired(generate_book(assumptions, seed=SEED), [FixedSchedule()], seed=SEED)
    second = run_paired(generate_book(assumptions, seed=SEED), [FixedSchedule()], seed=SEED)
    assert first.output_hash == second.output_hash


def test_recovery_rate_ordering_no_retry_le_fixed() -> None:
    """Sanity ordering. If this inverts, the engine or the harness is wrong: retrying
    cannot recover less than never retrying at all."""
    from rebound.harness.runner import run_paired
    from rebound.population.book import generate_book
    from rebound.strategies.fixed_schedule import FixedSchedule
    from rebound.strategies.no_retry import NoRetry

    book = generate_book(_assumptions(), seed=SEED)
    report = run_paired(book, [NoRetry(), FixedSchedule()], seed=SEED)
    assert report.for_strategy("NoRetry").recovery_rate <= (
        report.for_strategy("FixedSchedule").recovery_rate
    )


def test_recovery_rate_ordering_fixed_le_blended() -> None:
    """SPEC §5.2 sanity ordering.

    This briefly appeared to be false. It was not: `ComplianceGuard.filter` returned None
    when a proposal was blocked and the harness ended the retry chain, so a single
    presentation-window block killed the whole cycle. Since every strategy proposes one
    candidate, that turned a timing rule into a termination rule and penalised exactly
    the strategies that schedule outside the eNACH batch window. With
    `next_valid_slot` rescheduling blocked proposals the ordering holds. Kept as a hard
    assert, not an xfail: the guardrail was right and the measurement was wrong.
    """
    from rebound.harness.runner import run_paired
    from rebound.population.book import generate_book
    from rebound.strategies.blended import Blended
    from rebound.strategies.fixed_schedule import FixedSchedule

    book = generate_book(_assumptions(), seed=SEED)
    report = run_paired(book, [FixedSchedule(), Blended()], seed=SEED)
    assert report.for_strategy("FixedSchedule").recovery_rate <= (
        report.for_strategy("Blended").recovery_rate
    )


def test_total_recovered_never_exceeds_total_failed() -> None:
    """No money is created. Asserted over every strategy in the report, so the check
    keeps applying to strategies that do not exist yet — but only after confirming the
    report actually contains the strategies that were asked for, since a loop over an
    empty or silently-truncated report passes vacuously and proves nothing."""
    from rebound.harness.runner import run_paired
    from rebound.population.book import generate_book
    from rebound.strategies.fixed_schedule import FixedSchedule
    from rebound.strategies.no_retry import NoRetry

    requested = [NoRetry(), FixedSchedule()]
    book = generate_book(_assumptions(), seed=SEED)
    report = run_paired(book, requested, seed=SEED)

    assert report.strategies, "report contains no strategies at all"
    assert set(report.strategies) == {s.name for s in requested}
    assert len(report.strategies) == len(requested)

    for name in report.strategies:
        metrics = report.for_strategy(name)
        assert metrics.episodes > 0, f"{name} produced no episodes to measure"
        assert metrics.gross_recovered_paise <= metrics.total_failed_paise
