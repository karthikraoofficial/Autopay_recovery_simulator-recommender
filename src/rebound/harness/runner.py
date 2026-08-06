from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Protocol

from pydantic import ConfigDict, Field

from rebound.compliance.guard import (
    ComplianceBlock,
    ComplianceGuard,
    ComplianceReschedule,
    RetryContext,
)
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
from rebound.strategies.bank_aware import BankAware
from rebound.strategies.base import (
    CustomerObservable,
    LearningStrategy,
    ProposedRetry,
    RetryStrategy,
)
from rebound.strategies.blended import Blended
from rebound.strategies.fixed_schedule import FixedSchedule
from rebound.strategies.no_reschedule import NoReschedule
from rebound.strategies.no_retry import NoRetry
from rebound.strategies.reason_aware import ReasonAware
from rebound.strategies.salary_aware import SalaryAware

# Built fresh per strategy, so an engine that memoises draws cannot leak one strategy's
# probes into another's results.
EngineFactory = Callable[[Assumptions, int], PaymentEngine]

# Called with (completed, total) after each strategy finishes, purely so a long run can
# show progress. It receives counts and returns nothing: it cannot reach the RNG, the
# book, or the metrics, so a run with a callback and a run without produce the same
# output hash.
ProgressCallback = Callable[[int, int], None]


class RunObserver(Protocol):
    """Sees each mandate and each episode as a run produces them, for analyses that need
    per-mandate detail the aggregate counters have thrown away (SPEC §12).

    Read-only by contract: an observer receives finished objects and returns nothing, so
    it cannot reach the RNG or alter a decision. That is what lets the segment report be
    cut from the *same* runs as the headline instead of a second simulation, which could
    silently disagree with it.
    """

    def saw_mandate(self, strategy: str, seed: int, mandate: Mandate) -> None: ...

    def saw_episode(
        self, strategy: str, seed: int, mandate: Mandate, episode: RecoveryEpisode
    ) -> None: ...

    def saw_opening_attempt(
        self, strategy: str, seed: int, mandate: Mandate, cycle_index: int, failed: bool
    ) -> None:
        """The opening debit of one billing cycle, and whether it failed.

        The aggregate counters can produce this in total -- attempted is
        `total_attempts - retry_attempts`, failed is `episodes` -- but not per rail, and a
        merchant asks "how many of my UPI debits fail" before asking what recovery is
        worth. Handed over here so the answer is cut from the same run as the headline
        rather than from a second simulation that could disagree with it.

        A *successful* opening produces no episode, so `saw_episode` cannot see the
        denominator. That is the whole reason this hook exists.
        """

    def saw_cycle_guard_events(
        self,
        strategy: str,
        seed: int,
        mandate: Mandate,
        cycle_index: int,
        blocks: tuple[ComplianceBlock, ...],
        reschedules: tuple[ComplianceReschedule, ...],
    ) -> None:
        """The guard's verdicts for one billing cycle, sliced as that cycle runs.

        Handed over per cycle rather than joined afterwards on (mandate_id, time). The
        guard is constructed per strategy run and its log is append-only, so slicing it at
        the cycle boundary attributes every verdict exactly. A post-hoc join would have to
        guess which cycle a block belonged to and could silently drop rows.
        """


