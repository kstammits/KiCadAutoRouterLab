"""Tests for any-angle path smoothing (routing/smooth.py)."""

import math

import numpy as np

from kicad_autorouter.routing.smooth import (
    _line_walkable,
    own_net_soft_mask,
    smooth_grid_path,
    smoothed_wire_mm,
)


def _open(layers=1, size=30):
    return np.zeros((layers, size, size), dtype=np.int16)


def test_staircase_collapses_in_open_field():
    grid = _open()
    path = [(5, 5, 0)]
    for i in range(1, 11):
        path.append((5 + i, 5, 0))
    for i in range(1, 11):
        path.append((15, 5 + i, 0))
    smoothed = smooth_grid_path(path, grid)
    assert smoothed[0] == path[0]
    assert smoothed[-1] == path[-1]
    assert len(smoothed) < len(path)
    assert len(smoothed) == 2  # straight diagonal line-of-sight


def test_blocked_wall_prevents_shortcut():
    grid = _open()
    grid[0, 8:13, 8:13] = 100  # BLOCKED square in the middle
    path = [(5, 10, 0), (10, 10, 0), (10, 5, 0), (15, 5, 0)]
    smoothed = smooth_grid_path(path, grid)
    # Endpoints preserved, and the full direct shortcut must NOT happen:
    # (5,10)->(15,5) crosses the wall.
    assert smoothed[0] == path[0]
    assert smoothed[-1] == path[-1]
    assert len(smoothed) > 2


def test_wall_hugging_staircase_survives_and_is_fixpoint():
    """A run hugging a blocked wall keeps its stairsteps (DRC protection).

    Regression for the "stairsteps after string pull" report: pair-skip
    chords along the wall clip hard copper, so no pull — first, second,
    or angle-snapped — may take them. Locks in that smoothing is already
    at fixpoint here (another string-pull attempt is a no-op).
    """
    grid = _open(size=40)
    grid[0, :, 20] = 100  # blocked wall one cell east of the run
    path = [(19, r, 0) for r in range(5, 15)]
    smoothed = smooth_grid_path(path, grid, corridor_radius=1)
    assert smoothed[0] == path[0]
    assert smoothed[-1] == path[-1]
    # The wall-hugging steps survive: every longer chord clips the wall.
    assert len(smoothed) > 2
    assert not _line_walkable(
        smoothed[0][0], smoothed[0][1], smoothed[-1][0], smoothed[-1][1],
        grid[0], None, 100, 1,
    )
    # Second pull with identical gates changes nothing.
    assert smooth_grid_path(smoothed, grid, corridor_radius=1) == smoothed


def test_via_nodes_never_moved_or_removed():
    grid = _open(layers=2)
    path = [(5, 5, 0), (8, 5, 0), (8, 5, 1), (12, 8, 1)]
    smoothed = smooth_grid_path(path, grid)
    assert smoothed == path  # short runs: nothing to shortcut, vias exact
    # Longer runs still keep the via edge exactly.
    path2 = [(0, 0, 0), (5, 0, 0), (5, 0, 1), (10, 5, 1), (15, 5, 1)]
    smoothed2 = smooth_grid_path(path2, grid)
    assert (5, 0, 0) in smoothed2
    assert (5, 0, 1) in smoothed2
    idx = smoothed2.index((5, 0, 0))
    assert smoothed2[idx + 1] == (5, 0, 1)  # via edge intact


def test_corridor_radius_blocks_tight_shortcut():
    # A blocked cell adjacent to the diagonal: passable with radius 0,
    # blocked once the track footprint (radius 1) is considered.
    grid = _open()
    grid[0, 6, 8] = 100
    path = [(5, 5, 0), (10, 5, 0), (10, 10, 0)]
    assert len(smooth_grid_path(path, grid, corridor_radius=0)) == 2
    assert len(smooth_grid_path(path, grid, corridor_radius=1)) > 2


def test_endpoints_and_short_paths_untouched():
    grid = _open()
    assert smooth_grid_path([], grid) == []
    assert smooth_grid_path([(1, 1, 0)], grid) == [(1, 1, 0)]
    two = [(1, 1, 0), (5, 5, 0)]
    assert smooth_grid_path(two, grid) == two


