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

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from rebound.config import Assumptions, load_assumptions
from rebound.harness.segments import SegmentReport
from rebound.recommend.inputs import EXTENDED_COLUMNS, MINIMUM_COLUMNS, ObservedFailure
from rebound.recommend.service import Recommendation, recommend


class BatchTooLargeError(ValueError):
    """More rows than `recommend.batch_max_rows`. Refused whole, before any row is run."""


class RowRefusal(BaseModel):
    """One row that could not be answered, and why. A complete answer for that row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row_number: int = Field(ge=1)
    mandate_ref: str | None = None
    fields: tuple[str, ...] = ()
    detail: str = Field(min_length=1)


class BatchResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    rows_read: int = Field(ge=0)
    rows_answered: int = Field(ge=0)
    rows_refused: int = Field(ge=0)
    # Field name -> how many rows it refused. Stated ahead of the rows, per SPEC §14.9.
    refusals_by_field: dict[str, int] = Field(default_factory=dict)
    recommendations: tuple[Recommendation, ...] = ()
    refusals: tuple[RowRefusal, ...] = ()


def template(extended: bool = False) -> str:
    """The header row a merchant fills in. Derived from the tier definition, not restated."""
    columns = EXTENDED_COLUMNS if extended else MINIMUM_COLUMNS
    return ",".join(columns) + "\n"


def _clean(row: dict[str, str]) -> dict[str, str]:
    """Drop empty cells so an unfilled optional column reads as absent, not as an empty
    string. An absent optional field is a tier statement; an empty one is a typo."""
    return {k: v for k, v in row.items() if k is not None and v is not None and v.strip() != ""}


def _fields_in(error: ValidationError) -> tuple[str, ...]:
    """Which columns a row was refused for.

    A per-field error carries its field in `loc`. The cross-field rules of SPEC §14.2 —
    a retry without its original attempt time, eNACH without a cutoff — are model-level
    validators, and pydantic gives those an empty `loc`, so the columns they name are
    recovered from the message. That is why those messages name their field explicitly:
    the summary count in SPEC §14.9 is only useful if every refusal lands under a column.
    """
    fields: set[str] = set()
    for entry in error.errors():
        if entry["loc"]:
            fields.add(str(entry["loc"][0]))
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
        mandate_ref=values.get("mandate_ref"),
        fields=_fields_in(error),
        detail=detail,
    )


def run_batch(
    body: str,
    report: SegmentReport,
    assumptions: Assumptions | None = None,
) -> BatchResult:
    """Answer every row that can be answered, and account for every row that cannot."""
    assumptions = assumptions or load_assumptions()
    max_rows = int(assumptions.value("recommend.batch_max_rows"))
    rows = list(csv.DictReader(io.StringIO(body)))
    if len(rows) > max_rows:
        raise BatchTooLargeError(
            f"{len(rows)} rows exceeds recommend.batch_max_rows ({max_rows}). Nothing was "
            "processed; split the file rather than reading a partial answer as a whole one."
        )
    answers: list[Recommendation] = []
    refusals: list[RowRefusal] = []
    for index, row in enumerate(rows, start=2):  # row 1 is the header, as the caller sees it
        values = _clean(row)
        try:
            failure = ObservedFailure.model_validate(values)
        except ValidationError as error:
            refusals.append(_refusal(index, values, error))
            continue
        answers.append(recommend(failure, report, assumptions))
    counts = Counter(field for refusal in refusals for field in refusal.fields)
    return BatchResult(
        rows_read=len(rows),
        rows_answered=len(answers),
        rows_refused=len(refusals),
        refusals_by_field=dict(sorted(counts.items())),
        recommendations=tuple(answers),
        refusals=tuple(refusals),
    )
