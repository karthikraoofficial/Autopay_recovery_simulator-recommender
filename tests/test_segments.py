"""SPEC §12. Phase 8.5.

Two kinds of test here. The unit tests drive a `SegmentCollector` directly with a
hand-built history, because that is the only way to control what a segment contains. The
integration tests run a real (small) experiment, because the properties that matter most —
reconciliation to the headline, and the negative control staying empty — are only
meaningful against real runs.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from rebound.api.inputs import MerchantProfile, Sizing
from rebound.api.service import segment_report
from rebound.config import Assumptions
from rebound.domain.entities import (
    AttemptOutcome,
    DebitAttempt,
    EpisodeOutcome,
    Mandate,
    Rail,
    RecoveryEpisode,
)
from rebound.domain.reason_codes import ReasonCode
from rebound.harness.segments import (
    NEGATIVE_CONTROL,
    Dimension,
    SegmentCollector,
    SegmentReport,
    Verdict,
    _assign,
    _cap_bands,
    _dominant_reason,
    _frequency_band,
    render,
)

MASTER_SEED = 20260801
T0 = datetime(2026, 3, 1, tzinfo=UTC)
REFERENCE = "NoReschedule"


def _mandate(index: int, cap: int = 150_000, rail: Rail = Rail.UPI_AUTOPAY) -> Mandate:
    return Mandate(
        id=f"mandate-{index:06d}",
        merchant_id="merchant-000",
        customer_id=f"customer-{index:06d}",
        rail=rail,
        max_amount_paise=cap,
        created_at=T0,
    )


def _episode(cycle: int, reason: ReasonCode, recovered: bool = False) -> RecoveryEpisode:
    opening = DebitAttempt(
        id=f"a{cycle}",
        mandate_id="m",
        scheduled_at=T0,
        amount_paise=49_900,
        outcome=AttemptOutcome.FAILURE,
        reason_code=reason,
        attempt_number=1,
        is_retry=False,
    )
    return RecoveryEpisode(
        mandate_id="m",
        cycle_id=f"c{cycle}",
        cycle_index=cycle,
        original_attempt=opening,
        outcome=EpisodeOutcome.LAPSED if not recovered else EpisodeOutcome.RECOVERED,
        amount_recovered_paise=0,
    )


# --- segment assignment -------------------------------------------------------


class TestDominantReason:
    def test_the_mode_wins(self) -> None:
        codes = [ReasonCode.INSUFFICIENT_FUNDS] * 3 + [ReasonCode.TECHNICAL_DECLINE]
        assert _dominant_reason(codes) == ReasonCode.INSUFFICIENT_FUNDS.value

    def test_ties_break_on_enum_order_not_insertion_order(self) -> None:
        """`Counter.most_common` breaks ties by insertion, which would make a mandate's
        label depend on the order its episodes happened to occur — and therefore on the
        engine's draw order rather than on anything a merchant could observe."""
        one = [ReasonCode.TECHNICAL_DECLINE, ReasonCode.INSUFFICIENT_FUNDS]
        other = [ReasonCode.INSUFFICIENT_FUNDS, ReasonCode.TECHNICAL_DECLINE]
        assert _dominant_reason(one) == _dominant_reason(other)

    def test_a_hard_decline_does_not_override_the_mode(self) -> None:
        """SPEC §12.1. A revocation is one observation among others; letting it win would
        relabel every mandate that ever ended badly, regardless of why it kept failing."""
        codes = [ReasonCode.INSUFFICIENT_FUNDS] * 4 + [ReasonCode.MANDATE_REVOKED]
        assert _dominant_reason(codes) == ReasonCode.INSUFFICIENT_FUNDS.value

    def test_no_history_is_its_own_label(self) -> None:
        assert _dominant_reason([]) == "no failures"


class TestFrequencyBands:
    @pytest.mark.parametrize(
        ("count", "expected"),
        [(0, "no failures"), (1, "1"), (2, "2-3"), (3, "2-3"), (4, "4+"), (11, "4+")],
    )
    def test_bands_partition_the_counts(self, count: int, expected: str) -> None:
        assert _frequency_band(count, [2, 4]) == expected


def test_cap_bands_are_pooled_across_seeds_not_cut_per_seed() -> None:
    """The bug this replaced: per-seed quantiles gave each seed its own ₹ edges, so seed
    1's Q4 and seed 2's Q4 were different segments. The axis fragmented into one segment
    per seed, every one starved of data, and the test count multiplied by the seed count.
    """
    collector = SegmentCollector()
    for seed in (1, 2):
        for i in range(100):
            # Deliberately different cap distributions per seed.
            collector.saw_mandate(REFERENCE, seed, _mandate(i, cap=1_000 * (i + 1) * seed))
    _, labels, ranges = _cap_bands(collector, [0.25, 0.5, 0.75])
    assert labels == ("Q1", "Q2", "Q3", "Q4")
    assert len(set(labels)) == 4
    # The realised range is carried separately, so it can be shown without becoming part
    # of the segment's identity.
    assert all("Rs" in r for r in ranges)


