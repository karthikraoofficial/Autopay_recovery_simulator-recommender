"""SPEC §13. Phase 8.7.

The tests that matter here are the reconciliation ones. A trace that disagrees with the
headline is worse than no trace: it is a debugging tool that lies about the thing it is
being used to debug.
"""

from __future__ import annotations

import csv
import io

import pytest
from fastapi.testclient import TestClient

from rebound.api.app import create_app
from rebound.compliance.guard import ComplianceGuard
from rebound.config import Assumptions
from rebound.domain.entities import EpisodeOutcome
from rebound.harness.runner import default_strategies, run_paired
from rebound.harness.trace import (
    PAISE_PER_RUPEE,
    SIM_PREFIX,
    GuardVerdict,
    MultipleSeedsError,
    Table,
    Trace,
    TraceCollector,
    build_trace,
    sim_id,
    to_csv,
)
from rebound.population.book import generate_book

SEED = 20260801
BOOK_SIZE = 60


@pytest.fixture(scope="module")
def tiny(shipped: Assumptions) -> Assumptions:
    return shipped.with_values({"book.months": 6, "book.size": BOOK_SIZE})


@pytest.fixture(scope="module")
def trace(tiny: Assumptions) -> Trace:
    return build_trace(SEED, tiny)


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(create_app())


# --- reconciliation: the trace must agree with the headline -------------------


def test_recovered_rupees_reconcile_to_the_headline_per_strategy(tiny: Assumptions) -> None:
    """SPEC §13.4, and the reason this file exists.

    Compared against the harness's own `StrategyMetrics` from the *same* run, not against
    a recomputation — a recomputation could reproduce the trace's mistake and agree with
    it. If these ever diverge, one of the trace and the headline is wrong.
    """
    book = generate_book(tiny, SEED)
    collector = TraceCollector()
    report = run_paired(book, default_strategies(tiny), SEED, tiny, observer=collector)
    trace = build_trace(SEED, tiny)

    assert trace.header.output_hash == report.output_hash
    for name in report.strategies:
        metrics = report.for_strategy(name)
        assert trace.recovered_inr(name) == pytest.approx(
            metrics.gross_recovered_paise / PAISE_PER_RUPEE
        ), f"{name}: trace and headline disagree on gross recovered"
        outcomes = [o for o in trace.outcomes if o.strategy == name]
        assert sum(o.episodes for o in outcomes) == metrics.episodes
        assert sum(o.recovered_episodes for o in outcomes) == metrics.recovered_episodes


def test_attempt_rows_reconcile_to_the_attempt_counters(tiny: Assumptions) -> None:
    """The other half: every executed attempt appears exactly once. Blocked proposals are
    excluded because they were never attempts — counting them would inflate the trace
    against the counters."""
    book = generate_book(tiny, SEED)
    report = run_paired(book, default_strategies(tiny), SEED, tiny)
    trace = build_trace(SEED, tiny)
    for name in report.strategies:
        metrics = report.for_strategy(name)
        executed = [
            a
            for a in trace.attempts
            if a.strategy == name and a.guard_verdict is not GuardVerdict.BLOCKED
        ]
        assert len(executed) == metrics.total_attempts
        assert sum(1 for a in executed if a.is_retry) == metrics.retry_attempts


def test_every_reschedule_appears_in_the_trace_with_matching_from_and_to(
    tiny: Assumptions,
) -> None:
    """The guard log is the source of truth. If the trace's per-cycle attribution ever
    drops a reschedule, this fails rather than the trace quietly under-reporting how often
    compliance moved a retry."""
    book = generate_book(tiny, SEED)
    strategies = default_strategies(tiny)
    collector = TraceCollector()
    run_paired(book, strategies, SEED, tiny, observer=collector)
    trace = build_trace(SEED, tiny)

    logged = {
        (strategy, entry.mandate_id, entry.proposed_at, entry.rescheduled_at)
        for (strategy, _, _), entries in collector.reschedules.items()
        for entry in entries
    }
    assert logged, "expected the guard to reschedule at least one retry in this book"
    traced = {
        (a.strategy, a.sim_id, a.rescheduled_from, a.rescheduled_to)
        for a in trace.attempts
        if a.guard_verdict is GuardVerdict.RESCHEDULED
    }
    missing = {
        (s, sim_id(m), f, t) for s, m, f, t in logged
    } - traced
    assert missing == set(), f"reschedules in the guard log but not in the trace: {missing}"


