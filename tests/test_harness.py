from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from rebound.config import Assumptions
from rebound.domain.entities import (
    AttemptOutcome,
    DebitAttempt,
    EpisodeOutcome,
    RecoveryEpisode,
)
from rebound.domain.reason_codes import ReasonCode
from rebound.harness.metrics import StrategyMetrics, combine, summarise
from rebound.harness.runner import run_experiment, run_paired

# These are harness unit tests: they test the runner, not the engine, so they pin the
# deterministic phase-2 ScriptedEngine explicitly rather than following the default.
# Integration-level runs take whatever the default engine is.
from rebound.harness.stub_engine import ScriptedEngine
from rebound.population.book import generate_book
from rebound.strategies.fixed_schedule import FixedSchedule
from rebound.strategies.no_retry import NoRetry

SEED = 20260731
T0 = datetime(2026, 3, 1, tzinfo=UTC)


def episode(*, recovered: bool, amount: int = 10_000, days: int = 3) -> RecoveryEpisode:
    original = DebitAttempt(
        id="a1",
        mandate_id="m1",
        scheduled_at=T0,
        amount_paise=amount,
        outcome=AttemptOutcome.FAILURE,
        reason_code=ReasonCode.INSUFFICIENT_FUNDS,
        attempt_number=1,
        is_retry=False,
    )
    if not recovered:
        return RecoveryEpisode(
            mandate_id="m1",
            cycle_id="c1",
            original_attempt=original,
            outcome=EpisodeOutcome.LAPSED,
            amount_recovered_paise=0,
        )
    retry = DebitAttempt(
        id="a2",
        mandate_id="m1",
        scheduled_at=T0 + timedelta(days=days),
        amount_paise=amount,
        outcome=AttemptOutcome.SUCCESS,
        attempt_number=2,
        is_retry=True,
    )
    return RecoveryEpisode(
        mandate_id="m1",
        cycle_id="c1",
        original_attempt=original,
        retry_attempts=(retry,),
        outcome=EpisodeOutcome.RECOVERED,
        days_to_recovery=days,
        amount_recovered_paise=amount,
    )


# --- metric arithmetic --------------------------------------------------------


def test_net_is_gross_less_the_performance_fee() -> None:
    metrics = summarise("S", [episode(recovered=True, amount=100_000)], fee_rate=0.15)
    assert metrics.gross_recovered_paise == 100_000
    assert metrics.performance_fee_paise == 15_000
    assert metrics.net_recovered_paise == 85_000


def test_recovery_rate_and_attempts_per_recovery() -> None:
    metrics = summarise("S", [episode(recovered=True), episode(recovered=False)], fee_rate=0.15)
    assert metrics.recovery_rate == 0.5
    assert metrics.total_attempts == 3
    assert metrics.attempts_per_recovery == 3.0


def test_empty_metrics_do_not_divide_by_zero() -> None:
    metrics = summarise("S", [], fee_rate=0.15)
    assert metrics.recovery_rate == 0.0
    assert metrics.attempts_per_recovery is None
    assert metrics.median_days_to_recovery is None


def test_percentiles_use_nearest_rank_over_real_days() -> None:
    metrics = summarise(
        "S", [episode(recovered=True, days=d) for d in (1, 2, 3, 4, 30)], fee_rate=0.0
    )
    assert metrics.median_days_to_recovery == 3
    assert metrics.p90_days_to_recovery == 30


def test_combine_sums_counters_and_merges_day_samples() -> None:
    part = summarise("S", [episode(recovered=True, days=2)], fee_rate=0.15)
    total = combine("S", [part, part])
    assert total.episodes == 2
    assert total.gross_recovered_paise == 2 * part.gross_recovered_paise
    assert total.days_to_recovery == (2, 2)


def test_metrics_carry_no_floats() -> None:
    """The output hash is exact only because the stored state is integral."""
    dumped = summarise("S", [episode(recovered=True)], fee_rate=0.15).model_dump()
    for key, value in dumped.items():
        assert not isinstance(value, float), f"{key} is a float"


# --- the run ------------------------------------------------------------------


def test_no_retry_recovers_nothing(small: Assumptions) -> None:
    report = run_paired(
        generate_book(small, SEED), [NoRetry()], SEED, small, engine_factory=ScriptedEngine
    )
    metrics = report.for_strategy("NoRetry")
    assert metrics.episodes > 0
    assert metrics.recovered_episodes == 0
    assert metrics.retry_attempts == 0


def test_fixed_schedule_retries_land_on_t1_t3_t7_or_the_next_legal_slot(
    small: Assumptions,
) -> None:
    """The baseline proposes T+1, T+3, T+7 and nothing else.

    A recovery may still land later than the offset it was proposed for: an eNACH retry
    falling on a weekend or past the batch cutoff is moved to the next clearing slot by
    the guard rather than dropped. So the assertion is that every recovery day is one of
    the offsets or a legal slot within the reschedule horizon of one — never earlier than
    an offset, and never beyond what the horizon permits.
    """
    horizon = int(small.value("compliance.reschedule_horizon_days"))
    offsets = list(small.value("strategy.fixed_schedule.retry_offsets_days"))
    report = run_paired(
        generate_book(small, SEED),
        [FixedSchedule(small)],
        SEED,
        small,
        engine_factory=ScriptedEngine,
    )
    metrics = report.for_strategy("FixedSchedule")
    assert metrics.retry_attempts > 0
    assert metrics.recovered_episodes > 0
    allowed = {offset + slip for offset in offsets for slip in range(horizon + 1)}
    assert set(metrics.days_to_recovery) <= allowed
    assert min(metrics.days_to_recovery) >= min(offsets)


