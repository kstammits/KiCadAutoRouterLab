"""A* neighbor expansion (numba/python paths, split from router.py)."""
from __future__ import annotations
import numpy as np
from ._numba import njit



@njit
def _expand_neighbors_nb(
    col, row, lay,
    cost_grid, secondary, has_secondary,
    allow_mask, has_allow,
    passable, has_passable,
    tht_via_mask, via_block_mask, via_ok, has_via_ok,
    via_cost_cells,
    base_cost, edge_p, high_p, pour_p, court_p,
    blocked_thr, secondary_blocked, pour_cells,
    c0, r0, c1, r1, n_layers,
    p_ok, p_cost, v_ok, v_cost, v_fallback,
):
    """Evaluate all six neighbors of one A* pop (numba path).

    planar dirs [(1,0),(-1,0),(0,1),(0,-1)] fill p_ok/p_cost[4]; via
    layers [lay-1, lay+1] fill v_ok/v_cost/v_fallback[2]. v_fallback=1 means
    the caller must run the slow exact ``_via_landing_blocked`` halo scan
    (only when no precomputed via_ok mask was supplied). Semantics match
    the inlined checks in ``SingleNetRouter._a_star`` exactly.
    """
    for d in range(4):
        if d == 0:
            dc = 1
            dr = 0
        elif d == 1:
            dc = -1
            dr = 0
        elif d == 2:
            dc = 0
            dr = 1
        else:
            dc = 0
            dr = -1
        ncol = col + dc
        nrow = row + dr
        if ncol < c0 or ncol > c1 or nrow < r0 or nrow > r1:
            p_ok[d] = False
            continue
        if has_allow and not allow_mask[nrow, ncol]:
            p_ok[d] = False
            continue
        kept = has_passable and passable[lay, nrow, ncol]
        cell_val = cost_grid[lay, nrow, ncol]
        if cell_val >= blocked_thr and not kept:
            p_ok[d] = False
            continue
        sec = secondary[lay, nrow, ncol] if has_secondary else 0
        if has_secondary and sec >= secondary_blocked and not kept:
            p_ok[d] = False
            continue
        cc = base_cost
        if cell_val >= 80:
            cc += edge_p
        elif cell_val >= 50:
            cc += high_p
        elif cell_val >= pour_cells:
            cc += pour_p
        elif cell_val >= 25:
            cc += court_p
        if has_secondary:
            cc += sec
        p_ok[d] = True
        p_cost[d] = cc
    for vi in range(2):
        nlay = lay - 1 if vi == 0 else lay + 1
        if nlay < 0 or nlay >= n_layers:
            v_ok[vi] = False
            continue
        if has_allow and not allow_mask[row, col]:
            v_ok[vi] = False
            continue
        kept = has_passable and passable[nlay, row, col]
        if cost_grid[nlay, row, col] >= blocked_thr and not kept:
            v_ok[vi] = False
            continue
        if has_secondary and secondary[nlay, row, col] >= secondary_blocked and not kept:
            v_ok[vi] = False
            continue
        if tht_via_mask[row, col]:
            v_ok[vi] = True
            v_cost[vi] = 0
            v_fallback[vi] = False
            continue
        if via_block_mask[row, col] and not kept:
            v_ok[vi] = False
            continue
        if has_via_ok:
            if not via_ok[row, col] and not kept:
                v_ok[vi] = False
                continue
            v_ok[vi] = True
            v_fallback[vi] = False
            v_cost[vi] = via_cost_cells + (secondary[nlay, row, col] if has_secondary else 0)
        else:
            v_ok[vi] = True
            v_fallback[vi] = True
            v_cost[vi] = via_cost_cells + (secondary[nlay, row, col] if has_secondary else 0)


def _expand_neighbors_py(
    col, row, lay,
    cost_grid, secondary, has_secondary,
    allow_mask, has_allow,
    passable, has_passable,
    tht_via_mask, via_block_mask, via_ok, has_via_ok,
    via_cost_cells,
    base_cost, edge_p, high_p, pour_p, court_p,
    blocked_thr, secondary_blocked, pour_cells,
    c0, r0, c1, r1, n_layers,
    p_ok, p_cost, v_ok, v_cost, v_fallback,
):
    """Pure-Python mirror of _expand_neighbors_nb (fallback + A/B baseline)."""
    dirs = ((1, 0), (-1, 0), (0, 1), (0, -1))
    for d, (dc, dr) in enumerate(dirs):
        ncol, nrow = col + dc, row + dr
        if not (c0 <= ncol <= c1 and r0 <= nrow <= r1):
            p_ok[d] = False
            continue
        if has_allow and not allow_mask[nrow, ncol]:
            p_ok[d] = False
            continue
        kept = bool(has_passable and passable[lay, nrow, ncol])
        cell_val = int(cost_grid[lay, nrow, ncol])
        if cell_val >= blocked_thr and not kept:
            p_ok[d] = False
            continue
        sec = int(secondary[lay, nrow, ncol]) if has_secondary else 0
        if has_secondary and sec >= secondary_blocked and not kept:
            p_ok[d] = False
            continue
        cc = base_cost
        if cell_val >= 80:
            cc += edge_p
        elif cell_val >= 50:
            cc += high_p
        elif cell_val >= pour_cells:
            cc += pour_p
        elif cell_val >= 25:
            cc += court_p
        if has_secondary:
            cc += sec
        p_ok[d] = True
        p_cost[d] = cc
    for vi, nlay in enumerate((lay - 1, lay + 1)):
        if not (0 <= nlay < n_layers):
            v_ok[vi] = False
            continue
        if has_allow and not allow_mask[row, col]:
            v_ok[vi] = False
            continue
        kept = bool(has_passable and passable[nlay, row, col])
        if int(cost_grid[nlay, row, col]) >= blocked_thr and not kept:
            v_ok[vi] = False
            continue
        if has_secondary and int(secondary[nlay, row, col]) >= secondary_blocked and not kept:
            v_ok[vi] = False
            continue
        if tht_via_mask[row, col]:
            v_ok[vi] = True
            v_cost[vi] = 0
            v_fallback[vi] = False
            continue
        if via_block_mask[row, col] and not kept:
            v_ok[vi] = False
            continue
        if has_via_ok:
            if not via_ok[row, col] and not kept:
                v_ok[vi] = False
                continue
            v_ok[vi] = True
            v_fallback[vi] = False
            v_cost[vi] = via_cost_cells + (int(secondary[nlay, row, col]) if has_secondary else 0)
        else:
            v_ok[vi] = True
            v_fallback[vi] = True
            v_cost[vi] = via_cost_cells + (int(secondary[nlay, row, col]) if has_secondary else 0)
