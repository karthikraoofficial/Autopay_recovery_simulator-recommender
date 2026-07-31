from __future__ import annotations

import pytest

from rebound.config import Assumptions
from rebound.harness.runner import run_experiment, run_paired
from rebound.harness.seeding import stream

# These are harness unit tests: they test the runner, not the engine, so they pin the
# deterministic phase-2 ScriptedEngine explicitly rather than following the default.
# Integration-level runs take whatever the default engine is.
from rebound.harness.stub_engine import ScriptedEngine
from rebound.population.book import generate_book
from rebound.strategies.fixed_schedule import FixedSchedule
from rebound.strategies.no_retry import NoRetry

SEED = 20260731


def strategies(assumptions: Assumptions):  # noqa: ANN201
    return [NoRetry(), FixedSchedule(assumptions)]


# --- the seeding primitive ----------------------------------------------------


def test_stream_is_addressed_by_label_not_by_draw_order() -> None:
    first = stream(SEED, "retry", "mandate-1", "2026-03-04").random(5)
    _ = stream(SEED, "something", "else").random(100)
    second = stream(SEED, "retry", "mandate-1", "2026-03-04").random(5)
    assert (first == second).all()


def test_different_labels_give_different_streams() -> None:
    a = stream(SEED, "retry", "mandate-1", "2026-03-04").random(5)
    b = stream(SEED, "retry", "mandate-1", "2026-03-05").random(5)
    assert not (a == b).all()


def test_stream_needs_a_label() -> None:
    with pytest.raises(ValueError, match="at least one label"):
        stream(SEED)


# --- the book -----------------------------------------------------------------


def test_same_seed_gives_an_identical_book(small: Assumptions) -> None:
    first = generate_book(small, seed=SEED)
    second = generate_book(small, seed=SEED)
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_different_seed_gives_a_different_book(small: Assumptions) -> None:
    first = generate_book(small, seed=SEED)
    second = generate_book(small, seed=SEED + 1)
    assert first.model_dump(mode="json") != second.model_dump(mode="json")


# --- the run ------------------------------------------------------------------


def test_same_seed_gives_an_identical_output_hash(small: Assumptions) -> None:
    first = run_paired(
        generate_book(small, SEED), strategies(small), SEED, small, engine_factory=ScriptedEngine
    )
    second = run_paired(
        generate_book(small, SEED), strategies(small), SEED, small, engine_factory=ScriptedEngine
    )
    assert first.output_hash == second.output_hash


def test_different_seed_gives_a_different_output_hash(small: Assumptions) -> None:
    first = run_paired(
        generate_book(small, SEED), strategies(small), SEED, small, engine_factory=ScriptedEngine
    )
    second = run_paired(
        generate_book(small, SEED + 1),
        strategies(small),
        SEED + 1,
        small,
        engine_factory=ScriptedEngine,
    )
    assert first.output_hash != second.output_hash


def test_strategy_order_does_not_change_any_strategy_result(small: Assumptions) -> None:
    """A strategy's stream is addressed by its own labels, so it cannot inherit state
    from whatever ran before it."""
    forward = run_paired(
        generate_book(small, SEED),
        [NoRetry(), FixedSchedule(small)],
        SEED,
        small,
        engine_factory=ScriptedEngine,
    )
    reverse = run_paired(
        generate_book(small, SEED),
        [FixedSchedule(small), NoRetry()],
        SEED,
        small,
        engine_factory=ScriptedEngine,
    )
    for name in ("NoRetry", "FixedSchedule"):
        assert forward.for_strategy(name) == reverse.for_strategy(name)


def test_running_does_not_mutate_the_input_book(small: Assumptions) -> None:
    """SPEC §5.1: each strategy gets a deep copy. If the book were shared, the second
    strategy would inherit the first's revocations and the comparison would be rigged."""
    book = generate_book(small, SEED)
    before = book.model_dump(mode="json")
    run_paired(book, strategies(small), SEED, small, engine_factory=ScriptedEngine)
    assert book.model_dump(mode="json") == before


def test_experiment_is_reproducible(small: Assumptions) -> None:
    first = run_experiment(strategies(small), SEED, small, n_seeds=3, engine_factory=ScriptedEngine)
    second = run_experiment(
        strategies(small), SEED, small, n_seeds=3, engine_factory=ScriptedEngine
    )
    assert first.output_hash == second.output_hash
    assert [i.model_dump() for i in first.intervals] == [i.model_dump() for i in second.intervals]