def test_fixed_schedule_beats_no_retry_on_the_same_population(small: Assumptions) -> None:
    report = run_paired(
        generate_book(small, SEED),
        [NoRetry(), FixedSchedule(small)],
        SEED,
        small,
        engine_factory=ScriptedEngine,
    )
    assert report.for_strategy("NoRetry").episodes == report.for_strategy("FixedSchedule").episodes
    assert (
        report.for_strategy("NoRetry").recovery_rate
        < report.for_strategy("FixedSchedule").recovery_rate
    )


def test_no_attempt_follows_a_hard_decline(small: Assumptions) -> None:
    """The domain model refuses to build such an episode, so a violation would surface
    as a ValidationError during the run rather than as a bad number at the end."""
    report = run_paired(
        generate_book(small, SEED),
        [FixedSchedule(small)],
        SEED,
        small,
        engine_factory=ScriptedEngine,
    )
    assert report.for_strategy("FixedSchedule").episodes > 0


def test_retry_aggression_induces_revocations(small: Assumptions) -> None:
    """SPEC §2.3: if this is zero, the harness cannot see the downside of retrying."""
    report = run_paired(
        generate_book(small, SEED),
        [NoRetry(), FixedSchedule(small)],
        SEED,
        small,
        engine_factory=ScriptedEngine,
    )
    assert report.for_strategy("NoRetry").induced_revocations == 0
    assert report.for_strategy("FixedSchedule").induced_revocations > 0


def test_attempt_cap_is_respected(small: Assumptions) -> None:
    capped = small.model_copy(
        update={
            "assumptions": {
                **small.assumptions,
                "harness.max_attempts_per_cycle": small.assumptions[
                    "harness.max_attempts_per_cycle"
                ].model_copy(update={"value": 2}),
            }
        }
    )
    report = run_paired(
        generate_book(capped, SEED),
        [FixedSchedule(capped)],
        SEED,
        capped,
        engine_factory=ScriptedEngine,
    )
    metrics = report.for_strategy("FixedSchedule")
    assert metrics.retry_attempts <= metrics.episodes


def test_unknown_strategy_lookup_names_itself(small: Assumptions) -> None:
    report = run_paired(
        generate_book(small, SEED), [NoRetry()], SEED, small, engine_factory=ScriptedEngine
    )
    with pytest.raises(KeyError, match="Nope"):
        report.for_strategy("Nope")


# --- intervals ----------------------------------------------------------------


def test_experiment_reports_an_interval_for_every_metric_and_strategy(
    small: Assumptions,
) -> None:
    report = run_experiment(
        [NoRetry(), FixedSchedule(small)], SEED, small, n_seeds=4, engine_factory=ScriptedEngine
    )
    assert len(report.seeds) == 4
    for name in report.strategies:
        metrics = {i.metric for i in report.intervals_for(name) if i.vs_baseline is None}
        assert "recovery_rate" in metrics
        assert "net_recovered_paise" in metrics


def test_intervals_bracket_their_point_estimate(small: Assumptions) -> None:
    report = run_experiment(
        [NoRetry(), FixedSchedule(small)], SEED, small, n_seeds=4, engine_factory=ScriptedEngine
    )
    assert report.intervals
    for interval in report.intervals:
        assert interval.low <= interval.point <= interval.high


def test_paired_lift_is_reported_against_the_baseline(small: Assumptions) -> None:
    report = run_experiment(
        [NoRetry(), FixedSchedule(small)], SEED, small, n_seeds=4, engine_factory=ScriptedEngine
    )
    lift = [
        i
        for i in report.intervals_for("NoRetry")
        if i.vs_baseline == "FixedSchedule" and i.metric == "recovery_rate"
    ]
    assert len(lift) == 1
    assert lift[0].point < 0  # NoRetry is worse than the baseline, by construction


def test_single_seed_run_carries_no_intervals(small: Assumptions) -> None:
    """One seed cannot support a bootstrap over seeds. Better to report nothing than to
    report an interval of width zero and have someone quote it."""
    report = run_paired(
        generate_book(small, SEED), [NoRetry()], SEED, small, engine_factory=ScriptedEngine
    )
    assert report.intervals == ()


def test_strategy_metrics_are_frozen() -> None:
    metrics = summarise("S", [episode(recovered=True)], fee_rate=0.15)
    with pytest.raises(Exception):  # noqa: B017
        metrics.episodes = 5  # type: ignore[misc]
    assert isinstance(metrics, StrategyMetrics)
