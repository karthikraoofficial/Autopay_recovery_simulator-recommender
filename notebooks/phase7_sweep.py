"""Phase-7 deliverable: the sensitivity sweep (SPEC §5.1).

Exploration only, never imported by the package (SPEC §8).

    python notebooks/phase7_sweep.py [n_seeds] [months]

**Runs through `run_sweep`, deliberately.** The first attempt at this sweep imported
`run_sweep` and then called `Sweep.base_point` and `Sweep.sweep_key` directly in a loop,
which bypassed `UnderpoweredSweepError`. It ran for 3h12m over 103 keys against a base lift
of ₹1,096 [-₹212, ₹2,333] — an interval containing zero — and produced a fragility ranking
in which no key could ever be flagged fragile, because `is_fragile` is undefined when the
base is not significant. A guard that exists but is not on the path anyone takes is not a
guard. This script has no path that skips it.

**A shortlist, at full power, rather than every key at none.** SPEC §5.1 asks which
assumptions the conclusion is fragile to. A 103-key ranking computed around a null result
answers nothing; sixteen keys measured properly answers the question. Every key NOT swept
is listed in the output as untested, because "we did not test it" and "it did not matter"
must not look the same to a reader.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from rebound.config import Assumptions, load_assumptions
from rebound.harness.sensitivity import (
    Sweep,
    UnderpoweredSweepError,
    fragile_first,
    run_sweep,
    sweepable_keys,
)
from rebound.strategies.blended import Blended
from rebound.strategies.fixed_schedule import FixedSchedule

MASTER_SEED = 20260801
RUPEE = 100

# The load-bearing few, chosen from what phases 7, 8 and 8.5 already found to move the
# answer — not from this sweep's own output, which would be circular. Each line says why
# the key is here; a key nobody can justify in one line does not belong on the shortlist.
SHORTLIST: tuple[tuple[str, str], ...] = (
    # The single most consequential unmeasured number in the population model, by its own
    # note in assumptions.yaml: it alone sets the insufficient-funds rate.
    ("population.balance.cushion_lognormal_sigma", "sets the insufficient-funds rate"),
    ("population.balance.spend_decay_rate.low", "how fast the thinnest balances drain"),
    ("population.balance.spend_decay_rate.mid", "the same, for the largest income band"),
    # Phase 8: strategy lift swings ~17x across the failure-mix presets, which move these.
    ("population.bank.td_rate_max", "the technical-decline level the presets move"),
    ("engine.rail_technical_multiplier.upi_autopay", "per-rail failure, and rail mix drives lift"),
    ("engine.rail_technical_multiplier.enach", "the same, on the batch-cleared rail"),
    ("population.mandate.rail_mix.upi_autopay", "phase 8.5 found rail-specific winners"),
    # The cost side. SPEC §2.3: without these, retry-infinitely wins and the answer is wrong.
    ("engine.reaction.revocation_base_hazard", "the price of retry aggression"),
    ("engine.reaction.revocation_attempt_exponent", "whether an optimal retry count exists"),
    ("engine.reaction.topup_probability_notified", "all notification lift rests on this"),
    # The strategy's own content. A lift number for Blended is a claim about these.
    ("strategy.blended.weight_reason", "blend weight, none of them fitted"),
    ("strategy.blended.weight_salary", "phase 7: the inference behind it is weak"),
    ("strategy.blended.weight_bank", "blend weight, none of them fitted"),
    ("strategy.reason_aware.insufficient_funds_delay_days", "the branch SalaryAware must beat"),
    # Scheduler plumbing, which phase 7 found to be the larger line item.
    ("compliance.reschedule_horizon_days", "how long a blocked retry keeps looking"),
    ("book.avg_ticket_paise", "the largest swing in the first, void sweep"),
)


def build(a: Assumptions) -> list:
    """Two strategies, not seven: the sweep measures one comparison, and every extra
    strategy multiplies a runtime that is already hours."""
    return [FixedSchedule(a), Blended(a)]


def main(n_seeds: int, months: int) -> None:
    shipped = load_assumptions()
    # Full power, against the sensitivity keys sized_for_runtime reads. The first sweep ran
    # at 6 months, half the headline horizon, which halved the lift while leaving the
    # interval wide -- that is what made its base straddle zero.
    powered = shipped.with_values(
        {"harness.sensitivity.seeds": n_seeds, "harness.sensitivity.months": months}
    )
    sweep = Sweep.sized_for_runtime(powered, build, subject="Blended", master_seed=MASTER_SEED)
    keys = tuple(k for k, _ in SHORTLIST)
    every = set(sweepable_keys(sweep.assumptions))
    missing = [k for k in keys if k not in every]
    if missing:
        raise SystemExit(f"shortlist names keys the sweep cannot reach: {missing}")

    book = int(sweep.assumptions.value("book.size"))
    print(f"sweep: {len(keys)} keys, {2 * len(keys) + 1} experiments")
    print(f"book {book} x {months} months x {n_seeds} seeds, subject Blended vs FixedSchedule")
    print()

    started = time.time()
    try:
        results = run_sweep(sweep, keys)
    except UnderpoweredSweepError as exc:
        # The base effect and its interval, before any ranking. If the comparison being
        # swept is not itself significant there is nothing to be fragile about, and a
        # ranking printed here would be a list of the sign flips of noise.
        print("BASE EFFECT IS NOT SIGNIFICANT -- NO RANKING PRODUCED")
        print()
        print(exc)
        raise SystemExit(1) from exc

    base = results[0].base
    print("BASE EFFECT (before any fragility ranking)")
    print(
        f"  Blended - FixedSchedule, net to merchant per book: "
        f"[Rs {base.lift_low / RUPEE:,.0f}, Rs {base.lift_high / RUPEE:,.0f}] "
        f"(point Rs {base.lift_point / RUPEE:,.0f})"
    )
    print(f"  interval excludes zero: {base.interval_excludes_zero}  -- ranking below is valid")
    print(f"  elapsed {time.time() - started:.0f}s")
    print()
    _report(results, sweep.assumptions, every, keys)
    _write(results, sweep, n_seeds, months, book, every, keys)


def _report(results, assumptions: Assumptions, every: set[str], swept: tuple[str, ...]) -> None:
    factor = float(assumptions.value("harness.sensitivity.sweep_factor"))
    print(f"FRAGILITY RANKING (+/-{factor:.0%}, fragile first)")
    for result in fragile_first(results):
        flag = "FRAGILE" if result.is_fragile else "       "
        clamp = " [CLAMPED]" if any(p.clamped for p in result.points) else ""
        print(f"  {flag} {result.max_relative_swing:8.1%}  {result.key}{clamp}")

    clamped = [r.key for r in results if any(p.clamped for p in r.points)]
    if clamped:
        print()
        print("CLAMPED KEYS -- swing NOT comparable to the rest")
        print("  These were swept over a narrowed range because the full +/-50% would put")
        print("  the value outside the domain it describes. A small swing here is not")
        print("  evidence of robustness; it is a smaller question having been asked.")
        for key in clamped:
            print(f"    {key}")

    untested = sorted(every - set(swept))
    print()
    print(f"NOT TESTED AT THIS POWER ({len(untested)} of {len(every)} sweepable keys)")
    print("  Not robust, not omitted -- untested. These were left out to spend the runtime")
    print("  on the shortlist instead, and nothing here should be read as evidence either")
    print("  way about them.")
    for key in untested:
        print(f"    {key}")


def _write(results, sweep: Sweep, n_seeds: int, months: int, book: int, every, swept) -> None:
    out = Path("notebooks/phase7_sweep.json")
    out.write_text(
        json.dumps(
            {
                "master_seed": MASTER_SEED,
                "n_seeds": n_seeds,
                "months": months,
                "book_size": book,
                "subject": sweep.subject,
                "metric": sweep.metric,
                "base": results[0].base.model_dump(mode="json"),
                "swept_keys": list(swept),
                "untested_keys": sorted(set(every) - set(swept)),
                "clamped_keys": [
                    r.key for r in results if any(p.clamped for p in r.points)
                ],
                "results": [r.model_dump(mode="json") for r in fragile_first(results)],
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    print()
    print(f"wrote {out}")


if __name__ == "__main__":
    main(
        int(sys.argv[1]) if len(sys.argv) > 1 else 24,
        int(sys.argv[2]) if len(sys.argv) > 2 else 12,
    )
