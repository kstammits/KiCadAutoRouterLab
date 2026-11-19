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


class ViaOkTracker:
    """Run-level incremental via-landing mask.

    :func:`build_via_ok_mask` costs one full-board ``binary_dilation``
    per net (~0.3-0.5 s on DCCF-class boards), yet consecutive nets'
    dilation inputs differ by <0.2% of cells (own-THT punch swap,
    own-net secondary carve-out, newly stamped copper). This tracker
    keeps the last ``(blocked_input, forbidden)`` pair per dilation
    radius and recomputes the dilation only over ``bbox(changed) + 2r``
    instead of the whole board:

    - ``dilate(A | B) == dilate(A) | dilate(B)``, so grown inputs need
      only an OR of the crop dilation;
    - shrunk inputs (punch swaps, per-net secondary rings) are handled
      by recomputing the crop from true input values and writing back
      exactly the cells whose radius-disc lies inside the crop
      (``bbox(changed) + r``), which provably covers every affected
      output cell while border-approximation error stays outside the
      write-back region.

    Output is bit-identical to :func:`build_via_ok_mask` on the same
    inputs. One tracker serves one ``route_raw`` run: footprints (and
    hence ``via_block``/``tht``) are fixed within a run, and the
    tracker never crosses runs. ``via_block``/``tht`` arrays are read,
    never mutated; the returned mask is owned by the tracker and must
    be treated read-only by callers (same contract as ``cost_grid``).
    """

    def __init__(self):
        self._state = {}  # radius -> [blocked_input, forbidden]

    def build(
        self,
        cost_grid: np.ndarray,
        via_block_mask: np.ndarray,
        tht_via_mask: np.ndarray,
        secondary,
        radius: int,
        blocked_threshold: int,
        secondary_blocked: int,
    ) -> np.ndarray:
        """Via-ok mask for these inputs (incremental after the first call)."""
        n_layers, H, W = cost_grid.shape
        radius = max(0, int(radius))
        if radius <= 0:
            return np.ones((H, W), dtype=bool)
        blocked = np.zeros((H, W), dtype=bool)
        for l in range(n_layers):
            layer = cost_grid[l]
            sec = secondary[l] if secondary is not None else None
            if sec is not None:
                blocked |= (layer >= blocked_threshold) | (sec >= secondary_blocked)
            else:
                blocked |= layer >= blocked_threshold
        blocked |= np.asarray(via_block_mask, dtype=bool)

        entry = self._state.get(radius)
        if entry is None:
            forbidden = np.asarray(
                ndimage.binary_dilation(
                    blocked, structure=_disc_structure(radius)
                ),
                dtype=bool,
            )
            self._state[radius] = [blocked, forbidden]
        else:
            prev_blocked, forbidden = entry
            if prev_blocked.shape != (H, W) or forbidden.shape != (H, W):
                # Grid changed under the tracker (should not happen
                # within a run): fall back to a full rebuild.
                forbidden = np.asarray(
                    ndimage.binary_dilation(
                        blocked, structure=_disc_structure(radius)
                    ),
                    dtype=bool,
                )
                self._state[radius] = [blocked, forbidden]
            else:
                changed = blocked ^ prev_blocked
                if changed.any():
                    rows, cols = np.where(changed)
                    r0, r1 = int(rows.min()), int(rows.max())
                    c0, c1 = int(cols.min()), int(cols.max())
                    R0, R1 = max(0, r0 - 2 * radius), min(H - 1, r1 + 2 * radius)
                    C0, C1 = max(0, c0 - 2 * radius), min(W - 1, c1 + 2 * radius)
                    sub = blocked[R0:R1 + 1, C0:C1 + 1]
                    d = np.asarray(
                        ndimage.binary_dilation(
                            sub, structure=_disc_structure(radius)
                        ),
                        dtype=bool,
                    )
                    # Write back exactly the cells whose radius-disc
                    # lies inside the crop: bbox(changed)+r clamped to
                    # the crop. Cells farther from any change keep the
                    # old dilation (their discs hold no changed input).
                    w0, w1 = max(R0, r0 - radius), min(R1, r1 + radius)
                    v0, v1 = max(C0, c0 - radius), min(C1, c1 + radius)
                    forbidden[w0:w1 + 1, v0:v1 + 1] = d[
                        w0 - R0:w1 - R0 + 1, v0 - C0:v1 - C0 + 1
                    ]
                    entry[0] = blocked
        ok = ~self._state[radius][1] | np.asarray(
            tht_via_mask, dtype=bool
        )
        return np.ascontiguousarray(ok)


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
