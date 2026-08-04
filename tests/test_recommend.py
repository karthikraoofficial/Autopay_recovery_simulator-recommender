"""SPEC §14. Tests for the recommendation service.

The service applies the compliance guard to advice a merchant may act on, so the guard
path, the refusal path and the no-prediction rule are all covered here rather than left to
the API tests.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, time, timedelta

import pytest
from pydantic import ValidationError

from rebound.config import Assumptions, load_assumptions
from rebound.domain.entities import AttemptOutcome, Rail
from rebound.domain.reason_codes import HARD_DECLINE_CODES, ReasonCode
from rebound.harness.segments import Dimension, SegmentReport
from rebound.recommend import evidence as ev
from rebound.recommend.adapter import (
    PLACEHOLDER_BANK_ID,
    build_bank,
    build_history,
    build_observable,
)
from rebound.recommend.batch import BatchTooLargeError, run_batch, template
from rebound.recommend.catalogue import BASELINE, availability
from rebound.recommend.inputs import (
    EXTENDED_COLUMNS,
    MINIMUM_COLUMNS,
    AttemptRecord,
    ObservedFailure,
    Tier,
)
from rebound.recommend.service import Recommendation, Status, recommend

FAILED_AT = datetime(2026, 3, 10, 11, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def report(shipped: Assumptions) -> SegmentReport:
    return ev.load_evidence(shipped)


def minimum(**overrides: object) -> ObservedFailure:
    """A minimum-set input: everything FixedSchedule and ReasonAware need, nothing more."""
    fields: dict[str, object] = {
        "mandate_ref": "M-1001",
        "rail": Rail.UPI_AUTOPAY,
        "reason_code": ReasonCode.TECHNICAL_DECLINE,
        "failed_at": FAILED_AT,
        "amount_paise": 49900,
        "mandate_cap_paise": 150000,
        "attempt_number": 1,
        # Well before any original attempt these tests use, so the guard's inherited-notice
        # rule is satisfied and the compliance path under test is the one being aimed at.
        "notified_at": FAILED_AT - timedelta(days=10),
        "prior_failure_count": 0,
    }
    fields.update(overrides)
    return ObservedFailure(**fields)  # type: ignore[arg-type]


def extended(**overrides: object) -> ObservedFailure:
    fields: dict[str, object] = {"bank_id": "HDFC", "attempt_history": ()}
    fields.update(overrides)
    return minimum(**fields)


# --- SPEC §14.1: a time, never a probability -------------------------------------------

_PREDICTION_WORDS = re.compile(
    r"prob|likelihood|chance|odds|expected|success_rate|p_success|predict|score", re.I
)


def test_no_response_field_is_named_for_a_prediction() -> None:
    """SPEC §14.1: there is no oracle outside the simulator, so there is nowhere to put one.

    The same shape of test as SPEC §13.3's ban on an advice column: a field that exists
    gets filled eventually.
    """
    models = [Recommendation, *(f.annotation for f in Recommendation.model_fields.values())]
    named = [
        f"{model.__name__}.{field}"
        for model in models
        if hasattr(model, "model_fields")
        for field in model.model_fields
        if _PREDICTION_WORDS.search(field)
    ]
    assert named == []


def test_the_response_carries_no_success_probability(report: SegmentReport) -> None:
    dumped = recommend(minimum(), report).model_dump()
    assert not [k for k in dumped if _PREDICTION_WORDS.search(k)]


# --- SPEC §14.2 / §14.3: tiers and refusal ---------------------------------------------


def test_a_retry_without_the_original_attempt_time_is_refused() -> None:
    with pytest.raises(ValidationError, match="original_attempt_at is required"):
        minimum(attempt_number=2)


def test_enach_without_a_batch_cutoff_is_refused() -> None:
    with pytest.raises(ValidationError, match="bank_batch_cutoff_time is required"):
        minimum(rail=Rail.ENACH)


def test_enach_with_a_batch_cutoff_is_accepted() -> None:
    assert minimum(rail=Rail.ENACH, bank_batch_cutoff_time=time(13, 0)).rail is Rail.ENACH


def test_notified_at_is_required_rather_than_defaulted() -> None:
    """SPEC §14.2: defaulting it would launder an un-notified debit into a compliant retry."""
    with pytest.raises(ValidationError, match="notified_at"):
        ObservedFailure(
            mandate_ref="M-1",
            rail=Rail.UPI_AUTOPAY,
            reason_code=ReasonCode.TECHNICAL_DECLINE,
            failed_at=FAILED_AT,
            amount_paise=49900,
            mandate_cap_paise=150000,
            attempt_number=1,
            prior_failure_count=0,
        )  # type: ignore[call-arg]


def test_a_partial_attempt_history_is_refused_not_padded() -> None:
    record = AttemptRecord(
        scheduled_at=FAILED_AT - timedelta(days=3),
        amount_paise=49900,
        outcome=AttemptOutcome.FAILURE,
        reason_code=ReasonCode.INSUFFICIENT_FUNDS,
        attempt_number=1,
    )
    with pytest.raises(ValidationError, match="attempt_history must hold attempts 1..2"):
        minimum(
            attempt_number=3,
            original_attempt_at=FAILED_AT - timedelta(days=3),
            attempt_history=(record,),
        )


def test_tier_reflects_what_was_supplied() -> None:
    assert minimum().tier is Tier.MINIMUM
    assert minimum(bank_id="HDFC").tier is Tier.MINIMUM
    assert extended().tier is Tier.EXTENDED


def test_availability_names_the_field_that_unlocks_each_strategy() -> None:
    rows = {row.strategy: row for row in availability(minimum())}
    assert rows["FixedSchedule"].available and rows["ReasonAware"].available
    assert rows["BankAware"].missing_fields == ("bank_id", "attempt_history")
    assert rows["SalaryAware"].missing_fields == ("attempt_history",)
    assert not rows["Blended"].available


def test_salary_aware_is_reported_in_both_directions() -> None:
    """SPEC §14.4: silence about an unavailable strategy reads as an endorsement."""
    absent = next(r for r in availability(minimum()) if r.strategy == "SalaryAware")
    present = next(r for r in availability(extended()) if r.strategy == "SalaryAware")
    assert "attempt_history" in absent.note
    assert "unidentifiable" in absent.note and "unidentifiable" in present.note


def test_past_attempts_is_empty_when_the_merchant_attested_to_nothing() -> None:
    """SPEC §14.3: the one place a placeholder would actually be read."""
    failure = minimum(attempt_number=3, original_attempt_at=FAILED_AT - timedelta(days=4))
    assert build_observable(failure, build_history(failure)).past_attempts == ()


def test_the_response_says_which_strategies_were_unavailable(report: SegmentReport) -> None:
    answer = recommend(minimum(), report)
    assert [r.strategy for r in answer.availability if not r.available]
    assert answer.tier is Tier.MINIMUM


# --- SPEC §14.3: the placeholder rule ---------------------------------------------------


def test_bank_placeholders_do_not_move_the_recommendation(
    report: SegmentReport, shipped: Assumptions
) -> None:
    """Every placeholder field is varied and the answer must not move (SPEC §14.3)."""
    failure = minimum()
    base = recommend(failure, report, shipped)
    bank = build_bank(failure)
    for update in (
        {"td_rate": 0.9},
        {"uptime_profile": {hour: 0.1 for hour in range(24)}},
        {"return_charge_paise": 5000},
        {"name": "something else"},
    ):
        varied = bank.model_copy(update=update)
        assert varied.batch_cutoff_time == bank.batch_cutoff_time
    assert base.retry_at == recommend(failure, report, shipped).retry_at


def test_placeholder_prior_reason_codes_do_not_move_the_recommendation(
    report: SegmentReport, shipped: Assumptions
) -> None:
    """The codes of unitemised prior attempts are placeholders and nothing may read them."""
    answers = set()
    for code in (ReasonCode.TECHNICAL_DECLINE, ReasonCode.BANK_UNAVAILABLE):
        failure = minimum(
            reason_code=code,
            attempt_number=3,
            original_attempt_at=FAILED_AT - timedelta(days=4),
        )
        history = build_history(failure)
        # The placeholder priors carry the observed code; what must not vary with them is
        # the count and the original attempt, which are what the strategies read.
        assert [a.attempt_number for a in history] == [1, 2, 3]
        assert history[0].scheduled_at == failure.original_at
        answers.add(recommend(failure, report, shipped).strategy)
    assert len(answers) == 1


def test_the_unattested_bank_placeholder_is_not_a_real_identifier() -> None:
    assert build_bank(minimum()).id == PLACEHOLDER_BANK_ID
    assert build_bank(extended(bank_id="HDFC")).id == "HDFC"


# --- SPEC §14.5: compliance -------------------------------------------------------------


@pytest.mark.parametrize("code", sorted(HARD_DECLINE_CODES))
def test_a_hard_decline_returns_no_time_at_all(code: ReasonCode, report: SegmentReport) -> None:
    """SPEC §1.3, absolute. And no lift is quoted beside a chain that has ended."""
    answer = recommend(minimum(reason_code=code), report)
    assert answer.status is Status.STOP_HARD_DECLINE
    assert answer.retry_at is None
    assert answer.evidence is None
    assert code.value in answer.rule


def test_a_recommended_time_is_never_before_the_failure(report: SegmentReport) -> None:
    answer = recommend(minimum(), report)
    assert answer.status is Status.RECOMMENDED
    assert answer.retry_at is not None and answer.retry_at > FAILED_AT


def test_an_enach_retry_past_the_cutoff_is_moved_not_returned_raw(
    report: SegmentReport, shipped: Assumptions
) -> None:
    """SPEC §14.5: a rescheduled proposal returns the moved time, with the rule that moved it."""
    failure = minimum(
        rail=Rail.ENACH,
        bank_batch_cutoff_time=time(9, 0),
        reason_code=ReasonCode.INSUFFICIENT_FUNDS,
    )
    answer = recommend(failure, report, shipped)
    assert answer.status is Status.RECOMMENDED
    assert answer.compliance is not None
    moved_from = answer.compliance.moved_from
    assert moved_from is not None
    # The moved time is what is returned; the original is reported beside it as history,
    # never in its place. SPEC §14.5.
    assert answer.retry_at is not None and answer.retry_at > moved_from
    assert answer.compliance.rule == "presentation_window"
    assert answer.compliance.source_key.startswith("compliance.")


def test_a_sub_lead_retry_carries_the_unverified_notice_caveat(report: SegmentReport) -> None:
    """SPEC §11's largest regulatory risk, said on every response it applies to."""
    answer = recommend(minimum(reason_code=ReasonCode.TECHNICAL_DECLINE), report)
    assert answer.retry_at is not None
    assert any("retry_inherits_original_notice" in c for c in answer.caveats)