def test_high_cost_ring_blocks_shortcut_without_mask():
    # Another net's pad clearance ring (HIGH_COST) must block shortcuts —
    # blindly cutting through it caused DRC clearance/short violations.
    grid = _open()
    grid[0, 7:10, 7:10] = 50
    path = [(5, 5, 0), (10, 5, 0), (10, 10, 0)]
    assert len(smooth_grid_path(path, grid)) > 2


def test_soft_mask_allows_escape_through_own_pads():
    # The routed net's own pad ring is crossable (the track must escape).
    grid = _open()
    grid[0, 4:8, 4:8] = 50
    mask = np.zeros_like(grid, dtype=bool)
    mask[0, 4:8, 4:8] = True
    path = [(5, 5, 0), (10, 5, 0), (12, 8, 0)]
    assert len(smooth_grid_path(path, grid, soft_mask=mask)) == 2
    assert len(smooth_grid_path(path, grid)) > 2


def test_pinned_middle_terminal_survives():
    # MST-joined path through a middle pad: smoothing must not skip it,
    # or the pad would be left unconnected.
    grid = _open()
    path = [(0, 0, 0), (5, 0, 0), (10, 0, 0), (10, 5, 0), (10, 10, 0)]
    pinned = {(5, 0)}
    smoothed = smooth_grid_path(path, grid, pinned=pinned)
    assert (5, 0, 0) in smoothed
    # Without pinning the middle node may be skipped (documenting why
    # the pipeline always pins terminals).
    unpinned = smooth_grid_path(path, grid)
    assert unpinned[0] == path[0] and unpinned[-1] == path[-1]


def test_smoothed_wire_mm_shorter_than_staircase():
    grid = _open()
    path = [(5, 5, 0)]
    for i in range(1, 11):
        path.append((5 + i, 5, 0))
    for i in range(1, 11):
        path.append((15, 5 + i, 0))
    raw = smoothed_wire_mm(path, 0.1)
    smoothed = smooth_grid_path(path, grid)
    smooth_len = smoothed_wire_mm(smoothed, 0.1)
    assert smooth_len < raw
    assert smooth_len > 0


def test_courtyard_outline_allows_shortcut():
    """COURTYARD (25) cells must not block smoothing shortcuts.

    A 45-degree rotated SOT-23 courtyard appears as a diamond in the grid.
    The smoother should shortcut through it (COURTYARD < SOFT_THRESHOLD).
    """
    grid = _open()
    # Diamond-shaped courtyard outline (45-degree rotated square)
    diamond = [(15, 10), (20, 15), (15, 20), (10, 15), (15, 10)]
    for i in range(len(diamond) - 1):
        c1, r1 = diamond[i]
        c2, r2 = diamond[i + 1]
        dc = abs(c2 - c1)
        dr = abs(r2 - r1)
        steps = max(dc, dr)
        for s in range(steps + 1):
            t = s / steps if steps else 0
            c = round(c1 + (c2 - c1) * t)
            r = round(r1 + (r2 - r1) * t)
            grid[0, r, c] = 25  # COURTYARD

    # Horizontal path through the diamond center — crosses outline at (10,15) and (20,15)
    path = [(5, 15, 0), (10, 15, 0), (15, 15, 0), (20, 15, 0), (25, 15, 0)]
    smoothed = smooth_grid_path(path, grid)
    assert len(smoothed) < len(path)
    assert smoothed[0] == path[0]
    assert smoothed[-1] == path[-1]


