"""Tests for any-angle path smoothing (routing/smooth.py)."""

import math

import numpy as np
import pytest

from kicad_autorouter.board_model import board_model
from kicad_autorouter.io import add_footprint_to_tree
from kicad_autorouter.routing.output import routes_to_tracks_vias
from kicad_autorouter.routing.pipeline import RoutingParams
from kicad_autorouter.routing.polish import polish_routes
from kicad_autorouter.routing.route_raw import route_raw
from kicad_autorouter.routing.smooth import (
    _line_walkable,
    effort_rungs,
    own_net_soft_mask,
    resolve_config,
    smooth_grid_path,
    smoothed_wire_mm,
)
from kicad_autorouter.sexpr import parse_file

from .helpers import make_fp, zeros_grid as _open


@pytest.mark.unit
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


@pytest.mark.unit
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


@pytest.mark.unit
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


@pytest.mark.unit
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


@pytest.mark.unit
def test_corridor_radius_blocks_tight_shortcut():
    # A blocked cell adjacent to the diagonal: passable with radius 0,
    # blocked once the track footprint (radius 1) is considered.
    grid = _open()
    grid[0, 6, 8] = 100
    path = [(5, 5, 0), (10, 5, 0), (10, 10, 0)]
    assert len(smooth_grid_path(path, grid, corridor_radius=0)) == 2
    assert len(smooth_grid_path(path, grid, corridor_radius=1)) > 2


@pytest.mark.unit
def test_endpoints_and_short_paths_untouched():
    grid = _open()
    assert smooth_grid_path([], grid) == []
    assert smooth_grid_path([(1, 1, 0)], grid) == [(1, 1, 0)]
    two = [(1, 1, 0), (5, 5, 0)]
    assert smooth_grid_path(two, grid) == two


@pytest.mark.unit
def test_high_cost_ring_blocks_shortcut_without_mask():
    # Another net's pad clearance ring (HIGH_COST) must block shortcuts —
    # blindly cutting through it caused DRC clearance/short violations.
    grid = _open()
    grid[0, 7:10, 7:10] = 50
    path = [(5, 5, 0), (10, 5, 0), (10, 10, 0)]
    assert len(smooth_grid_path(path, grid)) > 2


@pytest.mark.unit
def test_soft_mask_allows_escape_through_own_pads():
    # The routed net's own pad ring is crossable (the track must escape).
    grid = _open()
    grid[0, 4:8, 4:8] = 50
    mask = np.zeros_like(grid, dtype=bool)
    mask[0, 4:8, 4:8] = True
    path = [(5, 5, 0), (10, 5, 0), (12, 8, 0)]
    assert len(smooth_grid_path(path, grid, soft_mask=mask)) == 2
    assert len(smooth_grid_path(path, grid)) > 2


@pytest.mark.unit
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


@pytest.mark.unit
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


@pytest.mark.unit
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


@pytest.mark.unit
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


@pytest.mark.unit
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


@pytest.mark.unit
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


@pytest.mark.unit
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


@pytest.mark.unit
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


@pytest.mark.unit
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


@pytest.mark.unit
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


@pytest.mark.unit
def test_keep_rule_idempotent():
    """A kept corner survives re-smoothing (forward/backward stability)."""
    from kicad_autorouter.routing.smooth import _smooth_run
    run, grid = _elbow()
    once = _smooth_run(run, grid[0], None, 100, 0, **_KEEP)
    twice = _smooth_run(once, grid[0], None, 100, 0, **_KEEP)
    assert twice == once
    assert (24, 5, 0) in twice


# ---------------------------------------------------------------------------
# Smoothing effort levels and the Polish re-smoothing pass.
# (Folded from test_smooth_polish.py; refine-ordering tests moved to
# test_order_refine.py. All synthetic + fast, 0.5mm grid.)
# ---------------------------------------------------------------------------

CORNERS = {
    "XNW": (65.0, 55.0),
    "XNE": (205.0, 55.0),
    "XSW": (65.0, 135.0),
    "XSE": (205.0, 135.0),
}


