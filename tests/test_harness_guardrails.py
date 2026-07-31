"""SPEC §5.2 guardrail tests.

These are written before the harness and the strategies exist and are expected to
FAIL until phase 2 (harness) and phase 6 (strategies) land. Do not stub anything to
make them pass — a green guardrail against a stub is worse than a red one.

Imports of not-yet-existing modules are done inside each test body on purpose, so a
missing module fails that one test rather than erroring collection for the whole file.

The names imported below are the API commitment for phase 2:

    rebound.harness.book      -> generate_book(assumptions, seed) -> Book
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
from rebound.domain import DebitAttempt, Mandate

SEED = 20260731


def _assumptions():
    return load_assumptions()


def test_retry_after_hard_decline_is_rejected_by_the_harness() -> None:
    """SPEC §1.3/§5.2: a strategy proposing a retry after a hard decline must not run."""
    from rebound.compliance.errors import HardDeclineViolation
    from rebound.harness.book import generate_book
    from rebound.harness.runner import run_paired
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

    book = generate_book(_assumptions(), seed=SEED)
    with pytest.raises(HardDeclineViolation):
        run_paired(book, [RetriesAnything()], seed=SEED)


def test_strategy_cannot_read_true_balance_or_true_salary_day() -> None:
    """SPEC §4: CustomerObservable exposes only what a real merchant could know."""
    from rebound.harness.book import generate_book
    from rebound.harness.runner import run_paired
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
    from rebound.harness.book import generate_book
    from rebound.harness.runner import run_paired
    from rebound.strategies.fixed_schedule import FixedSchedule

    assumptions = _assumptions()
    first = run_paired(generate_book(assumptions, seed=SEED), [FixedSchedule()], seed=SEED)
    second = run_paired(generate_book(assumptions, seed=SEED), [FixedSchedule()], seed=SEED)
    assert first.output_hash == second.output_hash


def test_recovery_rate_ordering_no_retry_le_fixed_le_blended() -> None:
    """Sanity ordering. If this inverts, the engine or the harness is wrong, not the strategy."""
    from rebound.harness.book import generate_book
    from rebound.harness.runner import run_paired
    from rebound.strategies.blended import Blended
    from rebound.strategies.fixed_schedule import FixedSchedule
    from rebound.strategies.no_retry import NoRetry

    book = generate_book(_assumptions(), seed=SEED)
    report = run_paired(book, [NoRetry(), FixedSchedule(), Blended()], seed=SEED)
    no_retry = report.for_strategy("NoRetry").recovery_rate
    fixed = report.for_strategy("FixedSchedule").recovery_rate
    blended = report.for_strategy("Blended").recovery_rate
    assert no_retry <= fixed <= blended


def test_total_recovered_never_exceeds_total_failed() -> None:
    """No money is created. Asserted over every strategy in the report, so the check
    keeps applying to strategies that do not exist yet — but only after confirming the
    report actually contains the strategies that were asked for, since a loop over an
    empty or silently-truncated report passes vacuously and proves nothing."""
    from rebound.harness.book import generate_book
    from rebound.harness.runner import run_paired
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