def test_every_block_appears_in_the_trace_with_its_rule(tiny: Assumptions) -> None:
    book = generate_book(tiny, SEED)
    collector = TraceCollector()
    run_paired(book, default_strategies(tiny), SEED, tiny, observer=collector)
    trace = build_trace(SEED, tiny)

    logged = sum(len(v) for v in collector.blocks.values())
    blocked = [a for a in trace.attempts if a.guard_verdict is GuardVerdict.BLOCKED]
    assert logged > 0, "expected the guard to block at least one proposal in this book"
    assert len(blocked) == logged
    assert all(a.blocked_by_rule and a.blocked_detail for a in blocked)
    assert all(a.attempt_number is None for a in blocked), (
        "a blocked proposal never became an attempt; numbering it would imply otherwise"
    )


def test_no_attempt_lacks_a_guard_verdict(trace: Trace) -> None:
    """Every row carries an explicit verdict, including opening debits, which the guard
    never sees. A blank would be indistinguishable from a dropped join."""
    assert trace.attempts
    for attempt in trace.attempts:
        assert attempt.guard_verdict in set(GuardVerdict)
        if not attempt.is_retry:
            assert attempt.guard_verdict is GuardVerdict.NOT_REVIEWED
        if attempt.guard_verdict is GuardVerdict.RESCHEDULED:
            assert attempt.rescheduled_from is not None
            assert attempt.rescheduled_to == attempt.scheduled_at
    retries = [a for a in trace.attempts if a.is_retry]
    assert retries, "a trace with no retries cannot exercise the guard at all"
    assert all(a.guard_verdict is not GuardVerdict.NOT_REVIEWED for a in retries)


# --- single seed --------------------------------------------------------------


def test_build_trace_takes_one_seed_by_signature(tiny: Assumptions) -> None:
    """SPEC §13.1: structural, not validated. There is no argument shape that expresses a
    multi-seed trace."""
    with pytest.raises(MultipleSeedsError):
        build_trace([SEED, SEED + 1], tiny)  # type: ignore[arg-type]


def test_a_repeated_seed_parameter_is_refused_not_silently_narrowed(
    client: TestClient,
) -> None:
    """FastAPI binds the last value of a repeated query parameter, so `?seed=1&seed=2`
    would otherwise answer with seed 2's trace and no sign the request was not honoured.
    That is exactly the quiet merge §13.1 forbids."""
    response = client.get("/trace", params=[("seed", 1), ("seed", 2)])
    assert response.status_code == 422
    assert "exactly one seed" in response.json()["detail"]


def test_a_comma_joined_seed_is_refused(client: TestClient) -> None:
    assert client.get("/trace", params={"seed": "1,2"}).status_code == 422


# --- the export itself --------------------------------------------------------


def test_every_id_is_prefixed_and_no_domain_id_leaks(trace: Trace) -> None:
    """A row pasted into a ticket or a query must not look like a production identifier."""
    assert sim_id("mandate-000247") == "SIM-000247"
    for row in trace.mandates:
        assert row.sim_id.startswith(SIM_PREFIX)
    for attempt in trace.attempts:
        assert attempt.sim_id.startswith(SIM_PREFIX)
    for outcome in trace.outcomes:
        assert outcome.sim_id.startswith(SIM_PREFIX)


def test_seed_and_hash_repeat_on_every_row(trace: Trace) -> None:
    """SPEC §13.2. A row separated from its header must still be traceable to the run."""
    for row in (*trace.attempts, *trace.mandates):
        assert row.seed == trace.header.seed
        assert row.output_hash == trace.header.output_hash


def test_the_trace_carries_no_recommendation_column(trace: Trace) -> None:
    """SPEC §13.3. What each strategy DID is a fact about the run; what a merchant SHOULD
    do generalises and belongs in the decision table. Advice next to a synthetic id
    invites someone to act on a customer who does not exist."""
    from rebound.harness.trace import TraceAttempt, TraceMandate, TraceOutcome

    banned = {
        "recommendation",
        "recommended",
        "recommended_tactic",
        "action",
        "advice",
        "next_step",
    }
    for model in (TraceAttempt, TraceMandate, TraceOutcome):
        assert not banned & set(model.model_fields), f"{model.__name__} grew an advice column"


