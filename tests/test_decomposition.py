"""The headline is two line items, not one.

Phase 7 measured rescheduling lift at several times strategy lift. Reporting the total
would sell scheduler plumbing as retry intelligence, so the split lives in the report
rather than in whichever script happened to produce a table.
"""

from __future__ import annotations

import pytest

from rebound.config import Assumptions
from rebound.harness.bootstrap import Interval
from rebound.harness.runner import default_strategies, run_experiment
from rebound.harness.stub_engine import ScriptedEngine

METRIC = "recovery_rate"


def _interval(point: float, low: float, high: float, vs: str | None = "FixedSchedule") -> Interval:
    return Interval(
        strategy="X", metric=METRIC, point=point, low=low, high=high, level=0.9, vs_baseline=vs
    )


class TestNegation:
    def test_it_turns_the_comparison_round(self) -> None:
        flipped = _interval(-0.068, -0.079, -0.055).negated("FixedSchedule", "NoReschedule")
        assert flipped.point == pytest.approx(0.068)
        assert (flipped.low, flipped.high) == pytest.approx((0.055, 0.079))
        assert flipped.strategy == "FixedSchedule"
        assert flipped.vs_baseline == "NoReschedule"

    def test_negating_twice_is_the_identity(self) -> None:
        """It is a relabelling of one bootstrap distribution, not a second estimate."""
        first = _interval(-0.068, -0.079, -0.055)
        back = first.negated("a", "b").negated("X", "FixedSchedule")
        assert (back.point, back.low, back.high) == pytest.approx(
            (first.point, first.low, first.high)
        )

    def test_a_significant_interval_stays_significant_when_turned_round(self) -> None:
        flipped = _interval(-0.068, -0.079, -0.055).negated("a", "b")
        assert flipped.low > 0.0


class TestDefaultSet:
    def test_it_contains_all_four_references(self, shipped: Assumptions) -> None:
        names = [s.name for s in default_strategies(shipped)]
        for reference in ("NoRetry", "NoReschedule", "FixedSchedule"):
            assert reference in names

    def test_the_scheduler_reference_and_headline_are_both_present(
        self, shipped: Assumptions
    ) -> None:
        """Without both, `decomposition` returns None and the split silently disappears."""
        names = {s.name for s in default_strategies(shipped)}
        assert str(shipped.value("harness.scheduler_reference_strategy")) in names
        assert str(shipped.value("harness.headline_strategy")) in names
        assert str(shipped.value("harness.baseline_strategy")) in names


class TestDecomposition:
    @pytest.fixture(scope="class")
    @classmethod
    def report(cls, small: Assumptions):
        return run_experiment(
            default_strategies(small), 0, small, n_seeds=4, engine_factory=ScriptedEngine
        )

    def test_both_line_items_are_reported(self, report, small: Assumptions) -> None:
        d = report.decomposition(METRIC, small)
        assert d is not None
        assert d.rescheduling.vs_baseline == "NoReschedule"
        assert d.strategy.vs_baseline == "FixedSchedule"

    def test_rescheduling_lift_is_the_baseline_gaining_on_the_scheduler_reference(
        self, report, small: Assumptions
    ) -> None:
        d = report.decomposition(METRIC, small)
        stored = report.interval("NoReschedule", METRIC)
        assert d.rescheduling.point == pytest.approx(-stored.point)

    def test_the_two_items_are_not_summed(self, report, small: Assumptions) -> None:
        """They are separate deliberately. A total would read as strategy lift."""
        d = report.decomposition(METRIC, small)
        assert not hasattr(d, "total")

    def test_it_returns_none_when_the_scheduler_reference_was_not_run(
        self, small: Assumptions
    ) -> None:
        """None rather than a partial answer: a decomposition missing its scheduler
        reference is indistinguishable from one where rescheduling is worth nothing."""
        from rebound.strategies.blended import Blended
        from rebound.strategies.fixed_schedule import FixedSchedule

        partial = run_experiment(
            [FixedSchedule(small), Blended(small)], 0, small, n_seeds=3,
            engine_factory=ScriptedEngine,
        )
        assert partial.decomposition(METRIC, small) is None

    def test_a_single_seed_run_has_no_decomposition(self, small: Assumptions) -> None:
        """One seed carries no intervals, and SPEC forbids a point estimate without one."""
        single = run_experiment(
            default_strategies(small), 0, small, n_seeds=1, engine_factory=ScriptedEngine
        )
        assert single.decomposition(METRIC, small) is None
