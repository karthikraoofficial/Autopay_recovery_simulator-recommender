"""SPEC §14.9: a CSV of failures in, the same answer per row out.

Two properties worth stating. **Refusal is per row, not per file** — failing the file
closed for one bad row would discard good answers for rows that were fine. And **refusals
are counted in a summary ahead of the rows**, because a partial refusal that is only
discoverable by reading four thousand rows is a silent one.

Columns come from `inputs.MINIMUM_COLUMNS`/`EXTENDED_COLUMNS`, which are the single
definition of the tiers; this module never restates them.
"""

from __future__ import annotations

import csv
import io
from collections import Counter
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from rebound.config import Assumptions, load_assumptions
from rebound.harness.segments import SegmentReport
from rebound.recommend.ingest import check_has_rows, prepare, refusing
from rebound.recommend.inputs import EXTENDED_COLUMNS, MINIMUM_COLUMNS, ObservedFailure
from rebound.recommend.mapping import MerchantMapping, UnmappedValueError
from rebound.recommend.service import Recommendation, recommend

# Surplus values land here and missing ones are filled with this, so that a row whose
# length disagrees with the header is detectable rather than silently reconciled.
_EXTRA_KEY = "__surplus_values__"
_MISSING = object()

# The one key `refusals_by_field` may carry that is not a column name. Bounded on purpose:
# whatever a malformed file names its columns, the summary stays a list of columns.
UNRECOGNISED_COLUMN = "(unrecognised column)"


class BatchTooLargeError(ValueError):
    """More rows than `recommend.batch_max_rows`. Refused whole, before any row is run."""


class RefusalKind(StrEnum):
    MALFORMED_ROW = "malformed row"
    UNMAPPED_VALUE = "unmapped value"
    FIELD = "field"
    # A row that failed in a way nothing here anticipated. Refused like any other rather
    # than raised, so one strange row costs its own answer and not the whole file's.
    UNREADABLE_ROW = "unreadable row"