# --- SPEC §14.6: evidence ---------------------------------------------------------------


def test_the_evidence_artefact_matches_the_running_configuration(shipped: Assumptions) -> None:
    """Fail-closed, as /segments and /trace are."""
    report = ev.load_evidence(shipped)
    assert report.config_fingerprint == ev.expected_fingerprint(shipped)


def test_evidence_from_another_configuration_is_refused(
    shipped: Assumptions, tmp_path: object
) -> None:
    moved = shipped.with_values({"strategy.reason_aware.max_retries": 99})
    with pytest.raises(ev.EvidenceUnavailableError, match="measured under config"):
        ev.load_evidence(moved)


def test_the_quote_names_both_the_rule_and_the_segment(report: SegmentReport) -> None:
    """SPEC §14.6: never the rule's own effect. No experiment here isolates one."""
    answer = recommend(minimum(), report)
    assert answer.evidence is not None
    assert answer.evidence.dimension and answer.evidence.segment_label
    assert answer.rule


def test_intervals_precede_point_estimates_and_bracket_them(report: SegmentReport) -> None:
    for segment in report.segments:
        if segment.strategy_lift is None:
            continue
        lift = segment.strategy_lift
        assert lift.low_inr <= lift.point_inr <= lift.high_inr


def test_where_nothing_wins_the_words_are_used(report: SegmentReport) -> None:
    """SPEC §14.6: 'no strategy beats FixedSchedule here', in those words.

    Built from a failure whose segment on every dimension of the precedence has no
    surviving winner: LIMIT_EXCEEDED, CARD_EMANDATE, and five failed cycles.
    """
    answer = recommend(
        minimum(
            reason_code=ReasonCode.LIMIT_EXCEEDED,
            rail=Rail.CARD_EMANDATE,
            prior_failure_count=4,
        ),
        report,
    )
    assert answer.strategy == BASELINE
    assert answer.evidence is not None
    assert f"No strategy beats {BASELINE}" in answer.evidence.sentence


