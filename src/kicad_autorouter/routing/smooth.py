"""Any-angle path smoothing (string-pulling) for routed nets.

The A* router moves 4-directionally, so angled runs come out as staircases:
one grid step per track segment. This module post-processes a successful
route's grid path, skipping intermediate nodes wherever the straight
line-of-sight is walkable. Surviving corners keep their grid coordinates,
so output stays as straight KiCad ``(segment)`` tracks — just fewer,
longer, any-angle ones that pack tighter.

Safety rules:
- Only shortcuts within a single layer run; via (layer-transition) nodes
  are never moved or removed.
- A shortcut is accepted only if every cell under its corridor footprint
  is FREE. Penalized-but-crossable costs (HIGH_COST pad rings,
  EDGE_KEEPOUT) also block shortcuts — the router may wade through them
  deliberately, but a blind shortcut would clip other nets' clearance
  (this showed up as DRC shorts/clearance errors). The only exception is
  the routed net's own pads/terminals, passed via ``soft_mask``.
- Cells within one cell of either shortcut endpoint are exempt, mirroring
  the router's allowance for terminals sitting on blocked pad cells.
"""

from __future__ import annotations

import math
from typing import List, Optional, Set, Tuple

import numpy as np

# Cost bands (mirrors routing/obstacles.py + router.CostMap).
BLOCKED_THRESHOLD = 100  # impassable: tracks, vias, zones, courtyards
SOFT_THRESHOLD = 50  # HIGH_COST pad rings / EDGE_KEEPOUT: no shortcut through


def own_net_soft_mask(model, grid, net_name: str, clearance_mm: float, track_half_mm: float):
    """Boolean ``(n_layers, H, W)`` mask of the net's own pad areas.

    Inside the mask, penalized cells may be crossed by shortcuts (the
    track must escape its own pads). Everywhere else shortcuts require
    FREE cells. BLOCKED cells are impassable regardless of the mask.
    """
    n_layers = len(grid.layers)
    mask = np.zeros((n_layers, grid.height_cells, grid.width_cells), dtype=bool)
    res_mm = grid.resolution_mm
    if res_mm <= 0:
        return mask
    from .grid import board_to_grid

    for fp in model.footprints:
        for pad in fp.pads:
            if not pad.net_name or pad.net_name != net_name:
                continue
            try:
                pad_half = max(float(s) for s in pad.size_mm) / 2.0
            except (TypeError, ValueError):
                pad_half = 0.0
            radius = max(1, int(math.ceil((pad_half + clearance_mm + track_half_mm) / res_mm)))
            col, row = board_to_grid(grid, pad.position.x_mm, pad.position.y_mm)
            layers = (0, 1) if pad.is_through_hole else (0 if fp.layer == "F.Cu" else 1,)
            for li in layers:
                if li >= n_layers:
                    continue
                r0, r1 = max(0, row - radius), min(grid.height_cells - 1, row + radius)
                c0, c1 = max(0, col - radius), min(grid.width_cells - 1, col + radius)
                mask[li, r0:r1 + 1, c0:c1 + 1] = True
    return mask


def _footprint_clear(
    cost_layer,
    soft_layer,
    c_f: float,
    r_f: float,
    radius: int,
    blocked_threshold: int,
) -> bool:
    """Check the ``(2*radius+1)`` square footprint around a sample point.

    Cells >= ``blocked_threshold`` are always impassable. Penalized cells
    (>= SOFT_THRESHOLD: pad rings, edge keepout) are impassable unless
    covered by ``soft_layer`` (the routed net's own pad areas).
    """
    H, W = cost_layer.shape
    c0 = int(math.floor(c_f))
    r0 = int(math.floor(r_f))
    for rr in range(r0 - radius, r0 + radius + 1):
        if rr < 0 or rr >= H:
            return False
        for cc in range(c0 - radius, c0 + radius + 1):
            if cc < 0 or cc >= W:
                return False
            val = int(cost_layer[rr, cc])
            if val >= blocked_threshold:
                return False
            if val >= SOFT_THRESHOLD and (soft_layer is None or not soft_layer[rr, cc]):
                return False
    return True


def _line_walkable(
    c1: int,
    r1: int,
    c2: int,
    r2: int,
    cost_layer,
    soft_layer,
    blocked_threshold: int,
    corridor_radius: int,
) -> bool:
    """Check an any-angle straight line between two grid cells.

    Samples at half-cell steps; each sample's corridor footprint must be
    clear, except near the endpoints (pad allowance).
    """
    dist = math.hypot(c2 - c1, r2 - r1)
    if dist == 0:
        return True
    steps = max(1, int(math.ceil(dist * 2)))
    for s in range(steps + 1):
        t = s / steps
        c_f = c1 + (c2 - c1) * t
        r_f = r1 + (r2 - r1) * t
        # Terminal allowance: skip the blockage check within one cell of
        # either endpoint (pads live on blocked cells).
        if (
            math.hypot(c_f - c1, r_f - r1) < 1.0
            or math.hypot(c_f - c2, r_f - r2) < 1.0
        ):
            continue
        if not _footprint_clear(cost_layer, soft_layer, c_f, r_f, corridor_radius, blocked_threshold):
            return False
    return True


