"""SPEC §15.3: a merchant's own vocabulary, stored under review rather than uploaded.

A mapping supplied with each upload is unversioned by construction — the same file answers
differently on two days and nothing records what moved. So profiles live in
`config/merchants/` and are named in the request, and each carries a fingerprint over its
semantic content so a caller can be *told* when it changed (SPEC §15.4).

**Unmapped values refuse.** No case-folding fallback, no fuzzy match, no passthrough. A
mapping that quietly resolved an unknown code to something near it would be inventing the
merchant's data, and the recommendation built on it would look exactly like a real one.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from rebound.domain.entities import Rail
from rebound.domain.reason_codes import ReasonCode
from rebound.recommend.inputs import EXTENDED_COLUMNS

MERCHANTS_DIR = Path(__file__).resolve().parents[3] / "config" / "merchants"

# The columns a `datetime_format` applies to. Named here rather than sniffed, so a profile
# cannot silently start reformatting a column that was never a datetime.
DATETIME_COLUMNS = ("failed_at", "notified_at", "original_attempt_at")


class MappingError(ValueError):
    """The profile itself is unusable — absent, malformed, or naming a code that does not
    exist. Raised when the profile is used, not when it is written, so a broken mapping
    cannot sit unnoticed in the directory."""


class UnmappedValueError(ValueError):
    """A value this profile does not know. A row-level refusal, never a guess."""

    def __init__(self, column: str, value: str, profile: str) -> None:
        super().__init__(
            f"{column}: {value!r} is not in the '{profile}' mapping. Add it to the profile "
            "rather than editing the file; an unmapped value is refused rather than guessed."
        )
        self.column = column
        self.value = value


class MerchantMapping(BaseModel):
    """One merchant's vocabulary. `description` is provenance and is not fingerprinted."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    description: str | None = None
    reason_codes: dict[str, ReasonCode] = Field(default_factory=dict)
    rails: dict[str, Rail] = Field(default_factory=dict)
    columns: dict[str, str] = Field(default_factory=dict)
    datetime_format: str | None = None
    # Offset in hours, applied to datetimes that carry none. SPEC §14.2 accepts only aware
    # datetimes, and an invented offset moves the recommendation by hours.
    timezone_offset_hours: float | None = None

    @model_validator(mode="after")
    def aliases_target_real_columns(self) -> MerchantMapping:
        unknown = sorted(set(self.columns.values()) - set(EXTENDED_COLUMNS))
        if unknown:
            raise ValueError(
                f"columns maps to {unknown}, which are not columns of the upload. "
                "The canonical columns are defined in recommend/inputs.py (SPEC §14.9)."
            )
        return self

    def fingerprint(self) -> str:
        """A digest over what the profile *does*, never over how it is described.

        Symmetric with `Assumptions.fingerprint()`, which excludes sources and notes for the
        same reason: editing a comment must not invalidate a comparison a caller is relying
        on, or the comparison becomes noise and stops being read.
        """
        canonical = json.dumps(
            {
                "name": self.name,
                "reason_codes": {k: v.value for k, v in sorted(self.reason_codes.items())},
                "rails": {k: v.value for k, v in sorted(self.rails.items())},
                "columns": dict(sorted(self.columns.items())),
                "datetime_format": self.datetime_format,
                "timezone_offset_hours": self.timezone_offset_hours,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def rename_columns(self, header: list[str]) -> list[str]:
        """Applied before the header check, so a merchant sends their own export headers."""
        return [self.columns.get(column, column) for column in header]

    def apply(self, row: dict[str, str]) -> dict[str, str]:
        """The row in canonical vocabulary. Raises `UnmappedValueError` on a value it does
        not know — which is a refusal for that row, not for the file."""
        mapped = dict(row)
        if "reason_code" in mapped:
            mapped["reason_code"] = self._lookup(
                "reason_code", mapped["reason_code"], self.reason_codes, ReasonCode
            )
        if "rail" in mapped:
            mapped["rail"] = self._lookup("rail", mapped["rail"], self.rails, Rail)
        for column in DATETIME_COLUMNS:
            if column in mapped:
                mapped[column] = self._normalise_datetime(column, mapped[column])
        return mapped

    def _lookup(
        self,
        column: str,
        value: str,
        table: dict[str, ReasonCode] | dict[str, Rail],
        enum: type[ReasonCode] | type[Rail],
    ) -> str:
        """Exact lookup, then the canonical value itself. Nothing else.

        A merchant whose export already carries canonical codes needs no entry per code,
        but a *near* match is never resolved: `U31` does not become whatever `U30` meant.
        """
        if value in table:
            return table[value].value
        if value in {member.value for member in enum}:
            return value
        raise UnmappedValueError(column, value, self.name)

    def _normalise_datetime(self, column: str, value: str) -> str:
        if self.datetime_format is None:
            return value
        try:
            parsed = datetime.strptime(value, self.datetime_format)
        except ValueError as exc:
            raise UnmappedValueError(column, value, self.name) from exc
        if parsed.tzinfo is None:
            if self.timezone_offset_hours is None:
                raise MappingError(
                    f"profile '{self.name}' parses {column} to a naive datetime and "
                    "declares no timezone_offset_hours. SPEC §14.2 accepts only aware "
                    "datetimes, and guessing an offset would move the recommendation."
                )
            parsed = parsed.replace(
                tzinfo=timezone(timedelta(hours=self.timezone_offset_hours))
            )
        return parsed.isoformat()


def profile_path(name: str, directory: Path | None = None) -> Path:
    """Resolved inside the merchants directory, and refused if it escapes.

    The name arrives in a request, so `../../etc/passwd` has to be a refusal rather than a
    file read.
    """
    directory = (directory or MERCHANTS_DIR).resolve()
    candidate = (directory / f"{name}.yaml").resolve()
    if candidate.parent != directory:
        raise MappingError(f"mapping profile name {name!r} is not a plain profile name")
    return candidate


def load_mapping(name: str, directory: Path | None = None) -> MerchantMapping:
    path = profile_path(name, directory)
    if not path.exists():
        raise MappingError(
            f"no mapping profile '{name}' in {(directory or MERCHANTS_DIR).name}/. "
            f"Available: {', '.join(available_mappings(directory)) or 'none'}."
        )
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return MerchantMapping.model_validate({"name": name, **raw})
    except (yaml.YAMLError, ValueError) as exc:
        raise MappingError(f"mapping profile '{name}' is unusable: {exc}") from exc


def available_mappings(directory: Path | None = None) -> tuple[str, ...]:
    directory = directory or MERCHANTS_DIR
    if not directory.exists():
        return ()
    return tuple(sorted(path.stem for path in directory.glob("*.yaml")))