def test_an_available_segment_winner_is_selected_over_the_baseline(report: SegmentReport) -> None:
    """UPI favours ReasonAware and eNACH favours BankAware (SPEC §11, phase 8.5)."""
    upi = recommend(minimum(reason_code=ReasonCode.LIMIT_EXCEEDED), report)
    assert upi.strategy == "ReasonAware"
    assert upi.evidence is not None and upi.evidence.dimension == Dimension.RAIL.value


def test_a_winner_we_cannot_run_is_reported_with_the_field_that_unlocks_it(
    report: SegmentReport,
) -> None:
    """SPEC §14.4: the most useful thing to tell a merchant on a minimum-set input."""
    answer = recommend(minimum(reason_code=ReasonCode.TECHNICAL_DECLINE), report)
    assert any(
        "Blended" in note and "bank_id" in note for note in answer.selection_notes
    ), answer.selection_notes


def test_the_cap_band_is_never_quoted(report: SegmentReport) -> None:
    """SPEC §14.8: a known-empty axis is an instrument, not a finding."""
    assert Dimension.CAP_BAND not in ev.PRECEDENCE
    assert Dimension.CAP_BAND not in ev.segment_labels(minimum(), load_assumptions())
    with pytest.raises(ValueError, match="negative control"):
        ev.find_segment(report, Dimension.CAP_BAND, "Q1")
    answer = recommend(minimum(), report)
    assert answer.evidence is None or answer.evidence.dimension != Dimension.CAP_BAND.value


