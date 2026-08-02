"""SPEC §5.1: sweep each assumption ±50% and report which ones the conclusion is
fragile to. This is the section that survives a CFO, so the sweep's own arithmetic is
tested rather than trusted."""

from __future__ import annotations

import pytest

from rebound.config import Assumptions
from rebound.harness.sensitivity import (
    Sweep,
    SweepPoint,
    SweepResult,
    UnderpoweredSweepError,
    fragile_first,
    run_sweep,
    scaled,
    sweepable_keys,
)
from rebound.strategies.blended import Blended
from rebound.strategies.fixed_schedule import FixedSchedule


def _point(lift: float, low: float, high: float, factor: float = 1.0) -> SweepPoint:
    return SweepPoint(
        key="k", factor=factor, lift_point=lift, lift_low=low, lift_high=high, metric="m"
    )


def _result(base: SweepPoint, low: SweepPoint, high: SweepPoint) -> SweepResult:
    return SweepResult(key="k", base=base, low=low, high=high)


class TestKeySelection:
    def test_measurement_settings_are_not_swept(self, shipped: Assumptions) -> None:
        """Sweeping the seed count or the book size changes the experiment, not the
        world. A 'result' from that would be a statement about our runtime budget."""
        keys = sweepable_keys(shipped)
        for excluded in ("book.size", "book.months", "harness.bootstrap_seeds",
                         "harness.sensitivity.seeds", "harness.confidence_level"):
            assert excluded not in keys

    def test_modelling_assumptions_are_swept(self, shipped: Assumptions) -> None:
        keys = sweepable_keys(shipped)
        for included in (
            "population.balance.cushion_lognormal_sigma",
            "engine.reaction.revocation_base_hazard",
            "strategy.blended.weight_salary",
            "compliance.max_retries_per_cycle.enach",
        ):
            assert included in keys

    def test_booleans_and_rules_are_not_swept(self, shipped: Assumptions) -> None:
        """±50% of a boolean is meaningless. The binary flags get both settings side by
        side instead, which SPEC §11 requires permanently for the notice question."""
        keys = sweepable_keys(shipped)
        assert "compliance.pre_debit_notification.retry_inherits_original_notice" not in keys
        assert "compliance.hard_decline_stop.rule" not in keys
        assert "strategy.fixed_schedule.retry_offsets_days" not in keys


class TestScaling:
    def test_it_scales_the_named_key_and_nothing_else(self, shipped: Assumptions) -> None:
        key = "engine.reaction.revocation_base_hazard"
        swept, _ = scaled(shipped, key, 1.5)
        assert swept.value(key) == pytest.approx(float(shipped.value(key)) * 1.5)
        for other in sweepable_keys(shipped):
            if other != key:
                assert swept.value(other) == shipped.value(other)

    def test_integer_assumptions_stay_integers(self, shipped: Assumptions) -> None:
        swept, _ = scaled(shipped, "compliance.max_retries_per_cycle.enach", 1.5)
        assert isinstance(swept.value("compliance.max_retries_per_cycle.enach"), int)

    def test_a_count_never_scales_to_zero(self, shipped: Assumptions) -> None:
        """Rounding a small count down to zero would not be a ±50% sweep, it would be
        deleting the mechanism."""
        swept, _ = scaled(shipped, "population.balance.salary_jitter_max_days", 0.5)
        assert swept.value("population.balance.salary_jitter_max_days") >= 1

    def test_a_probability_is_clamped_and_the_clamp_is_recorded(
        self, shipped: Assumptions
    ) -> None:
        """0.995 x 1.5 is not a probability. Clamping silently would understate
        sensitivity without saying so, so the flag travels with the result."""
        swept, clamped = scaled(shipped, "population.bank.uptime_daytime", 1.5)
        assert clamped
        assert swept.value("population.bank.uptime_daytime") == shipped.value(
            "harness.sensitivity.probability_ceiling"
        )

    def test_a_non_probability_ratio_is_not_clamped(self, shipped: Assumptions) -> None:
        key = "population.balance.first_week_spend_multiplier"
        swept, clamped = scaled(shipped, key, 1.5)
        assert not clamped
        assert swept.value(key) > 1.0


