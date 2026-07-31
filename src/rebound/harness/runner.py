from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable, Sequence
from datetime import datetime

from pydantic import ConfigDict, Field

from rebound.config import Assumptions, load_assumptions
from rebound.domain.entities import (
    AttemptOutcome,
    Bank,
    Customer,
    DebitAttempt,
    DomainModel,
    EpisodeOutcome,
    Mandate,
    MandateStatus,
    RecoveryEpisode,
)
from rebound.domain.reason_codes import ReasonCode
from rebound.engine.failure import FailureEngine
from rebound.engine.protocol import AttemptRequest, AttemptResult, PaymentEngine
from rebound.harness.bootstrap import Interval, paired_intervals
from rebound.harness.metrics import StrategyMetrics, combine, summarise
from rebound.population.book import LAST_UNIVERSAL_DAY_OF_MONTH, Book, generate_book
from rebound.strategies.base import CustomerObservable, ProposedRetry, RetryStrategy

# Built fresh per strategy, so an engine that memoises draws cannot leak one strategy's
# probes into another's results.
EngineFactory = Callable[[Assumptions, int], PaymentEngine]


class SeedResult(DomainModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    seed: int
    metrics: tuple[StrategyMetrics, ...] = Field(min_length=1)

    def for_strategy(self, name: str) -> StrategyMetrics:
        return _pick(self.metrics, name)


class HarnessReport(DomainModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    master_seed: int
    seeds: tuple[int, ...] = Field(min_length=1)
    strategies: tuple[str, ...] = Field(min_length=1)
    per_seed: tuple[SeedResult, ...] = Field(min_length=1)
    aggregate: tuple[StrategyMetrics, ...] = Field(min_length=1)
    intervals: tuple[Interval, ...] = ()
    output_hash: str

    def for_strategy(self, name: str) -> StrategyMetrics:
        return _pick(self.aggregate, name)

    def intervals_for(self, name: str) -> tuple[Interval, ...]:
        return tuple(i for i in self.intervals if i.strategy == name)


def _pick(metrics: Sequence[StrategyMetrics], name: str) -> StrategyMetrics:
    for m in metrics:
        if m.strategy == name:
            return m
    raise KeyError(f"no strategy {name!r} in report; have {[m.strategy for m in metrics]}")


def _cycle_date(start: datetime, billing_day: int, cycle_index: int) -> datetime:
    months = start.month - 1 + cycle_index
    year = start.year + months // 12
    month = months % 12 + 1
    return start.replace(year=year, month=month, day=min(billing_day, LAST_UNIVERSAL_DAY_OF_MONTH))


class _MandateRun:
    """One mandate, one strategy, all cycles. Holds the mutable state a real system
    would hold: mandate status, attempt history, and nothing about the customer's
    true balance."""

    def __init__(self, mandate: Mandate, customer: Customer, bank: Bank, book: Book) -> None:
        self.mandate = mandate
        self.customer = customer
        self.bank = bank
        self.book = book
        self.history: list[DebitAttempt] = []


def _request(
    run: _MandateRun,
    cycle_id: str,
    number: int,
    when: datetime,
    amount: int,
    original_reason: ReasonCode | None,
    days_since: int,
) -> AttemptRequest:
    return AttemptRequest(
        mandate=run.mandate,
        customer=run.customer,
        bank=run.bank,
        cycle_id=cycle_id,
        attempt_number=number,
        scheduled_at=when,
        amount_paise=amount,
        original_reason_code=original_reason,
        days_since_original=days_since,
    )


def _attempt_from(result: AttemptResult, request: AttemptRequest, attempt_id: str) -> DebitAttempt:
    return DebitAttempt(
        id=attempt_id,
        mandate_id=request.mandate.id,
        scheduled_at=request.scheduled_at,
        executed_at=result.executed_at,
        amount_paise=request.amount_paise,
        outcome=result.outcome,
        reason_code=result.reason_code,
        attempt_number=request.attempt_number,
        is_retry=request.attempt_number > 1,
    )


def _next_proposal(
    strategy: RetryStrategy, run: _MandateRun, attempts: list[DebitAttempt], amount: int
) -> ProposedRetry | None:
    view = CustomerObservable.from_customer(
        run.customer, run.mandate, past_attempts=tuple(run.history)
    )
    proposals = strategy.propose_retries(
        failed_attempt=attempts[-1],
        mandate=run.mandate,
        customer_view=view,
        history=list(attempts),
        clock=attempts[-1].scheduled_at,
    )
    valid = [p for p in proposals if p.scheduled_at > attempts[-1].scheduled_at]
    if not valid:
        return None
    return min(valid, key=lambda p: p.scheduled_at)


def _run_cycle(
    strategy: RetryStrategy,
    engine: PaymentEngine,
    run: _MandateRun,
    cycle_index: int,
    amount: int,
    max_attempts: int,
) -> tuple[RecoveryEpisode | None, int]:
    """Returns the episode (None if the first attempt succeeded) and induced revocations."""
    cycle_id = f"{run.mandate.id}:c{cycle_index:02d}"
    billed_at = _cycle_date(run.book.start_at, run.mandate.created_at.day, cycle_index)
    billed_at = billed_at.replace(hour=run.bank.batch_cutoff_time.hour, minute=0)
    opening = _request(run, cycle_id, 1, billed_at, amount, None, 0)
    first = engine.execute(opening)
    attempts = [_attempt_from(first, opening, f"{cycle_id}:a1")]
    run.history.extend(attempts)
    if first.outcome is AttemptOutcome.SUCCESS:
        return None, 0

    original_reason = first.reason_code
    revocations = 0
    while len(attempts) < max_attempts and not attempts[-1].is_hard_decline:
        proposal = _next_proposal(strategy, run, attempts, amount)
        if proposal is None:
            break
        days = (proposal.scheduled_at - billed_at).days
        request = _request(
            run,
            cycle_id,
            len(attempts) + 1,
            proposal.scheduled_at,
            proposal.amount_paise,
            original_reason,
            max(0, days),
        )
        result = engine.execute(request)
        attempt = _attempt_from(result, request, f"{cycle_id}:a{len(attempts) + 1}")
        attempts.append(attempt)
        run.history.append(attempt)
        if result.induced_revocation:
            revocations += 1
            run.mandate.status = MandateStatus.REVOKED
        if result.outcome is AttemptOutcome.SUCCESS:
            break
    return _episode(run, cycle_id, attempts, billed_at), revocations


def _episode(
    run: _MandateRun, cycle_id: str, attempts: list[DebitAttempt], billed_at: datetime
) -> RecoveryEpisode:
    last = attempts[-1]
    recovered = last.outcome is AttemptOutcome.SUCCESS
    return RecoveryEpisode(
        mandate_id=run.mandate.id,
        cycle_id=cycle_id,
        original_attempt=attempts[0],
        retry_attempts=tuple(attempts[1:]),
        outcome=EpisodeOutcome.RECOVERED if recovered else EpisodeOutcome.LAPSED,
        days_to_recovery=(last.scheduled_at - billed_at).days if recovered else None,
        amount_recovered_paise=last.amount_paise if recovered else 0,
    )


def _run_strategy(
    strategy: RetryStrategy, book: Book, engine: PaymentEngine, assumptions: Assumptions
) -> StrategyMetrics:
    max_attempts = int(assumptions.value("harness.max_attempts_per_cycle"))
    fee_rate = float(assumptions.value("harness.performance_fee_rate"))
    banks = {b.id: b for b in book.banks}
    customers = {c.id: c for c in book.customers}
    episodes: list[RecoveryEpisode] = []
    revocations = 0
    for mandate in book.mandates:
        customer = customers[mandate.customer_id]
        run = _MandateRun(mandate, customer, banks[customer.bank_id], book)
        amount = min(book.merchant.avg_ticket_paise, mandate.max_amount_paise)
        for cycle_index in range(book.months):
            if not mandate.is_debitable:
                break
            episode, induced = _run_cycle(strategy, engine, run, cycle_index, amount, max_attempts)
            revocations += induced
            if episode is not None:
                episodes.append(episode)
    return summarise(strategy.name, episodes, fee_rate, induced_revocations=revocations)


def run_paired(
    book: Book,
    strategies: Sequence[RetryStrategy],
    seed: int,
    assumptions: Assumptions | None = None,
    engine_factory: EngineFactory = FailureEngine,
) -> HarnessReport:
    """Every strategy sees a deep copy of the same book and the same engine seed, so the
    populations are identical and the outcomes are common random numbers. Differences
    between strategies are timing, never sampling.

    Each strategy gets its own freshly constructed engine, not a shared one: engines
    memoise draws, and a shared instance would leak one strategy's probes into another's
    results.
    """
    if not strategies:
        raise ValueError("run_paired needs at least one strategy")
    assumptions = assumptions or load_assumptions()
    metrics = tuple(
        _run_strategy(s, copy.deepcopy(book), engine_factory(assumptions, seed), assumptions)
        for s in strategies
    )
    seed_result = SeedResult(seed=seed, metrics=metrics)
    return _report(seed, (seed_result,), [s.name for s in strategies], assumptions)


def run_experiment(
    strategies: Sequence[RetryStrategy],
    master_seed: int,
    assumptions: Assumptions | None = None,
    n_seeds: int | None = None,
    engine_factory: EngineFactory = FailureEngine,
) -> HarnessReport:
    """SPEC §5.1: bootstrap confidence intervals over N seeds. `run_paired` is the single
    seed case and carries no intervals — one seed cannot support one."""
    assumptions = assumptions or load_assumptions()
    n_seeds = n_seeds or int(assumptions.value("harness.bootstrap_seeds"))
    seeds = [master_seed + i for i in range(n_seeds)]
    per_seed = []
    for seed in seeds:
        book = generate_book(assumptions, seed)
        single = run_paired(book, strategies, seed, assumptions, engine_factory)
        per_seed.append(single.per_seed[0])
    return _report(master_seed, tuple(per_seed), [s.name for s in strategies], assumptions)


def _report(
    master_seed: int,
    per_seed: tuple[SeedResult, ...],
    names: list[str],
    assumptions: Assumptions,
) -> HarnessReport:
    aggregate = tuple(combine(name, [s.for_strategy(name) for s in per_seed]) for name in names)
    intervals = (
        paired_intervals(per_seed, names, master_seed, assumptions) if len(per_seed) > 1 else ()
    )
    return HarnessReport(
        master_seed=master_seed,
        seeds=tuple(s.seed for s in per_seed),
        strategies=tuple(names),
        per_seed=per_seed,
        aggregate=aggregate,
        intervals=intervals,
        output_hash=_output_hash(master_seed, per_seed, names),
    )


def _output_hash(master_seed: int, per_seed: tuple[SeedResult, ...], names: list[str]) -> str:
    """Hashes the integer counters only. Every rate and every interval is a deterministic
    function of these, so a float never enters the digest and the comparison is exact."""
    payload = {
        "master_seed": master_seed,
        "strategies": names,
        "per_seed": [s.model_dump(mode="json") for s in per_seed],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
