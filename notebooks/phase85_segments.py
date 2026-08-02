"""Phase-8.5 result: the segment report, rendered.

Exploration only, never imported by the package (SPEC §8).

    python notebooks/phase85_segments.py [book_size] [n_seeds]

Prints SPEC §12's plain-text report and writes the JSON beside it. Runs one experiment
with a `SegmentCollector` attached, so the segments are cut from exactly the run that
produced them — nothing here re-simulates.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from rebound.api.inputs import MerchantProfile, Sizing
from rebound.api.service import segment_report
from rebound.config import load_assumptions
from rebound.harness.segments import Verdict, render

MASTER_SEED = 20260801
RUPEE = 100


def main(book_size: int, n_seeds: int) -> None:
    shipped = load_assumptions()
    configured = shipped.with_values(
        {"api.interactive_seeds": n_seeds, "api.interactive_book_size": book_size}
    )
    profile = MerchantProfile(
        book_size=book_size,
        avg_ticket_inr=float(shipped.value("book.avg_ticket_paise")) / RUPEE,
        upi_autopay_share=float(shipped.value("population.mandate.rail_mix.upi_autopay")),
        enach_share=float(shipped.value("population.mandate.rail_mix.enach")),
        performance_fee_rate=float(shipped.value("harness.performance_fee_rate")),
    )
    report = segment_report(profile, MASTER_SEED, Sizing.INTERACTIVE, configured)
    print(render(report))

    # The control's own summary, printed after the report so it is read last: whether the
    # correction held is the first thing to check before believing anything above it.
    control_winners = [s.label for s in report.negative_control if s.verdict is Verdict.WINNER]
    tested = [s for s in report.negative_control if s.verdict is not Verdict.INSUFFICIENT_DATA]
    print()
    print("=" * 96)
    if control_winners:
        print(f"CONTROL FAILED: {control_winners} won on an axis with no signal.")
        print("The correction is manufacturing findings. Do not believe anything above.")
    elif not tested:
        print("CONTROL INCONCLUSIVE: every control segment was too small to test.")
        print("Raise book_size or n_seeds before reading the findings.")
    else:
        print(f"Control held: {len(tested)} of {len(report.negative_control)} bands testable, "
              f"none won.")

    out = Path("notebooks/phase85_result.json")
    out.write_text(json.dumps(report.model_dump(mode="json"), indent=2), encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main(
        int(sys.argv[1]) if len(sys.argv) > 1 else 400,
        int(sys.argv[2]) if len(sys.argv) > 2 else 8,
    )
