"""Opening-debit volumes: how many debits were attempted, how many failed.

The question a merchant asks before "what is recovery worth": *how many of my debits fail?*
The lift figures could not answer it, because the counters that produce them are about
episodes and rupees rather than about the denominator underneath.

Two properties this file exists to hold:

1. **Cut from the same run as the headline, never re-simulated.** Volumes arrive through
   `RunObserver`, the seam SPEC §12.3 established for exactly this reason: a second
   simulation could silently disagree with the numbers it is supposed to be context for.
2. **The numerator reconciles to the hashed counters.** Observed failures must equal
   `StrategyMetrics.episodes`; if the two disagree, one of them is wrong.

   The *denominator* has no counterpart to check against, and that is the finding that
   made this work necessary: `total_attempts` sums the attempts inside episodes, and an
   episode exists only for a failed opening, so `total_attempts - retry_attempts` is
   identically `episodes`. A successful opening debit was recorded nowhere. The question
   "how many of my debits fail" was unanswerable from the run output because the run
   output had never counted the debits.
"""

from __future__ import annotations

import pytest

from rebound.config import Assumptions
from rebound.domain.entities import Rail
from rebound.harness.runner import default_strategies, run_experiment
from rebound.harness.volumes import ALL_RAILS, VolumeCollector, volume_summary

SEEDS = 3
MASTER_SEED = 20260801


@pytest.fixture(scope="module")
def tiny(shipped: Assumptions) -> Assumptions:
    """Small enough to run a paired experiment a few times in a suite."""
    return shipped.with_values(
        {"book.size": 60, "book.months": 4, "population.bank.count": 4}
    )


@pytest.fixture(scope="module")
def observed(tiny: Assumptions):  # type: ignore[no-untyped-def]
    collector = VolumeCollector(str(tiny.value("harness.scheduler_reference_strategy")))
    report = run_experiment(
        default_strategies(tiny), MASTER_SEED, tiny, n_seeds=SEEDS, observer=collector
    )
    return report, collector


def test_observed_failures_reconcile_to_the_hashed_counters(observed, tiny) -> None:  # type: ignore[no-untyped-def]
    """Per seed, not only in total: two errors of opposite sign would cancel in a total."""
    report, collector = observed
    strategy = str(tiny.value("harness.scheduler_reference_strategy"))
    for seed_result in report.per_seed:
        metrics = seed_result.for_strategy(strategy)
        _, failed = collector.totals(seed_result.seed)
        assert failed == metrics.episodes


def test_the_denominator_is_new_information_not_a_restatement(observed, tiny) -> None:  # type: ignore[no-untyped-def]
    """`total_attempts - retry_attempts` is identically `episodes`, because attempts are
    only counted inside episodes and an episode is a failed opening. So the existing
    counters could report the failures and never the debits."""
    report, collector = observed
    strategy = str(tiny.value("harness.scheduler_reference_strategy"))
    for seed_result in report.per_seed:
        metrics = seed_result.for_strategy(strategy)
        assert metrics.total_attempts - metrics.retry_attempts == metrics.episodes
        attempted, failed = collector.totals(seed_result.seed)
        assert attempted > failed, "a book where every opening failed would prove nothing"
        # One opening per debitable cycle, so the ceiling is the whole book every month.
        assert attempted <= int(tiny.value("book.size")) * int(tiny.value("book.months"))


def test_the_rails_sum_to_the_overall_figure(observed) -> None:  # type: ignore[no-untyped-def]
    _, collector = observed
    for seed in collector.seeds():
        attempted, failed = collector.totals(seed)
        by_rail = [collector.for_rail(seed, rail) for rail in Rail]
        assert sum(a for a, _ in by_rail) == attempted
        assert sum(f for _, f in by_rail) == failed


