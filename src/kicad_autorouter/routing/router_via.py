"""Via-transition masks (split from router.py)."""
from __future__ import annotations
from typing import Tuple
import numpy as np
from scipy import ndimage
from ..board_model import BoardModel
from .grid import RoutingGrid



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
    # dilate equals dilate-then-OR, so a single dilation covers all layers.
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


def build_via_ok_cached(
    entry,
    cost_grid: np.ndarray,
    secondary,
    radius: int,
    blocked_threshold: int,
    secondary_blocked: int,
) -> np.ndarray:
    """Content-memoized twin of :func:`build_via_ok_mask` over a cache entry.

    ``entry`` is an ``obstacles._ObstacleEntry`` carrying the static
    via-block/THT masks plus a bounded memo of computed masks keyed by
    content digests. Misses run the original single-dilation body (never
    slower than the uncached path); hits serve retries, chunk loops, and
    trial replays with identical inputs in milliseconds.
    """
    n_layers, H, W = cost_grid.shape
    blocked2d = np.zeros((H, W), dtype=bool)
    for l in range(n_layers):
        blocked2d |= cost_grid[l] >= blocked_threshold
    blocked2d |= np.asarray(entry.via_block_mask, dtype=bool)
    key = (
        hash(blocked2d.tobytes()),
        hash(secondary.tobytes()) if secondary is not None else None,
        max(0, int(radius)),
    )
    memo = entry.via_ok_memo
    hit = memo.get(key)
    if hit is not None and hit.shape == (H, W):
        return hit.copy()
    ok = build_via_ok_mask(
        cost_grid,
        entry.via_block_mask,
        entry.tht_via_mask,
        secondary,
        radius,
        blocked_threshold,
        secondary_blocked,
    )
    if len(memo) >= 4:
        memo.clear()
    memo[key] = ok
    # Hand out a private copy: the memoized array must stay pristine.
    return ok.copy()