class RowRefusal(BaseModel):
    """One row that could not be answered, and why. A complete answer for that row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row_number: int = Field(ge=1)
    kind: RefusalKind = RefusalKind.FIELD
    mandate_ref: str | None = None
    # Empty for a MALFORMED_ROW: a row with the wrong number of values has no column to
    # fix, and putting a pseudo-column in the summary would make it unreadable as a list
    # of columns.
    fields: tuple[str, ...] = ()
    detail: str = Field(min_length=1)
    # SPEC §15.2: the raw cells this row was refused for, so a caller can fix and resubmit
    # from the response rather than going back to their file to work out which line it was.
    # The merchant's own data returning to them; the upload already disclosed it.
    input: dict[str, str] = Field(default_factory=dict)


class BatchResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # SPEC §15.4. Both fingerprints on the summary, so a file answered differently on two
    # days is traceable to which of the two inputs moved. `None` means no mapping was
    # applied and the values arrived already canonical.
    mapping_profile: str | None = None
    mapping_fingerprint: str | None = None
    evidence_fingerprint: str = Field(min_length=1)
    rows_read: int = Field(ge=0)
    rows_answered: int = Field(ge=0)
    rows_refused: int = Field(ge=0)
    # Rows refused for their shape rather than for a value. Counted separately because
    # they have no column, and a caller who sees a large number here has a file problem
    # rather than a data problem.
    rows_malformed: int = Field(default=0, ge=0)
    # Field name -> how many rows it refused. Stated ahead of the rows, per SPEC §14.9.
    # Keys are column names, or `UNRECOGNISED_COLUMN` -- never anything else.
    refusals_by_field: dict[str, int] = Field(default_factory=dict)
    recommendations: tuple[Recommendation, ...] = ()
    refusals: tuple[RowRefusal, ...] = ()


def template(extended: bool = False) -> str:
    """The header row a merchant fills in. Derived from the tier definition, not restated."""
    columns = EXTENDED_COLUMNS if extended else MINIMUM_COLUMNS
    return ",".join(columns) + "\n"


def _shape_refusal(row_number: int, row: dict[str, object]) -> RowRefusal | None:
    """A row whose value count disagrees with the header's is refused, never trimmed.

    `csv.DictReader` is lenient by default: surplus values are collected under `restkey`
    and missing ones filled with `restval`, so a row with an extra trailing value used to
    be answered confidently over a discarded cell. That was the only path in this file
    that returned an answer built on data it had thrown away.

    Both directions are refused. A short row's missing tail is not discarded data, but a
    row that disagrees with its header is malformed either way, and reading its last two
    optional columns as deliberately blank is a guess about which end was truncated.
    """
    surplus = row.get(_EXTRA_KEY)
    absent = sorted(key for key, value in row.items() if value is _MISSING)
    if surplus:
        count = len(row) - 1 + len(surplus)  # the mapped columns, less the restkey, plus extras
        detail = (
            f"row has {count} values but the header has {len(row) - 1}. Refused rather "
            f"than trimmed: the surplus value(s) {surplus} would otherwise be discarded "
            "and the row answered as though they had never been sent."
        )
    elif absent:
        detail = (
            f"row has {len(row) - len(absent)} values but the header has {len(row)}; "
            f"{', '.join(absent)} ran off the end of the row. Refused rather than read as "
            "deliberately blank."
        )
    else:
        return None
    return RowRefusal(
        row_number=row_number,
        kind=RefusalKind.MALFORMED_ROW,
        mandate_ref=_mandate_ref(row),
        detail=detail,
    )


def _mandate_ref(row: dict[str, object]) -> str | None:
    value = row.get("mandate_ref")
    return value if isinstance(value, str) and value.strip() else None


def _clean(row: dict[str, object]) -> dict[str, str]:
    """Drop empty cells so an unfilled optional column reads as absent, not as an empty
    string. An absent optional field is a tier statement; an empty one is a typo."""
    return {
        key: value
        for key, value in row.items()
        if isinstance(key, str) and isinstance(value, str) and value.strip() != ""
    }


def _fields_in(error: ValidationError) -> tuple[str, ...]:
    """Which columns a row was refused for.

    A per-field error carries its field in `loc`. The cross-field rules of SPEC §14.2 —
    a retry without its original attempt time, eNACH without a cutoff — are model-level
    validators, and pydantic gives those an empty `loc`, so the columns they name are
    recovered from the message. That is why those messages name their field explicitly:
    the summary count in SPEC §14.9 is only useful if every refusal lands under a column.

    **Every name returned is a real column or `UNRECOGNISED_COLUMN`.** A mis-delimited
    file gives `DictReader` one column whose name is the entire header line, and passing
    that through put a header line in the summary as if it were a field. The summary is a
    list of columns to fix; it must not be able to take any other shape, whatever arrives.
    """
    known = set(EXTENDED_COLUMNS)
    fields: set[str] = set()
    for entry in error.errors():
        if entry["loc"]:
            name = str(entry["loc"][0])
            fields.add(name if name in known else UNRECOGNISED_COLUMN)
            continue
        # The column the merchant has to supply, which is the one the message leads with.
        # A cross-field message names the other column too ("original_attempt_at is
        # required when attempt_number > 1"); counting both would put one refusal under
        # two headings and overstate the summary.
        named = [(entry["msg"].index(c), c) for c in EXTENDED_COLUMNS if c in entry["msg"]]
        if named:
            fields.add(min(named)[1])
    return tuple(sorted(fields))


def _refusal(row_number: int, values: dict[str, str], error: ValidationError) -> RowRefusal:
    detail = "; ".join(
        f"{'.'.join(str(p) for p in e['loc']) or 'row'}: {e['msg']}" for e in error.errors()
    )
    return RowRefusal(
        row_number=row_number,
        kind=RefusalKind.FIELD,
        mandate_ref=values.get("mandate_ref"),
        fields=_fields_in(error),
        detail=detail,
        input=values,
    )


def _unmapped_refusal(
    row_number: int, values: dict[str, str], error: UnmappedValueError
) -> RowRefusal:
    return RowRefusal(
        row_number=row_number,
        kind=RefusalKind.UNMAPPED_VALUE,
        mandate_ref=values.get("mandate_ref"),
        fields=(error.column,) if error.column in EXTENDED_COLUMNS else (),
        detail=str(error),
        input=values,
    )


def _answer_row(
    row_number: int,
    row: dict[str, object],
    mapping: MerchantMapping | None,
    report: SegmentReport,
    assumptions: Assumptions,
) -> Recommendation | RowRefusal:
    """One row: shape, then vocabulary, then the model, then the same `recommend` as §14."""
    malformed = _shape_refusal(row_number, row)
    if malformed is not None:
        return malformed
    values = _clean(row)
    try:
        mapped = mapping.apply(values) if mapping is not None else values
    except UnmappedValueError as error:
        return _unmapped_refusal(row_number, values, error)
    try:
        failure = ObservedFailure.model_validate(mapped)
    except ValidationError as error:
        # The echo is the *raw* cells, not the mapped ones: a caller fixing their file
        # needs to see what they sent, not what we turned it into.
        return _refusal(row_number, values, error)
    return recommend(failure, report, assumptions, mapping_fingerprint=_fingerprint(mapping))


def _fingerprint(mapping: MerchantMapping | None) -> str | None:
    return mapping.fingerprint() if mapping is not None else None


def _parse_rows(text: str) -> list[dict[str, object]]:
    """Every row, or a refusal naming the line the parser gave up on.

    `csv` raises during iteration, and its state after an error is not defined, so a failure
    here refuses the file rather than trying to resume past it. The line number comes from
    the reader, because "somewhere in your file" is not an actionable message.
    """
    reader = csv.DictReader(io.StringIO(text), restkey=_EXTRA_KEY, restval=_MISSING)
    # `line_num` is the last line the reader *finished*, so the record that failed starts on
    # the next one. Reporting the finished line would send someone to look at the row before
    # the problem, which is worse than reporting nothing.
    with refusing("parsing the rows", lambda: reader.line_num + 1):
        return list(reader)


def run_batch(
    body: bytes | str,
    report: SegmentReport,
    assumptions: Assumptions | None = None,
    mapping: MerchantMapping | None = None,
) -> BatchResult:
    """Answer every row that can be answered, and account for every row that cannot.

    File-level defects are refused whole by `ingest.prepare` before any row is read
    (SPEC §15.1); everything that varies row to row is answered row to row (SPEC §15.2).
    """
    assumptions = assumptions or load_assumptions()
    max_rows = int(assumptions.value("recommend.batch_max_rows"))
    text = prepare(body if isinstance(body, bytes) else body.encode("utf-8"), mapping)
    rows = _parse_rows(text)
    if len(rows) > max_rows:
        raise BatchTooLargeError(
            f"{len(rows)} rows exceeds recommend.batch_max_rows ({max_rows}). Nothing was "
            "processed; split the file rather than reading a partial answer as a whole one."
        )
    check_has_rows(len(rows))
    answers: list[Recommendation] = []
    refusals: list[RowRefusal] = []
    for index, row in enumerate(rows, start=2):  # row 1 is the header, as the caller sees it
        try:
            outcome = _answer_row(index, row, mapping, report, assumptions)
        except Exception as exc:  # noqa: BLE001 - deliberate; see RefusalKind.UNREADABLE_ROW
            # Contained at the row, not the file: an unanticipated failure on one row must
            # not cost the other rows their answers. It is reported as a refusal naming the
            # exception, so it stays diagnosable rather than being quietly swallowed.
            outcome = RowRefusal(
                row_number=index,
                kind=RefusalKind.UNREADABLE_ROW,
                mandate_ref=_mandate_ref(row),
                detail=(
                    "This row was not answered because rebound failed while processing "
                    "it. THIS IS A DEFECT IN REBOUND, NOT A PROBLEM WITH YOUR DATA: there "
                    "is nothing to correct in this row, and re-sending it unchanged will "
                    "fail the same way until the defect is fixed. The other rows in the "
                    f"file were answered normally. For the report: {type(exc).__name__}: "
                    f"{exc}"
                ),
                input=_clean(row),
            )
        (answers if isinstance(outcome, Recommendation) else refusals).append(outcome)  # type: ignore[arg-type]
    counts = Counter(field for refusal in refusals for field in refusal.fields)
    return BatchResult(
        mapping_profile=mapping.name if mapping is not None else None,
        mapping_fingerprint=_fingerprint(mapping),
        evidence_fingerprint=report.config_fingerprint,
        rows_read=len(rows),
        rows_answered=len(answers),
        rows_refused=len(refusals),
        rows_malformed=sum(1 for r in refusals if r.kind is RefusalKind.MALFORMED_ROW),
        refusals_by_field=dict(sorted(counts.items())),
        recommendations=tuple(answers),
        refusals=tuple(refusals),
    )
