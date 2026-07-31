from __future__ import annotations

import hashlib
from functools import lru_cache

import numpy as np

_SPAWN_KEY_BYTES = 4


@lru_cache(maxsize=8192)
def _label_key(label: str) -> int:
    """SHA-256, not `hash()`: PYTHONHASHSEED randomises `hash()` between processes and
    would silently break the byte-identical guarantee across runs."""
    digest = hashlib.sha256(label.encode("utf-8")).digest()
    return int.from_bytes(digest[:_SPAWN_KEY_BYTES], "big")


def stream(master_seed: int, *labels: str) -> np.random.Generator:
    """A generator addressed by name rather than by draw order.

    Two runs that reach the same labelled point get the same numbers regardless of
    what else has been drawn, which is what makes paired comparison across strategies
    possible: a retry landing on the same day for two strategies faces the same luck.
    """
    if not labels:
        raise ValueError("a stream needs at least one label")
    spawn_key = tuple(_label_key(label) for label in labels)
    return np.random.default_rng(np.random.SeedSequence(entropy=master_seed, spawn_key=spawn_key))