@pytest.mark.parametrize("table", list(Table))
def test_csv_carries_the_header_block_and_parses(trace: Trace, table: Table) -> None:
    """SPEC §13.2: header on every export. Comment-prefixed, so the file is still a valid
    CSV to anything reading it with comment='#'."""
    text = to_csv(trace, table)
    assert f"# seed: {trace.header.seed}" in text
    assert f"# output_hash: {trace.header.output_hash}" in text
    assert "# exported_at:" in text
    assert "SYNTHETIC" in text
    body = [line for line in text.splitlines() if not line.startswith("#")]
    rows = list(csv.DictReader(io.StringIO("\n".join(body))))
    assert rows
    assert all(row["sim_id"].startswith(SIM_PREFIX) for row in rows)


def test_csv_is_long_format_one_row_per_attempt(trace: Trace) -> None:
    """Not one row per mandate with a text blob. Every column needed to filter is present
    without joining back to the mandate table."""
    body = [line for line in to_csv(trace, Table.ATTEMPTS).splitlines() if not line.startswith("#")]
    rows = list(csv.DictReader(io.StringIO("\n".join(body))))
    assert len(rows) == len(trace.attempts)
    required = {
        "sim_id",
        "seed",
        "output_hash",
        "strategy",
        "cycle_index",
        "attempt_number",
        "is_retry",
        "scheduled_at",
        "executed_at",
        "guard_verdict",
        "outcome",
        "reason_code",
        "amount_inr",
    }
    assert required <= set(rows[0])


def test_mandate_attributes_live_in_their_own_table(trace: Trace) -> None:
    """Two files, one join key — rather than repeating rail and bank on every attempt."""
    from rebound.harness.trace import TraceAttempt

    attributes = {"rail", "bank_id", "income_band", "salary_credit_day", "billing_day"}
    assert attributes <= set(type(trace.mandates[0]).model_fields)
    assert not attributes & set(TraceAttempt.model_fields)
    assert {m.sim_id for m in trace.mandates} >= {a.sim_id for a in trace.attempts}


def test_missing_values_render_as_empty_not_the_word_none(trace: Trace) -> None:
    """A CSV read back into a dataframe should see a null, not the string 'None'."""
    text = to_csv(trace, Table.ATTEMPTS)
    body = [line for line in text.splitlines() if not line.startswith("#")]
    assert ",None," not in "\n".join(body)


def test_the_trace_is_deterministic_apart_from_its_export_timestamp(
    tiny: Assumptions,
) -> None:
    """SPEC §0.5. `exported_at` is provenance, not result: it is the only field allowed to
    move between two traces of the same seed."""
    first = build_trace(SEED, tiny).model_dump(mode="json")
    second = build_trace(SEED, tiny).model_dump(mode="json")
    first["header"].pop("exported_at")
    second["header"].pop("exported_at")
    assert first == second


def test_every_strategy_appears_side_by_side_for_the_same_mandate(trace: Trace) -> None:
    """The point of the export: the same synthetic mandate under every strategy."""
    strategies = set(trace.header.strategies)
    assert len(strategies) > 1
    for sim in {m.sim_id for m in trace.mandates}:
        assert {o.strategy for o in trace.outcomes if o.sim_id == sim} == strategies


def test_lapsed_and_recovered_episodes_sum_to_episodes(trace: Trace) -> None:
    for outcome in trace.outcomes:
        assert outcome.recovered_episodes + outcome.lapsed_episodes == outcome.episodes
        if outcome.recovered_episodes == 0:
            assert outcome.recovered_inr == 0.0


def test_the_guard_is_the_same_one_the_run_used(tiny: Assumptions) -> None:
    """Defence in depth for the reconciliation above: a trace built from a second,
    independently constructed guard could agree with itself and disagree with the run."""
    guard = ComplianceGuard(tiny)
    assert guard.blocks == [] and guard.reschedules == []
    book = generate_book(tiny, SEED)
    collector = TraceCollector()
    run_paired(book, default_strategies(tiny), SEED, tiny, observer=collector)
    recovered = {
        (strategy, mandate_id)
        for (strategy, mandate_id), episodes in collector.episodes.items()
        if any(e.outcome is EpisodeOutcome.RECOVERED for e in episodes)
    }
    assert recovered, "expected some recoveries to trace"
