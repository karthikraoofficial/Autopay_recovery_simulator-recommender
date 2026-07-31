"""Capture the baseline strategy set under a named engine, for before/after comparison.

Exploration only, never imported by the package (SPEC §8). Usage:

    python notebooks/capture_baseline.py scripted notebooks/phase2_scripted_baseline.json
    python notebooks/capture_baseline.py failure  notebooks/phase4_failure_engine.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from rebound.config import load_assumptions
from rebound.engine.failure import FailureEngine
from rebound.harness.runner import run_experiment
from rebound.harness.stub_engine import ScriptedEngine
from rebound.strategies.fixed_schedule import FixedSchedule
from rebound.strategies.no_retry import NoRetry

ENGINES = {"scripted": ScriptedEngine, "failure": FailureEngine}
MASTER_SEED = 20260731
N_SEEDS = 8


def main(engine_name: str, out_path: str) -> None:
    assumptions = load_assumptions()
    report = run_experiment(
        [NoRetry(), FixedSchedule(assumptions)],
        MASTER_SEED,
        assumptions,
        n_seeds=N_SEEDS,
        engine_factory=ENGINES[engine_name],
    )
    payload = {
        "engine": engine_name,
        "master_seed": MASTER_SEED,
        "n_seeds": N_SEEDS,
        "output_hash": report.output_hash,
        "aggregate": [m.model_dump(mode="json") for m in report.aggregate],
        "intervals": [i.model_dump(mode="json") for i in report.intervals],
    }
    Path(out_path).write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
