"""SPEC §15.5: the batch result as CSV, in two tables.

An answer and a refusal share almost no columns. One table would be mostly empty cells
with a status column deciding which half of the row to read, so they are kept apart and
selected with `table=`.

Both carry a `#`-prefixed header block with both fingerprints, as SPEC §13.2 does for the
trace: a table separated from its response is still traceable to the measurement and the
vocabulary that produced it. The file still parses with `comment='#'`.
"""

from __future__ import annotations

import csv
import io
from enum import StrEnum

from rebound.recommend.batch import BatchResult
from rebound.recommend.inputs import EXTENDED_COLUMNS


class Table(StrEnum):
    ANSWERS = "answers"
    REFUSALS = "refusals"


_ANSWER_COLUMNS = (
    "mandate_ref",
    "status",
    "retry_at",
    "strategy",
    "rule",
    "tier",
    "compliance_rule",
    "compliance_moved_from",
    "evidence_dimension",
    "evidence_segment",
    "evidence_verdict",
    "evidence_low_inr",
    "evidence_high_inr",
    "evidence_point_inr",
    "evidence_level",
    "evidence_sentence",
    "selection_notes",
)

# The echoed input (SPEC §15.2) is spread across the canonical columns, so a caller can
# correct the offending cell and resubmit the refusals table as a file in its own right.
_REFUSAL_COLUMNS = ("row_number", "kind", "detail", "fields", *EXTENDED_COLUMNS)


def _header_block(result: BatchResult, table: Table) -> list[str]:
    return [
        f"# rebound recommendation batch - {table.value}",
        f"# mapping_profile: {result.mapping_profile or 'none (values already canonical)'}",
        f"# mapping_fingerprint: {result.mapping_fingerprint or 'none'}",
        f"# evidence_fingerprint: {result.evidence_fingerprint}",
        f"# rows_read: {result.rows_read}  answered: {result.rows_answered}  "
        f"refused: {result.rows_refused}  (malformed: {result.rows_malformed})",
        "# Recommended times only. No field here is a prediction of success (SPEC 14.1).",
        "# Batch is minimum-tier by construction: BankAware, SalaryAware and Blended "
        "cannot be selected from a CSV upload (SPEC 11). Their absence is a format limit, "
        "not a measurement.",
    ]


def _answer_rows(result: BatchResult) -> list[dict[str, object]]:
    rows = []
    for answer in result.recommendations:
        evidence = answer.evidence
        compliance = answer.compliance
        rows.append(
            {
                "mandate_ref": answer.mandate_ref,
                "status": answer.status.value,
                "retry_at": answer.retry_at.isoformat() if answer.retry_at else "",
                "strategy": answer.strategy or "",
                "rule": answer.rule,
                "tier": answer.tier.value,
                "compliance_rule": compliance.rule if compliance else "",
                "compliance_moved_from": (
                    compliance.moved_from.isoformat()
                    if compliance and compliance.moved_from
                    else ""
                ),
                "evidence_dimension": evidence.dimension if evidence else "",
                "evidence_segment": evidence.segment_label if evidence else "",
                "evidence_verdict": evidence.verdict if evidence else "",
                "evidence_low_inr": evidence.low_inr if evidence else "",
                "evidence_high_inr": evidence.high_inr if evidence else "",
                "evidence_point_inr": evidence.point_inr if evidence else "",
                "evidence_level": evidence.level if evidence else "",
                "evidence_sentence": evidence.sentence if evidence else "",
                "selection_notes": " | ".join(answer.selection_notes),
            }
        )
    return rows


def _refusal_rows(result: BatchResult) -> list[dict[str, object]]:
    return [
        {
            "row_number": refusal.row_number,
            "kind": refusal.kind.value,
            "detail": refusal.detail,
            "fields": " ".join(refusal.fields),
            **{column: refusal.input.get(column, "") for column in EXTENDED_COLUMNS},
        }
        for refusal in result.refusals
    ]


def to_csv(result: BatchResult, table: Table) -> str:
    columns = _ANSWER_COLUMNS if table is Table.ANSWERS else _REFUSAL_COLUMNS
    rows = _answer_rows(result) if table is Table.ANSWERS else _refusal_rows(result)
    buffer = io.StringIO()
    buffer.write("\n".join(_header_block(result, table)) + "\n")
    writer = csv.DictWriter(buffer, fieldnames=list(columns), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()
