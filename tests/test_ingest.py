"""SPEC §15. The ingestion surface: file-level checks, mapping profiles, fingerprints.

The point of most of these is *where* a defect is reported. A file-level defect diagnosed
per row is the same defect reported four thousand times, and the one thing the caller
needed to know — that the file was wrong — is the thing it does not say.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from rebound.config import Assumptions
from rebound.domain.reason_codes import ReasonCode
from rebound.harness.segments import SegmentReport
from rebound.recommend import evidence as ev
from rebound.recommend.batch import RefusalKind, run_batch
from rebound.recommend.csv_out import Table, to_csv
from rebound.recommend.ingest import FileRefusalError
from rebound.recommend.inputs import EXTENDED_COLUMNS, MINIMUM_COLUMNS, required_columns
from rebound.recommend.mapping import (
    MappingError,
    MerchantMapping,
    UnmappedValueError,
    available_mappings,
    load_mapping,
)

HEADER = ",".join(MINIMUM_COLUMNS)
ROW = (
    "M-1,UPI_AUTOPAY,TECHNICAL_DECLINE,2026-03-10T11:00:00+00:00,49900,150000,1,"
    "2026-02-28T11:00:00+00:00,0,,"
)


@pytest.fixture(scope="module")
def report(shipped: Assumptions) -> SegmentReport:
    return ev.load_evidence(shipped)


MAPPED_HEADER = (
    "reference,channel,failure_code,failure_time,amount_in_paise,cap_in_paise,"
    "attempt,notice_sent_at,prior_failures,first_attempt_time"
)


def body(*lines: str) -> bytes:
    return ("\n".join(lines) + "\n").encode("utf-8")


# --- SPEC §15.1: file-level defects refuse the file whole --------------------------------


def test_a_non_utf8_file_is_refused_rather_than_mangled(report: SegmentReport) -> None:
    """Previously an uncaught UnicodeDecodeError, which reached the caller as a 500."""
    with pytest.raises(FileRefusalError, match="not valid UTF-8"):
        run_batch("M-1,UPI".encode("utf-16"), report)


def test_a_utf8_bom_is_accepted(report: SegmentReport) -> None:
    result = run_batch(b"\xef\xbb\xbf" + body(HEADER, ROW), report)
    assert result.rows_answered == 1


def test_a_semicolon_delimited_file_is_refused_once_not_per_row(
    report: SegmentReport,
) -> None:
    """The defect that used to produce a header line as a key in the summary."""
    with pytest.raises(FileRefusalError, match="semicolon-delimited"):
        run_batch(body(HEADER.replace(",", ";"), ROW.replace(",", ";")), report)


def test_an_unknown_column_refuses_the_file(report: SegmentReport) -> None:
    with pytest.raises(FileRefusalError, match="unknown column"):
        run_batch(body(HEADER + ",notes", ROW + ",hello"), report)


def test_a_missing_required_column_refuses_the_file(report: SegmentReport) -> None:
    columns = [c for c in MINIMUM_COLUMNS if c != "notified_at"]
    with pytest.raises(FileRefusalError, match="required column"):
        run_batch(body(",".join(columns), ROW.rsplit(",", 3)[0] + ",0,,"), report)


def test_an_optional_column_may_be_absent(report: SegmentReport) -> None:
    """`original_attempt_at` and `bank_batch_cutoff_time` are conditionally required per
    row, so their absence is a row-level question, not a file-level one."""
    optional = ("original_attempt_at", "bank_batch_cutoff_time")
    columns = [c for c in MINIMUM_COLUMNS if c not in optional]
    row = ",".join(ROW.split(",")[:-2])
    assert run_batch(body(",".join(columns), row), report).rows_answered == 1


def test_required_columns_are_derived_from_the_model() -> None:
    """Listed by hand, a new required field would be forgotten by the header check."""
    required = required_columns()
    assert "notified_at" in required and "mandate_ref" in required
    assert "original_attempt_at" not in required
    assert "bank_batch_cutoff_time" not in required
    assert required <= set(EXTENDED_COLUMNS)


def test_a_headerless_file_is_refused(report: SegmentReport) -> None:
    with pytest.raises(FileRefusalError, match="no recognised column"):
        run_batch(body(ROW), report)


def test_an_empty_file_is_refused(report: SegmentReport) -> None:
    with pytest.raises(FileRefusalError, match="empty"):
        run_batch(b"", report)


def test_a_header_only_file_is_refused_rather_than_answered_as_zero_rows(
    report: SegmentReport,
) -> None:
    """SPEC §15.1: 'you sent nothing' is not a successful answer about nothing."""
    with pytest.raises(FileRefusalError, match="no data rows"):
        run_batch(body(HEADER), report)


# --- SPEC §15.2: row-level refusals stay per row -----------------------------------------


def test_one_bad_row_does_not_cost_the_good_ones(report: SegmentReport) -> None:
    result = run_batch(body(HEADER, ROW, ROW.replace("UPI_AUTOPAY", "ENACH"), ROW), report)
    assert (result.rows_answered, result.rows_refused) == (2, 1)


def test_a_refused_row_echoes_the_values_it_was_refused_for(report: SegmentReport) -> None:
    """SPEC §15.2: fix-and-resubmit from the response, without going back to the file."""
    result = run_batch(body(HEADER, ROW.replace("UPI_AUTOPAY", "ENACH")), report)
    echoed = result.refusals[0].input
    assert echoed["mandate_ref"] == "M-1"
    assert echoed["rail"] == "ENACH"
    assert echoed["amount_paise"] == "49900"


def test_the_echo_is_the_raw_value_not_the_mapped_one(
    report: SegmentReport, mapping: MerchantMapping
) -> None:
    """A caller fixing their file needs to see what they sent, not what we made of it."""
    row = "M-9,nach,U30,10/03/2026 11:00,49900,150000,1,28/02/2026 09:00,0,"
    result = run_batch(body(MAPPED_HEADER, row), report, mapping=mapping)
    assert result.refusals[0].input["rail"] == "nach"
    assert result.refusals[0].input["reason_code"] == "U30"


# --- SPEC §15.3: stored mapping profiles -------------------------------------------------


@pytest.fixture(scope="module")
def mapping() -> MerchantMapping:
    return load_mapping("example")


def test_the_example_profile_loads_and_is_listed() -> None:
    assert "example" in available_mappings()


def test_a_mapping_translates_the_merchant_vocabulary(
    report: SegmentReport, mapping: MerchantMapping
) -> None:
    row = "M-1,upi,U30,10/03/2026 11:00,49900,150000,1,28/02/2026 09:00,0,"
    result = run_batch(body(MAPPED_HEADER, row), report, mapping=mapping)
    assert result.rows_answered == 1
    assert result.recommendations[0].retry_at is not None


def test_an_unmapped_value_refuses_the_row_and_names_it(
    report: SegmentReport, mapping: MerchantMapping
) -> None:
    """SPEC §15.3: never guessed at. `U31` does not become whatever `U30` meant."""
    row = "M-1,upi,MYSTERY,10/03/2026 11:00,49900,150000,1,28/02/2026 09:00,0,"
    result = run_batch(body(MAPPED_HEADER, row), report, mapping=mapping)
    refusal = result.refusals[0]
    assert refusal.kind is RefusalKind.UNMAPPED_VALUE
    assert "MYSTERY" in refusal.detail and "example" in refusal.detail
    assert result.refusals_by_field == {"reason_code": 1}


def test_a_near_match_is_not_resolved(mapping: MerchantMapping) -> None:
    assert mapping.apply({"reason_code": "U30"})["reason_code"] == "INSUFFICIENT_FUNDS"
    with pytest.raises(UnmappedValueError):
        mapping.apply({"reason_code": "u30"})
    with pytest.raises(UnmappedValueError):
        mapping.apply({"reason_code": "U3"})


def test_a_canonical_value_needs_no_mapping_entry(mapping: MerchantMapping) -> None:
    assert mapping.apply({"rail": "ENACH"})["rail"] == "ENACH"


def test_a_profile_naming_a_code_that_does_not_exist_is_refused(tmp_path: Path) -> None:
    (tmp_path / "broken.yaml").write_text(
        yaml.safe_dump({"reason_codes": {"X": "NOT_A_REAL_CODE"}}), encoding="utf-8"
    )
    with pytest.raises(MappingError, match="unusable"):
        load_mapping("broken", tmp_path)


def test_a_profile_aliasing_onto_a_column_that_does_not_exist_is_refused(
    tmp_path: Path,
) -> None:
    (tmp_path / "broken.yaml").write_text(
        yaml.safe_dump({"columns": {"ref": "not_a_column"}}), encoding="utf-8"
    )
    with pytest.raises(MappingError, match="not columns of the upload"):
        load_mapping("broken", tmp_path)


def test_a_profile_name_cannot_escape_the_merchants_directory(tmp_path: Path) -> None:
    """The name arrives in a request, so traversal has to refuse rather than read a file."""
    with pytest.raises(MappingError, match="not a plain profile name"):
        load_mapping("../../etc/passwd", tmp_path)


def test_an_absent_profile_lists_what_is_available() -> None:
    with pytest.raises(MappingError, match="Available:"):
        load_mapping("no-such-merchant")


def test_a_naive_datetime_format_without_a_timezone_is_refused(tmp_path: Path) -> None:
    """SPEC §14.2 accepts only aware datetimes; guessing an offset moves the answer."""
    (tmp_path / "naive.yaml").write_text(
        yaml.safe_dump({"datetime_format": "%d/%m/%Y %H:%M"}), encoding="utf-8"
    )
    profile = load_mapping("naive", tmp_path)
    with pytest.raises(MappingError, match="timezone_offset_hours"):
        profile.apply({"failed_at": "10/03/2026 11:00"})


# --- SPEC §15.4: the mapping fingerprint --------------------------------------------------


def test_the_fingerprint_ignores_the_description(mapping: MerchantMapping) -> None:
    """Symmetric with Assumptions.fingerprint(): editing a comment must not invalidate a
    comparison a caller relies on, or the comparison becomes noise."""
    renamed = mapping.model_copy(update={"description": "completely different words"})
    assert renamed.fingerprint() == mapping.fingerprint()


def test_the_fingerprint_moves_when_a_mapping_moves(mapping: MerchantMapping) -> None:
    changed = mapping.model_copy(
        update={"reason_codes": {**mapping.reason_codes, "U99": ReasonCode.LIMIT_EXCEEDED}}
    )
    assert changed.fingerprint() != mapping.fingerprint()
    retimed = mapping.model_copy(update={"timezone_offset_hours": 0.0})
    assert retimed.fingerprint() != mapping.fingerprint()


def test_every_recommendation_echoes_both_fingerprints(
    report: SegmentReport, mapping: MerchantMapping
) -> None:
    row = "M-1,upi,U30,10/03/2026 11:00,49900,150000,1,28/02/2026 09:00,0,"
    result = run_batch(body(MAPPED_HEADER, row), report, mapping=mapping)
    assert result.mapping_fingerprint == mapping.fingerprint()
    assert result.evidence_fingerprint == report.config_fingerprint
    assert result.recommendations[0].mapping_fingerprint == mapping.fingerprint()


def test_no_mapping_reports_a_null_fingerprint_rather_than_a_blank_one(
    report: SegmentReport,
) -> None:
    """`None` says the values arrived canonical. An empty string would look like a hash."""
    result = run_batch(body(HEADER, ROW), report)
    assert result.mapping_fingerprint is None
    assert result.recommendations[0].mapping_fingerprint is None


# --- SPEC §15.5: CSV output ---------------------------------------------------------------


def test_the_csv_header_block_carries_both_fingerprints(
    report: SegmentReport, mapping: MerchantMapping
) -> None:
    result = run_batch(body(HEADER, ROW), report)
    text = to_csv(result, Table.ANSWERS)
    assert f"# evidence_fingerprint: {result.evidence_fingerprint}" in text
    assert "# mapping_fingerprint: none" in text
    assert "minimum-tier by construction" in text


def test_the_answers_table_parses_with_comment_hash(report: SegmentReport) -> None:
    import csv
    import io

    text = to_csv(run_batch(body(HEADER, ROW), report), Table.ANSWERS)
    rows = list(csv.DictReader(line for line in io.StringIO(text) if not line.startswith("#")))
    assert rows[0]["mandate_ref"] == "M-1"
    assert rows[0]["retry_at"]


def test_the_refusals_table_carries_the_input_back_under_canonical_columns(
    report: SegmentReport,
) -> None:
    """So it can be corrected and resubmitted as a file in its own right (SPEC §15.2)."""
    result = run_batch(body(HEADER, ROW.replace("UPI_AUTOPAY", "ENACH")), report)
    text = to_csv(result, Table.REFUSALS)
    header = next(line for line in text.splitlines() if not line.startswith("#"))
    assert set(MINIMUM_COLUMNS) <= set(header.split(","))
    assert "M-1" in text


def test_no_csv_column_is_named_for_a_prediction(report: SegmentReport) -> None:
    """SPEC §14.1 holds at every edge, including the one people open in Excel."""
    text = to_csv(run_batch(body(HEADER, ROW), report), Table.ANSWERS)
    header = next(line for line in text.splitlines() if not line.startswith("#"))
    assert not [c for c in header.split(",") if "prob" in c or "score" in c or "expect" in c]
