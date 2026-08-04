"""SPEC §14.6: the measured lift this service is allowed to quote, and where it comes from.

**The evidence is the phase 8.5 segment report (SPEC §12), not the phase 7 sensitivity
sweep (SPEC §11).** The sweep measures how fragile the headline is to a ±50% move in an
assumption; it produces no per-segment figure and cannot answer "what is this worth here".
The two were conflated once during phase 10 planning and the correction is recorded in
SPEC §14.6 so the wrong artefact is not reached for again.

Nothing here is recomputed. The report is read as it was written, and refused outright if
it was written under a different configuration than the rules being applied.
"""

from __future__ import annotations

import json
from pathlib import Path

from rebound.api.inputs import MerchantProfile, Sizing, sizing_plan
from rebound.config import Assumptions

# Private import on purpose. The band label this service reports has to be byte-identical
# to the one the report was cut on, and a second copy of the banding rule here would be
# free to drift from it.
from rebound.harness.segments import (
    NEGATIVE_CONTROL,
    Dimension,
    Segment,
    SegmentReport,
    Verdict,
    _frequency_band,  # noqa: PLC2701
)
from rebound.recommend.inputs import ObservedFailure

PAISE_PER_RUPEE = 100

EVIDENCE_PATH = Path(__file__).resolve().parents[3] / "notebooks" / "phase85_result.json"

# SPEC §14.6. Reason code leads because it is the failure's own attribute and SPEC §0.4
# makes reason-code branching the thesis of the project; failure frequency trails because
# it describes the mandate's past rather than this failure. The negative control (mandate
# cap band, SPEC §12.5/§14.8) is absent and must stay absent.
PRECEDENCE: tuple[Dimension, ...] = (
    Dimension.DOMINANT_REASON,
    Dimension.RAIL,
    Dimension.FAILURE_FREQUENCY,
)


class EvidenceUnavailableError(RuntimeError):
    """The recorded measurement cannot be trusted for the configuration in force."""


def expected_fingerprint(assumptions: Assumptions) -> str:
    """The configuration the evidence run was made under, rebuilt from shipped values.

    Mirrors `notebooks/phase85_segments.py`, which is the script that produces the
    artefact: shipped assumptions, the evidence sizing, and a merchant profile whose every
    field is taken from the shipped values. The float arithmetic in `MerchantProfile`
    matters — the card share is a computed remainder — so the profile is built through the
    same public API rather than by writing the resulting numbers down here.

    A test asserts this equals the recorded artefact's fingerprint, so a change to either
    side is caught by the suite rather than at request time.
    """
    book_size = int(assumptions.value("recommend.evidence.book_size"))
    seeds = int(assumptions.value("recommend.evidence.seeds"))
    configured = assumptions.with_values(
        {"api.interactive_seeds": seeds, "api.interactive_book_size": book_size}
    )
    profile = MerchantProfile(
        book_size=book_size,
        avg_ticket_inr=float(assumptions.value("book.avg_ticket_paise")) / PAISE_PER_RUPEE,
        upi_autopay_share=float(assumptions.value("population.mandate.rail_mix.upi_autopay")),
        enach_share=float(assumptions.value("population.mandate.rail_mix.enach")),
        performance_fee_rate=float(assumptions.value("harness.performance_fee_rate")),
    )
    plan = sizing_plan(configured, Sizing.INTERACTIVE, book_size)
    return profile.configure(configured, plan).fingerprint()


def load_evidence(assumptions: Assumptions, path: Path | None = None) -> SegmentReport:
    """The recorded segment report, refused if it was measured under other assumptions.

    Fail-closed, as `/segments` and `/trace` are. Lift measured under a different
    configuration than the rules being applied is not evidence about the recommendation
    being made, and the mismatch is invisible in the numbers themselves.
    """
    path = path or EVIDENCE_PATH
    if not path.exists():
        raise EvidenceUnavailableError(f"no evidence artefact at {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if "config_fingerprint" not in raw:
        raise EvidenceUnavailableError(
            f"{path.name} carries no config_fingerprint, so the configuration it was "
            "measured under cannot be established. Regenerate it with "
            "notebooks/phase85_segments.py."
        )
    report = SegmentReport.model_validate(raw)
    expected = expected_fingerprint(assumptions)
    if report.config_fingerprint != expected:
        raise EvidenceUnavailableError(
            f"{path.name} was measured under config {report.config_fingerprint[:12]}; "
            f"the running configuration is {expected[:12]}. Nothing is quoted. "
            "Regenerate the evidence with notebooks/phase85_segments.py."
        )
    return report


def opening_reason_label(failure: ObservedFailure) -> str | None:
    """The cycle's opening reason code, or None when the input cannot establish it.

    SPEC §12.1 defines the dominant reason as the mode over a mandate's opening attempts
    across the horizon; one failure is not a mode, so this is a one-cycle approximation and
    every response says so. Where the failure is itself a retry and the merchant did not
    itemise the earlier attempts, the opening code is simply unknown — the dimension goes
    unassigned rather than borrowing this failure's code, which would make the quoted lift
    a function of our guess.
    """
    if failure.attempt_history:
        return failure.attempt_history[0].reason_code.value  # type: ignore[union-attr]
    if failure.attempt_number == 1:
        return failure.reason_code.value
    return None


def segment_labels(failure: ObservedFailure, assumptions: Assumptions) -> dict[Dimension, str]:
    """Where this failure sits on each dimension. Mandate cap band is deliberately absent."""
    edges = [int(e) for e in assumptions.value("segment.failure_frequency_band_edges")]
    labels: dict[Dimension, str] = {
        Dimension.RAIL: failure.rail.value,
        Dimension.FAILURE_FREQUENCY: _frequency_band(failure.prior_failure_count + 1, edges),
    }
    opening = opening_reason_label(failure)
    if opening is not None:
        labels[Dimension.DOMINANT_REASON] = opening
    return labels


def find_segment(report: SegmentReport, dimension: Dimension, label: str) -> Segment | None:
    if dimension is NEGATIVE_CONTROL:
        raise ValueError(
            "SPEC §14.8: the mandate cap band is a negative control and is never quoted"
        )
    return next(
        (s for s in report.segments if s.dimension is dimension and s.label == label),
        None,
    )


def winning_strategy(segment: Segment) -> str | None:
    """The strategy that beat the baseline here and survived correction, or None."""
    if segment.verdict is not Verdict.WINNER or segment.strategy_lift is None:
        return None
    return segment.strategy_lift.strategy