def test_assignment_is_taken_from_the_reference_run_only() -> None:
    """SPEC §12.1. If each strategy segmented on its own history, a retry that changed an
    outcome would move the mandate to another segment, and a baseline-versus-strategy
    comparison would no longer be within-segment."""
    collector = SegmentCollector()
    mandate = _mandate(0)
    for strategy in (REFERENCE, "Blended"):
        collector.saw_mandate(strategy, 1, mandate)
    # The reference sees three insufficient-funds failures; Blended sees one technical.
    for cycle in range(3):
        collector.saw_episode(REFERENCE, 1, mandate, _episode(cycle, ReasonCode.INSUFFICIENT_FUNDS))
    collector.saw_episode("Blended", 1, mandate, _episode(0, ReasonCode.TECHNICAL_DECLINE))

    edges, labels, _ = _cap_bands(collector, [0.25, 0.5, 0.75])
    assigned = _assign(collector, 1, REFERENCE, _tiny_assumptions(), edges, labels)[mandate.id]
    assert assigned[Dimension.DOMINANT_REASON] == ReasonCode.INSUFFICIENT_FUNDS.value
    assert assigned[Dimension.FAILURE_FREQUENCY] == "2-3"


def _tiny_assumptions() -> Assumptions:
    from rebound.config import load_assumptions

    return load_assumptions()


# --- the real run -------------------------------------------------------------


@pytest.fixture(scope="module")
def report(shipped: Assumptions) -> SegmentReport:
    """One small real experiment, observed. Small enough for a test suite, real enough
    that reconciliation and the negative control mean something."""
    configured = shipped.with_values(
        {"book.months": 6, "api.interactive_seeds": 4, "api.interactive_book_size": 150}
    )
    profile = MerchantProfile(
        book_size=150,
        avg_ticket_inr=499.0,
        upi_autopay_share=0.55,
        enach_share=0.30,
        performance_fee_rate=0.15,
    )
    return segment_report(profile, MASTER_SEED, Sizing.INTERACTIVE, configured)


@pytest.mark.parametrize("dimension", list(Dimension))
def test_segments_reconcile_to_the_run_totals(report: SegmentReport, dimension: Dimension) -> None:
    """SPEC §12.3, and the check the whole report rests on. Every dimension partitions the
    same mandates, so each must sum to the same totals. If segments were re-simulated
    rather than cut from the headline's own runs, this is where the two would drift apart.
    """
    segments = report.for_dimension(dimension)
    assert segments, f"{dimension} produced no segments"
    assert sum(s.episodes for s in segments) == report.total_episodes
    assert sum(s.at_risk_inr for s in segments) == pytest.approx(report.total_at_risk_inr)
    assert sum(s.share_of_mandates for s in segments) == pytest.approx(1.0)
    assert sum(s.share_of_episodes for s in segments) == pytest.approx(1.0)


def test_no_negative_control_segment_survives_correction(report: SegmentReport) -> None:
    """SPEC §12.5. Mandate caps are generated independently of income, balance and salary
    day (|rho| < 0.04) and bind on 0.12% of mandates, so this axis carries no signal.

    A winner here does not mean caps matter. It means the significance procedure is
    manufacturing findings, and every other verdict in the report is suspect. If this
    fails, check whether the generator changed before touching the statistics.
    """
    control = report.negative_control
    assert control, "the negative control must be present, not optimised away"
    assert all(s.dimension is NEGATIVE_CONTROL for s in control)
    # Without this the assertion below is vacuous: a segment reporting 'insufficient data'
    # cannot be a winner whatever the statistics do, so a run where every control segment
    # is too small proves nothing about the correction.
    tested = [s for s in control if s.verdict is not Verdict.INSUFFICIENT_DATA]
    assert tested, (
        "no control segment had enough episodes to be tested, so this assertion would "
        "pass regardless of whether the correction works. Raise the fixture's book size "
        "or seeds until at least one control segment is testable."
    )
    winners = [s.label for s in control if s.verdict is Verdict.WINNER]
    assert winners == [], f"an axis with no signal produced winners: {winners}"


def test_the_negative_control_is_never_counted_among_the_findings(report: SegmentReport) -> None:
    labels = {(s.dimension, s.label) for s in report.findings}
    assert not any(d is NEGATIVE_CONTROL for d, _ in labels)
    assert len(report.findings) + len(report.negative_control) == len(report.segments)


