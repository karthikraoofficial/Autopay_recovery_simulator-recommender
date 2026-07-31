from __future__ import annotations

import numpy as np

from rebound.config import Assumptions

_TOLERANCE = 1e-9


def share_vector(assumptions: Assumptions, keys: tuple[str, ...]) -> np.ndarray:
    """Read a set of share keys as a probability vector, in the caller's key order.

    The order is the caller's tuple, never dict iteration order, because these vectors
    feed seeded `Generator.choice` calls: reordering them silently changes which
    customer gets which bank for a fixed seed.
    """
    weights = np.array([float(assumptions.value(key)) for key in keys], dtype=float)
    if (weights < 0.0).any():
        raise ValueError(f"negative share in {keys}")
    total = float(weights.sum())
    if abs(total - 1.0) > _TOLERANCE:
        raise ValueError(f"shares {keys} must sum to 1.0, got {total}")
    return weights / total
