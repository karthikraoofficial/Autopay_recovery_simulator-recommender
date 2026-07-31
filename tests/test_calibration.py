"""Phase-4 calibration tests.

SPEC §9 phase 4 is done when the reason-code mix is tunable to a target distribution.
These assert the measurement is honest — that it moves in the right direction when a
knob moves, and that it distinguishes a mis-set knob from a mechanism the model does
not have.
"""

from __future__ import annotations

import pytest

from rebound.config import Assumptions
from rebound.domain.reason_codes import ReasonCode
from rebound.engine.calibration import REACHABILITY, Reachability, calibrate

SEEDS = (1, 2)

# Stand-in target: the phase-2 stub engine's invented mix. It is NOT a merchant's
# observed distribution, and no assumption in this repo is fitted to it. It exists so
# the calibration machinery has something to measure against.
STAND_IN_TARGET = {
    ReasonCode.INSUFFICIENT_FUNDS: 0.55,
    ReasonCode.TECHNICAL_DECLINE: 0.15,
    ReasonCode.BANK_UNAVAILABLE: 0.08,
    ReasonCode.MANDATE_REVOKED: 0.07,
    ReasonCode.LIMIT_EXCEEDED: 0.06,
    ReasonCode.MANDATE_EXPIRED: 0.04,
    ReasonCode.INVALID_PIN: 0.02,
    ReasonCode.MANDATE_AMOUNT_EXCEEDED: 0.02,
    ReasonCode.ACCOUNT_FROZEN: 0.01,
}


@pytest.fixture(scope="module")
def tiny(shipped: Assumptions) -> Assumptions:
    """Small enough to calibrate repeatedly in a test suite."""
    entries = dict(shipped.assumptions)
    for key, value in {"book.size": 150, "book.months": 6, "population.bank.count": 4}.items():
        entries[key] = entries[key].model_copy(update={"value": value})
    return shipped.model_copy(update={"assumptions": entries})


def _override(assumptions: Assumptions, key: str, value: float) -> Assumptions:
    entries = dict(assumptions.assumptions)
    entries[key] = entries[key].model_copy(update={"value": value})
    return assumptions.model_copy(update={"assumptions": entries})


def test_target_mix_must_be_a_distribution(tiny: Assumptions) -> None:
    with pytest.raises(ValueError, match="sum to 1.0"):
        calibrate(tiny, {ReasonCode.INSUFFICIENT_FUNDS: 0.5}, seeds=SEEDS)


def test_calibration_needs_at_least_one_seed(tiny: Assumptions) -> None:
    with pytest.raises(ValueError, match="at least one seed"):
        calibrate(tiny, STAND_IN_TARGET, seeds=())


def test_report_shares_form_a_distribution(tiny: Assumptions) -> None:
    report = calibrate(tiny, STAND_IN_TARGET, seeds=SEEDS)
    assert report.failures > 0
    assert sum(g.observed_share for g in report.gaps) == pytest.approx(1.0)
    assert 0.0 <= report.total_variation_distance <= 1.0


def test_a_matching_target_has_zero_distance(tiny: Assumptions) -> None:
    """Feeding the observed mix back in as the target must close the distance exactly.
    If it does not, the distance is measuring something other than what it claims."""
    observed = calibrate(tiny, STAND_IN_TARGET, seeds=SEEDS)
    exact = {g.code: g.observed_share for g in observed.gaps}
    # Renormalise away float drift so the target is a valid distribution.
    total = sum(exact.values())
    exact = {code: share / total for code, share in exact.items()}
    report = calibrate(tiny, exact, seeds=SEEDS)
    assert report.total_variation_distance == pytest.approx(0.0, abs=1e-9)


def test_turning_a_knob_moves_the_code_it_governs(tiny: Assumptions) -> None:
    """The point of the exercise: the reported gap has to respond to the assumption the
    report names. A calibration report whose knobs did not move their codes would send
    someone tuning the wrong number."""

    def expired_share(hazard: float) -> float:
        report = calibrate(
            _override(tiny, "engine.mandate_expiry_hazard_per_cycle", hazard),
            STAND_IN_TARGET,
            seeds=SEEDS,
        )
        return next(g.observed_share for g in report.gaps if g.code is ReasonCode.MANDATE_EXPIRED)

    assert expired_share(0.05) > expired_share(0.0015) > expired_share(0.0)


def test_moving_a_knob_toward_the_target_reduces_the_distance(tiny: Assumptions) -> None:
    """Tunability, stated as the phase-4 done criterion: the distance is something a
    knob can actually close."""
    baseline = calibrate(tiny, STAND_IN_TARGET, seeds=SEEDS)
    expired = next(g for g in baseline.gaps if g.code is ReasonCode.MANDATE_EXPIRED)
    assert expired.gap < 0, "expected too few expiries to begin with"
    # Raise the one knob the report names for this code, part of the way.
    closer = calibrate(
        _override(tiny, "engine.mandate_expiry_hazard_per_cycle", 0.0035),
        STAND_IN_TARGET,
        seeds=SEEDS,
    )
    moved = next(g for g in closer.gaps if g.code is ReasonCode.MANDATE_EXPIRED)
    assert abs(moved.gap) < abs(expired.gap)


def test_every_code_carries_the_knobs_that_move_it(tiny: Assumptions) -> None:
    report = calibrate(tiny, STAND_IN_TARGET, seeds=SEEDS)
    for gap in report.gaps:
        if gap.reachability is Reachability.MEASURED:
            assert gap.knobs, f"{gap.code} has no knob to turn"
        for key in gap.knobs:
            assert key in tiny.assumptions, f"{gap.code} names a knob that does not exist: {key}"


def test_invalid_pin_is_reported_as_not_modelled_rather_than_mis_tuned(
    tiny: Assumptions,
) -> None:
    """SPEC §1.3 lists INVALID_PIN for completeness and says it does not apply to autopay
    execution. It must read as a deliberate absence, not as a knob someone should chase."""
    report = calibrate(tiny, STAND_IN_TARGET, seeds=SEEDS)
    gap = next(g for g in report.gaps if g.code is ReasonCode.INVALID_PIN)
    assert gap.observed_share == 0.0
    assert gap.reachability is Reachability.NOT_MODELLED
    assert not gap.missing_mechanism
    assert gap.code in {g.code for g in report.codes_outside_measurement}


def test_revocation_is_reported_as_retry_only_not_as_a_missing_mechanism(
    tiny: Assumptions,
) -> None:
    """Calibration measures first attempts. Induced revocation is a §2.3 reaction to
    being retried, so its absence here says nothing about whether the mechanism works —
    `tests/test_engine.py` covers that."""
    report = calibrate(tiny, STAND_IN_TARGET, seeds=SEEDS)
    gap = next(g for g in report.gaps if g.code is ReasonCode.MANDATE_REVOKED)
    assert gap.reachability is Reachability.RETRY_ONLY
    assert not gap.missing_mechanism


def test_every_reason_code_has_a_reachability_verdict() -> None:
    assert set(REACHABILITY) == set(ReasonCode)


def test_calibration_is_deterministic(tiny: Assumptions) -> None:
    first = calibrate(tiny, STAND_IN_TARGET, seeds=SEEDS)
    second = calibrate(tiny, STAND_IN_TARGET, seeds=SEEDS)
    assert first.model_dump() == second.model_dump()
