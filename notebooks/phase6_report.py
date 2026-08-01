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
        # intervals_for returns BOTH the absolute interval and the paired difference
        # against the baseline. Keying on metric alone silently lets the difference
        # overwrite the absolute, which reads as a strategy with a negative recovery
        # rate. They are different quantities and are kept apart.
        absolute = {i.metric: i for i in report.intervals_for(name) if i.vs_baseline is None}
        paired = {i.metric: i for i in report.intervals_for(name) if i.vs_baseline is not None}

        def band(metric: str, source: dict, money: bool = False) -> str:
            i = source.get(metric)
            if i is None:
                return "n/a"
            return (
                f"[{rupees(i.low)}, {rupees(i.high)}]"
                if money
                else f"[{i.low:,.4f}, {i.high:,.4f}]"
            )

        print(f"=== {name}")
        # Every interval below is per seeded book, and so is the point beside it. The
        # aggregate counters are 12-seed totals; printing a total point against a
        # per-book interval would overstate the number by a factor of n_seeds.
        for label, metric, money in (
            ("recovery rate", "recovery_rate", False),
            ("net to merchant/book", "net_recovered_paise", True),
            ("gross recovered/book", "gross_recovered_paise", True),
            ("induced revocations", "induced_revocations", False),
        ):
            i = absolute.get(metric)
            point = "n/a"
            if i is not None:
                point = rupees(i.point) if money else f"{i.point:,.4f}"
            print(f"    {label:20s} CI {band(metric, absolute, money)}   point {point}")
        print(
            f"    compliance blocks    (counter, no CI)                point {m.compliance_blocks}"
        )
        print(
            f"    hard-decline stops   (counter, no CI)                "
            f"point {m.terminated_hard_decline}"
        )
        print(
            f"    12-seed totals: episodes {m.episodes}  attempts {m.total_attempts}  "
            f"retries {m.retry_attempts}  net {rupees(m.net_recovered_paise)}"
        )
        if paired:
            print(f"    --- paired lift vs {a.value('harness.baseline_strategy')}")
            for metric in ("recovery_rate", "net_recovered_paise", "induced_revocations"):
                i = paired.get(metric)
                if i is None:
                    continue
                money = metric.endswith("_paise")
                point = rupees(i.point) if money else f"{i.point:,.4f}"
                beats = "excludes 0" if (i.low > 0 or i.high < 0) else "SPANS 0"
                print(
                    f"        {metric:22s} CI {band(metric, paired, money)}"
                    f"   point {point}   ({beats})"
                )
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
