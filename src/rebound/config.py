from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

DEFAULT_ASSUMPTIONS_PATH = Path(__file__).resolve().parents[2] / "config" / "assumptions.yaml"


class Confidence(StrEnum):
    PRIMARY = "primary"
    PRACTITIONER = "practitioner"
    ESTIMATE = "estimate"


class Assumption(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # Lists are permitted so an ordered schedule (retry offsets) stays one auditable
    # entry with one source, rather than being smeared across numbered keys.
    value: float | int | str | bool | list[int] | list[float]
    unit: str
    source: str
    confidence: Confidence
    notes: str | None = None


class OpenQuestion(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str | None = None
    question: str


class Assumptions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int
    assumptions: dict[str, Assumption]
    open_questions: list[OpenQuestion]

    @model_validator(mode="after")
    def estimates_must_be_open_questions(self) -> Assumptions:
        """SPEC §0.1: an unsourced number is only acceptable if it is also admitted to."""
        listed = {q.key for q in self.open_questions if q.key is not None}
        unlisted = sorted(
            key
            for key, a in self.assumptions.items()
            if a.confidence is Confidence.ESTIMATE and key not in listed
        )
        if unlisted:
            raise ValueError(f"confidence: estimate but not in open_questions: {unlisted}")
        return self

    def value(self, key: str) -> Any:
        try:
            return self.assumptions[key].value
        except KeyError:
            raise KeyError(f"no assumption {key!r} in assumptions.yaml") from None

    def fingerprint(self) -> str:
        """A digest of every assumption value, for comparing two runs' configurations.

        Exists because a mismatch between the headline run and a derived export is silent
        by nature: the same seed produces the same mandate ids whatever the config, so a
        trace generated under different assumptions looks entirely plausible. Comparing
        output hashes does not help — a single-seed trace has a different hash by
        construction — so the *input* has to be compared instead.

        Values only. Sources and notes are provenance for the reader and do not change
        what the simulation does, so editing a note must not invalidate an export.
        """
        canonical = json.dumps(
            {key: entry.value for key, entry in sorted(self.assumptions.items())},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def with_values(self, values: dict[str, Any]) -> Assumptions:
        """A copy with some values replaced, keeping each entry's source and confidence.

        The API's merchant inputs land here. Replacing the value and not the provenance is
        deliberate: a book size the caller supplied is still governed by the note on
        `book.size`, and the dashboard renders that note beside the number.

        Only existing keys may be set. A typo would otherwise add an assumption nothing
        reads and report the run as if the input had been applied.
        """
        entries = dict(self.assumptions)
        for key, value in values.items():
            if key not in entries:
                raise KeyError(f"no assumption {key!r} in assumptions.yaml")
            entries[key] = entries[key].model_copy(update={"value": value})
        return self.model_copy(update={"assumptions": entries})


def load_assumptions(path: Path | None = None) -> Assumptions:
    path = path or DEFAULT_ASSUMPTIONS_PATH
    with path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return Assumptions.model_validate(raw)
