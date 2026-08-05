"""SPEC §15.1: the checks that belong to the file, run before any row is read.

A file-level defect diagnosed per row is a defect reported four thousand times. Before
phase 10.5 an extra column refused every row with "Extra inputs are not permitted" and a
semicolon-delimited export refused every row for every field; neither message said the one
thing the caller needed, which is that the *file* was wrong.

Order matters and each check refuses before the next runs: a mis-decoded file has no
meaningful delimiter, and a mis-delimited one has no meaningful columns.
"""

from __future__ import annotations

import csv

from rebound.recommend.inputs import EXTENDED_COLUMNS, required_columns
from rebound.recommend.mapping import MerchantMapping

# Checked against a comma. Not `csv.Sniffer`, which guesses from data and can be confidently
# wrong on a file whose fields contain punctuation; this asks one question with one answer.
_CANDIDATE_DELIMITERS = {";": "semicolon", "\t": "tab", "|": "pipe"}


class FileRefusalError(ValueError):
    """The file is refused whole. One message, naming the defect (SPEC §15.1)."""


def decode(body: bytes) -> str:
    """UTF-8, BOM tolerated. Anything else is refused rather than mangled."""
    try:
        return body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise FileRefusalError(
            f"the file is not valid UTF-8 (byte {exc.object[exc.start]:#04x} at position "
            f"{exc.start}). Nothing was read. Re-export it as UTF-8 — a file saved as "
            "Windows-1252 or UTF-16 will otherwise be read as the wrong characters, or "
            "not at all."
        ) from exc


def _header_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line
    raise FileRefusalError("the file is empty. Nothing was read.")


def check_delimiter(text: str) -> None:
    """Refuse a file whose header another delimiter splits better than a comma does."""
    header = _header_line(text)
    commas = len(next(csv.reader([header])))
    for delimiter, name in _CANDIDATE_DELIMITERS.items():
        if len(header.split(delimiter)) > commas:
            raise FileRefusalError(
                f"the file looks {name}-delimited, not comma-delimited: the header splits "
                f"into {len(header.split(delimiter))} fields on a {name} and {commas} on a "
                "comma. Nothing was read. Re-export it with commas, or every row would be "
                "refused for every column."
            )


def check_header(columns: list[str]) -> None:
    """Every column known, every structurally required column present (SPEC §15.1).

    Required columns are derived from the model rather than listed here, so a field added
    to `ObservedFailure` cannot be forgotten by this check.
    """
    named = [column for column in columns if column]
    if not set(named) & set(EXTENDED_COLUMNS):
        raise FileRefusalError(
            "the first line carries no recognised column, so the file has no header. "
            "Nothing was read. Download the template from /recommend/template — a file "
            "whose header was stripped would otherwise be read as one row short."
        )
    unknown = sorted(set(named) - set(EXTENDED_COLUMNS))
    missing = sorted(required_columns() - set(named))
    if unknown or missing:
        parts = []
        if unknown:
            parts.append(f"unknown column(s) {unknown}")
        if missing:
            parts.append(f"required column(s) {missing} absent")
        raise FileRefusalError(
            f"the header does not match the expected columns: {'; '.join(parts)}. Nothing "
            "was read. The columns are those of /recommend/template; a mapping profile's "
            "`columns` block can rename a merchant's own headers onto them."
        )


def read_header(text: str, mapping: MerchantMapping | None) -> list[str]:
    """The header as canonical column names, after aliasing and before any row is parsed."""
    header = next(csv.reader([_header_line(text)]))
    stripped = [column.strip() for column in header]
    return mapping.rename_columns(stripped) if mapping is not None else stripped


def rewrite_header(text: str, columns: list[str]) -> str:
    """The file with its header replaced by the canonical one, so the row reader sees the
    aliasing that the header check already validated."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip():
            lines[index] = ",".join(columns)
            break
    return "\n".join(lines)


def prepare(body: bytes, mapping: MerchantMapping | None) -> str:
    """Every file-level check, in order, returning the text the row engine should read."""
    text = decode(body)
    check_delimiter(text)
    columns = read_header(text, mapping)
    check_header(columns)
    return rewrite_header(text, columns)


def check_has_rows(row_count: int) -> None:
    """SPEC §15.1: a header with no rows refuses.

    Answering 200 with zero rows treats "you sent nothing" as a successful answer about
    nothing, and an export that silently produced no rows is then discoverable only from a
    count nobody was looking at.
    """
    if row_count == 0:
        raise FileRefusalError(
            "the file has a valid header and no data rows. Nothing was answered — an "
            "export that produced no rows is reported rather than answered as an empty "
            "success."
        )