def _xcross_model():
    pcb_tree = parse_file("tests/fixtures/minimal.kicad_pcb")
    specs = [
        ("XNW", "xcross-nw-uuid-0001", *CORNERS["XNW"], "X1", "STUB_NW"),
        ("XNE", "xcross-ne-uuid-0002", *CORNERS["XNE"], "X2", "STUB_NE"),
        ("XSW", "xcross-sw-uuid-0003", *CORNERS["XSW"], "X2", "STUB_SW"),
        ("XSE", "xcross-se-uuid-0004", *CORNERS["XSE"], "X1", "STUB_SE"),
    ]
    for spec in specs:
        pcb_tree = add_footprint_to_tree(
            pcb_tree,
            make_fp(spec[0], spec[1], spec[2], spec[3], (spec[4], spec[5]),
                    footprint_id="Test:R_0805_XCross"),
            "Test:R_0805_XCross")
    return pcb_tree, board_model(pcb_tree)


def _xparams(**kw):
    base = dict(grid_resolution_mm=0.5, run_drc=False, refill_zones=False)
    base.update(kw)
    return RoutingParams(**base)


@pytest.mark.unit
@pytest.mark.parametrize("effort,arcs,keep,sample", [
    (2.0, True, 0.0, 0.05),  # default: shortcut-everything, no keep-cut
    (1.0, False, 0.0, None),  # quick: no arcs or keep
    (1.75, True, 0.0, None),  # fractional interpolates keep (from 0.0)
    (3.0, True, 1.0, 0.025),  # hot rung
])
@pytest.mark.unit
def test_resolve_config_anchors(effort, arcs, keep, sample):
    cfg = resolve_config(effort, _xparams())
    assert cfg["fit_arcs"] is arcs
    assert cfg["keep_min_radius_mm"] == pytest.approx(keep)
    if sample is not None:
        assert cfg["sample_mm"] == pytest.approx(sample)


@pytest.mark.unit
def test_effort_rungs_shapes():
    assert effort_rungs(2.0) == [2.0]
    assert effort_rungs(1.0) == [1.0]
    assert effort_rungs(2.5) == [2.0, 2.5]
    assert effort_rungs(3.0) == [2.0, 2.5, 3.0]
    assert max(len(effort_rungs(e)) for e in (0.0, 1.0, 2.0, 2.9, 3.0)) <= 3


@pytest.mark.unit
def test_smooth_effort_validation():
    assert RoutingParams.from_dict({"smooth_effort": 3}).smooth_effort == 3
    assert RoutingParams().to_dict()["smooth_effort"] == 2.0
    for bad in (-0.5, 3.5, float("nan"), True, "high"):
        with pytest.raises(ValueError):
            RoutingParams.from_dict({"smooth_effort": bad})


def _max_step(branches):
    steps = []
    for branch in branches:
        for (c1, r1, _), (c2, r2, _) in zip(branch, branch[1:]):
            steps.append(max(abs(c2 - c1), abs(r2 - r1)))
    return max(steps) if steps else 0


@pytest.mark.unit
@pytest.mark.parametrize("effort", [0.0, 1.0])
@pytest.mark.minimal
def test_low_effort_emits_no_arcs(effort):
    _, model = _xcross_model()
    raw, _, _ = route_raw(model, ["X1", "X2"], set(),
                          params=_xparams(smooth_effort=effort))
    assert raw.nets_routed == 2
    for net, result in raw.all_results.items():
        pieces = tuple(getattr(result, "arc_pieces", ()) or ())
        assert all(p == () for p in pieces)  # no fitted arcs emitted
    if effort == 0.0:
        # Raw emission: no shortcut chords either.
        for result in raw.all_results.values():
            branches = list(getattr(result, "branches", ()))
            assert _max_step(branches) <= 1


@pytest.mark.minimal
def test_single_pass_routing_is_deterministic():
    # Route time takes one smoothing pass at the requested effort (retries
    # live in polish): the same params must reproduce the same copper.
    for effort in (2.0, 3.0):
        _, model = _xcross_model()
        first, _, _ = route_raw(model, ["X1", "X2"], set(),
                                params=_xparams(smooth_effort=effort))
        _, model = _xcross_model()
        second, _, _ = route_raw(model, ["X1", "X2"], set(),
                                 params=_xparams(smooth_effort=effort))
        assert first.nets_routed == second.nets_routed == 2
        for net in ("X1", "X2"):
            assert (first.all_results[net].wire_mm ==
                    second.all_results[net].wire_mm)


