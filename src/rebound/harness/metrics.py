from __future__ import annotations

import math
from collections.abc import Iterable

from pydantic import ConfigDict, Field

from rebound.domain.entities import DomainModel, EpisodeOutcome, Paise, RecoveryEpisode


def _nearest_rank(sorted_values: tuple[int, ...], percentile: float) -> int | None:
    """Integer-in, integer-out. Avoids interpolation so the number is exactly a day that
    actually happened, and so the output hash contains no floats."""
    if not sorted_values:
        return None
    index = math.ceil(len(sorted_values) * percentile) - 1
    return sorted_values[max(0, min(len(sorted_values) - 1, index))]


class StrategyMetrics(DomainModel):
    """Counters only. Every rate is derived, so the stored state is exactly integral and
    two runs can be compared byte for byte."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy: str = Field(min_length=1)
    episodes: int = Field(ge=0)
    recovered_episodes: int = Field(ge=0)
    gross_recovered_paise: Paise = Field(ge=0)
    total_failed_paise: Paise = Field(ge=0)
    performance_fee_paise: Paise = Field(ge=0)
    total_attempts: int = Field(ge=0)
    retry_attempts: int = Field(ge=0)
    induced_revocations: int = Field(ge=0)
    compliance_blocks: int = Field(ge=0)
    # Blocked by a timing rule and moved to a legal slot, not abandoned. Kept apart from
    # compliance_blocks because the two mean opposite things commercially: a block is
    # revenue forgone, a reschedule is the same revenue collected later.
    compliance_reschedules: int = Field(ge=0)
    # Kept apart from compliance_blocks on purpose. A hard decline ends the episode
    # as a matter of lifecycle (SPEC §1.3); it is not a retry the merchant wanted and
    # compliance forbade, so folding it into 'opportunity forgone' would inflate that
    # number with terminations no policy change could ever release.
    terminated_hard_decline: int = Field(ge=0)
    days_to_recovery: tuple[int, ...] = ()
    # Gross ₹ recovered in each billing month, zero-based, one entry per month of the
    # run. SPEC §6 renders this; it is not derivable from the totals above.
    recovered_paise_by_cycle: tuple[Paise, ...] = ()

    @property
    def recovery_rate(self) -> float:
        return self.recovered_episodes / self.episodes if self.episodes else 0.0

    @property
    def net_recovered_paise(self) -> Paise:
        return self.gross_recovered_paise - self.performance_fee_paise

    @property
    def median_days_to_recovery(self) -> int | None:
        return _nearest_rank(self.days_to_recovery, 0.5)

    @property
    def p90_days_to_recovery(self) -> int | None:
        return _nearest_rank(self.days_to_recovery, 0.9)

    @property
    def attempts_per_recovery(self) -> float | None:
        if not self.recovered_episodes:
            return None
        return self.total_attempts / self.recovered_episodes

    @property
    def attempts_per_episode(self) -> float:
        """Mean attempts actually executed per failed episode — the parity check.

        Two strategies are only comparable on recovery if they were allowed comparable
        numbers of shots at it. A strategy that recovers less while also attempting less
        has not been shown to be worse at timing; it has been shown to have been stopped
        earlier, by its own cap, by the attempt cap, or by a rule that ended its chain.
        Reported beside recovery rate for that reason, never behind it.
        """
        return self.total_attempts / self.episodes if self.episodes else 0.0

    @property
    def retries_per_episode(self) -> float:
        return self.retry_attempts / self.episodes if self.episodes else 0.0


def _by_cycle(recovered: Iterable[RecoveryEpisode], months: int) -> tuple[Paise, ...]:
    """Fixed length, not the observed maximum. A strategy that recovered nothing in the
    final month must still report a zero there, or two strategies' series would have
    different lengths and could not be charted against each other."""
    series = [0] * months
    for episode in recovered:
        if episode.cycle_index >= months:
            raise ValueError(f"episode in month {episode.cycle_index} of a {months}-month run")
        series[episode.cycle_index] += episode.amount_recovered_paise
    return tuple(series)


def summarise(
    strategy: str,
    episodes: Iterable[RecoveryEpisode],
    fee_rate: float,
    induced_revocations: int = 0,
    compliance_blocks: int = 0,
    compliance_reschedules: int = 0,
    terminated_hard_decline: int = 0,
    # Required, with no default. A default would silently produce a zero-length monthly
    # series for any caller that forgot it, and an empty chart is harder to notice than
    # a missing argument.
    *,
    months: int,
) -> StrategyMetrics:
    episodes = tuple(episodes)
    recovered = [e for e in episodes if e.outcome is EpisodeOutcome.RECOVERED]
    gross = sum(e.amount_recovered_paise for e in recovered)
    return StrategyMetrics(
        strategy=strategy,
        episodes=len(episodes),
        recovered_episodes=len(recovered),
        gross_recovered_paise=gross,
        total_failed_paise=sum(e.original_attempt.amount_paise for e in episodes),
        performance_fee_paise=int(round(gross * fee_rate)),
        total_attempts=sum(len(e.attempts) for e in episodes),
        retry_attempts=sum(len(e.retry_attempts) for e in episodes),
        induced_revocations=induced_revocations,
        compliance_blocks=compliance_blocks,
        compliance_reschedules=compliance_reschedules,
        terminated_hard_decline=terminated_hard_decline,
        days_to_recovery=tuple(sorted(e.days_to_recovery or 0 for e in recovered)),
        recovered_paise_by_cycle=_by_cycle(recovered, months),
    )


def combine(strategy: str, parts: Iterable[StrategyMetrics]) -> StrategyMetrics:
    parts = tuple(parts)
    if not parts:
        raise ValueError(f"nothing to combine for {strategy}")
    lengths = {len(p.recovered_paise_by_cycle) for p in parts}
    if len(lengths) > 1:
        raise ValueError(f"{strategy} combines runs of differing horizons: {sorted(lengths)}")
    return StrategyMetrics(
        strategy=strategy,
        episodes=sum(p.episodes for p in parts),
        recovered_episodes=sum(p.recovered_episodes for p in parts),
        gross_recovered_paise=sum(p.gross_recovered_paise for p in parts),
        total_failed_paise=sum(p.total_failed_paise for p in parts),
        performance_fee_paise=sum(p.performance_fee_paise for p in parts),
        total_attempts=sum(p.total_attempts for p in parts),
        retry_attempts=sum(p.retry_attempts for p in parts),
        induced_revocations=sum(p.induced_revocations for p in parts),
        compliance_blocks=sum(p.compliance_blocks for p in parts),
        compliance_reschedules=sum(p.compliance_reschedules for p in parts),
        terminated_hard_decline=sum(p.terminated_hard_decline for p in parts),
        days_to_recovery=tuple(sorted(d for p in parts for d in p.days_to_recovery)),
        recovered_paise_by_cycle=tuple(
            sum(values) for values in zip(*(p.recovered_paise_by_cycle for p in parts), strict=True)
        ),
    )
