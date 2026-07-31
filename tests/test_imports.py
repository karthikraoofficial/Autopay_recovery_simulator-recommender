"""Import-graph tests.

Phase 4 briefly created a cycle — population imported harness for the seeding
primitive, harness imported engine for the payment protocol, and engine imported
population for the balance process. The package then imported cleanly or not depending
purely on which module a caller happened to reach first, which is the kind of failure
that shows up in the phase-8 API and nowhere earlier.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

# Every public subpackage, each as the very first rebound import in a fresh interpreter.
ENTRY_POINTS = [
    "rebound.config",
    "rebound.seeding",
    "rebound.domain",
    "rebound.population",
    "rebound.population.balance",
    "rebound.population.book",
    "rebound.engine",
    "rebound.engine.failure",
    "rebound.engine.calibration",
    "rebound.harness",
    "rebound.harness.runner",
    "rebound.strategies.fixed_schedule",
]


@pytest.mark.parametrize("module", ENTRY_POINTS)
def test_module_imports_first_in_a_fresh_interpreter(module: str) -> None:
    """A cycle makes importing one module first work and another fail, so each has to be
    checked in its own process rather than after the suite has already imported half the
    package."""
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"importing {module} first failed:\n{result.stderr}"


def test_seeding_does_not_depend_on_any_rebound_subpackage() -> None:
    """The seeding primitive is used by population, engine and harness alike. It has to
    sit below all three or it reintroduces the cycle."""
    source = (
        subprocess.run(
            [
                sys.executable,
                "-c",
                "import inspect, rebound.seeding as s; print(inspect.getsource(s))",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    ).stdout
    assert "from rebound." not in source
    assert "import rebound" not in source