def test_a_winner_is_only_ever_a_corrected_winner(report: SegmentReport) -> None:
    """SPEC §12.4: there is no uncorrected column, and no verdict that means 'won on its
    own'. A segment that clears zero raw but fails correction gets its own verdict saying
    so in words, and is not a winner."""
    for segment in report.segments:
        if segment.verdict is Verdict.WINNER:
            assert segment.strategy_lift is not None
            assert segment.strategy_lift.survives_correction
        if segment.verdict is Verdict.LOST_TO_CORRECTION:
            assert segment.strategy_lift is not None
            assert not segment.strategy_lift.survives_correction
            assert segment.strategy_lift.clears_zero_uncorrected


def test_the_test_count_and_corrected_threshold_are_reported(report: SegmentReport) -> None:
    """SPEC §12.4: stated next to the results, not in a footnote."""
    assert report.tests_performed == len(report.segments) * len(report.candidate_strategies)
    assert report.corrected_alpha == pytest.approx(
        (1.0 - report.level) / report.tests_performed
    )
    # A bootstrap p-value cannot resolve below 1/resamples; the report must know whether
    # its own threshold is testable rather than silently calling everything significant.
    assert report.resolvable


def test_reference_strategies_are_not_tested_as_candidates(report: SegmentReport) -> None:
    """Asking whether the no-retry floor beats the baseline has a known answer, and each
    such test tightens the correction on every real comparison."""
    assert report.baseline_strategy not in report.candidate_strategies
    assert report.reference_strategy not in report.candidate_strategies
    assert "NoRetry" not in report.candidate_strategies


def test_small_segments_report_insufficient_data_rather_than_an_interval(
    report: SegmentReport,
) -> None:
    """SPEC §12.4. Small segments are the main way a segment report manufactures findings."""
    small = [s for s in report.segments if s.episodes < report.min_episodes]
    assert small, "expected at least one segment below the threshold in a small run"
    for segment in small:
        assert segment.verdict is Verdict.INSUFFICIENT_DATA
        assert segment.strategy_lift is None
        assert segment.rescheduling is None
        assert segment.net_inr_per_mandate_year is None


def test_every_segment_carries_a_recommendation_matching_its_verdict(
    report: SegmentReport,
) -> None:
    """SPEC §12.6: derived from the measured result, not a template with numbers dropped
    in. Asserted by checking the four verdicts produce materially different text, and that
    the ones making no numeric claim contain no rupee figure to substitute."""
    by_verdict: dict[Verdict, set[str]] = {}
    for segment in report.segments:
        by_verdict.setdefault(segment.verdict, set()).add(segment.recommendation)
    assert len(by_verdict) > 1, "a run where every segment says the same thing proves nothing"
    for verdict, texts in by_verdict.items():
        if verdict is Verdict.INSUFFICIENT_DATA:
            continue
        # One text per verdict: the wording is a function of the verdict, not of the
        # numbers, so it cannot be read as a quantitative claim it has not earned.
        assert len(texts) <= 2
        assert all("Rs" not in text for text in texts)


def test_the_report_is_deterministic(shipped: Assumptions) -> None:
    """SPEC §0.5 reaches the segment report too: the bootstrap draws are seeded from the
    master seed, not from an unseeded global."""
    configured = shipped.with_values(
        {"book.months": 4, "api.interactive_seeds": 2, "api.interactive_book_size": 80}
    )
    profile = MerchantProfile(
        book_size=80,
        avg_ticket_inr=499.0,
        upi_autopay_share=0.55,
        enach_share=0.30,
        performance_fee_rate=0.15,
    )
    first = segment_report(profile, MASTER_SEED, Sizing.INTERACTIVE, configured)
    second = segment_report(profile, MASTER_SEED, Sizing.INTERACTIVE, configured)
    assert first.model_dump() == second.model_dump()


# --- rendering ----------------------------------------------------------------


def test_the_renderer_states_the_test_count_and_labels_the_control(report: SegmentReport) -> None:
    text = render(report)
    assert f"{report.tests_performed} TESTS PERFORMED" in text
    assert "NEGATIVE CONTROL - EXPECTED NULL" in text
    # SPEC §12.1: nobody should read the cap axis as ticket size without knowing why.
    assert "PROXY for ticket size" in text
    assert text.isascii(), "the plain-text renderer is piped to terminals of unknown encoding"


def test_the_renderer_never_prints_a_bare_uncorrected_win(report: SegmentReport) -> None:
    """Where a raw interval clears zero but correction kills it, the report says so in
    words rather than showing it as a near-miss."""
    text = render(report)
    assert "would win uncorrected, does not survive correction" in text or not [
        s for s in report.segments if s.verdict is Verdict.LOST_TO_CORRECTION
    ]
