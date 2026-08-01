"""Phase-8 result: what each dashboard failure-mix preset actually produces.

Exploration only, never imported by the package (SPEC §8).

    python notebooks/phase8_presets.py [n_seeds]

Two measurements per preset, neither of them fitted:

1. The reason-code mix the engine produces under it, via `engine/calibration.py`. The
   presets are named for the mix they are meant to produce; this is where that name is
   checked rather than asserted. A preset whose observed mix does not match its label
   would put a lie in the dashboard's dropdown.
2. The headline decomposition under it. This is the phase-8 finding: the rescheduling
   line item survives every preset, and the strategy line item does not.

Intervals before point estimates throughout, per CLAUDE.md.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from rebound.api.inputs import FailureMix, MerchantProfile, Sizing, sizing_plan
from rebound.config import load_assumptions
from rebound.domain.reason_codes import ReasonCode
from rebound.engine.calibration import calibrate
from rebound.harness.runner import default_strategies, run_experiment

MASTER_SEED = 20260801
RUPEE = 100
# A flat target. `calibrate` measures the distance to whatever it is given; here only the
# observed shares are wanted, so the target is deliberately uninformative rather than a
# claim about any merchant's book.
FLAT_TARGET = {code: 1.0 / len(ReasonCode) for code in ReasonCode}


def rupees(paise: float) -> str:
    return f"Rs {paise / RUPEE:,.0f}"


def main(n_seeds: int) -> None:
    shipped = load_assumptions()
    profile = MerchantProfile(
        book_size=int(shipped.value("harness.sensitivity.book_size")),
        avg_ticket_inr=float(shipped.value("book.avg_ticket_paise")) / RUPEE,
        upi_autopay_share=float(shipped.value("population.mandate.rail_mix.upi_autopay")),
        enach_share=float(shipped.value("population.mandate.rail_mix.enach")),
        performance_fee_rate=float(shipped.value("harness.performance_fee_rate")),
    )
    plan = sizing_plan(shipped, Sizing.PUBLICATION, profile.book_size)
    print(f"book={plan.book_size}  seeds={n_seeds}  master_seed={MASTER_SEED}")
    print(f"CI level={shipped.value('harness.confidence_level'):.0%}\n")

    payload = {}
    for mix in FailureMix:
        configured = profile.model_copy(update={"failure_mix": mix}).configure(shipped, plan)
        report = calibrate(configured, FLAT_TARGET, seeds=(MASTER_SEED, MASTER_SEED + 1))
        shares = {
            g.code.value: round(g.observed_share, 4)
            for g in sorted(report.gaps, key=lambda g: -g.observed_share)
            if g.observed_share > 0.0
        }
        experiment = run_experiment(
            default_strategies(configured), MASTER_SEED, configured, n_seeds=n_seeds
        )
        decomposition = experiment.decomposition("net_recovered_paise", configured)
        assert decomposition is not None

        print(f"=== {mix.value}")
        print(f"    first-attempt failure rate {report.failure_rate:.4f}")
        print("    observed reason-code mix:")
        for code, share in shares.items():
            print(f"        {code:26s} {share:.4f}")
        print(f"    {decomposition}")
        for name, line in (
            ("rescheduling", decomposition.rescheduling),
            ("strategy", decomposition.strategy),
        ):
            significant = line.low > 0 or line.high < 0
            verdict = "excludes 0" if significant else "SPANS 0 (not significant)"
            print(
                f"    {name:13s} net/book "
                f"[{rupees(line.low)}, {rupees(line.high)}] -> {verdict}"
            )
        print()

        payload[mix.value] = {
            "failure_rate": report.failure_rate,
            "reason_code_mix": shares,
            "decomposition": decomposition.model_dump(mode="json"),
            "output_hash": experiment.output_hash,
        }

    out = Path("notebooks/phase8_result.json")
    out.write_text(
        json.dumps(
            {
                "master_seed": MASTER_SEED,
                "n_seeds": n_seeds,
                "book_size": plan.book_size,
                **payload,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    print(f"wrote {out}")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 12)
