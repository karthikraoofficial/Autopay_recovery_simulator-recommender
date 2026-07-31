from __future__ import annotations

from typing import Any

import pytest

from rebound.config import Assumptions, load_assumptions


def override(assumptions: Assumptions, values: dict[str, Any]) -> Assumptions:
    """A copy of the shipped assumptions with some values replaced, so tests can shrink
    the book without a second YAML file drifting out of sync with the real one."""
    entries = dict(assumptions.assumptions)
    for key, value in values.items():
        entries[key] = entries[key].model_copy(update={"value": value})
    return assumptions.model_copy(update={"assumptions": entries})


@pytest.fixture(scope="session")
def shipped() -> Assumptions:
    return load_assumptions()


@pytest.fixture(scope="session")
def small(shipped: Assumptions) -> Assumptions:
    """A book small enough to run many times in a test suite. Shape is unchanged."""
    return override(shipped, {"book.size": 120, "book.months": 6, "population.bank.count": 4})


@pytest.fixture(scope="session")
def wide(shipped: Assumptions) -> Assumptions:
    """A book wide enough that a bucketed probability is a measurement rather than
    noise. Used by the population tests, which assert distribution shape."""
    return override(shipped, {"book.size": 1500})