class SeedResult(DomainModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    seed: int
    metrics: tuple[StrategyMetrics, ...] = Field(min_length=1)

    def for_strategy(self, name: str) -> StrategyMetrics:
        return _pick(self.metrics, name)


class LiftDecomposition(DomainModel):
    """The headline split into the two things a merchant is actually being sold.

    `rescheduling` is what re-presenting a retry that a timing rule blocked is worth. It
    is scheduler plumbing: no reason codes, no inference, no model. `strategy` is what the
    retry logic adds on top of a scheduler that already does that.

    They are reported apart because they are bought apart, and because measurement showed
    the first to be several times the second. A single combined number would sell plumbing
    as intelligence — and the phase-7 audit found that when the harness abandoned blocked
    retries, that same confusion made every strategy look worse than its baseline.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    metric: str = Field(min_length=1)
    scheduler_reference: str = Field(min_length=1)
    baseline: str = Field(min_length=1)
    headline: str = Field(min_length=1)
    rescheduling: Interval
    strategy: Interval

    def __str__(self) -> str:
        return (
            f"{self.metric}\n"
            f"  rescheduling ({self.scheduler_reference} -> {self.baseline}): "
            f"[{self.rescheduling.low:,.4g}, {self.rescheduling.high:,.4g}] "
            f"(point {self.rescheduling.point:,.4g})\n"
            f"  strategy     ({self.baseline} -> {self.headline}): "
            f"[{self.strategy.low:,.4g}, {self.strategy.high:,.4g}] "
            f"(point {self.strategy.point:,.4g})"
        )


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

    def interval(self, strategy: str, metric: str, vs_baseline: bool = True) -> Interval | None:
        for i in self.intervals_for(strategy):
            if i.metric == metric and (i.vs_baseline is not None) == vs_baseline:
                return i
        return None

    def decomposition(
        self, metric: str = "net_recovered_paise", assumptions: Assumptions | None = None
    ) -> LiftDecomposition | None:
        """Both line items, or None if the run did not include the strategies to form them.

        None rather than a partial answer: a decomposition missing its scheduler reference
        would be indistinguishable from one where rescheduling is worth nothing.
        """
        assumptions = assumptions or load_assumptions()
        scheduler = str(assumptions.value("harness.scheduler_reference_strategy"))
        baseline = str(assumptions.value("harness.baseline_strategy"))
        headline = str(assumptions.value("harness.headline_strategy"))
        against_baseline = self.interval(scheduler, metric)
        strategy = self.interval(headline, metric)
        if against_baseline is None or strategy is None:
            return None
        return LiftDecomposition(
            metric=metric,
            scheduler_reference=scheduler,
            baseline=baseline,
            headline=headline,
            # Stored as scheduler-minus-baseline (negative). Rescheduling lift is the
            # gain going the other way, so the interval is turned round rather than
            # re-bootstrapped against a different reference.
            rescheduling=against_baseline.negated(strategy=baseline, vs_baseline=scheduler),
            strategy=strategy,
        )


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
    notified: bool = False,
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
        pre_debit_notified=notified,
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
    strategy: RetryStrategy,
    run: _MandateRun,
    attempts: list[DebitAttempt],
    guard: ComplianceGuard,
    notified_at: datetime | None,
) -> ProposedRetry | None:
    """Ask the strategy, then put every proposal through the compliance gate.

    SPEC §0.3: the guard is the only path to execution, so a strategy cannot reach the
    engine with a retry it is not allowed to make. A blocked proposal costs the strategy
    that choice, not the whole chain — the earliest surviving alternative still runs.
    """
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
    context = RetryContext(
        mandate=run.mandate,
        bank=run.bank,
        original_attempt=attempts[0],
        history=tuple(attempts),
        notified_at=notified_at,
    )
    return guard.filter(valid, context)


def _run_cycle(
    strategy: RetryStrategy,
    engine: PaymentEngine,
    run: _MandateRun,
    cycle_index: int,
    amount: int,
    max_attempts: int,
    guard: ComplianceGuard,
    notice_lead: timedelta,
) -> tuple[RecoveryEpisode | None, int, int]:
    """Returns the episode (None if the first attempt succeeded), induced revocations,
    and whether a hard decline terminated the chain."""
    cycle_id = f"{run.mandate.id}:c{cycle_index:02d}"
    billed_at = _cycle_date(run.book.start_at, run.mandate.created_at.day, cycle_index)
    billed_at = billed_at.replace(hour=run.bank.batch_cutoff_time.hour, minute=0)
    # The merchant is modelled as compliant: notice goes out exactly the required lead
    # ahead of the scheduled charge. SPEC §3 requires it, so a merchant who skipped it
    # could not legally debit at all and is not a scenario worth simulating.
    notified_at = billed_at - notice_lead
    opening = _request(run, cycle_id, 1, billed_at, amount, None, 0, notified=True)
    first = engine.execute(opening)
    attempts = [_attempt_from(first, opening, f"{cycle_id}:a1")]
    run.history.extend(attempts)
    if first.outcome is AttemptOutcome.SUCCESS:
        return None, 0, 0

    original_reason = first.reason_code
    revocations = 0
    # SPEC §1.3: a hard decline ends the episode as a matter of lifecycle. The strategy
    # is never asked, so a reason-blind baseline cannot propose against a dead mandate.
    # The guard raises on such a proposal as defence in depth, should this regress.
    while len(attempts) < max_attempts and not attempts[-1].is_hard_decline:
        proposal = _next_proposal(strategy, run, attempts, guard, notified_at)
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
            notified=True,
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
    terminated = int(attempts[-1].is_hard_decline)
    return _episode(run, cycle_id, cycle_index, attempts, billed_at), revocations, terminated


def _episode(
    run: _MandateRun,
    cycle_id: str,
    cycle_index: int,
    attempts: list[DebitAttempt],
    billed_at: datetime,
) -> RecoveryEpisode:
    last = attempts[-1]
    recovered = last.outcome is AttemptOutcome.SUCCESS
    return RecoveryEpisode(
        mandate_id=run.mandate.id,
        cycle_id=cycle_id,
        cycle_index=cycle_index,
        original_attempt=attempts[0],
        retry_attempts=tuple(attempts[1:]),
        outcome=EpisodeOutcome.RECOVERED if recovered else EpisodeOutcome.LAPSED,
        days_to_recovery=(last.scheduled_at - billed_at).days if recovered else None,
        amount_recovered_paise=last.amount_paise if recovered else 0,
    )


def _run_strategy(
    strategy: RetryStrategy,
    book: Book,
    engine: PaymentEngine,
    assumptions: Assumptions,
    seed: int = 0,
    observer: RunObserver | None = None,
) -> StrategyMetrics:
    max_attempts = int(assumptions.value("harness.max_attempts_per_cycle"))
    fee_rate = float(assumptions.value("harness.performance_fee_rate"))
    notice_lead = timedelta(
        hours=float(assumptions.value("compliance.pre_debit_notification.lead_hours"))
    )
    # One guard per strategy run, so its block log is that strategy's own opportunity
    # forgone rather than a total shared across the paired comparison.
    # Optional, and absent on every strategy but `NoReschedule`: whether a retry blocked
    # by a timing rule is moved to the next legal slot or abandoned. It belongs to the
    # scheduler a merchant runs, not to the retry logic, which is why it is a property of
    # the strategy under test rather than a global setting.
    guard = ComplianceGuard(
        assumptions, reschedule=getattr(strategy, "reschedules_blocked_retries", True)
    )
    # A strategy that learns from the merchant's own history starts each book knowing
    # nothing, so seed N is not measured on state accumulated over seeds 1..N-1.
    if isinstance(strategy, LearningStrategy):
        strategy.reset()
    banks = {b.id: b for b in book.banks}
    customers = {c.id: c for c in book.customers}
    episodes: list[RecoveryEpisode] = []
    revocations = 0
    terminated = 0
    for mandate in book.mandates:
        customer = customers[mandate.customer_id]
        run = _MandateRun(mandate, customer, banks[customer.bank_id], book)
        amount = min(book.merchant.avg_ticket_paise, mandate.max_amount_paise)
        if observer is not None:
            observer.saw_mandate(strategy.name, seed, mandate)
        for cycle_index in range(book.months):
            if not mandate.is_debitable:
                break
            # Marks into the guard's append-only log, so this cycle's verdicts can be
            # sliced out exactly rather than matched back to it later.
            seen_blocks, seen_reschedules = len(guard.blocks), len(guard.reschedules)
            episode, induced, stopped = _run_cycle(
                strategy, engine, run, cycle_index, amount, max_attempts, guard, notice_lead
            )
            revocations += induced
            terminated += stopped
            if observer is not None:
                # Every cycle that reaches `_run_cycle` makes exactly one opening debit,
                # and it failed iff an episode came back. Reported before the episode so
                # the denominator is counted whatever happens to the numerator.
                observer.saw_opening_attempt(
                    strategy.name, seed, mandate, cycle_index, episode is not None
                )
            if episode is not None:
                episodes.append(episode)
                if observer is not None:
                    observer.saw_episode(strategy.name, seed, mandate, episode)
                    observer.saw_cycle_guard_events(
                        strategy.name,
                        seed,
                        mandate,
                        cycle_index,
                        tuple(guard.blocks[seen_blocks:]),
                        tuple(guard.reschedules[seen_reschedules:]),
                    )
    return summarise(
        strategy.name,
        episodes,
        fee_rate,
        induced_revocations=revocations,
        compliance_blocks=len(guard.blocks),
        compliance_reschedules=len(guard.reschedules),
        terminated_hard_decline=terminated,
        months=book.months,
    )


def default_strategies(assumptions: Assumptions | None = None) -> list[RetryStrategy]:
    """Every strategy the harness reports on, in the order results are read in.

    Four references then three strategies. The references are not decoration: `NoRetry`
    is the natural-recovery floor, `NoReschedule` is what a merchant runs today,
    `FixedSchedule` is what they run with a scheduler that re-presents blocked retries,
    and only the difference above that is attributable to retry logic. Dropping
    `NoReschedule` from the set collapses the two line items of `LiftDecomposition` into
    one number that reads as strategy lift and mostly is not.
    """
    assumptions = assumptions or load_assumptions()
    return [
        NoRetry(),
        NoReschedule(assumptions),
        FixedSchedule(assumptions),
        ReasonAware(assumptions),
        SalaryAware(assumptions),
        BankAware(assumptions),
        Blended(assumptions),
    ]


def run_paired(
    book: Book,
    strategies: Sequence[RetryStrategy],
    seed: int,
    assumptions: Assumptions | None = None,
    engine_factory: EngineFactory = FailureEngine,
    on_strategy_done: Callable[[], None] | None = None,
    observer: RunObserver | None = None,
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
    metrics = []
    for strategy in strategies:
        metrics.append(
            _run_strategy(
                strategy,
                copy.deepcopy(book),
                engine_factory(assumptions, seed),
                assumptions,
                seed,
                observer,
            )
        )
        if on_strategy_done is not None:
            on_strategy_done()
    seed_result = SeedResult(seed=seed, metrics=tuple(metrics))
    return _report(seed, (seed_result,), [s.name for s in strategies], assumptions)


def run_experiment(
    strategies: Sequence[RetryStrategy],
    master_seed: int,
    assumptions: Assumptions | None = None,
    n_seeds: int | None = None,
    engine_factory: EngineFactory = FailureEngine,
    progress: ProgressCallback | None = None,
    observer: RunObserver | None = None,
) -> HarnessReport:
    """SPEC §5.1: bootstrap confidence intervals over N seeds. `run_paired` is the single
    seed case and carries no intervals — one seed cannot support one."""
    assumptions = assumptions or load_assumptions()
    n_seeds = n_seeds or int(assumptions.value("harness.bootstrap_seeds"))
    seeds = [master_seed + i for i in range(n_seeds)]
    total = n_seeds * len(strategies)
    done = 0

    def step() -> None:
        nonlocal done
        done += 1
        if progress is not None:
            progress(done, total)

    per_seed = []
    for seed in seeds:
        book = generate_book(assumptions, seed)
        single = run_paired(
            book, strategies, seed, assumptions, engine_factory, step, observer
        )
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