def test_steiner_branch_endpoints_preserved():
    """Branch endpoints (Steiner connections) must survive smoothing.

    In multi-terminal nets, MST routing creates branches where later branches
    attach to interior points of earlier branches (via goal_mask). Each branch
    endpoint is a connection point that must be pinned during smoothing, or
    the branch would shortcut past it and disconnect the later branch.
    """
    grid = _open()

    # Branch 1: A(5,5) -> B(15,5) horizontal line
    # Branch 2: C(10,10) connects to (10,5) on Branch 1 (Steiner point)
    branch1 = [(5, 5, 0), (6, 5, 0), (7, 5, 0), (8, 5, 0), (9, 5, 0),
               (10, 5, 0), (11, 5, 0), (12, 5, 0), (13, 5, 0), (14, 5, 0), (15, 5, 0)]
    branch2 = [(10, 10, 0), (10, 9, 0), (10, 8, 0), (10, 7, 0), (10, 6, 0), (10, 5, 0)]

    # Old behavior: only original terminals pinned
    pinned_terminals = {(5, 5), (15, 5), (10, 10)}
    smoothed1_old = smooth_grid_path(branch1, grid, pinned=pinned_terminals)
    # Branch1 would shortcut (5,5)->(15,5), losing the Steiner point (10,5)
    assert (10, 5, 0) not in smoothed1_old, "old behavior loses connection"

    # New behavior: all branch endpoints pinned (terminals + Steiner points)
    all_pinned = set(pinned_terminals)
    for b in (branch1, branch2):
        all_pinned.add((b[0][0], b[0][1]))
        all_pinned.add((b[-1][0], b[-1][1]))

    smoothed1 = smooth_grid_path(branch1, grid, pinned=all_pinned)
    smoothed2 = smooth_grid_path(branch2, grid, pinned=all_pinned)

    # Both branches must preserve the shared connection point
    assert (10, 5, 0) in smoothed1, "Steiner point lost on branch1"
    assert (10, 5, 0) in smoothed2, "Steiner point lost on branch2"
    # Endpoints still preserved
    assert smoothed1[0] == branch1[0] and smoothed1[-1] == branch1[-1]
    assert smoothed2[0] == branch2[0] and smoothed2[-1] == branch2[-1]


# ---------------------------------------------------------------------------
# Detour regression (moved from test_routing_smooth_detour.py): horizontal
# run vs foreign pad. A track running into a foreign pad's repulsion zone
# detours in a wide staircase arc; the smoother must collapse it to a few
# 45°-style legs while keeping every leg walkable (clearance preserved).
# ---------------------------------------------------------------------------

def _detour_grid(H=40, W=70, fc=35, fr=20, excl=6, high=9):
    cost = np.zeros((1, H, W), dtype=np.int16)
    blocked = np.zeros((1, H, W), dtype=bool)
    for r in range(H):
        for c in range(W):
            d = math.hypot(c - fc, r - fr)
            if d <= excl:
                blocked[0, r, c] = True
            elif d <= high:
                cost[0, r, c] = 50  # HIGH_COST pad ring
    return cost, blocked


def _staircase_arc():
    """Manhattan staircase hugging a foreign exclusion disc, as A* emits."""
    path = [(c, 20, 0) for c in range(5, 28)]
    path += [(27, r, 0) for r in range(19, 10, -1)]
    path += [(c, 11, 0) for c in range(28, 44)]
    path += [(43, r, 0) for r in range(12, 21)]
    path += [(c, 20, 0) for c in range(44, 66)]
    return path


def test_detour_arc_collapses_to_few_segments():
    cost, blocked = _detour_grid()
    path = _staircase_arc()
    assert len(path) > 60
    smoothed = smooth_grid_path(
        path, cost, blocked_threshold=100,
        corridor_radius=4, blocked_mask=blocked, hard_radius=1,
    )
    assert smoothed[0] == path[0]
    assert smoothed[-1] == path[-1]
    assert len(smoothed) <= 6, f"stairstep survived: {len(smoothed)} nodes"
    stairs = sum(
        1 for k in range(len(smoothed) - 1)
        if abs(smoothed[k][0] - smoothed[k + 1][0])
        + abs(smoothed[k][1] - smoothed[k + 1][1]) <= 1
    )
    assert stairs == 0, f"{stairs} single-cell stairsteps left: {smoothed}"


def test_detour_legs_keep_clearance():
    """Every emitted leg re-verifies walkable with the full corridor."""
    cost, blocked = _detour_grid()
    smoothed = smooth_grid_path(
        _staircase_arc(), cost, blocked_threshold=100,
        corridor_radius=4, blocked_mask=blocked, hard_radius=1,
    )
    for k in range(len(smoothed) - 1):
        assert _line_walkable(
            smoothed[k][0], smoothed[k][1],
            smoothed[k + 1][0], smoothed[k + 1][1],
            cost[0], None, 100, 4, blocked[0], 1,
        ), f"leg {k} clips foreign copper: {smoothed[k]} -> {smoothed[k + 1]}"


