"""What this process actually is, so a stale one announces itself.

Twice now a server left running across a phase boundary has been diagnosed from *which
paths returned 404* — which points at routing, the proxy, or the front end, and is wrong
every time. The process knew the answer all along and had no way to say it.

So `/health` reports the commit the process was **started from** and the configuration it
would use for the next request. A version string does not do this job: someone has to
already know which version is current for `0.1.0` to mean anything, and it is stamped by
whoever last remembered to bump it. A commit is checkable against the tree in front of you
without knowing anything.

Nothing here is a result. `commit`, `dirty` and `started_at` are facts about a process, and
they never enter an output hash or any comparison a simulation makes.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from rebound.config import load_assumptions

REPO_ROOT = Path(__file__).resolve().parents[3]


def _git(*args: str) -> str | None:
    """A git query, or None where git cannot answer — never an exception.

    A health endpoint that fails when the checkout is unusual is a health endpoint that
    tells you nothing at the moment you most need it.
    """
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip() or None


# Read once, at import. That is deliberate and is the whole point: these describe the code
# this process is *running*, not the code currently on disk. A process whose tree has moved
# on underneath it must keep reporting what it booted with, or it cannot be caught being
# stale.
COMMIT: str | None = _git("rev-parse", "HEAD")
DIRTY: bool | None = None if COMMIT is None else bool(_git("status", "--porcelain"))
STARTED_AT: datetime = datetime.now(UTC)


class Health(BaseModel):
    """Enough to tell whether this process is the one you think it is."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # None where git could not answer. Said as "unknown" rather than guessed at, because a
    # fabricated commit would make the comparison this endpoint exists for silently wrong.
    commit: str | None = None
    # Uncommitted changes in the working tree the process was started from. A matching
    # commit with `dirty: true` means the comparison cannot prove very much.
    dirty: bool | None = None
    started_at: AwareDatetime
    # Computed per request, not at import, because `load_assumptions()` re-reads the file
    # on every call: editing assumptions.yaml takes effect without a restart. So this is
    # what the *next* request will use, which is the useful thing to report.
    config_fingerprint: str = Field(min_length=1)


def health() -> Health:
    return Health(
        commit=COMMIT,
        dirty=DIRTY,
        started_at=STARTED_AT,
        config_fingerprint=load_assumptions().fingerprint(),
    )