def _smooth_run(
    run: List[Tuple[int, int, int]],
    cost_layer,
    soft_layer,
    blocked_threshold: int,
    corridor_radius: int,
) -> List[Tuple[int, int, int]]:
    """Greedy furthest-visible shortcutting over one same-layer run."""
    n = len(run)
    if n < 3:
        return list(run)
    out = [run[0]]
    i = 0
    while i < n - 1:
        # Furthest reachable node wins (fewest, longest segments).
        j = n - 1
        while j > i + 1:
            if _line_walkable(
                run[i][0], run[i][1], run[j][0], run[j][1],
                cost_layer, soft_layer, blocked_threshold, corridor_radius,
            ):
                break
            j -= 1
        out.append(run[j])
        i = j
    return out


def smooth_grid_path(
    path: List[Tuple[int, int, int]],
    cost_grid,
    blocked_threshold: int = 100,
    corridor_radius: int = 0,
    pinned: Optional[Set[Tuple[int, int]]] = None,
    soft_mask=None,
) -> List[Tuple[int, int, int]]:
    """Shortcut a routed grid path within each same-layer run.

    Args:
        path: ``[(col, row, layer_idx)]`` from :class:`SingleNetRouter`.
        cost_grid: ``(n_layers, H, W)`` occupancy grid (pre-route state:
            must NOT yet contain this path's own corridor).
        blocked_threshold: cells >= this are impassable.
        corridor_radius: footprint radius in cells around the shortcut line
            (same convention as ``_apply_route_to_grid``: ``half_width-1``).
        pinned: ``{(col, row)}`` terminals that must survive smoothing.
            MST-joined multi-pad paths pass *through* middle terminals; a
            shortcut skipping one would disconnect that pad. Pinned
            interior nodes force run boundaries.
        soft_mask: optional ``(n_layers, H, W)`` boolean array (see
            :func:`own_net_soft_mask`). Penalized cells are crossable only
            where it is True. When None, every penalized cell blocks.

    Returns:
        New path with identical endpoints, identical via nodes/layers, all
        pinned nodes, and fewer intermediate nodes. Never returns an empty
        path for non-empty input.
    """
    if not path:
        return []
    if len(path) < 3:
        return list(path)
    n_layers = cost_grid.shape[0]
    pinned = pinned or set()

    # Split into maximal same-layer runs. A layer transition ends the
    # current run and starts a new one, so smoothing can never shortcut
    # across a via (which would move the via position). Concatenating the
    # smoothed runs preserves every via edge exactly: each run keeps its
    # endpoints, and adjacent runs join at the original via nodes.
    # Pinned middle terminals likewise force boundaries (shared node in
    # both runs — same layer, so sharing is safe here).
    runs: List[List[Tuple[int, int, int]]] = [[path[0]]]
    for idx, node in enumerate(path[1:], start=1):
        last_node = runs[-1][-1]
        if node[2] != last_node[2]:
            runs.append([node])
        else:
            runs[-1].append(node)
            is_interior = idx < len(path) - 1
            if is_interior and (node[0], node[1]) in pinned:
                runs.append([node])

    smoothed: List[Tuple[int, int, int]] = []
    for run in runs:
        layer = run[0][2]
        if len(run) < 3 or not (0 <= layer < n_layers):
            new_run = list(run)
        else:
            cost_layer = cost_grid[layer]
            soft_layer = soft_mask[layer] if soft_mask is not None else None
            new_run = _smooth_run(run, cost_layer, soft_layer, blocked_threshold, max(0, corridor_radius))
        if smoothed and new_run and new_run[0] == smoothed[-1]:
            # Shared pinned node: dedupe. (Via boundaries are disjoint
            # node pairs, so they are never merged here.)
            new_run = new_run[1:]
        smoothed.extend(new_run)
    return smoothed


def smoothed_wire_mm(path: List[Tuple[int, int, int]], resolution_mm: float) -> float:
    """Euclidean wire length of a (possibly shortcut) grid path in mm."""
    total = 0.0
    for k in range(len(path) - 1):
        c1, r1, _ = path[k]
        c2, r2, _ = path[k + 1]
        total += math.hypot(c2 - c1, r2 - r1)
    return total * resolution_mm
