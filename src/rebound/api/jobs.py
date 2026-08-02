"""Background runs, so a publication run on a real book does not hold an HTTP request open.

In-process and in-memory. This is not the SQLite cache SPEC §7 offers and is not
persistence: a job is the record of one run in flight, it is discarded when it finishes
being read, and restarting the API loses it. Nothing about the simulation is stateful, and
a job that survived a restart would be the first thing here that was.

A job is *not* a cache either. The same request submitted twice runs twice and produces the
same output hash, which is the property that makes the number checkable. Deduplicating on
that hash would be a cache, and it would need an invalidation story that assumptions.yaml
changing underneath it makes non-trivial.
"""

from __future__ import annotations

import threading
import time
import uuid
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, computed_field

from rebound.api.inputs import MerchantProfile, Sizing, sizing_plan
from rebound.api.service import SimulationResult, simulate
from rebound.config import Assumptions, load_assumptions


class JobState(StrEnum):
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class JobStatus(BaseModel):
    """What the dashboard polls. Carries elapsed time beside the estimate on purpose: the
    estimate is machine-dependent, and showing both lets a bad one be seen to be bad."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    state: JobState
    completed_steps: int = Field(ge=0)
    total_steps: int = Field(ge=0)
    elapsed_seconds: float = Field(ge=0.0)
    estimated_seconds: float = Field(ge=0.0)
    book_size: int = Field(gt=0)
    n_seeds: int = Field(gt=1)
    result: SimulationResult | None = None
    error: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def fraction_done(self) -> float:
        return self.completed_steps / self.total_steps if self.total_steps else 0.0


class _Job:
    """Mutable run state. Every field is written under `lock` and read under it, because a
    worker thread writes while the polling request reads."""

    def __init__(self, job_id: str, book_size: int, n_seeds: int, estimated: float) -> None:
        self.id = job_id
        self.lock = threading.Lock()
        self.state = JobState.RUNNING
        self.completed = 0
        self.total = 0
        self.started_at = time.monotonic()
        self.finished_at: float | None = None
        self.estimated = estimated
        self.book_size = book_size
        self.n_seeds = n_seeds
        self.result: SimulationResult | None = None
        self.error: str | None = None

    def elapsed(self) -> float:
        return (self.finished_at or time.monotonic()) - self.started_at

    def status(self) -> JobStatus:
        with self.lock:
            return JobStatus(
                id=self.id,
                state=self.state,
                completed_steps=self.completed,
                total_steps=self.total,
                elapsed_seconds=self.elapsed(),
                estimated_seconds=self.estimated,
                book_size=self.book_size,
                n_seeds=self.n_seeds,
                result=self.result,
                error=self.error,
            )


class JobRegistry:
    """Holds jobs for one API process. Bounded, so a long-lived process cannot accumulate
    completed runs until it runs out of memory."""

    def __init__(self, max_jobs: int = 32) -> None:
        self._jobs: dict[str, _Job] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._max = max_jobs

    def submit(
        self,
        profile: MerchantProfile,
        master_seed: int,
        sizing: Sizing,
        assumptions: Assumptions | None = None,
    ) -> JobStatus:
        base = assumptions or load_assumptions()
        # Raises BookSizeTooLargeError here, on the submitting request, rather than inside
        # a worker thread where it would surface as a failed job minutes later.
        plan = sizing_plan(base, sizing, profile.book_size)
        job = _Job(uuid.uuid4().hex, plan.book_size, plan.n_seeds, plan.estimated_seconds)
        self._register(job)
        thread = threading.Thread(
            target=self._run, args=(job, profile, master_seed, sizing, base), daemon=True
        )
        thread.start()
        return job.status()

    def _register(self, job: _Job) -> None:
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            while len(self._order) > self._max:
                self._jobs.pop(self._order.pop(0), None)

    def _run(
        self,
        job: _Job,
        profile: MerchantProfile,
        master_seed: int,
        sizing: Sizing,
        assumptions: Assumptions,
    ) -> None:
        def progress(done: int, total: int) -> None:
            with job.lock:
                job.completed, job.total = done, total

        try:
            result = simulate(profile, master_seed, sizing, assumptions, progress=progress)
        except Exception as exc:  # noqa: BLE001 - reported to the caller, not swallowed
            with job.lock:
                job.state, job.error = JobState.FAILED, f"{type(exc).__name__}: {exc}"
                job.finished_at = time.monotonic()
            return
        with job.lock:
            job.state, job.result = JobState.DONE, result
            job.finished_at = time.monotonic()

    def status(self, job_id: str) -> JobStatus | None:
        with self._lock:
            job = self._jobs.get(job_id)
        return job.status() if job is not None else None