class TestConstrainedKeys:
    def test_a_mix_member_sweep_keeps_the_group_summing_to_one(
        self, shipped: Assumptions
    ) -> None:
        """Scaling one share alone is not a configuration that exists. The rest absorb
        the change, so the swept share of the whole moves by the sweep factor and the
        book still generates."""
        group = (
            "population.mandate.rail_mix.upi_autopay",
            "population.mandate.rail_mix.enach",
            "population.mandate.rail_mix.card_emandate",
        )
        swept, _ = scaled(shipped, group[0], 0.5)
        assert swept.value(group[0]) == pytest.approx(float(shipped.value(group[0])) * 0.5)
        assert sum(float(swept.value(k)) for k in group) == pytest.approx(1.0)

    def test_the_others_keep_their_relative_proportions(self, shipped: Assumptions) -> None:
        a, b = "population.mandate.rail_mix.enach", "population.mandate.rail_mix.card_emandate"
        before = float(shipped.value(a)) / float(shipped.value(b))
        swept, _ = scaled(shipped, "population.mandate.rail_mix.upi_autopay", 0.5)
        assert float(swept.value(a)) / float(swept.value(b)) == pytest.approx(before)

    def test_a_calendar_value_is_clamped_to_its_domain(self, shipped: Assumptions) -> None:
        """17:00 x 1.5 is not an hour of the day."""
        swept, clamped = scaled(shipped, "population.bank.batch_cutoff_hour", 1.5)
        assert clamped
        assert 0 <= swept.value("population.bank.batch_cutoff_hour") <= 23

    def test_a_calendar_value_may_scale_to_zero(self, shipped: Assumptions) -> None:
        """Midnight is an hour; a count of zero is a deleted mechanism. The floor differs
        for that reason."""
        swept, _ = scaled(shipped, "population.bank.uptime_trough_hour", 0.5)
        assert swept.value("population.bank.uptime_trough_hour") == 1

    def test_an_unrepresentable_sweep_raises_rather_than_reporting_robustness(
        self, shipped: Assumptions
    ) -> None:
        """Returning the unswept config would report the key as robust when nothing was
        tested — the most expensive kind of silent pass in a sensitivity analysis."""
        key = "population.customer.salary_day.month_end_min"
        assert key not in sweepable_keys(shipped)
        with pytest.raises(ValueError, match="cannot be swept"):
            scaled(shipped, key, 1.5)


class TestFragility:
    def test_a_sign_flip_is_fragile(self) -> None:
        base = _point(100.0, 50.0, 150.0)
        assert _result(base, _point(-20.0, -60.0, 10.0), base).is_fragile

    def test_losing_significance_is_fragile(self) -> None:
        """The lift stops being distinguishable from zero. Still positive, no longer a
        claim anyone can make."""
        base = _point(100.0, 50.0, 150.0)
        result = _result(base, _point(40.0, -10.0, 90.0), base)
        assert result.loses_significance
        assert result.is_fragile

    def test_a_conclusion_already_insignificant_cannot_lose_significance(self) -> None:
        base = _point(10.0, -50.0, 70.0)
        assert not _result(base, _point(5.0, -60.0, 70.0), base).loses_significance

    def test_a_large_but_same_signed_significant_move_is_not_fragile(self) -> None:
        """A number that halves but keeps its sign and its interval has not changed the
        answer. Reporting it as fragile would bury the keys that did."""
        base = _point(100.0, 50.0, 150.0)
        result = _result(base, _point(50.0, 20.0, 80.0), _point(200.0, 150.0, 250.0))
        assert not result.is_fragile
        assert result.max_relative_swing == pytest.approx(1.0)

    def test_fragile_keys_are_ranked_first(self) -> None:
        base = _point(100.0, 50.0, 150.0)
        steady = SweepResult(
            key="steady", base=base, low=_point(90.0, 60.0, 120.0), high=_point(110.0, 80.0, 140.0)
        )
        flips = SweepResult(
            key="flips", base=base, low=_point(-5.0, -40.0, 30.0), high=base
        )
        assert [r.key for r in fragile_first([steady, flips])] == ["flips", "steady"]


def test_the_sweep_runs_on_a_smaller_book_than_the_headline(shipped: Assumptions) -> None:
    """Stated in code because the intervals it produces are wider than the headline's and
    the two must not be quoted interchangeably."""
    sweep = Sweep.sized_for_runtime(shipped, lambda a: [FixedSchedule(a), Blended(a)], "Blended")
    assert sweep.assumptions.value("book.size") == shipped.value("harness.sensitivity.book_size")
    assert sweep.assumptions.value("book.months") == shipped.value("harness.sensitivity.months")
    assert sweep.assumptions.value("book.size") < shipped.value("book.size")


