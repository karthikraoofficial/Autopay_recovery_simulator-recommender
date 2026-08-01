"""Phase-6 result: NoRetry vs FixedSchedule vs ReasonAware, paired over N seeds.

Exploration only, never imported by the package (SPEC §8).

    python notebooks/phase6_report.py [n_seeds]

Intervals are printed before point estimates throughout, per CLAUDE.md: a point
estimate read first anchors the reader, and these intervals are wide enough that the
anchoring would be misleading.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from rebound.config import load_assumptions
from rebound.harness.runner import run_experiment
from rebound.strategies.fixed_schedule import FixedSchedule
from rebound.strategies.no_retry import NoRetry
from rebound.strategies.reason_aware import ReasonAware

MASTER_SEED = 20260801
RUPEE = 100


def rupees(paise: float) -> str:
    return f"Rs {paise / RUPEE:,.0f}"


def main(n_seeds: int) -> None:
    a = load_assumptions()
    fee = float(a.value("harness.performance_fee_rate"))
    level = float(a.value("harness.confidence_level"))
    inherits = bool(a.value("compliance.pre_debit_notification.retry_inherits_original_notice"))
    names = ["NoRetry", "FixedSchedule", "ReasonAware"]
    report = run_experiment(
        [NoRetry(), FixedSchedule(a), ReasonAware(a)], MASTER_SEED, a, n_seeds=n_seeds
    )

    print(f"seeds={n_seeds}  master_seed={MASTER_SEED}  CI level={level:.0%}  fee={fee:.0%}")
    print(f"notice inherited by retries: {inherits}")
    print(f"output_hash={report.output_hash[:16]}")
    print()
    for name in names:
        m = report.for_strategy(name)
        ci = {i.metric: i for i in report.intervals_for(name)}

        def band(metric: str) -> str:
            i = ci.get(metric)
            return "n/a" if i is None else f"[{i.low:,.4f}, {i.high:,.4f}]"

        def band_rs(metric: str) -> str:
            i = ci.get(metric)
            return "n/a" if i is None else f"[{rupees(i.low)}, {rupees(i.high)}]"

        print(f"=== {name}")
        print(f"    recovery rate        CI {band('recovery_rate')}   point {m.recovery_rate:.4f}")
        print(
            f"    net to merchant      CI {band_rs('net_recovered_paise')}"
            f"   point {rupees(m.net_recovered_paise)}"
        )
        print(
            f"    gross recovered      CI {band_rs('gross_recovered_paise')}"
            f"   point {rupees(m.gross_recovered_paise)}"
        )
        print(
            f"    induced revocations  CI {band('induced_revocations')}"
            f"   point {m.induced_revocations}"
        )
        print(f"    compliance blocks    (counter)                      point {m.compliance_blocks}")
        print(
            f"    hard-decline stops   (counter)                      "
            f"point {m.terminated_hard_decline}"
        )
        print(f"    episodes {m.episodes}  attempts {m.total_attempts}  retries {m.retry_attempts}")
        print()

    payload = {
        "master_seed": MASTER_SEED,
        "n_seeds": n_seeds,
        "confidence_level": level,
        "retry_inherits_original_notice": inherits,
        "output_hash": report.output_hash,
        "aggregate": [m.model_dump(mode="json") for m in report.aggregate],
        "intervals": [i.model_dump(mode="json") for i in report.intervals],
    }
    out = Path("notebooks/phase6_result.json")
    out.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 12)
