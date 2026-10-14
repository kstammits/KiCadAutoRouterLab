"""Coarse corridor discovery for tight-spot routing.

Unguided A* floods the whole board when the only walkable corridor
threads hundreds of tolled halo cells (DCCF VIN: a walkable path exists
but needs >2M expansions to find). A 4-connected BFS on a downsampled
blockage grid finds that corridor in ~0.1s; the detailed search is then
restricted to the dilated corridor instead of the open board.

Conservative by construction: a supercell is blocked when ANY subcell
is hard-blocked on ANY layer, so a found corridor always contains a
walkable fine path. A missed corridor (coarsening can seal fine
threads) only means "no guidance" — callers must fall back to the
regular windowed search, never fail fast on it.
"""

from __future__ import annotations

from collections import deque
from typing import Optional, Tuple

import numpy as np
from scipy import ndimage


def find_corridor(
    cost_grid: np.ndarray,
    secondary,
    start: Tuple[int, int, int],
    goal: Tuple[int, int, int],
    blocked_threshold: int,
    secondary_blocked: int,
    downsample: int = 4,
    dilate_cells: int = 12,
) -> Optional[np.ndarray]:
    """Boolean ``(H, W)`` corridor mask between two terminals, or None.

    ``start``/``goal`` are ``(col, row, layer_idx)`` grid terminals;
    layers are OR-ed (a via can switch layers wherever the detailed
    search deems legal — the corridor only guides *where*, not *how*).
    Returns None when no coarse path exists or inputs are degenerate.
    """
    S = max(1, int(downsample))
    n_layers, H, W = cost_grid.shape
    Hb, Wb = H // S, W // S
    if Hb == 0 or Wb == 0:
        return None

    sc, sr, _sl = start
    gc, gr, _gl = goal
    qs = (sc // S, sr // S)
    gs = (gc // S, gr // S)
    if not (0 <= qs[0] < Wb and 0 <= qs[1] < Hb):
        return None
    if not (0 <= gs[0] < Wb and 0 <= gs[1] < Hb):
        return None
    if qs == gs:
        return None

    # Conservative blockage: any hard-blocked subcell seals the supercell.
    blocked = np.zeros((Hb, Wb), dtype=bool)
    crop_h, crop_w = Hb * S, Wb * S
    for l in range(n_layers):
        hard = cost_grid[l, :crop_h, :crop_w] >= blocked_threshold
        sec = secondary[l] if secondary is not None else None
        if sec is not None:
            hard = hard | (sec[:crop_h, :crop_w] >= secondary_blocked)
        blocked |= hard.reshape(Hb, S, Wb, S).max(axis=(1, 3))
    if blocked[qs[1], qs[0]] or blocked[gs[1], gs[0]]:
        return None

    # 4-connectivity BFS with parent pointers (bounded: Hb x Wb cells).
    prev = np.zeros((Hb, Wb, 2), dtype=np.int32)
    seen = np.zeros((Hb, Wb), dtype=bool)
    dq = deque([qs])
    seen[qs[1], qs[0]] = True
    found = False
    while dq:
        cc, rr = dq.popleft()
        if (cc, rr) == gs:
            found = True
            break
        for dc, dr in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nc, nr = cc + dc, rr + dr
            if (
                0 <= nr < Hb
                and 0 <= nc < Wb
                and not seen[nr, nc]
                and not blocked[nr, nc]
            ):
                seen[nr, nc] = True
                prev[nr, nc] = (cc, rr)
                dq.append((nc, nr))
    if not found:
        return None

    # Paint the coarse path, dilate for maneuvering room (in cheap
    # coarse cells), then upscale to the fine grid.
    coarse = np.zeros((Hb, Wb), dtype=bool)
    node = gs
    while True:
        coarse[node[1], node[0]] = True
        if node == qs:
            break
        node = (int(prev[node[1], node[0], 0]), int(prev[node[1], node[0], 1]))
    dilate_super = max(1, int((int(dilate_cells) + S - 1) // S))
    coarse = np.asarray(
        ndimage.binary_dilation(coarse, iterations=dilate_super), dtype=bool
    )
    mask = np.zeros((H, W), dtype=bool)
    mask[:crop_h, :crop_w] = np.repeat(np.repeat(coarse, S, axis=0), S, axis=1)
    # Terminal fine cells are inside their (unblocked) supercells by
    # construction; ensure exact membership anyway.
    if 0 <= sr < H and 0 <= sc < W:
        mask[sr, sc] = True
    if 0 <= gr < H and 0 <= gc < W:
        mask[gr, gc] = True
    return mask