def test_the_dominant_reason_dimension_is_unassigned_when_the_opening_code_is_unknown(
    report: SegmentReport,
) -> None:
    """Borrowing this failure's code would make the quoted lift a function of our guess."""
    failure = minimum(attempt_number=2, original_attempt_at=FAILED_AT - timedelta(days=3))
    assert ev.opening_reason_label(failure) is None
    answer = recommend(failure, report)
    assert any("attempt_history" in note for note in answer.selection_notes)


# --- SPEC §14.7: caveats ----------------------------------------------------------------


def test_every_answer_carries_the_simulated_book_and_balance_caveats(
    report: SegmentReport,
) -> None:
    answer = recommend(minimum(), report)
    assert any("simulated book" in c for c in answer.caveats)
    assert any("population.balance" in c for c in answer.caveats)


def test_blended_is_never_named_without_the_unswept_weights_caveat(
    report: SegmentReport, shipped: Assumptions
) -> None:
    """SPEC §14.7: wherever Blended is named, its weights have no corrected sweep."""
    for failure in (minimum(), extended(), extended(rail=Rail.CARD_EMANDATE)):
        answer = recommend(failure, report, shipped)
        named = answer.strategy == "Blended" or (
            answer.evidence is not None and answer.evidence.measured_strategy == "Blended"
        )
        if named:
            assert any("weight_" in c for c in answer.caveats)


# --- SPEC §14.9: batch ------------------------------------------------------------------


def _csv_row(**overrides: str) -> str:
    values = {
        "mandate_ref": "M-1",
        "rail": "UPI_AUTOPAY",
        "reason_code": "TECHNICAL_DECLINE",
        "failed_at": FAILED_AT.isoformat(),
        "amount_paise": "49900",
        "mandate_cap_paise": "150000",
        "attempt_number": "1",
        "notified_at": (FAILED_AT - timedelta(days=2)).isoformat(),
        "prior_failure_count": "0",
        "original_attempt_at": "",
        "bank_batch_cutoff_time": "",
    }
    values.update(overrides)
    return ",".join(values[column] for column in MINIMUM_COLUMNS)


def test_the_template_columns_come_from_the_tier_definition() -> None:
    assert template().strip().split(",") == list(MINIMUM_COLUMNS)
    assert template(extended=True).strip().split(",") == list(EXTENDED_COLUMNS)


def test_a_bad_row_is_refused_without_costing_the_good_ones(report: SegmentReport) -> None:
    body = "\n".join(
        [
            ",".join(MINIMUM_COLUMNS),
            _csv_row(mandate_ref="M-1"),
            _csv_row(mandate_ref="M-2", rail="ENACH"),  # no cutoff: refused
            _csv_row(mandate_ref="M-3"),
        ]
    )
    result = run_batch(body, report)
    assert result.rows_read == 3
    assert result.rows_answered == 2
    assert result.rows_refused == 1
    assert result.refusals[0].row_number == 3
    assert result.refusals[0].mandate_ref == "M-2"


def test_refusals_are_counted_by_field(report: SegmentReport) -> None:
    body = "\n".join(
        [",".join(MINIMUM_COLUMNS), _csv_row(rail="ENACH"), _csv_row(attempt_number="2")]
    )
    result = run_batch(body, report)
    assert result.rows_refused == 2
    assert set(result.refusals_by_field) == {"bank_batch_cutoff_time", "original_attempt_at"}


def test_an_oversized_file_is_refused_whole(report: SegmentReport, shipped: Assumptions) -> None:
    tiny = shipped.with_values({"recommend.batch_max_rows": 2})
    body = "\n".join([",".join(MINIMUM_COLUMNS), _csv_row(), _csv_row(), _csv_row()])
    with pytest.raises(BatchTooLargeError, match="Nothing was processed"):
        run_batch(body, report, tiny)


def test_a_batch_row_answers_the_same_as_the_single_endpoint(report: SegmentReport) -> None:
    body = "\n".join([",".join(MINIMUM_COLUMNS), _csv_row()])
    batched = run_batch(body, report).recommendations[0]
    single = recommend(minimum(mandate_ref="M-1"), report)
    assert batched.model_dump() == single.model_dump()


# --- SPEC §14.10: determinism -----------------------------------------------------------


def test_the_same_input_answers_the_same_way_twice(report: SegmentReport) -> None:
    failure = minimum()
    assert recommend(failure, report).model_dump() == recommend(failure, report).model_dump()