def test_a_swept_strategy_constant_actually_reaches_the_strategy(
    shipped: Assumptions,
) -> None:
    """Strategies read their constants at construction. A sweep that built its strategies
    once, before scaling, would measure nothing and report every strategy key as robust —
    which would look like a clean result."""
    key = "strategy.reason_aware.insufficient_funds_delay_days"
    swept, _ = scaled(shipped, key, 1.5)
    assert Blended(swept)._reason.delay_for  # the accessor Blended scores against
    from rebound.domain.reason_codes import ReasonCode
    from rebound.strategies.reason_aware import ReasonAware

    base_delay = ReasonAware(shipped).delay_for(ReasonCode.INSUFFICIENT_FUNDS)
    swept_delay = ReasonAware(swept).delay_for(ReasonCode.INSUFFICIENT_FUNDS)
    assert swept_delay > base_delay


def test_a_sweep_around_a_null_result_refuses_to_run() -> None:
    """The first phase-7 sweep ran at a sizing where the unswept lift straddled zero. It
    returned 30 'fragile' keys with swings up to 51,700% — every one of them the sign
    flip of noise around a null, divided by a near-zero base. A fragility list is only
    meaningful when there is a conclusion to be fragile about."""
    from rebound.harness.sensitivity import UnderpoweredSweepError

    class _NullSweep:
        def base_point(self):
            return _point(-14_138.0, -91_899.0, 63_622.0)

    with pytest.raises(UnderpoweredSweepError, match="contains zero"):
        run_sweep(_NullSweep())


def test_fragility_is_not_claimed_when_the_base_is_not_significant() -> None:
    base = _point(-14_138.0, -91_899.0, 63_622.0)
    result = _result(base, _point(200_000.0, 100_000.0, 300_000.0), base)
    assert not result.base_is_significant
    assert not result.is_fragile


# --- the probability floor (added after the first phase-7 sweep) ---------------


def test_a_near_one_reliability_figure_cannot_be_halved_into_nonsense(
    shipped: Assumptions,
) -> None:
    """The first phase-7 sweep took population.bank.uptime_daytime from 0.995 to 0.4975 --
    banks offline half the time -- and that world produced a lift 118x the base, dominating
    the fragility ranking with a scenario nobody claims could happen."""
    floor = float(shipped.value("harness.sensitivity.probability_floor"))
    swept, clamped = scaled(shipped, "population.bank.uptime_daytime", 0.5)
    assert clamped
    assert swept.value("population.bank.uptime_daytime") == floor


def test_a_small_probability_still_sweeps_freely(shipped: Assumptions) -> None:
    """The floor applies only where the unswept value is already above it. A 0.005
    technical-decline rate halving to 0.0025 is a real bank, not nonsense, and clamping it
    would silently narrow the question being asked."""
    base = float(shipped.value("population.bank.td_rate_min"))
    swept, clamped = scaled(shipped, "population.bank.td_rate_min", 0.5)
    assert not clamped
    assert swept.value("population.bank.td_rate_min") == pytest.approx(base * 0.5)


def test_every_clamped_point_is_flagged(shipped: Assumptions) -> None:
    """A clamped key is swept over a narrowed range, so its swing is not comparable to an
    unclamped key's. If the flag were lost, a small swing would read as robustness."""
    for factor in (0.5, 1.5):
        _, clamped = scaled(shipped, "population.bank.uptime_daytime", factor)
        assert clamped, f"uptime at x{factor} left the domain without being flagged"


def test_the_sweep_refuses_to_rank_around_a_null_base(shipped: Assumptions) -> None:
    """The guard the first sweep bypassed. `is_fragile` is undefined when the base is not
    significant, so a ranking computed there flags nothing by construction and reads as
    'no assumption matters'. run_sweep must raise rather than produce it."""

    class _NullBase(Sweep):
        def base_point(self) -> SweepPoint:  # type: ignore[override]
            return SweepPoint(
                key="<unswept>",
                factor=1.0,
                lift_point=109_572.0,
                lift_low=-21_207.0,
                lift_high=233_282.0,
                metric="net_recovered_paise",
            )

    sweep = _NullBase(
        assumptions=shipped,
        build=lambda a: [FixedSchedule(a)],
        subject="Blended",
    )
    with pytest.raises(UnderpoweredSweepError, match="contains zero"):
        run_sweep(sweep, ["book.avg_ticket_paise"])