def test_a_failure_can_never_exceed_an_attempt(observed) -> None:  # type: ignore[no-untyped-def]
    _, collector = observed
    for seed in collector.seeds():
        for rail in Rail:
            attempted, failed = collector.for_rail(seed, rail)
            assert 0 <= failed <= attempted


def test_every_figure_carries_an_interval_that_brackets_its_point(observed, tiny) -> None:  # type: ignore[no-untyped-def]
    """CLAUDE.md: never a point estimate without an interval, and that applies to every
    reported metric rather than only to lift."""
    _, collector = observed
    summary = volume_summary(collector, MASTER_SEED, tiny)
    assert summary.lines
    for line in summary.lines:
        for interval in (line.attempted, line.failed, line.failure_rate):
            assert interval.low <= interval.point <= interval.high
            assert 0.0 < interval.level < 1.0


def test_the_overall_line_is_reported_and_is_named_for_every_rail(observed, tiny) -> None:  # type: ignore[no-untyped-def]
    _, collector = observed
    summary = volume_summary(collector, MASTER_SEED, tiny)
    labels = [line.rail for line in summary.lines]
    assert labels[-1] == ALL_RAILS, "the overall line reads last, under the rails"
    assert set(labels) - {ALL_RAILS} <= {rail.value for rail in Rail}


def test_the_failure_rate_is_a_pooled_ratio_not_a_mean_of_ratios(observed, tiny) -> None:  # type: ignore[no-untyped-def]
    """A seed with few debits must not count as much as a seed with many. The point
    estimate is total failures over total attempts, which is the rate the merchant would
    compute from their own book."""
    _, collector = observed
    summary = volume_summary(collector, MASTER_SEED, tiny)
    overall = summary.lines[-1]
    attempted = sum(collector.totals(seed)[0] for seed in collector.seeds())
    failed = sum(collector.totals(seed)[1] for seed in collector.seeds())
    assert overall.failure_rate.point == pytest.approx(failed / attempted)


def test_a_rail_with_no_debits_is_absent_rather_than_reported_as_zero(tiny: Assumptions) -> None:
    """A failure rate over zero debits is undefined, not 0%. Rendering it as zero would
    put a reassuring number against a rail the merchant does not use."""
    upi_only = tiny.with_values(
        {
            "population.mandate.rail_mix.upi_autopay": 1.0,
            "population.mandate.rail_mix.enach": 0.0,
            "population.mandate.rail_mix.card_emandate": 0.0,
        }
    )
    collector = VolumeCollector(str(upi_only.value("harness.scheduler_reference_strategy")))
    run_experiment(
        default_strategies(upi_only), MASTER_SEED, upi_only, n_seeds=2, observer=collector
    )
    summary = volume_summary(collector, MASTER_SEED, upi_only)
    labels = [line.rail for line in summary.lines]
    assert Rail.ENACH.value not in labels
    assert Rail.UPI_AUTOPAY.value in labels


def test_observing_does_not_change_the_output_hash(tiny: Assumptions) -> None:
    """The observer contract (SPEC §12.3): it receives finished objects and returns
    nothing, so a watched run and a silent one are the same run."""
    collector = VolumeCollector(str(tiny.value("harness.scheduler_reference_strategy")))
    watched = run_experiment(
        default_strategies(tiny), MASTER_SEED, tiny, n_seeds=2, observer=collector
    )
    silent = run_experiment(default_strategies(tiny), MASTER_SEED, tiny, n_seeds=2)
    assert watched.output_hash == silent.output_hash


def test_the_summary_names_the_run_it_was_measured_on(observed, tiny) -> None:  # type: ignore[no-untyped-def]
    """Opening debits are not quite strategy-invariant: retry aggression induces
    revocations, and a revoked mandate has no further opening debits. So the figure has to
    say which run produced it rather than reading as a property of the book."""
    _, collector = observed
    summary = volume_summary(collector, MASTER_SEED, tiny)
    assert summary.strategy == str(tiny.value("harness.scheduler_reference_strategy"))
