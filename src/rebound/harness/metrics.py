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
    days_to_recovery: tuple[int, ...] = ()

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


def summarise(
    strategy: str,
    episodes: Iterable[RecoveryEpisode],
    fee_rate: float,
    induced_revocations: int = 0,
    compliance_blocks: int = 0,
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
        days_to_recovery=tuple(sorted(e.days_to_recovery or 0 for e in recovered)),
    )


def combine(strategy: str, parts: Iterable[StrategyMetrics]) -> StrategyMetrics:
    parts = tuple(parts)
    if not parts:
        raise ValueError(f"nothing to combine for {strategy}")
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
        days_to_recovery=tuple(sorted(d for p in parts for d in p.days_to_recovery)),
    )
