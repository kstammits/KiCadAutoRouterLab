"""Detour regression: horizontal run vs foreign pad (three-pads-collinear).

A track running horizontally into a foreign pad's zone of repulsion detours
in a wide staircase arc. The old forward-only greedy smoother kept the
whole staircase (reported: 130 segments after smoothing). The smoother must
collapse it to a few 45°-style legs while keeping every leg walkable under
the same conservative corridor (clearance preserved).
"""

import math

import numpy as np

from kicad_autorouter.routing.smooth import _line_walkable, smooth_grid_path


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