def test_bidirectional_no_start_bias():
    """Forward and reversed inputs converge to the same small count."""
    cost, blocked = _detour_grid()
    path = _staircase_arc()
    fwd = smooth_grid_path(
        path, cost, blocked_threshold=100,
        corridor_radius=4, blocked_mask=blocked, hard_radius=1,
    )
    rev = smooth_grid_path(
        list(reversed(path)), cost, blocked_threshold=100,
        corridor_radius=4, blocked_mask=blocked, hard_radius=1,
    )
    assert abs(len(fwd) - len(rev)) <= 2, (len(fwd), len(rev))
    assert len(fwd) <= 6 and len(rev) <= 6


def _elbow(size=60):
    """Open 90-degree corner with long legs: raw cell-by-cell steps."""
    run = [(5 + i, 5, 0) for i in range(20)]
    run += [(24, 5 + i, 0) for i in range(1, 20)]
    grid = np.zeros((1, size, size), dtype=np.int16)
    return run, grid


_KEEP = dict(keep_min_radius_cells=2.0, keep_max_radius_cells=20.0,
             keep_min_leg_cells=0.6, keep_sample_spacing_cells=0.5)


def test_keep_rule_preserves_open_90_for_fillet():
    """The pull-string must spare a roomy corner so the fitter can arc it."""
    from kicad_autorouter.routing.smooth import smooth_branch_pieces
    run, grid = _elbow()
    smoothed, pieces = smooth_branch_pieces(
        run, grid, blocked_threshold=100,
        max_radius_cells=20.0, min_leg_cells=0.6,
        sample_spacing_cells=0.5, keep_min_radius_cells=2.0,
    )
    assert (24, 5, 0) in smoothed
    assert any(p[0] == "arc" for p in pieces), "kept corner never became an arc"
    # keep=0 is legacy: the diagonal erases the corner, nothing to fillet.
    legacy, legacy_pieces = smooth_branch_pieces(run, grid, blocked_threshold=100)
    assert legacy == [(5, 5, 0), (24, 24, 0)]
    assert not any(p[0] == "arc" for p in legacy_pieces)


def test_keep_rule_still_collapses_staircases():
    """Cell-scale legs admit only nub radii: staircases keep collapsing."""
    from kicad_autorouter.routing.smooth import _greedy_pass
    _, grid = _elbow()
    st = [(5, 5, 0)]
    c, r = 5, 5
    for _ in range(10):
        c += 1
        st.append((c, r, 0))
        r += 1
        st.append((c, r, 0))
    assert len(_greedy_pass(st, grid[0], None, 100, 0, **_KEEP)) == 2


def test_keep_rule_vetoed_by_clearance_gate():
    """A corner whose fillet would clip copper is shortcut as before."""
    from kicad_autorouter.routing.smooth import _greedy_pass
    grid = np.zeros((1, 40, 40), dtype=np.int16)
    grid[0, 6, 14] = 100  # inside the (10,5)->(15,5)->(15,10) fillet band
    # (arc hugs the corner through x in [12.5,15], y in [5,7.5]),
    # off the (10,5)->(15,10) diagonal chord.
    run = [(10, 5, 0), (15, 5, 0), (15, 10, 0)]
    vetoed = _greedy_pass(run, grid[0], None, 100, 0, **_KEEP)
    assert vetoed == [(10, 5, 0), (15, 10, 0)]
    grid[0, 6, 14] = 0
    kept = _greedy_pass(run, grid[0], None, 100, 0, **_KEEP)
    assert kept == run


def test_keep_rule_idempotent():
    """A kept corner survives re-smoothing (forward/backward stability)."""
    from kicad_autorouter.routing.smooth import _smooth_run
    run, grid = _elbow()
    once = _smooth_run(run, grid[0], None, 100, 0, **_KEEP)
    twice = _smooth_run(once, grid[0], None, 100, 0, **_KEEP)
    assert twice == once
    assert (24, 5, 0) in twice
