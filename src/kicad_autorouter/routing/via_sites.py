"""Precomputed via-landing mask: hoist the clearance scan out of A*.

``SingleNetRouter._via_landing_blocked`` scanned a ``radius``-disc on every
layer for every via candidate — up to twice per expansion, in pure Python.
The predicate depends only on static-per-net inputs (cost grid, secondary
exclusion rings, via-block solder areas, clearance radius), so it is
evaluated once per net here with identical disc semantics, by reverse
stamping: each obstacle cell forbids all via centers within ``radius``.

Returns ``(H, W)`` bool where True = via landing allowed (before the
THT-free / via-block checks the search still applies per cell).
"""

from __future__ import annotations

import numpy as np
from scipy import ndimage


def _disc_structure(radius: int) -> np.ndarray:
    """Boolean ``(2*radius+1, 2*radius+1)`` disc (``dr²+dc² <= r²``).

    Matches the historical per-cell ``_stamp`` scan exactly (integer
    arithmetic, no float error): a via center is forbidden when an
    obstacle cell lies within the Euclidean ``radius`` disc.
    """
    y, x = np.ogrid[-radius : radius + 1, -radius : radius + 1]
    return (x * x + y * y <= radius * radius)


def build_via_ok_mask(
    cost_grid: np.ndarray,
    via_block_mask: np.ndarray,
    tht_via_mask: np.ndarray,
    secondary,
    radius: int,
    blocked_threshold: int,
    secondary_blocked: int,
) -> np.ndarray:
    """Boolean ``(H, W)`` mask of allowed via centers."""
    n_layers, H, W = cost_grid.shape
    radius = max(0, int(radius))
    if radius <= 0:
        return np.ones((H, W), dtype=bool)

    # Union of all obstacle sources across layers (2D): a via halo on
    # any layer forbids the (row, col) center on every layer. OR-then-
    # dilate equals dilate-then-OR, so one dilation replaces the old
    # per-obstacle-cell Python ``_stamp`` loop with identical results.
    obstacle = np.zeros((H, W), dtype=bool)
    for l in range(n_layers):
        layer = cost_grid[l]
        sec = secondary[l] if secondary is not None else None
        if sec is not None:
            obstacle |= (layer >= blocked_threshold) | (sec >= secondary_blocked)
        else:
            obstacle |= layer >= blocked_threshold
    obstacle |= np.asarray(via_block_mask, dtype=bool)

    if not obstacle.any():
        forbidden = np.zeros((H, W), dtype=bool)
    else:
        forbidden = ndimage.binary_dilation(
            obstacle, structure=_disc_structure(radius)
        )
        forbidden = np.asarray(forbidden, dtype=bool)

    ok = ~forbidden
    # Drilled holes stay legal hop sites (the barrel already exists).
    ok |= np.asarray(tht_via_mask, dtype=bool)
    return ok