@pytest.mark.minimal
def test_polish_noop_at_zero_effort():
    tree, model = _xcross_model()
    raw, _, rg = route_raw(model, ["X1", "X2"], set(), params=_xparams())
    new, report = polish_routes(raw.all_results, raw.width_map, raw.model,
                                rg, _xparams(), ["X1", "X2"], 0.0)
    assert report["skipped"] == 2
    assert new == raw.all_results


@pytest.mark.unit
def test_polish_prefers_roomiest_within_ten_percent():
    # Rung pick is margin-scored inside a 10% wire band: a 5%-longer swoop
    # with a 1.0mm gap beats a grazer at 0.21mm; a 20%-longer detour with
    # an even bigger gap stays out of band and loses to the shortest.
    from types import SimpleNamespace
    from unittest import mock

    from kicad_autorouter.routing import polish as polish_mod
    from kicad_autorouter.routing.polish import _polish_one

    def _cand(wire):
        return SimpleNamespace(branches=[[(0, 0, 0), (10, 0, 0)]],
                              arc_pieces=(), wire_mm=wire)

    grazer, swoop, detour = _cand(100.0), _cand(105.0), _cand(120.0)
    current = SimpleNamespace(success=True, path=[(0, 0)],
                              branches=[[(0, 0, 0), (10, 0, 0)]],
                              arc_pieces=(), wire_mm=110.0)
    grid = mock.MagicMock()
    base = mock.MagicMock()
    base.copy.return_value = mock.MagicMock()
    model = mock.MagicMock()
    model.board_regions = ()

    def _run(cands, margins):
        with mock.patch.object(polish_mod, "effort_rungs",
                               return_value=[2.0] * len(cands)), \
             mock.patch.object(polish_mod, "_smooth_result",
                               side_effect=list(cands)), \
             mock.patch.object(polish_mod, "fitted_pieces_violate",
                               return_value=[]), \
             mock.patch.object(polish_mod, "gate_touch_margin",
                               side_effect=[(False, m) for m in margins]), \
             mock.patch.object(polish_mod, "build_fanout_cost",
                               return_value=np.zeros((1, 4, 4),
                                                     dtype=np.int16)), \
             mock.patch.object(polish_mod, "own_net_hard_cells",
                               return_value=None), \
             mock.patch.object(polish_mod, "_board_region_masks",
                               return_value=[]), \
             mock.patch.object(polish_mod, "_apply_route_to_grid"), \
             mock.patch.object(polish_mod, "_terminals_for_net",
                               return_value=[]), \
             mock.patch.object(polish_mod, "own_net_soft_mask",
                               return_value=None):
            return _polish_one("S", current, 0.25, model, model, grid,
                               base, {}, {"S": 0.25}, _xparams(), 3.0,
                               0.1, [], False, ())

    assert _run([grazer, swoop], [0.21, 1.0]) is swoop
    assert _run([grazer, detour], [0.21, 5.0]) is grazer


@pytest.mark.minimal
def test_polish_never_grows_wire_and_converts():
    tree, model = _xcross_model()
    raw, _, rg = route_raw(model, ["X1", "X2"], set(),
                           params=_xparams(smooth_effort=1.0))
    before = {n: r.wire_mm for n, r in raw.all_results.items()}
    new, report = polish_routes(raw.all_results, raw.width_map, raw.model,
                                rg, _xparams(), ["X1", "X2", "NOPE"], 3.0)
    assert report["polished"] + report["kept"] + report["reverted"] + report["skipped"] == 3
    assert report["nets"]["NOPE"]["status"] == "skipped"
    assert report["wire_saved_mm"] >= 0.0
    for net, entry in report["nets"].items():
        if net == "NOPE":
            continue
        assert entry["wire_after_mm"] <= entry["wire_before_mm"] + 1e-9
        assert entry["wire_before_mm"] == pytest.approx(before[net])
    # Polished branches still convert to board copper. Mirror the server
    # polish conversion call (ui/server_jobs.py) kwarg-for-kwarg — a wrong
    # keyword there once broke Polish end to end while this test stayed green.
    xp = _xparams()
    tracks, vias, arcs = routes_to_tracks_vias(
        new, rg, net_class_widths=raw.width_map,
        default_width_mm=xp.track_width_mm,
        power_width_mm=xp.power_width_mm,
        via_size_mm=xp.via_size_mm,
        via_drill_mm=xp.via_drill_mm,
        tht_sites={},
        extra_power=())
    assert tracks and all(t.net_name in ("X1", "X2") for t in tracks)
