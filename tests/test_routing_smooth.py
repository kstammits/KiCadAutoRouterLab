"""Tests for any-angle path smoothing (routing/smooth.py)."""

import numpy as np

from kicad_autorouter.routing.smooth import (
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
