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

import math

import numpy as np


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

    forbidden = np.zeros((H, W), dtype=bool)
    r2 = radius * radius

    def _stamp(rr: int, cc: int) -> None:
        for r in range(max(0, rr - radius), min(H, rr + radius + 1)):
            dr = r - rr
            dc_max = int(math.sqrt(max(0, r2 - dr * dr)))
            c0 = max(0, cc - dc_max)
            c1 = min(W - 1, cc + dc_max)
            forbidden[r, c0 : c1 + 1] = True

    for l in range(n_layers):
        layer = cost_grid[l]
        sec = secondary[l] if secondary is not None else None
        rows, cols = np.nonzero(
            (layer >= blocked_threshold)
            | ((sec >= secondary_blocked) if sec is not None else False)
        )
        for rr, cc in zip(rows.tolist(), cols.tolist()):
            _stamp(int(rr), int(cc))

    rows, cols = np.nonzero(np.asarray(via_block_mask, dtype=bool))
    for rr, cc in zip(rows.tolist(), cols.tolist()):
        _stamp(int(rr), int(cc))

    ok = ~forbidden
    # Drilled holes stay legal hop sites (the barrel already exists).
    ok |= np.asarray(tht_via_mask, dtype=bool)
    return ok
